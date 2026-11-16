"""Single-frame B contracts, feature semantics and FP16 preservation; CPU only.
单帧 ViT-B 契约、特征语义及 FP16 参数保真测试；不连接硬件。
"""
import copy
import hashlib
import json

import pytest

from test_quicforge import bundle
from test_quicforge import bundle as single_frame_bundle
from test_prepare_quicforge import flat, flat_lva
from test_import_quicforge import project, complete
from quicdeploy.config.quicforge import validate_inputs


@pytest.mark.parametrize('variant', [None, 'dinov3_vitl16', 'dinov3_vitb16'])
def test_backbone_contract_accepts_legacy_and_explicit_variants(bundle, variant):
    _, _, values = bundle
    m, i, s, t, c, conversion = [copy.deepcopy(values[k]) for k in (
        'model_config.json', 'inference_config.json', 'normalization.json',
        'provenance/training_config.json', 'provenance/checkpoint.json', 'provenance/conversion_meta.json')]
    if variant:
        for owner in (m, t['model'], c['model']):
            owner['backbone'] = variant
    validate_inputs(m, i, s, t, c, conversion)
    if variant == 'dinov3_vitl16':
        t['model'].pop('backbone')
        validate_inputs(m, i, s, t, c, conversion)


@pytest.mark.parametrize('owner', ['export', 'training', 'checkpoint'])
@pytest.mark.parametrize('variant', ['dinov3_vitb16', 'unknown', None])
def test_backbone_contract_rejects_mismatch_and_invalid_values(bundle, owner, variant):
    _, _, values = bundle
    m, i, s, t, c, conversion = [copy.deepcopy(values[k]) for k in (
        'model_config.json', 'inference_config.json', 'normalization.json',
        'provenance/training_config.json', 'provenance/checkpoint.json', 'provenance/conversion_meta.json')]
    {'export': m, 'training': t['model'], 'checkpoint': c['model']}[owner]['backbone'] = variant
    with pytest.raises(ValueError, match='backbone'):
        validate_inputs(m, i, s, t, c, conversion)


@pytest.mark.parametrize('precision', ['fp16', 'fp32'])
def test_import_vitb_pins_official_sources_and_retains_directory_name(project, flat_lva, precision):
    from tools.import_quicforge import run_import, targets
    from quicdeploy.config.model import load_model_card, register
    for name in ('model_config.json', 'config.resolved.json', 'checkpoint_manifest.json'):
        path = flat_lva / name
        value = json.loads(path.read_text())
        (value if name == 'model_config.json' else value['model'])['backbone'] = 'dinov3_vitb16'
        path.write_text(json.dumps(value))
    path = flat_lva / 'checkpoint_manifest.json'
    value = json.loads(path.read_text())
    value['config_hash'] = hashlib.sha256(json.dumps(json.loads((flat_lva / 'config.resolved.json').read_text()),
                                                   sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()
    path.write_text(json.dumps(value))
    source = complete(flat_lva, manifest=False, name='ViTB_update00117150')
    run_import(source, precision=precision)
    paths = targets(source.name)
    card = load_model_card(paths['card'])
    assert paths['card'].name == source.name + '.json'
    assert paths['config'].name == source.name + '.yaml'
    assert paths['launcher'].name == 'start_' + source.name + '.sh'
    assert card['model']['weight_dtype'] == ('float16' if precision == 'fp16' else 'float32')
    assert card['metadata']['architecture'] == 'DINOv3 ViT-B/16 + FM'
    assert any('/third_party/dinov3/' in p for p in card['artifacts'])
    assert any(p.endswith('/backends/temporal_lva.py') for p in card['artifacts'])
    raw = json.loads(paths['card'].read_text())
    raw['model']['implementation_files'] = [p for p in raw['model']['implementation_files']
                                           if not p.endswith('dinov3/models/vision_transformer.py')]
    paths['card'].write_text(json.dumps(raw))
    with pytest.raises(ValueError, match='pin adapter and frozen network code'):
        register(paths['card'])


def test_vitb_uses_sum_of_four_unnormalized_patch_features(monkeypatch):
    import torch
    from quicdeploy.policy.backends.quicforge_lva.model import LVADecoder
    import quicdeploy.policy.backends.temporal_lva as backend

    class Backbone(torch.nn.Module):
        embed_dim = 768
        def get_intermediate_layers(self, image, n, norm):
            assert n == 4 and norm is False
            return tuple(torch.full((1, 2, 768), float(i)) for i in range(1, 5))
    monkeypatch.setattr(backend, 'build_dinov3_vitb16', Backbone)
    model = LVADecoder(backbone='dinov3_vitb16', n_att_layer=1)
    assert model._emb_proj.weight.shape == (256, 768)
    torch.testing.assert_close(model.backbone_forward(torch.empty(1, 3, 16, 32)), torch.full((1, 2, 768), 10.0))
    with pytest.raises(ValueError, match='backbone'):
        LVADecoder(backbone='other')


def test_half_vitb_preserves_original_fp32_values_and_rejects_bad_structure():
    import torch
    from quicdeploy.policy.backends.temporal_lva import half_vitb16
    class LayerScale(torch.nn.Module):
        def __init__(self):
            super().__init__()
            self.gamma = torch.nn.Parameter(torch.tensor([70000.0, 0.123456789]))
    model = torch.nn.Module()
    model.scales = torch.nn.ModuleList([LayerScale() for _ in range(24)])
    model.rope_embed = torch.nn.Module()
    model.rope_embed.register_buffer('periods', torch.tensor([0.123456789]))
    model.proj = torch.nn.Linear(2, 2)
    original = {k: v.clone() for k, v in model.state_dict().items()}
    half_vitb16(model)
    for name, value in model.state_dict().items():
        if name.endswith('.gamma') or name.endswith('rope_embed.periods'):
            assert value.dtype == torch.float32
            assert torch.equal(value, original[name])
        else:
            assert value.dtype == torch.float16
    bad = torch.nn.Linear(2, 2)
    with pytest.raises(ValueError, match='24 official ViT-B'):
        half_vitb16(bad)
    assert bad.weight.dtype == torch.float32


def test_real_vitb_and_legacy_vitl_state_shapes_on_meta():
    import torch
    from quicdeploy.policy.backends.quicforge_lva.model import LVADecoder
    with torch.device('meta'):
        b = LVADecoder(backbone='dinov3_vitb16', n_att_layer=4)
        l = LVADecoder(n_att_layer=4)
    assert len(b.state_dict()) == 414 and len(l.state_dict()) == 642
    assert b._backbone.n_blocks == 12 and l._backbone.depth == 24
    assert b._emb_proj.in_features == 768 and l._emb_proj.in_features == 1024
