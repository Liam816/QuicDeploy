"""Map the pinned PiperX EE training labels into the encoder joint convention.

把锁定的 PiperX EE 训练末端位姿还原到编码器关节约定。
"""
import numpy as np
from scipy.spatial.transform import Rotation

from ..common.geometry import rot6d, validate_pose
from ..common.layout import EEF_BASES, EEF_GRIPPERS, GRIPPER_COLUMNS, JOINT_COLUMNS, SIDES

# The label's J4 and J6 are nearly coaxial when J5 is near zero. Prefer a
# continuous J6 when the Cartesian target offers little evidence to move it.
# 训练链的 J5 接近零位时，J4 与 J6 近乎同轴。位姿对 J6 约束不足时，优先延续上一目标的 J6。
_RESIDUAL_SCALE = np.array([1.0, 1.0, 1.0, 0.20, 0.20, 0.20])
_POSTURE_WEIGHT = np.square([0.003] * 5 + [0.015])
_DAMPING = np.diag(_POSTURE_WEIGHT + 1e-6)
_MAX_POSITION_RESIDUAL_M = 0.050
_MAX_ORIENTATION_RESIDUAL_RAD = np.deg2rad(12)


class _RevoluteChain:
    """Precompute URDF joint origins for analytic FK/Jacobian at the control rate.

    预计算 URDF 关节原点，在控制循环内计算正运动学与解析雅可比。
    """

    def __init__(self, chain, names):
        origins, axes, actual_names = [], [], []
        fixed = np.eye(4)
        for joint in chain:
            fixed = fixed @ joint.origin
            if joint.joint_type != "fixed":
                if joint.joint_type != "revolute":
                    raise ValueError("PiperX EE projection requires six revolute joints")
                origins.append(fixed)
                axes.append(joint.axis)
                actual_names.append(joint.name)
                fixed = np.eye(4)
        if actual_names != names:
            raise ValueError("PiperX EE projection joint order mismatch")
        self.rotations = np.asarray(origins)[:, :3, :3]
        self.translations = np.asarray(origins)[:, :3, 3]
        axes = np.asarray(axes, dtype=float)
        self.axes = axes / np.linalg.norm(axes, axis=1, keepdims=True)
        self.tool = fixed

    def evaluate(self, q, *, jacobian):
        rotations = self.rotations @ Rotation.from_rotvec(self.axes * q[:, None]).as_matrix()
        r, p = np.eye(3), np.zeros(3)
        axes, pivot = [], []
        for i in range(6):
            p = p + r @ self.translations[i]
            if jacobian:
                pivot.append(p)
                axes.append(r @ self.rotations[i] @ self.axes[i])
            r = r @ rotations[i]
        p = p + r @ self.tool[:3, 3]
        r = r @ self.tool[:3, :3]
        j = np.vstack((np.cross(axes, p - np.asarray(pivot)).T, np.transpose(axes))) if jacobian else None
        return p, r, j


class PiperXEEProjection:
    """Recover a nearby encoder target before evaluating dashboard Link6 FK.

    先求邻近的编码器关节目标，再用 dashboard 的 Link6 正运动学转换控制位姿。
    """

    def __init__(self, label_fk, frame, ends, label_names, control_fk, control_names):
        self.arms = [
            _RevoluteChain(label_fk.link_chain(frame, end), label_names[i * 6:i * 6 + 6])
            for i, end in enumerate(ends)
        ]
        # In both chains J1..J6 use the raw native angle; the different axes and
        # zeros are in their URDF origins. Bound hints by the *control* URDF.
        # 两套链均按 J1..J6 读取原始角度。轴向与零位的几何差异由各自的 URDF 定义。
        # 投影出的关节目标受控制 URDF 的限位约束。
        limits = [control_fk.joints[name].position_limits for name in control_names]
        self.limits = np.asarray(limits, dtype=float).reshape(2, 6, 2)
        if not np.isfinite(self.limits).all() or np.any(self.limits[:, :, 0] >= self.limits[:, :, 1]):
            raise ValueError("Invalid PiperX control joint limits")

    def project(self, target, seed):
        target = validate_pose(np.asarray(target)[None])[0]
        values = np.empty((2, 6))
        diagnostics = {}
        for i, (side, base, arm) in enumerate(zip(SIDES, EEF_BASES, self.arms, strict=True)):
            lower, upper = self.limits[i].T
            prior = np.clip(seed[i], lower, upper)
            q = prior.copy()
            position = target[base:base + 3]
            rotation = rot6d(target[base + 3:base + 9])
            for _ in range(8):
                p, r, jacobian = arm.evaluate(q, jacobian=True)
                error = np.r_[position - p, Rotation.from_matrix(rotation @ r.T).as_rotvec()]
                j = jacobian * _RESIDUAL_SCALE[:, None]
                update = np.linalg.solve(j.T @ j + _DAMPING, j.T @ (error * _RESIDUAL_SCALE)
                                         + _POSTURE_WEIGHT * (prior - q))
                # Keep each iteration local to the selected encoder branch.
                # 限制每次迭代的幅度，保持在选定的编码器分支附近。
                next_q = np.clip(q + update / max(1.0, np.max(np.abs(update)) / 0.20), lower, upper)
                if np.max(np.abs(next_q - q)) < 1e-5:
                    break
                q = next_q

            p, r, _ = arm.evaluate(q, jacobian=False)
            distance = float(np.linalg.norm(position - p))
            angle = Rotation.from_matrix(rotation @ r.T).magnitude()
            if distance > _MAX_POSITION_RESIDUAL_M or angle > _MAX_ORIENTATION_RESIDUAL_RAD:
                raise ValueError(f"PiperX EE {side} target projection residual: "
                                 f"{distance * 1000:.1f} mm / {np.rad2deg(angle):.1f} deg")
            diagnostics[side] = {"position_mm": distance * 1000, "orientation_deg": float(np.rad2deg(angle))}
            values[i] = q

        joints = np.empty(14)
        joints[JOINT_COLUMNS] = np.rad2deg(values.ravel())
        joints[GRIPPER_COLUMNS] = target[EEF_GRIPPERS]
        return joints, values, diagnostics
