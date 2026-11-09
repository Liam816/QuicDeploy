"""Latest-only telemetry tap; all JSON/JPEG/network work runs off the control path."""
import base64
import json
import os
import threading
import time
import uuid
import warnings
from urllib.request import Request, urlopen


def post(port, packet):
    body = json.dumps(packet, allow_nan=False, separators=(",", ":")).encode()
    request = Request(f"http://127.0.0.1:{port}/api/ingest", data=body,
                      headers={"Content-Type": "application/json", "X-QuicDeploy-Ingest": "1"})
    with urlopen(request, timeout=0.35) as response:
        response.read()


def prediction(step, reply, accepted, request, observation):
    """Called on the publisher thread; accepted trajectories are immutable."""
    return {"step": step, "observation_time": request.observation_time,
            "received_at": accepted.received_at, "anchor": request.anchor.tolist(),
            "u": {"times": accepted.u.times.tolist(), "poses": accepted.u.poses.tolist()},
            "e": {"times": accepted.e.times.tolist(), "poses": accepted.e.poses.tolist()},
            "joint_state": observation.get("joint_state", []).tolist()
            if hasattr(observation.get("joint_state"), "tolist") else [],
            "roundtrip_ms": 1000 * float(reply.get("roundtrip_s", 0)),
            "inference_ms": None if "inference_s" not in reply else 1000 * float(reply["inference_s"]),
            "expired_steps": accepted.expired_steps, "fused_steps": accepted.fused_steps,
            "selected_steps": accepted.selected_steps, "truncated_steps": accepted.truncated_steps,
            "prefix_source_rows": list(request.prefix_source_rows)}


class Telemetry:
    def __init__(self, session, *, port, fps=8, width=640):
        self.session = session
        self.port, self.period, self.width = port, 1 / fps, width
        self.id = uuid.uuid4().hex
        self.predictions = (None, None)
        self._cached = (None, None)
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self.run, name="webviz-publisher", daemon=True)
        self.thread.start()

    def publish_prediction(self, *args):
        # Tuple replacement is atomic under CPython. No image copy, I/O or lock.
        self.predictions = (self.predictions[1], args)

    def packet(self, final=False):
        session = self.session
        previous, current = self.predictions
        cached_refs, cached_values = self._cached
        refs = (previous, current)
        if cached_refs is None or any(a is not b for a, b in zip(refs, cached_refs)):
            cached_values = tuple(prediction(*item) if item else None for item in refs)
            self._cached = (refs, cached_values)
        status = (session.result.get("status", "stopped") if final else
                  "paused" if session.paused else "predicting" if session.pending_request is not None else
                  "holding" if session.held else "running")
        measured = session.measured
        observer = session.observer
        if observer is not None and observer.joints:
            measured = observer.joints[-1][1]
        return {"session_id": self.id, "source": "live", "status": status, "finished": final,
                "model": session.config["model"]["id"], "mode": session.mode,
                "control_hz": session.exe["control_hz"], "policy_hz": session.exe["policy_hz"],
                "inference": (session.rec.inference_metrics.snapshot()
                              if hasattr(session, "rec") and hasattr(session.rec, "inference_metrics") else None),
                "previous": cached_values[0], "current": cached_values[1],
                "robot": {"joints": None if measured is None else measured.tolist(),
                          "command": None if session.last_command is None else session.last_command.tolist()},
                "cameras": {}, "error": session.result.get("error")}

    def run(self):
        # Existing runtime dependencies only, imported away from the control loop.
        import cv2
        last_stamps = {}
        while not self.stop.is_set():
            started = time.monotonic()
            try:
                packet = self.packet()
                robot = self.session.robot
                if robot is not None:
                    for name, camera in robot.cameras.items():
                        # Never wait for the capture thread's lock.
                        if not camera.frame_lock.acquire(blocking=False):
                            continue
                        try:
                            image, stamp = camera.latest_frame, camera.latest_timestamp
                        finally:
                            camera.frame_lock.release()
                        if image is None or stamp is None:
                            continue
                        metadata = {"timestamp": float(stamp), "width": int(image.shape[1]),
                                    "height": int(image.shape[0]), "preview_fps": round(1 / self.period, 1)}
                        if image.shape[1] > self.width:
                            metadata.update(source_width=int(image.shape[1]), source_height=int(image.shape[0]),
                                            width=self.width, height=round(image.shape[0] * self.width / image.shape[1]))
                        if stamp != last_stamps.get(name):
                            preview = image
                            if image.shape[1] > self.width:
                                preview = cv2.resize(image, (self.width, round(image.shape[0] * self.width / image.shape[1])))
                            color = getattr(getattr(camera, "config", None), "color_mode", "rgb")
                            if color == "rgb":
                                preview = cv2.cvtColor(preview, cv2.COLOR_RGB2BGR)
                            ok, encoded = cv2.imencode(".jpg", preview, [cv2.IMWRITE_JPEG_QUALITY, 75])
                            if ok:
                                metadata["jpeg"] = base64.b64encode(encoded).decode("ascii")
                        packet["cameras"][name] = metadata
                post(self.port, packet)
                for name, camera in packet["cameras"].items():
                    last_stamps[name] = camera["timestamp"]
            except Exception:
                # Visualization is best effort; retry next tick with bounded memory.
                last_stamps.clear()
            self.stop.wait(max(0.01, self.period - (time.monotonic() - started)))
        try:
            post(self.port, self.packet(final=True))
        except Exception:
            pass

    def close(self):
        self.stop.set()
        self.thread.join(timeout=1.2)


def start(session):
    if os.environ.get("QUICDEPLOY_WEBVIZ", "0") != "1":
        return None
    try:
        return Telemetry(session, port=int(os.environ.get("QUICDEPLOY_WEBVIZ_PORT", "8765")),
                         fps=max(1, min(20, float(os.environ.get("QUICDEPLOY_WEBVIZ_FPS", "8")))),
                         width=max(160, min(1280, int(os.environ.get("QUICDEPLOY_WEBVIZ_WIDTH", "640")))))
    except Exception as error:
        warnings.warn(f"Web visualization unavailable; inference continues: {error}")
        return None
