"""Startup token history begins at the first model request, not camera warm-up."""
import copy

import numpy as np
import pytest

from test_temporal import adapter, observer, populate


def buffered_history(current, session="startup"):
    offsets = list(range(-120, 1, 8))
    return {"session": session, "frame_index": current, "first_frame_index": 0,
            "time_offsets": offsets,
            "samples": [{"frame_index": current + offset,
                         "images": {"head": np.array([current + offset])}} for offset in offsets]}


def test_observer_first_prediction_ignores_preexisting_camera_history():
    obs = observer()
    populate(obs, 0, 120)
    current = {"head": np.array([120])}
    history = obs._history(120 / 30, current)
    assert history["first_frame_index"] == 120
    assert [item["frame_index"] for item in history["samples"]] == [120] * 16
    assert all(item["images"] is current for item in history["samples"][:-1])
    populate(obs, 121, 128)
    next_images = {"head": np.array([128])}
    history = obs._history(128 / 30, next_images)
    assert history["first_frame_index"] == 120
    assert [item["frame_index"] for item in history["samples"]] == [128] * 14 + [120, 128]


def test_empty_token_cache_encodes_only_current_even_with_full_raw_history(monkeypatch):
    import torch
    policy = adapter(monkeypatch)
    history = buffered_history(120)
    calls = []
    original = policy.model._encode_frame
    def encode(image):
        calls.append(image.clone())
        return original(image)
    policy.model._encode_frame = encode
    packed = policy._image_input({"vision_history": history})
    assert len(calls) == 1
    assert policy.history_diagnostics["new_frame_encodings"] == 1
    assert policy.history_diagnostics["startup_padding_slots"] == 15
    assert list(policy.tokens) == [120]
    assert packed["valid_mask"].all()
    assert packed["time_offsets"].tolist() == [list(range(-120, 1, 8))]
    assert torch.equal(packed["frame_slots"], policy.tokens[120][:, None].expand(-1, 16, -1, -1))
    assert packed["frame_slots"].stride(1) == 0

    next_history = buffered_history(128)
    packed = policy._image_input({"vision_history": next_history})
    assert len(calls) == 2
    assert policy.history_diagnostics["new_frame_encodings"] == 1
    assert policy.history_diagnostics["startup_padding_slots"] == 14
    assert set(policy.tokens) == {120, 128}
    assert torch.equal(packed["frame_slots"][:, :14], policy.tokens[128][:, None].expand(-1, 14, -1, -1))
    assert torch.equal(packed["frame_slots"][:, 14], policy.tokens[120])
    assert torch.equal(packed["frame_slots"][:, 15], policy.tokens[128])


@pytest.mark.parametrize("reset", ["session", "explicit"])
def test_reset_discards_old_tokens_and_pads_from_new_current(monkeypatch, reset):
    policy = adapter(monkeypatch)
    policy._image_input({"vision_history": buffered_history(120)})
    history = buffered_history(128)
    if reset == "session":
        history["session"] = "resumed"
    else:
        policy.reset()
    packed = policy._image_input({"vision_history": history})
    assert policy.history_diagnostics["new_frame_encodings"] == 1
    assert policy.history_diagnostics["startup_padding_slots"] == 15
    assert list(policy.tokens) == [128]


def test_missing_frame_after_token_history_started_is_not_startup_padding(monkeypatch):
    policy = adapter(monkeypatch)
    policy._image_input({"vision_history": buffered_history(120)})
    history = buffered_history(128)
    history["samples"][-2] = copy.deepcopy(history["samples"][-1])
    with pytest.raises(ValueError, match="startup|sampling|history"):
        policy._image_input({"vision_history": history})
