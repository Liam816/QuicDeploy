"""Measured inference timing with bounded, non-blocking terminal output."""
import math
import queue
import threading
import time


def _milliseconds(value):
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    return value * 1000 if math.isfinite(value) and value > 0 else None


class InferenceMetrics:
    """One control-thread writer; readers consume an immutable state snapshot.

    latency_ms is submit-to-reply round trip, including IPC and poll delay.
    frequency_hz counts completed requests in (now - 5 seconds, now] / 5.
    latency_equivalent_hz is 1000 / latency_ms, not scheduled throughput.
    """
    window_s = 5.0

    def __init__(self, *, log=True):
        self._state = (0, (), None)
        self._stop = threading.Event()
        self._logs = queue.Queue(maxsize=128)
        self._thread = None
        if log:
            self._thread = threading.Thread(target=self._write_logs, name="inference-log", daemon=True)
            self._thread.start()

    def record(self, reply, *, step=None, now=None):
        now = time.monotonic() if now is None else float(now)
        count, times, _ = self._state
        times = tuple(t for t in times if t > now - self.window_s) + (now,)
        latency = _milliseconds(reply.get("roundtrip_s"))
        event = {"sequence": count + 1, "step": step, "completed_at": now,
                 "latency_ms": latency, "model_ms": _milliseconds(reply.get("inference_s")),
                 "latency_equivalent_hz": None if latency is None else 1000 / latency}
        if reply.get("vision_cache") is not None:
            event["vision_cache"] = dict(reply["vision_cache"])
        self._state = (count + 1, times, event)
        snapshot = self.snapshot(now=now)
        if self._thread is not None:
            try:
                self._logs.put_nowait(snapshot)
            except queue.Full:
                pass  # A slow terminal never backpressures the arm control loop.
        return snapshot

    def snapshot(self, *, now=None):
        now = time.monotonic() if now is None else float(now)
        count, times, event = self._state
        if event is None:
            return None
        completed = sum(now - self.window_s < t <= now for t in times)
        return dict(event, total_completed=count, window_s=self.window_s,
                    window_completed=completed, frequency_hz=completed / self.window_s,
                    sample_age_s=max(0.0, now - event["completed_at"]))

    @staticmethod
    def format_line(event):
        def value(key, places):
            number = event.get(key)
            return "n/a" if number is None else f"{number:.{places}f}"
        cache = event.get("vision_cache")
        suffix = (f" vision_new={cache['new_frame_encodings']} background_new={cache['background_frames']}"
                  if cache is not None else "")
        return (f"[inference #{event['sequence']:06d} step={event.get('step')}] "
                f"latency={value('latency_ms', 1)} ms "
                f"model={value('model_ms', 1)} ms "
                f"equivalent={value('latency_equivalent_hz', 2)} Hz "
                f"completed_5s={event['window_completed']} "
                f"frequency_5s={value('frequency_hz', 2)} Hz" + suffix)

    def _write_logs(self):
        writable = True
        while not self._stop.is_set() or not self._logs.empty():
            try:
                event = self._logs.get(timeout=.1)
            except queue.Empty:
                continue
            if writable:
                try:
                    print(self.format_line(event), flush=True)
                except Exception:
                    writable = False

    def close(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=1)
