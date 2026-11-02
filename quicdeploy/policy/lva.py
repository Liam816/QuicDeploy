"""LVA (DINOv3 + flow matching) adapters with clean-prefix RTC.

LVA（DINOv3 视觉骨干 + 流匹配解码器）推理适配器，支持“干净前缀”RTC。

Class hierarchy / 类层次:
    LVAAdapter               FP32 reference, CPU or CUDA / FP32 参考实现
     └─ LVAFP16Adapter       CUDA FP16 weights, FP32 LayerScale / FP16 权重 + FP32 LayerScale
    ActionGTContractMixin    checks the action-supervised target contract / 校验 action 监督契约
    LVAActionGTFP16Adapter   = mixin + FP16  (deployed / 实际部署使用)
    LVAActionGTFP32Adapter   = mixin + FP32  (precision reference / 精度对照)

The model architecture code is loaded from the checkpoint's own frozen source
directory (`source_dir`), never re-implemented here.
模型结构代码直接从权重配套的冻结源码目录 source_dir 加载，这里不重新实现。
"""
import importlib.util
import json
import sys
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import patch

import numpy as np

from ..common.geometry import relative_prefix, validate_pose
from .base import PolicyAdapter

ACTION_GT_CONTRACT = {"pose_source": "action", "anchor_pose_source": "state", "first_target_offset_steps": 1}


def load_source(path, name):
    """Import a python file by path under an explicit module name. / 按路径导入源码文件。"""
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class LVAAdapter(PolicyAdapter):
    _fp16 = False

    # ------------------------------------------------------------------ load
    def load(self, config):
        self._configure_runtime(config)
        state = self.torch.load(config["weights"], map_location="cpu", weights_only=True, mmap=True)
        cfg, weights = state["config"], state["model"]
        if (state["epoch"], state["global_step"], state["weights_variant"]) != (
            config["checkpoint_epoch"], config["global_step"], "model"
        ):
            raise ValueError("Checkpoint identity mismatch")
        if cfg.get("use_advantage") is not False or cfg["max_delay"] != 10:
            raise ValueError("Checkpoint training RTC/advantage contract mismatch")
        if any(int(cfg[k]) != v for k, v in {
            "emb_dim": 256, "time_dim": 32, "n_att_layer": 4, "n_context": 50, "out_dim": 20,
        }.items()):
            raise ValueError("Unexpected model architecture")

        self._build_legacy_network(cfg, weights)
        self._set_normalization(json.loads(Path(config["normalization"]).read_text()))

    def _configure_runtime(self, config):
        import torch

        self.config, self.torch = config, torch
        # Exact runtime pin: numerics were validated with these versions.
        # 严格锁定运行时版本：数值精度是在该版本组合下验证的。
        if config["runtime_versions"] != {"torch": torch.__version__, "numpy": np.__version__}:
            raise ValueError("Runtime differs from deployment preparation")
        if config["device"] not in ("cpu", "cuda"):
            raise ValueError("Explicit CPU or CUDA device required")
        if config["device"] == "cuda":
            if not torch.cuda.is_available():
                raise RuntimeError("CUDA unavailable; repair NVIDIA driver/library mismatch first. No live CPU fallback.")
            torch.cuda.init()
        torch.backends.cuda.matmul.allow_tf32 = False
        torch.backends.cudnn.allow_tf32 = False
        torch.set_num_threads(int(config.get("cpu_threads", 4)))
        if (config["fixed_prefix_steps"], config["horizon"], config["num_steps"],
                config["guidance_scale"], config["noise_std"], config["weights_variant"]) != (10, 50, 10, 0, 1, "model"):
            raise ValueError("Expected raw model / horizon50 / prefix10 / steps10 / guidance0 / noise1")

    def _build_legacy_network(self, cfg, weights):
        torch, config = self.torch, self.config
        # model.py does `import dinov3_vitl`; expose the frozen copy only while importing.
        # model.py 内部 import dinov3_vitl：导入期间临时挂上冻结版本，结束后还原。
        source = Path(config["source_dir"])
        dino = load_source(source / "dinov3_vitl.py", "_rtc_dino")
        previous = sys.modules.get("dinov3_vitl")
        try:
            sys.modules["dinov3_vitl"] = dino
            module = load_source(source / "model.py", "_rtc_lva")
        finally:
            if previous is None:
                sys.modules.pop("dinov3_vitl", None)
            else:
                sys.modules["dinov3_vitl"] = previous

        # The checkpoint already contains the whole backbone: build on the meta
        # device (no download / allocation), then strictly assign every tensor.
        # 权重文件已包含完整骨干：先在 meta 设备上构建（不下载、不分配），再严格加载全部参数。
        with torch.device("meta"), patch.object(dino, "load_weights_from_safetensors", lambda model, path: model):
            model = module.Decoder(
                **{k: int(cfg[k]) for k in ("emb_dim", "time_dim", "out_dim", "n_att_layer", "n_context")},
                num_advantage_conditions=weights["_advantage_embedding.weight"].shape[0],
            )
        model.load_state_dict(weights, strict=True, assign=True)
        if any(t.is_meta for t in list(model.parameters()) + list(model.buffers())):
            raise ValueError("Incomplete model state")
        self.model = model.to(config["device"]).eval().requires_grad_(False)

    def _set_normalization(self, stats, mean_key="mean"):
        """Paired 20D physical stats, kept float64. / 配套的 20 维物理统计，保持 float64。"""
        self.mean = np.asarray(stats[mean_key], dtype=np.float64)
        self.std = np.asarray(stats["std"], dtype=np.float64)
        if (self.mean.shape != (20,) or self.std.shape != (20,)
                or not np.isfinite([self.mean, self.std]).all() or np.any(self.std <= 0)):
            raise ValueError("Invalid paired normalization")

    # ------------------------------------------------------------------ inference
    def preprocess(self, images):
        """Training-identical crop + ImageNet normalization; views concatenated on width.
        与训练一致的中心裁剪 + ImageNet 归一化，三路相机沿宽度方向拼接，输出 [1,3,H,W]。
        """
        p = self.config["preprocessing"]
        views = []
        for camera in self.config["cameras"]:
            image = images[camera]
            if image.shape != (480, 640, 3) or image.dtype != np.uint8:
                raise ValueError(f"{camera}: expected recorded RGB uint8 480x640")
            top, left, height, width = p["crop"]
            crop = image[top:top + height, left:left + width].astype(np.float32) / 255
            if crop.shape != (448, 608, 3):
                raise ValueError("Invalid training center crop")
            crop = (crop - np.asarray(p["mean"], np.float32)) / np.asarray(p["std"], np.float32)
            views.append(crop)
        return np.ascontiguousarray(np.concatenate(views, axis=1).transpose(2, 0, 1)[None])

    def infer(self, observation, context):
        torch, c = self.torch, self.config
        image = torch.from_numpy(self.preprocess(observation["images"])).to(c["device"])
        # Deterministic CPU-generated noise, identical to offline evaluation.
        # 在 CPU 上用确定性种子生成噪声，与离线评测完全一致。
        seed = (int(context.get("seed", 0)) * 1000003
                + int(context.get("episode", 0)) * 100003
                + int(context["anchor_index"]))
        noise = torch.randn((1, 50, 20), generator=torch.Generator().manual_seed(seed)).to(c["device"])
        size = 0
        absolute = context.get("prefix_absolute")
        relative = prefix = None
        if absolute is not None:
            # RTC: re-encode the old absolute rows against the NEW anchor, normalize,
            # and place them as the clean (noise-free) start of the chunk.
            # RTC：用新锚点把旧绝对位姿重编码为相对量并归一化，作为“干净前缀”放在噪声开头。
            absolute = np.asarray(absolute)
            if absolute.ndim != 2 or absolute.shape[1] != 20 or not 1 <= len(absolute) <= c["fixed_prefix_steps"]:
                raise ValueError("RTC prefix must contain 1..10 old absolute EEF20 targets")
            size = len(absolute)
            relative = relative_prefix(absolute, observation["eef_state"])
            prefix = torch.as_tensor((relative - self.mean) / self.std, dtype=torch.float32, device=c["device"])[None]
            noise[:, :size] = prefix
        precision = torch.autocast("cuda", dtype=torch.float16) if self._fp16 else nullcontext()
        with torch.inference_mode(), precision:
            prediction = self.model.inference(
                image, action_prefix=prefix, delay=0 if prefix is None else size,
                num_steps=10, noise=noise, guidance_scale=0.0,
            )
        if prefix is not None:
            # The source casts the clean prefix to the parameter dtype each step;
            # verify it survived denoising bit-exactly.
            # 源码每步把前缀转换为参数精度；这里校验前缀在去噪过程中逐位保持不变。
            expected = prefix.to(dtype=prediction.dtype) if self._fp16 else prefix
            if not torch.equal(prediction[:, :size], expected):
                raise ValueError("RTC clean prefix changed during denoising")
        normalized = prediction[0].float().cpu().numpy()
        output = normalized * self.std + self.mean
        if output.shape != (50, 20) or not np.isfinite(output).all():
            raise ValueError("Invalid LVA prediction")
        if self._fp16:
            self.inference_diagnostics = {}
            if c.get("record_diagnostics", False):
                self.inference_diagnostics = {
                    "normalized_output": normalized, "normalization_mean": self.mean,
                    "normalization_std": self.std, "noise_seed": seed,
                }
            if relative is not None:
                # Restore the exact relative prefix (FP16 round-trip removed).
                # 写回精确的相对前缀，消除 FP16 往返误差。
                output[:size] = relative
            return validate_pose(output)
        return output


class LVAHalfPrecisionMixin:
    _fp16 = True

    def load(self, config):
        if (config.get("device") != "cuda"
                or config.get("weight_dtype") != "float16"
                or config.get("layerscale_dtype") != "float32"):
            raise ValueError("LVA FP16 requires CUDA, FP16 weights and FP32 LayerScale")
        super().load(config)
        self.model.half()
        # DINO LayerScale gammas overflow in FP16; keep them FP32.
        # DINO 的 LayerScale 系数在 FP16 下会溢出，保持 FP32。
        restored = 0
        for module in self.model.modules():
            if type(module).__name__ == "LayerScale":
                module.gamma.data = module.gamma.data.float()
                restored += 1
        if restored != 48:
            raise ValueError(f"Unexpected DINO LayerScale module count: {restored}")


class LVAFP16Adapter(LVAHalfPrecisionMixin, LVAAdapter):
    """Shared FP16 execution with the legacy checkpoint loader. / 旧权重的共享 FP16 执行策略。"""


def validate_action_contract(config, checkpoint, normalization):
    """Model, checkpoint and normalization must all be action-supervised.
    模型配置、权重与归一化参数必须同属 action 监督契约（锚点取实测 state）。
    """
    if config.get("supervision") != "action" or config.get("target_contract") != ACTION_GT_CONTRACT:
        raise ValueError("Action-GT profile requires action supervision and measured-state anchor")
    if checkpoint["config"].get("target_contract") != ACTION_GT_CONTRACT:
        raise ValueError("Checkpoint is not action-GT; refusing legacy state-GT weights")
    if normalization.get("target_contract") != ACTION_GT_CONTRACT:
        raise ValueError("Normalization belongs to a different target contract")
    if normalization.get("source_sha256") != config.get("preproc_sha256") or not config.get("preproc_sha256"):
        raise ValueError("Paired preproc identity mismatch")
    if config.get("first_target_offset_s") != config.get("action_dt_s"):
        raise ValueError("Action-GT first target must be one training step after observation")


class ActionGTContractMixin:
    def load(self, config):
        import torch

        checkpoint = torch.load(config["weights"], map_location="cpu", weights_only=True, mmap=True)
        normalization = json.loads(Path(config["normalization"]).read_text())
        validate_action_contract(config, checkpoint, normalization)
        del checkpoint
        super().load(config)


class LVAActionGTFP16Adapter(ActionGTContractMixin, LVAFP16Adapter):
    """FP16 weights with FP32 LayerScale; physical EEF20, measured FK anchor."""


class LVAActionGTFP32Adapter(ActionGTContractMixin, LVAAdapter):
    """Reference only, for same-input precision verification. / 仅用于同输入精度对照。"""
