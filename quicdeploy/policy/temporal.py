"""Temporal-LVA deployment: source-indexed spatial-token cache, shared RTC sampler."""
from collections import OrderedDict
from contextlib import nullcontext

from .lva import LVAAdapter


class TemporalLVAAdapter(LVAAdapter):
    def load(self, config):
        from ..config.temporal import load_bundle
        from .backends.temporal_lva import TemporalLVADecoder, CachedTemporalLVADecoder, half_temporal

        bundle = load_bundle(config)
        self._configure_runtime(config)
        torch = self.torch
        kwargs = {k: v for k, v in bundle["model_config"].items() if k != "type"}
        kwargs["backbone_repo"] = (bundle["backbone_repo"]
                                   if kwargs.get("backbone", "dinov3_vitb16") == "dinov3_vitb16" else None)
        model_class = (CachedTemporalLVADecoder if bundle["model_config"]["type"] == "CachedTemporalLVADecoder"
                       else TemporalLVADecoder)
        weights = torch.load(config["weights"], map_location="cpu", weights_only=True, mmap=True)
        if not isinstance(weights, dict) or not weights or not all(torch.is_tensor(v) for v in weights.values()):
            raise ValueError("Temporal-LVA weights must be a plain state_dict")
        for value in weights.values():
            if value.is_floating_point() and (value.dtype != torch.float32 or not torch.isfinite(value).all()):
                raise ValueError("Temporal-LVA reference weights must be finite FP32")
        with torch.device("meta"):
            model = model_class(**kwargs)
        model.load_state_dict(weights, strict=True, assign=True)
        if any(t.is_meta for t in (*model.parameters(), *model.buffers())):
            raise ValueError("Incomplete Temporal-LVA state_dict")
        self.model = model.to(config["device"]).eval().requires_grad_(False)
        self._set_normalization(bundle["stats"], mean_key="mu")
        self.sampling = bundle["sampling"]
        if self._fp16:
            if config["device"] != "cuda" or config.get("layerscale_dtype") != "float32":
                raise ValueError("Temporal FP16 requires CUDA and FP32 LayerScale")
            half_temporal(self.model)
        self.reset()

    def reset(self):
        self.tokens = OrderedDict()
        self.session = None
        self.last_frame_index = None
        self.first_token_frame_index = None

    def warmup(self):
        """Initialize CUDA kernels before ready/arm connection, without history."""
        torch = self.torch
        count, stride = self.sampling["history_frames"], self.sampling["history_stride"]
        precision = torch.autocast("cuda", dtype=torch.float16) if self._fp16 else nullcontext()
        with torch.inference_mode(), precision:
            image = torch.zeros((1, 3, 448, 1824), device=self.config["device"])
            slots = self.model._encode_frame(image)[:, None].expand(-1, count, -1, -1)
            packed = {"frame_slots": slots,
                      "valid_mask": torch.ones((1, count), dtype=torch.bool, device=slots.device),
                      "time_offsets": torch.tensor([list(range(-(count - 1) * stride, 1, stride))],
                                                   device=slots.device, dtype=torch.float32)}
            self.model.inference(packed, noise=torch.zeros((1, 50, 20), device=slots.device),
                                 delay=0, num_steps=10, guidance_scale=0)
        if self.config["device"] == "cuda":
            torch.cuda.synchronize()

    def observe_vision(self, sample):
        """Encode one source-grid frame independently of action requests."""
        if sample["session"] != self.session or self.first_token_frame_index is None:
            raise ValueError("Vision update requires an initialized prediction session")
        index = sample["frame_index"]
        stride = self.sampling["history_stride"]
        if index < self.first_token_frame_index or (index - self.first_token_frame_index) % stride:
            raise ValueError("Vision update is outside the source-frame sampling grid")
        if index in self.tokens:
            return False
        torch = self.torch
        precision = torch.autocast("cuda", dtype=torch.float16) if self._fp16 else nullcontext()
        with torch.inference_mode(), precision:
            image = torch.from_numpy(self.preprocess(sample["images"])).to(self.config["device"])
            self.tokens[index] = self.model._encode_frame(image).detach()
        if self.config["device"] == "cuda":
            torch.cuda.synchronize()
        earliest = index - (self.sampling["history_frames"] - 1) * stride
        for old in list(self.tokens):
            if old < earliest:
                del self.tokens[old]
        return True

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
        if self.first_token_frame_index is None:
            self.first_token_frame_index = current
        first = max(self.first_token_frame_index, history["first_frame_index"])
        current_sample = history["samples"][-1]
        if current_sample["frame_index"] != current:
            raise ValueError("Temporal history must end with the current frame")
        resolved, padding = [], 0
        for offset, sample in zip(expected, history["samples"]):
            index = sample["frame_index"]
            if index > current or index < 0:
                raise ValueError("Temporal history contains invalid/future frame indices")
            wanted = current + offset
            if wanted < first:
                # No retrospective encoding before this token session began.
                # Always reuse THIS request's latest token for absent slots.
                resolved.append(current_sample)
                padding += 1
            else:
                if index != wanted:
                    raise ValueError("Only missing startup history may use current-frame padding")
                resolved.append(sample)
        encoded = 0
        precision = torch.autocast("cuda", dtype=torch.float16) if self._fp16 else nullcontext()
        with torch.inference_mode(), precision:
            # Encode current first so all startup slots can reuse its tokens.
            unique = {sample["frame_index"]: sample for sample in reversed(resolved)}
            for index, sample in unique.items():
                if index not in self.tokens:
                    image = torch.from_numpy(self.preprocess(sample["images"])).to(self.config["device"])
                    self.tokens[index] = self.model._encode_frame(image).detach()
                    encoded += 1
            if padding == count - 1:
                # A broadcast view shares one computed token across all slots.
                frame_slots = self.tokens[current][:, None].expand(-1, count, -1, -1)
            else:
                slots = [self.tokens[sample["frame_index"]] for sample in resolved]
                frame_slots = torch.stack(slots, dim=1)
        earliest = current - (count - 1) * stride
        for index in list(self.tokens):
            if index < earliest:
                del self.tokens[index]
        self.last_frame_index = current
        self.history_diagnostics = {"history_frame_indices": [s["frame_index"] for s in resolved],
                                    "time_offsets": expected, "new_frame_encodings": encoded,
                                    "cached_frames": len(self.tokens), "startup_padding_slots": padding,
                                    "first_token_frame_index": self.first_token_frame_index}
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
