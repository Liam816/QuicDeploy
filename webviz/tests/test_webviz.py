"""Hardware-free contract tests for sidecar transport and runtime tap."""
import base64
import json
import threading
import time
from types import SimpleNamespace
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import numpy as np
import pytest

from webviz.server import make_server
from webviz.telemetry import Telemetry, prediction, post


@pytest.fixture
def server():
    service = make_server(port=0)
    thread = threading.Thread(target=service.serve_forever, daemon=True)
    thread.start()
    yield service
    service.shutdown()
    service.server_close()
    thread.join(timeout=1)


def packet(session="first", jpeg=True):
    return {"session_id": session, "current": {"step": 2}, "previous": {"step": 1}, "source": "live",
            "status": "running", "robot": None,
            "cameras": {"head": {"timestamp": time.monotonic(), "width": 4, "height": 4,
                                  **({"jpeg": base64.b64encode(b"jpeg-test").decode()} if jpeg else {})}}}


def test_http_ingest_streams_and_session_reset(server):
    port = server.server_port
    post(port, packet())
    with urlopen(f"http://127.0.0.1:{port}/api/state", timeout=1) as response:
        data = json.load(response)
    assert data["current"]["step"] == 2 and data["previous"]["step"] == 1
    assert "jpeg" not in data["cameras"]["head"]
    with urlopen(f"http://127.0.0.1:{port}/api/events", timeout=1) as response:
        assert response.readline().startswith(b"id: ")
        assert json.loads(response.readline().removeprefix(b"data: "))["session_id"] == "first"
    with urlopen(f"http://127.0.0.1:{port}/api/camera/head", timeout=1) as response:
        assert response.readline() == b"--frame\r\n"
        assert response.readline() == b"Content-Type: image/jpeg\r\n"
        assert response.readline().startswith(b"Content-Length: ")
        assert response.readline() == b"\r\n"
        assert response.read(9) == b"jpeg-test"
    post(port, packet("second", jpeg=False))
    assert not server.state.images


def test_ingest_requires_producer_header_and_assets_are_confined(server, tmp_path):
    url = f"http://127.0.0.1:{server.server_port}"
    with pytest.raises(HTTPError) as rejected:
        urlopen(Request(url + "/api/ingest", data=b"{}"), timeout=1)
    assert rejected.value.code == 403
    urdf = tmp_path / "robot.urdf"
    urdf.write_text("<robot/>")
    server.state.urdf = urdf
    with pytest.raises(HTTPError) as rejected:
        urlopen(url + "/robot/%2e%2e/server.py", timeout=1)
    assert rejected.value.code == 404


def test_original_and_execution_trajectories_keep_world_values():
    u = SimpleNamespace(times=np.array([10., 11.]), poses=np.arange(40).reshape(2, 20))
    e = SimpleNamespace(times=np.array([11.]), poses=np.arange(20).reshape(1, 20) + 100)
    accepted = SimpleNamespace(u=u, e=e, received_at=10., expired_steps=1, fused_steps=1,
                               selected_steps=2, truncated_steps=0)
    request = SimpleNamespace(observation_time=9., anchor=np.zeros(20), prefix_source_rows=(1, 2))
    result = prediction(7, {"roundtrip_s": .1}, accepted, request, {"joint_state": np.zeros(14)})
    assert result["u"]["poses"] == u.poses.tolist()
    assert result["e"]["poses"] == e.poses.tolist()
    assert result["prefix_source_rows"] == [1, 2]
    assert result["roundtrip_ms"] == 100


def test_latest_pair_is_bounded_and_publish_does_no_serialization():
    telemetry = object.__new__(Telemetry)
    telemetry.predictions = (None, None)
    for step in range(10000):
        telemetry.publish_prediction(step, object())
    assert telemetry.predictions[0][0] == 9998
    assert telemetry.predictions[1][0] == 9999


def test_stopped_sidecar_does_not_break_publisher():
    session = SimpleNamespace(result={"status": "passed"}, paused=False, held=True, pending_request=None,
                              measured=None, observer=None, last_command=None, robot=None,
                              config={"model": {"id": "test"}}, mode="async",
                              exe={"control_hz": 50, "policy_hz": 30})
    telemetry = Telemetry(session, port=1, fps=20)
    time.sleep(.1)
    assert telemetry.thread.is_alive()
    telemetry.close()
    assert not telemetry.thread.is_alive()
