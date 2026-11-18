"""Resolve runtime YAML + model card + hardware bundle into one checked config.

把运行参数 YAML、模型卡和硬件包合并为一份经过完整校验的部署配置（不连接硬件）。

Layout of the resolved dict / 解析结果结构:
    model           model card + runtime device / 模型参数（含运行设备）
    robot           robot.json "robot" section, rate_hz = control_hz / 机械臂配置
    kinematics      URDF / IK yaml / extension paths, control_dt_s / 运动学文件
    guard, hold, motion_limits, runtime, observation, execution, postprocess
    timing          derived queue timing / 推导出的队列时序
    hardware_identity, config_sources (sha256 of every file read), fingerprint
"""
import os
from copy import deepcopy
from pathlib import Path

import numpy as np

from ..common.io import digest, load_json, load_yaml, resolve_path, sha256
from ..common.validation import is_int, is_number, validate_gripper_binary_thresholds, validate_gripper_speed
from .model import PHYSICAL_CONTRACT, PIPERX_EE_CONTRACT, PIPERX_EE_JOINT_NAMES, PIPERX_EE_URDF_SHA256, load_model_card
from .defaults import OPTIONAL_DEFAULTS

MODES = ("sync", "async", "rtc")
SECTION_KEYS = {
    "runtime": {"device", "cpu_threads"},
    "observation": {"max_camera_age_ms", "max_camera_skew_ms", "max_joint_age_ms"},
    "execution": {"mode", "prefix_steps", "control_hz", "policy_hz", "policy_overclock_enabled",
                  "n_action_steps", "prefetch_remaining_steps", "handoff_margin_s",
                  "max_inference_wait_s", "max_tick_lateness_s", "late_policy"},
    "postprocess": {"blend_s", "arm_delay_s", "gripper_delay_s"},
    "guard": {"joint_limits_deg", "gripper_widths_mm", "gripper_speed_mm_s", "max_tracking_error_deg",
              "max_joint_speed_deg_s", "gripper_feedback_tolerance_mm", "gripper_binary_thresholds_mm"},
    "hold": {"max_hold_drift_deg", "max_hold_image_change"},
    "motion_limits": {"max_chunk_displacement_m", "max_chunk_rotation_deg"},
}
OPTIONAL_KEYS = {"guard": {"gripper_binary_thresholds_mm"}}
HARDWARE_KEYS = {"schema_version", "physical_contract", "robot_config", "kinematics", "can_adapter_serials", "artifacts"}


def _keys(value, required, optional=(), *, label):
    """Exact key set: a misspelled setting must never silently do nothing.
    键集合必须精确匹配：拼错的参数绝不能被静默忽略。
    """
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be a mapping")
    missing, unknown = set(required) - value.keys(), value.keys() - set(required) - set(optional)
    if missing or unknown:
        raise ValueError(f"{label}: missing {sorted(missing)}, unknown {sorted(unknown)}")


def _nonnegative(section, name, value):
    if not is_number(value) or not np.isfinite(value) or value < 0:
        raise ValueError(f"Invalid {section}.{name}")


def validate_runtime(settings, model, *, rtc_capable):
    """All cross-checks between runtime settings and the model.
    运行参数与模型之间的全部交叉校验。
    """
    runtime = settings["runtime"]
    if runtime["device"] not in ("cpu", "cuda"):
        raise ValueError("Runtime must declare device cpu or cuda")
    if not is_int(runtime["cpu_threads"]) or runtime["cpu_threads"] < 1:
        raise ValueError("Invalid runtime CPU thread count")

    execution = settings["execution"]
    mode = execution["mode"]
    if mode not in MODES or (mode == "rtc" and not rtc_capable):
        raise ValueError(f"Model is not registered for execution mode {mode!r}")
    for key in ("n_action_steps", "prefetch_remaining_steps", "prefix_steps"):
        if not is_int(execution[key]):
            raise ValueError(f"Invalid integer execution.{key}")
    prefix_steps = execution["prefix_steps"]
    if not 0 <= prefix_steps <= model["fixed_prefix_steps"]:
        raise ValueError("RTC prefix exceeds registered training support")
    if (mode == "sync" and prefix_steps) or (mode == "rtc" and not prefix_steps):
        raise ValueError("Sync requires no prefix; rtc mode requires a prefix")
    if prefix_steps and not rtc_capable:
        raise ValueError("Model is not registered for RTC conditioning")

    # Rates / 频率
    for key in ("control_hz", "policy_hz"):
        value = execution[key]
        if not is_number(value) or not np.isfinite(value) or value <= 0:
            raise ValueError(f"Invalid execution.{key}")
    if execution["control_hz"] < 20 or execution["policy_hz"] > execution["control_hz"]:
        raise ValueError("Require control_hz >= 20 and policy_hz <= control_hz")
    if not isinstance(execution["policy_overclock_enabled"], bool):
        raise ValueError("Explicit policy_overclock_enabled is required")
    if not execution["policy_overclock_enabled"] and not np.isclose(execution["policy_hz"], 1 / model["action_dt_s"]):
        raise ValueError("Changing policy rate requires explicit policy_overclock_enabled")

    # Queue / 队列
    count, remaining = execution["n_action_steps"], execution["prefetch_remaining_steps"]
    if not 1 <= count <= model["horizon"]:
        raise ValueError("n_action_steps must lie within model horizon")
    if mode == "sync":
        if remaining != 0:
            raise ValueError("Sync mode requires prefetch_remaining_steps=0")
    elif not 1 <= remaining < count:
        raise ValueError("prefetch_remaining_steps must lie between 1 and n_action_steps-1")
    if prefix_steps and remaining < prefix_steps:
        raise ValueError("RTC remaining queue must cover the registered fixed prefix")
    _nonnegative("execution", "handoff_margin_s", execution["handoff_margin_s"])
    if not 0 < model["first_target_offset_s"] <= model["action_dt_s"]:
        raise ValueError("Queue playback requires the first target within one model action interval")

    # Budgets and post-processing / 时间预算与后处理
    for section, names in {
        "observation": ("max_camera_age_ms", "max_camera_skew_ms", "max_joint_age_ms"),
        "execution": ("max_inference_wait_s", "max_tick_lateness_s"),
        "postprocess": ("blend_s", "arm_delay_s", "gripper_delay_s"),
    }.items():
        for name in names:
            _nonnegative(section, name, settings[section][name])
    if execution["max_inference_wait_s"] == 0:
        raise ValueError("Inference timeout must be positive")
    if execution["late_policy"] not in ("hold", "fail"):
        raise ValueError("Explicit hold/fail late policy required")

    guard = settings["guard"]
    validate_gripper_binary_thresholds(guard.get("gripper_binary_thresholds_mm"))
    validate_gripper_speed(guard["gripper_speed_mm_s"])
    for key in ("max_tracking_error_deg", "max_joint_speed_deg_s"):
        limit = guard[key]
        if key == "max_tracking_error_deg" and limit is None:
            continue  # null: log tracking error, never abort / null 表示只记录不中止
        if not is_number(limit) or not np.isfinite(limit) or limit <= 0:
            raise ValueError("Invalid command guard limit: " + key)


def load_hardware(hardware_path, sources):
    """Hardware bundle: robot.json, kinematics files and pinned binaries.
    硬件包：robot.json、运动学文件以及被哈希锁定的二进制文件。
    """
    hardware = load_yaml(hardware_path)
    sources[str(hardware_path)] = sha256(hardware_path)
    _keys(hardware, HARDWARE_KEYS, label="hardware")
    if hardware["schema_version"] != 1:
        raise ValueError("Unsupported hardware version")
    physical = hardware["physical_contract"]
    piperx_ee = physical.get("frame") == PIPERX_EE_CONTRACT["frame"]
    expected_contract = PIPERX_EE_CONTRACT if piperx_ee else PHYSICAL_CONTRACT
    for key, expected in expected_contract.items():
        if hardware["physical_contract"].get(key) != expected:
            raise ValueError(f"Hardware bundle coordinate contract differs: {key}")
    robot_path = resolve_path(hardware["robot_config"], hardware_path)
    robot = load_json(robot_path)
    sources[str(robot_path)] = sha256(robot_path)
    _keys(robot, {"robot"}, label="robot config")
    kinematics = deepcopy(hardware["kinematics"])
    _keys(kinematics, {"urdf", "yaml", "extension", "python"}, {"solver_state_mode"}, label="hardware.kinematics")
    for key in ("urdf", "yaml", "extension", "python"):
        kinematics[key] = str(resolve_path(kinematics[key], hardware_path))
    for key in ("urdf", "yaml", "extension"):
        if not Path(kinematics[key]).is_file():
            raise ValueError(f"Missing hardware file: {kinematics[key]}")
    # The IK worker imports the extension from its own directory.
    # IK 子进程从扩展所在目录导入它。
    kinematics["extension_dir"] = str(Path(kinematics["extension"]).parent)
    ik_parameters = load_yaml(kinematics["yaml"])
    sources[kinematics["yaml"]] = sha256(kinematics["yaml"])
    # The native IK reads its URDF from ik.yaml; it must be the same geometry as FK.
    # 原生 IK 从 ik.yaml 读取 URDF，必须与 FK 使用的几何完全一致。
    kinematics["native_urdf"] = str(resolve_path(ik_parameters["model"]["urdf_path"], kinematics["yaml"]))
    if not Path(kinematics["native_urdf"]).is_file():
        raise ValueError(f"Missing IK URDF: {kinematics['native_urdf']}")
    if sha256(kinematics["native_urdf"]) != sha256(kinematics["urdf"]):
        raise ValueError("ik.yaml urdf_path differs from the FK URDF; FK and IK frames would disagree")
    for key in ("urdf", "native_urdf", "extension"):
        sources[kinematics[key]] = sha256(kinematics[key])
    if piperx_ee:
        if sources[kinematics["urdf"]] != PIPERX_EE_URDF_SHA256:
            raise ValueError("PiperX EE hardware profile requires the pinned training URDF")
        ik_model = ik_parameters["model"]
        if (ik_model["root_frame"] != physical["frame"]
                or ik_model["tcp_frames"] != [physical["ends"][side] for side in ("left", "right")]
                or ik_model["controlled_joints"] != PIPERX_EE_JOINT_NAMES
                or ik_model.get("tcp_definitions")):
            raise ValueError("PiperX EE IK frames or joint order differ from the training URDF")
        kinematics.update(frame=physical["frame"], ends=deepcopy(physical["ends"]),
                          ik_root_frame=physical["frame"], joint_names=PIPERX_EE_JOINT_NAMES.copy(),
                          native_base_proxies=True)
    if robot["robot"].get("log_dir"):
        robot["robot"]["log_dir"] = str(resolve_path(robot["robot"]["log_dir"], robot_path))
    initial = robot["robot"].get("initial_pose_file")
    if initial:
        robot["robot"]["initial_pose_file"] = str(resolve_path(initial, robot_path))
        sources[robot["robot"]["initial_pose_file"]] = sha256(robot["robot"]["initial_pose_file"])
    for file, expected in hardware["artifacts"].items():
        file = str(resolve_path(file, hardware_path))
        if sha256(file) != expected:
            raise ValueError(f"Hardware artifact changed: {file}")
    return {
        "robot": robot["robot"], "kinematics": kinematics, "ik_parameters": ik_parameters,
        "hardware_identity": {"can_adapter_serials": hardware["can_adapter_serials"]},
    }


def load_runtime_profile(path, sources, stack=()):
    """Merge explicit parent files; relative references belong to their declaring file.
    合并显式父配置；路径相对声明它的文件，循环继承立即报错。
    """
    path = Path(os.path.abspath(path))
    identity = path.resolve()
    if identity in stack:
        raise ValueError(f"Runtime inheritance cycle: {path}")
    raw = load_yaml(path)
    sources[str(path)] = sha256(path)
    version = raw.get("schema_version")
    if version == 1:
        _keys(raw, {"schema_version", "hardware", "model", *SECTION_KEYS}, label="runtime config")
        for section, keys in SECTION_KEYS.items():
            optional = OPTIONAL_KEYS.get(section, set())
            _keys(raw[section], keys - optional, optional, label=section)
    elif version == 2:
        _keys(raw, {"schema_version"}, {"extends", "hardware", "model", *SECTION_KEYS}, label="runtime config")
        for section, keys in SECTION_KEYS.items():
            if section in raw:
                _keys(raw[section], (), keys, label=section)
    else:
        raise ValueError("Unsupported runtime config version")
    merged = {}
    if "extends" in raw:
        parent = resolve_path(raw["extends"], path)
        merged = load_runtime_profile(parent, sources, (*stack, identity))
    for key, value in raw.items():
        if key in ("schema_version", "extends"):
            continue
        if key in ("hardware", "model"):
            merged[key] = str(resolve_path(value, path))
        elif key in SECTION_KEYS:
            merged[key] = {**merged.get(key, {}), **deepcopy(value)}
    return merged


def resolve(config_path, *, model_card=None):
    """Resolve v1 full configs or v2 inherited profiles; never connect hardware.
    解析 v1 完整配置或 v2 继承配置，全程不连接硬件。
    """
    path = Path(os.path.abspath(config_path))
    sources = {}
    raw = load_runtime_profile(path, sources)
    if "hardware" not in raw or (not model_card and "model" not in raw):
        raise ValueError("Runtime config requires hardware and model")
    settings = {section: {**deepcopy(OPTIONAL_DEFAULTS.get(section, {})),
                          **deepcopy(raw.get(section, {}))} for section in SECTION_KEYS}

    card_path = Path(os.path.abspath(model_card)) if model_card else resolve_path(raw["model"], path)
    card = load_model_card(card_path)
    sources[str(card_path)] = sha256(card_path)
    model = dict(card["model"], device=settings["runtime"]["device"], cpu_threads=settings["runtime"]["cpu_threads"])

    hardware = load_hardware(resolve_path(raw["hardware"], path), sources)
    for key in ("frame", "ends"):
        if model.get(key, PHYSICAL_CONTRACT[key]) != hardware["kinematics"].get(key, PHYSICAL_CONTRACT[key]):
            raise ValueError(f"Model and hardware coordinate contracts differ: {key}")
    if model.get("artifact_format") in ("quicforge_lva_v1", "quicforge_temporal_lva_v1"):
        if model["artifact_format"] == "quicforge_temporal_lva_v1":
            from .temporal import load_bundle
        else:
            from .quicforge import load_bundle

        conversion = load_bundle(model, verify_hashes=False)["conversion"]
        if conversion["kinematics"].get("urdf_sha256") != sha256(hardware["kinematics"]["urdf"]):
            raise ValueError("QuicForge training URDF differs from deployment kinematics")
    robot = hardware["robot"]
    # Derive absent values only: explicit disagreements must still fail validation.
    # 只补缺省值；显式填写但不一致的配置仍然必须报错。
    settings["execution"].setdefault("policy_hz", 1 / model["action_dt_s"])
    settings["guard"].setdefault("gripper_widths_mm", [robot["left_gripper_width_mm"], robot["right_gripper_width_mm"]])
    for section, keys in SECTION_KEYS.items():
        _keys(settings[section], keys, label=section)
    validate_runtime(settings, model, rtc_capable=model.get("fixed_prefix_steps", 0) > 0)
    if [robot["left_gripper_width_mm"], robot["right_gripper_width_mm"]] != settings["guard"]["gripper_widths_mm"]:
        raise ValueError("Robot and command guard gripper widths disagree")

    execution = settings["execution"]
    count, remaining, prefix = execution["n_action_steps"], execution["prefetch_remaining_steps"], execution["prefix_steps"]
    # control_hz is authoritative for the IK workers and the native MIT loop.
    # control_hz 同时决定 IK 积分步长与原生 MIT 控制频率。
    robot["rate_hz"] = execution["control_hz"]
    hardware["kinematics"]["control_dt_s"] = 1 / execution["control_hz"]
    hardware["ik_parameters"]["ik"]["dt"] = 1 / execution["control_hz"]
    resolved = {
        "schema_version": 1,
        "model_card": str(card_path),
        "model": model,
        "metadata": deepcopy(card.get("metadata", {})),
        **hardware,
        **settings,
        "timing": {
            "training_hz": 1 / model["action_dt_s"], "policy_hz": execution["policy_hz"],
            "control_hz": execution["control_hz"], "policy_dt_s": 1 / execution["policy_hz"],
            "trigger_after_steps": count - remaining,
            "prefix_rows_one_based": [count - remaining + 1, count - remaining + prefix] if prefix else None,
        },
        "config_sources": sources,
    }
    resolved["fingerprint"] = digest(resolved)
    return resolved
