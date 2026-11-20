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
from quicdeploy.config.model import PHYSICAL_CONTRACT, PIPERX_EE_CONTRACT, compact_card, register
from quicdeploy.config.quicforge import (CAMERA_MAP, FILES, FORMAT, PREPROCESSING, TARGET,
                                       backbone_variant, code_paths, require, validate_aggregate_sources,
                                       validate_inputs)


def import_source(input_dir, checkpoint_name, conversion_meta, *, transfer_manifest=None):
    """Read a run, flat checkpoint or verified official export without rewriting it.
    读取训练目录、平铺断点或已校验的官方导出包，不修改上传原件。
    """
    source = Path(input_dir).resolve()
    conversions = ([conversion_meta] if isinstance(conversion_meta, (str, Path)) else list(conversion_meta))
    if not conversions:
        raise ValueError("At least one conversion metadata source is required")
    conversions = [Path(p).resolve() for p in conversions]
    exported = (source / "weights.pt").is_file() and (source / "manifest.json").is_file()
    flat = not exported and (source / "checkpoint_manifest.json").is_file()
    portable = flat or exported
    checkpoint = source if portable else source / "checkpoints" / checkpoint_name
    paths = {"weights": checkpoint / ("weights.pt" if exported else "model.pt" if flat else "backend/model.pt"),
             "normalization": checkpoint / ("normalization.json" if flat else "assets/normalization.json"),
             "checkpoint": checkpoint / ("checkpoint_manifest.json" if portable else "manifest.json"),
             "training": source / "config.resolved.json", "conversion": conversions[0]}
    for path in [*paths.values(), *conversions]:
        if not path.is_file():
            raise ValueError(f"Missing QuicForge import asset: {path}")
    provenance = {"kind": "quicforge_export" if exported else "quicforge_flat_checkpoint" if flat else "quicforge_checkpoint",
                  "directory": str(checkpoint)}
    if portable:
        transfer_path = (Path(transfer_manifest).resolve() if transfer_manifest is not None else
                         source / ("download_sources.json" if exported else "download_manifest.json"))
        transfer = load_json(transfer_path)
        entries = ([{**info, "name": name} for name, info in transfer["files"].items()]
                   if exported else transfer["files"])
        verified = set()
        for entry in entries:
            name = Path(entry["name"])
            file = (source / name).resolve()
            if name.is_absolute() or ".." in name.parts or not file.is_relative_to(source) or file in verified:
                raise ValueError(f"Invalid/duplicate downloaded asset: {name}")
            if file.stat().st_size != entry["bytes"] or sha256(file) != entry["sha256"]:
                raise ValueError(f"Downloaded asset changed: {name}")
            verified.add(file)
        required = {*paths.values(), *conversions, source / "model_config.json", source / "inference_config.json"}
        if exported:
            required |= {source / "manifest.json", source / "assets/preproc.pt"}
        if not {p.resolve() for p in required} <= verified:
            raise ValueError("Flat download manifest must pin all import assets")
        provenance["transfer_manifest_sha256"] = sha256(transfer_path)
        provenance["transfer_manifest_kind"] = transfer.get("kind", "provided_manifest")
    training, manifest = load_json(paths["training"]), load_json(paths["checkpoint"])
    if portable:
        require(manifest.get("name"), checkpoint_name, "flat checkpoint name")
        encoded = json.dumps(training, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
        require(hashlib.sha256(encoded.encode()).hexdigest(), manifest.get("config_hash"), "training config hash")
        provenance["config_hash"] = manifest["config_hash"]
    if exported:
        # The export must identify this checkpoint and retain its original model
        # weights. A transfer success flag alone does not establish either fact.
        # 导出包必须绑定本断点及原始 model 权重，不能仅凭传输成功标记确认身份。
        export = load_json(source / "manifest.json")
        require(export.get("format_version"), 1, "export format")
        require(export.get("weights"), "weights.pt", "export weights path")
        require(export.get("weights_sha256"), sha256(paths["weights"]), "export weights hash")
        require(export.get("dtypes"), ["float32"], "export reference dtype")
        require(export.get("assets"), ["normalization.json", "preproc.pt"], "export assets")
        for key, expected in (("global_step", manifest["global_step"]), ("config_hash", manifest["config_hash"]),
                              ("weights", "model")):
            require(export.get("source", {}).get(key), expected, "export source." + key)
        provenance["export_manifest_sha256"] = sha256(source / "manifest.json")
    model_config = dict(manifest["model"])
    model_config.pop("weights", None)
    model_config.update(manifest["model_capabilities"]["inference_kwargs"])
    if portable:
        require(load_json(source / "model_config.json"), model_config, "flat exported model config")
        require(load_json(source / "inference_config.json"), manifest["inference"], "flat exported inference config")
    if model_config.get("type") in ("LVADecoder", "TemporalLVADecoder") and "backbone_flash_attention" in model_config:
        value = model_config.pop("backbone_flash_attention")
        if type(value) is not bool:
            raise ValueError("backbone_flash_attention must be boolean")
        require(training["model"].get("backbone_flash_attention"), value, "training attention implementation")
        # MUSA's training acceleration changes no state_dict keys. Deployment uses
        # the existing frozen CUDA backbone, not the training-only flash patch.
        # MUSA 训练加速不改变权重键；部署复用已冻结的 CUDA 骨干，不导入训练专用补丁。
        provenance["training_model_options"] = {"backbone_flash_attention": value}
    return source, paths, training, manifest, model_config, provenance


def prepare(input_dir, checkpoint_name, conversion_meta, output, card_stem, *, transfer_manifest=None, dry_run=False,
            card_precision=None, hardlink_weights=False):
    """Optional card_precision writes one unsuffixed card; legacy calls keep two.
    指定 card_precision 时只写同名的一张卡；未指定的旧调用仍生成原有两张精度卡。
    """
    if card_precision is not None and card_precision not in ("fp16", "fp32"):
        raise ValueError("card_precision must be fp16 or fp32")
    precisions = (card_precision,) if card_precision else ("fp32", "fp16")
    cards = {p: PROJECT / "configs/models" / (f"{card_stem}.json" if card_precision else f"{card_stem}_{p}.json")
             for p in precisions}
    conversions = ([conversion_meta] if isinstance(conversion_meta, (str, Path)) else list(conversion_meta))
    if not conversions:
        raise ValueError("At least one conversion metadata source is required")
    source, paths, training, manifest, model_config, provenance = import_source(
        input_dir, checkpoint_name, conversions, transfer_manifest=transfer_manifest)
    stats = load_json(paths["normalization"])
    metadata = [load_json(Path(p)) for p in conversions]
    conversion = dict(metadata[0])
    aggregated = "quicforge_sources" in conversion
    if aggregated:
        # A downloaded aggregate keeps every source in training order. Validate
        # the embedded identities before exposing the existing bundle contract.
        # 下载的汇总文件按训练顺序保留各源；先校验内嵌身份，再转换为既有部署契约。
        if len(metadata) != 1:
            raise ValueError("Use one aggregated conversion file or separate per-dataset files")
        metadata = validate_aggregate_sources(conversion, training["data"]["datasets"])
    if len(metadata) > 1 or aggregated:
        # Preserve every dataset's original metadata inside one pinned bundle
        # member; top-level physical fields retain the existing runtime API.
        # 在同一受哈希约束的文件中保留各数据集原始元数据，顶层物理字段兼容现有运行接口。
        conversion["dataset_sources"] = metadata
    inference = manifest["inference"]
    temporal = model_config.get("type") in {"TemporalLVADecoder", "CachedTemporalLVADecoder"}
    bundle_format = FORMAT
    sampling = None
    physical = PHYSICAL_CONTRACT
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
        physical = validate_inputs(model_config, inference, stats, training, manifest, conversion, allow_piperx_ee=True)
    # COMPLETE is absent in the uploaded minimal copy; bind the raw weights by
    # independently checking its transfer digest, not a boolean "all_matched".
    # 上传精简副本可能不含 COMPLETE；读取并独立复算传输哈希，不信任 all_matched 布尔值。
    transfer_path = source / "transfer_verification.json"
    if transfer_path.is_file():
        for name, expected in load_json(transfer_path)["sha256"].items():
            file = (source / name).resolve()
            if not file.is_relative_to(source) or sha256(file) != expected:
                raise ValueError(f"Uploaded file differs from transfer manifest: {name}")
    for path in cards.values():
        if path.exists():
            raise FileExistsError(f"Model card already exists: {path}")
    if output.exists():
        raise FileExistsError(f"Refusing to overwrite deployment bundle: {output}")
    if dry_run:
        inspection = {"model_type": model_config["type"], "epoch": manifest["epoch"],
                      "global_step": manifest["global_step"], "temporal_sampling": sampling}
        if physical == PIPERX_EE_CONTRACT:
            inspection["hardware"] = "hardware/piper_x/ee/hardware.yaml"
        return inspection
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".quicforge-import-", dir=output.parent) as tmp:
        bundle = Path(tmp) / "bundle"
        (bundle / "provenance").mkdir(parents=True)
        if hardlink_weights:
            os.link(paths["weights"], bundle / "weights.pt")
        else:
            shutil.copyfile(paths["weights"], bundle / "weights.pt")
        shutil.copyfile(paths["normalization"], bundle / "normalization.json")
        for source_file, destination in ((paths["training"], "training_config.json"),
                                         (paths["checkpoint"], "checkpoint.json")):
            shutil.copyfile(source_file, bundle / "provenance" / destination)
        if len(metadata) == 1 and not aggregated:
            shutil.copyfile(paths["conversion"], bundle / "provenance/conversion_meta.json")
        else:
            (bundle / "provenance/conversion_meta.json").write_text(json.dumps(conversion, indent=2) + "\n")
        for name, value in (("model_config.json", model_config), ("inference_config.json", inference)):
            (bundle / name).write_text(json.dumps(value, indent=2) + "\n")
        hashes = {name: sha256(bundle / name) for name in sorted(FILES)}
        if hashes["weights.pt"] != sha256(paths["weights"]):
            raise ValueError("Weights changed while copying")
        deployment = {"schema_version": 1, "format": bundle_format, "files": hashes,
                      "source": {**provenance,
                                 "epoch": manifest["epoch"], "global_step": manifest["global_step"],
                                 "input_weights_sha256": hashes["weights.pt"]},
                      "contract": {**physical, "horizon": 50, "action_dt_s": 1 / 30,
                                   "first_target_offset_s": 1 / 30, "fixed_prefix_steps": 10,
                                   "target_contract": TARGET, "history_frames": 1, "camera_map": CAMERA_MAP}}
        if temporal:
            deployment["contract"].pop("history_frames")
            deployment["contract"]["temporal_sampling"] = sampling
        (bundle / "deployment_manifest.json").write_text(json.dumps(deployment, indent=2) + "\n")
        bundle.rename(output)
    code = code_paths(model_config) if not temporal else []
    if temporal:
        from quicdeploy.config.temporal import code_paths as temporal_code_paths
        code = temporal_code_paths()
    for precision, card_path in cards.items():
        if card_path.exists():
            raise FileExistsError(f"Refusing to overwrite model card: {card_path}")
        relative = lambda p: os.path.relpath(p, card_path.parent)
        model = {**physical, "id": card_path.stem,
                 "name": f"{training.get('experiment', {}).get('name', card_stem)} · Epoch {manifest['epoch']} · Step {manifest['global_step']} · {precision.upper()}",
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
            variant = model_config.get("backbone", "dinov3_vitb16")
            model.update(name=f"{training.get('experiment', {}).get('name', card_stem)} · Epoch {manifest['epoch']} · Step {manifest['global_step']} · {precision.upper()}",
                         architecture=("DINOv3 ViT-L/16" if variant == "dinov3_vitl16" else "DINOv3 ViT-B")
                                      + " + spatial queries + temporal queries + FM",
                         temporal_sampling=sampling,
                         adapter="quicdeploy.policy.temporal:" +
                                 ("TemporalLVAAdapter" if precision == "fp32" else "TemporalLVAFP16Adapter"))
        else:
            model["architecture"] = ("DINOv3 ViT-B/16 + FM" if backbone_variant(model_config) == "dinov3_vitb16"
                                     else "DINOv3 ViT-L/16 + FM")
        card_path.write_text(json.dumps(compact_card({"schema_version": 1, "model": model}), indent=2, ensure_ascii=False) + "\n")
        register(card_path, relative=True)
        print(card_path)
    if temporal:
        import yaml
        selected = cards["fp16"] if "fp16" in cards else next(iter(cards.values()))
        runtime = {"schema_version": 2, "extends": "defaults.yaml",
                   "model": f"models/{selected.name}",
                   "execution": {"mode": "async", "prefix_steps": 10, "n_action_steps": 40,
                                 "prefetch_remaining_steps": 23}}
        runtime_path.write_text(yaml.safe_dump(runtime, sort_keys=False, allow_unicode=True))
        print(runtime_path)
    return output


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, type=Path, help="original run or verified flat download / 原始训练目录或已校验平铺资产")
    parser.add_argument("--checkpoint", default="update_00043700")
    parser.add_argument("--conversion-meta", required=True, type=Path, action="append",
                        help="repeat in training dataset order / 按训练数据集顺序重复指定")
    parser.add_argument("--output", type=Path, default=PROJECT / "models/quicforge/pick_kun_once_e50_bs20")
    parser.add_argument("--card-stem", default="quicforge_pick_kun_ep050_step43700")
    args = parser.parse_args()
    prepare(args.input, args.checkpoint, args.conversion_meta, args.output.resolve(), args.card_stem)


if __name__ == "__main__":
    main()
