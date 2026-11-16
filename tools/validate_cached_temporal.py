"""Offline CachedTemporal validation against unmodified training source; no hardware.

Full 16-frame synthetic inputs check numerical equivalence. Recorded RGB inputs
with synthetic source indices check the deployed adapter, cache and physical RTC.
Neither input is claimed to be a chronological task-success evaluation.
"""
import argparse
import gc
import hashlib
import importlib
import json
from pathlib import Path
import sys
import time
import types

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))


def reference_class():
    root = PROJECT / "tools/reference/quicforge_cached_temporal"
    proof = json.loads((root / "PROVENANCE.json").read_text())
    for name, expected in proof["files"].items():
        if hashlib.sha256((root / name).read_bytes()).hexdigest() != expected:
            raise ValueError("Independent reference source changed: " + name)
    for name, relative in (("quicforge", ""), ("quicforge.models", "models"),
                           ("quicforge.models.lva", "models/lva"),
                           ("quicforge.models.temporal_lva", "models/temporal_lva"),
                           ("quicforge.io", "io")):
        if name in sys.modules:
            raise RuntimeError("Reference requires a clean process: " + name)
        module = types.ModuleType(name)
        module.__path__ = [str(root / relative)]
        sys.modules[name] = module
    return importlib.import_module("quicforge.models.temporal_lva.frozen").CachedTemporalLVADecoder, proof


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--fp32-card", type=Path, required=True)
    parser.add_argument("--fp16-card", type=Path, required=True)
    parser.add_argument("--sample", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    import numpy as np
    import torch
    from quicdeploy.config.resolve import resolve
    from quicdeploy.config.temporal import load_bundle
    from quicdeploy.policy.base import load_adapter
    from quicdeploy.common.geometry import decode
    from quicdeploy.common.io import sha256
    from tools.validate_temporal_deployment import observation
    from tools.validate_quicforge import metrics, within

    args.output.mkdir(parents=True, exist_ok=True)
    config = resolve(args.config, model_card=args.fp32_card)
    bundle = load_bundle(config["model"])
    Reference, proof = reference_class()
    torch.set_num_threads(4)
    torch.backends.cuda.matmul.allow_tf32 = False
    torch.backends.cudnn.allow_tf32 = False
    report = {"status": "running", "hardware_connected": False, "physical_commands_sent": 0,
              "torch": torch.__version__, "gpu": torch.cuda.get_device_name(),
              "weights_sha256": config["model"]["weights_sha256"], "reference": proof,
              "input_scope": {"numerical": "16 distinct synthetic full-resolution frames",
                              "adapter": "recorded RGB views with synthetic temporal indices"},
              "sample_sha256": sha256(args.sample), "adapter_cases": []}
    try:
        weights = torch.load(config["model"]["weights"], map_location="cpu", weights_only=True, mmap=True)
        kwargs = {k: v for k, v in bundle["model_config"].items() if k != "type"}
        with torch.device("meta"):
            reference = Reference(**kwargs)
        reference.load_state_dict(weights, strict=True, assign=True)
        reference.to("cuda").eval().requires_grad_(False)
        report["weight_keys"] = len(weights)
        sampling = bundle["sampling"]
        count, stride = sampling["history_frames"], sampling["history_stride"]
        assert (count, stride) == (16, 8)
        frames = torch.randn((1, count, 3, 448, 1824), generator=torch.Generator().manual_seed(123)).cuda()
        offsets = torch.tensor([list(range(-(count - 1) * stride, 1, stride))], device="cuda", dtype=torch.float32)
        valid = torch.ones((1, count), device="cuda", dtype=torch.bool)
        explicit = {"frames": frames, "valid_mask": valid, "time_offsets": offsets}
        noise = torch.randn((1, 50, 20), generator=torch.Generator().manual_seed(234)).cuda()
        prefix = torch.randn((1, 10, 20), generator=torch.Generator().manual_seed(345)).cuda()
        report["input_shape"] = list(frames.shape)
        with torch.inference_mode():
            start = time.monotonic()
            expected_context = reference.encode_image(explicit).cpu()
            expected_no_prefix = reference.inference(explicit, noise=noise, delay=0,
                                                      num_steps=10, guidance_scale=0).cpu()
            expected_rtc = reference.inference(explicit, noise=noise, action_prefix=prefix, delay=10,
                                               num_steps=10, guidance_scale=0).cpu()
            padded = {**explicit, "frames": frames[:, -1:].expand(-1, count, -1, -1, -1)}
            expected_startup = reference.encode_image(padded).cpu()
            torch.cuda.synchronize()
            report["reference_s"] = time.monotonic() - start
        del reference
        gc.collect()
        torch.cuda.empty_cache()

        adapter = load_adapter(dict(config["model"]))
        torch.cuda.reset_peak_memory_stats()
        with torch.inference_mode():
            start = time.monotonic()
            slots = torch.stack([adapter.model._encode_frame(frames[:, i]) for i in range(count)], dim=1)
            torch.cuda.synchronize()
            report["encode_16_frames_s"] = time.monotonic() - start
            cached = {"frame_slots": slots, "valid_mask": valid, "time_offsets": offsets}
            actual_context = adapter.model.encode_image(cached).cpu()
            start = time.monotonic()
            no_prefix = adapter.model.inference(cached, noise=noise, delay=0, num_steps=10, guidance_scale=0).cpu()
            rtc = adapter.model.inference(cached, noise=noise, action_prefix=prefix, delay=10,
                                          num_steps=10, guidance_scale=0).cpu()
            torch.cuda.synchronize()
            report["cached_two_samplers_s"] = time.monotonic() - start
            startup = adapter.model.encode_image({**cached, "frame_slots": slots[:, -1:].expand(-1, count, -1, -1)}).cpu()
            pairs = {"context": (actual_context, expected_context), "no_prefix": (no_prefix, expected_no_prefix),
                     "rtc": (rtc, expected_rtc), "startup": (startup, expected_startup)}
            for name, (actual, expected) in pairs.items():
                report[name + "_max_abs_error"] = (actual - expected).abs().max().item()
                torch.testing.assert_close(actual, expected, rtol=1e-5, atol=1e-6)
            assert torch.equal(rtc[:, :10], prefix.cpu())
            report["rtc_prefix_exact"] = True
            report["peak_allocated_mib"] = torch.cuda.max_memory_allocated() / 2**20
        del adapter, frames, explicit, padded, slots, cached, weights
        gc.collect()
        torch.cuda.empty_cache()

        with np.load(args.sample, allow_pickle=False) as data:
            sample = dict(data)
        baseline = {}
        thresholds = {"position_mm": 1.0, "rotation_deg": 0.5, "gripper_mm": 1.0}
        report["mixed_precision_thresholds"] = thresholds
        for precision, card in (("fp32", args.fp32_card), ("fp16", args.fp16_card)):
            config = resolve(args.config, model_card=card)
            adapter = load_adapter(dict(config["model"]))
            images = {c: sample["image_" + c] for c in config["model"]["cameras"]}
            span = (count - 1) * stride
            cases = (("startup", 0, "first", None, 1), ("full", span, "first", None, count - 1),
                     ("reuse", span, "first", None, 0),
                     ("rtc_shift", span + stride, "first", sample["prefix_absolute"], 1),
                     ("new_session", 0, "next", None, 1))
            values = {}
            for name, current, session, absolute_prefix, expected_encodings in cases:
                obs = observation(images, sample["anchor"], current, session, sampling)
                started = time.monotonic()
                raw = adapter.infer(obs, {"seed": 0, "episode": 0, "anchor_index": current,
                                          "prefix_absolute": absolute_prefix})
                torch.cuda.synchronize()
                result = {"precision": precision, "case": name, "inference_s": time.monotonic() - started,
                          "cache": dict(adapter.history_diagnostics), "shape": list(raw.shape)}
                assert raw.shape == (50, 20) and np.isfinite(raw).all()
                assert adapter.history_diagnostics["new_frame_encodings"] == expected_encodings
                if name == "reuse":
                    assert np.array_equal(raw, values["full"])
                if name == "new_session":
                    assert np.array_equal(raw, values["startup"])
                if absolute_prefix is not None:
                    error = float(np.max(abs(decode(raw, sample["anchor"], "eef20_relative")[:len(absolute_prefix)] - absolute_prefix)))
                    assert error < 2e-5
                    result["absolute_prefix_max_abs_error"] = error
                if precision == "fp32":
                    baseline[name] = raw
                else:
                    result["difference_from_fp32"] = metrics(raw, baseline[name])
                    assert within(result["difference_from_fp32"], thresholds), result
                values[name] = raw
                np.save(args.output / f"{precision}_{name}.npy", raw)
                report["adapter_cases"].append(result)
                print(json.dumps(result), flush=True)
            del adapter
            gc.collect()
            torch.cuda.empty_cache()
        report["status"] = "passed"
    except BaseException as exc:
        report.update(status="failed", error=str(exc))
        raise
    finally:
        (args.output / "validation.json").write_text(json.dumps(report, indent=2) + "\n")
        print(json.dumps({k: v for k, v in report.items() if k not in ("reference", "adapter_cases")}), flush=True)


if __name__ == "__main__":
    main()
