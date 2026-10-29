"""Vector layouts used on the wire, in the model and in the IK solver.
关节/夹爪向量与 EEF20 位姿向量的排布约定（唯一定义处）。

Joint vector (14D, degrees + mm) / 关节向量（14 维，角度制 + 毫米）:
    [L_j1..L_j6, L_gripper, R_j1..R_j6, R_gripper]
    Every arm is followed by its own gripper, NOT both grippers at the end.
    每条臂后面紧跟它自己的夹爪，而不是两个夹爪放在最后。

EEF20 pose vector (world frame, Link6) / EEF20 末端位姿向量（world 坐标系，Link6）:
    [L_xyz(3), L_rot6d(6), L_gripper_mm(1), R_xyz(3), R_rot6d(6), R_gripper_mm(1)]
    rot6d = the first two COLUMNS of the rotation matrix.
    rot6d 为旋转矩阵的前两列。
"""
import numpy as np

SIDES = ("left", "right")

# Wire order of the native robot API. / 原生机器人接口的字段顺序。
ACTION_KEYS = tuple(
    f"{side}_{joint}.pos"
    for side in SIDES
    for joint in (*[f"joint_{i}" for i in range(1, 7)], "gripper")
)

# 12 arm joints inside the 14D vector, and the two gripper slots.
# 14 维向量中 12 个臂关节的下标，以及两个夹爪的下标。
JOINT_COLUMNS = np.array([0, 1, 2, 3, 4, 5, 7, 8, 9, 10, 11, 12])
GRIPPER_COLUMNS = [6, 13]

# URDF / native IK joint names, same order as JOINT_COLUMNS.
# URDF 与原生 IK 的关节名，顺序与 JOINT_COLUMNS 一致。
IK_JOINT_NAMES = [f"{side}_joint{i}" for side in SIDES for i in range(1, 7)]

# EEF20 block offsets: left arm at 0, right arm at 10.
# EEF20 中左右臂数据块的起始下标。
EEF_BASES = (0, 10)
EEF_GRIPPERS = [9, 19]
