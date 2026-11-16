"""Temporal-LVA deployment: source-indexed spatial-token cache, shared RTC sampler."""
from collections import OrderedDict
from contextlib import nullcontext

from .lva import LVAAdapter


class TemporalLVAAdapter(LVAAdapter):
    def load(self, config):
        from ..config.temporal import load_bundle
        from .backends.temporal_lva import TemporalLVADecoder, half_vitb16

        bundle = load_bundle(config)
        self._configure_runtime(config)
        torch = self.torch
        kwargs = {k: v for k, v in bundle["model_config"].items() if k != "type"}
        kwargs["backbone_repo"] = bundle["backbone_repo"]
        weights = torch.load(config["weights"], map_location="cpu", weights_only=True, mmap=True)
        if not isinstance(weights, dict) or not weights or not all(torch.is_tensor(v) for v in weights.values()):
            raise ValueError("Temporal-LVA weights must be a plain state_dict")
        for value in weights.values():
            if value.is_floating_point() and (value.dtype != torch.float32 or not torch.isfinite(value).all()):
                raise ValueError("Temporal-LVA reference weights must be finite FP32")
        with torch.device("meta"):
            model = TemporalLVADecoder(**kwargs)
        model.load_state_dict(weights, strict=True, assign=True)
        if any(t.is_meta for t in (*model.parameters(), *model.buffers())):
            raise ValueError("Incomplete Temporal-LVA state_dict")
        self.model = model.to(config["device"]).eval().requires_grad_(False)
        self._set_normalization(bundle["stats"], mean_key="mu")
        self.sampling = bundle["sampling"]
        if self._fp16:
            if config["device"] != "cuda" or config.get("layerscale_dtype") != "float32":
                raise ValueError("Temporal FP16 requires CUDA and FP32 LayerScale")
            half_vitb16(self.model)
        self.reset()

    def reset(self):
        self.tokens = OrderedDict()
        self.session = None
        self.last_frame_index = None

    def _image_input(self, observation):
        """Lazily encode only uncached observations, before relative time is added.

        The observer transports exact source-index samples, independently of
        predict frequency. Missing startup slots use THIS request's current
        frame, with nominal negative offsets and valid=True (repeat padding).
        """
        torch = self.torch
        history = observation["vision_history"]
        if self.session != history["session"]:
            self.reset()
            self.session = history["session"]
        current = history["frame_index"]
        if self.last_frame_index is not None and current < self.last_frame_index:
            raise ValueError("Temporal source frame index moved backwards; reset the session")
        count, stride = self.sampling["history_frames"], self.sampling["history_stride"]
        expected = list(range(-(count - 1) * stride, 1, stride))
        if history["time_offsets"] != expected or len(history["samples"]) != count:
            raise ValueError("Temporal observations do not match the training sampling contract")
        slots, encoded = [], 0
        precision = torch.autocast("cuda", dtype=torch.float16) if self._fp16 else nullcontext()
        with torch.inference_mode(), precision:
            # Encode current first so all startup slots can reuse its tokens.
            for sample in reversed(history["samples"]):
                index = sample["frame_index"]
                if index > current or index < 0:
                    raise ValueError("Temporal history contains invalid/future frame indices")
                if index not in self.tokens:
                    image = torch.from_numpy(self.preprocess(sample["images"])).to(self.config["device"])
                    self.tokens[index] = self.model._encode_frame(image).detach()
                    encoded += 1
            for offset, sample in zip(expected, history["samples"]):
                index = sample["frame_index"]
                wanted = current + offset
                if index != wanted and not (wanted < history["first_frame_index"] and index == current):
                    raise ValueError("Only missing startup history may use current-frame padding")
                slots.append(self.tokens[index])
            frame_slots = torch.stack(slots, dim=1)
        earliest = current - (count - 1) * stride
        for index in list(self.tokens):
            if index < earliest:
                del self.tokens[index]
        self.last_frame_index = current
        self.history_diagnostics = {"history_frame_indices": [s["frame_index"] for s in history["samples"]],
                                    "time_offsets": expected, "new_frame_encodings": encoded,
                                    "cached_frames": len(self.tokens)}
        return {"frame_slots": frame_slots,
                "valid_mask": torch.ones((1, count), dtype=torch.bool, device=frame_slots.device),
                "time_offsets": torch.tensor([expected], dtype=torch.float32, device=frame_slots.device)}

    def infer(self, observation, context):
        result = super().infer(observation, context)
        if self.config.get("record_diagnostics", False):
            self.inference_diagnostics = {**getattr(self, "inference_diagnostics", {}), **self.history_diagnostics}
        return result


class TemporalLVAFP16Adapter(TemporalLVAAdapter):
    _fp16 = True
