"""Startup self-check: dataset FK and native IK must agree on Link6.
启动自检：数据集 FK 与原生 IK 的 Link6 坐标系必须一致，且 IK 能局部收敛。
"""
import numpy as np
from scipy.spatial.transform import Rotation

from ..common.geometry import pose_errors
from ..common.layout import JOINT_COLUMNS
from ..robot.guard import CommandGuard


def check_kinematics(ik, guard_settings):
    # 1) 64 random joint samples: FK(python) vs FK(native) must match to 1e-7.
    #    64 组随机关节：Python FK 与原生 FK 的结果误差必须 < 1e-7。
    rng = np.random.default_rng(42)
    limits = np.tile(np.array(guard_settings["joint_limits_deg"]), (2, 1))
    max_position, max_rotation = 0.0, 0.0
    for _ in range(64):
        joints = np.zeros(14)
        joints[JOINT_COLUMNS] = rng.uniform(limits[:, 0] + 1, limits[:, 1] - 1)
        native = ik.request("fk", q=np.deg2rad(joints[JOINT_COLUMNS]).tolist())["poses"]
        converted = ik.ik_targets(ik.wrist(joints))
        for a, b in zip(native, converted, strict=True):
            max_position = max(max_position, float(np.linalg.norm(np.array(a[:3]) - b[:3])))
            ra = Rotation.from_quat([*a[4:], a[3]]).as_matrix()
            rb = Rotation.from_quat([*b[4:], b[3]]).as_matrix()
            max_rotation = max(max_rotation, float(np.linalg.norm(ra - rb)))
    if max_position > 1e-7 or max_rotation > 1e-7:
        raise ValueError("Dataset wrist FK and native IK Link6 frames disagree")

    # 2) From the IK home pose, move J1 of both arms by 1 deg through IK + guard;
    #    the closed loop must converge within 5 mm / 2 deg.
    #    从 IK 初始位姿出发，经 IK + 指令保护把两臂 J1 各转 1°，闭环需收敛到 5mm / 2° 内。
    joints = np.zeros(14)
    joints[JOINT_COLUMNS] = np.rad2deg(ik.initial)
    goal = joints.copy()
    goal[[0, 7]] += 1
    target = ik.wrist(goal)
    ik.reset(joints)
    guard = CommandGuard(guard_settings)
    guard.reset(joints)
    for _ in range(300):
        command, _ = ik.step(joints, target)
        joints, _ = guard.apply(command, joints, ik.dt)
    residual = pose_errors(ik.wrist(joints)[None], target[None])
    if any(
        residual[side]["position_mm"][0] > 5 or residual[side]["orientation_deg"][0] > 2
        for side in ("left", "right")
    ):
        raise ValueError("Native differential IK local roundtrip did not converge: " + str(residual))
    return {
        "samples": 64,
        "target_frames": ["left_Link6", "right_Link6"],
        "target_root_frame": "arm_origin",
        "max_fk_position_difference_m": max_position,
        "max_fk_rotation_matrix_difference": max_rotation,
        "local_roundtrip": residual,
    }
