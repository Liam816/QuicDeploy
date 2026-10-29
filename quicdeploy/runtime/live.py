"""Attended live deployment: model inference driving the real dual Piper-X arms.

值守式实机部署：模型推理驱动真实的双臂 Piper-X。运行即会使能双臂并回到初始位姿。

Pipeline per control tick (control_hz, default 50 Hz) / 每个控制节拍的流程:

    observer.sample()          joints + newest camera frames      读关节与最新相机帧
        │
    [request due?] ── yes ──> observer.capture() → scheduler.prepare() → worker.submit()
        │                     (dedicated tick, no motion command)  请求节拍：只发起推理不下发运动
        no
        │
    scheduler.target(t+dt)     EEF20 pose from execution trajectory E   从执行轨迹 E 取目标位姿
        │
    ik.step()                  differential IK → 14D joints              差分 IK 得到关节目标
        │
    guard.apply()              limits / speed / grippers                 指令保护
        │
    robot.send()               native MIT workers (hold last target)     下发到原生 MIT 子进程

Async replies are polled each tick and swapped into the queue by scheduler.accept().
异步推理结果每拍轮询一次，由 scheduler.accept() 换入执行队列。

Modes / 模式:
    sync    hold → infer → verify still → play the chunk → hold ...     停-推-播循环
    async   first chunk is a held inference, then prefetching inference   首块静止推理，之后异步预取
    rtc     async + clean fixed prefix from the old queue                 异步 + 旧队列固定前缀

Pauses (robot holds, queue discarded, RIGHT resumes from a fresh observation):
暂停情形（机械臂保持、丢弃队列、按 → 从新观测恢复）:
    ControlTimingError (late_policy=hold), SceneChangedError, LEFT key (rehome).
"""
import fcntl
import os
import sys
import time

import numpy as np

from ..kinematics.ik import Kinematics
from ..kinematics.selfcheck import check_kinematics
from ..planning.scheduler import PrefixWindowExpired, QueueScheduler
from ..policy.worker import InferenceWorker
from ..robot.guard import CommandGuard
from ..robot.piper.robot import PiperDualArm
from .controls import RehomeRequested, TerminalControls
from .observer import SynchronizedObserver
from .recorder import RunRecorder
from .safety import ControlTimingError, SceneChangedError, check_tick_budget, confirm_still, verify_chunk

# Shared with the teleop one-click tools: only one program may own the arms.
# 与遥操作一键启动工具共用的锁：同一时刻只允许一个程序控制机械臂。
ARM_LOCK_PATH = f"/tmp/piper-vr-oneclick-{os.getuid()}.lock"


def _context(step, prefix):
    """Deterministic per-request noise context. / 每个请求的确定性噪声上下文。"""
    return {
        "seed": 0, "episode": 0, "anchor_index": step,
        "rng": np.random.default_rng(np.random.SeedSequence([0, step])),
        "prefix_absolute": prefix,
    }


class LiveSession:
    def __init__(self, config, *, diagnostics=False, duration_s=0):
        if not sys.stdin.isatty():
            raise RuntimeError("Live deployment requires an attended terminal")
        if config["runtime"]["device"] != "cuda":
            raise RuntimeError("Live policy requires a CUDA-verified model; CPU is offline only")
        if not np.isfinite(duration_s) or duration_s < 0:
            raise ValueError("Invalid live duration")
        self.config = config
        self.exe = config["execution"]
        self.mode = self.exe["mode"]
        self.tick_dt = 1 / self.exe["control_hz"]
        self.lateness = self.exe["max_tick_lateness_s"]
        self.duration_s = duration_s
        self.diagnostics = diagnostics
        self.rec = RunRecorder(config, diagnostics=diagnostics)
        self.result = self.rec.result
        self.controls = TerminalControls()
        self.robot = self.ik = self.worker = None
        self.observer = self.scheduler = self.guard = None
        # Loop state / 控制环状态
        self.started = None
        self.next_step = 0
        self.pending_request = None      # outstanding QueueRequest / 未完成的推理请求
        self.pending_observation = None
        self.pending_since = None
        self.policy_origin = None        # None => next chunk starts with a held inference / 下一块需静止推理
        self.held = True                 # robot currently holding / 机械臂当前处于保持
        self.paused = False
        self.last_command = None         # last guarded command (sync hold check) / 最后一条放行指令
        self.last_reply = None
        self.measured = None

    # ================================================================== lifecycle
    def run(self):
        with open(ARM_LOCK_PATH, "a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            try:
                self._start()
                while self._time_left():
                    try:
                        self._tick()
                    except ControlTimingError as exc:
                        if self.exe["late_policy"] != "hold":
                            raise
                        self._on_timing_pause(exc)
                    except SceneChangedError as exc:
                        self._on_scene_changed(exc)
                    except RehomeRequested:
                        self._on_rehome()
                self.result["status"] = "paused" if self.paused else "passed"
            except BaseException as exc:
                if self.robot is not None:
                    try:
                        self.robot.hold()
                    except BaseException as hold_error:
                        self.result["hold_error"] = repr(hold_error)
                self.rec.write_event({"event": "deployment_stop", "time": time.monotonic(), "error": repr(exc)})
                self.result["status"] = "interrupted" if isinstance(exc, KeyboardInterrupt) else "failed"
                self.result["error"] = repr(exc)
                raise
            finally:
                self._cleanup()
        return self.result

    def _start(self):
        config = self.config
        # 1) IK subprocess + FK/IK self-check / IK 子进程与自检
        self.ik = Kinematics(config["kinematics"], self.rec.ik_log)
        self.result["kinematics"] = check_kinematics(self.ik, config["guard"])
        if not np.isclose(self.tick_dt, self.ik.dt):
            raise RuntimeError("Control period differs from IK integration period")
        # 2) Model process (loads weights on GPU) / 模型进程（加载权重到 GPU）
        self.worker = InferenceWorker(dict(config["model"], record_diagnostics=self.diagnostics))
        self.result["hardware_commands_scope"] = "policy send() calls; native homing/hold excluded"
        # 3) Robot: enable + smooth startup homing (REAL MOTION) / 机械臂：使能并平滑回初始位（真机运动）
        robot = PiperDualArm(config["robot"], rate_hz=self.exe["control_hz"])
        robot.connect()
        self.robot = robot
        self.result["startup_homing_completed"] = True
        robot.hold()
        self.controls.open()
        self.controls.discard()
        print("Deployment keys: LEFT = smooth return + pause; RIGHT = resume from fresh observation; Ctrl+C = stop",
              flush=True)
        self.guard = CommandGuard(config["guard"])
        self._new_queue()
        self.started = time.monotonic()

    def _cleanup(self):
        # Each resource is released even if an earlier one fails.
        # 每项资源独立释放，前一项失败不影响后续清理。
        def cleanup(name, operation):
            try:
                operation()
            except BaseException as error:
                self.result.setdefault("cleanup_errors", []).append({"resource": name, "error": repr(error)})
                if self.result["status"] in ("passed", "paused"):
                    self.result["status"] = "failed"

        cleanup("terminal", self.controls.close)
        if self.robot is not None:
            self.result["hardware_commands"] = self.robot.commands
            cleanup("robot_hold", self.robot.hold)
            cleanup("robot_close", self.robot.close)
        if self.worker is not None:
            cleanup("model_worker", self.worker.close)
        if self.ik is not None:
            cleanup("ik_worker", self.ik.close)
        self.rec.finish()

    # ================================================================== helpers
    def _time_left(self):
        return self.duration_s == 0 or time.monotonic() - self.started < self.duration_s

    def _new_queue(self):
        self.scheduler = QueueScheduler(self.config["model"], self.exe, self.config["postprocess"])
        self.observer = SynchronizedObserver(self.robot, self.ik, self.config)

    def _check_home(self):
        if "home" in self.controls.poll():
            raise RehomeRequested

    def _hold(self):
        self.robot.hold()
        self.held = True

    def _send_target(self, measured, target, target_time):
        """IK → guard → send, logging every attempt (including refusals).

        IK → 指令保护 → 下发；无论成功或被拒都记录一行日志。
        Any failure holds the robot first, then re-raises.
        任何失败都会先让机械臂保持，再抛出异常。
        """
        ik, guard = self.ik, self.guard
        reference = ik.reference_joints
        details = {"ik_reference_before": None if reference is None else reference.tolist(),
                   "ik_started_at": time.monotonic(), "send_attempted": False, "command_sent": False}
        command = None
        try:
            details["wrist_measured"] = ik.wrist(measured).tolist()
            requested, ik_details = ik.step(measured, target)
            details.update(ik_finished_at=time.monotonic(), ik_joint_target=requested.tolist(),
                           ik_diagnostics=ik_details, wrist_ik_target=ik.wrist(requested).tolist())
            command, interventions = guard.apply(requested, measured, self.tick_dt)
            details.update(interventions)
            details.update(send_started_at=time.monotonic(), send_attempted=True)
            accepted = self.robot.send(command)
            details.update(send_finished_at=time.monotonic(), command_sent=True)
            if not np.allclose(accepted, command, atol=1e-6):
                raise RuntimeError("Piper acknowledgement differs from guarded joint command")
            details["status"] = "sent"
        except BaseException as exc:
            details.update(status="send_failed" if details["send_attempted"] else "rejected", error=repr(exc))
            if hasattr(exc, "ik_diagnostics"):
                details["ik_diagnostics"] = exc.ik_diagnostics
            details.setdefault("ik_finished_at", time.monotonic())
            try:
                self.robot.hold()
            except BaseException as hold_error:
                details["hold_error"] = repr(hold_error)
            self._record_command(target_time, target, measured, command, details)
            raise
        self._record_command(target_time, target, measured, command, details)
        self.last_command = guard.previous.copy()

    def _record_command(self, target_time, target, measured, command, details):
        scheduler = self.scheduler
        trajectory = scheduler.active_e
        if target_time < trajectory.times[0] and scheduler.previous_e is not None:
            trajectory = scheduler.previous_e
        self.rec.record_command({
            "trace_schema_version": 2, "time": target_time,
            "prediction_step": self.rec.trajectory_steps.get(id(trajectory)),
            **self.observer.feedback_metadata,
            "wrist_target": target.tolist(), "joint_target": None if command is None else command.tolist(),
            "joint_measured": measured.tolist(), **details,
        })

    # ================================================================== one tick
    def _tick(self):
        if self.paused:
            keys = self.controls.poll()
            self.measured = self.observer.sample()
            if "home" in keys:
                raise RehomeRequested
            if "resume" not in keys:
                time.sleep(self.tick_dt)
                return
            self.paused = False
            print("Resuming policy from fresh observation; old queue and RTC prefix discarded.", flush=True)
        self._check_home()
        loop_start = time.monotonic()
        self.measured = self.observer.sample()
        sample_done = time.monotonic()
        if self.mode == "sync":
            self._sync_cycle()
        elif self.policy_origin is None:
            self._held_inference(bootstrap=True)
        else:
            self._async_tick(loop_start, sample_done)

    def _held_inference(self, *, bootstrap):
        """Infer while the robot holds, then verify robot and scene stayed still.

        机械臂静止时推理，推理结束后校验机械臂与场景都没有变化，再接受结果。
        Used for every sync chunk and for the first async/RTC chunk.
        用于 sync 的每一块，以及 async/RTC 的第一块。
        """
        observation, before = self.observer.capture()
        if bootstrap:
            self.policy_origin = observation["timestamp"]
        step = self.next_step
        # requested_at = now, not the (older) head-frame time: in sync mode the
        # threshold is the last played row, which the newest frame usually predates.
        # (Legacy passed no requested_at, so its 2nd sync chunk was usually rejected.)
        # requested_at 取当前时刻而非更早的头部帧时间：sync 模式的阈值是刚播完的最后一行，
        # 最新帧通常早于它（旧代码未传 requested_at，导致 sync 第二块通常被拒绝）。
        request = self.scheduler.prepare(step, observation["timestamp"], observation["eef_state"],
                                         requested_at=time.monotonic())
        self.pending_request = request
        self.worker.submit(observation, _context(step, None))
        reply = self.worker.wait(self.exe["max_inference_wait_s"], on_tick=self._check_home)
        self.last_reply = reply
        self.observer.sample()
        after_observation, after = self.observer.capture()
        held_command = None if bootstrap else self.last_command
        confirm_still(
            before, after, observation["images"], after_observation["images"], self.config["hold"],
            gripper_target_mm=None if held_command is None else held_command[[6, 13]],
            gripper_widths_mm=self.config["guard"]["gripper_widths_mm"],
            gripper_feedback_tolerance_mm=self.config["guard"].get("gripper_feedback_tolerance_mm", 0),
        )
        accepted = self.scheduler.accept(reply["raw"], request, time.monotonic(), held_verified=True)
        verify_chunk(self.config["motion_limits"], accepted.u.poses, request.anchor)
        self.result["prediction_events"].append({
            "step": step, "roundtrip_s": reply["roundtrip_s"],
            "expired_steps": accepted.expired_steps, "gap_s": accepted.gap_s,
        })
        self.rec.save_prediction(step, reply, accepted, request, observation, held_verified=True)
        self.pending_request = None
        self.next_step += 1
        self.ik.reset(after)
        self.guard.reset(after)
        return accepted

    def _sync_cycle(self):
        """hold → held inference → play the whole chunk → hold. / 停 → 静止推理 → 播放整块 → 停。"""
        self._hold()
        accepted = self._held_inference(bootstrap=False)
        time.sleep(max(0, accepted.e.times[0] - time.monotonic()))
        while self._time_left():
            self._check_home()
            deadline = time.monotonic() + self.tick_dt
            measured = self.observer.sample()
            target = self.scheduler.target(deadline)
            if target is None and time.monotonic() < accepted.e.times[-1]:
                deadline = float(accepted.e.times[-1])
                target = self.scheduler.target(deadline)
            if target is None:
                break
            self._send_target(measured, target, deadline)
            if time.monotonic() - deadline > self.lateness:
                raise ControlTimingError("Control deadline missed")
            time.sleep(max(0, deadline - time.monotonic()))
        self.robot.hold()

    def _async_tick(self, loop_start, sample_done):
        self.rec.check_writes()
        self._poll_reply()
        poll_done = time.monotonic()
        request_parts = None
        if self.pending_request is None and self.scheduler.should_request(time.monotonic()):
            request_parts = self._submit_request(poll_done)
        request_done = time.monotonic()
        if request_parts is not None:
            # A request tick is dedicated: the native MIT worker keeps the last
            # guarded target while we copy images / build the prefix; no IK/send
            # is stacked onto the same tick.
            # 请求节拍专用：原生 MIT 子进程继续保持上一条目标，本拍只做图像拷贝与前缀构造，不叠加 IK/下发。
            elapsed = request_done - loop_start
            self.rec.append("request_tick_events", {"step": self.pending_request.step, "elapsed_ms": 1000 * elapsed})
            check_tick_budget(elapsed, self.tick_dt, self.lateness, "Policy request tick exceeded latency budget",
                              request_parts)
            time.sleep(max(0, loop_start + self.tick_dt - time.monotonic()))
            return
        target_time = time.monotonic() + self.tick_dt
        target = self.scheduler.target(target_time)
        target_done = time.monotonic()
        if target is None:
            if not self.held:
                self.robot.hold()
                self.result["hold_events"].append({"time": time.monotonic(), "reason": "no covered target"})
            self.held = True
        else:
            if self.held:
                # Resume IK/guard from the measured state after any hold.
                # 从保持状态恢复运动时，以实测关节重置 IK 与指令保护。
                self.ik.reset(self.measured)
                self.guard.reset(self.measured)
            self.held = False
            self._send_target(self.measured, target, target_time)
        loop_done = time.monotonic()
        if loop_done - loop_start > self.tick_dt + self.lateness:
            parts = {
                "sample_ms": 1000 * (sample_done - loop_start),
                "poll_ms": 1000 * (poll_done - sample_done),
                "request_ms": 1000 * (request_done - poll_done),
                "target_ms": 1000 * (target_done - request_done),
                "command_ms": 1000 * (loop_done - target_done),
            }
            self.result["tick_overrun"] = parts
            check_tick_budget(loop_done - loop_start, self.tick_dt, self.lateness,
                              "Control loop exceeded tick latency budget", parts)
        time.sleep(max(0, loop_start + self.tick_dt - time.monotonic()))

    def _poll_reply(self):
        """Swap a finished async prediction into the queue. / 把完成的异步预测换入队列。"""
        if self.pending_request is None:
            return
        if time.monotonic() - self.pending_since > self.exe["max_inference_wait_s"]:
            raise TimeoutError("Asynchronous model inference exceeded timeout")
        reply = self.worker.poll()
        if reply is None:
            return
        request = self.pending_request
        accepted = self.scheduler.accept(reply["raw"], request, time.monotonic())
        verify_chunk(self.config["motion_limits"], accepted.u.poses, request.anchor)
        self.result["prediction_events"].append({
            "step": request.step, "roundtrip_s": reply["roundtrip_s"],
            "expired_steps": accepted.expired_steps, "gap_s": accepted.gap_s,
            "fused_steps": accepted.fused_steps,
        })
        self.rec.save_prediction(request.step, reply, accepted, request, self.pending_observation)
        self.pending_request = self.pending_since = None

    def _submit_request(self, poll_done):
        """Capture, build the (RTC) request and submit without blocking.
        采集观测、构造（RTC）请求并非阻塞提交。
        """
        step = self.next_step
        observation, _ = self.observer.capture()
        capture_done = time.monotonic()
        try:
            request = self.scheduler.prepare(step, observation["timestamp"], observation["eef_state"],
                                             requested_at=time.monotonic())
        except PrefixWindowExpired as miss:
            # Never fabricate a prefix: stop, drop the old queue and restart from
            # an ordinary unconditioned chunk on a fresh observation.
            # 绝不伪造前缀：先保持、丢弃旧队列，再用新观测发起无前缀的普通请求。
            self._hold()
            self.scheduler.restart_rtc()
            observation, _ = self.observer.capture()
            request = self.scheduler.prepare(step, observation["timestamp"], observation["eef_state"],
                                             requested_at=time.monotonic())
            self.rec.append("rtc_restart_events", {"step": step, "reason": str(miss)})
        self.rec.append("queue_request_events", {
            "step": step, "requested_at": request.requested_at,
            "observation_time": request.observation_time,
            "remaining_steps": request.remaining_at_request,
            "prefix_source_rows": request.prefix_source_rows,
            "policy_origin_time": request.policy_origin_time,
        })
        prepare_done = time.monotonic()
        self.pending_request, self.pending_observation = request, observation
        self.pending_since = time.monotonic()
        self.worker.submit(observation, _context(step, request.prefix_u))
        submit_done = time.monotonic()
        self.next_step += 1
        return {
            "capture_ms": 1000 * (capture_done - poll_done),
            "prepare_ms": 1000 * (prepare_done - capture_done),
            "submit_ms": 1000 * (submit_done - prepare_done),
        }

    # ================================================================== pauses
    def _discard_pending(self, *, wait_reply):
        """Drop the outstanding request; an old GPU result is never accepted.
        丢弃未完成请求；旧的 GPU 结果永远不会被接受。
        """
        if self.pending_request is not None:
            if wait_reply:
                self.worker.wait(self.exe["max_inference_wait_s"])
            self.next_step = max(self.next_step, self.pending_request.step + 1)
        self.pending_request = self.pending_since = self.pending_observation = None

    def _reset_paused(self, *, clear_last_command=True):
        """Fresh queue + observer, IK/guard re-seeded from measured joints, paused.
        新建队列与观测器，用实测关节重置 IK/指令保护，并进入暂停。
        """
        self._new_queue()
        self.measured = self.observer.sample()
        self.ik.reset(self.measured)
        self.guard.reset(self.measured)
        if clear_last_command:
            self.last_command = None
        self.policy_origin = None
        self.paused = True
        self.controls.discard()

    def _on_timing_pause(self, exc):
        # No catch-up commands: hold, discard the queue, await manual resume.
        # 不补发指令：先保持，丢弃队列，等待人工恢复。
        self._hold()
        event = {"event": "control_timing_pause", "time": time.monotonic(), "reason": str(exc),
                 "discarded_request_step": None if self.pending_request is None else self.pending_request.step}
        self.rec.append("timing_pause_events", event)
        self.result["hold_events"].append(event)
        self.rec.write_event(event)
        self._discard_pending(wait_reply=True)
        self._reset_paused()
        print(f"Control timing overrun; holding and paused: {exc}. "
              "Press RIGHT to resume from fresh observation; LEFT = home; Ctrl+C = stop.", flush=True)

    def _on_scene_changed(self, exc):
        # The held reply was already consumed; it is discarded with the queue.
        # 静止推理的结果已被读取，连同队列一起丢弃。
        self._hold()
        event = {
            "event": "scene_changed_pause", "time": time.monotonic(), "reason": str(exc),
            "discarded_request_step": None if self.pending_request is None else self.pending_request.step,
            "roundtrip_s": None if self.last_reply is None else self.last_reply.get("roundtrip_s"),
        }
        self.rec.append("scene_change_events", event)
        self.result["hold_events"].append(event)
        self.rec.write_event(event)
        self._discard_pending(wait_reply=False)
        self._reset_paused(clear_last_command=False)
        print(f"Scene changed; prediction discarded and policy paused: {exc}. "
              "Keep the scene stable, then press RIGHT to infer from a fresh observation; "
              "LEFT = return to initial pose; Ctrl+C = exit.", flush=True)

    def _on_rehome(self):
        self._hold()
        event = {"requested_at": time.monotonic(), "status": "homing",
                 "discarded_request_step": None if self.pending_request is None else self.pending_request.step}
        self.rec.append("home_events", event)
        print("LEFT: holding policy, smoothly returning both arms to the configured initial pose...", flush=True)
        if not self.robot.rehome():
            raise RuntimeError("Return to initial pose was cancelled")
        self._discard_pending(wait_reply=True)
        self._reset_paused()
        event.update(status="paused_at_initial", completed_at=time.monotonic())
        print("At initial pose and paused. Press RIGHT to resume; Ctrl+C to exit.", flush=True)
