"""Cached-token training variants preserve backbone identity, precision and history."""
import hashlib
import json

import pytest

from test_quicforge import bundle as single_frame_bundle
from test_prepare_quicforge import flat
from test_import_quicforge import project, complete


def cached_source(flat):
    values = {name: json.loads((flat / name).read_text()) for name in
              ("model_config.json", "config.resolved.json", "checkpoint_manifest.json")}
    for name, value in values.items():
        model = value if name == "model_config.json" else value["model"]
        model.pop("backbone_flash_attention", None)
        model.update(type="CachedTemporalLVADecoder", backbone="dinov3_vitl16",
                     freeze_backbone=True, backbone_precision="bf16")
    values["checkpoint_manifest.json"]["config_hash"] = hashlib.sha256(json.dumps(
        values["config.resolved.json"], sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()
    for name, value in values.items():
        (flat / name).write_text(json.dumps(value))
    return complete(flat, manifest=False, name="cached_temporal_vitl")


@pytest.mark.parametrize("precision", ["fp32", "fp16"])
def test_cached_import_preserves_model_sampling_and_precision(project, flat, precision):
    from tools.import_quicforge import run_import, targets
    from quicdeploy.config.model import load_model_card
    source = cached_source(flat)
    run_import(source, precision=precision)
    paths = targets(source.name)
    card = load_model_card(paths["card"])
    assert card["model"]["artifact_format"] == "quicforge_temporal_lva_v1"
    assert card["model"]["temporal_sampling"]["history_frames"] == 16
    assert card["model"]["temporal_sampling"]["history_stride"] == 8
    assert "ViT-L/16" in card["metadata"]["architecture"]
    exported = json.loads((paths["bundle"] / "model_config.json").read_text())
    assert exported["type"] == "CachedTemporalLVADecoder"
    assert exported["backbone"] == "dinov3_vitl16" and exported["backbone_precision"] == "bf16"


@pytest.mark.parametrize("owner,key,value", [
    ("model_config.json", "backbone", "other"),
    ("config.resolved.json", "backbone", "dinov3_vitb16"),
    ("checkpoint_manifest.json", "backbone_precision", "fp32"),
    ("model_config.json", "freeze_backbone", False),
    ("checkpoint_manifest.json", "freeze_backbone", 1),
])
def test_cached_contract_rejects_identity_or_precision_mismatch(flat, owner, key, value):
    from quicdeploy.config.temporal import validate_temporal
    source = cached_source(flat)
    model = json.loads((source / "model_config.json").read_text())
    training = json.loads((source / "config.resolved.json").read_text())
    checkpoint = json.loads((source / "checkpoint_manifest.json").read_text())
    target = {"model_config.json": model, "config.resolved.json": training["model"],
              "checkpoint_manifest.json": checkpoint["model"]}[owner]
    target[key] = value
    with pytest.raises(ValueError, match="backbone|precision"):
        validate_temporal(model, {}, {}, training, checkpoint, {})


def tiny_cached(monkeypatch):
    import torch
    import quicdeploy.policy.backends.temporal_lva as backend
    class Backbone(torch.nn.Module):
        def __init__(self):
            super().__init__()
            self.weight = torch.nn.Parameter(torch.tensor([0.123456789, 70000.0]))
        def forward_with_features(self, image, layers_to_save):
            assert layers_to_save == [20, 21, 22, 23]
            intermediate = {}
            for index in layers_to_save:
                value = torch.full((image.shape[0], 7, 1024), index + .123, device=image.device)
                value[:, :5] = -999
                intermediate[f"layer_{index}"] = value
            return {"intermediate": intermediate}
    monkeypatch.setattr(backend, "build_temporal_backbone", lambda *args: Backbone())
    return backend.CachedTemporalLVADecoder(backbone="dinov3_vitl16", emb_dim=16, time_dim=8,
                                           frame_queries=4, temporal_queries=4, spatial_layers=1,
                                           temporal_layers=1, n_att_layer=1).eval()


def test_cached_raw_features_sum_four_layers_strip_special_tokens_and_round_bf16(monkeypatch):
    import torch
    model = tiny_cached(monkeypatch)
    captured = []
    model._frame_projection.register_forward_pre_hook(lambda module, args: captured.append(args[0].clone()))
    with torch.inference_mode():
        result = model._encode_frame(torch.zeros(1, 3, 16, 32))
    assert result.shape == (1, 4, 16)
    expected = torch.tensor(sum(i + .123 for i in range(20, 24))).bfloat16().float()
    torch.testing.assert_close(captured[0], expected.expand(1, 2, 1024), rtol=0, atol=0)
    assert not any(p.requires_grad for p in model._backbone.parameters())


def test_half_cached_retains_exact_backbone_and_converts_spatial_and_action_parameters(monkeypatch):
    import torch
    from quicdeploy.policy.backends.temporal_lva import half_temporal
    model = tiny_cached(monkeypatch)
    original = {name: tensor.clone() for name, tensor in model._backbone.state_dict().items()}
    half_temporal(model)
    for name, tensor in model.state_dict().items():
        if name.startswith("_backbone."):
            assert tensor.dtype == torch.float32
            assert torch.equal(tensor, original[name.removeprefix("_backbone.")])
        elif tensor.is_floating_point():
            assert tensor.dtype == torch.float16


def test_cached_vitl_and_legacy_vitb_build_on_meta():
    import torch
    from quicdeploy.policy.backends.temporal_lva import CachedTemporalLVADecoder, TemporalLVADecoder
    with torch.device("meta"):
        cached = CachedTemporalLVADecoder(backbone="dinov3_vitl16")
        legacy = TemporalLVADecoder()
    assert cached._backbone.depth == 24 and legacy._backbone.n_blocks == 12
    assert cached._frame_projection.in_features == 1024 and legacy._frame_projection.in_features == 768


def test_hardlink_import_shares_weight_inode_and_rejects_later_source_changes(project, flat):
    from tools.import_quicforge import run_import, targets
    from quicdeploy.config.model import load_model_card
    source = cached_source(flat)
    result = run_import(source, hardlink_weights=True)
    paths = targets(source.name)
    weight = paths["bundle"] / "weights.pt"
    assert result["weights_storage"] == "hardlink"
    assert weight.stat().st_ino == (source / "model.pt").stat().st_ino
    assert weight.stat().st_dev == (source / "model.pt").stat().st_dev
    (source / "model.pt").write_bytes(b"changed")
    with pytest.raises(ValueError, match="artifact changed|hash|SHA|changed"):
        load_model_card(paths["card"])
