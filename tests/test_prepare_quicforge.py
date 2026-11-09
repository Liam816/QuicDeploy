"""Verified flat uploads and original runs share one import path; no GPU/hardware.
已校验平铺资产与原始训练目录共用导入逻辑；不加载 GPU，也不连接硬件。
"""
import copy
import hashlib
import json
from pathlib import Path

import pytest

from quicdeploy.common.io import sha256
from quicdeploy.config.model import load_model_card
from test_quicforge import bundle as single_frame_bundle
from tools.prepare_quicforge import import_source, prepare


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value))


def pin(root):
    files = [{"name": p.name, "bytes": p.stat().st_size, "sha256": sha256(p)}
             for p in root.iterdir() if p.is_file() and p.name != "download_manifest.json"]
    write(root / "download_manifest.json", {"files": files})


@pytest.fixture
def flat(single_frame_bundle, tmp_path):
    _, _, values = single_frame_bundle
    root = tmp_path / "flat"
    training = copy.deepcopy(values["provenance/training_config.json"])
    checkpoint = copy.deepcopy(values["provenance/checkpoint.json"])
    stats = copy.deepcopy(values["normalization.json"])
    for owner in (training, checkpoint):
        owner["model"].update(type="TemporalLVADecoder", backbone_flash_attention=True)
    training["data"].update(history_frames=16, history_stride=8, window_stride=1)
    stats.update(history_frames=16, history_stride=8, window_stride=1, history_first_anchor=120,
                 history_policy="complete", history_time_units="video_frames")
    checkpoint.update(name="update_00050000", global_step=50000, epoch=51,
                      model_capabilities={"inference_kwargs": {"gradient_checkpointing": False, "preproc_file": None}},
                      inference=values["inference_config.json"])
    checkpoint["config_hash"] = hashlib.sha256(json.dumps(training, sort_keys=True, ensure_ascii=False,
                                                           separators=(",", ":")).encode()).hexdigest()
    model = dict(checkpoint["model"])
    model.update(checkpoint["model_capabilities"]["inference_kwargs"])
    for name, value in {"config.resolved.json": training, "checkpoint_manifest.json": checkpoint,
                        "model_config.json": model, "inference_config.json": checkpoint["inference"],
                        "normalization.json": stats,
                        "conversion_meta.json": values["provenance/conversion_meta.json"]}.items():
        write(root / name, value)
    (root / "model.pt").write_bytes(b"fixture plain state_dict placeholder; no torch is loaded")
    pin(root)
    return root


def test_flat_import_preserves_originals_and_records_training_option(flat):
    before = {p.name: p.read_bytes() for p in flat.iterdir()}
    _, paths, _, checkpoint, model, provenance = import_source(flat, "update_00050000", flat / "conversion_meta.json")
    assert paths["weights"] == flat / "model.pt"
    assert checkpoint["epoch"] == 51
    assert "backbone_flash_attention" not in model
    assert provenance["training_model_options"] == {"backbone_flash_attention": True}
    assert {p.name: p.read_bytes() for p in flat.iterdir()} == before


def test_prepare_generates_portable_temporal_cards_and_runtime(flat, tmp_path, monkeypatch):
    import tools.prepare_quicforge as importer
    import yaml
    project = tmp_path / "deploy"
    (project / "configs/models").mkdir(parents=True)
    monkeypatch.setattr(importer, "PROJECT", project)
    output = project / "models/temporal"
    prepare(flat, "update_00050000", flat / "conversion_meta.json", output, "temporal_update50000")
    for precision in ("fp32", "fp16"):
        path = project / f"configs/models/temporal_update50000_{precision}.json"
        card = load_model_card(path)
        assert card["model"]["temporal_sampling"]["history_stride"] == 8
        assert card["metadata"]["checkpoint"] == {"epoch": 51, "global_step": 50000}
        assert all(not Path(p).is_absolute() for p in json.loads(path.read_text())["artifacts"])
    runtime = yaml.safe_load((project / "configs/temporal_update50000.yaml").read_text())
    assert runtime["execution"]["prefetch_remaining_steps"] == 23
    assert "backbone_flash_attention" not in json.loads((output / "model_config.json").read_text())
    assert json.loads((output / "provenance/checkpoint.json").read_text())["model"]["backbone_flash_attention"] is True
    assert sha256(output / "weights.pt") == sha256(flat / "model.pt")


def test_original_run_layout_is_still_supported(flat, tmp_path):
    root = tmp_path / "original"
    checkpoint = root / "checkpoints/update_00050000"
    mapping = {"model.pt": checkpoint / "backend/model.pt", "normalization.json": checkpoint / "assets/normalization.json",
               "checkpoint_manifest.json": checkpoint / "manifest.json", "config.resolved.json": root / "config.resolved.json"}
    for name, destination in mapping.items():
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes((flat / name).read_bytes())
    _, paths, _, _, model, provenance = import_source(root, "update_00050000", flat / "conversion_meta.json")
    assert paths["weights"] == checkpoint / "backend/model.pt"
    assert provenance["kind"] == "quicforge_checkpoint"
    assert "backbone_flash_attention" not in model


@pytest.mark.parametrize("change,match", [
    ("weights", "Downloaded asset changed"),
    ("config_hash", "training config hash"),
    ("exported", "flat exported model config"),
    ("checkpoint", "flat checkpoint name"),
    ("missing_pin", "must pin all import assets"),
    ("traversal", "Invalid/duplicate"),
    ("duplicate", "Invalid/duplicate"),
    ("attention_type", "must be boolean"),
])
def test_flat_mismatches_are_rejected(flat, change, match):
    if change == "weights":
        (flat / "model.pt").write_bytes(b"replaced")
    elif change == "config_hash":
        cfg = json.loads((flat / "config.resolved.json").read_text()); cfg["data"]["history_stride"] = 4
        write(flat / "config.resolved.json", cfg); pin(flat)
    elif change == "exported":
        cfg = json.loads((flat / "model_config.json").read_text()); cfg["out_dim"] = 22
        write(flat / "model_config.json", cfg); pin(flat)
    elif change == "checkpoint":
        cfg = json.loads((flat / "checkpoint_manifest.json").read_text()); cfg["name"] = "update_00040000"
        write(flat / "checkpoint_manifest.json", cfg); pin(flat)
    elif change in ("missing_pin", "traversal", "duplicate"):
        manifest = json.loads((flat / "download_manifest.json").read_text())
        if change == "missing_pin": manifest["files"] = [e for e in manifest["files"] if e["name"] != "model.pt"]
        if change == "traversal": manifest["files"][0]["name"] = "../outside"
        if change == "duplicate": manifest["files"].append(manifest["files"][0])
        write(flat / "download_manifest.json", manifest)
    elif change == "attention_type":
        for name in ("checkpoint_manifest.json", "model_config.json"):
            cfg = json.loads((flat / name).read_text())
            (cfg["model"] if name.startswith("checkpoint") else cfg)["backbone_flash_attention"] = "true"
            write(flat / name, cfg)
        pin(flat)
    with pytest.raises(ValueError, match=match):
        import_source(flat, "update_00050000", flat / "conversion_meta.json")
