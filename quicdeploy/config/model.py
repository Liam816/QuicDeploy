"""Model cards: contract validation and artifact integrity.

模型卡：物理契约校验与模型产物完整性校验。

A model card (configs/models/*.json) is
    {"schema_version": 1, "model": {...}, "artifacts": {path: sha256}}
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
    if set(card) - {"schema_version", "model", "artifacts"} or "model" not in card:
        raise ValueError("Model card must contain 'model' (and optionally 'artifacts')")
    model = resolved_model(card["model"], path)
    validate_model(model)
    card = {"schema_version": 1, "model": card["model"],
            "artifacts": {(os.path.relpath(file, path.resolve().parent) if relative else file): sha256(file)
                          for file in artifact_paths(model)}}
    path.write_text(json.dumps(card, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")
    return card


def load_model_card(card_path):
    card = load_json(card_path)
    if card.get("schema_version") != 1 or set(card) != {"schema_version", "model", "artifacts"}:
        raise ValueError(f"Invalid model card: {card_path}")
    model = resolved_model(card["model"], card_path)
    artifacts = {str(resolve_path(file, card_path).resolve()): expected
                 for file, expected in card["artifacts"].items()}
    if len(artifacts) != len(card["artifacts"]) or sorted(artifacts) != artifact_paths(model):
        raise ValueError("Model card artifacts are incomplete or duplicated; run register-model")
    for file, expected in artifacts.items():
        if sha256(file) != expected:
            raise ValueError(f"Model artifact changed after registration: {file}")
    validate_model(model)
    return {**card, "model": model, "artifacts": artifacts}
