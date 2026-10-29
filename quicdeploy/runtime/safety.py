"""Runtime safety checks around inference and the control tick.
推理与控制节拍相关的运行时安全检查。
"""
import numpy as np

from ..common.geometry import pose_errors
from ..common.layout import JOINT_COLUMNS
from ..common.validation import validate_gripper_feedback


class SceneChangedError(RuntimeError):
    """Cameras changed while the robot was held for inference.
    机械臂静止等待推理期间画面发生了变化，该预测作废。
    """


class ControlTimingError(RuntimeError):
    """A control or request tick exceeded its time budget. / 控制/请求节拍超出时间预算。"""


def check_tick_budget(elapsed, period, lateness, label, parts=None):
    # Lateness is measured beyond the normal period. / 超时量按超出正常周期的部分计算。
    if elapsed > period + lateness:
        raise ControlTimingError(f"{label}: elapsed={elapsed:.6f}s, period={period:.6f}s, "
                                 f"lateness_limit={lateness:.6f}s; {parts or {}}")


def verify_chunk(motion_limits, targets, anchor):
    """Reject chunks that move a wrist too far from the anchor.
    拒绝手腕相对锚点位移/旋转过大的动作块。
    """
    for base in (0, 10):
        if np.max(np.linalg.norm(targets[:, base:base + 3] - anchor[base:base + 3], axis=1)) > motion_limits["max_chunk_displacement_m"]:
            raise RuntimeError("Model chunk exceeds wrist displacement limit")
    errors = pose_errors(targets, np.tile(anchor, (len(targets), 1)))
    if any(max(errors[side]["orientation_deg"]) > motion_limits["max_chunk_rotation_deg"] for side in ("left", "right")):
        raise RuntimeError("Model chunk exceeds wrist rotation limit")


def confirm_still(
    before_joints, after_joints, before_images, after_images, config,
    *, gripper_target_mm=None, gripper_widths_mm=None, gripper_feedback_tolerance_mm=0.0,
):
    """Verify robot and scene stayed still during a held inference.

    校验静止推理期间机械臂与场景保持不变：
      * arm joints drift <= max_hold_drift_deg / 关节漂移不超过阈值
      * grippers stay inside the envelope towards the last ACCEPTED command
        夹爪只允许朝最后一次被接受的指令方向运动（含 2mm 容差）
      * mean absolute image change <= max_hold_image_change, else SceneChangedError
        图像平均绝对变化不超过阈值，否则抛 SceneChangedError
    """
    before_joints = np.asarray(before_joints, dtype=float)
    after_joints = np.asarray(after_joints, dtype=float)
    if (before_joints.shape != (14,) or after_joints.shape != (14,)
            or not np.isfinite([before_joints, after_joints]).all()):
        raise RuntimeError("Invalid joint/gripper feedback while waiting for inference")
    if np.max(abs(before_joints[JOINT_COLUMNS] - after_joints[JOINT_COLUMNS])) > config["max_hold_drift_deg"]:
        raise RuntimeError("Robot moved while waiting for inference; discard chunk")
    before, after = before_joints[[6, 13]], after_joints[[6, 13]]
    tolerance_mm = 2.0
    if gripper_target_mm is None:
        lower, upper = before - tolerance_mm, before + tolerance_mm
        target = None
        mode = "strict_hold"
    else:
        target = np.asarray(gripper_target_mm, dtype=float)
        widths = np.asarray(gripper_widths_mm, dtype=float)
        if (target.shape != (2,) or widths.shape != (2,)
                or not np.isfinite([target, widths]).all() or np.any(widths <= 0)):
            raise ValueError("Held gripper command requires two finite targets and physical widths")
        if np.any(target < 0) or np.any(target > widths):
            raise RuntimeError(f"Held gripper target outside physical range: {target.tolist()} mm")
        # Native hold keeps the latched gripper command: allow motion along the
        # segment towards it. / 原生保持会继续执行已锁存的夹爪指令，允许沿该方向运动。
        lower = np.minimum(before, target) - tolerance_mm
        upper = np.maximum(before, target) + tolerance_mm
        mode = "settle_to_accepted_command"
    if gripper_widths_mm is not None:
        widths = np.asarray(gripper_widths_mm, dtype=float)
        try:
            for name, values in (("before", before), ("after", after)):
                validate_gripper_feedback(values, widths, gripper_feedback_tolerance_mm, label=f"Held gripper {name}")
        except ValueError as exc:
            raise RuntimeError(str(exc)) from exc
        lower = np.maximum(-gripper_feedback_tolerance_mm, lower)
        upper = np.minimum(widths + gripper_feedback_tolerance_mm, upper)
    bad = np.flatnonzero((after < lower) | (after > upper))
    if len(bad):
        i = int(bad[0])
        side = ("left", "right")[i]
        goal = "none" if target is None else f"{target[i]:.3f}"
        raise RuntimeError(
            f"Gripper moved outside held-command envelope: {side} "
            f"before={before[i]:.3f} after={after[i]:.3f} target={goal} "
            f"allowed=[{lower[i]:.3f}, {upper[i]:.3f}] mm; discard chunk"
        )
    for name in before_images:
        error = float(np.mean(abs(before_images[name].astype(float) - after_images[name].astype(float))) / 255)
        if error > config["max_hold_image_change"]:
            raise SceneChangedError(
                f"{name}: scene changed during held inference "
                f"({error:.3f} > {config['max_hold_image_change']:.3f}); discard chunk"
            )
    return dict(
        gripper_check=mode, gripper_before_mm=before.tolist(), gripper_after_mm=after.tolist(),
        gripper_feedback_tolerance_mm=gripper_feedback_tolerance_mm,
        held_gripper_target_mm=None if target is None else target.tolist(),
        gripper_motion_mm=(after - before).tolist(),
        gripper_allowed_lower_mm=lower.tolist(), gripper_allowed_upper_mm=upper.tolist(),
    )
