"""Typed configuration for the dual Piper-X robot and its cameras.
双臂 Piper-X 机器人与相机的强类型配置（从 robot.json 的 "robot" 段解析）。
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field, fields

DEFAULT_JOINT_LIMITS_DEG = (
    (-154.0, 154.0),
    (-7.0, 186.0),
    (-177.0, 7.0),
    (-97.0, 97.0),
    (-97.0, 97.0),
    (-186.0, 186.0),
)


@dataclass
class CameraConfig:
    index_or_path: str
    width: int
    height: int
    fps: float
    fourcc: str | None = None
    warmup_s: float = 1
    backend: str = "ANY"          # "ANY" | "V4L2"
    color_mode: str = "rgb"       # model preprocessing expects RGB / 模型预处理要求 RGB
    type: str = "opencv"

    def __post_init__(self):
        if self.type != "opencv":
            raise ValueError("Only OpenCV cameras are supported")
        if self.fourcc is not None and (not isinstance(self.fourcc, str) or len(self.fourcc) != 4):
            raise ValueError(f"fourcc must be a 4-character string, got {self.fourcc!r}")
        if self.backend not in ("ANY", "V4L2"):
            raise ValueError(f"Unsupported OpenCV backend {self.backend!r}")
        if self.color_mode not in ("rgb", "bgr"):
            raise ValueError(f"Invalid color mode {self.color_mode!r}")


@dataclass
class PiperConfig:
    """Fields consumed by the parent driver and forwarded to native workers.
    父进程驱动使用、并转发给原生 MIT 子进程的字段。
    """

    left_can: str = "can0"
    right_can: str = "can1"
    left_gripper_width_mm: float = 70.0
    right_gripper_width_mm: float = 100.0
    rate_hz: float = 90.0                 # overridden by execution.control_hz / 由 control_hz 覆盖
    speed_percent: int = 50
    max_deg_s: float = 250.0
    max_acc_deg_s2: float = 700.0
    startup_home_speed_deg_s: float = 6.0
    startup_home_acc_deg_s2: float = 15.0
    episode_home_speed_deg_s: float = 12.0
    episode_home_acc_deg_s2: float = 30.0
    command_timeout_s: float = 0.5
    feedback_timeout_s: float = 0.5
    response_timeout_s: float = 1.0
    startup_timeout_s: float = 150.0
    joint_limits_deg: tuple = DEFAULT_JOINT_LIMITS_DEG
    # Pinned upstream MIT driver (teleop_platform_xk) and its Python env.
    # 固定版本的上游 MIT 驱动仓库及其 Python 环境。
    reference_dir: str = "/home/ih/workspace/yihuigou/teleop_platform_xk"
    expected_reference_commit: str = "5942fff6f572adffbd6e0bc7c088e51493a568ab"
    quest_python: str = "/home/ih/workspace/piper_pipeline/envs/quest3/bin/python"
    initial_pose_file: str | None = None
    log_dir: str = "/tmp/quicinfer-piper-native-logs"
    cameras: dict[str, CameraConfig] = field(default_factory=dict)
    # Descriptive metadata only. / 仅作描述用的元数据。
    type: str = "bi_piper_native"
    id: str | None = None
    calibration_dir: str | None = None

    @classmethod
    def from_dict(cls, spec, *, rate_hz=None):
        known = {f.name for f in fields(cls)}
        unknown = set(spec) - known
        if unknown:
            raise ValueError(f"Unknown robot config keys: {sorted(unknown)}")
        values = dict(spec)
        values["cameras"] = {name: CameraConfig(**cam) for name, cam in spec.get("cameras", {}).items()}
        if "joint_limits_deg" in values:
            values["joint_limits_deg"] = tuple(tuple(float(x) for x in pair) for pair in values["joint_limits_deg"])
        if rate_hz is not None:
            values["rate_hz"] = rate_hz
        return cls(**values)

    def __post_init__(self):
        if self.type != "bi_piper_native":
            raise ValueError("This driver requires robot type bi_piper_native")
        if len(self.joint_limits_deg) != 6 or any(
            len(pair) != 2 or not all(math.isfinite(x) for x in pair) or pair[0] >= pair[1]
            for pair in self.joint_limits_deg
        ):
            raise ValueError("Expected six finite joint limit pairs")
        if self.left_can == self.right_can:
            raise ValueError("Left and right CAN interfaces must differ")
        for port in (self.left_can, self.right_can):
            if not port or not port.replace("_", "").replace("-", "").isalnum():
                raise ValueError("Invalid CAN interface name")
        for key in (
            "left_gripper_width_mm", "right_gripper_width_mm", "rate_hz", "max_deg_s", "max_acc_deg_s2",
            "startup_home_speed_deg_s", "startup_home_acc_deg_s2", "episode_home_speed_deg_s",
            "episode_home_acc_deg_s2", "command_timeout_s", "feedback_timeout_s", "response_timeout_s",
            "startup_timeout_s",
        ):
            value = getattr(self, key)
            if not math.isfinite(value) or value <= 0:
                raise ValueError(f"{key} must be finite and positive")
        # Homing must stay slow. / 回初始位必须足够慢。
        if self.startup_home_speed_deg_s > 30 or self.startup_home_acc_deg_s2 > 100:
            raise ValueError("Startup homing limits must be <= 30 deg/s and <= 100 deg/s²")
        if self.episode_home_speed_deg_s > 30 or self.episode_home_acc_deg_s2 > 100:
            raise ValueError("Episode homing limits must be <= 30 deg/s and <= 100 deg/s²")
        if not 10 <= self.speed_percent <= 100:
            raise ValueError("speed_percent must be in [10, 100]")

    def gripper_width(self, side):
        return getattr(self, f"{side}_gripper_width_mm")
