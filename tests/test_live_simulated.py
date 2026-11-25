"""End-to-end LiveSession run against a simulated robot (no CAN, no GPU).

用模拟机械臂端到端运行 LiveSession（不连 CAN、不用 GPU）。
Real pieces: config resolution, native IK worker, guard, scheduler, observer, recorder.
Fake pieces: arm + cameras (ideal joint sink), policy worker, keyboard.
真实组件：配置解析、原生 IK 子进程、指令保护、调度器、观测器、记录器；
模拟组件：机械臂与相机（理想关节执行）、策略进程、键盘。
"""
import sys
import threading
import time
from pathlib import Path

import numpy as np
import pytest

from quicdeploy.common.geometry import relative_prefix, to6
from quicdeploy.common.io import PROJECT_ROOT
from quicdeploy.common.layout import JOINT_COLUMNS

CONFIG = PROJECT_ROOT / "configs/runtime.yaml"


def _config(model_card=None, config_path=CONFIG, **execution):
    from quicdeploy.config.resolve import resolve

    try:
        config = resolve(config_path, model_card=model_card)
    except (FileNotFoundError, ValueError) as exc:
        pytest.skip(f"deployment files unavailable on this host: {exc}")
    if not Path(config["kinematics"]["python"]).exists():
        pytest.skip("IK python env unavailable")
    config["execution"].update(execution)
    return config


class FakeCamera:
    def __init__(self):
        from collections import deque
        self.frame_lock = threading.Lock()
        self.fps, self.frame_index = 30, 0
        self.frame_history = deque(maxlen=1)
        self.latest_frame = None
        self.latest_timestamp = None
        self._stop = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def enable_history(self, capacity):
        from collections import deque
        with self.frame_lock:
            self.frame_history = deque(self.frame_history, maxlen=capacity)

    def _run(self):
        frame = np.full((480, 640, 3), 128, np.uint8)
        while not self._stop.is_set():
            with self.frame_lock:
                self.latest_frame, self.latest_timestamp = frame, time.monotonic()
                self.frame_history.append((self.frame_index, self.latest_timestamp, frame))
                self.frame_index += 1
            time.sleep(1 / 30)


class FakeArm:
    """Ideal sink: measured joints become exactly the last command. / 理想执行：实测即指令。"""
    instances = []

    def __init__(self, spec, *, rate_hz=None):
        self.cameras = {name: FakeCamera() for name in ("left_wrist", "head", "right_wrist")}
        self.state = np.zeros(14)
        self.state[JOINT_COLUMNS] = np.tile([0.0, 40.0, -50.0, 40.0, 0.0, 0.0], 2)
        self.state[[6, 13]] = [35.0, 50.0]
        self.commands = self.holds = self.rehomes = 0
        self.position_holds = []
        FakeArm.instances.append(self)

    def connect(self, *, pause_requested):
        paused = pause_requested()
        # Like OpenCVCamera warmup: wait for a first frame. / 与真实相机预热一致，等待首帧。
        while any(camera.latest_frame is None for camera in self.cameras.values()):
            time.sleep(0.01)
        return not paused

    def read_joints(self):
        return self.state.copy()

    def send(self, command):
        self.state = np.asarray(command, float).copy()
        self.commands += 1
        return self.state.copy()

    def hold(self, *, current=False):
        self.holds += 1
        if current:
            self.position_holds.append(self.commands)

    def rehome(self, *, pause_requested):
        self.rehomes += 1
        return not pause_requested()

    def close(self):
        for camera in self.cameras.values():
            camera._stop.set()


class FakeWorker:
    """Returns a slow +x drift chunk after 120 ms, honouring the RTC prefix.
    120ms 后返回一段缓慢沿 +x 漂移的动作块，并保留 RTC 前缀。
    """

    def __init__(self, config, *, record_diagnostics=False):
        self.pending = None
        self.temporal = config.get("temporal_sampling")
        self.vision_session, self.vision_index = None, None

    def vision_cursor(self, session):
        return self.vision_index if session == self.vision_session else None

    def offer_vision(self, sample):
        self.vision_session, self.vision_index = sample["session"], sample["frame_index"]
        return True

    def submit(self, observation, context):
        assert self.pending is None
        if self.temporal:
            history = observation["vision_history"]
            count, stride = self.temporal["history_frames"], self.temporal["history_stride"]
            assert len(history["samples"]) == count
            assert history["time_offsets"] == list(range(-(count - 1) * stride, 1, stride))
        self.pending = (time.monotonic(), observation, context)

    def poll(self):
        if self.pending is None or time.monotonic() - self.pending[0] < 0.12:
            return None
        started, observation, context = self.pending
        self.pending = None
        raw = np.zeros((50, 20))
        raw[:, 3:9] = raw[:, 13:19] = to6(np.eye(3))
        raw[:, 0] = raw[:, 10] = 0.0005 * np.arange(1, 51)
        raw[:, 9], raw[:, 19] = observation["eef_state"][9], observation["eef_state"][19]
        if context["prefix_absolute"] is not None:
            k = len(context["prefix_absolute"])
            raw[:k] = relative_prefix(context["prefix_absolute"], observation["eef_state"])
        return {"raw": raw, "roundtrip_s": time.monotonic() - started, "inference_s": 0.1}

    def wait(self, timeout=60, *, on_tick=None):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if on_tick is not None:
                on_tick()
            reply = self.poll()
            if reply is not None:
                return reply
            time.sleep(0.001)
        raise TimeoutError

    def close(self):
        pass


class FakeControls:
    """Scripted keys: LEFT at 2.0 s, RIGHT at 2.6 s. / 预设按键：2.0s 按左，2.6s 按右。"""

    def __init__(self):
        self.t0 = None
        self.script = [(2.0, "home"), (2.6, "resume")]

    def open(self):
        self.t0 = time.monotonic()

    def poll(self):
        self.check_pause()
        if self.t0 is None:
            return []
        now = time.monotonic() - self.t0
        due = [key for at, key in self.script if at <= now]
        self.script = [(at, key) for at, key in self.script if at > now]
        return due

    def pause_requested(self):
        return False

    def check_pause(self):
        from quicdeploy.runtime.controls import PauseRequested

        if self.pause_requested():
            raise PauseRequested("SPACE pressed")

    def discard(self):
        self.check_pause()

    def close(self):
        pass


@pytest.fixture
def patched(monkeypatch, tmp_path):
    import quicdeploy.runtime.live as live
    from quicdeploy.runtime.recorder import RunRecorder

    FakeArm.instances.clear()
    monkeypatch.setattr(live, "PiperDualArm", FakeArm)
    monkeypatch.setattr(live, "InferenceWorker", FakeWorker)
    monkeypatch.setattr(live, "TerminalControls", FakeControls)
    monkeypatch.setattr(live, "ARM_LOCK_PATH", str(tmp_path / "arm.lock"))
    monkeypatch.setattr(live, "RunRecorder", lambda config, diagnostics: RunRecorder(
        config, diagnostics=diagnostics, root=tmp_path))
    monkeypatch.setattr(sys, "stdin", type("TTY", (), {"isatty": lambda self: True})())
    return live


@pytest.mark.parametrize("mode", ["async_rtc", "sync"])
@pytest.mark.parametrize("config_path,model_card", [
    (CONFIG, None),
    (CONFIG, PROJECT_ROOT / "configs/models/quicforge_pick_kun_ep050_step43700_fp16.json"),
    (PROJECT_ROOT / "configs/temporal_lva_update_00050000.yaml", None),
])
def test_live_session_simulated(patched, mode, config_path, model_card):
    extra = {} if mode == "async_rtc" else dict(mode="sync", prefix_steps=0, prefetch_remaining_steps=0)
    config = _config(model_card=model_card, config_path=config_path, **extra)
    result = patched.LiveSession(config, diagnostics=True, duration_s=4.0).run()
    arm = FakeArm.instances[-1]
    assert result["status"] == "paused" or result["status"] == "passed", result
    assert arm.commands > 50 and result["hardware_commands"] == arm.commands
    assert arm.rehomes == 1 and result["home_events"][0]["status"] == "paused_at_initial"
    assert len(result["prediction_events"]) >= 3
    if mode == "async_rtc":
        # Prefetched requests after the first carry the 10-row RTC prefix.
        # 首块之后的预取请求都带 10 行 RTC 前缀。
        assert any(event["prefix_source_rows"] for event in result.get("queue_request_events", []))
    events = result["inference_events"]
    assert len(events) >= len(result["prediction_events"])
    assert all(event["latency_ms"] > 0 and event["model_ms"] == 100 for event in events)
    assert all(event["latency_equivalent_hz"] > 0 for event in events)
    assert [event["sequence"] for event in events] == list(range(1, len(events) + 1))
    assert "error" not in result, result.get("error")


@pytest.mark.parametrize("phase", ["startup", "pending", "active", "rehome"])
def test_live_space_pauses_and_resumes_from_fresh_observation(patched, monkeypatch, phase):
    session = patched.LiveSession(_config(), duration_s=2.0)
    session.controls.script = [(0.05, "home")] if phase == "rehome" else []
    pressed = False
    paused_ticks = 0
    requests = []
    original_poll = session.controls.poll
    original_submit = FakeWorker.submit

    def press_space():
        nonlocal pressed
        if pressed:
            return False
        homing = bool(session.result.get("home_events"))
        pressed = (phase == "startup" and session.robot is None
                   or phase == "pending" and session.pending_request is not None
                   or phase == "active" and session.robot is not None and session.robot.commands > 0
                   or phase == "rehome" and homing)
        return pressed

    def keys():
        nonlocal paused_ticks
        if session.operator_paused:
            arm = FakeArm.instances[-1]
            assert arm.commands == arm.position_holds[-1]
            paused_ticks += 1
            if paused_ticks == 1:
                return ["home"]  # Position hold requires an explicit RIGHT.
            if paused_ticks == 2:
                return ["resume"]
        return original_poll()

    def capture_submit(worker, observation, context):
        requests.append((context["anchor_index"], context["prefix_absolute"] is None))
        return original_submit(worker, observation, context)

    monkeypatch.setattr(session.controls, "pause_requested", press_space)
    monkeypatch.setattr(session.controls, "poll", keys)
    monkeypatch.setattr(FakeWorker, "submit", capture_submit)
    result = session.run()
    arm = FakeArm.instances[-1]
    pause, = result["operator_pause_events"]
    discarded = pause["discarded_request_step"]
    assert pressed and paused_ticks >= 2
    assert result["startup_homing_completed"] == (phase != "startup")
    assert arm.rehomes == (1 if phase == "rehome" else 0)
    assert len(arm.position_holds) == 1
    assert result["hardware_commands"] == arm.commands > arm.position_holds[0]
    assert "error" not in result and "cleanup_errors" not in result
    if phase == "pending":
        assert discarded == 0 and requests[:2] == [(0, True), (1, True)]
        assert discarded not in {event["step"] for event in result["prediction_events"]}
    if phase == "rehome":
        assert result["home_events"][0]["status"] == "paused_in_place"
