"""Dual Piper-X arms driven by two native MIT worker processes.

双臂 Piper-X 驱动：每条臂一个原生 MIT 子进程（native_worker.py），父进程只做调度与校验。

Process layout / 进程结构:
    parent (this file, QuicInfer env)
      ├─ cameras: 3 x OpenCVCamera reader threads / 三路相机读帧线程
      ├─ left  native_worker.py  (quest_python env, owns can0) / 左臂子进程
      └─ right native_worker.py  (quest_python env, owns can1) / 右臂子进程
    parent <-> worker: AF_UNIX SOCK_SEQPACKET, one JSON message per packet.
    父子进程之间用 SOCK_SEQPACKET 交换 JSON 消息。

Worker ops / 子进程操作: observe | hold | action | home | close.
Each worker runs its own MIT loop at rate_hz and holds the last accepted target;
an expired target (command_timeout_s) falls back to native hold.
子进程以 rate_hz 运行 MIT 控制环并保持最后一个接受的目标；目标超时后自动原地保持。
"""
import json
import os
import select
import signal
import socket
import subprocess
import tempfile
import time
from pathlib import Path

import numpy as np

from ...common.layout import SIDES
from .camera import OpenCVCamera
from .config import PiperConfig
from .native_worker import vector
from .startup import check_can, connect_camera, resolve_startup, verify_reference

# Fields forwarded to each worker. / 转发给每个子进程的配置字段。
_WORKER_KEYS = (
    "reference_dir", "rate_hz", "speed_percent", "max_deg_s", "max_acc_deg_s2",
    "startup_home_speed_deg_s", "startup_home_acc_deg_s2", "episode_home_speed_deg_s",
    "episode_home_acc_deg_s2", "command_timeout_s", "feedback_timeout_s", "expected_reference_commit",
)


def is_original_can_controller(command):
    """Detect the teleop Dora CAN follower, which must not run concurrently.
    识别遥操作的 Dora CAN 跟随进程（不能与本程序同时控制 CAN）。
    """
    words = [arg.decode(errors="replace") if isinstance(arg, bytes) else arg for arg in command]
    for index, word in enumerate(words[:2]):
        name = Path(word).name
        if name == "dora-vr-follower-piper":
            return True
        if name == "vr_startup.py" and words[index + 1:index + 2] == ["follower"]:
            return True
    return False


class PiperDualArm:
    """Connect() enables both arms and performs the configured smooth homing.
    connect() 会使能双臂并平滑回到配置的初始位姿 —— 调用即会让真机运动。
    """

    def __init__(self, spec, *, rate_hz=None):
        self.config = PiperConfig.from_dict(spec, rate_hz=rate_hz)
        self.cameras = {name: OpenCVCamera(cam) for name, cam in self.config.cameras.items()}
        self.commands = 0                 # policy send() calls / 策略下发次数
        self._workers = {}
        self._sequence = 0
        self._connected = False
        self._startup_pose = None
        self._startup_directory = None

    # ------------------------------------------------------------------ lifecycle
    def connect(self):
        if self._connected:
            raise RuntimeError("Piper robot already connected")
        cfg = self.config
        # Preflight: pinned driver source, env, initial pose, USB-CAN identity, CAN state.
        # 预检：上游驱动版本、Python 环境、初始位姿、USB-CAN 身份映射、CAN 状态。
        verify_reference(cfg.reference_dir, cfg.expected_reference_commit)
        if not Path(cfg.quest_python).is_file():
            raise FileNotFoundError(cfg.quest_python)
        self._startup_pose = resolve_startup(cfg)  # may remap left/right CAN by USB serial
        check_can((cfg.left_can, cfg.right_can))
        for path in Path("/proc").glob("[0-9]*/cmdline"):
            try:
                command = path.read_bytes().split(b"\0")
            except OSError:
                continue
            if is_original_can_controller(command):
                raise RuntimeError("Stop the original Dora CAN followers before connecting")
        try:
            if self._startup_pose is not None:
                # Shared dual-arm barrier directory for synchronized homing.
                # 双臂同步回零使用的共享屏障目录。
                self._startup_directory = tempfile.TemporaryDirectory(prefix="quicinfer-piper-init-")
            for camera in self.cameras.values():
                connect_camera(camera)
            if self._startup_pose is not None:
                print(
                    "Opening both grippers and smoothly homing both arms with native MIT: "
                    f"max {cfg.startup_home_speed_deg_s:g} deg/s, {cfg.startup_home_acc_deg_s2:g} deg/s².",
                    flush=True,
                )
            for side in SIDES:
                self._start_worker(side)
            for side, (process, sock, log) in self._workers.items():
                reply = json.loads(sock.recv(16384))
                if not reply.get("ready"):
                    raise RuntimeError(f"{side} startup failed: {reply.get('error')}; log: {log}")
                vector(reply["observation"], 7)
                if process.poll() is not None:
                    raise RuntimeError(f"{side} worker exited; log: {log}")
                sock.settimeout(cfg.response_timeout_s)
            self._connected = True
            if self._startup_pose is not None:
                print("Both arms initialized at the configured initial pose.", flush=True)
        except BaseException:
            self.close()
            raise

    def _start_worker(self, side):
        cfg = self.config
        payload = {key: getattr(cfg, key) for key in _WORKER_KEYS}
        payload.update(
            side=side, can=getattr(cfg, f"{side}_can"), gripper_width_mm=cfg.gripper_width(side),
            joint_limits_deg=[list(pair) for pair in cfg.joint_limits_deg],
        )
        if self._startup_pose is not None:
            payload.update(
                initial_pose=self._startup_pose, initial_pose_file=cfg.initial_pose_file,
                barrier_dir=self._startup_directory.name,
            )
        parent, child = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        parent.settimeout(cfg.startup_timeout_s)
        # Clean env: no inherited PYTHONPATH/user site; force Piper-X + gripper.
        # 干净的子进程环境：不继承 PYTHONPATH；固定 Piper-X 机型并启用夹爪。
        env = os.environ.copy()
        env["PYTHONPATH"] = ""
        env["PYTHONNOUSERSITE"] = "1"
        for key in ("PIPER_ADMITTANCE", "PIPER_TABLE_CONTACT", "PIPER_COMPLIANT"):
            env.pop(key, None)
        env.update(VR_ROBOT="piper_x", PIPER_GRIPPER="1")
        for arm_side in SIDES:
            env[f"PIPER_{arm_side.upper()}_GRIPPER_WIDTH_M"] = str(cfg.gripper_width(arm_side) / 1000.0)
        python = Path(cfg.quest_python).absolute()
        libs = list((python.parent.parent / "lib").glob("python*/site-packages/cmeel.prefix/lib"))
        if len(libs) != 1:
            parent.close()
            child.close()
            raise RuntimeError("Cannot locate Quest native libraries")
        env["LD_LIBRARY_PATH"] = str(libs[0])
        log_dir = Path(cfg.log_dir)
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / f"{side}-{time.time_ns()}.log"
        try:
            with log_path.open("w") as log:
                process = subprocess.Popen(
                    [str(python), str(Path(__file__).with_name("native_worker.py")),
                     "--fd", str(child.fileno()), "--config", json.dumps(payload)],
                    pass_fds=(child.fileno(),), stdin=subprocess.DEVNULL, stdout=log,
                    stderr=subprocess.STDOUT, env=env, start_new_session=True,
                )
        except Exception:
            parent.close()
            raise
        finally:
            child.close()
        self._workers[side] = (process, parent, log_path)

    def close(self):
        """Stop workers (holding torque) and release cameras. / 停止子进程（保持力矩）并释放相机。"""
        self._connected = False
        workers, self._workers = self._workers, {}
        for _process, sock, _log in workers.values():
            try:
                sock.sendall(json.dumps({"operation": "close"}).encode())
            except OSError:
                pass
            finally:
                sock.close()
        for process, _sock, _log in workers.values():
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.send_signal(signal.SIGTERM)
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    # Never leave a live orphan issuing targets. / 绝不留下仍在发指令的孤儿进程。
                    process.kill()
                    process.wait()
        if self._startup_directory is not None:
            self._startup_directory.cleanup()
            self._startup_directory = None
        for camera in self.cameras.values():
            if camera.is_connected:
                camera.disconnect()

    # ------------------------------------------------------------------ worker RPC
    def _request(self, side, operation, **payload):
        self._sequence += 1
        process, sock, log = self._workers[side]
        if process.poll() is not None:
            raise RuntimeError(f"{side} native worker exited; inspect {log}")
        seq = self._sequence
        sock.sendall(json.dumps(
            {"sequence": seq, "operation": operation, "sent_at": time.monotonic(), **payload}, allow_nan=False,
        ).encode())
        deadline = time.monotonic() + self.config.response_timeout_s
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f"{side} MIT worker timed out")
            sock.settimeout(remaining)
            raw = sock.recv(16384)
            if not raw:
                raise RuntimeError(f"{side} MIT worker disconnected; inspect {log}")
            reply = json.loads(raw)
            if reply.get("sequence") == seq:
                if not reply.get("ok"):
                    raise RuntimeError(f"{side} MIT worker: {reply.get('error')}")
                return reply
            if reply.get("ok") is False:
                raise RuntimeError(f"{side} MIT worker: {reply.get('error')}")

    def _require_connected(self):
        if not self._connected:
            raise RuntimeError("Piper robot not connected")

    # ------------------------------------------------------------------ public API
    def read_joints(self):
        """Measured 14D joints/grippers (deg, mm) without touching cameras.
        读取 14 维实测关节/夹爪（度、毫米），不访问相机。
        """
        self._require_connected()
        values = []
        for side in SIDES:
            values.extend(vector(self._request(side, "observe")["observation"], 7))
        return np.asarray(values, dtype=float)

    def send(self, command):
        """Send one guarded 14D joint target; returns the workers' acknowledgement.

        下发一条已通过保护的 14 维关节目标，返回子进程确认的目标值。
        Both arms are validated before either is sent, so a bad right-arm target
        can never leave only the left arm moving.
        先校验两条臂再发送，避免右臂目标非法时只有左臂动作。
        Any failure disconnects (workers then hold). / 任何失败都会断开（子进程保持力矩）。
        """
        self._require_connected()
        command = vector([float(v) for v in np.asarray(command, dtype=float)], 14)
        for i, side in enumerate(SIDES):
            arm = command[7 * i:7 * i + 7]
            for j, (lo, hi) in enumerate(self.config.joint_limits_deg, 1):
                if not lo <= arm[j - 1] <= hi:
                    raise ValueError(f"{side} joint {j} target outside native limits")
            if not 0 <= arm[6] <= self.config.gripper_width(side):
                raise ValueError(f"{side} gripper target outside configured stroke")
        accepted = []
        try:
            for i, side in enumerate(SIDES):
                reply = self._request(side, "action", action=command[7 * i:7 * i + 7])
                accepted.extend(vector(reply["action"], 7))
        except BaseException:
            self.close()
            raise
        self.commands += 1
        return np.asarray(accepted, dtype=float)

    def hold(self):
        """Cancel pending targets and keep native MIT hold. / 取消未完成目标并原地保持。"""
        self._require_connected()
        try:
            for side in SIDES:
                self._request(side, "hold")
        except BaseException:
            self.close()
            raise

    def rehome(self, *, cancel=lambda: False):
        """Smoothly return both arms to the initial pose (dual-arm barrier).

        双臂经共享屏障同步、平滑地回到初始位姿；保持相机连接与力矩。
        Returns False if cancelled. / 被取消时返回 False。
        """
        if not self._connected or self._startup_pose is None:
            raise RuntimeError("Homing requires connected workers and an explicit initial pose")
        with tempfile.TemporaryDirectory(prefix="quicinfer-piper-episode-") as directory:
            try:
                if cancel():
                    self.close()
                    return False
                pending = {}
                # Dispatch BOTH requests before awaiting either barrier. / 先同时下发两条臂的请求。
                for side in SIDES:
                    process, sock, log = self._workers[side]
                    if process.poll() is not None:
                        raise RuntimeError(f"{side} worker exited; inspect {log}")
                    self._sequence += 1
                    sock.settimeout(self.config.response_timeout_s)
                    sock.sendall(json.dumps(
                        {"operation": "home", "sequence": self._sequence, "barrier_dir": directory}
                    ).encode())
                    pending[sock] = (side, self._sequence)
                deadline = time.monotonic() + self.config.startup_timeout_s
                while pending:
                    if cancel():
                        self.close()
                        return False
                    if time.monotonic() >= deadline:
                        raise TimeoutError("Both arms did not finish homing in time")
                    for side, (process, _, log) in self._workers.items():
                        if process.poll() is not None:
                            raise RuntimeError(f"{side} worker exited during homing; inspect {log}")
                    readable, _, _ = select.select(list(pending), [], [], 0.05)
                    for sock in readable:
                        side, seq = pending[sock]
                        raw = sock.recv(16384)
                        if not raw:
                            raise RuntimeError(f"{side} worker disconnected during homing")
                        reply = json.loads(raw)
                        if not reply.get("ok"):
                            raise RuntimeError(f"{side} homing: {reply.get('error')}")
                        if reply.get("sequence") != seq or not reply.get("homed"):
                            raise RuntimeError(f"{side} invalid homing acknowledgement")
                        vector(reply["observation"], 7)
                        del pending[sock]
                return True
            except BaseException:
                # Tell the peer arm before any slow cleanup. / 先通知另一条臂停止。
                (Path(directory) / "failed").write_text("Parent stopped homing\n")
                self.close()
                raise
