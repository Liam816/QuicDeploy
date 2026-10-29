"""Small value validators shared by config, guard and observer.
配置、指令保护与观测模块共用的数值校验。
"""
import numpy as np


def is_number(value):
    """Real int/float, rejecting bool. / 真正的数值类型，排除 bool。"""
    return not isinstance(value, bool) and isinstance(value, (int, float))


def is_int(value):
    return not isinstance(value, bool) and isinstance(value, int)


def validate_gripper_feedback(values, widths, tolerance_mm=0.0, *, label="Measured gripper"):
    """Validate encoder readings against [0, width] +/- a small endpoint tolerance.

    夹爪编码器读数需落在 [0, 行程] ± 容差内；容差仅用于反馈，指令仍严格限幅。
    Returns clipped values for command initialization only, never for raw logs.
    返回值只用于初始化指令，不得替换原始日志。
    """
    values, widths = np.asarray(values, dtype=float), np.asarray(widths, dtype=float)
    if (values.shape != (2,) or widths.shape != (2,)
            or not np.isfinite([values, widths]).all() or np.any(widths <= 0)):
        raise ValueError(f"{label}: expected two finite readings and positive physical widths")
    if not np.isfinite(tolerance_mm) or not 0 <= tolerance_mm <= 0.5:
        raise ValueError("Gripper feedback tolerance must be in [0, 0.5] mm")
    if np.any(values < -tolerance_mm) or np.any(values > widths + tolerance_mm):
        raise ValueError(
            f"{label} outside physical range: measured={values.tolist()} mm, "
            f"nominal=[0, {widths.tolist()}] mm, feedback_tolerance={tolerance_mm:g} mm"
        )
    return np.clip(values, 0, widths)


def validate_gripper_binary_thresholds(value):
    """Optional left/right thresholds (mm); null disables binarization.
    可选的左右夹爪二值化阈值（mm）；null 表示不二值化。
    """
    if value is None:
        return None
    if not isinstance(value, (list, tuple)) or len(value) != 2 or not all(is_number(x) for x in value):
        raise ValueError("gripper_binary_thresholds_mm requires two finite nonnegative numbers or null")
    thresholds = np.asarray(value, dtype=float)
    if not np.isfinite(thresholds).all() or np.any(thresholds < 0):
        raise ValueError("gripper_binary_thresholds_mm requires two finite nonnegative numbers or null")
    return thresholds


def validate_gripper_speed(value):
    """Null sends the bounded target directly, as native teleoperation does.
    null 表示与原生遥操作一致、直接发送限幅后的目标；正数表示额外软件限速 (mm/s)。
    """
    if value is None:
        return None
    if not is_number(value) or not np.isfinite(value) or value <= 0:
        raise ValueError("gripper_speed_mm_s must be finite and positive, or null for direct targets")
    return float(value)
