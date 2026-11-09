"""Validate real temporal cards, normalization, cache, RTC and native IK offline.
离线验证真实时序模型卡、归一化、缓存、RTC 与原生 IK，不连接相机或机械臂。
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

from quicdeploy.common.geometry import decode
from quicdeploy.common.io import sha256
from quicdeploy.config.resolve import resolve
from quicdeploy.policy.base import load_adapter
from tools.validate_quicforge import ik_check, metrics, within


def observation(images, anchor, current, session, sampling):
    count, stride = sampling["history_frames"], sampling["history_stride"]
    offsets = list(range(-(count - 1) * stride, 1, stride))
    # Reuse one recorded image with synthetic frame indices to exercise cache
    # lifecycle. This is not a chronological 16-frame dataset replay.
    # 用单张记录图像和合成帧号验证缓存生命周期，不将其称作真实时序数据回放。
    return {"images": images, "eef_state": anchor, "timestamp": current / sampling["source_fps"],
            "vision_history": {"session": session, "frame_index": current, "first_frame_index": 0,
                               "time_offsets": offsets,
                               "samples": [{"frame_index": current if current + offset < 0 else current + offset, "images": images}
                                           for offset in offsets]}}


def main():
    import torch

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--fp32-card", required=True, type=Path)
    parser.add_argument("--fp16-card", required=True, type=Path)
    parser.add_argument("--sample", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--ik-check", action="store_true")
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    with np.load(args.sample, allow_pickle=False) as data:
        sample = dict(data)
    report = {"status": "running", "physical_commands_sent": 0, "sample_sha256": sha256(args.sample),
              "input_scope": "recorded-image replay with synthetic temporal frame indices",
              "thresholds": {"position_mm": 1.0, "rotation_deg": 0.5, "gripper_mm": 1.0}, "cases": []}
    baseline = {}
    weight_hash = None
    try:
        for precision, card in (("fp32", args.fp32_card), ("fp16", args.fp16_card)):
            config = resolve(args.config, model_card=card)
            if weight_hash is None:
                weight_hash = config["model"]["weights_sha256"]
            assert config["model"]["weights_sha256"] == weight_hash
            sampling = config["model"]["temporal_sampling"]
            if "sampling" in report:
                assert sampling == report["sampling"], "Precision cards use different sampling"
            report["sampling"] = sampling
            adapter = load_adapter(dict(config["model"]))
            images = {c: sample["image_" + c] for c in config["model"]["cameras"]}
            count, stride = sampling["history_frames"], sampling["history_stride"]
            span = (count - 1) * stride
            gap_frame = span + stride + max(sampling["source_fps"], count * stride)
            cases = (("startup", 0, "first", None, 1), ("full", span, "first", None, count - 1),
                     ("reuse", span, "first", None, 0), ("rtc_shift", span + stride, "first", sample["prefix_absolute"], 1),
                     ("no_overlap", gap_frame, "first", None, count),
                     ("new_session", 0, "next", None, 1))
            values = {}
            for name, current, session, prefix, expected_encodings in cases:
                obs = observation(images, sample["anchor"], current, session, sampling)
                context = {"seed": 0, "episode": 0, "anchor_index": current, "prefix_absolute": prefix}
                started = time.monotonic()
                raw = adapter.infer(obs, context)
                elapsed = time.monotonic() - started
                assert raw.shape == (50, 20) and np.isfinite(raw).all()
                assert adapter.history_diagnostics["new_frame_encodings"] == expected_encodings
                if name == "rtc_shift":
                    assert min(adapter.tokens) == stride and len(adapter.tokens) == count
                if name == "reuse":
                    assert np.array_equal(raw, values["full"]), "Cache reuse changed predictions"
                if name == "new_session":
                    assert len(adapter.tokens) == 1 and np.array_equal(raw, values["startup"])
                result = {"precision": precision, "case": name, "inference_s": elapsed,
                          "shape": list(raw.shape), "cache": dict(adapter.history_diagnostics)}
                if prefix is not None:
                    error = float(np.max(abs(decode(raw, sample["anchor"], "eef20_relative")[:len(prefix)] - prefix)))
                    assert error < 2e-5
                    result["absolute_prefix_max_abs_error"] = error
                if precision == "fp32":
                    baseline[name] = raw
                else:
                    differences = metrics(raw, baseline[name])
                    assert within(differences, report["thresholds"]), (name, differences)
                    result["difference_from_fp32"] = differences
                if args.ik_check and precision == "fp16" and name in ("startup", "full"):
                    result["ik_guard"] = ik_check(config, sample, raw, args.output)
                values[name] = raw
                np.save(args.output / f"{precision}_{name}.npy", raw)
                result["passed"] = True
                report["cases"].append(result)
                print(json.dumps(result), flush=True)
            del adapter
            gc.collect()
            torch.cuda.empty_cache()
        report.update(status="passed", weights_sha256=weight_hash)
    except BaseException as exc:
        report.update(status="failed", error=str(exc))
        raise
    finally:
        (args.output / "deployment_validation.json").write_text(json.dumps(report, indent=2) + "\n")


if __name__ == "__main__":
    main()
