"""Offline QuicForge reference -> QuicDeploy FP32 -> FP16 validation (no hardware).
离线对比原生 QuicForge 参考、部署 FP32、部署 FP16；可选 IK/保护链路，不连接机械臂。
    python tools/validate_quicforge.py --fixtures <directory> --ik-check
"""
import argparse
import gc
import json
import sys
import time
from pathlib import Path

import numpy as np

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))

from quicdeploy.common.geometry import decode, pose_errors
from quicdeploy.config.resolve import resolve
from quicdeploy.planning.scheduler import QueueScheduler
from quicdeploy.policy.base import load_adapter


def metrics(actual, expected):
    errors = pose_errors(actual, expected)
    return {"max_abs_component": float(np.max(np.abs(actual - expected))),
            "position_mm": max(max(errors[s]["position_mm"]) for s in ("left", "right")),
            "rotation_deg": max(max(errors[s]["orientation_deg"]) for s in ("left", "right")),
            "gripper_mm": max(max(errors[s]["gripper_mm"]) for s in ("left", "right"))}


def within(value, limits):
    return all(value[k] <= limit for k, limit in limits.items())


def ik_check(config, sample, raw, output):
    from quicdeploy.kinematics.ik import Kinematics
    from quicdeploy.robot.guard import CommandGuard
    from quicdeploy.common.layout import JOINT_COLUMNS

    if "joint_state" not in sample:
        raise ValueError("--ik-check requires original joint_state in each recorded sample")
    ik = Kinematics(config["kinematics"], output / "ik_worker.log")
    try:
        measured = sample["joint_state"].astype(float)
        fk = ik.wrist(measured)
        # KVDB claws are action labels; compare arm poses, not measured claw width.
        # KVDB 夹爪是 action 标签；这里只比较实测关节 FK 的双臂位姿，不比较夹爪标签。
        columns = [i for i in range(20) if i not in (9, 19)]
        mismatch = float(np.max(np.abs(fk[columns] - sample["anchor"][columns])))
        if mismatch > 1e-5:
            raise ValueError(f"Recorded joints and FK anchor disagree: {mismatch}")
        guard = CommandGuard(config["guard"]); guard.reset(measured); ik.reset(measured)
        scheduler = QueueScheduler(config["model"], config["execution"], config["postprocess"])
        request = scheduler.prepare(0, 100.0, sample["anchor"], requested_at=100.0)
        accepted = scheduler.accept(raw, request, 100.2, held_verified=True)
        from quicdeploy.runtime.safety import verify_chunk
        verify_chunk(config["motion_limits"], accepted.u.poses, sample["anchor"])
        joint_range = np.asarray(config["guard"]["joint_limits_deg"])
        commands = []
        dt = 1 / config["execution"]["control_hz"]
        for tick in range(1, 67):
            target = scheduler.target(100.2 + tick * dt)
            if target is None:
                continue
            command, _ = ik.step(measured, target)
            command, _ = guard.apply(command, measured, dt)
            assert np.isfinite(command).all()
            q = command[JOINT_COLUMNS].reshape(2, 6)
            assert (q >= joint_range[:, 0]).all() and (q <= joint_range[:, 1]).all()
            measured = command  # Ideal software sink only. / 仅为理想的软件执行模型。
            commands.append(command.tolist())
        return {"status": "passed", "fk_max_abs_difference": mismatch,
                "software_commands_checked": len(commands), "physical_commands_sent": 0,
                "selected_policy_rows": accepted.selected_steps}
    finally:
        ik.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixtures", required=True, type=Path)
    parser.add_argument("--config", type=Path, default=PROJECT / "configs/runtime.yaml")
    parser.add_argument("--fp32-card", type=Path, default=PROJECT / "configs/models/quicforge_pick_kun_ep050_step43700_fp32.json")
    parser.add_argument("--fp16-card", type=Path, default=PROJECT / "configs/models/quicforge_pick_kun_ep050_step43700_fp16.json")
    parser.add_argument("--output", type=Path, default=PROJECT / "outputs/quicforge_validation")
    parser.add_argument("--ik-check", action="store_true")
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    provenance = json.loads((args.fixtures / "reference_provenance.json").read_text())
    paths = sorted(args.fixtures.glob("episode*.npz"))
    if not paths:
        raise ValueError("No recorded reference fixtures found")
    from quicdeploy.common.io import sha256
    fixture_hashes = {entry["file"]: entry["sha256"] for entry in provenance["samples"]}
    for path in paths:
        if fixture_hashes.get(path.name) != sha256(path):
            raise ValueError("Reference fixture changed: " + path.name)
    samples = {path.name: dict(np.load(path, allow_pickle=False)) for path in paths}
    report = {"physical_commands_sent": 0, "reference": provenance,
              "thresholds": {"fp32": {"position_mm": 0.05, "rotation_deg": 0.02, "gripper_mm": 0.05},
                             "fp16": {"position_mm": 1.0, "rotation_deg": 0.5, "gripper_mm": 1.0}},
              "cases": [], "status": "running"}
    baseline = {}
    import torch
    report["target_runtime"] = {"torch": torch.__version__, "numpy": np.__version__, "device": torch.cuda.get_device_name()}
    try:
        for precision, card in (("fp32", args.fp32_card), ("fp16", args.fp16_card)):
            config = resolve(args.config, model_card=card)
            if provenance["checkpoint_weights_sha256"] != config["model"]["weights_sha256"]:
                raise ValueError("Reference and deployed checkpoint weights differ")
            backend_provenance = json.loads((PROJECT / "quicdeploy/policy/backends/quicforge_lva/PROVENANCE.json").read_text())
            from quicdeploy.config.quicforge import backbone_variant
            bundle_config = json.loads((Path(config["model"]["artifact_dir"]) / "model_config.json").read_text())
            variant = backbone_variant(bundle_config)
            if provenance.get("backbone_variant", "dinov3_vitl16") != variant:
                raise ValueError("Reference and deployed backbone variants differ")
            sources = (backend_provenance["variants"][variant]["reference_files"] if variant == "dinov3_vitb16" else
                       {"models/lva/" + name: entry["original_sha256"] for name, entry in backend_provenance["files"].items()})
            for name, expected_hash in sources.items():
                if provenance["source_sha256"][name] != expected_hash:
                    raise ValueError("Reference and frozen source differ: " + name)
            adapter = load_adapter(dict(config["model"]))
            for name, sample in samples.items():
                observation = {"images": {c: sample["image_" + c] for c in config["model"]["cameras"]},
                               "eef_state": sample["anchor"], "timestamp": 0.0}
                image_difference = float(np.max(np.abs(adapter.preprocess(observation["images"]) - sample["reference_image"])))
                if image_difference > 1e-6:
                    raise ValueError(f"Preprocessing differs from QuicForge: {image_difference}")
                for mode, prefix in (("no_prefix", None), ("rtc", sample["prefix_absolute"])):
                    context = {"seed": 0, "episode": 0, "anchor_index": int(sample["frame"]), "prefix_absolute": prefix}
                    started = time.perf_counter()
                    raw = adapter.infer(observation, context)
                    inference_s = time.perf_counter() - started
                    np.save(args.output / f"{name}_{precision}_{mode}.npy", raw)
                    key = (name, mode)
                    expected = sample["reference_" + mode] if precision == "fp32" else baseline[key]
                    value = metrics(raw, expected)
                    case = {"sample": name, "precision": precision, "mode": mode,
                            "preprocess_max_abs_difference": image_difference, "inference_s": inference_s, **value,
                            "passed": within(value, report["thresholds"][precision])}
                    if prefix is not None:
                        delta = float(np.max(np.abs(decode(raw, observation["eef_state"], "eef20_relative")[:10] - prefix)))
                        case["absolute_prefix_max_abs_difference"] = delta
                        case["passed"] &= delta < 2e-5
                    if precision == "fp32":
                        baseline[key] = raw
                    if args.ik_check and precision == "fp16" and prefix is None:
                        case["ik_guard"] = ik_check(config, sample, raw, args.output)
                    report["cases"].append(case)
                    print(json.dumps(case), flush=True)
                    if not case["passed"]:
                        raise ValueError(f"Offline precision gate failed: {name}/{precision}/{mode}")
            del adapter
            gc.collect(); torch.cuda.empty_cache()
        report["status"] = "passed"
    except BaseException as exc:
        report["status"] = "failed"; report["error"] = str(exc)
        raise
    finally:
        (args.output / "report.json").write_text(json.dumps(report, indent=2) + "\n")
    print("PASS: QuicForge reference, deployment FP32 and FP16; no hardware commands")


if __name__ == "__main__":
    main()
