"""Fail-closed artifact contracts and portable cards; tiny CPU-only loader tests.
严格拒绝不兼容契约、篡改文件和缺失权重；仅用小型 CPU 网络测试加载行为。
"""
import copy
import json
import os
from pathlib import Path

import numpy as np
import pytest

from quicdeploy.common.io import PROJECT_ROOT, sha256
from quicdeploy.config.model import load_model_card, register
from quicdeploy.config.quicforge import (CAMERA_MAP, DIMS, FILES, FORMAT, PHYSICAL_CONTRACT,
                                        PREPROCESSING, TARGET, load_bundle)


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value))


@pytest.fixture
def bundle(tmp_path):
    root = tmp_path / "bundle"
    root.mkdir()
    model_config = {"type": "LVADecoder", **DIMS, "gradient_checkpointing": False, "preproc_file": None}
    training = {"model": {"type": "LVADecoder", **DIMS}, "task": {"unconditional_prob": 1.0},
                "data": {"horizon": 50, "target_pose_source": "action", "max_delay": 10,
                         "datasets": [{"label_file": "/data/task_kvdb/label.pkl"}]}}
    checkpoint = {"format_version": 1, "epoch": 50, "global_step": 43700,
                  "model": {**model_config, "gradient_checkpointing": True}, "export": {"weights": "model"}}
    conversion = {"fps": 30, "source_dataset": "/data/task", "kinematics": {
        side: {"base_link": "world", "end_link": side + "_Link6"} for side in ("left", "right")},
        "pose_labels": {"position_unit": "meters", "gripper_unit": "millimeters",
                        "rotation_representation": "rotation_6d_first_two_columns", "tcp_offset_applied": False},
        "camera_map": {"observation.images.head": "top", "observation.images.left_wrist": "left_wrist",
                       "observation.images.right_wrist": "right_wrist"}}
    stats = {"mu": [0.0] * 20, "std": [1.0] * 20, "target_contract": TARGET,
             "horizon": 50, "crop_size": [448, 608], "camera_order": ["left_wrist", "top", "right_wrist"]}
    inference = {"precision": "fp32", "predictor": {"type": "LVAFlowPredictor", "num_steps": 10, "guidance_scale": 0.0}}
    values = {"model_config.json": model_config, "inference_config.json": inference, "normalization.json": stats,
              "provenance/training_config.json": training, "provenance/checkpoint.json": checkpoint,
              "provenance/conversion_meta.json": conversion}
    for name, value in values.items():
        write(root / name, value)
    (root / "weights.pt").write_bytes(b"small fixture; metadata-only tests")
    contract = {**PHYSICAL_CONTRACT, "horizon": 50, "action_dt_s": 1 / 30, "first_target_offset_s": 1 / 30,
                "fixed_prefix_steps": 10, "target_contract": TARGET, "history_frames": 1, "camera_map": CAMERA_MAP}
    manifest = {"schema_version": 1, "format": FORMAT, "contract": contract,
                "source": {"epoch": 50, "global_step": 43700, "input_weights_sha256": sha256(root / "weights.pt")},
                "files": {p: sha256(root / p) for p in FILES}}
    write(root / "deployment_manifest.json", manifest)
    backend = PROJECT_ROOT / "quicdeploy/policy/backends/quicforge_lva"
    code = [backend / p for p in ("__init__.py", "model.py", "decoder.py", "dinov3.py", "task_ids.py", "PROVENANCE.json")]
    code += [PROJECT_ROOT / p for p in ("quicdeploy/policy/lva.py", "quicdeploy/policy/quicforge.py", "quicdeploy/config/quicforge.py")]
    config = {**PHYSICAL_CONTRACT, "id": "test", "name": "test", "architecture": "DINOv3 + FM",
              "adapter": "quicdeploy.policy.quicforge:QuicForgeLVAAdapter", "artifact_format": FORMAT,
              "artifact_dir": str(root), "weights": str(root / "weights.pt"),
              "normalization": str(root / "normalization.json"), "export_metadata": str(root / "deployment_manifest.json"),
              "preprocessing": PREPROCESSING, "weights_variant": "model", "weights_sha256": sha256(root / "weights.pt"),
              "checkpoint_epoch": 50, "global_step": 43700, "horizon": 50, "fixed_prefix_steps": 10,
              "action_dt_s": 1 / 30, "first_target_offset_s": 1 / 30, "target_contract": TARGET,
              "cameras": ["left_wrist", "head", "right_wrist"], "output_domain": "physical", "supervision": "action",
              "uses_state": False, "uses_language": False, "demo": False, "timing_domain": "stored_recording_time",
              "implementation_files": [str(root / p) for p in FILES - {"weights.pt", "normalization.json"}]
                                      + list(map(str, code)),
              "weight_dtype": "float32", "device": "cpu", "cpu_threads": 1,
              "noise_std": 1, "num_steps": 10, "guidance_scale": 0}
    return config, root, values


def test_relative_card_is_independent_of_cwd(bundle, tmp_path, monkeypatch):
    config, root, _ = bundle
    owner = tmp_path / "cards/test.json"
    owner.parent.mkdir()
    for key in ("weights", "normalization", "export_metadata", "artifact_dir"):
        config[key] = os.path.relpath(config[key], owner.parent)
    config["implementation_files"] = [os.path.relpath(p, owner.parent) for p in config["implementation_files"]]
    write(owner, {"schema_version": 1, "model": config})
    register(owner, relative=True)
    monkeypatch.chdir(tmp_path.parent)
    loaded = load_model_card(owner)
    assert loaded["model"]["weights"] == str(root / "weights.pt")
    assert all(not Path(p).is_absolute() for p in json.loads(owner.read_text())["artifacts"])
    (root / "weights.pt").write_bytes(b"tampered")
    with pytest.raises(ValueError, match="artifact changed"):
        load_model_card(owner)


@pytest.mark.parametrize("file,key,value,match", [
    ("normalization.json", "history_frames", 16, "history_frames"),
    ("normalization.json", "camera_order", ["top", "left_wrist", "right_wrist"], "camera order"),
    ("normalization.json", "std", [0.0] * 20, "std > 0"),
    ("normalization.json", "target_contract", {**TARGET, "pose_source": "state"}, "target contract"),
    ("model_config.json", "type", "TemporalLVADecoder", "model type"),
    ("model_config.json", "task_ids", [1], "task_ids"),
    ("provenance/conversion_meta.json", "fps", 60, "data fps"),
])
def test_incompatible_exports_are_rejected(bundle, file, key, value, match):
    config, root, values = bundle
    changed = copy.deepcopy(values[file]); changed[key] = value; write(root / file, changed)
    with pytest.raises(ValueError, match=match):
        load_bundle(config, verify_hashes=False)


def test_missing_or_replaced_assets_and_path_traversal(bundle):
    config, root, _ = bundle
    (root / "normalization.json").write_text("{}")
    with pytest.raises(ValueError, match="artifact changed"):
        load_bundle(config)
    manifest = json.loads((root / "deployment_manifest.json").read_text())
    manifest["files"]["../escape"] = "ignored"
    write(root / "deployment_manifest.json", manifest)
    with pytest.raises(ValueError, match="unknown members"):
        load_bundle(config)


def test_card_must_pin_all_code_and_metadata(bundle, tmp_path):
    config, _, _ = bundle
    config["implementation_files"] = []
    owner = tmp_path / "test.json"
    write(owner, {"schema_version": 1, "model": config})
    with pytest.raises(ValueError, match="pin every bundle member"):
        register(owner)


def test_raw_loader_rejects_bad_state_and_uses_strict_loading(bundle, monkeypatch):
    import torch
    from quicdeploy.policy.quicforge import QuicForgeLVAAdapter
    import quicdeploy.policy.backends.quicforge_lva as backend

    config, root, _ = bundle
    config["runtime_versions"] = {"torch": torch.__version__, "numpy": np.__version__}

    class Tiny(torch.nn.Module):
        def __init__(self, **kwargs):
            super().__init__()
            self.weight = torch.nn.Parameter(torch.zeros(2, 2))

    monkeypatch.setattr(backend, "LVADecoder", Tiny)

    def update_weights(weights):
        torch.save(weights, root / "weights.pt")
        config["weights_sha256"] = sha256(root / "weights.pt")
        manifest = json.loads((root / "deployment_manifest.json").read_text())
        manifest["files"]["weights.pt"] = config["weights_sha256"]
        manifest["source"]["input_weights_sha256"] = config["weights_sha256"]
        write(root / "deployment_manifest.json", manifest)

    update_weights({"weight": torch.ones(2, 2)})
    adapter = QuicForgeLVAAdapter(); adapter.load(config)
    assert torch.equal(adapter.model.weight, torch.ones(2, 2))
    update_weights({"wrong_key": torch.ones(2, 2)})
    with pytest.raises(RuntimeError, match="Missing key"):
        QuicForgeLVAAdapter().load(config)
    update_weights({"weight": torch.full((2, 2), float("nan"))})
    with pytest.raises(ValueError, match="finite FP32"):
        QuicForgeLVAAdapter().load(config)


def test_card_must_pin_network_code(bundle, tmp_path):
    config, _, _ = bundle
    config["implementation_files"] = [p for p in config["implementation_files"] if not p.endswith("/decoder.py")]
    owner = tmp_path / "test.json"
    write(owner, {"schema_version": 1, "model": config})
    with pytest.raises(ValueError, match="pin adapter and frozen network code"):
        register(owner)


@pytest.mark.parametrize("key,value", [("global_step", 1), ("guidance_scale", 1.0), ("artifact_format", "unknown")])
def test_model_card_cannot_change_reference_identity(bundle, key, value):
    config, _, _ = bundle
    config[key] = value
    with pytest.raises(ValueError, match="QuicForge"):
        load_bundle(config)


def test_resolve_rejects_training_deployment_urdf_mismatch(bundle, tmp_path, monkeypatch):
    import quicdeploy.config.resolve as resolver
    config, _, _ = bundle
    urdf = tmp_path / "different.urdf"; urdf.write_text("different robot geometry")
    monkeypatch.setattr(resolver, "load_model_card", lambda path: {"model": config})
    monkeypatch.setattr(resolver, "load_hardware", lambda path, sources: {"kinematics": {"urdf": str(urdf)}})
    # resolve also fingerprints the card path; use a real temporary file.
    card = tmp_path / "card.json"; card.write_text("{}")
    with pytest.raises(ValueError, match="training URDF differs"):
        resolver.resolve(PROJECT_ROOT / "configs/runtime.yaml", model_card=card)
