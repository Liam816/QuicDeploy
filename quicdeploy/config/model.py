"""Model cards: contract validation and artifact integrity.

模型卡：物理契约校验与模型产物完整性校验。

A model card (configs/models/*.json) is
    {"schema_version": 2, "model": {...}, "metadata": {...}, "artifacts": {path: sha256}}
Paths may be absolute (legacy) or relative to the card; load returns absolute paths.
路径可为旧绝对路径，或相对模型卡的路径；加载结果统一为绝对路径。
`register` freezes the artifact hashes; `load_model_card` re-verifies them on
every start, so a silently replaced checkpoint can never reach the robot.
register 冻结产物哈希；每次启动 load_model_card 都会重新校验，
被悄悄替换的权重永远到不了真机。
"""
import json
import os
from copy import deepcopy
from pathlib import Path

import numpy as np

from ..common.io import load_json, resolve_path, sha256

# Coordinate/units contract shared by model, hardware bundle and runtime.
# 模型、硬件包与运行时三方共享的坐标/单位契约。
PHYSICAL_CONTRACT = {
    "protocol": "eef20_relative",
    "frame": "world",
    "ends": {"left": "left_Link6", "right": "right_Link6"},
    "position_unit": "m",
    "gripper_unit": "mm",
    "rotation": "columns6d",
}
CAMERA_ORDER = ["left_wrist", "head", "right_wrist"]
ARTIFACT_KEYS = ("weights", "normalization", "export_metadata")
REQUIRED_KEYS = (
    "adapter", "weights", "normalization", "cameras", "preprocessing", "uses_state",
    "uses_language", "protocol", "horizon", "action_dt_s", "first_target_offset_s", "supervision",
    "position_unit", "gripper_unit", "rotation", "frame", "ends", "timing_domain",
)


DESCRIPTIVE_KEYS = {"name", "architecture", "demo", "dataset_split", "training_source_commit", "noise_generator"}
MODEL_KEYS = {*REQUIRED_KEYS, "id", "artifact_dir", "artifact_format", "source_dir", "export_metadata",
              "implementation_files", "output_domain", "fixed_prefix_steps", "preproc_sha256", "weights_sha256",
              "runtime_versions", "target_contract", "language", "num_steps", "guidance_scale", "noise_std",
              "weights_variant", "weight_dtype", "layerscale_dtype", "temporal_sampling"}
LVA_ADAPTERS = {
    "quicdeploy.policy.lva:" + name: precision for name, precision in (
        ("LVAAdapter", "float32"), ("LVAFP16Adapter", "float16"),
        ("LVAActionGTFP16Adapter", "float16"), ("LVAActionGTFP32Adapter", "float32"))
}
LVA_ADAPTERS.update({"quicdeploy.policy.quicforge:QuicForgeLVAAdapter": "float32",
                     "quicdeploy.policy.quicforge:QuicForgeLVAFP16Adapter": "float16"})
LVA_ADAPTERS.update({"quicdeploy.policy.temporal:TemporalLVAAdapter": "float32",
                     "quicdeploy.policy.temporal:TemporalLVAFP16Adapter": "float16"})


def adapter_defaults(adapter):
    """Known, validated execution capabilities, never inferred from a filename.
    已支持适配器的固定执行能力，不根据文件名猜测。
    """
    return {"num_steps": 10, "guidance_scale": 0, "noise_std": 1, "weights_variant": "model",
            "weight_dtype": LVA_ADAPTERS[adapter], "layerscale_dtype": "float32",
            "uses_state": False, "uses_language": False}


def expand_model(card):
    """Expand v2 fixed adapter capabilities; v1 values retain their original meaning.
    展开 v2 的固定适配器能力；v1 参数保持原意，溯源信息不允许覆盖运行参数。
    """
    model = deepcopy(card["model"])
    if card.get("schema_version", 1) == 1:
        return model
    if not isinstance(model, dict):
        raise ValueError("Model must be a mapping")
    if set(model) - MODEL_KEYS:
        raise ValueError("Unknown v2 model fields: " + str(sorted(set(model) - MODEL_KEYS)))
    metadata = card.get("metadata", {})
    if not isinstance(metadata, dict):
        raise ValueError("Model metadata must be a mapping")
    adapter = model.get("adapter")
    if adapter in LVA_ADAPTERS:
        for key, value in adapter_defaults(adapter).items():
            if key in model and model[key] != value:
                raise ValueError(f"LVA adapter capability mismatch: {key}")
            model.setdefault(key, value)
        preprocessing = model.get("preprocessing", {})
        if not isinstance(preprocessing, dict) or set(preprocessing) - {"resize", "color", "crop", "mean", "std"}:
            raise ValueError("Unknown LVA preprocessing fields")
        model["preprocessing"] = {"resize": [480, 640], "color": "RGB", **preprocessing}
        if model["preprocessing"]["resize"] != [480, 640] or model["preprocessing"]["color"] != "RGB":
            raise ValueError("LVA requires RGB input at 480x640; resize/color are not conversion switches")
        identity = metadata.get("checkpoint", {})
        if not isinstance(identity, dict) or not {"epoch", "global_step"} <= identity.keys():
            raise ValueError("LVA metadata.checkpoint requires epoch and global_step for identity checks")
        model["checkpoint_epoch"], model["global_step"] = identity["epoch"], identity["global_step"]
    # Current Action-GT predicts its first target one training step after observation.
    # 当前 Action-GT 首个目标位于观测后一个训练步；显式值仍交由契约校验。
    if model.get("supervision") == "action" and model.get("target_contract", {}).get("first_target_offset_steps") == 1:
        model.setdefault("first_target_offset_s", model["action_dt_s"])
    return model


def compact_card(card):
    """Convert a registered v1 card to v2 without dropping non-default settings.
    将已注册的 v1 模型卡转换为 v2；保留所有非默认参数和原有元数据。
    """
    if card.get("schema_version") == 2:
        return deepcopy(card)
    model = deepcopy(card["model"])
    metadata = deepcopy(card.get("metadata", {}))
    for key in DESCRIPTIVE_KEYS:
        if key in model:
            metadata[key] = model.pop(key)
    for key in ("device", "cpu_threads", "checkpoint_sha256"):
        model.pop(key, None)  # runtime-owned or duplicate artifact hash / 运行时参数或重复哈希
    if model.get("adapter") in LVA_ADAPTERS:
        metadata["checkpoint"] = {"epoch": model.pop("checkpoint_epoch"), "global_step": model.pop("global_step")}
        for key, value in adapter_defaults(model["adapter"]).items():
            if model.get(key) == value:
                model.pop(key)
        for key, value in {"resize": [480, 640], "color": "RGB"}.items():
            if model.get("preprocessing", {}).get(key) == value:
                model["preprocessing"].pop(key)
    if (model.get("supervision") == "action"
            and model.get("target_contract", {}).get("first_target_offset_steps") == 1
            and model.get("first_target_offset_s") == model.get("action_dt_s")):
        model.pop("first_target_offset_s")
    return {"schema_version": 2, "model": model, "metadata": metadata,
            **({"artifacts": deepcopy(card["artifacts"])} if "artifacts" in card else {})}


def validate_model(model):
    for key in REQUIRED_KEYS:
        if key not in model:
            raise ValueError(f"Model config missing {key}")
    if model["horizon"] <= 0 or model["action_dt_s"] <= 0 or model["first_target_offset_s"] < 0:
        raise ValueError("Invalid explicit model timing")
    for key in ARTIFACT_KEYS:
        if model.get(key) and not Path(model[key]).is_file():
            raise ValueError(f"Missing {key}: {model[key]}")
    if model["uses_language"] and not model.get("language"):
        raise ValueError("Language-conditioned model requires explicit language")
    for key, value in PHYSICAL_CONTRACT.items():
        if model.get(key) != value:
            raise ValueError(f"Model physical contract mismatch: {key}")
    if model.get("output_domain") != "physical":
        raise ValueError("Adapter must output physical EEF20")
    if model.get("cameras") != CAMERA_ORDER:
        raise ValueError("Piper requires left_wrist/head/right_wrist order")
    if model.get("supervision") not in ("action", "state_pose_action_gripper"):
        raise ValueError("Unknown training supervision; delay handling would be ambiguous")
    if model.get("fixed_prefix_steps", 0) < 0:
        raise ValueError("Invalid fixed-prefix length")
    if model["fixed_prefix_steps"] >= model["horizon"]:
        raise ValueError("Fixed prefix must be shorter than horizon")
    if not np.isfinite(model["action_dt_s"]) or not np.isfinite(model["first_target_offset_s"]):
        raise ValueError("Non-finite model timing")
    if model["timing_domain"] != "stored_recording_time":
        raise ValueError("Unknown training time semantics")
    if model.get("adapter", "").startswith("quicdeploy.policy.quicforge:") and model.get("artifact_format") != "quicforge_lva_v1":
        raise ValueError("QuicForge adapter requires an explicit supported artifact_format")
    if model.get("artifact_format") == "quicforge_lva_v1":
        from .quicforge import load_bundle

        bundle = load_bundle(model, verify_hashes=False)
        expected = {str(p.resolve()) for p in bundle["paths"].values()}
        expected.add(str(Path(model["export_metadata"]).resolve()))
        if not expected <= set(artifact_paths(model)):
            raise ValueError("QuicForge model card must pin every bundle member")
        backend = Path(__file__).parents[1] / "policy/backends/quicforge_lva"
        code = {backend / name for name in ("__init__.py", "model.py", "decoder.py", "dinov3.py", "task_ids.py", "PROVENANCE.json")}
        code |= {Path(__file__).parents[1] / p for p in ("policy/quicforge.py", "policy/lva.py", "config/quicforge.py")}
        if not {str(p.resolve()) for p in code} <= set(artifact_paths(model)):
            raise ValueError("QuicForge model card must pin adapter and frozen network code")
    if model.get("adapter", "").startswith("quicdeploy.policy.temporal:") and model.get("artifact_format") != "quicforge_temporal_lva_v1":
        raise ValueError("Temporal adapter requires quicforge_temporal_lva_v1")
    if model.get("artifact_format") == "quicforge_temporal_lva_v1":
        from .temporal import load_bundle, code_paths

        bundle = load_bundle(model, verify_hashes=False)
        expected = {str(p.resolve()) for p in (*bundle["paths"].values(), *code_paths())}
        expected.add(str(Path(model["export_metadata"]).resolve()))
        if not expected <= set(artifact_paths(model)):
            raise ValueError("Temporal card must pin all bundle members and inference code")


def resolved_model(model, owner):
    """Paths belong to the card, not the caller's cwd. / 模型路径相对模型卡，而非 cwd。"""
    result = deepcopy(model)
    for key in (*ARTIFACT_KEYS, "source_dir", "artifact_dir"):
        if result.get(key):
            result[key] = str(resolve_path(result[key], owner))
    result["implementation_files"] = [str(resolve_path(p, owner)) for p in result.get("implementation_files", [])]
    return result


def artifact_paths(model):
    paths = [model[key] for key in ARTIFACT_KEYS if model.get(key)]
    paths.extend(model.get("implementation_files", []))
    return sorted({str(Path(path).resolve()) for path in paths})


def register(card_path, *, relative=False):
    """Hash every model artifact into the card (in place). / 计算并写入全部产物哈希。"""
    path = Path(card_path)
    card = load_json(path)
    version = card.get("schema_version", 1)
    allowed = {"schema_version", "model", "artifacts"} | ({"metadata"} if version == 2 else set())
    if version not in (1, 2) or set(card) - allowed or "model" not in card:
        raise ValueError("Invalid model card schema")
    model = resolved_model(expand_model(card), path)
    validate_model(model)
    card = {"schema_version": version, "model": card["model"],
            **({"metadata": card.get("metadata", {})} if version == 2 else {}),
            "artifacts": {(os.path.relpath(file, path.resolve().parent) if relative else file): sha256(file)
                          for file in artifact_paths(model)}}
    path.write_text(json.dumps(card, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")
    return card


def load_model_card(card_path):
    card = load_json(card_path)
    version = card.get("schema_version")
    required = {"schema_version", "model", "artifacts"} | ({"metadata"} if version == 2 else set())
    if version not in (1, 2) or set(card) != required:
        raise ValueError(f"Invalid model card: {card_path}")
    model = resolved_model(expand_model(card), card_path)
    artifacts = {str(resolve_path(file, card_path).resolve()): expected
                 for file, expected in card["artifacts"].items()}
    if len(artifacts) != len(card["artifacts"]) or sorted(artifacts) != artifact_paths(model):
        raise ValueError("Model card artifacts are incomplete or duplicated; run register-model")
    for file, expected in artifacts.items():
        if sha256(file) != expected:
            raise ValueError(f"Model artifact changed after registration: {file}")
    validate_model(model)
    return {**card, "model": model, "artifacts": artifacts}
