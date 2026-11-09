"""Offline numerical comparison with an independent QuicForge source snapshot.

No camera, CAN or robot is opened. A raw checkpoint verifies architecture and
cache arithmetic; deployment metadata must still be checked by prepare_quicforge.
"""
import argparse
import hashlib
import json
import sys
import time
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))


def main():
    import torch
    from quicdeploy.policy.backends.temporal_lva import TemporalLVADecoder
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--weights", required=True, type=Path)
    parser.add_argument("--reference-package", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--history-stride", type=int, default=8,
                        help="deployment source-frame spacing / 部署源帧间距")
    args = parser.parse_args()
    if args.history_stride < 1:
        parser.error("--history-stride must be positive")
    sys.path.insert(0, str(args.reference_package))
    from quicforge.models.temporal_lva.model import TemporalLVADecoder as Reference

    torch.set_num_threads(4)
    torch.backends.cuda.matmul.allow_tf32 = False
    torch.backends.cudnn.allow_tf32 = False
    repo = str(PROJECT / "third_party/dinov3")
    state = torch.load(args.weights, map_location="cpu", weights_only=True, mmap=True)
    with torch.device("meta"):
        reference = Reference(backbone_repo=repo)
        deployed = TemporalLVADecoder(backbone_repo=repo)
    reference.load_state_dict(state, strict=True, assign=True)
    # Separate tensors: later precision changes must not mutate the reference.
    deployed.load_state_dict(state, strict=True, assign=True)
    reference = reference.to("cuda").eval().requires_grad_(False)
    deployed = deployed.to("cuda").eval().requires_grad_(False)
    report = {"weights": str(args.weights.resolve()), "weight_keys": len(state),
              "weights_sha256": hashlib.file_digest(args.weights.open("rb"), "sha256").hexdigest(),
              "shape": [1, 16, 3, 448, 1824], "history_stride": args.history_stride,
              "time_offsets": list(range(-15 * args.history_stride, 1, args.history_stride)),
              "torch": torch.__version__, "gpu": torch.cuda.get_device_name(), "scope": "offline; no hardware"}
    torch.manual_seed(123)
    frames = torch.randn(1, 16, 3, 448, 1824, device="cuda")
    valid = torch.ones(1, 16, dtype=torch.bool, device="cuda")
    offsets = torch.tensor([report["time_offsets"]], dtype=torch.float32, device="cuda")
    explicit = {"frames": frames, "valid_mask": valid, "time_offsets": offsets}
    noise = torch.randn(1, 50, 20, device="cuda")
    prefix = torch.randn(1, 10, 20, device="cuda")
    torch.cuda.reset_peak_memory_stats()
    with torch.inference_mode():
        start = time.monotonic()
        slots = torch.stack([deployed._encode_frame(frames[:, i]) for i in range(16)], dim=1)
        torch.cuda.synchronize()
        report["encode_16_frames_s"] = time.monotonic() - start
        cached = {"frame_slots": slots, "valid_mask": valid, "time_offsets": offsets}
        expected_context = reference.encode_image(explicit)
        actual_context = deployed.encode_image(cached)
        report["context_max_abs_error"] = (expected_context - actual_context).abs().max().item()
        torch.testing.assert_close(actual_context, expected_context, rtol=1e-5, atol=1e-6)
        # Compare the unmodified training sampler against the cached path.
        expected = reference.inference(explicit, action_prefix=prefix, delay=10, num_steps=10,
                                       noise=noise, guidance_scale=0)
        start = time.monotonic()
        actual = deployed.inference(cached, action_prefix=prefix, delay=10, num_steps=10,
                                    noise=noise, guidance_scale=0)
        torch.cuda.synchronize()
        report["cached_fusion_and_sampler_s"] = time.monotonic() - start
        report["actions_max_abs_error"] = (expected - actual).abs().max().item()
        torch.testing.assert_close(actual, expected, rtol=1e-5, atol=1e-6)
        report["rtc_prefix_exact"] = torch.equal(actual[:, :10], prefix)
        assert report["rtc_prefix_exact"]
        # Cold start: repeated current tokens must equal repeated current images.
        current = frames[:, -1:]
        padded = {"frames": current.expand(-1, 16, -1, -1, -1), "valid_mask": valid, "time_offsets": offsets}
        padded_slots = {"frame_slots": slots[:, -1:].expand(-1, 16, -1, -1),
                        "valid_mask": valid, "time_offsets": offsets}
        target = reference.encode_image(padded)
        result = deployed.encode_image(padded_slots)
        report["startup_context_max_abs_error"] = (target - result).abs().max().item()
        torch.testing.assert_close(result, target, rtol=1e-5, atol=1e-6)
        preserved = {name: t.clone() for name, t in deployed.state_dict().items()
                     if name.endswith(".gamma") or name.endswith("rope_embed.periods")}
        deployed.half()
        for name, tensor in preserved.items():
            parent, attr = name.rsplit(".", 1)
            getattr(deployed.get_submodule(parent), attr).data = tensor
        with torch.autocast("cuda", dtype=torch.float16):
            # Warm kernels separately from the full-history measurement.
            deployed._encode_frame(frames[:, -1])
            torch.cuda.synchronize()
            start = time.monotonic()
            half_slots = torch.stack([deployed._encode_frame(frames[:, i]) for i in range(16)], dim=1)
            torch.cuda.synchronize()
            report["fp16_encode_16_frames_s"] = time.monotonic() - start
            start = time.monotonic()
            half = deployed.inference({"frame_slots": half_slots, "valid_mask": valid, "time_offsets": offsets},
                                      action_prefix=prefix, delay=10, num_steps=10, noise=noise, guidance_scale=0)
            torch.cuda.synchronize()
            report["fp16_cached_fusion_and_sampler_s"] = time.monotonic() - start
            assert torch.isfinite(half).all()
            assert torch.equal(half[:, :10], prefix.half())
            report["fp16_actions_max_abs_difference"] = (half.float() - expected).abs().max().item()
            report["fp16_actions_mean_abs_difference"] = (half.float() - expected).abs().mean().item()
    report["peak_allocated_mib"] = torch.cuda.max_memory_allocated() / 2**20
    report["passed"] = True
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
