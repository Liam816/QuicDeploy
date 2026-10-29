"""Pure unit tests (numpy/scipy only). / 纯单元测试（只依赖 numpy/scipy）。"""
import os

import numpy as np
import pytest
from scipy.spatial.transform import Rotation

from quicinfer.common.geometry import decode, interpolate_pose, relative_prefix, to6
from quicinfer.planning.scheduler import PrefixWindowExpired, QueueScheduler
from quicinfer.planning.trajectory import TimedTrajectory
from quicinfer.robot.guard import CommandGuard
from quicinfer.runtime.controls import TerminalControls

MODEL = {"first_target_offset_s": 1 / 30, "action_dt_s": 1 / 30, "horizon": 50, "protocol": "eef20_relative"}
EXECUTION = {"mode": "async", "prefix_steps": 10, "policy_hz": 30, "n_action_steps": 40,
             "prefetch_remaining_steps": 15, "handoff_margin_s": 0.011, "late_policy": "hold"}
POST = {"blend_s": 0.1, "arm_delay_s": 0.0, "gripper_delay_s": 0.0}


def pose(x=0.0, rot=None, grip=(30.0, 40.0)):
    r = np.eye(3) if rot is None else rot
    return np.r_[x, 0.1, 0.2, to6(r), grip[0], -x, 0.1, 0.2, to6(r), grip[1]]


def test_relative_prefix_inverts_decode():
    rng = np.random.default_rng(0)
    anchor = pose(0.3, Rotation.from_rotvec([0.1, 0.2, -0.3]).as_matrix())
    raw = np.stack([pose(x, Rotation.from_rotvec(rng.normal(0, 0.2, 3)).as_matrix()) for x in rng.normal(0, 0.05, 12)])
    assert np.allclose(relative_prefix(decode(raw, anchor, "eef20_relative"), anchor), raw, atol=1e-12)


def test_interpolation_endpoints_and_midpoint():
    a, b = pose(0.0), pose(0.2, Rotation.from_rotvec([0, 0, np.pi / 2]).as_matrix())
    assert np.array_equal(interpolate_pose(a, b, 0), a) and np.array_equal(interpolate_pose(a, b, 1), b)
    mid = TimedTrajectory([0.0, 1.0], np.stack([a, b])).at(0.5)
    assert np.allclose(mid, interpolate_pose(a, b, 0.5))
    assert np.isclose(mid[0], 0.1)


def chunk(anchor, prefix=None):
    raw = np.stack([pose(0.001 * i) for i in range(50)])
    if prefix is not None:
        raw[:len(prefix)] = relative_prefix(prefix, anchor)
    return raw


def test_rtc_queue_flow():
    s = QueueScheduler(MODEL, EXECUTION, POST)
    anchor = pose(0.0)
    assert s.should_request(0.0)
    first = s.prepare(0, 10.0, anchor, requested_at=10.0)
    assert first.prefix_u is None
    accepted = s.accept(chunk(anchor), first, 10.2, held_verified=True)
    # Held start re-bases the clock on the reply time. / 静止启动把时钟对齐到结果到达时刻。
    assert np.isclose(accepted.u.times[0], 10.2 + 1 / 30)
    assert accepted.selected_steps == 40 and accepted.truncated_steps == 10
    due = accepted.next_request_time
    assert not s.should_request(due - 0.01) and s.should_request(due)
    second = s.prepare(1, due - 0.02, anchor, requested_at=due)
    assert second.prefix_source_rows == tuple(range(26, 36))
    assert np.array_equal(second.prefix_u, accepted.u.poses[25:35])
    nxt = s.accept(chunk(anchor, second.prefix_u), second, due + 0.1)
    assert nxt.fused_steps > 0 and nxt.gap_s == 0.0
    assert s.target(due + 0.05) is not None  # old E keeps playing / 旧 E 继续播放


def test_rtc_prefix_must_survive_and_can_expire():
    s = QueueScheduler(MODEL, EXECUTION, POST)
    anchor = pose(0.0)
    accepted = s.accept(chunk(anchor), s.prepare(0, 0.0, anchor), 0.1, held_verified=True)
    request = s.prepare(1, accepted.next_request_time, anchor, requested_at=accepted.next_request_time)
    with pytest.raises(ValueError, match="fixed raw-U prefix"):
        s.accept(chunk(anchor), request, accepted.next_request_time + 0.1)
    late = accepted.u.times[35]
    with pytest.raises(PrefixWindowExpired):
        s.prepare(2, late, anchor, requested_at=late)


def guard_settings(**extra):
    return {"joint_limits_deg": [[-154, 154], [-7, 186], [-177, 7], [-97, 97], [-97, 97], [-186, 186]],
            "gripper_widths_mm": [70, 100], "gripper_speed_mm_s": None, "max_tracking_error_deg": None,
            "max_joint_speed_deg_s": 135, "gripper_feedback_tolerance_mm": 0.5,
            "gripper_binary_thresholds_mm": [40, 75], **extra}


def test_guard_limits_speed_and_grippers():
    measured = np.array([0, 40, -50, 40, 0, 0, 35, 0, 40, -50, 40, 0, 0, 50], float)
    guard = CommandGuard(guard_settings())
    guard.reset(measured)
    target = measured.copy()
    target[0] += 1.0
    target[[6, 13]] = [50, 60]
    command, info = guard.apply(target, measured, 0.02)
    assert command[0] == 1.0 and command[6] == 70 and command[13] == 0  # binarized / 二值化
    too_fast = command.copy()
    too_fast[1] += 3.0  # 150 deg/s > 135 / 超速
    with pytest.raises(ValueError, match="velocity"):
        guard.apply(too_fast, command, 0.02)
    outside = command.copy()
    outside[2] = 8.0
    with pytest.raises(ValueError, match="joint limit"):
        CommandGuard(guard_settings()).apply(outside, measured, 0.02)


def test_arrow_key_decoding():
    read, write = os.pipe()
    controls = TerminalControls()
    controls.fd = read
    os.write(write, b"x\x1b[D\x1b[Cq\x1bOD\x1b[")
    assert controls.poll() == ["home", "resume", "home"]
    os.write(write, b"C")
    assert controls.poll() == ["resume"]
    os.close(read)
    os.close(write)
