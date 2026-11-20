"""Source-grid visual updates, FIFO image elision, warm-up and bounded dispatch."""
from collections import deque
from concurrent.futures import Future
import pickle
from types import SimpleNamespace

import numpy as np
import pytest

from test_temporal import adapter, observer, populate
from quicdeploy.policy.worker import InferenceWorker, worker_main


def test_observer_updates_keep_all_source_grid_frames(monkeypatch):
    obs = observer()
    populate(obs, 0, 24)
    obs._history(0, {"head": np.array([0])})
    monkeypatch.setattr("quicdeploy.runtime.observer.time.monotonic", lambda: 24/30+.001)
    assert [v["frame_index"] for v in obs.vision_updates()] == [0, 8, 16, 24]
    updates = obs.vision_updates(8)
    assert [v["frame_index"] for v in updates] == [16, 24]
    assert updates[-1]["images"]["head"] is obs.frames["head"][-1][1]
    assert obs.vision_updates(24) == []
    obs.frames["head"] = deque((t, im) for t, im in obs.frames["head"] if t != 16/30)
    with pytest.raises(RuntimeError, match="fell behind"):
        obs.vision_updates(8)


def test_background_tokens_eliminate_request_backfill_and_keep_exact_context(monkeypatch):
    import torch
    policy = adapter(monkeypatch)
    obs = observer()
    populate(obs, 0, 0)
    policy._image_input({"vision_history": obs._history(0, {"head": np.array([0])})})
    for index in (8, 16):
        assert policy.observe_vision({"session": obs.session, "frame_index": index,
                                      "images": {"head": np.array([index])}})
    assert not policy.observe_vision({"session": obs.session, "frame_index": 16, "images": {}})
    populate(obs, 1, 16)
    history = obs._history(16/30, {"head": np.array([16])})
    for value in history["samples"]:
        value.pop("images")  # queued frames require no repeated image transport
    packed = policy._image_input({"vision_history": history})
    assert policy.history_diagnostics["new_frame_encodings"] == 0
    assert policy.history_diagnostics["startup_padding_slots"] == 13
    values = [16] * 13 + [0, 8, 16]
    frames = torch.stack([torch.full((3, 16, 32), float(v)) for v in values])[None]
    explicit = {"frames": frames, "valid_mask": packed["valid_mask"], "time_offsets": packed["time_offsets"]}
    with torch.inference_mode():
        torch.testing.assert_close(policy.model.encode_image(packed), policy.model.encode_image(explicit), rtol=0, atol=0)
    with pytest.raises(ValueError, match="sampling grid"):
        policy.observe_vision({"session": obs.session, "frame_index": 17, "images": {}})
    with pytest.raises(ValueError, match="initialized"):
        policy.observe_vision({"session": "other", "frame_index": 24, "images": {}})


def test_warmup_does_not_create_a_prediction_history(monkeypatch):
    policy = adapter(monkeypatch)
    calls = []
    encode = policy.model._encode_frame
    def counted(image):
        calls.append(1)
        return encode(image)
    policy.model._encode_frame = counted
    policy.warmup()
    assert len(calls) == 1 and not policy.tokens
    assert policy.session is None and policy.first_token_frame_index is None


class ImmediateExecutor:
    def submit(self, function, *args):
        future = Future()
        try:
            future.set_result(function(*args))
        except BaseException as exc:
            future.set_exception(exc)
        return future


def sender():
    result = InferenceWorker.__new__(InferenceWorker)
    result.temporal_sampling = {"history_frames": 16, "history_stride": 8}
    result._vision_session, result._vision_index = None, None
    result._vision_known, result._vision_sends = set(), deque()
    result._sender = ImmediateExecutor()
    result.pending, result.counter = None, 0
    result.messages = []
    result.connection = SimpleNamespace(send=result.messages.append)
    result.process = SimpleNamespace(is_alive=lambda: True)
    return result


def test_fifo_compaction_removes_only_previously_queued_images():
    worker = sender()
    samples = [{"frame_index": i, "images": {"head": np.full((32,32,3), i, np.uint8)}}
               for i in range(0, 121, 8)]
    observation = {"images": samples[-1]["images"], "eef_state": np.zeros(20),
                   "vision_history": {"session": "s", "frame_index": 120, "samples": samples}}
    assert worker._compact_observation(observation) is observation
    for sample in samples:
        assert worker.offer_vision(dict(sample, session="s"))
    worker.submit(observation, {"anchor_index":120})
    assert [v["sample"]["frame_index"] for v in worker.messages[:-1]] == list(range(0,121,8))
    packed = worker.messages[-1]["observation"]
    assert packed["images"] == {} and all("images" not in v for v in packed["vision_history"]["samples"])
    assert all("images" in v for v in observation["vision_history"]["samples"])
    assert len(pickle.dumps(packed)) < len(pickle.dumps(observation)) / 10
    other = dict(observation, vision_history=dict(observation["vision_history"], session="new"))
    assert worker._compact_observation(other) is other


def test_vision_backpressure_never_advances_cursor_for_unsent_frames():
    worker = sender()
    worker._vision_sends = deque(Future() for _ in range(4))
    assert not worker.offer_vision({"session":"s", "frame_index":8, "images":{}})
    assert worker.vision_cursor("s") is None and not worker._vision_known


def test_worker_warms_before_ready_and_reports_background_work_without_array_diagnostics(monkeypatch):
    import quicdeploy.policy.base as base
    from quicdeploy.common.geometry import to6
    events = []
    class Policy:
        def warmup(self): events.append("warmup")
        def observe_vision(self, sample):
            events.append("vision")
            return True
        def infer(self, observation, context):
            events.append("action")
            self.history_diagnostics = {"new_frame_encodings":0,"cached_frames":16}
            self.last_frame_index = 8
            raw = np.zeros((50,20)); raw[:,3:9] = raw[:,13:19] = to6(np.eye(3))
            return raw
    monkeypatch.setattr(base,"load_adapter",lambda config: Policy())
    incoming = iter([{"kind":"vision","sample":{}},
                     {"id":1,"observation":{"eef_state":np.zeros(20)},"context":{}},None])
    outgoing = []
    connection = SimpleNamespace(recv=lambda:next(incoming),send=outgoing.append,close=lambda:None)
    worker_main(connection,{"horizon":50,"temporal_sampling":{"history_frames":16},"record_diagnostics":False})
    assert events == ["warmup","vision","action"]
    assert outgoing[0]["kind"] == "ready"
    assert outgoing[1]["vision_cache"]["background_frames"] == 1
    assert "diagnostics" not in outgoing[1]


def test_runtime_metrics_expose_request_backfill_and_background_counts():
    from quicdeploy.runtime.inference_metrics import InferenceMetrics
    cache={"new_frame_encodings":0,"background_frames":2}
    event=InferenceMetrics(log=False).record({"roundtrip_s":.05,"inference_s":.04,"vision_cache":cache})
    assert event["vision_cache"] == cache
    assert "vision_new=0 background_new=2" in InferenceMetrics.format_line(event)
