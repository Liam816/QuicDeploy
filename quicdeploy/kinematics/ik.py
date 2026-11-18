"""End-effector FK and a client for the isolated native differential-IK worker.

末端正运动学，以及隔离进程中原生差分 IK 求解器的客户端。

The IK extension is a compiled module built for a different Python env, so it
runs in its own subprocess (ik_worker.py) and talks JSON lines over a socketpair.
IK 扩展是为另一个 Python 环境编译的二进制模块，因此在独立子进程中运行，
通过 socketpair 以逐行 JSON 通信。
"""
import json
import os
import socket
import subprocess
import time
from pathlib import Path

import numpy as np
import yaml
from scipy.spatial.transform import Rotation

from ..common.geometry import rot6d, to6, validate_pose
from ..common.layout import EEF_GRIPPERS, GRIPPER_COLUMNS, IK_JOINT_NAMES, JOINT_COLUMNS, SIDES
from ..config.model import PHYSICAL_CONTRACT
from .piperx_ee import PiperXEEProjection
from .urdf import URDFFK


class Kinematics:
    def __init__(self, config, log_path):
        self.config = config
        self.reference_joints = None
        self.reset_joints = None
        self.joint_travel_deg = np.zeros(12)
        if config.get("solver_state_mode", "measured") not in ("measured", "reference"):
            raise ValueError("Unknown IK solver state mode")
        self.fk = URDFFK(Path(config["urdf"]))
        # Model anchors and RTC always use the exact training geometry.
        # 模型锚点和 RTC 始终使用训练时的原始几何。
        self.frame = config.get("frame", PHYSICAL_CONTRACT["frame"])
        self.ik_root_frame = config.get("ik_root_frame", "arm_origin")
        self.target_frames = [config.get("ends", PHYSICAL_CONTRACT["ends"])[side] for side in SIDES]
        self.joint_names = config.get("joint_names", IK_JOINT_NAMES)
        self.control_fk = self.fk
        self.control_frame = self.frame
        self.control_target_frames = self.target_frames
        self.control_joint_names = self.joint_names
        self.projector = None
        self.projection_seed = None
        if config.get("project_piperx_ee"):
            self.control_fk = URDFFK(Path(config["native_urdf"]))
            self.control_frame = PHYSICAL_CONTRACT["frame"]
            self.control_target_frames = [PHYSICAL_CONTRACT["ends"][side] for side in SIDES]
            self.control_joint_names = IK_JOINT_NAMES
            self.projector = PiperXEEProjection(self.fk, self.frame, self.target_frames, self.joint_names,
                                                self.control_fk, self.control_joint_names)
        self.origin_inverse = np.linalg.inv(self.control_fk.link_fk(self.control_frame, self.ik_root_frame, {}))
        native = yaml.safe_load(Path(config["yaml"]).read_text())
        if native["model"]["root_frame"] != self.ik_root_frame:
            raise ValueError("Native IK frame changed")
        self.dt = float(config.get("control_dt_s", native["ik"]["dt"]))
        if not np.isfinite(self.dt) or not 0 < self.dt <= 0.05:
            raise ValueError("Invalid control timestep")
        if native["model"]["tcp_frames"] != self.control_target_frames or native["model"].get("tcp_definitions"):
            raise ValueError("Deployment IK must control the configured end links directly without extra TCP offsets")
        self.sock, child = socket.socketpair()
        self.sock.settimeout(2)
        env = os.environ.copy()
        env.pop("PYTHONPATH", None)
        env["PYTHONNOUSERSITE"] = "1"
        # The extension links against the env's cmeel (pinocchio) libraries.
        # A venv python may resolve to the base interpreter, so search both paths.
        # 扩展依赖该环境中 cmeel(pinocchio) 动态库；venv 的 python 可能解析到基础解释器，两处都找。
        libs = list(Path(config["python"]).resolve().parent.parent.glob("lib/python*/site-packages/cmeel.prefix/lib"))
        libs += list(Path(config["python"]).parent.parent.glob("lib/python*/site-packages/cmeel.prefix/lib"))
        env["LD_LIBRARY_PATH"] = ":".join(map(str, libs)) + ":" + env.get("LD_LIBRARY_PATH", "")
        self.log = Path(log_path).open("a")  # noqa: SIM115 -- closed with the worker
        self.process = subprocess.Popen(
            [config["python"], str(Path(__file__).with_name("ik_worker.py")), str(child.fileno()), json.dumps(config)],
            pass_fds=(child.fileno(),),
            env=env,
            stdout=self.log,
            stderr=self.log,
        )
        child.close()
        self.stream = self.sock.makefile("rwb")
        try:
            info = self.request("info")
            if info["names"] != self.control_joint_names:
                raise ValueError("Native joint order mismatch")
            self.initial = np.array(info["initial_q"])
            self.joint_damping = info["joint_damping"]
        except BaseException:
            self.close()
            raise

    def request(self, op, **values):
        """One JSON request/response round-trip. / 一次 JSON 请求-应答。"""
        try:
            self.stream.write((json.dumps(dict(op=op, **values), allow_nan=False) + "\n").encode())
            self.stream.flush()
            line = self.stream.readline()
        except (BrokenPipeError, ConnectionResetError) as exc:
            raise RuntimeError("Native IK worker exited; see " + str(self.log.name)) from exc
        if not line:
            raise RuntimeError("Native IK worker exited")
        response = json.loads(line)
        if not response["ok"]:
            raise RuntimeError("Native IK: " + response["error"])
        return response["result"]

    def reset(self, joints):
        """Re-seed the solver from measured joints. / 用实测关节重置求解器内部状态。"""
        joints = np.asarray(joints, dtype=float)
        angles = np.deg2rad(joints[JOINT_COLUMNS])
        self.request("reset", q=angles.tolist())
        self.projection_seed = angles.reshape(2, 6) if self.projector else None
        self.reference_joints = joints.copy()
        self.reset_joints = joints.copy()
        self.joint_travel_deg = np.zeros(12)

    def wrist(self, joints):
        """14D joints (deg/mm) -> EEF20 in the trained pose frame. / 关节转为训练坐标系的 EEF20。"""
        return self._wrist(joints, self.fk, self.frame, self.target_frames, self.joint_names)

    def control_wrist(self, joints):
        """14D encoder state -> physical control wrist pose. / 编码器关节状态转为控制末端位姿。"""
        return self._wrist(joints, self.control_fk, self.control_frame, self.control_target_frames,
                           self.control_joint_names)

    @staticmethod
    def _wrist(joints, fk, frame, ends, names):
        q = np.deg2rad(np.asarray(joints)[JOINT_COLUMNS])
        values = dict(zip(names, q, strict=True))
        pose = []
        for end, grip in zip(ends, GRIPPER_COLUMNS, strict=True):
            t = fk.link_fk(frame, end, values)
            pose.extend(t[:3, 3])
            pose.extend(to6(t[:3, :3]))
            pose.append(float(joints[grip]))
        return validate_pose([pose])[0]

    def ik_targets(self, wrist):
        """Control EEF20 -> two solver-root [xyz, wxyz] targets. / 控制位姿转为求解器根坐标系。"""
        targets = []
        for b in (0, 10):
            t = np.eye(4)
            t[:3, 3] = wrist[b:b + 3]
            t[:3, :3] = rot6d(wrist[b + 3:b + 9])
            wrist_in_root = self.origin_inverse @ t
            quat = Rotation.from_matrix(wrist_in_root[:3, :3]).as_quat()  # xyzw -> wxyz
            targets.append(np.r_[wrist_in_root[:3, 3], quat[3], quat[:3]].tolist())
        return targets

    def step(self, measured, target):
        """One differential-IK integration step towards an EEF20 target.

        朝 EEF20 目标积分一步差分 IK，返回 14D 关节指令（夹爪直接取目标值）。
        In "reference" mode the solver integrates from the last accepted command
        (like the teleop recorder); encoder feedback stays authoritative for the
        command guard and model anchoring.
        reference 模式下从上一次接受的指令积分（与遥操作录制一致）；
        编码器反馈仍用于指令保护和模型锚点。
        """
        control_target = target
        if self.projector is not None:
            if self.projection_seed is None:
                raise RuntimeError("PiperX EE projection requires reset from observed joints before motion")
            started = time.perf_counter()
            hint, projected, residual = self.projector.project(target, self.projection_seed)
            control_target = self.control_wrist(hint)
            projection_ms = (time.perf_counter() - started) * 1000
        left, right = self.ik_targets(control_target)
        solver_joints = measured
        if self.config.get("solver_state_mode", "measured") == "reference":
            if self.reference_joints is None:
                raise RuntimeError("Reference IK requires reset from observed joints before motion")
            solver_joints = self.reference_joints
        reply = self.request(
            "step", q=np.deg2rad(np.asarray(solver_joints)[JOINT_COLUMNS]).tolist(),
            measured_q=np.deg2rad(np.asarray(measured)[JOINT_COLUMNS]).tolist(), left=left, right=right,
        )
        if not reply["accepted"]:
            raise RuntimeError("IK rejected: " + reply["detail"])
        out = np.asarray(measured, dtype=float).copy()
        out[JOINT_COLUMNS] = np.rad2deg(reply["q"])
        out[GRIPPER_COLUMNS] = target[EEF_GRIPPERS]
        if self.projector is not None:
            self.projection_seed = projected
            reply.update(control_wrist_target=control_target.tolist(), label_projection_residual=residual,
                         label_projection_ms=projection_ms)
        if self.reference_joints is not None:
            self.joint_travel_deg += np.abs((out - self.reference_joints)[JOINT_COLUMNS])
        reply["joint_travel_from_reset_deg"] = self.joint_travel_deg.tolist()
        reply["joint_delta_from_reset_deg"] = (
            None if self.reset_joints is None else (out - self.reset_joints)[JOINT_COLUMNS].tolist()
        )
        self.reference_joints = out.copy()
        return out, reply

    def close(self):
        self.stream.close()
        self.sock.close()
        try:
            self.process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            self.process.terminate()
            try:
                self.process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()
        self.log.close()
