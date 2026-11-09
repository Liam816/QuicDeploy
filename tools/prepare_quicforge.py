"""Import a QuicForge LVA checkpoint into a minimal, hash-pinned deployment bundle.
把 QuicForge LVA 断点整理成精简部署包和模型卡；不修改原断点，不伪造旧 checkpoint 外层。
    python tools/prepare_quicforge.py --input <uploaded-run> --conversion-meta <conversion_meta.json>
"""
import argparse
import hashlib
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))

from quicdeploy.common.io import load_json, sha256
from quicdeploy.config.model import PHYSICAL_CONTRACT, compact_card, register
from quicdeploy.config.quicforge import CAMERA_MAP, FILES, FORMAT, PREPROCESSING, TARGET, require, validate_inputs


def import_source(input_dir, checkpoint_name, conversion_meta):
    """Read an original run or a verified flat download without rewriting either.
    读取原始训练目录或经过哈希校验的平铺下载目录，不修改上传原件。
    """
    source = Path(input_dir).resolve()
    flat = (source / "checkpoint_manifest.json").is_file()
    checkpoint = source if flat else source / "checkpoints" / checkpoint_name
    paths = {"weights": checkpoint / ("model.pt" if flat else "backend/model.pt"),
             "normalization": checkpoint / ("normalization.json" if flat else "assets/normalization.json"),
             "checkpoint": checkpoint / ("checkpoint_manifest.json" if flat else "manifest.json"),
             "training": source / "config.resolved.json", "conversion": Path(conversion_meta).resolve()}
    for path in paths.values():
        if not path.is_file():
            raise ValueError(f"Missing QuicForge import asset: {path}")
    provenance = {"kind": "quicforge_flat_checkpoint" if flat else "quicforge_checkpoint",
                  "directory": str(checkpoint)}
    if flat:
        transfer = load_json(source / "download_manifest.json")
        verified = set()
        for entry in transfer["files"]:
            name = Path(entry["name"])
            file = (source / name).resolve()
            if name.is_absolute() or ".." in name.parts or not file.is_relative_to(source) or file in verified:
                raise ValueError(f"Invalid/duplicate downloaded asset: {name}")
            if file.stat().st_size != entry["bytes"] or sha256(file) != entry["sha256"]:
                raise ValueError(f"Downloaded asset changed: {name}")
            verified.add(file)
        required = {*paths.values(), source / "model_config.json", source / "inference_config.json"}
        if not {p.resolve() for p in required} <= verified:
            raise ValueError("Flat download manifest must pin all import assets")
        provenance["transfer_manifest_sha256"] = sha256(source / "download_manifest.json")
    training, manifest = load_json(paths["training"]), load_json(paths["checkpoint"])
    if flat:
        require(manifest.get("name"), checkpoint_name, "flat checkpoint name")
        encoded = json.dumps(training, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
        require(hashlib.sha256(encoded.encode()).hexdigest(), manifest.get("config_hash"), "training config hash")
        provenance["config_hash"] = manifest["config_hash"]
    model_config = dict(manifest["model"])
    model_config.pop("weights", None)
    model_config.update(manifest["model_capabilities"]["inference_kwargs"])
    if flat:
        require(load_json(source / "model_config.json"), model_config, "flat exported model config")
        require(load_json(source / "inference_config.json"), manifest["inference"], "flat exported inference config")
    if model_config.get("type") == "TemporalLVADecoder" and "backbone_flash_attention" in model_config:
        value = model_config.pop("backbone_flash_attention")
        if type(value) is not bool:
            raise ValueError("backbone_flash_attention must be boolean")
        require(training["model"].get("backbone_flash_attention"), value, "training attention implementation")
        # MUSA's training acceleration changes no state_dict keys. Deployment uses
        # the existing frozen CUDA backbone, not the training-only flash patch.
        # MUSA 训练加速不改变权重键；部署复用已冻结的 CUDA 骨干，不导入训练专用补丁。
        provenance["training_model_options"] = {"backbone_flash_attention": value}
    return source, paths, training, manifest, model_config, provenance


def prepare(input_dir, checkpoint_name, conversion_meta, output, card_stem):
    source, paths, training, manifest, model_config, provenance = import_source(input_dir, checkpoint_name, conversion_meta)
    stats = load_json(paths["normalization"])
    conversion = load_json(paths["conversion"])
    inference = manifest["inference"]
    temporal = model_config.get("type") == "TemporalLVADecoder"
    bundle_format = FORMAT
    sampling = None
    if temporal:
        from quicdeploy.config.temporal import FORMAT as TEMPORAL_FORMAT, validate_temporal
        bundle_format = TEMPORAL_FORMAT
        sampling = validate_temporal(model_config, inference, stats, training, manifest, conversion)
        # The deployment owns a frozen official backbone; discard the training server path.
        model_config["backbone_repo"] = None
        runtime_path = PROJECT / "configs" / f"{card_stem}.yaml"
        if runtime_path.exists():
            raise FileExistsError(f"Temporal runtime already exists: {runtime_path}")
    else:
        validate_inputs(model_config, inference, stats, training, manifest, conversion)
    # COMPLETE is absent in the uploaded minimal copy; bind the raw weights by
    # independently checking its transfer digest, not a boolean "all_matched".
    # 上传精简副本可能不含 COMPLETE；读取并独立复算传输哈希，不信任 all_matched 布尔值。
    transfer_path = source / "transfer_verification.json"
    if transfer_path.is_file():
        for name, expected in load_json(transfer_path)["sha256"].items():
            file = (source / name).resolve()
            if not file.is_relative_to(source) or sha256(file) != expected:
                raise ValueError(f"Uploaded file differs from transfer manifest: {name}")
    for precision in ("fp32", "fp16"):
        if (PROJECT / "configs/models" / f"{card_stem}_{precision}.json").exists():
            raise FileExistsError(f"Model card already exists for {card_stem}_{precision}")
    if output.exists():
        raise FileExistsError(f"Refusing to overwrite deployment bundle: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".quicforge-import-", dir=output.parent) as tmp:
        bundle = Path(tmp) / "bundle"
        (bundle / "provenance").mkdir(parents=True)
        shutil.copyfile(paths["weights"], bundle / "weights.pt")
        shutil.copyfile(paths["normalization"], bundle / "normalization.json")
        for source_file, destination in ((paths["training"], "training_config.json"),
                                         (paths["checkpoint"], "checkpoint.json"),
                                         (paths["conversion"], "conversion_meta.json")):
            shutil.copyfile(source_file, bundle / "provenance" / destination)
        for name, value in (("model_config.json", model_config), ("inference_config.json", inference)):
            (bundle / name).write_text(json.dumps(value, indent=2) + "\n")
        hashes = {name: sha256(bundle / name) for name in sorted(FILES)}
        if hashes["weights.pt"] != sha256(paths["weights"]):
            raise ValueError("Weights changed while copying")
        deployment = {"schema_version": 1, "format": bundle_format, "files": hashes,
                      "source": {**provenance,
                                 "epoch": manifest["epoch"], "global_step": manifest["global_step"],
                                 "input_weights_sha256": hashes["weights.pt"]},
                      "contract": {**PHYSICAL_CONTRACT, "horizon": 50, "action_dt_s": 1 / 30,
                                   "first_target_offset_s": 1 / 30, "fixed_prefix_steps": 10,
                                   "target_contract": TARGET, "history_frames": 1, "camera_map": CAMERA_MAP}}
        if temporal:
            deployment["contract"].pop("history_frames")
            deployment["contract"]["temporal_sampling"] = sampling
        (bundle / "deployment_manifest.json").write_text(json.dumps(deployment, indent=2) + "\n")
        bundle.rename(output)
    backend = PROJECT / "quicdeploy/policy/backends/quicforge_lva"
    code = [backend / name for name in ("__init__.py", "model.py", "decoder.py", "dinov3.py", "task_ids.py", "PROVENANCE.json")]
    code.extend(PROJECT / p for p in ("quicdeploy/policy/quicforge.py", "quicdeploy/policy/lva.py", "quicdeploy/config/quicforge.py"))
    if temporal:
        from quicdeploy.config.temporal import code_paths
        code = code_paths()
    for precision in ("fp32", "fp16"):
        card_path = PROJECT / "configs/models" / f"{card_stem}_{precision}.json"
        if card_path.exists():
            raise FileExistsError(f"Refusing to overwrite model card: {card_path}")
        relative = lambda p: os.path.relpath(p, card_path.parent)
        model = {**PHYSICAL_CONTRACT, "id": f"{card_stem}_{precision}",
                 "name": f"QuicForge Pick Kun Once · Epoch {manifest['epoch']} · Step {manifest['global_step']} · {precision.upper()}",
                 "architecture": "DINOv3 + FM", "artifact_format": bundle_format,
                 "adapter": "quicdeploy.policy.quicforge:" + ("QuicForgeLVAAdapter" if precision == "fp32" else "QuicForgeLVAFP16Adapter"),
                 "artifact_dir": relative(output), "weights": relative(output / "weights.pt"),
                 "normalization": relative(output / "normalization.json"),
                 "export_metadata": relative(output / "deployment_manifest.json"),
                 "weights_sha256": hashes["weights.pt"], "checkpoint_epoch": manifest["epoch"],
                 "global_step": manifest["global_step"], "weights_variant": "model", "weight_dtype": "float32" if precision == "fp32" else "float16",
                 "layerscale_dtype": "float32", "horizon": 50, "action_dt_s": 1 / 30,
                 "first_target_offset_s": 1 / 30, "fixed_prefix_steps": 10, "supervision": "action",
                 "target_contract": TARGET, "cameras": ["left_wrist", "head", "right_wrist"],
                 "preprocessing": PREPROCESSING, "uses_state": False, "uses_language": False, "demo": False,
                 "output_domain": "physical", "timing_domain": "stored_recording_time", "dataset_split": "training_replay",
                 "num_steps": 10, "guidance_scale": 0, "noise_std": 1, "device": "cuda", "cpu_threads": 4,
                 "runtime_versions": {"torch": "2.11.0+cu128", "numpy": "2.2.6"},
                 "implementation_files": [relative(output / p) for p in sorted(FILES - {"weights.pt", "normalization.json"})]
                                         + [relative(p) for p in code]}
        if temporal:
            model.update(name=f"Temporal-LVA · Pick Kun Once · Epoch {manifest['epoch']} · Step {manifest['global_step']} · {precision.upper()}",
                         architecture="DINOv3 ViT-B + spatial queries + temporal queries + FM",
                         temporal_sampling=sampling,
                         adapter="quicdeploy.policy.temporal:" +
                                 ("TemporalLVAAdapter" if precision == "fp32" else "TemporalLVAFP16Adapter"))
        card_path.write_text(json.dumps(compact_card({"schema_version": 1, "model": model}), indent=2, ensure_ascii=False) + "\n")
        register(card_path, relative=True)
        print(card_path)
    if temporal:
        import yaml
        runtime = {"schema_version": 2, "extends": "defaults.yaml",
                   "model": f"models/{card_stem}_fp16.json",
                   "execution": {"mode": "async", "prefix_steps": 10, "n_action_steps": 40,
                                 "prefetch_remaining_steps": 23}}
        runtime_path.write_text(yaml.safe_dump(runtime, sort_keys=False, allow_unicode=True))
        print(runtime_path)
    return output


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, type=Path, help="original run or verified flat download / 原始训练目录或已校验平铺资产")
    parser.add_argument("--checkpoint", default="update_00043700")
    parser.add_argument("--conversion-meta", required=True, type=Path)
    parser.add_argument("--output", type=Path, default=PROJECT / "models/quicforge/pick_kun_once_e50_bs20")
    parser.add_argument("--card-stem", default="quicforge_pick_kun_ep050_step43700")
    args = parser.parse_args()
    prepare(args.input, args.checkpoint, args.conversion_meta, args.output.resolve(), args.card_stem)


if __name__ == "__main__":
    main()
