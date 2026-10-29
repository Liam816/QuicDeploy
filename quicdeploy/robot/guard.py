"""Final joint-command guard between IK and the robot.
IK 与机械臂之间的最后一道关节指令保护。

Rejects (raises) on: joint-limit violation, optional tracking-error limit,
joint speed limit. Shapes grippers: optional binarization, stroke clipping,
optional software rate limit.
拒绝（抛异常）：关节越限、跟踪误差超限（可选）、关节速度超限。
夹爪处理：可选二值化、行程限幅、可选软件限速。
"""
import numpy as np

from ..common.layout import ACTION_KEYS, JOINT_COLUMNS
from ..common.validation import (
    validate_gripper_binary_thresholds,
    validate_gripper_feedback,
    validate_gripper_speed,
)


class CommandGuard:
    def __init__(self, settings):
        self.settings = settings
        self.gripper_thresholds = validate_gripper_binary_thresholds(settings.get("gripper_binary_thresholds_mm"))
        self.gripper_speed = validate_gripper_speed(settings["gripper_speed_mm_s"])
        self.previous = None  # last command actually let through / 上一条放行的指令

    def reset(self, joints):
        """Start speed limiting from measured joints. / 以实测关节作为限速起点。"""
        joints = np.asarray(joints, dtype=float)
        if joints.shape != (14,) or not np.isfinite(joints).all():
            raise ValueError("Invalid 14D measured joints")
        grip = validate_gripper_feedback(
            joints[[6, 13]], self.settings["gripper_widths_mm"],
            self.settings.get("gripper_feedback_tolerance_mm", 0.0),
        )
        self.previous = joints.copy()
        self.previous[[6, 13]] = grip

    def apply(self, target, measured, dt):
        """Check/shape one 14D command; returns (command, diagnostics).
        检查并整形一条 14D 指令，返回 (指令, 诊断信息)。
        """
        c = self.settings
        target = np.asarray(target, dtype=float).copy()
        measured = np.asarray(measured, dtype=float)
        if target.shape != (14,) or measured.shape != (14,) or not np.isfinite([target, measured]).all():
            raise ValueError("Invalid joint command / feedback")
        if not 0 < dt <= 0.05:
            raise ValueError("Invalid command timestep")
        feedback_grip = validate_gripper_feedback(
            measured[[6, 13]], c["gripper_widths_mm"], c.get("gripper_feedback_tolerance_mm", 0.0),
        )

        # Joint limits: absorb URDF radian round-off (<3e-7 deg) but never exceed.
        # 关节限位：吸收 URDF 弧度舍入误差，但绝不发送越限指令。
        limits = np.tile(np.asarray(c["joint_limits_deg"]), (2, 1))
        joints = target[JOINT_COLUMNS]
        outside = np.flatnonzero((joints < limits[:, 0] - 1e-6) | (joints > limits[:, 1] + 1e-6))
        if len(outside):
            detail = "; ".join(
                f"{ACTION_KEYS[JOINT_COLUMNS[i]]} target={joints[i]:.9f} "
                f"measured={measured[JOINT_COLUMNS[i]]:.9f} "
                f"limits=[{limits[i, 0]:.6f}, {limits[i, 1]:.6f}] "
                f"excess={max(limits[i, 0] - joints[i], joints[i] - limits[i, 1]):.9f} deg"
                for i in outside
            )
            raise ValueError(f"IK joint limit violation: {detail}")
        target[JOINT_COLUMNS] = np.clip(joints, limits[:, 0], limits[:, 1])

        # Tracking error: null disables the abort but the error is still logged.
        # 跟踪误差：配置为 null 时只记录不中止。
        tracking = target[JOINT_COLUMNS] - measured[JOINT_COLUMNS]
        worst = int(np.argmax(abs(tracking)))
        tracking_limit = c["max_tracking_error_deg"]
        if tracking_limit is not None and abs(tracking[worst]) > tracking_limit:
            column = JOINT_COLUMNS[worst]
            raise ValueError(
                f"IK/measured tracking error exceeded: {ACTION_KEYS[column]} "
                f"target={target[column]:.3f} measured={measured[column]:.3f} "
                f"error={tracking[worst]:.3f} limit={c['max_tracking_error_deg']:.3f} deg"
            )

        # Joint speed relative to the previous accepted command. / 相对上一条指令的关节速度。
        if np.max(abs(target[JOINT_COLUMNS] - self.previous[JOINT_COLUMNS])) > c["max_joint_speed_deg_s"] * dt + 1e-6:
            raise ValueError("IK joint command velocity exceeded")

        # Grippers / 夹爪
        raw_grip = target[[6, 13]].copy()
        desired_grip = raw_grip
        if self.gripper_thresholds is not None:
            desired_grip = np.where(raw_grip > self.gripper_thresholds, c["gripper_widths_mm"], 0.0)
        target[[6, 13]] = np.clip(desired_grip, 0, c["gripper_widths_mm"])
        bounded_grip = target[[6, 13]].copy()
        if self.gripper_speed is not None:
            target[[6, 13]] = np.clip(
                bounded_grip,
                self.previous[[6, 13]] - self.gripper_speed * dt,
                self.previous[[6, 13]] + self.gripper_speed * dt,
            )
        diagnostics = {
            "joint_measured": measured.tolist(),
            "gripper_feedback_endpoint_offset_mm": (measured[[6, 13]] - feedback_grip).tolist(),
            "joint_tracking_error_deg": tracking.tolist(),
            "max_tracking_error_deg": float(np.max(abs(tracking))),
            "tracking_stop_enabled": tracking_limit is not None,
            "tracking_limit_deg": tracking_limit,
            "joint_command_speed_deg_s": ((target[JOINT_COLUMNS] - self.previous[JOINT_COLUMNS]) / dt).tolist(),
            "requested_gripper_mm": raw_grip.tolist(),
            "gripper_binary_enabled": self.gripper_thresholds is not None,
            "gripper_binary_target_mm": None if self.gripper_thresholds is None else desired_grip.tolist(),
            "sent_gripper_mm": target[[6, 13]].tolist(),
            "gripper_rate_limit_enabled": self.gripper_speed is not None,
            "gripper_speed_mm_s": self.gripper_speed,
            "gripper_speed_limited": bool(np.any(abs(bounded_grip - target[[6, 13]]) > 1e-6)),
            "gripper_limited": bool(np.any(abs(raw_grip - target[[6, 13]]) > 1e-6)),
        }
        self.previous = target.copy()
        return target, diagnostics
