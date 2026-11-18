"""Replay LeRobot encoder trajectories through PiperX EE and dashboard Link6 IK.

离线回放 LeRobot 编码器轨迹，对比 PiperX EE 与 dashboard Link6 的 IK；不连接硬件。
"""
import argparse
import itertools
import json
import sys
import time
from contextlib import ExitStack, closing
from pathlib import Path

import numpy as np

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))

from quicdeploy.common.geometry import interpolate_pose, pose_errors
from quicdeploy.common.io import load_json, sha256
from quicdeploy.common.layout import ACTION_KEYS, JOINT_COLUMNS, SIDES
from quicdeploy.config.resolve import resolve
from quicdeploy.kinematics.ik import Kinematics
from quicdeploy.robot.guard import CommandGuard


def recordings(dataset, episodes):
    # PyArrow is used only for replay; deployment does not import it.
    # PyArrow 仅用于离线回放，部署运行不导入。
    import pyarrow.parquet as pq

    info = load_json(dataset / "meta/info.json")
    for key in ("observation.state", "action"):
        if tuple(info["features"][key]["names"]) != ACTION_KEYS:
            raise ValueError(f"{dataset}: {key} is not in native degree/mm joint order")
    fps = float(info["fps"])
    if not np.isfinite(fps) or fps <= 0:
        raise ValueError(f"{dataset}: invalid recording FPS")
    columns = ["episode_index", "frame_index", "observation.state", "action"]
    filters = [("episode_index", "in", episodes)] if episodes is not None else None
    table = pq.read_table(dataset / "data", columns=columns, filters=filters).sort_by(
        [(key, "ascending") for key in columns[:2]]
    )
    found = set()
    for episode, rows in itertools.groupby(table.to_pylist(), key=lambda row: row["episode_index"]):
        rows = list(rows)
        if [row["frame_index"] for row in rows] != list(range(len(rows))) or len(rows) < 2:
            raise ValueError(f"{dataset}: incomplete episode {episode}")
        found.add(episode)
        yield f"{dataset.name}:{episode}", fps, np.asarray(rows[0]["observation.state"]), np.asarray(
            [row["action"] for row in rows]
        )
    if not found or (episodes is not None and found != set(episodes)):
        raise ValueError(f"{dataset}: requested episodes are missing")


def replay(ik, reference, guard, initial, actions, fps):
    """Use each guarded command as the next feedback sample. / 放行指令作为下一拍的理想反馈。"""
    model_poses = np.asarray([ik.wrist(q) for q in actions])
    control_poses = np.asarray([reference.wrist(q) for q in actions])
    ik.reset(initial)
    guard.reset(initial)
    measured = initial.copy()
    actual, expected, elapsed, speeds, projection = [], [], [], [], []
    rejected = None
    ticks = int(np.floor((len(actions) - 1) / (fps * ik.dt) + 1e-9))
    for tick in range(1, ticks + 1):
        time_at_policy_rate = min(tick * ik.dt * fps, len(actions) - 1)
        i = min(int(time_at_policy_rate), len(actions) - 2)
        weight = time_at_policy_rate - i
        target = interpolate_pose(model_poses[i], model_poses[i + 1], weight)
        try:
            started = time.perf_counter()
            command, details = ik.step(measured, target)
            elapsed_ms = 1000 * (time.perf_counter() - started)
            command, _ = guard.apply(command, measured, ik.dt)
        except (RuntimeError, ValueError) as exc:
            rejected = {"tick": tick, "error": str(exc)}
            break
        elapsed.append(elapsed_ms)
        speeds.append(float(np.max(np.abs((measured - command)[JOINT_COLUMNS])) / ik.dt))
        measured = command
        actual.append(reference.wrist(measured))
        expected.append(interpolate_pose(control_poses[i], control_poses[i + 1], weight))
        if "label_projection_residual" in details:
            projection.append(details["label_projection_residual"])
    errors = pose_errors(actual, expected) if actual else {}
    return ticks, errors, elapsed, speeds, projection, rejected


def percentiles(values):
    if not len(values):
        return {}
    return dict(zip(("p50", "p95", "p99", "max"), np.percentile(values, [50, 95, 99, 100]).tolist(), strict=True))


def summarize(ik, reference, guard, inputs):
    result = {"episodes": 0, "requested_ticks": 0, "guarded_ticks": 0, "rejections": []}
    errors = {side: {key: [] for key in ("position_mm", "orientation_deg")} for side in SIDES}
    projection_errors = {side: {key: [] for key in errors[side]} for side in SIDES}
    time_ms, speed = [], []
    for label, fps, first, actions in inputs:
        ticks, pe, elapsed, speeds, projection, rejected = replay(ik, reference, guard, first, actions, fps)
        result["episodes"] += 1
        result["requested_ticks"] += ticks
        result["guarded_ticks"] += len(elapsed)
        if rejected:
            result["rejections"].append({"episode": label, **rejected})
        for side in pe:
            for key in errors[side]:
                errors[side][key].extend(pe[side][key])
                projection_errors[side][key].extend(row[side][key] for row in projection)
        time_ms.extend(elapsed)
        speed.extend(speeds)
    result["physical_link6_error"] = {s: {k: percentiles(v) for k, v in row.items()} for s, row in errors.items()}
    result["training_projection_residual"] = {
        s: {k: percentiles(v) for k, v in row.items()} for s, row in projection_errors.items() if row["position_mm"]
    }
    result["ik_step_ms"] = percentiles(time_ms)
    result["max_joint_command_speed_deg_s"] = max(speed, default=0.0)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--episodes", type=int, nargs="+", help="episode indices (all by default) / 默认回放全部轨迹")
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--dashboard-config", type=Path, default=PROJECT / "configs/runtime.yaml")
    parser.add_argument("--output", type=Path, required=True, help="JSON report path / JSON 报告路径")
    args = parser.parse_args()
    config, dashboard = resolve(args.config), resolve(args.dashboard_config)
    inputs = list(recordings(args.dataset, args.episodes))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with ExitStack() as stack:
        active = stack.enter_context(closing(Kinematics(config["kinematics"],
                                                        args.output.with_suffix(".candidate.log"))))
        reference = stack.enter_context(closing(Kinematics(dashboard["kinematics"],
                                                           args.output.with_suffix(".dashboard.log"))))
        if active.dt != reference.dt or reference.target_frames != ["left_Link6", "right_Link6"]:
            raise ValueError("Replay requires dashboard Link6 and matching control rates")
        guard = CommandGuard(config["guard"])
        report = {
            "dataset": str(args.dataset.resolve()), "config": str(args.config), "control_dt_s": active.dt,
            "model_id": config["model"]["id"], "weights_sha256": config["model"]["weights_sha256"],
            "training_urdf_sha256": sha256(config["kinematics"]["urdf"]),
            "active_ik_urdf_sha256": sha256(config["kinematics"]["native_urdf"]),
            "dashboard_urdf_sha256": sha256(dashboard["kinematics"]["native_urdf"]),
            "physical_commands_sent": 0,
        }
        report["active"] = summarize(active, reference, guard, inputs)
        report["dashboard"] = summarize(reference, reference, guard, inputs)
    args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(args.output)
    if report["active"]["rejections"] or report["dashboard"]["rejections"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
