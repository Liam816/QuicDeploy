"""QuicForge single-frame LVA loader; shared preprocessing, noise and RTC execution.
QuicForge 单帧 LVA 加载器；复用已有预处理、确定性噪声、RTC 和 FP16 策略。
No QuicForge training package, optimizer or data loader is imported.
不导入完整 QuicForge 训练框架、优化器或数据加载器。
"""
from .lva import LVAAdapter, LVAHalfPrecisionMixin


class QuicForgeLVAAdapter(LVAAdapter):
    """FP32 reference for a validated QuicForge deployment bundle. / FP32 参考实现。"""

    def load(self, config):
        from ..config.quicforge import load_bundle, object_adaln_ids, validate_object_id

        # Recheck hashes inside the worker, before importing the frozen network.
        # 在推理子进程中复验哈希，再导入冻结网络，防止检查后文件被替换。
        bundle = load_bundle(config)
        expected_dtype = "float16" if self._fp16 else "float32"
        if config.get("weight_dtype") != expected_dtype:
            raise ValueError(f"QuicForge adapter requires {expected_dtype} execution")
        self._configure_runtime(config)
        ids = object_adaln_ids(bundle["model_config"])
        if ids:
            self._model_inference_kwargs["object_id"] = validate_object_id(config.get("object_id"), ids)
        elif "object_id" in config:
            raise ValueError("object_id requires a QuicForge Object AdaLN model")
        torch = self.torch
        from .backends.quicforge_lva import LVADecoder

        weights = torch.load(config["weights"], map_location="cpu", weights_only=True, mmap=True)
        if not isinstance(weights, dict) or not weights or not all(torch.is_tensor(v) for v in weights.values()):
            raise ValueError("QuicForge weights must be a plain state_dict")
        if ids:
            # The buffer binds the embedding rows to the exported ID order.
            # 用权重中的 ID 缓冲区核对 embedding 行与导出 ID 顺序的对应关系。
            table = weights.get("_object_ids")
            if table is None or table.dtype != torch.int32 or table.tolist() != ids:
                raise ValueError("QuicForge state_dict _object_ids disagrees with model_config.object_ids")
        for name, tensor in weights.items():
            if ids and name == "_object_ids":
                continue
            if tensor.dtype != torch.float32 or not torch.isfinite(tensor).all():
                raise ValueError("QuicForge reference parameter tensors must be finite FP32")
        kwargs = {k: v for k, v in bundle["model_config"].items() if k != "type"}
        with torch.device("meta"):
            model = LVADecoder(**kwargs)
        model.load_state_dict(weights, strict=True, assign=True)
        if any(t.is_meta for t in (*model.parameters(), *model.buffers())):
            raise ValueError("Incomplete QuicForge state_dict")
        self.model = model.to(config["device"]).eval().requires_grad_(False)
        self._set_normalization(bundle["stats"], mean_key="mu")


class QuicForgeLVAFP16Adapter(LVAHalfPrecisionMixin, QuicForgeLVAAdapter):
    """Same loader and execution, FP16 weights with FP32 LayerScale. / 相同加载器的 FP16 部署版。"""

    def _convert_half_precision(self):
        if self.model.backbone_variant == "dinov3_vitb16":
            from .backends.temporal_lva import half_vitb16
            half_vitb16(self.model)
        else:
            # Keep the existing ViT-L conversion byte-for-byte. / ViT-L 沿用原转换行为。
            super()._convert_half_precision()
