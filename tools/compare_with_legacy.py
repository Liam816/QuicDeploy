"""Side-by-side regression against the legacy openarm deployment code (no robot motion).

与旧版 openarm 部署代码逐项对比的回归工具（不会让机械臂运动）。

    .venv/bin/python tools/compare_with_legacy.py \
        --legacy /home/ih/workspace/yihuigou/openarm_workspace_hp \
        --sample <legacy live run>/chunk_000003.npz [--skip-gpu] [--skip-ik]

Sections / 对比项:
    config     resolved runtime config vs legacy current.json profile / 解析后的配置
    scheduler  identical raw chunks through both queue schedulers / 调度器 U/E 轨迹与目标采样
    guard      random commands through both command guards / 指令保护
    kinematics FK + IK worker self-check + IK steps / 正逆运动学
    inference  same images (+RTC prefix) through both GPU adapters / GPU 推理结果
Only CAN-free code is imported: the IK worker is a pure solver and no robot driver is loaded.
只导入与 CAN 无关的代码：IK 子进程是纯求解器，不加载任何机器人驱动。
"""
import argparse
import sys
import tempfile
from pathlib import Path

import numpy as np

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))

FAILURES = []


def report(name, ok, detail=""):
    print(f"[{'PASS' if ok else 'FAIL'}] {name} {detail}", flush=True)
    if not ok:
        FAILURES.append(name)


def same(a, b, atol=0.0):
    a, b = np.asarray(a, dtype=float), np.asarray(b, dtype=float)
    return a.shape == b.shape and (np.array_equal(a, b) if atol == 0 else np.allclose(a, b, atol=atol, rtol=0))


# ---------------------------------------------------------------------- config
def compare_config(old_cfg, new_cfg):
    from quicdeploy.common.io import sha256

    for section in ("runtime", "observation", "execution", "postprocess", "guard", "hold",
                    "motion_limits", "timing", "hardware_identity"):
        report(f"config.{section}", old_cfg[section] == new_cfg[section])
    # ik.yaml, both URDFs and the IK extension were copied into the project:
    # compare file contents, not paths.
    # ik.yaml、两份 URDF 与 IK 扩展已迁入本项目：比较文件内容而不是路径。
    old_ik, new_ik = (dict(cfg["ik_parameters"], model=dict(cfg["ik_parameters"]["model"]))
                      for cfg in (old_cfg, new_cfg))
    old_urdf = sha256(old_ik["model"].pop("urdf_path"))
    new_urdf = sha256(Path(new_cfg["kinematics"]["yaml"]).parent / new_ik["model"].pop("urdf_path"))
    report("config.ik_parameters", old_urdf == new_urdf and old_ik == new_ik)
    old_kin, new_kin = dict(old_cfg["kinematics"]), dict(new_cfg["kinematics"])
    # ik.yaml content is compared above (its urdf_path is intentionally relative now).
    # ik.yaml 的内容已在上面比较（其 urdf_path 现在有意改为相对路径）。
    old_kin.pop("yaml"), new_kin.pop("yaml")
    files = ("urdf", "native_urdf", "extension")
    same_files = all(sha256(old_kin.pop(key)) == sha256(new_kin.pop(key)) for key in files)
    same_dir = new_kin.pop("extension_dir") == str(Path(new_cfg["kinematics"]["extension"]).parent)
    old_kin.pop("extension_dir")
    report("config.kinematics", same_files and same_dir and old_kin == new_kin)
    ignored = {"initial_pose_file", "log_dir", "quest_python"}  # relocated paths / 仅路径迁移
    old_robot = {k: v for k, v in old_cfg["robot"].items() if k not in ignored}
    new_robot = {k: v for k, v in new_cfg["robot"].items() if k not in ignored}
    report("config.robot", old_robot == new_robot)
    ignored = {"adapter", "implementation_files"}  # adapter code moved into quicdeploy / 适配器代码已迁入
    old_model = {k: v for k, v in old_cfg["model"].items() if k not in ignored}
    new_model = {k: v for k, v in new_cfg["model"].items() if k not in ignored}
    report("config.model", old_model == new_model)


# ---------------------------------------------------------------------- scheduler
def compare_scheduler(old_mod, new_mod, model, execution, postprocess, sample, label):
    from quicdeploy.common.geometry import relative_prefix

    rng = np.random.default_rng(7)
    old = old_mod.QueuePolicyScheduler(model, execution, postprocess)
    new = new_mod.QueueScheduler(model, execution, postprocess)
    anchor = sample["anchor"].copy()
    base_raw = sample["raw"].copy()
    now = 1000.0
    ok = True
    for step in range(8):
        obs_time = now - 0.03
        old_req = old.prepare(step, obs_time, anchor, requested_at=now)
        new_req = new.prepare(step, obs_time, anchor, requested_at=now)
        ok &= (old_req.policy_origin_time == new_req.policy_origin_time
               and old_req.prefix_source_rows == new_req.prefix_source_rows
               and old_req.remaining_at_request == new_req.remaining_at_request
               and ((old_req.prefix_u is None and new_req.prefix_u is None) or same(old_req.prefix_u, new_req.prefix_u)))
        raw = base_raw.copy()
        raw[:, [0, 1, 2, 10, 11, 12]] += rng.normal(0, 0.002, (len(raw), 6))
        if new_req.prefix_u is not None:
            raw[:len(new_req.prefix_u)] = relative_prefix(new_req.prefix_u, anchor)
        received = now + rng.uniform(0.05, 0.25)
        held = step == 0 or execution["mode"] == "sync"
        a = old.accept(raw, old_req, received, held_verified=held)
        b = new.accept(raw, new_req, received, held_verified=held)
        ok &= all(same(getattr(a, f).times, getattr(b, f).times) and same(getattr(a, f).poses, getattr(b, f).poses)
                  for f in ("u", "e"))
        ok &= (a.expired_steps, a.fused_steps, a.gap_s, a.next_request_time, a.compensated_steps) == (
            b.expired_steps, b.fused_steps, b.gap_s, b.next_request_time, b.compensated_steps)
        for t in np.linspace(received - 0.2, b.e.times[-1] + 0.1, 97):
            x, y = old.target(t), new.target(t)
            ok &= (x is None and y is None) or (x is not None and y is not None and same(x, y))
        now = max(b.next_request_time, received) + 0.001
        anchor = b.e.at(min(max(now, b.e.times[0]), b.e.times[-1]))
    report(f"scheduler.{label}", bool(ok), "(8 chunks, U/E rows, request fields, 97 target samples each)")


# ---------------------------------------------------------------------- guard
def compare_guard(old_cls, new_cls, settings):
    from quicdeploy.common.layout import JOINT_COLUMNS

    rng = np.random.default_rng(11)
    ok = True
    for variant in ({}, {"gripper_speed_mm_s": 80.0}, {"gripper_binary_thresholds_mm": None}):
        cfg = dict(settings, **variant)
        old, new = old_cls(cfg), new_cls(cfg)
        measured = np.zeros(14)
        measured[JOINT_COLUMNS] = rng.uniform(-5, 5, 12) + np.tile([0, 40, -35, 30, 0, 0], 2)
        measured[[6, 13]] = [35.0, 50.0]
        old.reset(measured)
        new.reset(measured)
        for _ in range(300):
            target = measured + rng.normal(0, 1.2, 14)
            target[[6, 13]] = rng.uniform(-5, 110, 2)
            outcomes = []
            for guard in (old, new):
                try:
                    outcomes.append(guard.apply(target, measured, 0.02))
                except ValueError as exc:
                    outcomes.append(str(exc))
            if isinstance(outcomes[0], str) or isinstance(outcomes[1], str):
                ok &= outcomes[0] == outcomes[1]
                old.reset(measured)
                new.reset(measured)
                continue
            ok &= same(outcomes[0][0], outcomes[1][0]) and outcomes[0][1] == outcomes[1][1]
            measured = outcomes[1][0]
    report("guard", bool(ok), "(900 commands incl. rejections, 3 settings variants)")


# ---------------------------------------------------------------------- kinematics
def compare_kinematics(old_piper, old_cli, new_cfg, legacy_cfg):
    from quicdeploy.common.layout import JOINT_COLUMNS
    from quicdeploy.kinematics.ik import Kinematics
    from quicdeploy.kinematics.selfcheck import check_kinematics

    with tempfile.TemporaryDirectory() as tmp:
        old_ik = old_piper.Kinematics(legacy_cfg["kinematics"], Path(tmp) / "old.log")
        new_ik = Kinematics(new_cfg["kinematics"], Path(tmp) / "new.log")
        try:
            rng = np.random.default_rng(3)
            ok = True
            for _ in range(200):
                joints = np.zeros(14)
                joints[JOINT_COLUMNS] = rng.uniform(-60, 60, 12)
                joints[[6, 13]] = rng.uniform(0, 70, 2)
                ok &= same(old_ik.wrist(joints), new_ik.wrist(joints))
            report("kinematics.fk", bool(ok), "(200 random joint sets)")
            a = old_cli.check_kinematics(old_ik, legacy_cfg["guard"])
            b = check_kinematics(new_ik, new_cfg["guard"])
            report("kinematics.selfcheck", a == b, f"(max fk diff {b['max_fk_position_difference_m']:.2e} m)")
            start = np.zeros(14)
            start[JOINT_COLUMNS] = np.rad2deg(new_ik.initial)
            start[[6, 13]] = [35.0, 50.0]
            goal = start.copy()
            goal[JOINT_COLUMNS] += 3
            target = new_ik.wrist(goal)
            old_ik.reset(start)
            new_ik.reset(start)
            ok, x, y = True, start.copy(), start.copy()
            for _ in range(50):
                x, _ = old_ik.step(x, target)
                y, _ = new_ik.step(y, target)
                ok &= same(x, y)
            report("kinematics.ik_steps", bool(ok), "(50 reference-mode IK steps)")
        finally:
            old_ik.close()
            new_ik.close()


# ---------------------------------------------------------------------- inference
def compare_inference(old_adapters, new_cfg, legacy_cfg, sample):
    import time

    from quicdeploy.policy.base import load_adapter

    images = {name: sample["image_" + name] for name in new_cfg["model"]["cameras"]}
    observation = {"images": images, "eef_state": sample["anchor"], "timestamp": 0.0}
    old = old_adapters.load_adapter(dict(legacy_cfg["model"]))
    new = load_adapter(dict(new_cfg["model"]))
    for label, prefix in (("no_prefix", None), ("rtc_prefix", sample["prefix_absolute"])):
        context = {"seed": 0, "episode": 0, "anchor_index": 3, "prefix_absolute": prefix,
                   "rng": np.random.default_rng(np.random.SeedSequence([0, 3]))}
        started = time.monotonic()
        a = old.infer(observation, dict(context))
        mid = time.monotonic()
        b = new.infer(observation, dict(context))
        done = time.monotonic()
        diff = float(np.max(np.abs(a - b)))
        report(f"inference.{label}", np.array_equal(a, b),
               f"(max |diff| {diff:.3e}; legacy {mid - started:.3f}s, quicdeploy {done - mid:.3f}s)")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--legacy", default="/home/ih/workspace/yihuigou/openarm_workspace_hp")
    parser.add_argument("--legacy-profile", default="examples/piper_deployment/unified/current.json")
    parser.add_argument("--config", default=str(PROJECT / "configs/runtime.yaml"))
    parser.add_argument("--sample", required=True, help="legacy chunk_*.npz with images, anchor, raw, prefix_absolute")
    parser.add_argument("--skip-gpu", action="store_true")
    parser.add_argument("--skip-ik", action="store_true")
    args = parser.parse_args()

    legacy = Path(args.legacy)
    sys.path.insert(0, str(legacy / "src"))
    from lerobot.deployment import queue_timeline, unified_config
    from lerobot.deployment import piper as old_piper

    from quicdeploy.config.resolve import resolve
    from quicdeploy.planning import scheduler as new_sched
    from quicdeploy.robot.guard import CommandGuard

    legacy_cfg = unified_config.resolve(legacy / args.legacy_profile)
    new_cfg = resolve(args.config)
    sample = dict(np.load(args.sample))
    compare_config(legacy_cfg, new_cfg)

    model, post = new_cfg["model"], new_cfg["postprocess"]
    variants = {
        "async_rtc": (new_cfg["execution"], post),
        "async_no_prefix": (dict(new_cfg["execution"], prefix_steps=0), post),
        "async_lead_comp": (new_cfg["execution"], dict(post, arm_delay_s=0.05, gripper_delay_s=0.03)),
        "sync": (dict(new_cfg["execution"], mode="sync", prefix_steps=0, prefetch_remaining_steps=0),
                 dict(post, blend_s=0.0)),
    }
    for label, (execution, postprocess) in variants.items():
        compare_scheduler(queue_timeline, new_sched, model, execution, postprocess, sample, label)
    compare_guard(old_piper.CommandGuard, CommandGuard, new_cfg["guard"])
    if not args.skip_ik:
        from lerobot.deployment import cli as old_cli
        compare_kinematics(old_piper, old_cli, new_cfg, legacy_cfg)
    if not args.skip_gpu:
        import adapters as old_adapters  # legacy scripts/offline_policy_eval, on sys.path via prepare_imports
        compare_inference(old_adapters, new_cfg, legacy_cfg, sample)
    print("ALL PASS" if not FAILURES else f"FAILED: {FAILURES}")
    sys.exit(1 if FAILURES else 0)


if __name__ == "__main__":
    main()
