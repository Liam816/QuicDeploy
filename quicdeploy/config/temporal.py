"""Temporal-LVA bundle contract, separate from the frozen single-frame contract."""
from copy import deepcopy
from pathlib import Path

from ..common.io import load_json, sha256
from .quicforge import FILES, CAMERA_MAP, PREPROCESSING, TARGET, checked_path, require, validate_inputs
from .model import PHYSICAL_CONTRACT

FORMAT = "quicforge_temporal_lva_v1"
TEMPORAL_DEFAULTS = {"frame_queries": 96, "temporal_queries": 96, "spatial_layers": 2,
                     "temporal_layers": 2, "encoder_heads": 8, "encoder_ffn_ratio": 4,
                     "time_encoding_scale": 1.0}


def validate_temporal(model, inference, stats, training, checkpoint, conversion):
    # Reuse all physical, RTC, dataset and normalization checks without relaxing
    # the original single-frame validator or changing old model identities.
    require(model.get("type"), "TemporalLVADecoder", "temporal model type")
    require(training["model"].get("type"), model["type"], "temporal training type")
    require(checkpoint["model"].get("type"), model["type"], "temporal checkpoint type")
    allowed = {"type", "emb_dim", "time_dim", "out_dim", "n_att_layer", "n_context", "task_ids",
               "gradient_checkpointing", "preproc_file", "backbone_repo", *TEMPORAL_DEFAULTS}
    if set(model) - allowed:
        raise ValueError(f"Unsupported temporal model arguments: {sorted(set(model) - allowed)}")
    for key, default in TEMPORAL_DEFAULTS.items():
        value = model.get(key, default)
        require(training["model"].get(key, default), value, "training." + key)
        require(checkpoint["model"].get(key, default), value, "checkpoint." + key)
    if model.get("frame_queries", 96) != model.get("temporal_queries", 96):
        raise ValueError("Temporal frame_queries must equal temporal_queries")
    sampling = {}
    for key in ("history_frames", "history_stride", "window_stride"):
        value = stats.get(key)
        if type(value) is not int or value < 1:
            raise ValueError(f"Temporal normalization requires a positive integer {key}")
        require(training["data"].get(key), value, "training." + key)
        sampling[key] = value
    sampling.update(source_fps=conversion["fps"], padding="current", history_time_units="video_frames",
                    anchor_alignment="history_stride")
    require(stats.get("history_time_units"), "video_frames", "history time units")
    require(stats.get("history_policy"), "complete", "training history policy")
    require(stats.get("history_first_anchor"), (sampling["history_frames"] - 1) * sampling["history_stride"],
            "history first anchor")
    m, s, t, ck = map(deepcopy, (model, stats, training, checkpoint))
    m["type"] = t["model"]["type"] = ck["model"]["type"] = "LVADecoder"
    for key in (*TEMPORAL_DEFAULTS, "backbone_repo"):
        m.pop(key, None)
    for key in ("history_frames", "history_stride", "window_stride"):
        s[key] = t["data"][key] = 1
    validate_inputs(m, inference, s, t, ck, conversion)
    return sampling


def code_paths():
    project = Path(__file__).resolve().parents[2]
    files = [project / p for p in (
        "quicdeploy/policy/temporal.py", "quicdeploy/policy/lva.py", "quicdeploy/config/temporal.py",
        "quicdeploy/config/quicforge.py", "quicdeploy/policy/backends/temporal_lva.py",
        "quicdeploy/runtime/observer.py", "quicdeploy/runtime/live.py", "quicdeploy/robot/piper/camera.py",
        "quicdeploy/policy/backends/quicforge_lva/decoder.py",
        "quicdeploy/policy/backends/quicforge_lva/dinov3.py",
        "quicdeploy/policy/backends/quicforge_lva/task_ids.py",
        "quicdeploy/policy/backends/quicforge_lva/__init__.py",
        "quicdeploy/policy/backends/quicforge_lva/model.py",
        "quicdeploy/policy/backends/quicforge_lva/PROVENANCE.json")]
    files += sorted((project / "third_party/dinov3").rglob("*.py"))
    files += [project / "third_party/dinov3/PROVENANCE.json", project / "third_party/dinov3/LICENSE.md"]
    return files


def deployment_sampling(config, training_sampling):
    """Override only frame spacing after validating the original training contract.
    原训练契约校验通过后，只允许显式覆盖部署帧间距，其他采样语义保持严格校验。
    """
    allow = config.get("allow_history_stride_override", False)
    if type(allow) is not bool:
        raise ValueError("allow_history_stride_override must be boolean")
    requested = config.get("temporal_sampling")
    if not isinstance(requested, dict):
        raise ValueError("Temporal card requires temporal_sampling")
    effective = deepcopy(training_sampling)
    if allow:
        stride = requested.get("history_stride")
        if type(stride) is not int or stride < 1:
            raise ValueError("Deployment history_stride must be a positive integer")
        effective["history_stride"] = stride
    require(requested, effective, "temporal card.temporal_sampling")
    return effective


def load_bundle(config, *, verify_hashes=True):
    require(config.get("artifact_format"), FORMAT, "temporal artifact format")
    root = Path(config["artifact_dir"]).resolve()
    manifest = load_json(checked_path(root, "deployment_manifest.json"))
    require(manifest.get("schema_version"), 1, "temporal bundle schema")
    require(manifest.get("format"), FORMAT, "temporal bundle format")
    if set(manifest.get("files", {})) != FILES:
        raise ValueError("Incomplete temporal bundle manifest")
    paths = {name: checked_path(root, name) for name in FILES}
    if verify_hashes:
        for name, path in paths.items():
            require(sha256(path), manifest["files"][name], "temporal artifact changed: " + name)
    model, inference, stats, training, checkpoint, conversion = [load_json(paths[name]) for name in (
        "model_config.json", "inference_config.json", "normalization.json", "provenance/training_config.json",
        "provenance/checkpoint.json", "provenance/conversion_meta.json")]
    sampling = validate_temporal(model, inference, stats, training, checkpoint, conversion)
    contract = {**PHYSICAL_CONTRACT, "horizon": 50, "action_dt_s": 1 / 30,
                "first_target_offset_s": 1 / 30, "fixed_prefix_steps": 10, "target_contract": TARGET,
                "camera_map": CAMERA_MAP, "temporal_sampling": sampling}
    require(manifest.get("contract"), contract, "temporal physical contract")
    for key in (*PHYSICAL_CONTRACT, "horizon", "action_dt_s", "first_target_offset_s", "fixed_prefix_steps",
                "target_contract"):
        require(config.get(key), contract[key], "temporal card." + key)
    # Keep bundle/normalization/training stride intact; the card is the explicit
    # deployment request. Observer and adapter consume the same effective stride.
    # 包清单、归一化和训练 stride 不改写；模型卡声明部署值，观测器与适配器使用同一有效间距。
    effective_sampling = deployment_sampling(config, sampling)
    for key, expected in {"supervision": "action", "uses_state": False, "uses_language": False,
                          "output_domain": "physical", "preprocessing": PREPROCESSING,
                          "cameras": ["left_wrist", "head", "right_wrist"], "weights_variant": "model",
                          "num_steps": 10, "guidance_scale": 0, "noise_std": 1}.items():
        require(config.get(key), expected, "temporal card." + key)
    adapters = {"quicdeploy.policy.temporal:TemporalLVAAdapter": "float32",
                "quicdeploy.policy.temporal:TemporalLVAFP16Adapter": "float16"}
    if config.get("adapter") not in adapters:
        raise ValueError("Unsupported temporal adapter")
    require(config.get("weight_dtype"), adapters[config["adapter"]], "temporal precision")
    require(config.get("weights_sha256"), manifest["files"]["weights.pt"], "temporal weights identity")
    for key, name in (("weights", "weights.pt"), ("normalization", "normalization.json"),
                      ("export_metadata", "deployment_manifest.json")):
        require(str(Path(config[key]).resolve()), str((root / name).resolve()), "temporal path." + key)
    for key, card_key in (("epoch", "checkpoint_epoch"), ("global_step", "global_step")):
        require(manifest["source"].get(key), checkpoint[key], "temporal source." + key)
        require(config.get(card_key), checkpoint[key], "temporal card." + card_key)
    require(manifest["source"].get("input_weights_sha256"), manifest["files"]["weights.pt"], "temporal source weights")
    return {"manifest": manifest, "paths": paths, "model_config": model, "stats": stats,
            "sampling": effective_sampling, "training_sampling": sampling, "conversion": conversion,
            "backbone_repo": str(Path(__file__).resolve().parents[2] / "third_party/dinov3")}
