"""Temporal-LVA v1 with the unchanged LVA action decoder.

Three cameras are already concatenated into each RGB image. Time is processed
serially through one official DINOv3 ViT-B/16 and one shared spatial resampler.
The constructor only builds the architecture. ``model.weights`` must initialize
the backbone using its matching official pretrained checkpoint; there is no
network access or implicit pretrained/random fallback in this module.

Historical observations use a tensor (B,T,3,H,W), or a mapping with ``frames``,
``valid_mask`` (B,T), and ``time_offsets`` (B,T). Offsets are differences in video
frame indices, not seconds. A 4-D tensor explicitly means one current frame;
it is never replicated to manufacture a temporal history.

History sampling belongs to the data configuration. The model accepts dynamic
history lengths, and the Dataset/Predictor supplies the sampled frame offsets.
For direct tensor calls without offsets, consecutive source frames are assumed;
legacy constructor history arguments remain accepted for older artifacts.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import importlib
import math
from pathlib import Path
import sys
from typing import Any

import torch
from torch import nn
from torch.utils.checkpoint import checkpoint

from .quicforge_lva.decoder import (
    AdaLNBlock, AttLayer, Decoder, NUM_ADVANTAGE_CONDITIONS, SinusoidalTimeEmbedding,
)
from .quicforge_lva.task_ids import validate_task_ids


def build_dinov3_vitb16(backbone_repo: str | None = None) -> nn.Module:
    """Build the official local implementation; weights are loaded separately.

    Using a local pinned checkout makes model construction reproducible and
    avoids torch.hub downloading code as a side effect of a training worker.
    """
    project_root = Path(__file__).resolve().parents[3]
    repo = Path(backbone_repo).expanduser() if backbone_repo is not None else project_root / "third_party/dinov3"
    if not repo.is_absolute():
        repo = project_root / repo
    repo = repo.resolve()
    if not (repo / "dinov3/hub/backbones.py").is_file():
        raise FileNotFoundError(
            f"Official DINOv3 checkout not found at {repo}; provide backbone_repo "
            "or install the pinned source under third_party/dinov3."
        )
    # Import the official backbone entry point directly. Its hubconf.py also
    # imports unrelated detection/depth heads with extra dependencies.
    inserted = str(repo) not in sys.path
    if inserted:
        sys.path.insert(0, str(repo))
    try:
        factory_module = importlib.import_module("dinov3.hub.backbones")
        if not Path(factory_module.__file__).resolve().is_relative_to(repo):
            raise RuntimeError("A different dinov3 package is already imported; use the pinned backbone_repo")
        backbone = factory_module.dinov3_vitb16(pretrained=False)
    finally:
        if inserted:
            sys.path.remove(str(repo))
    expected = {"embed_dim": 768, "n_blocks": 12, "num_heads": 12, "patch_size": 16}
    actual = {key: getattr(backbone, key, None) for key in expected}
    if actual != expected:
        raise ValueError(f"Expected the official DINOv3 ViT-B/16 architecture {expected}, got {actual}")
    return backbone


class CrossAttentionBlock(nn.Module):
    """Single-projection pre-LN cross attention and FFN for the visual encoder."""

    def __init__(self, dim: int, heads: int, ffn_ratio: int):
        super().__init__()
        self.q_norm = nn.LayerNorm(dim)
        self.kv_norm = nn.LayerNorm(dim)
        self.ffn_norm = nn.LayerNorm(dim)
        self.attention = nn.MultiheadAttention(dim, heads, batch_first=True, dropout=0.0)
        self.ffn = nn.Sequential(
            nn.Linear(dim, dim * ffn_ratio), nn.GELU(), nn.Linear(dim * ffn_ratio, dim),
        )

    def forward(self, query: torch.Tensor, memory: torch.Tensor,
                key_padding_mask: torch.Tensor | None = None) -> torch.Tensor:
        normalized_memory = self.kv_norm(memory)
        query = query + self.attention(
            self.q_norm(query), normalized_memory, normalized_memory,
            key_padding_mask=key_padding_mask, need_weights=False,
        )[0]
        return query + self.ffn(self.ffn_norm(query))


def spatial_position_encoding(height: int, width: int, dim: int, device: torch.device,
                              dtype: torch.dtype) -> torch.Tensor:
    """Fixed global 2-D coordinates for the complete concatenated image."""
    if dim % 4:
        raise ValueError("spatial position encoding requires emb_dim divisible by 4")
    frequency = torch.exp(-math.log(10000.0) * torch.arange(dim // 4, device=device, dtype=torch.float32)
                          / max(dim // 4 - 1, 1))
    y = ((torch.arange(height, device=device, dtype=torch.float32) + 0.5) / height * 2 - 1) * math.pi
    x = ((torch.arange(width, device=device, dtype=torch.float32) + 0.5) / width * 2 - 1) * math.pi
    yy, xx = torch.meshgrid(y, x, indexing="ij")
    y_phase, x_phase = yy.flatten()[:, None] * frequency, xx.flatten()[:, None] * frequency
    return torch.cat((y_phase.sin(), y_phase.cos(), x_phase.sin(), x_phase.cos()), dim=-1).to(dtype)


def relative_time_encoding(offsets: torch.Tensor, dim: int, scale: float,
                           dtype: torch.dtype) -> torch.Tensor:
    """Fixed sin/cos of relative video-frame-index offsets."""
    frequency = torch.exp(-math.log(10000.0) * torch.arange(dim // 2, device=offsets.device, dtype=torch.float32)
                          / max(dim // 2 - 1, 1))
    phase = offsets.float()[..., None] / scale * frequency
    return torch.cat((phase.sin(), phase.cos()), dim=-1).to(dtype)


@dataclass(frozen=True)
class _HistoryObservation:
    """Supply tensor metadata to the inherited, otherwise unchanged flow sampler."""

    values: Mapping[str, torch.Tensor]

    @property
    def shape(self):
        return self.values.get("frames", self.values.get("frame_slots")).shape

    @property
    def device(self):
        return self.values.get("frames", self.values.get("frame_slots")).device


class TemporalLVADecoder(Decoder):
    """Official ViT-B/16 -> frame queries -> temporal queries -> LVA decoder.

    Action module names, types, tensor shapes and ``predict_velocity`` are
    exactly the existing LVA decoder's. Only the visual encoder is replaced.
    ``gradient_checkpointing`` checkpoints a whole frame encoder (ViT plus
    spatial queries), so dense features from all history frames need not be
    retained for backward. Frames are still computed sequentially.
    """

    quicforge_capabilities = {
        "zero3_init": False,
        "inference_kwargs": {"gradient_checkpointing": False, "preproc_file": None},
    }

    def __init__(self, emb_dim: int = 256, time_dim: int = 32, out_dim: int = 20,
                 n_att_layer: int = 4, n_context: int = 50, task_ids: list[int] | None = None,
                 gradient_checkpointing: bool = False, preproc_file: str | None = None,
                 backbone_repo: str | None = None, history_frames: int | None = None, history_stride: int = 1,
                 frame_queries: int = 96, temporal_queries: int = 96, spatial_layers: int = 2,
                 temporal_layers: int = 2, encoder_heads: int = 8, encoder_ffn_ratio: int = 4,
                 time_encoding_scale: float = 1.0):
        # Avoid Decoder.__init__, which would allocate the old 303M ViT-L.
        nn.Module.__init__(self)
        positive = dict(emb_dim=emb_dim, time_dim=time_dim, n_att_layer=n_att_layer,
                        n_context=n_context, history_stride=history_stride,
                        frame_queries=frame_queries, temporal_queries=temporal_queries,
                        spatial_layers=spatial_layers, temporal_layers=temporal_layers,
                        encoder_heads=encoder_heads, encoder_ffn_ratio=encoder_ffn_ratio)
        if any(isinstance(value, bool) or not isinstance(value, int) or value < 1 for value in positive.values()):
            raise ValueError(f"Model dimensions/counts must be positive integers, got {positive}")
        if history_frames is not None and (
                isinstance(history_frames, bool) or not isinstance(history_frames, int) or history_frames < 1):
            raise ValueError("legacy history_frames must be a positive integer or None for dynamic history")
        if out_dim != 20:
            raise ValueError("Temporal-LVA uses the same bimanual 20-D EEF action layout as LVA")
        if emb_dim % 8 or emb_dim % encoder_heads or emb_dim % 4:
            raise ValueError("emb_dim must be divisible by 8, encoder_heads and 4")
        if frame_queries != temporal_queries:
            raise ValueError("frame_queries must equal temporal_queries for current-frame query initialization")
        if not math.isfinite(time_encoding_scale) or time_encoding_scale <= 0:
            raise ValueError("time_encoding_scale must be finite and positive")
        self.preproc_file = preproc_file
        self.history_frames, self.history_stride = history_frames, history_stride
        self.gradient_checkpointing = bool(gradient_checkpointing)
        self.time_encoding_scale = float(time_encoding_scale)
        self._encoder_dim = emb_dim

        self._backbone = build_dinov3_vitb16(backbone_repo)
        self._frame_projection = nn.Linear(768, emb_dim)
        self._frame_queries = nn.Parameter(torch.empty(frame_queries, emb_dim))
        self._temporal_queries = nn.Parameter(torch.empty(temporal_queries, emb_dim))
        nn.init.normal_(self._frame_queries, std=0.02)
        nn.init.normal_(self._temporal_queries, std=0.02)
        self._spatial_blocks = nn.ModuleList([
            CrossAttentionBlock(emb_dim, encoder_heads, encoder_ffn_ratio) for _ in range(spatial_layers)
        ])
        self._temporal_blocks = nn.ModuleList([
            CrossAttentionBlock(emb_dim, encoder_heads, encoder_ffn_ratio) for _ in range(temporal_layers)
        ])
        self._visual_output_norm = nn.LayerNorm(emb_dim)

        # Same action-decoder definitions as Decoder.__init__, without visual
        # _backbone/_emb_proj/_context_pe. Keep names for checkpoint reuse.
        self._action_proj = nn.Linear(out_dim, emb_dim)
        self._time_embedding = SinusoidalTimeEmbedding(time_dim)
        self._advantage_embedding = nn.Embedding(NUM_ADVANTAGE_CONDITIONS, time_dim)
        self._advantage_context_proj = nn.Linear(time_dim, emb_dim, bias=False)
        ids = validate_task_ids([] if task_ids is None else task_ids)
        self._task_embedding = None
        if ids:
            self.register_buffer("_task_ids", torch.tensor(ids, dtype=torch.int32))
            self._task_embedding = nn.Embedding(len(ids), emb_dim)
            nn.init.normal_(self._task_embedding.weight, std=0.02)
        nn.init.zeros_(self._advantage_embedding.weight)
        self._query_pe = nn.Parameter(torch.randn(n_context, emb_dim))
        self._ada_ln = nn.ModuleList([AdaLNBlock(emb_dim, time_dim) for _ in range(n_att_layer)])
        self._self_att = nn.ModuleList([AttLayer(emb_dim, emb_dim) for _ in range(n_att_layer)])
        self._cross_att = nn.ModuleList([AttLayer(emb_dim, emb_dim) for _ in range(n_att_layer)])
        self._smoother = nn.ModuleList([nn.Conv1d(emb_dim, emb_dim, 3, 1, 1) for _ in range(n_att_layer)])
        self._out_proj = nn.Linear(emb_dim, out_dim)
        self._n_context, self._out_dim = n_context, out_dim

    def _history(self, observation: Any) -> tuple[torch.Tensor, torch.Tensor, torch.Tensor]:
        if isinstance(observation, _HistoryObservation):
            observation = observation.values
        values = observation if isinstance(observation, Mapping) else {"frames": observation}
        frames = values["frames"]
        if not torch.is_tensor(frames):
            raise TypeError("frames must be a preprocessed image tensor")
        if frames.ndim == 4:
            frames = frames[:, None]
        if frames.ndim != 5 or frames.shape[2] != 3:
            raise ValueError("Temporal-LVA images must be (B,T,3,H,W) or current-only (B,3,H,W)")
        batch, history, _, height, width = frames.shape
        if batch < 1 or history < 1:
            raise ValueError("expected a nonempty batch and at least one history frame")
        if self.history_frames is not None and history > self.history_frames:
            raise ValueError(f"expected at most {self.history_frames} history frames for this legacy model")
        if min(height, width) < 16 or height % 16 or width % 16:
            raise ValueError("image height/width must be positive multiples of patch size 16")
        valid = values.get("valid_mask")
        if valid is None:
            valid = torch.ones(batch, history, dtype=torch.bool, device=frames.device)
        elif not torch.is_tensor(valid) or valid.dtype != torch.bool or tuple(valid.shape) != (batch, history):
            raise ValueError("valid_mask must be bool (B,T)")
        valid = valid.to(device=frames.device)
        offsets = values.get("time_offsets")
        if offsets is None:
            offsets = (torch.arange(history, device=frames.device, dtype=torch.float32) - (history - 1))
            offsets = offsets[None].expand(batch, -1) * self.history_stride
        elif not torch.is_tensor(offsets) or tuple(offsets.shape) != (batch, history):
            raise ValueError("time_offsets must be (B,T) relative video-frame offsets")
        offsets = offsets.to(device=frames.device, dtype=torch.float32)
        if frames.device.type != "meta" and not torch.compiler.is_compiling() and not torch.jit.is_tracing():
            if not bool(valid[:, -1].all()):
                raise ValueError("the final/current frame must be valid in every sample")
            if not bool(torch.isfinite(offsets).all()) or bool((offsets > 0).any()):
                raise ValueError("time_offsets must be finite and nonpositive (no future frames)")
            if not bool((offsets[:, -1] == 0).all()):
                raise ValueError("the current frame must have time_offset=0")
            if bool(((offsets[:, 1:] < offsets[:, :-1]) & valid[:, 1:] & valid[:, :-1]).any()):
                raise ValueError("valid history frames must be ordered oldest to current")
        return frames, valid, offsets

    def _encode_frame(self, image: torch.Tensor) -> torch.Tensor:
        # Preserve the official model's full forward, normalization and RoPE.
        features = self._backbone.forward_features(image)["x_norm_patchtokens"]
        memory = self._frame_projection(features)
        height, width = image.shape[-2] // 16, image.shape[-1] // 16
        if memory.shape[1] != height * width:
            raise ValueError("backbone patch count differs from the unresized patch16 input grid")
        memory = memory + spatial_position_encoding(height, width, self._encoder_dim,
                                                    memory.device, memory.dtype)[None]
        query = self._frame_queries[None].expand(image.shape[0], -1, -1)
        for block in self._spatial_blocks:
            query = block(query, memory)
        return query

    def encode_image(self, img) -> torch.Tensor:
        if isinstance(img, _HistoryObservation):
            img = img.values
        if isinstance(img, Mapping) and "frame_slots" in img:
            return self.fuse_frame_slots(img["frame_slots"], img["valid_mask"], img["time_offsets"])
        frames, valid, offsets = self._history(img)
        slots = []
        for index in range(frames.shape[1]):
            # Invalid padding cannot introduce NaNs or receive image gradients.
            image = torch.where(valid[:, index, None, None, None], frames[:, index], 0.0)
            if self.gradient_checkpointing and self.training and torch.is_grad_enabled():
                encoded = checkpoint(self._encode_frame, image, use_reentrant=False, preserve_rng_state=True)
            else:
                encoded = self._encode_frame(image)
            slots.append(encoded)
        frame_slots = torch.stack(slots, dim=1)
        return self.fuse_frame_slots(frame_slots, valid, offsets)

    def fuse_frame_slots(self, frame_slots, valid, offsets):
        """Fuse cached spatial tokens; add relative time afresh for each anchor."""
        time = relative_time_encoding(offsets, self._encoder_dim, self.time_encoding_scale, frame_slots.dtype)
        memory = (frame_slots + time[:, :, None, :]).flatten(1, 2)
        padding_mask = (~valid)[:, :, None].expand(-1, -1, self._frame_queries.shape[0]).flatten(1, 2)
        query = frame_slots[:, -1] + self._temporal_queries[None]
        for block in self._temporal_blocks:
            query = block(query, memory, padding_mask)
        return self._visual_output_norm(query)

    @torch.no_grad()
    def inference(self, img, action_prefix=None, delay=None, num_steps=10, noise=None,
                  guidance_scale=1.0, task_id=None):
        # Decoder.inference itself, including all flow/RTC/CFG arithmetic, is
        # reused unchanged. The adapter only exposes batch size and device.
        observation = _HistoryObservation(img) if isinstance(img, Mapping) else img
        return super().inference(observation, action_prefix=action_prefix, delay=delay,
                                 num_steps=num_steps, noise=noise,
                                 guidance_scale=guidance_scale, task_id=task_id)
