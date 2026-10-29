"""Private per-arm worker. Only this process imports the upstream MIT driver.

单臂私有子进程：只有本进程会导入上游 MIT 驱动（vr_teleop.follower.piper.can.PiperCanArm）。

Vendored verbatim from the original bi_piper_native backend: hardware control
logic is intentionally unchanged. It runs as a standalone script under the
`quest_python` env and imports `startup` from its own directory.
从原 bi_piper_native 后端原样迁移，硬件控制逻辑刻意保持不变。
它以独立脚本方式在 quest_python 环境中运行，并从同目录导入 startup 模块。

The socket is inherited from the parent; there is no network listener. Importing
this module does not load robot SDKs or connect hardware.
通信 socket 由父进程继承而来，没有网络监听；仅导入本模块不会加载 SDK 或连接硬件。

Main loop (serve) / 主循环: every 1/rate_hz run controller.tick() — follow the last
accepted target with the MIT profile, or keepalive-hold once it is older than
command_timeout_s — then answer parent requests (observe / hold / action / home).
每 1/rate_hz 执行一次 tick：用 MIT 轨迹跟随最后接受的目标，目标超过 command_timeout_s
未更新则原地保持；其余时间响应父进程请求。
"""

import argparse
import contextlib
import fcntl
import json
import math
import os
import select
import signal
import socket
import sys
import time
from pathlib import Path


def vector(values, length):
    if not isinstance(values, list) or len(values) != length:
        raise ValueError(f"Expected {length} numbers")
    if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) for v in values):
        raise ValueError("Invalid finite vector")
    return [float(v) for v in values]


class FeedbackReceipts:
    """Record actual decoded CAN arrivals using a clock unaffected by NTP."""

    def __init__(self, arm, clock=time.monotonic):
        self.clock = clock
        self.received = {}
        self.groups = {}
        for parser, names in (
            (arm._agx._parser, ("joint_12", "joint_34", "joint_56")),
            (arm._effector._parser, ("gripper",)),
        ):
            for can_id, spec in parser._rx_map.items():
                if spec[0] in names:
                    self.groups[can_id] = spec[0]
        if set(self.groups.values()) != {"joint_12", "joint_34", "joint_56", "gripper"}:
            raise RuntimeError("SDK feedback receive mapping is incomplete")
        # Registered after the SDK decoders: failed decoding cannot refresh age.
        arm._agx.get_context().register_parser_packet_fun(self.on_frame)

    def on_frame(self, frame):
        if frame.is_error_frame or frame.is_remote_frame:
            return
        name = self.groups.get(frame.arbitration_id)
        if name is not None:
            self.received[name] = self.clock()

    def check(self, name, timeout):
        # Aggregate joint state uses the three separately checked frame groups.
        if name == "joint":
            return
        stamp = self.received.get(name)
        if stamp is None:
            raise RuntimeError(f"No received {name} feedback since monitor startup")
        age = self.clock() - stamp
        if not math.isfinite(age) or age < 0 or age > timeout:
            raise RuntimeError(
                f"Stale {name} feedback: monotonic receive age={age:.6f}s, limit={timeout:.3f}s"
            )


class NativeController:
    def __init__(self, arm, config, limits, clock=time.monotonic):
        self.arm, self.config, self.limits, self.clock = arm, config, limits, clock
        self.target = None
        self.command_at = 0.0
        self.state = None
        self.seen = {}
        self.changed_at = {}
        self.fault = None

    def set_fault(self, exc, operation):
        """Keep the first failure in the worker log, not only the parent socket."""
        self.target = None
        if self.fault is not None:
            return
        self.fault = str(exc)
        print(
            "[native-worker-fault] "
            + json.dumps(
                {
                    "time_unix_s": time.time(),
                    "side": self.config.get("side"),
                    "can": self.config.get("can"),
                    "operation": operation,
                    "error_type": type(exc).__name__,
                    "error": self.fault,
                },
                ensure_ascii=False,
            ),
            flush=True,
        )

    def _stamp(self, name, stamp, now):
        if stamp is None or not math.isfinite(float(stamp)) or float(stamp) <= 0:
            raise RuntimeError(f"No timestamp on {name} feedback")
        receipts = getattr(self.arm, "_feedback_receipts", None)
        if receipts is not None:
            receipts.check(name, self.config["feedback_timeout_s"])
            return
        # Legacy/test arms without the SDK receive hook retain strict cache checks.
        # CAN timestamps are Unix seconds in this SDK. Reject a frozen cache on
        # the first read as well as a timestamp that stops advancing later.
        if abs(time.time() - float(stamp)) > self.config["feedback_timeout_s"]:
            raise RuntimeError(
                f"Stale {name} feedback timestamp: age={time.time() - float(stamp):.6f}s, "
                f"limit={self.config['feedback_timeout_s']:.3f}s"
            )
        if self.seen.get(name) != stamp:
            self.seen[name] = stamp
            self.changed_at[name] = now
        if now - self.changed_at[name] > self.config["feedback_timeout_s"]:
            raise RuntimeError(f"Stale {name} feedback")

    def observe(self):
        now = self.clock()
        # SDK aggregate timestamp only covers the last joint pair. Require all
        # three frame groups so a dead J1/J2 channel cannot appear fresh.
        parser = self.arm._agx._parser
        for group in ("joint_12", "joint_34", "joint_56"):
            frame = getattr(parser, group, None)
            if frame is None:
                raise RuntimeError(f"Missing {group} feedback")
            self._stamp(group, frame.timestamp, now)
        joints = self.arm._agx.get_joint_angles()
        gripper = self.arm._effector.get_gripper_status() if self.arm._effector is not None else None
        if joints is None or gripper is None:
            raise RuntimeError("Missing measured joint/gripper feedback")
        self._stamp("joint", joints.timestamp, now)
        self._stamp("gripper", gripper.timestamp, now)
        q = vector(list(joints.msg), 6)
        if getattr(gripper.msg, "mode", "width") != "width":
            raise RuntimeError("Gripper feedback must use width mode")
        width = vector([gripper.msg.value], 1)[0] * 1000.0
        self.state = [*[math.degrees(v) for v in q], width]
        return self.state

    def validate_target(self, values):
        values = vector(values, 7)
        for value, (lower, upper) in zip(values[:6], self.limits, strict=True):
            if not lower <= value <= upper:
                raise ValueError("Joint target outside native model limits")
        if not 0 <= values[6] <= self.config["gripper_width_mm"]:
            raise ValueError("Gripper target outside configured stroke")
        return values

    def command(self, values, sent_at):
        now = self.clock()
        if not isinstance(sent_at, (int, float)) or not math.isfinite(sent_at):
            raise ValueError("Invalid command timestamp")
        if not 0 <= now - sent_at <= self.config["command_timeout_s"]:
            raise RuntimeError("Expired command")
        values = self.validate_target(values)
        self.observe()
        if self.arm._is_teaching():
            raise RuntimeError("Arm is in teaching/leader mode")
        self.target, self.command_at = values, now
        return values

    def tick(self):
        if self.fault:
            self.target = None
            self.arm.keepalive()
            return
        self.observe()
        if self.arm._is_teaching():
            raise RuntimeError("Arm is in teaching/leader mode; command rejected")
        if self.target is None or self.clock() - self.command_at > self.config["command_timeout_s"]:
            # Native keepalive zeros desired velocity and holds the last emitted
            # MIT position, rather than continuing toward an expired IK target.
            self.target = None
            self.arm.keepalive()
        else:
            import numpy as np

            # Input is already an absolute, startup-aligned joint goal. Reuse
            # the upstream MIT profile exactly, without applying its IK offset
            # a second time (or wrapping absolute J6 goals at +/-180 degrees).
            self.arm.read()
            self.arm._gripper_cmd = self.target[6] / self.config["gripper_width_mm"]
            self.arm._follow_mit(np.deg2rad(self.target[:6]), self.clock())
            if self.clock() - self.arm._last_send_at > self.config["command_timeout_s"]:
                raise RuntimeError("Native MIT driver is not completing sends")

    def reply(self, request):
        seq = request.get("sequence")
        try:
            if self.fault:
                raise RuntimeError(self.fault)
            op = request["operation"]
            if op == "observe":
                result = {"observation": self.observe()}
            elif op == "hold":
                self.target = None
                self.arm.keepalive()
                result = {"observation": self.observe()}
            elif op == "action":
                result = {"action": self.command(request["action"], request["sent_at"])}
            else:
                raise ValueError("Unknown worker operation")
            return {"sequence": seq, "ok": True, **result}
        except (RuntimeError, ValueError, KeyError, TypeError) as exc:
            self.set_fault(exc, request.get("operation", "unknown"))
            return {"sequence": seq, "ok": False, "error": self.fault}


def serve(sock, controller, startup=None):
    period = 1.0 / controller.config["rate_hz"]
    next_tick = time.monotonic()
    sock.settimeout(0.25)
    while True:
        now = time.monotonic()
        if now >= next_tick:
            try:
                controller.tick()
            except Exception as exc:
                controller.set_fault(exc, "tick")
                # Do not continue producing new movement targets after a fault.
                # Keep native hold torque while the owning client tears down.
                with contextlib.suppress(Exception):
                    controller.arm.keepalive()
            next_tick = time.monotonic() + period
        readable, _, _ = select.select([sock], [], [], max(0.0, next_tick - time.monotonic()))
        if not readable:
            continue
        raw = sock.recv(16384)
        if not raw:
            break
        request = json.loads(raw)
        if request.get("operation") == "close":
            break
        reply = (
            rehome(controller, request, startup, sock)
            if request.get("operation") == "home"
            else controller.reply(request)
        )
        sock.sendall(json.dumps(reply, allow_nan=False).encode())


def initialization_clock(sock, directory, clock=time.monotonic):
    """Check parent cancellation and peer failure before each homing iteration."""

    def checked():
        if (Path(directory) / "failed").exists():
            raise RuntimeError("Peer initialization failed; stop homing: " + (Path(directory) / "failed").read_text().strip())
        readable, _, _ = select.select([sock], [], [], 0)
        if readable:
            raise RuntimeError("Parent cancelled/disconnected during initialization")
        return clock()

    return checked


def move_episode_to_pose(
    arm, target, observe, config, *, startup=False, clock=time.monotonic, pause=time.sleep,
):
    """Smooth position/velocity references sent through the original MIT driver."""
    import numpy as np

    stable, lag_since = None, None
    next_log = 0.0
    speed, acceleration = arm.max_deg_s, arm.max_acc_deg_s2
    phase = "startup" if startup else "episode"
    timeout = 90.0 if startup else 60.0
    arm.max_deg_s = config[f"{phase}_home_speed_deg_s"]
    arm.max_acc_deg_s2 = config[f"{phase}_home_acc_deg_s2"]
    try:
        # Start from fresh measured joints, not the old VR/IK goal.
        actual = observe()
        start = actual.copy()
        started = clock()
        arm._cmd = actual.copy()
        arm._mit_vel[:] = 0
        arm._mit_acc[:] = 0
        distance_deg = float(np.max(np.abs(np.rad2deg(target - start))))
        # Quintic ease-in/out: peak slope 1.875, peak curvature 10/sqrt(3).
        duration = max(
            1.0,
            1.875 * distance_deg / arm.max_deg_s,
            math.sqrt((10 / math.sqrt(3)) * distance_deg / arm.max_acc_deg_s2),
        )
        if duration + 3.0 >= timeout:
            raise ValueError("Configured homing speed is too low for this return distance")
        while clock() - started < timeout:
            now = clock()
            actual = observe()
            if arm._is_teaching():
                raise RuntimeError("Teaching mode during homing")
            arm.joints_rad = actual.copy()
            lag = float(np.max(np.abs(np.rad2deg(actual - arm._cmd))))
            lag_since = (now if lag_since is None else lag_since) if lag > 15.0 else None
            if lag_since is not None and now - lag_since > 0.5:
                raise RuntimeError(f"Homing tracking error exceeds 15 degrees: joint={int(np.argmax(np.abs(actual-arm._cmd)))+1}, error_deg={lag:.3f}, measured_deg={np.rad2deg(actual).tolist()}, command_deg={np.rad2deg(arm._cmd).tolist()}")
            # Do not catch up with a large setpoint step after a scheduler stall.
            if now - arm._last_send_at > max(0.1, arm.min_dt * 5):
                raise RuntimeError("MIT output stalled during homing")
            progress = min(1.0, max(0.0, (now - started) / duration))
            blend = progress**3 * (10 + progress * (-15 + 6 * progress))
            velocity = 30 * progress**2 * (1 - progress) ** 2 / duration
            profile_acceleration = 60 * progress * (1 - progress) * (1 - 2 * progress) / duration**2
            # Absolute interpolation avoids a wrapped short path across joint limits.
            # Use the original MIT sender directly: the VR profile's settled-axis
            # velocity snap would reintroduce acceleration spikes near this goal.
            arm._cmd = start + (target - start) * blend
            arm._mit_vel = (target - start) * velocity
            arm._mit_acc = (target - start) * profile_acceleration
            arm._hold_changed_at = now
            arm.send_joints(arm._cmd, force=True, vel=arm._mit_vel)
            if now >= next_log:
                print("[homing-sample] " + json.dumps({
                    "time_monotonic": now, "side": config.get("side"), "phase": phase,
                    "progress": progress, "measured_deg": np.rad2deg(actual).tolist(),
                    "command_deg": np.rad2deg(arm._cmd).tolist(),
                    "velocity_deg_s": np.rad2deg(arm._mit_vel).tolist(),
                    "tracking_error_deg": lag, "send_period_s": arm.min_dt,
                }), flush=True)
                next_log = now + 0.2
            if now - arm._last_send_at > max(0.1, arm.min_dt * 5):
                raise RuntimeError("MIT output stalled during homing")
            arrived = (
                progress == 1.0
                and float(np.max(np.abs(np.rad2deg(actual - target)))) <= 3.0
                and float(np.max(np.abs(np.rad2deg(arm._cmd - target)))) <= 0.1
                and float(np.max(np.abs(np.rad2deg(arm._mit_vel)))) <= 0.5
            )
            stable = (now if stable is None else stable) if arrived else None
            if stable is not None and now - stable >= 0.6:
                arm._mit_vel[:] = 0
                arm._ik_ref = arm._q_ref = None
                arm._start_joints = arm._cmd.copy()
                return
            pause(arm.min_dt)
        raise TimeoutError(f"{phase.capitalize()} homing did not settle within {timeout:g} seconds")
    except BaseException as exc:
        # Do not let upstream close() re-send the distant failed homing goal.
        # Only re-anchor to feedback that passes the same freshness checks.
        try:
            measured = np.asarray(observe(), dtype=float)
            if measured.shape != (6,) or not np.isfinite(measured).all():
                raise ValueError("Invalid abort feedback")
            arm._cmd = measured.copy()
            arm._mit_q_send = measured.copy()
            arm._mit_vel[:] = 0
            arm._mit_acc[:] = 0
            print("[homing-abort] " + json.dumps({"side": config.get("side"),
                "error": str(exc), "hold_measured_deg": np.rad2deg(measured).tolist()}), flush=True)
        except Exception as feedback_error:
            print(f"[homing-abort] no fresh hold pose: {feedback_error}; original failure: {exc}", flush=True)
        raise
    finally:
        arm.max_deg_s, arm.max_acc_deg_s2 = speed, acceleration


def rehome(controller, request, startup, sock):
    """Blocking homing operation; checked clocks monitor cancel/peer failure."""
    directory = Path(request["barrier_dir"])
    cfg = {**controller.config, "barrier_dir": str(directory)}
    controller.target = None
    try:
        if controller.fault:
            raise RuntimeError(controller.fault)
        if startup is None or not cfg.get("initial_pose"):
            raise RuntimeError("No fixed initial pose for episode homing")
        current = startup.load_pose(cfg["initial_pose_file"], cfg["joint_limits_deg"])
        if current != cfg["initial_pose"]:
            raise RuntimeError("Initial pose changed during recording; restart with a reviewed pose")
        controller.observe()
        if controller.arm._is_teaching():
            raise RuntimeError("Teaching mode during episode homing")
        controller.arm._gripper_cmd = current["arms"][cfg["side"]]["initial_gripper_openness"]
        controller.arm.keepalive()
        initialize_arm(controller.arm, cfg, startup, sock, episode=True)
        return {
            "sequence": request["sequence"],
            "ok": True,
            "homed": True,
            "observation": controller.observe(),
        }
    except BaseException as exc:
        with contextlib.suppress(OSError):
            (directory / "failed").write_text(f"{cfg['side']}: {exc}\n")
        controller.set_fault(exc, "home")
        with contextlib.suppress(Exception):
            controller.arm.keepalive()
        if not isinstance(exc, Exception):
            raise
        return {"sequence": request["sequence"], "ok": False, "error": str(exc)}


def wait_for_received_feedback(arm, timeout, *, clock=time.monotonic, pause=time.sleep):
    """Prime the receive monitor without trusting pre-existing SDK caches."""
    receipts = arm._feedback_receipts
    names = ("joint_12", "joint_34", "joint_56", "gripper")
    deadline = clock() + timeout
    while True:
        clock()  # Includes parent cancellation / peer failure in production.
        if arm._is_teaching():
            raise RuntimeError("Teaching mode while waiting for initial feedback")
        if all(name in receipts.received for name in names):
            for name in names:
                receipts.check(name, timeout)
            return
        if clock() >= deadline:
            raise TimeoutError("No complete fresh CAN feedback after monitor startup")
        arm.keepalive()
        pause(arm.min_dt)


def wait_for_received_gripper(
    arm, pose, observer, *, clock=time.monotonic, pause=time.sleep, timeout=8.0, settle_s=0.6,
):
    """Keep upstream arrival/fault checks, using monotonic CAN receipt freshness."""
    if "initial_gripper_openness" not in pose:
        return
    if not arm.enable_gripper or arm._effector is None:
        raise RuntimeError("Initial gripper pose requires an enabled gripper")
    target = float(pose["initial_gripper_openness"]) * arm.gripper_width
    deadline, stable = clock() + timeout, None
    while clock() < deadline:
        observer.observe()  # Validates all joint groups, gripper age, mode and values.
        if arm._is_teaching():
            raise RuntimeError("Teaching mode while waiting for initial gripper")
        feedback = arm._effector.get_gripper_status()
        if feedback is None:
            raise RuntimeError("Missing initial gripper feedback")
        msg = feedback.msg
        if msg.mode != "width" or not math.isfinite(msg.value):
            raise RuntimeError("Invalid initial gripper feedback")
        if msg.status_code & 0x3f:
            raise RuntimeError(f"Initial gripper fault: status={msg.status_code}")
        arrived = bool(msg.status_code & 0x40) and abs(msg.value - target) <= 0.003
        stable = (clock() if stable is None else stable) if arrived else None
        if stable is not None and clock() - stable >= settle_s:
            return
        arm.keepalive()
        pause(arm.min_dt)
    raise TimeoutError("Initial gripper did not settle within timeout")


def initialize_arm(arm, config, startup, sock, *, episode=False):
    previous_dt = getattr(arm, "min_dt", 1 / config.get("rate_hz", 90))
    arm.min_dt = min(previous_dt, 1 / 90)
    try:
        return _initialize_arm(arm, config, startup, sock, episode=episode)
    finally:
        arm.min_dt = previous_dt


def _initialize_arm(arm, config, startup, sock, *, episode=False):
    import numpy as np

    directory = Path(config["barrier_dir"])
    clock = initialization_clock(sock, directory)
    clock()
    side = config["side"]
    arm_config = config["initial_pose"]["arms"][side]
    if getattr(arm, "_feedback_receipts", None) is not None:
        wait_for_received_feedback(arm, config["feedback_timeout_s"], clock=clock)
        # Cancellation is checked by the motion loop. Feedback validation uses
        # its independent monotonic clock so abort can still latch a fresh pose.
        observer = NativeController(arm, config, [])
        wait_for_received_gripper(arm, arm_config, observer, clock=clock)
        observe = lambda: np.deg2rad(observer.observe()[:6])
    else:
        # Compatibility for upstream/test arms without the production receive hook.
        startup.wait_for_initial_gripper(arm, arm_config, clock=clock)
        observe = lambda: startup.observe_arm(arm)
    phase = "episode" if episode else "startup"
    speed = config[f"{phase}_home_speed_deg_s"]
    acceleration = config[f"{phase}_home_acc_deg_s2"]
    print(
        f"[initial-pose] {side}: quintic homing, max {speed:g} deg/s, {acceleration:g} deg/s²",
        flush=True,
    )
    target = np.deg2rad(arm_config["joints_deg"])
    move_episode_to_pose(
        arm, target, observe, config, startup=not episode, clock=clock,
    )
    (directory / f"{side}.done").write_text("ready\n")
    deadline = clock() + (75.0 if episode else 105.0)
    while not all((directory / f"{other}.done").exists() for other in ("left", "right")):
        if clock() >= deadline:
            raise TimeoutError("Waiting for peer initialization timed out")
        observe()
        arm.keepalive()
        time.sleep(arm.min_dt)
    clock()
    print(f"[initial-pose] {side}: both arms ready", flush=True)


def configure_gripper_with_hold(arm, max_range, *, clock=time.monotonic, pause=time.sleep):
    """Configure the gripper without a blocking ACK wait in the MIT send loop."""
    deadline = clock() + 1.0
    arm.keepalive()
    arm._effector.set_gripper_teaching_pendant_param(
        teaching_range_per=100, max_range_config=max_range,
        teaching_friction=1, timeout=0.0,
    )
    while True:
        if arm._is_teaching():
            raise RuntimeError("Teaching mode during gripper initialization")
        arm.keepalive()
        response = arm._effector.get_gripper_teaching_pendant_param(timeout=0.0)
        if response is not None:
            value = response.msg
            if (
                value.teaching_range_per == 100
                and abs(value.max_range_config - max_range) < 1e-6
                and value.teaching_friction == 1
            ):
                return True
        if clock() >= deadline:
            return False
        pause(arm.min_dt)


def create_native_arm(base, config):
    class MitOnlyArm(base):
        def _init_agx_gripper(self):
            # Upstream waits up to one second for a gripper configuration ACK
            # after enabling MIT. Keep sending the already latched joint pose
            # throughout that wait; never disable or switch joint control modes.
            if not self.enable_gripper or self._agx is None:
                return
            from vr_teleop.follower.piper.can import agx_gripper_max_range_m

            self.keepalive()
            self._effector = self._agx.init_effector(self._agx.OPTIONS.EFFECTOR.AGX_GRIPPER)
            max_range = agx_gripper_max_range_m(self.gripper_width)
            confirmed = configure_gripper_with_hold(self, max_range)
            print(
                f"[initial-pose] {self.side}: MIT hold maintained during gripper setup; "
                f"range={max_range:g}m confirmed={confirmed}",
                flush=True,
            )
            # Actual gripper enable/position is still validated by the existing
            # wait_for_initial_gripper loop before any homing trajectory begins.
            self.send_joints(self._cmd, force=True, vel=self._mit_vel, admit=False)

        def _connect_piper(self, *args, **kwargs):
            raise RuntimeError("LeRobot native startup requires PyAgxArm MIT; MOVE J fallback forbidden")

    arm = MitOnlyArm(
        config["can"],
        side=config["side"],
        enable_gripper=True,
        use_mit=True,
        speed_percent=config["speed_percent"],
        max_deg_s=config["max_deg_s"],
        max_acc_deg_s2=config["max_acc_deg_s2"],
        rate_hz=max(90, config["rate_hz"]) if config.get("initial_pose") else config["rate_hz"],
        use_admittance=False,
        use_compliant=False,
        use_table_contact=False,
    )
    if config.get("initial_pose"):
        # Must be set before connect(), which emits the first gripper command.
        arm._gripper_cmd = config["initial_pose"]["arms"][config["side"]]["initial_gripper_openness"]
    return arm


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--fd", type=int, required=True)
    parser.add_argument("--config", required=True)
    args = parser.parse_args()
    config = json.loads(args.config)
    sock = socket.socket(fileno=args.fd)
    arm = None
    lock = None
    try:
        lock_dir = Path(f"/tmp/lerobot-piper-native-{os.getuid()}")
        lock_dir.mkdir(mode=0o700, exist_ok=True)
        lock = (lock_dir / (config["can"] + ".lock")).open("a")
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        ref = Path(config["reference_dir"]).resolve()
        from startup import load_startup, verify_reference

        verify_reference(ref, config["expected_reference_commit"])
        startup = None
        if config.get("initial_pose"):
            startup = load_startup(ref)
            current_pose = startup.load_pose(config["initial_pose_file"], config["joint_limits_deg"])
            if current_pose != config["initial_pose"]:
                raise RuntimeError("Initial pose changed after preflight")
            if startup.resolve_ports(current_pose)[config["side"]] != config["can"]:
                raise RuntimeError("USB-CAN identity changed after preflight")
        sys.path.insert(0, str(ref / "src"))
        from vr_teleop.follower.piper.can import LIMITS_DEG, PiperCanArm

        if [list(pair) for pair in LIMITS_DEG] != config["joint_limits_deg"]:
            raise RuntimeError("Configured joint limits differ from pinned native model")

        def interrupted(_sig, _frame):
            raise KeyboardInterrupt

        signal.signal(signal.SIGTERM, interrupted)
        signal.signal(signal.SIGINT, interrupted)
        arm = create_native_arm(PiperCanArm, config)
        arm.connect()
        if arm._agx is None or not arm.use_mit:
            raise RuntimeError("Native MIT driver unavailable")
        arm._feedback_receipts = FeedbackReceipts(arm)
        if startup is not None:
            initialize_arm(arm, config, startup, sock)
        arm.min_dt = 1 / config["rate_hz"]
        controller = NativeController(arm, config, LIMITS_DEG)
        # Startup alignment belongs to the Teleoperator; the Robot API accepts
        # absolute joints, so a policy can use the same Robot without VR.
        deadline = time.monotonic() + config["feedback_timeout_s"] * 4
        while True:
            arm.keepalive()
            try:
                state = controller.observe()
                break
            except RuntimeError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.01)
        sock.sendall(json.dumps({"ok": True, "ready": True, "observation": state}).encode())
        serve(sock, controller, startup)
    except (Exception, KeyboardInterrupt) as exc:
        if config.get("barrier_dir"):
            with contextlib.suppress(OSError):
                (Path(config["barrier_dir"]) / "failed").write_text(f"{config['side']}: {exc}\n")
        with contextlib.suppress(Exception):
            sock.sendall(json.dumps({"ok": False, "error": str(exc)}).encode())
        if not isinstance(exc, KeyboardInterrupt):
            raise
    finally:
        if arm is not None:
            try:
                arm.close(disable=False)
            finally:
                arm.close_diagnostics()
        sock.close()
        if lock is not None:
            lock.close()


if __name__ == "__main__":
    main()
