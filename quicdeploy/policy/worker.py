"""Model inference in a spawned process with one outstanding request.

在独立（spawn）进程中运行模型推理，同一时刻最多一个未完成请求。

Why a process / 为什么用进程: the GPU call must never block the 50 Hz control
thread, and a timed-out call must be killable without leaving CUDA state behind.
GPU 推理绝不能阻塞 50Hz 控制线程；超时的推理必须能被直接杀掉，不残留 CUDA 状态。

Observation pickling + IPC also run on a helper thread, so submit() returns at once.
观测数据的序列化与进程间发送也放在辅助线程中，submit() 立即返回。
"""
import multiprocessing
import time
import traceback
from concurrent.futures import ThreadPoolExecutor


def worker_main(connection, config):
    try:
        from ..common.geometry import validate_pose
        from .base import load_adapter

        adapter = load_adapter(config)
        connection.send({"kind": "ready"})
        while True:
            request = connection.recv()
            if request is None:
                return
            started = time.monotonic()
            raw = validate_pose(adapter.infer(request["observation"], request["context"]))
            if len(raw) > config["horizon"]:
                raise ValueError("Model returned padding or excess horizon")
            reply = {
                "kind": "prediction", "id": request["id"], "raw": raw,
                "inference_s": time.monotonic() - started,
                "anchor": request["observation"]["eef_state"],
            }
            if config.get("record_diagnostics", False):
                reply.update(diagnostics=getattr(adapter, "inference_diagnostics", {}),
                             inference_started_at=started, inference_finished_at=time.monotonic())
            connection.send(reply)
    except BaseException:
        try:
            connection.send({"kind": "error", "error": traceback.format_exc()})
        except (OSError, EOFError):
            pass
    finally:
        connection.close()


class InferenceWorker:
    def __init__(self, config, *, startup_timeout_s=120):
        ctx = multiprocessing.get_context("spawn")
        self.connection, child = ctx.Pipe()
        self.process = ctx.Process(target=worker_main, args=(child, config), daemon=True)
        self.pending = None   # (request id, submitted_at) / (请求号, 提交时刻)
        self.counter = 0
        self._sender = ThreadPoolExecutor(max_workers=1, thread_name_prefix="policy-observation")
        self._send_future = None
        self.process.start()
        child.close()
        try:
            if not self.connection.poll(startup_timeout_s):
                raise TimeoutError("Model worker did not initialize")
            if self._receive()["kind"] != "ready":
                raise RuntimeError("Invalid worker initialization")
        except BaseException:
            self.close()
            raise

    def _receive(self):
        reply = self.connection.recv()
        if reply["kind"] == "error":
            raise RuntimeError("Inference worker failed:\n" + reply["error"])
        return reply

    def submit(self, observation, context):
        """Non-blocking enqueue of one request. / 非阻塞地提交一次推理请求。"""
        if self.pending is not None:
            raise RuntimeError("Only one outstanding inference is allowed")
        self.counter += 1
        self.pending = (self.counter, time.monotonic())
        self._send_future = self._sender.submit(self._send, self.counter, observation, context)

    def _send(self, index, observation, context):
        self.connection.send(dict(id=index, observation=observation, context=context))

    def poll(self):
        """Reply with 'roundtrip_s', or None if not ready. / 有结果则返回，否则返回 None。"""
        if self.pending is None:
            return None
        if self._send_future is not None:
            if not self._send_future.done():
                if not self.process.is_alive():
                    raise RuntimeError("Inference worker exited during request dispatch")
                return None
            self._send_future.result()  # propagate IPC errors / 抛出发送阶段的异常
            self._send_future = None
        if not self.process.is_alive() and not self.connection.poll():
            raise RuntimeError("Inference worker exited")
        if not self.connection.poll():
            return None
        reply = self._receive()
        index, started = self.pending
        if reply["kind"] != "prediction" or reply["id"] != index:
            raise RuntimeError("Stale or mismatched inference result")
        reply["roundtrip_s"] = time.monotonic() - started
        self.pending = None
        return reply

    def wait(self, timeout=60, *, on_tick=None):
        """Block until the reply arrives; `on_tick` runs every ~1 ms (e.g. key polling).
        阻塞等待结果；on_tick 约每 1ms 调用一次（例如检查键盘）。
        """
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if on_tick is not None:
                on_tick()
            result = self.poll()
            if result is not None:
                return result
            time.sleep(0.001)
        raise TimeoutError("Inference request timed out")

    def close(self):
        # A timed-out GPU call must never survive into another session.
        # 超时的 GPU 调用绝不能残留到下一次会话。
        if self.process.is_alive():
            self.process.terminate()
        self.process.join(timeout=3)
        if self.process.is_alive():
            self.process.kill()
            self.process.join(timeout=3)
        self.connection.close()
        self._sender.shutdown(wait=True, cancel_futures=True)
