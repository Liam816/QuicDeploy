"""Model cards: contract validation and artifact integrity.

模型卡：物理契约校验与模型产物完整性校验。

A model card (configs/models/*.json) is
    {"schema_version": 1, "model": {...}, "artifacts": {abs_path: sha256}}
`register` freezes the artifact hashes; `load_model_card` re-verifies them on
every start, so a silently replaced checkpoint can never reach the robot.
register 冻结产物哈希；每次启动 load_model_card 都会重新校验，
被悄悄替换的权重永远到不了真机。
"""
import json
from pathlib import Path

import numpy as np

from ..common.io import load_json, sha256

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
    "name", "adapter", "architecture", "weights", "normalization", "cameras", "preprocessing", "uses_state",
    "uses_language", "protocol", "horizon", "action_dt_s", "first_target_offset_s", "supervision",
    "position_unit", "gripper_unit", "rotation", "demo", "frame", "ends", "timing_domain",
)


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


def artifact_paths(model):
    paths = [model[key] for key in ARTIFACT_KEYS if model.get(key)]
    paths.extend(model.get("implementation_files", []))
    return sorted({str(Path(path).resolve()) for path in paths})


def register(card_path):
    """Hash every model artifact into the card (in place). / 计算并写入全部产物哈希。"""
    path = Path(card_path)
    card = load_json(path)
    if set(card) - {"schema_version", "model", "artifacts"} or "model" not in card:
        raise ValueError("Model card must contain 'model' (and optionally 'artifacts')")
    validate_model(card["model"])
    card = {"schema_version": 1, "model": card["model"],
            "artifacts": {file: sha256(file) for file in artifact_paths(card["model"])}}
    path.write_text(json.dumps(card, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")
    return card


def load_model_card(card_path):
    card = load_json(card_path)
    if card.get("schema_version") != 1 or set(card) != {"schema_version", "model", "artifacts"}:
        raise ValueError(f"Invalid model card: {card_path}")
    model = card["model"]
    if sorted(card["artifacts"]) != artifact_paths(model):
        raise ValueError("Model card artifacts are incomplete; run register-model")
    for file, expected in card["artifacts"].items():
        if sha256(file) != expected:
            raise ValueError(f"Model artifact changed after registration: {file}")
    validate_model(model)
    return card
