"""No GPU/CAN tests for measured inference timing and logging isolation."""
import threading
import time
from types import SimpleNamespace

import numpy as np
import pytest

from quicdeploy.runtime.inference_metrics import InferenceMetrics
from quicdeploy.runtime.recorder import RunRecorder
from webviz.telemetry import Telemetry


def test_latency_reciprocal_and_actual_completion_rate_are_distinct():
    metrics = InferenceMetrics(log=False)
    first = metrics.record({"roundtrip_s": .125, "inference_s": .1}, step=10, now=100)
    assert first["latency_ms"] == 125
    assert first["model_ms"] == 100
    assert first["latency_equivalent_hz"] == 8
    assert first["frequency_hz"] == .2  # one completion in a fixed five-second window
    second = metrics.record({"roundtrip_s": .25}, step=11, now=101)
    assert second["frequency_hz"] == .4
    assert second["latency_equivalent_hz"] == 4
    assert metrics.snapshot(now=105)["window_completed"] == 1  # left boundary excluded
    assert metrics.snapshot(now=106)["frequency_hz"] == 0
    assert metrics.snapshot(now=106)["sample_age_s"] == 5
    assert InferenceMetrics(log=False).snapshot() is None


def test_invalid_timing_is_missing_not_infinite():
    metrics = InferenceMetrics(log=False)
    for value in (None, 0, -1, float("nan"), float("inf")):
        event = metrics.record({"roundtrip_s": value}, now=100)
        assert event["latency_ms"] is None and event["latency_equivalent_hz"] is None


def test_terminal_logs_measurements_without_blocking_record(monkeypatch):
    entered, release = threading.Event(), threading.Event()
    lines = []
    def slow_print(line, **kwargs):
        entered.set()
        release.wait(timeout=2)
        lines.append(line)
    monkeypatch.setattr("builtins.print", slow_print)
    metrics = InferenceMetrics()
    try:
        metrics.record({"roundtrip_s": .125, "inference_s": .1}, step=7, now=100)
        assert entered.wait(timeout=1)
        started = time.monotonic()
        for i in range(200):
            metrics.record({"roundtrip_s": .125}, step=8 + i, now=101 + i / 100)
        assert time.monotonic() - started < .5
        assert metrics._logs.qsize() <= 128
    finally:
        release.set()
        metrics.close()
    assert "latency=125.0 ms" in lines[0]
    assert "model=100.0 ms" in lines[0]
    assert "equivalent=8.00 Hz" in lines[0]
    assert "frequency_5s=0.20 Hz" in lines[0]


def test_recorder_logs_when_webviz_and_diagnostics_are_disabled(tmp_path, capsys):
    config = {"execution": {"mode": "async"}, "kinematics": {},
              "fingerprint": "test", "model": {"id": "test"}}
    recorder = RunRecorder(config, diagnostics=False, root=tmp_path)
    recorder.record_inference({"roundtrip_s": .2, "inference_s": .15}, step=3)
    recorder.finish()
    assert "latency=200.0 ms" in capsys.readouterr().out
    assert recorder.result["inference_events"][0]["latency_equivalent_hz"] == 5
    assert recorder.result["inference_events"][0]["model_ms"] == 150


def test_telemetry_publishes_metrics_independently_of_accepted_trajectory():
    metrics = InferenceMetrics(log=False)
    metrics.record({"roundtrip_s": .2, "inference_s": .15}, step=4)
    session = SimpleNamespace(result={"status": "running"}, paused=False, held=True,
                              pending_request=None, measured=None, observer=None, last_command=None,
                              config={"model": {"id": "test"}}, mode="async",
                              exe={"control_hz": 50, "policy_hz": 30},
                              rec=SimpleNamespace(inference_metrics=metrics))
    telemetry = object.__new__(Telemetry)
    telemetry.session, telemetry.id = session, "test-session"
    telemetry.predictions, telemetry._cached = (None, None), (None, None)
    packet = telemetry.packet()
    assert packet["inference"]["latency_ms"] == 200
    assert packet["inference"]["latency_equivalent_hz"] == 5
    assert packet["current"] is None  # finished requests can be rejected by the scheduler
