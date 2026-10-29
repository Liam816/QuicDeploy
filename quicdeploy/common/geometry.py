"""Physical EEF20 geometry: rotation-6D, anchor encoding, interpolation.
EEF20 物理几何：6D 旋转表示、相对锚点编解码、位姿插值。不导入任何硬件代码。
"""
import numpy as np
from scipy.spatial.transform import Rotation

from .layout import EEF_BASES


def rot6d(v):
    """Rotation-6D (two matrix columns) -> 3x3 matrix via Gram-Schmidt.
    6D 旋转（矩阵前两列）经 Gram-Schmidt 正交化还原为 3x3 旋转矩阵。
    """
    v = np.asarray(v, dtype=np.float64)
    if v.shape[-1] != 6 or not np.isfinite(v).all():
        raise ValueError("rotation6D: expected finite last dimension 6")
    a, b = v[..., :3], v[..., 3:]
    na = np.linalg.norm(a, axis=-1, keepdims=True)
    if np.any(na < 1e-8):
        raise ValueError("degenerate rotation: zero first column")
    a = a / na
    b = b - np.sum(a * b, axis=-1, keepdims=True) * a
    nb = np.linalg.norm(b, axis=-1, keepdims=True)
    if np.any(nb < 1e-8):
        raise ValueError("degenerate rotation: parallel columns")
    b = b / nb
    return np.stack((a, b, np.cross(a, b)), axis=-1)


def to6(r):
    """3x3 matrix -> rotation-6D. / 旋转矩阵取前两列得到 6D 表示。"""
    return np.concatenate((r[..., :, 0], r[..., :, 1]), axis=-1)


def validate_pose(x):
    """Require a nonempty finite [H, 20] array with valid rotations.
    校验 [H,20] 位姿数组：非空、有限、两只手腕的旋转非退化。
    """
    x = np.asarray(x, dtype=np.float64)
    if x.ndim != 2 or x.shape[1] != 20 or not len(x):
        raise ValueError(f"dimension error: expected nonempty [H,20], got {x.shape}")
    if not np.isfinite(x).all():
        raise ValueError("NaN/Inf in physical actions")
    for b in EEF_BASES:
        rot6d(x[:, b + 3:b + 9])
    return x


def decode(raw, anchor, protocol):
    """Model output -> absolute world poses.

    模型输出 -> world 系绝对位姿。eef20_relative：位置加锚点、旋转右乘锚点旋转；
    夹爪始终为绝对毫米值。
    """
    out = validate_pose(raw).copy()
    anchor = validate_pose(np.asarray(anchor)[None])[0]
    if protocol not in ("eef20_relative", "eef20_absolute"):
        raise ValueError("unsupported action protocol; configure explicitly")
    for b in EEF_BASES:
        r = rot6d(out[:, b + 3:b + 9])
        if protocol == "eef20_relative":
            out[:, b:b + 3] += anchor[b:b + 3]
            r = r @ rot6d(anchor[b + 3:b + 9])
        out[:, b + 3:b + 9] = to6(r)
    return out


def relative_prefix(absolute, anchor):
    """Inverse of decode(): re-encode absolute poses against a NEW anchor.

    decode() 的逆变换：用新的实测锚点把旧的绝对位姿重新编码为相对量（RTC 前缀用）。
    夹爪保持绝对毫米值。
    """
    out = validate_pose(absolute).copy()
    anchor = validate_pose(np.asarray(anchor)[None])[0]
    for b in EEF_BASES:
        out[:, b:b + 3] -= anchor[b:b + 3]
        out[:, b + 3:b + 9] = to6(rot6d(out[:, b + 3:b + 9]) @ rot6d(anchor[b + 3:b + 9]).T)
    return out


def pose_errors(pred, gt):
    """Per-row position (mm), orientation (deg) and gripper (mm) errors.
    逐行计算位置误差(mm)、姿态误差(deg)和夹爪误差(mm)。
    """
    pred, gt = validate_pose(pred), validate_pose(gt)
    result = {}
    for side, b in (("left", 0), ("right", 10)):
        rp, rg = rot6d(pred[:, b + 3:b + 9]), rot6d(gt[:, b + 3:b + 9])
        angle = np.degrees(np.arccos(np.clip((np.einsum("nij,nij->n", rp, rg) - 1) / 2, -1, 1)))
        result[side] = dict(
            position_mm=(np.linalg.norm(pred[:, b:b + 3] - gt[:, b:b + 3], axis=1) * 1000).tolist(),
            orientation_deg=angle.tolist(),
            gripper_mm=np.abs(pred[:, b + 9] - gt[:, b + 9]).tolist(),
        )
    return result


def slerp_quaternions(starts, ends, fraction):
    """Shortest-path SLERP of paired quaternions (xyzw), returned as rotation-6D.

    成对四元数的最短路径球面插值，结果以 6D 旋转返回；夹角极小时退化为线性插值。
    `ends` may be modified in place. / 注意：会原地修改 ends。
    """
    dot = np.sum(starts * ends, axis=1)
    ends[dot < 0] *= -1
    dot = np.clip(np.abs(dot), 0, 1)
    close = dot > 0.9995
    theta = np.arccos(dot)
    safe_sin = np.where(close, 1.0, np.sin(theta))
    start_scale = np.where(close, 1 - fraction, np.sin((1 - fraction) * theta) / safe_sin)
    end_scale = np.where(close, fraction, np.sin(fraction * theta) / safe_sin)
    quaternions = start_scale[:, None] * starts + end_scale[:, None] * ends
    quaternions /= np.linalg.norm(quaternions, axis=1, keepdims=True)
    return to6(Rotation.from_quat(quaternions).as_matrix())


def interpolate_pose(a, b, fraction):
    """Blend two EEF20 poses: linear XYZ/grippers, SLERP for both wrists.
    两个 EEF20 位姿之间插值：位置与夹爪线性插值，两只手腕旋转做球面插值。
    """
    if not 0 <= fraction <= 1:
        raise ValueError("Interpolation fraction outside [0,1]")
    a, b = validate_pose(np.asarray(a)[None])[0], validate_pose(np.asarray(b)[None])[0]
    if fraction == 0:
        return a.copy()
    if fraction == 1:
        return b.copy()
    out = (1 - fraction) * a + fraction * b
    starts = Rotation.from_matrix(rot6d(np.stack((a[3:9], a[13:19])))).as_quat()
    ends = Rotation.from_matrix(rot6d(np.stack((b[3:9], b[13:19])))).as_quat()
    out[3:9], out[13:19] = slerp_quaternions(starts, ends, fraction)
    return out
