"""QuicForge single-frame LVA bundle contract; no torch or hardware imports.
QuicForge 单帧 LVA 部署包契约；配置检查不加载 torch，也不连接硬件。
"""
from pathlib import Path

import numpy as np

from ..common.io import load_json, sha256
from .model import (CAMERA_ORDER, PHYSICAL_CONTRACT, PIPERX_EE_CONTRACT,
                    PIPERX_EE_JOINT_NAMES, PIPERX_EE_URDF_SHA256)

FORMAT = "quicforge_lva_v1"
TARGET = {"pose_source": "action", "anchor_pose_source": "state", "first_target_offset_steps": 1}
DIMS = {"emb_dim": 256, "time_dim": 32, "out_dim": 20, "n_att_layer": 4, "n_context": 50}
FILES = {"weights.pt", "normalization.json", "model_config.json", "inference_config.json",
         "provenance/training_config.json", "provenance/checkpoint.json", "provenance/conversion_meta.json"}
PREPROCESSING = {"resize": [480, 640], "crop": [16, 16, 448, 608], "mean": [0.485, 0.456, 0.406],
                 "std": [0.229, 0.224, 0.225], "color": "RGB"}
CAMERA_MAP = {"left_wrist": "left_wrist", "top": "head", "right_wrist": "right_wrist"}
BACKBONES = ("dinov3_vitl16", "dinov3_vitb16")


def backbone_variant(model):
    """Old exports omit the field and mean ViT-L. / 旧导出未写字段时仍为 ViT-L。"""
    value = model.get("backbone", "dinov3_vitl16")
    if value not in BACKBONES:
        raise ValueError(f"Unsupported QuicForge backbone: {value!r}")
    return value


def code_paths(model):
    """Pin the selected implementation, including local official B sources.
    锁定所选实现；ViT-B 额外锁定本地官方源码，不在运行时下载。
    """
    project = Path(__file__).resolve().parents[2]
    backend = project / "quicdeploy/policy/backends/quicforge_lva"
    files = [backend / name for name in ("__init__.py", "model.py", "decoder.py", "dinov3.py", "task_ids.py", "PROVENANCE.json")]
    files += [project / p for p in ("quicdeploy/policy/quicforge.py", "quicdeploy/policy/lva.py", "quicdeploy/config/quicforge.py")]
    if backbone_variant(model) == "dinov3_vitb16":
        files.append(project / "quicdeploy/policy/backends/temporal_lva.py")
        files += sorted((project / "third_party/dinov3").rglob("*.py"))
        files += [project / "third_party/dinov3/PROVENANCE.json", project / "third_party/dinov3/LICENSE.md"]
    return files


def require(value, expected, label):
    if value != expected:
        raise ValueError(f"QuicForge {label}: expected {expected!r}, got {value!r}")


def checked_path(root, relative):
    """Manifest members must stay inside the bundle. / 清单路径必须位于部署包内部。"""
    path = Path(relative)
    if path.is_absolute() or ".." in path.parts:
        raise ValueError(f"Invalid QuicForge bundle member: {relative}")
    result = (root / path).resolve()
    if not result.is_relative_to(root.resolve()) or not result.is_file():
        raise ValueError(f"Missing/outside QuicForge bundle member: {relative}")
    return result


def validate_inputs(model_config, inference, stats, training, checkpoint, conversion, *, allow_piperx_ee=False):
    """Reject temporal/multi-task/state-GT exports rather than guessing a contract.
    明确拒绝多帧、多任务、state-GT 等尚未支持的模型，不从权重形状猜训练语义。
    """
    require(model_config.get("type"), "LVADecoder", "model type")
    unknown = set(model_config) - {"type", *DIMS, "gradient_checkpointing", "preproc_file", "task_ids", "dino_weights", "backbone"}
    if unknown:
        raise ValueError(f"Unsupported QuicForge model arguments: {sorted(unknown)}")
    variant = backbone_variant(model_config)
    for label, owner in (("training", training), ("checkpoint", checkpoint)):
        require(backbone_variant(owner["model"]), variant, label + ".model.backbone")
    for k, v in DIMS.items():
        require(model_config.get(k), v, k)
        require(training["model"].get(k), v, f"training.model.{k}")
        require(checkpoint["model"].get(k), v, f"checkpoint.model.{k}")
    require(model_config.get("task_ids", []), [], "task_ids")
    require(model_config.get("dino_weights"), None, "external DINO weights")
    require(model_config.get("preproc_file"), None, "external preproc")
    require(model_config.get("gradient_checkpointing", False), False, "inference gradient checkpointing")
    require(training["model"].get("type"), "LVADecoder", "training model type")
    require(training["model"].get("task_ids", []), [], "training task_ids")
    require(training["data"].get("target_pose_source"), "action", "training target source")
    require(training["data"].get("horizon"), 50, "training horizon")
    require(training["data"].get("max_delay"), 10, "training RTC support")
    require(training["task"].get("unconditional_prob"), 1.0, "unconditional training")
    require(checkpoint.get("format_version"), 1, "checkpoint format")
    require(checkpoint.get("export", {}).get("weights"), "model", "weight variant")
    require(inference.get("predictor"), {"type": "LVAFlowPredictor", "num_steps": 10, "guidance_scale": 0.0},
            "inference predictor")
    require(inference.get("precision"), "fp32", "reference inference precision")
    require(stats.get("target_contract"), TARGET, "target contract")
    require(stats.get("horizon"), 50, "normalization horizon")
    require(stats.get("crop_size"), [448, 608], "crop")
    require(stats.get("camera_order"), ["left_wrist", "top", "right_wrist"], "camera order")
    require(stats.get("task_ids", []), [], "normalization task ids")
    for k in ("history_frames", "history_stride", "window_stride"):
        require(stats.get(k, 1), 1, k)
        require(training["data"].get(k, 1), 1, "training." + k)
    mean, std = np.asarray(stats.get("mu"), float), np.asarray(stats.get("std"), float)
    if mean.shape != (20,) or std.shape != (20,) or not np.isfinite([mean, std]).all() or np.any(std <= 0):
        raise ValueError("QuicForge requires paired finite mu/std vectors of length 20, std > 0")
    require(conversion.get("fps"), 30, "data fps")
    kinematics = conversion["kinematics"]
    labels = conversion["pose_labels"]
    physical = PHYSICAL_CONTRACT
    if allow_piperx_ee and (
        kinematics.get("urdf_sha256") == PIPERX_EE_URDF_SHA256
        or any(kinematics[side]["base_link"] == PIPERX_EE_CONTRACT["frame"] for side in ("left", "right"))
    ):
        physical = PIPERX_EE_CONTRACT
        require(kinematics.get("urdf_sha256"), PIPERX_EE_URDF_SHA256, "PiperX EE training URDF SHA-256")
        require(conversion.get("schema_version"), 2, "PiperX EE conversion schema")
        require(conversion.get("source_robot_type"), "bi_piper_native", "PiperX EE source robot")
        require(conversion.get("robot_profile"), "piperx", "PiperX EE conversion profile")
        require(kinematics.get("input_joint_angle_unit"), "degrees", "PiperX EE joint unit")
        require(labels.get("transform_direction"), "base_link_to_end_link", "PiperX EE pose direction")
        require(labels.get("gripper_source_feature"), "action", "PiperX EE gripper source")
        for i, side in enumerate(("left", "right")):
            start = 6 * i
            require(kinematics[side].get("joint_names"), PIPERX_EE_JOINT_NAMES[start:start + 6], f"{side} joint order")
        # The native recorder uses mm. This converter schema uses the URDF
        # end link directly with no post-FK offset. Explicit conflicts still fail.
        # 原生录制的夹爪单位为 mm。该转换格式直接取 URDF 末端，不额外叠加 FK 偏移。显式冲突仍拒绝。
        labels = {"gripper_unit": "millimeters", "tcp_offset_applied": False, **labels}
    for side in ("left", "right"):
        require(kinematics[side]["base_link"], physical["frame"], f"{side} frame")
        require(kinematics[side]["end_link"], physical["ends"][side], f"{side} endpoint")
    for key, value in {"position_unit": "meters", "gripper_unit": "millimeters",
                       "rotation_representation": "rotation_6d_first_two_columns", "tcp_offset_applied": False}.items():
        require(labels.get(key), value, key)
    require(conversion["camera_map"], {"observation.images.head": "top",
            "observation.images.left_wrist": "left_wrist", "observation.images.right_wrist": "right_wrist"}, "camera map")
    # Bind every training dataset to its own metadata and require identical
    # physical semantics. Keep the original single-dataset representation valid.
    # 每个训练数据集分别绑定转换元数据并核对物理语义一致，兼容原单数据集文件格式。
    datasets = training["data"]["datasets"]
    sources = conversion.get("dataset_sources", [conversion])
    if not isinstance(sources, list) or not sources or len(sources) != len(datasets):
        raise ValueError("Conversion metadata must cover every training dataset in order")
    require(conversion.get("source_dataset"), sources[0].get("source_dataset"), "primary dataset identity")
    identities = set()
    for dataset, metadata in zip(datasets, sources, strict=True):
        data_id = Path(dataset["label_file"]).parent.name
        if data_id in identities:
            raise ValueError("Duplicate training dataset identity: " + data_id)
        identities.add(data_id)
        require(data_id, Path(metadata["source_dataset"]).name + "_kvdb", "dataset identity")
        for key in ("schema_version", "source_robot_type", "robot_profile", "fps", "kinematics", "pose_labels", "camera_map"):
            require(metadata.get(key), conversion.get(key), "dataset physical contract." + key)
    return physical


def load_bundle(config, *, verify_hashes=True):
    require(config.get("artifact_format"), FORMAT, "card artifact format")
    root = Path(config["artifact_dir"]).resolve()
    manifest_path = checked_path(root, "deployment_manifest.json")
    manifest = load_json(manifest_path)
    require(manifest.get("schema_version"), 1, "bundle schema")
    require(manifest.get("format"), FORMAT, "bundle format")
    files = manifest.get("files", {})
    if set(files) != FILES:
        raise ValueError("QuicForge bundle file manifest is incomplete or has unknown members")
    paths = {name: checked_path(root, name) for name in files}
    if verify_hashes:
        for name, path in paths.items():
            if sha256(path) != files[name]:
                raise ValueError(f"QuicForge bundle artifact changed: {name}")
    model = load_json(paths["model_config.json"])
    inference = load_json(paths["inference_config.json"])
    stats = load_json(paths["normalization.json"])
    training = load_json(paths["provenance/training_config.json"])
    checkpoint = load_json(paths["provenance/checkpoint.json"])
    conversion = load_json(paths["provenance/conversion_meta.json"])
    physical = validate_inputs(model, inference, stats, training, checkpoint, conversion, allow_piperx_ee=True)
    expected_contract = {**physical, "horizon": 50, "action_dt_s": 1 / 30,
                         "first_target_offset_s": 1 / 30, "fixed_prefix_steps": 10,
                         "target_contract": TARGET, "history_frames": 1, "camera_map": CAMERA_MAP}
    require(manifest.get("contract"), expected_contract, "physical contract")
    for key in (*physical, "horizon", "action_dt_s", "first_target_offset_s", "fixed_prefix_steps", "target_contract"):
        require(config.get(key), expected_contract[key], "model card." + key)
    for key, expected in {"supervision": "action", "uses_state": False, "uses_language": False,
                          "output_domain": "physical"}.items():
        require(config.get(key), expected, "card." + key)
    require(config.get("cameras"), CAMERA_ORDER, "deployment cameras")
    require(config.get("preprocessing"), PREPROCESSING, "preprocessing")
    adapters = {"quicdeploy.policy.quicforge:QuicForgeLVAAdapter": "float32",
                "quicdeploy.policy.quicforge:QuicForgeLVAFP16Adapter": "float16"}
    if config.get("adapter") not in adapters:
        raise ValueError("Unsupported adapter for QuicForge bundle")
    require(config.get("weight_dtype"), adapters[config["adapter"]], "execution precision")
    require(config.get("weights_variant"), "model", "card weight variant")
    for key, expected in {"num_steps": 10, "guidance_scale": 0, "noise_std": 1}.items():
        require(config.get(key), expected, "card." + key)
    require(config.get("weights_sha256"), files["weights.pt"], "card weight identity")
    for key, name in (("weights", "weights.pt"), ("normalization", "normalization.json"),
                      ("export_metadata", "deployment_manifest.json")):
        require(str(Path(config[key]).resolve()), str((root / name).resolve()), "path." + key)
    for key in ("epoch", "global_step"):
        require(manifest["source"].get(key), checkpoint[key], "source." + key)
        card_key = "checkpoint_epoch" if key == "epoch" else key
        require(config.get(card_key), checkpoint[key], "card." + card_key)
    require(manifest["source"].get("input_weights_sha256"), files["weights.pt"], "source weights hash")
    return {"manifest": manifest, "model_config": model, "stats": stats, "paths": paths,
            "conversion": conversion}
