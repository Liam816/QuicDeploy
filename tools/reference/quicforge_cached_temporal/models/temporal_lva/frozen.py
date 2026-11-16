"""Frozen single-frame ViT-B/16 or ViT-L/16 features, trainable temporal resampling and action head."""
from __future__ import annotations

from collections.abc import Mapping
from contextlib import nullcontext
import hashlib
from pathlib import Path

import torch
from torch import nn
from torch.utils.checkpoint import checkpoint

from quicforge.io.weights import WeightsPayload
from quicforge.models.temporal_lva.model import (
    TemporalLVADecoder, spatial_position_encoding, relative_time_encoding, _HistoryObservation,
)

FEATURE_KINDS = {
    "dinov3_vitb16": "lva_vitb16_sum_last4_patch_tokens_v1",
    "dinov3_vitl16": "lva_vitl16_sum_last4_patch_tokens_v1",
}
FEATURE_KIND = FEATURE_KINDS["dinov3_vitb16"]


def sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(8 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def single_frame_patch_tokens(backbone, images, precision="bf16"):
    """Exact single-frame feature recipe for the chosen B/16 or L/16 implementation."""
    if precision not in ("bf16", "fp32"):
        raise ValueError("backbone_precision must be bf16 or fp32")
    context = torch.autocast("cuda", dtype=torch.bfloat16, enabled=precision == "bf16") \
        if images.device.type == "cuda" else nullcontext()
    with torch.no_grad(), context:
        # The official API has already removed CLS/register tokens. Match
        # the configured cache dtype even when the residual stream is FP32.
        if callable(getattr(backbone, "get_intermediate_layers", None)):
            features = sum(backbone.get_intermediate_layers(images, n=4, norm=False))
        else:
            intermediate = backbone.forward_with_features(
                images, layers_to_save=[20, 21, 22, 23])["intermediate"]
            features = sum(intermediate[f"layer_{index}"] for index in range(20, 24))[:, 5:]
        return features.to(torch.bfloat16) if precision == "bf16" else features.float()


class SingleFrameLVABackboneLoader:
    """Strictly cover only trained _backbone.*; never initialize the new trainable head."""
    covers = ("_backbone.",)

    def __init__(self, path: str, expected_sha256: str | None = None):
        self.path = str(path)
        self.expected_sha256 = expected_sha256

    def read(self, model):
        path = Path(self.path).expanduser().resolve()
        if not path.is_file():
            raise FileNotFoundError(path)
        digest = sha256_file(path)
        if self.expected_sha256 is not None and digest != self.expected_sha256:
            raise ValueError("single-frame checkpoint SHA256 mismatch")
        source = torch.load(path, map_location="cpu", mmap=True, weights_only=True)
        if not isinstance(source, dict):
            raise TypeError("expected native QuicForge model.pt state_dict")
        state = {k: v for k, v in source.items() if k.startswith(self.covers)}
        if not state:
            raise ValueError("checkpoint contains no _backbone.* weights")
        for name, value in state.items():
            if not torch.is_tensor(value):
                raise TypeError(f"non-tensor backbone key: {name}")
            if value.is_floating_point() and not bool(torch.isfinite(value).all()):
                raise ValueError(f"nonfinite backbone weight: {name}")
        return WeightsPayload(
            state_dict=state, strict=True, covers=self.covers,
            report={"loader": "trained_single_frame_lva_backbone", "path": str(path),
                    "sha256": digest, "optimizer_restored": False, "decoder_restored": False},
        )


class CachedTemporalLVADecoder(TemporalLVADecoder):
    """Use the selected trained ViT-B/16 or ViT-L/16; raw-image inference and cached-token training share all non-ViT layers."""

    def __init__(self, *, freeze_backbone: bool = True, backbone_precision: str = "bf16",
                 backbone: str = "dinov3_vitb16", **kwargs):
        if not freeze_backbone:
            raise ValueError("cached-token mode requires freeze_backbone=True")
        if backbone_precision not in ("bf16", "fp32"):
            raise ValueError("backbone_precision must be bf16 or fp32")
        super().__init__(backbone=backbone, **kwargs)
        self.freeze_backbone = True
        self.backbone_precision = backbone_precision
        self._backbone.requires_grad_(False)
        self._backbone.eval()

    def train(self, mode=True):
        super().train(mode)
        self._backbone.eval()
        return self

    def _spatial_from_tokens(self, features, grid):
        if features.ndim != 3 or features.shape[-1] != self._frame_projection.in_features:
            raise ValueError("patch_tokens must match the selected ViT embedding dimension")
        height, width = grid
        if features.shape[1] != height * width:
            raise ValueError("patch token count differs from patch_grid")
        memory = self._frame_projection(features.to(self._frame_projection.weight.dtype))
        memory = memory + spatial_position_encoding(
            height, width, self._encoder_dim, memory.device, memory.dtype)[None]
        query = self._frame_queries[None].expand(features.shape[0], -1, -1)
        for block in self._spatial_blocks:
            query = block(query, memory)
        return query

    def _encode_frame(self, image):
        features = single_frame_patch_tokens(self._backbone, image, self.backbone_precision)
        return self._spatial_from_tokens(features, (image.shape[-2] // 16, image.shape[-1] // 16))

    def encode_image(self, img):
        if isinstance(img, _HistoryObservation):
            img = img.values
        if not isinstance(img, Mapping) or "patch_tokens" not in img:
            return super().encode_image(img)
        # The inherited inference adapter exposes its mapping via .values.
        return self._encode_cached(img)

    def _history(self, observation):
        # Preserve the parent's raw image path and all its causal validations.
        return super()._history(observation)

    def _encode_cached(self, observation):
        if any(p.requires_grad for p in self._backbone.parameters()):
            raise ValueError("cached-token training cannot use a trainable backbone")
        tokens = observation["patch_tokens"]
        if not torch.is_tensor(tokens) or tokens.ndim != 4 or not tokens.is_floating_point():
            raise ValueError("patch_tokens must be floating (B,T,N,C)")
        batch, history = tokens.shape[:2]
        if batch < 1 or history < 1 or tokens.shape[-1] != self._frame_projection.in_features:
            raise ValueError("invalid cached token shape")
        if self.history_frames is not None and history > self.history_frames:
            raise ValueError("cached history exceeds the legacy model limit")
        grid = observation.get("patch_grid")
        if not torch.is_tensor(grid) or grid.dtype not in (torch.int32, torch.int64):
            raise ValueError("cached observations require integer patch_grid")
        if grid.ndim == 2:
            if tuple(grid.shape) != (batch, 2) or not bool((grid == grid[0]).all()):
                raise ValueError("a cached batch must use a common patch grid")
            grid = grid[0]
        if tuple(grid.shape) != (2,):
            raise ValueError("patch_grid must be (2,) or (B,2)")
        grid = tuple(int(x) for x in grid.tolist())
        if min(grid) < 1 or grid[0] * grid[1] != tokens.shape[2]:
            raise ValueError("invalid patch_grid")
        valid = observation.get("valid_mask")
        offsets = observation.get("time_offsets")
        if not torch.is_tensor(valid) or valid.dtype != torch.bool or tuple(valid.shape) != (batch, history):
            raise ValueError("valid_mask must be bool (B,T)")
        if not torch.is_tensor(offsets) or tuple(offsets.shape) != (batch, history):
            raise ValueError("time_offsets must be (B,T)")
        valid, offsets = valid.to(tokens.device), offsets.to(tokens.device, dtype=torch.float32)
        if not bool(valid[:, -1].all()) or not bool((offsets[:, -1] == 0).all()):
            raise ValueError("current frame must be valid with offset zero")
        if not bool(torch.isfinite(offsets).all()) or bool((offsets > 0).any()):
            raise ValueError("cached offsets must be finite and causal")
        if bool(((offsets[:, 1:] < offsets[:, :-1]) & valid[:, 1:] & valid[:, :-1]).any()):
            raise ValueError("cached history must be chronological")
        slots = []
        for index in range(history):
            features = torch.where(valid[:, index, None, None], tokens[:, index], 0.0)
            if self.gradient_checkpointing and self.training and torch.is_grad_enabled():
                encoded = checkpoint(self._spatial_from_tokens, features, grid,
                                     use_reentrant=False, preserve_rng_state=True)
            else:
                encoded = self._spatial_from_tokens(features, grid)
            slots.append(encoded)
        frame_slots = torch.stack(slots, dim=1)
        time = relative_time_encoding(offsets, self._encoder_dim, self.time_encoding_scale, frame_slots.dtype)
        memory = (frame_slots + time[:, :, None, :]).flatten(1, 2)
        mask = (~valid)[:, :, None].expand(-1, -1, self._frame_queries.shape[0]).flatten(1, 2)
        query = frame_slots[:, -1] + self._temporal_queries[None]
        for block in self._temporal_blocks:
            query = block(query, memory, mask)
        return self._visual_output_norm(query)
