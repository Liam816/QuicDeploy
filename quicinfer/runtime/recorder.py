"""Per-run output directory: config snapshot, command trace, predictions, report.

单次运行的输出目录：配置快照、逐拍指令日志、预测快照与运行报告。

    outputs/live/<timestamp>_<mode>/
      resolved_config.json   exact config used / 本次使用的完整配置
      kinematics.urdf        URDF snapshot / URDF 快照
      ik_worker.log          IK subprocess log / IK 子进程日志
      commands.jsonl         every IK/guard/send attempt + pause events / 每拍指令与暂停事件
      chunk_XXXXXX.npz       (diagnostics only) request inputs and outputs / 诊断模式下每次推理的输入输出
      report.json            summary written on exit / 退出时写入的汇总报告

Prediction snapshots are written on a background thread, never on the CAN path.
预测快照在后台线程写盘，不占用控制环。
"""
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np

from ..common.io import PROJECT_ROOT, canonical


def prediction_arrays(reply, accepted, request, observation, *, held_verified=False):
    """Immutable snapshot of one request/reply pair. / 一次请求与结果的不可变快照。"""
    names = list(observation["images"])
    arrays = dict(
        trace_schema_version=2, raw=np.asarray(reply["raw"]).copy(),
        observation_time=request.observation_time, anchor=request.anchor.copy(),
        u_times=accepted.u.times.copy(), u_poses=accepted.u.poses.copy(),
        e_times=accepted.e.times.copy(), e_poses=accepted.e.poses.copy(),
        policy_origin_time=accepted.policy_origin_time, requested_at=request.requested_at,
        received_at=accepted.received_at, held_verified=held_verified,
        expired_steps=accepted.expired_steps, fused_steps=accepted.fused_steps, gap_s=accepted.gap_s,
        prefix_source_rows=request.prefix_source_rows, selected_steps=accepted.selected_steps,
        truncated_steps=accepted.truncated_steps,
        prefix_absolute=np.empty((0, 20)) if request.prefix_u is None else request.prefix_u.copy(),
        camera_names=np.asarray(names),
        camera_timestamps=np.asarray([observation.get("camera_timestamps", {}).get(n, np.nan) for n in names]),
        joint_timestamp=observation.get("joint_timestamp", np.nan),
        roundtrip_s=reply.get("roundtrip_s", np.nan),
    )
    for name in ("inference_s", "inference_started_at", "inference_finished_at"):
        if name in reply:
            arrays[name] = reply[name]
    if "joint_state" in observation:
        arrays["observation_joint_state"] = observation["joint_state"].copy()
    arrays.update(reply.get("diagnostics", {}))
    # capture() already owns these RGB copies; uncompressed to save CPU.
    # capture() 已经拷贝过图像，这里直接引用；不压缩以节省 CPU。
    arrays.update({"image_" + name: observation["images"][name] for name in names})
    return arrays


class RunRecorder:
    def __init__(self, config, *, diagnostics, root=None):
        root = Path(root) if root else PROJECT_ROOT / "outputs/live"
        self.out = root / f"{time.strftime('%Y%m%d_%H%M%S')}_{config['execution']['mode']}"
        self.out.mkdir(parents=True, exist_ok=False)
        (self.out / "resolved_config.json").write_text(canonical(config) + "\n")
        if config["kinematics"].get("urdf"):
            (self.out / "kinematics.urdf").write_bytes(Path(config["kinematics"]["urdf"]).read_bytes())
        self.diagnostics = diagnostics
        self.result = {
            "status": "running", "mode": config["execution"]["mode"], "fingerprint": config["fingerprint"],
            "model_id": config["model"]["id"], "hardware_commands": 0, "prediction_events": [],
            "hold_events": [], "diagnostics_enabled": diagnostics, "command_trace_enabled": True,
        }
        self.trajectory_steps = {}   # id(E trajectory) -> request step / E 轨迹到请求序号的映射
        self._writer = ThreadPoolExecutor(max_workers=1, thread_name_prefix="policy-result") if diagnostics else None
        self._writes = []
        self._commands = (self.out / "commands.jsonl").open("w", buffering=1)

    @property
    def ik_log(self):
        return self.out / "ik_worker.log"

    def append(self, key, event):
        self.result.setdefault(key, []).append(event)

    def write_event(self, event):
        self._commands.write(canonical(event) + "\n")
        self._commands.flush()

    def record_command(self, row):
        self._commands.write(canonical(row) + "\n")

    def save_prediction(self, step, reply, accepted, request, observation, *, held_verified=False):
        self.trajectory_steps[id(accepted.e)] = step
        if not self.diagnostics:
            return
        arrays = prediction_arrays(reply, accepted, request, observation, held_verified=held_verified)
        self._writes.append(self._writer.submit(np.savez, self.out / f"chunk_{step:06d}.npz", **arrays))

    def check_writes(self):
        """Surface storage errors early; drop finished futures. / 尽早暴露写盘错误。"""
        for future in self._writes:
            if future.done():
                future.result()
        self._writes[:] = [future for future in self._writes if not future.done()]

    def finish(self):
        """Flush pending writes and write report.json. / 等待写盘完成并输出 report.json。"""
        if self._writer is not None:
            self._writer.shutdown(wait=True, cancel_futures=False)
        for future in self._writes:
            try:
                future.result()
            except BaseException as error:
                self.result["status"] = "failed"
                self.result["storage_error"] = repr(error)
        self._commands.close()
        report = self.out / "report.json"
        report.write_text(canonical(self.result) + "\n")
        print("Report:", report, flush=True)
