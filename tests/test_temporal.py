"""Source-frame sampling, current padding, cache reuse and temporal numerical equivalence."""
from collections import deque
from types import SimpleNamespace
import threading

import numpy as np
import pytest

from quicdeploy.runtime.observer import SynchronizedObserver
from test_quicforge import bundle as single_frame_bundle


def observer(stride=8):
    cameras = {}
    for name in ("left_wrist", "head", "right_wrist"):
        camera = SimpleNamespace(fps=30, frame_lock=threading.Lock(), frame_history=deque(maxlen=132))
        camera.enable_history = lambda capacity: None
        cameras[name] = camera
    config = {"observation": {"max_camera_skew_ms": 20, "max_camera_age_ms": 100}, "guard": {}, "model": {
        "cameras": list(cameras), "temporal_sampling": {"history_frames": 16, "history_stride": stride, "source_fps": 30}}}
    result = SynchronizedObserver(SimpleNamespace(cameras=cameras), None, config)
    result.first_frame_index = 0
    return result


def populate(obs, first, last):
    for index in range(first, last + 1):
        stamp = index / 30
        for camera, queue in obs.frames.items():
            queue.append((stamp, np.array([index])))
        obs.head_indices[stamp] = index


@pytest.mark.parametrize("stride,current,expected", [(8, 17, [17] * 13 + [1, 9, 17]),
                                                       (1, 3, [3] * 12 + [0, 1, 2, 3])])
def test_exact_sampling_and_padding_uses_this_current_frame(stride, current, expected):
    obs = observer(stride)
    span = 15 * stride
    populate(obs, 0, current)
    # This test exercises an already-started token history, not a cold first request.
    obs._history(0, {name: np.array([0]) for name in obs.frames})
    images = {name: np.array([current]) for name in obs.frames}
    hist = obs._history(current / 30, images)
    assert hist["time_offsets"] == list(range(-span, 1, stride))
    assert [sample["frame_index"] for sample in hist["samples"]] == expected
    assert hist["samples"][0]["images"] is images
    populate(obs, current + 1, span)
    assert [s["frame_index"] for s in obs._history(span / 30, images)["samples"]] == list(range(0, span + 1, stride))


@pytest.mark.parametrize("stride", [1, 8])
def test_source_gap_and_historical_camera_skew_fail(stride):
    obs = observer(stride)
    span = 15 * stride
    populate(obs, 0, span)
    obs._history(0, {name: np.array([0]) for name in obs.frames})
    obs.frames["head"] = deque((s, im) for s, im in obs.frames["head"] if s != 8 / 30)
    with pytest.raises(RuntimeError, match="acquisition gap"):
        obs._history(span / 30, {})
    populate(obs, 0, span)
    obs.frames["left_wrist"] = deque([(span / 30, np.array([span]))])
    with pytest.raises(RuntimeError, match="historical camera skew"):
        obs._history(span / 30, {})


def tiny_model(monkeypatch):
    import torch
    import quicdeploy.policy.backends.temporal_lva as backend

    class Backbone(torch.nn.Module):
        def __init__(self):
            super().__init__()
            self.proj = torch.nn.Conv2d(3, 768, 16, 16)

        def forward_features(self, image):
            return {"x_norm_patchtokens": self.proj(image).flatten(2).transpose(1, 2)}

    monkeypatch.setattr(backend, "build_dinov3_vitb16", lambda path: Backbone())
    torch.manual_seed(42)
    return backend.TemporalLVADecoder(emb_dim=16, time_dim=8, frame_queries=4, temporal_queries=4,
                                    spatial_layers=1, temporal_layers=1, n_att_layer=1).eval()


@pytest.mark.parametrize("stride", [1, 8])
def test_cached_context_and_rtc_equal_explicit_images(monkeypatch, stride):
    import torch
    model = tiny_model(monkeypatch)
    images = torch.randn(1, 3, 3, 16, 32)
    valid = torch.ones(1, 3, dtype=torch.bool)
    offsets = torch.tensor([[-2 * stride, -stride, 0]], dtype=torch.float32)
    with torch.inference_mode():
        slots = torch.stack([model._encode_frame(images[:, i]) for i in range(3)], dim=1)
        explicit = {"frames": images, "valid_mask": valid, "time_offsets": offsets}
        cached = {"frame_slots": slots, "valid_mask": valid, "time_offsets": offsets}
        torch.testing.assert_close(model.encode_image(explicit), model.encode_image(cached), rtol=0, atol=0)
        noise, prefix = torch.randn(1, 50, 20), torch.randn(1, 10, 20)
        a = model.inference(explicit, noise=noise, action_prefix=prefix, delay=10, guidance_scale=0, num_steps=2)
        b = model.inference(cached, noise=noise, action_prefix=prefix, delay=10, guidance_scale=0, num_steps=2)
        torch.testing.assert_close(a, b, rtol=0, atol=0)
        assert torch.equal(b[:, :10], prefix)


def adapter(monkeypatch, stride=8):
    import torch
    from quicdeploy.policy.temporal import TemporalLVAAdapter
    result = TemporalLVAAdapter()
    result.torch, result.model = torch, tiny_model(monkeypatch)
    result.config = {"device": "cpu"}
    result.sampling = {"history_frames": 16, "history_stride": stride}
    result.preprocess = lambda images: np.full((1, 3, 16, 32), images["head"][0], dtype=np.float32)
    result.reset()
    return result


@pytest.mark.parametrize("stride", [1, 8])
def test_token_queue_reuse_eviction_session_reset_and_startup(monkeypatch, stride):
    policy = adapter(monkeypatch, stride)
    obs = observer(stride)
    span = 15 * stride
    populate(obs, 0, 0)
    packed = policy._image_input({"vision_history": obs._history(0, {"head": np.array([0])})})
    assert policy.history_diagnostics["new_frame_encodings"] == 1
    assert packed["valid_mask"].all()
    assert packed["time_offsets"].tolist() == [list(range(-span, 1, stride))]
    populate(obs, 1, span)
    history = obs._history(span / 30, {"head": np.array([span])})
    policy._image_input({"vision_history": history})
    assert policy.history_diagnostics["new_frame_encodings"] == 15
    policy._image_input({"vision_history": history})
    assert policy.history_diagnostics["new_frame_encodings"] == 0
    current = span + stride
    populate(obs, span + 1, current)
    policy._image_input({"vision_history": obs._history(current / 30, {"head": np.array([current])})})
    assert policy.history_diagnostics["new_frame_encodings"] == 1
    assert min(policy.tokens) == stride and len(policy.tokens) == 16
    obs.session = "next episode"
    obs.first_frame_index = current
    policy._image_input({"vision_history": obs._history(current / 30, {"head": np.array([current])})})
    assert len(policy.tokens) == 1


def test_temporal_metadata_must_match_training():
    from quicdeploy.config.temporal import validate_temporal
    # Reject history disagreement before physical validation is reached.
    model = {"type": "TemporalLVADecoder"}
    training = {"model": dict(model), "data": {"history_frames": 8}}
    checkpoint = {"model": dict(model)}
    with pytest.raises(ValueError, match="training.history_frames"):
        validate_temporal(model, {}, {"history_frames": 16}, training, checkpoint, {"fps": 30})


def test_camera_history_reset_keeps_only_current():
    from quicdeploy.robot.piper.camera import OpenCVCamera
    camera = OpenCVCamera.__new__(OpenCVCamera)
    camera.frame_lock = threading.Lock()
    camera.frame_index, camera.latest_timestamp = 100, 10.0
    camera.latest_frame = np.zeros((2, 2, 3), np.uint8)
    camera.frame_history = deque([(99, 9.9, camera.latest_frame), (100, 10.0, camera.latest_frame)])
    camera.enable_history(132)
    assert len(camera.frame_history) == 1 and camera.frame_history[0][0] == 100
    assert camera.frame_history.maxlen == 132


def test_prediction_phase_and_freshness(monkeypatch):
    obs = observer()
    populate(obs, 0, 9)
    monkeypatch.setattr("quicdeploy.runtime.observer.time.monotonic", lambda: 9 / 30 + .001)
    assert obs.prediction_ready() and obs._prediction_stamp() == 8 / 30
    monkeypatch.setattr("quicdeploy.runtime.observer.time.monotonic", lambda: 14 / 30)
    assert not obs.prediction_ready()
    populate(obs, 10, 16)
    monkeypatch.setattr("quicdeploy.runtime.observer.time.monotonic", lambda: 16 / 30 + .001)
    assert obs.prediction_ready() and obs._prediction_stamp() == 16 / 30


@pytest.fixture
def temporal_bundle(single_frame_bundle):
    import json
    from quicdeploy.config.temporal import FORMAT, code_paths, load_bundle
    from quicdeploy.config.model import register, load_model_card, compact_card
    from quicdeploy.common.io import sha256

    config, root, values = single_frame_bundle
    sampling = {"history_frames": 16, "history_stride": 8, "window_stride": 1,
                "source_fps": 30, "padding": "current", "history_time_units": "video_frames",
                "anchor_alignment": "history_stride"}
    config.update(artifact_format=FORMAT, adapter="quicdeploy.policy.temporal:TemporalLVAAdapter",
                  temporal_sampling=sampling)
    for name in ("model_config.json", "provenance/training_config.json", "provenance/checkpoint.json"):
        m = values[name] if name == "model_config.json" else values[name]["model"]
        m["type"] = "TemporalLVADecoder"
    values["provenance/training_config.json"]["data"].update({k: sampling[k] for k in (
        "history_frames", "history_stride", "window_stride")})
    values["normalization.json"].update(history_frames=16, history_stride=8, window_stride=1,
                                        history_policy="complete", history_time_units="video_frames",
                                        history_first_anchor=120)
    for name, value in values.items():
        (root / name).write_text(json.dumps(value))
    manifest_path = root / "deployment_manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["format"] = FORMAT
    manifest["contract"].pop("history_frames")
    manifest["contract"]["temporal_sampling"] = sampling
    manifest["files"] = {name: sha256(root / name) for name in manifest["files"]}
    manifest_path.write_text(json.dumps(manifest))
    config["implementation_files"] += list(map(str, code_paths()))
    return config, root, sampling


def test_temporal_bundle_and_registered_card(temporal_bundle, tmp_path):
    import json
    from quicdeploy.config.temporal import load_bundle
    from quicdeploy.config.model import register, load_model_card, compact_card
    config, root, sampling = temporal_bundle
    assert load_bundle(config)["sampling"] == sampling
    card = tmp_path / "temporal.json"
    card.write_text(json.dumps({"schema_version": 1, "model": config}))
    register(card)
    assert load_model_card(card)["model"]["temporal_sampling"] == sampling
    card.write_text(json.dumps(compact_card(json.loads(card.read_text()))))
    register(card)
    assert load_model_card(card)["model"]["temporal_sampling"] == sampling
    config["temporal_sampling"] = {**sampling, "history_stride": 4}
    with pytest.raises(ValueError, match="temporal card.temporal_sampling"):
        load_bundle(config)


@pytest.mark.parametrize("stride", [1, 4, 8])
def test_explicit_deployment_stride_keeps_training_metadata(temporal_bundle, stride, tmp_path):
    import json
    from quicdeploy.config.temporal import load_bundle
    from quicdeploy.config.model import compact_card, register, load_model_card
    config, root, sampling = temporal_bundle
    original = {p: p.read_bytes() for p in (root / "normalization.json", root / "provenance/training_config.json",
                                            root / "deployment_manifest.json")}
    config["temporal_sampling"] = {**sampling, "history_stride": stride}
    config["allow_history_stride_override"] = True
    bundle = load_bundle(config)
    assert bundle["sampling"]["history_stride"] == stride
    assert bundle["training_sampling"]["history_stride"] == 8
    assert all(p.read_bytes() == content for p, content in original.items())
    card = tmp_path / "override.json"
    card.write_text(json.dumps(compact_card({"schema_version": 1, "model": config})))
    register(card)
    assert load_model_card(card)["model"]["temporal_sampling"]["history_stride"] == stride


@pytest.mark.parametrize("stride", [0, -1, True, 1.5, "1", None])
def test_invalid_deployment_stride_is_rejected(temporal_bundle, stride):
    from quicdeploy.config.temporal import load_bundle
    config, _, sampling = temporal_bundle
    config["allow_history_stride_override"] = True
    config["temporal_sampling"] = {**sampling, "history_stride": stride}
    with pytest.raises(ValueError, match="positive integer"):
        load_bundle(config)


@pytest.mark.parametrize("key,value", [("history_frames", 8), ("source_fps", 15), ("window_stride", 2),
                                      ("history_time_units", "seconds"), ("anchor_alignment", "none")])
def test_stride_override_does_not_relax_other_contracts(temporal_bundle, key, value):
    from quicdeploy.config.temporal import load_bundle
    config, _, sampling = temporal_bundle
    config["allow_history_stride_override"] = True
    config["temporal_sampling"] = {**sampling, "history_stride": 1, key: value}
    with pytest.raises(ValueError, match="temporal card.temporal_sampling"):
        load_bundle(config)


@pytest.mark.parametrize("allow", [False, 1, "true", None])
def test_override_requires_explicit_boolean_opt_in(temporal_bundle, allow):
    from quicdeploy.config.temporal import load_bundle
    config, _, sampling = temporal_bundle
    config["temporal_sampling"] = {**sampling, "history_stride": 1}
    config["allow_history_stride_override"] = allow
    with pytest.raises(ValueError, match="temporal_sampling|must be boolean"):
        load_bundle(config)


def test_stride_one_uses_latest_source_frame(monkeypatch):
    obs = observer(1)
    populate(obs, 0, 20)
    obs._history(0, {})
    monkeypatch.setattr("quicdeploy.runtime.observer.time.monotonic", lambda: 20 / 30 + .001)
    assert obs._prediction_stamp() == 20 / 30
    history = obs._history(20 / 30, {})
    assert [s["frame_index"] for s in history["samples"]] == list(range(5, 21))
    assert history["time_offsets"] == list(range(-15, 1))
