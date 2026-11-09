"""Optional runtime values, independent of the selected model and hardware.
可省略的运行参数；模型频率与夹爪宽度由解析器从各自唯一来源推导。
"""

# Safety thresholds remain explicit in configs/defaults.yaml.
# 保护阈值仍在 configs/defaults.yaml 显式声明，不在代码里悄悄补齐。
OPTIONAL_DEFAULTS = {
    "runtime": {"device": "cuda", "cpu_threads": 4},
    "execution": {"policy_overclock_enabled": False},
    "postprocess": {"arm_delay_s": 0.0, "gripper_delay_s": 0.0},
    "guard": {"gripper_speed_mm_s": None, "max_tracking_error_deg": None,
              "gripper_binary_thresholds_mm": None},
}
