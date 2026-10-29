"""Hardware-free action-queue scheduler for sync / async / RTC execution.

无硬件依赖的动作队列调度器，统一支持 sync（停等）、async（异步预取）和 RTC（带固定前缀的异步）。

Terminology / 术语:
    U  raw model trajectory, decoded against the REAL observation anchor.
       模型原始轨迹，用真实观测锚点解码得到的绝对位姿。
    E  execution trajectory: U cropped to the future, lead-compensated and
       blended with the previous E. The control loop only ever samples E.
       执行轨迹：U 裁掉已过期部分、做超前补偿、并与上一段 E 融合；控制环只采样 E。

Queue timing / 队列时序 (policy_hz rows, n_action_steps executed per chunk):
    The next request fires when `prefetch_remaining_steps` rows remain.
    In RTC mode the first `prefix_steps` of those remaining rows are sent to the
    model as a fixed (clean) prefix, so the new chunk joins the old one seamlessly.
    当剩余 prefetch_remaining_steps 行时发起下一次推理；RTC 模式下把剩余行中的前
    prefix_steps 行作为固定前缀交给模型，使新旧动作块无缝衔接。
"""
from dataclasses import dataclass

import numpy as np

from ..common.geometry import decode, interpolate_pose, validate_pose
from .trajectory import TimedTrajectory

MODES = ("sync", "async", "rtc")


class PrefixWindowExpired(RuntimeError):
    """The old queue's fixed prefix already lies before the fresh observation.
    旧队列中的固定前缀在新观测之前就已全部过期。
    """


@dataclass(frozen=True)
class QueueRequest:
    step: int                     # monotonically increasing request id / 递增请求序号
    observation_time: float       # head-camera timestamp / 头部相机时间戳
    anchor: np.ndarray            # measured EEF20 at observation / 观测时刻实测末端位姿
    prefix_u: np.ndarray | None   # absolute RTC prefix rows, or None / RTC 绝对前缀
    policy_origin_time: float     # where row 0 of the new chunk sits / 新块第 0 行的时间原点
    requested_at: float
    prefix_source_rows: tuple     # 1-based old-queue rows used as prefix / 前缀取自旧队列的行号(1 起)
    remaining_at_request: int

    def __post_init__(self):
        object.__setattr__(self, "anchor", validate_pose(np.asarray(self.anchor)[None])[0].copy())
        if self.prefix_u is not None:
            object.__setattr__(self, "prefix_u", validate_pose(self.prefix_u).copy())


@dataclass(frozen=True)
class QueuePrediction:
    u: TimedTrajectory
    e: TimedTrajectory
    expired_steps: int            # rows dropped because inference finished too late / 推理耗时导致过期的行数
    compensated_steps: int        # rows altered by arm/gripper lead / 被超前补偿修改的行数
    fused_steps: int              # rows blended with the previous E / 与上一段 E 融合的行数
    gap_s: float                  # coverage gap after the previous E / 与上一段 E 之间的空档
    observation_time: float
    received_at: float
    selected_steps: int           # min(len(raw), n_action_steps)
    truncated_steps: int          # model rows beyond n_action_steps / 超出 n_action_steps 被丢弃的行
    policy_origin_time: float
    next_request_time: float


class QueueScheduler:
    """One outstanding inference at a time; explicit time domains.
    同一时刻最多一个未完成推理请求；观测时间、请求时间、执行时间显式区分。
    """

    def __init__(self, model, execution, postprocess):
        self.model, self.execution, self.postprocess = model, execution, postprocess
        self.mode = execution["mode"]
        if self.mode not in MODES:
            raise ValueError("Unknown schedule mode")
        self.prefix_steps = execution["prefix_steps"]
        self.policy_dt = 1.0 / execution["policy_hz"]
        # First target offset is defined in training steps; rescale to policy_hz.
        # 首个目标相对观测的偏移按训练步长定义，这里换算到部署的 policy_hz。
        self.first_offset = model["first_target_offset_s"] / model["action_dt_s"] * self.policy_dt
        self.pending = None
        self.active_u = None
        self.active_e = None
        self.previous_e = None
        self.last_request_step = None
        self.selected_steps = 0
        self.next_request_time = None

    # ------------------------------------------------------------------ queue state
    def remaining_steps(self, timestamp):
        """Selected rows of the active chunk still in the future. / 当前块中尚未到期的行数。"""
        if self.active_u is None:
            return 0
        return int(np.sum(self.active_u.times[:self.selected_steps] > float(timestamp) + 1e-9))

    def should_request(self, timestamp):
        """True when the queue reached its prefetch threshold. / 队列是否已到预取阈值。"""
        if self.pending is not None:
            return False
        if self.active_u is None:
            return True
        return self.next_request_time is not None and timestamp >= self.next_request_time - 1e-9

    def restart_rtc(self):
        """Drop expired RTC conditioning and execution; the next chunk starts held.
        丢弃已过期的 RTC 前缀与执行队列；下一块从静止状态重新开始。
        """
        if not self.prefix_steps or self.pending is not None:
            raise RuntimeError("RTC restart requires an idle RTC scheduler")
        self.active_u = None
        self.active_e = None
        self.previous_e = None
        self.selected_steps = 0
        self.next_request_time = None

    # ------------------------------------------------------------------ request
    def prepare(self, step, observation_time, anchor, *, requested_at=None):
        """Build the next request, including the RTC prefix taken from the old U.
        构造下一次推理请求；RTC 模式下从旧 U 中截取固定前缀。
        """
        requested_at = float(observation_time if requested_at is None else requested_at)
        if self.pending is not None:
            raise RuntimeError("An inference request is already outstanding")
        if (not isinstance(step, int) or step < 0 or not np.isfinite(observation_time)
                or not np.isfinite(requested_at) or requested_at < observation_time - 1e-9):
            raise ValueError("Invalid request step or observation/request time")
        if self.last_request_step is not None and step <= self.last_request_step:
            raise RuntimeError("Request sequence must increase")
        if self.active_u is not None and not self.should_request(requested_at):
            raise RuntimeError("Execution queue has not reached prefetch threshold")
        prefix, rows = None, ()
        origin = float(observation_time)
        remaining = self.remaining_steps(requested_at)
        if self.active_u is not None and self.mode != "sync":
            # Keep the queue boundary fixed despite camera age or tick quantization.
            # The prefix keeps its absolute poses and is re-encoded later against the
            # REAL new anchor; the anchor is never replaced by a predicted state.
            # 队列边界固定，不受相机延迟或控制节拍量化影响；前缀保留绝对位姿，
            # 之后用新的真实锚点重新编码，绝不用预测状态替换锚点。
            start = self.selected_steps - self.execution["prefetch_remaining_steps"]
            origin = float(self.active_u.times[start] - self.first_offset)
            if self.prefix_steps:
                size = self.prefix_steps
                if start + size > self.selected_steps:
                    raise ValueError("Fixed prefix exceeds executable queue")
                if observation_time >= self.active_u.times[start + size - 1] - 1e-9:
                    raise PrefixWindowExpired("RTC prefix has expired before the fresh observation")
                prefix = self.active_u.poses[start:start + size].copy()
                rows = tuple(range(start + 1, start + size + 1))  # human 1-based / 人类可读的 1 起行号
        request = QueueRequest(step, float(observation_time), anchor, prefix, origin, requested_at, rows, remaining)
        self.pending = request
        self.last_request_step = step
        return request

    # ------------------------------------------------------------------ reply
    def accept(self, raw, request, received_at, *, held_verified=False):
        """Turn a raw model chunk into the new U/E and swap it into the queue.

        把模型输出转换为新的 U/E 并替换进执行队列：
          1. decode against the request anchor / 用请求锚点解码为绝对位姿
          2. verify + restore the RTC prefix / 校验并恢复 RTC 前缀
          3. place rows on the policy clock / 按 policy_hz 放到时间轴上
          4. drop rows already in the past / 丢弃已过期的行
          5. apply arm/gripper lead / 手臂与夹爪超前补偿
          6. blend with the previous E / 与上一段 E 平滑融合
        """
        if request is not self.pending:
            raise RuntimeError("Stale or mismatched inference result")
        if not np.isfinite(received_at) or received_at < request.requested_at:
            raise ValueError("Inference completion predates request")
        if self.mode == "sync" and not held_verified:
            raise RuntimeError("Sync mode requires verified held scene and robot state")
        try:
            raw = validate_pose(raw)
            if not len(raw) or len(raw) > self.model["horizon"]:
                raise ValueError("Invalid prediction length")
            count = min(len(raw), self.execution["n_action_steps"])
            prefetch = self.execution["prefetch_remaining_steps"]
            if self.mode != "sync" and count <= prefetch:
                raise ValueError("Short chunk has no executable span before prefetch")
            poses_u = decode(raw, request.anchor, self.model["protocol"])
            if request.prefix_u is not None:
                size = len(request.prefix_u)
                if len(raw) < size or not np.allclose(poses_u[:size], request.prefix_u, atol=2e-5, rtol=0):
                    raise ValueError("RTC model changed the fixed raw-U prefix")
                # Keep the exact old rows (no accumulated normalization round-off).
                # 写回精确的旧行，避免归一化往返带来的累计误差。
                poses_u[:size] = request.prefix_u
            origin = request.policy_origin_time
            # Only a verified held start / sync cycle may restart the playback clock.
            # The spatial FK anchor always stays on the recorded observation.
            # 只有经过静止校验的启动/同步周期才允许把播放时钟对齐到推理完成时刻；
            # 空间锚点始终保持为观测时刻的实测位姿。
            if held_verified and (self.mode == "sync" or self.active_u is None):
                origin = float(received_at)
            times = origin + self.first_offset + np.arange(len(raw)) * self.policy_dt
            u = TimedTrajectory(times, poses_u)
            earliest = float(received_at) + self.execution["handoff_margin_s"]
            arm_lead, grip_lead = self.postprocess["arm_delay_s"], self.postprocess["gripper_delay_s"]
            lead = max(arm_lead, grip_lead)
            indices = np.arange(count)  # rows [n_action_steps:] are never executed / 超出部分永不执行
            eligible = (times[:count] >= earliest - 1e-9) & (times[:count] + lead <= times[count - 1] + 1e-9)
            indices = indices[eligible]
            if not len(indices):
                raise ValueError("No future targets remain within selected execution window")
            e_times = times[indices]
            e_poses = poses_u[indices].copy()
            for j, t in enumerate(e_times):
                if arm_lead:
                    advanced = u.at(t + arm_lead)
                    for base in (0, 10):
                        e_poses[j, base:base + 9] = advanced[base:base + 9]
                if grip_lead:
                    e_poses[j, [9, 19]] = u.at(t + grip_lead)[[9, 19]]
            old = self.active_e
            fused = 0
            blend = self.postprocess["blend_s"]
            if self.mode != "sync" and old is not None and blend > 0:
                for j, t in enumerate(e_times):
                    if t >= e_times[0] + blend or t > old.times[-1] + 1e-9:
                        break
                    if t >= old.times[0] - 1e-9:
                        e_poses[j] = interpolate_pose(old.at(t), e_poses[j], float((t - e_times[0]) / blend))
                        fused += 1
            e = TimedTrajectory(e_times, e_poses)
            gap = 0.0 if old is None else max(0.0, float(e.times[0] - old.times[-1]))
            if self.mode != "sync" and gap > 1e-6 and self.execution["late_policy"] == "fail":
                raise RuntimeError(f"New execution trajectory has a {gap:.3f}s coverage gap")
            trigger_index = count - prefetch - 1 if self.mode != "sync" else count - 1
            due = float(times[trigger_index])
            result = QueuePrediction(
                u=u, e=e,
                expired_steps=int(np.sum(times[:count] < earliest - 1e-9)),
                compensated_steps=len(e.times) if lead else 0,
                fused_steps=fused, gap_s=gap,
                observation_time=request.observation_time, received_at=float(received_at),
                selected_steps=count, truncated_steps=len(raw) - count,
                policy_origin_time=origin, next_request_time=due,
            )
            self.previous_e = old if self.mode != "sync" else None
            self.active_u, self.active_e = u, e
            self.selected_steps, self.next_request_time = count, due
            return result
        finally:
            self.pending = None

    # ------------------------------------------------------------------ playback
    def target(self, timestamp):
        """EEF20 target for a control tick, or None when nothing covers it.

        返回某个控制节拍的目标位姿；无覆盖时返回 None（调用方据此保持）。
        Until the first row of the new E is due, the previous E keeps playing,
        so a reply that lands between rows does not cause a hold.
        新 E 的第一行到期前继续播放旧 E，推理结果落在两行之间不会导致停顿。
        """
        if self.active_e is not None and timestamp < self.active_e.times[0] - 1e-9:
            if self.previous_e is not None:
                try:
                    return self.previous_e.at(timestamp)
                except ValueError:
                    return None
            return None
        if self.active_e is None:
            return None
        try:
            return self.active_e.at(timestamp)
        except ValueError:
            return None
