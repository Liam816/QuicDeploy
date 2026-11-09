"""Threaded OpenCV camera with a timestamped latest-frame buffer.

带后台读帧线程的 OpenCV 相机：始终持有“最新一帧 + 采集时间戳”。
The observer reads (latest_frame, latest_timestamp) under frame_lock and checks
thread liveness, so these attribute names are part of the contract.
观测器在 frame_lock 下读取 latest_frame / latest_timestamp 并检查线程存活，
这些属性名属于接口约定。

Timestamps use time.monotonic(), the clock used by the whole control loop.
时间戳使用 time.monotonic()，与整个控制环同一时钟。
"""
import logging
import math
import time
from collections import deque
from threading import Event, Lock, Thread

import cv2

logger = logging.getLogger(__name__)

_BACKENDS = {"ANY": cv2.CAP_ANY, "V4L2": cv2.CAP_V4L2}


class _Disconnected(Exception):
    pass


class OpenCVCamera:
    def __init__(self, config):
        self.config = config
        self.index_or_path = config.index_or_path
        self.width, self.height, self.fps = config.width, config.height, config.fps
        self.warmup_s = config.warmup_s
        self.videocapture = None
        self.thread = None
        self.stop_event = None
        self.frame_lock = Lock()
        self.latest_frame = None
        self.latest_timestamp = None
        self.new_frame_event = Event()
        self.frame_index = -1
        self.frame_history = deque(maxlen=1)

    def enable_history(self, capacity):
        """Start a fresh acquisition window, retaining only the latest current frame."""
        with self.frame_lock:
            self.frame_history = deque(maxlen=capacity)
            if self.latest_frame is not None and self.latest_timestamp is not None:
                self.frame_history.append((self.frame_index, self.latest_timestamp, self.latest_frame))

    def __str__(self):
        return f"OpenCVCamera({self.index_or_path})"

    @property
    def is_connected(self):
        return isinstance(self.videocapture, cv2.VideoCapture) and self.videocapture.isOpened()

    # ------------------------------------------------------------------ connect
    def connect(self):
        """Open, apply format, start the reader and wait for a first frame.
        打开设备、设置格式、启动读帧线程，并在 warmup_s 内等待第一帧。
        """
        if self.is_connected:
            raise RuntimeError(f"{self} already connected")
        cv2.setNumThreads(1)
        self.videocapture = cv2.VideoCapture(str(self.index_or_path), _BACKENDS[self.config.backend])
        if not self.videocapture.isOpened():
            self.videocapture.release()
            self.videocapture = None
            raise ConnectionError(f"Failed to open {self}.")
        self._configure()
        self._start_read_thread()
        if self.warmup_s > 0:
            deadline = time.time() + self.warmup_s
            last_timeout = None
            while time.time() < deadline:
                remaining_ms = max(1, int((deadline - time.time()) * 1000))
                try:
                    self._wait_frame(timeout_ms=min(1000, remaining_ms))
                    last_timeout = None
                    break
                except TimeoutError as exc:
                    last_timeout = exc
                    time.sleep(0.1)
            with self.frame_lock:
                if self.latest_frame is None:
                    if last_timeout is not None:
                        raise TimeoutError(
                            f"Timed out waiting for frame from camera {self} after warmup_s={self.warmup_s}."
                        ) from last_timeout
                    raise ConnectionError(f"{self} failed to capture frames during warmup.")
        logger.info("%s connected.", self)

    def _configure(self):
        # FOURCC first: it constrains the available sizes and rates (Linux order).
        # 先设 FOURCC：它决定可用的分辨率和帧率。
        cap = self.videocapture
        if self.config.fourcc is not None:
            success = cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*self.config.fourcc))
            code = int(cap.get(cv2.CAP_PROP_FOURCC))
            actual = "".join(chr((code >> 8 * i) & 0xFF) for i in range(4))
            if not success or actual != self.config.fourcc:
                logger.warning("%s failed to set fourcc=%s (actual=%s); continuing.", self, self.config.fourcc, actual)
        for prop, wanted, name in ((cv2.CAP_PROP_FRAME_WIDTH, self.width, "width"),
                                   (cv2.CAP_PROP_FRAME_HEIGHT, self.height, "height")):
            cap.set(prop, float(wanted))
            actual = int(round(cap.get(prop)))
            if actual != wanted:
                raise RuntimeError(f"{self} failed to set {name}={wanted} (actual={actual}).")
        cap.set(cv2.CAP_PROP_FPS, float(self.fps))
        actual_fps = cap.get(cv2.CAP_PROP_FPS)
        if not math.isclose(self.fps, actual_fps, rel_tol=1e-3):
            raise RuntimeError(f"{self} failed to set fps={self.fps} (actual={actual_fps}).")

    # ------------------------------------------------------------------ reader thread
    def _postprocess(self, image):
        h, w, c = image.shape
        if h != self.height or w != self.width:
            raise RuntimeError(f"{self} frame {w}x{h} does not match configured {self.width}x{self.height}.")
        if c != 3:
            raise RuntimeError(f"{self} frame channels={c}, expected 3.")
        if self.config.color_mode == "rgb":
            return cv2.cvtColor(image, cv2.COLOR_BGR2RGB)
        return image

    def _read_loop(self):
        stop_event = self.stop_event
        failure_count = 0
        while not stop_event.is_set():
            try:
                if self.videocapture is None:
                    raise _Disconnected
                ok, raw = self.videocapture.read()
                if not ok:
                    raise RuntimeError(f"{self} read failed (status={ok}).")
                frame = self._postprocess(raw)
                stamp = time.monotonic()
                with self.frame_lock:
                    self.frame_index += 1
                    self.latest_frame = frame
                    self.latest_timestamp = stamp
                    self.frame_history.append((self.frame_index, stamp, frame))
                self.new_frame_event.set()
                failure_count = 0
            except _Disconnected:
                break
            except Exception as exc:
                # Tolerate up to 10 consecutive failures; then the thread dies and
                # the observer stops the deployment on "read thread stopped".
                # 最多容忍 10 次连续失败，之后线程退出，观测器据此停止部署。
                if failure_count <= 10:
                    failure_count += 1
                    logger.warning("Error reading frame in background thread for %s: %s", self, exc)
                else:
                    raise RuntimeError(f"{self} exceeded maximum consecutive read failures.") from exc

    def _start_read_thread(self):
        self._stop_read_thread()
        self.stop_event = Event()
        self.thread = Thread(target=self._read_loop, name=f"{self}_read_loop", daemon=True)
        self.thread.start()
        time.sleep(0.1)

    def _stop_read_thread(self):
        if self.stop_event is not None:
            self.stop_event.set()
        if self.thread is not None and self.thread.is_alive():
            self.thread.join(timeout=2.0)
        self.thread = None
        self.stop_event = None
        with self.frame_lock:
            self.latest_frame = None
            self.latest_timestamp = None
            self.new_frame_event.clear()

    def _wait_frame(self, timeout_ms=200):
        if self.thread is None or not self.thread.is_alive():
            raise RuntimeError(f"{self} read thread is not running.")
        if not self.new_frame_event.wait(timeout=timeout_ms / 1000.0):
            with self.frame_lock:
                frame = self.latest_frame
            if frame is not None:
                return frame
            raise TimeoutError(f"Timed out waiting for frame from camera {self} after {timeout_ms} ms.")
        with self.frame_lock:
            frame = self.latest_frame
            self.new_frame_event.clear()
        return frame

    def disconnect(self):
        if self.thread is not None:
            self._stop_read_thread()
        if self.videocapture is not None:
            self.videocapture.release()
            self.videocapture = None
        with self.frame_lock:
            self.latest_frame = None
            self.latest_timestamp = None
            self.new_frame_event.clear()
        logger.info("%s disconnected.", self)
