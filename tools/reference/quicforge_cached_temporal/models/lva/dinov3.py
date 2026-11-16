"""DINOv3 ViT-L/16 backbone, migrated from lva_hp_rl_261013/src/dinov3_vitl.py.

Architecture and parameter names are unchanged so original checkpoints load strictly.

Reference: https://github.com/facebookresearch/dinov3
"""
import math
from typing import Dict, List, Optional, Tuple

import torch
import torch.nn as nn
import torch.nn.functional as F
from torch.utils.checkpoint import checkpoint


# ============================================================================
#  Configuration
# ============================================================================



# ============================================================================
#  Architecture hyper-parameters for DINOv3 ViT-L/16
# ============================================================================

DINOV3_VITL16_CONFIG = {
    "model_name": "DINOv3 ViT-L/16",
    "patch_size": 16,
    "img_size": 224,
    "in_channels": 3,
    "embed_dim": 1024,         # Token embedding dimension
    "depth": 24,               # Number of transformer blocks
    "num_heads": 16,           # Number of self-attention heads
    "head_dim": 64,            # embed_dim // num_heads = 1024 / 16
    "ffn_hidden_dim": 4096,    # embed_dim * ffn_ratio (4×)
    "num_register_tokens": 4,  # Storage / register tokens
    "layerscale_init": 1e-5,   # LayerScale initial value
    "rope_base": 100.0,        # RoPE frequency base
    "rope_normalize_coords": "separate",  # Normalize h/w coords separately
    "rope_rescale_coords": None,          # None at eval: reference applies rescale only as a train-only jitter
    "total_params": "303M",
}


# ============================================================================
#  RoPE Positional Encoding
# ============================================================================

class RopePositionEmbedding(nn.Module):
    """
    Rotary Position Embedding (RoPE) for 2D spatial positions.

    Uses axial decomposition (h and w coordinates independently) with no
    learnable weights — the periods are fixed geometrically from `base`.

    For DINOv3 ViT-L: base=100, normalize_coords="separate", rescale=2.0
    The first token (CLS) and register tokens are NOT rotated — only patch tokens.
    """

    def __init__(
        self,
        embed_dim: int,
        num_heads: int,
        base: float = 100.0,
        normalize_coords: str = "separate",
        rescale_coords: float | None = None,
        dtype: torch.dtype = torch.float32,
    ):
        super().__init__()
        assert embed_dim % (4 * num_heads) == 0
        self.D_head = embed_dim // num_heads
        self.base = base
        self.normalize_coords = normalize_coords
        self.rescale_coords = rescale_coords
        self.dtype = dtype

        # Pre-compute periods: base^(2i / D) for i in [0, D//4)
        periods = base ** (
            2 * torch.arange(self.D_head // 4, dtype=dtype) / (self.D_head // 2)
        )
        self.register_buffer("periods", periods, persistent=True)

    def forward(self, H: int, W: int) -> Tuple[torch.Tensor, torch.Tensor]:
        device = self.periods.device
        dtype = self.dtype
        dd = {"device": device, "dtype": dtype}

        # Normalized coordinates in [-1, +1]
        if self.normalize_coords == "separate":
            coords_h = torch.arange(0.5, H, **dd) / H
            coords_w = torch.arange(0.5, W, **dd) / W
        else:
            raise ValueError(f"Unsupported: {self.normalize_coords}")

        coords = torch.stack(torch.meshgrid(coords_h, coords_w, indexing="ij"), dim=-1)
        coords = coords.flatten(0, 1)                     # [HW, 2]
        coords = 2.0 * coords - 1.0                       # → [-1, +1]

        if self.rescale_coords is not None:
            coords = coords * self.rescale_coords

        # Angles → sin/cos
        angles = 2 * math.pi * coords[:, :, None] / self.periods[None, None, :]  # [HW, 2, D//4]
        angles = angles.flatten(1, 2)    # [HW, D//2]
        angles = angles.tile(2)           # [HW, D]   (tile to full head dim)
        cos = torch.cos(angles)
        sin = torch.sin(angles)
        return sin, cos                   # each [HW, D_head]


def rope_rotate_half(x: torch.Tensor) -> torch.Tensor:
    x1, x2 = x.chunk(2, dim=-1)
    return torch.cat([-x2, x1], dim=-1)


def rope_apply(x, sin, cos):
    return x * cos + rope_rotate_half(x) * sin


# ============================================================================
#  Building Blocks
# ============================================================================

class PatchEmbedding(nn.Module):
    """Conv2d-based patch embedding: (B, 3, 224, 224) → (B, 196, 1024)"""

    def __init__(self, in_channels=3, embed_dim=1024, patch_size=16):
        super().__init__()
        self.proj = nn.Conv2d(in_channels, embed_dim, kernel_size=patch_size, stride=patch_size)
        self.patch_size = patch_size

    def forward(self, x):
        # x: (B, 3, H, W) → (B, H', W', embed_dim)
        x = self.proj(x)
        H, W = x.shape[2], x.shape[3]
        x = x.flatten(2).transpose(1, 2)  # (B, H'*W', embed_dim)
        return x, H, W


class LayerScale(nn.Module):
    """Per-channel learnable scaling, initialized near zero."""

    def __init__(self, dim: int, init_value: float = 1e-5):
        super().__init__()
        self.gamma = nn.Parameter(torch.ones(dim) * init_value)

    def forward(self, x):
        return x * self.gamma


class MultiHeadSelfAttention(nn.Module):
    """
    Multi-head self-attention with separate Q/K/V projections and RoPE.

    DINOv3 ViT-L: 16 heads × 64 dim = 1024 total.
    K bias is masked to zero during training (mask_k_bias).
    """

    def __init__(self, embed_dim=1024, num_heads=16, qkv_bias=True, proj_bias=True):
        super().__init__()
        self.num_heads = num_heads
        self.head_dim = embed_dim // num_heads
        self.scale = self.head_dim ** -0.5

        # Separate Q, K, V projections (HuggingFace checkpoint format)
        self.q_proj = nn.Linear(embed_dim, embed_dim, bias=qkv_bias)
        # K has no bias: DINOv3 masks the K bias (mask_k_bias=True), so the
        # checkpoint stores no k_proj.bias and the real model uses none here.
        self.k_proj = nn.Linear(embed_dim, embed_dim, bias=False)
        self.v_proj = nn.Linear(embed_dim, embed_dim, bias=qkv_bias)
        self.o_proj = nn.Linear(embed_dim, embed_dim, bias=proj_bias)

    def forward(self, x, rope=None):
        B, N, C = x.shape

        q = self.q_proj(x).reshape(B, N, self.num_heads, self.head_dim).transpose(1, 2)
        k = self.k_proj(x).reshape(B, N, self.num_heads, self.head_dim).transpose(1, 2)
        v = self.v_proj(x).reshape(B, N, self.num_heads, self.head_dim).transpose(1, 2)

        # Apply RoPE to patch tokens only (skip CLS + register prefix)
        if rope is not None:
            sin, cos = rope
            prefix = N - sin.shape[0]
            if prefix > 0:
                q_prefix, q_patch = q[:, :, :prefix], q[:, :, prefix:]
                k_prefix, k_patch = k[:, :, :prefix], k[:, :, prefix:]
                # Expand sin/cos for batch and heads: [HW, D] → [1, 1, HW, D]
                sin_exp = sin.unsqueeze(0).unsqueeze(0)
                cos_exp = cos.unsqueeze(0).unsqueeze(0)
                q = torch.cat([q_prefix, rope_apply(q_patch, sin_exp, cos_exp)], dim=2)
                k = torch.cat([k_prefix, rope_apply(k_patch, sin_exp, cos_exp)], dim=2)

        x = F.scaled_dot_product_attention(q, k, v)    # (B, heads, N, head_dim)
        x = x.transpose(1, 2).reshape(B, N, C)         # (B, N, embed_dim)
        x = self.o_proj(x)
        return x


class MLP(nn.Module):
    """Standard 2-layer MLP with GELU activation: 1024 → 4096 → 1024."""

    def __init__(self, embed_dim=1024, hidden_dim=4096, bias=True):
        super().__init__()
        self.up_proj = nn.Linear(embed_dim, hidden_dim, bias=bias)
        self.act = nn.GELU()
        self.down_proj = nn.Linear(hidden_dim, embed_dim, bias=bias)

    def forward(self, x):
        x = self.up_proj(x)
        x = self.act(x)
        x = self.down_proj(x)
        return x


class TransformerBlock(nn.Module):
    """
    Pre-norm transformer block:
        x' = x + LayerScale(Attention(LayerNorm(x)))
        x'' = x' + LayerScale(MLP(LayerNorm(x')))
    """

    def __init__(self, embed_dim=1024, num_heads=16, ffn_hidden_dim=4096,
                 layerscale_init=1e-5, qkv_bias=True, proj_bias=True, ffn_bias=True):
        super().__init__()
        self.norm1 = nn.LayerNorm(embed_dim, eps=1e-5)
        self.attention = MultiHeadSelfAttention(embed_dim, num_heads, qkv_bias, proj_bias)
        self.layer_scale1 = LayerScale(embed_dim, layerscale_init)

        self.norm2 = nn.LayerNorm(embed_dim, eps=1e-5)
        self.mlp = MLP(embed_dim, ffn_hidden_dim, bias=ffn_bias)
        self.layer_scale2 = LayerScale(embed_dim, layerscale_init)

    def forward(self, x, rope=None):
        x = x + self.layer_scale1(self.attention(self.norm1(x), rope=rope))
        x = x + self.layer_scale2(self.mlp(self.norm2(x)))
        return x


# ============================================================================
#  Full DINOv3 ViT-L/16 Model
# ============================================================================

class DINOv3ViTL16(nn.Module):
    """
    DINOv3 ViT-L/16 Vision Transformer (distilled from ViT-7B teacher).

    Architecture:
        ┌──────────────────────────────────────────────────┐
        │  Input: (B, 3, 224, 224)                         │
        │    ↓                                              │
        │  Patch Embed: Conv2d(3, 1024, 16×16, stride=16)  │
        │    → (B, 196, 1024)                               │
        │    ↓                                              │
        │  Prepend [CLS] (1 token) + [Registers] (4 tokens)│
        │    → (B, 201, 1024)                               │
        │    ↓                                              │
        │  24× TransformerBlock                             │
        │    each: LN → MHSA(RoPE) → LS → + → LN → MLP   │
        │           → LS → +                                │
        │    ↓                                              │
        │  Final LayerNorm                                  │
        │    ↓                                              │
        │  Output:                                          │
        │    CLS token   (B, 1024) — global image feature   │
        │    Patch tokens (B, 196, 1024) — dense features   │
        │    Register tokens (B, 4, 1024)                   │
        └──────────────────────────────────────────────────┘

    Key design choices vs. DINOv2:
        • RoPE positional encoding replaces learned absolute positional embeddings
        • Register tokens (4) for cleaner attention maps
        • LayerScale (init=1e-5) for stable deep training
        • Separate Q/K/V projections with masked K bias
    """

    def __init__(self, cfg=None):
        super().__init__()
        cfg = cfg or DINOV3_VITL16_CONFIG

        self.embed_dim = cfg["embed_dim"]
        self.depth = cfg["depth"]
        self.num_heads = cfg["num_heads"]
        self.patch_size = cfg["patch_size"]
        self.num_register_tokens = cfg["num_register_tokens"]
        self.img_size = cfg["img_size"]
        self.gradient_checkpointing = False

        # Patch embedding
        self.patch_embed = PatchEmbedding(
            cfg["in_channels"], cfg["embed_dim"], cfg["patch_size"]
        )

        # Learnable tokens
        self.cls_token = nn.Parameter(torch.zeros(1, 1, cfg["embed_dim"]))
        self.register_tokens = nn.Parameter(torch.zeros(1, cfg["num_register_tokens"], cfg["embed_dim"]))
        self.mask_token = nn.Parameter(torch.zeros(1, cfg["embed_dim"]))

        # RoPE
        self.rope = RopePositionEmbedding(
            embed_dim=cfg["embed_dim"],
            num_heads=cfg["num_heads"],
            base=cfg["rope_base"],
            normalize_coords=cfg["rope_normalize_coords"],
            rescale_coords=cfg.get("rope_rescale_coords"),
            dtype=torch.float32,
        )

        # Transformer blocks
        self.layers = nn.ModuleList([
            TransformerBlock(
                embed_dim=cfg["embed_dim"],
                num_heads=cfg["num_heads"],
                ffn_hidden_dim=cfg["ffn_hidden_dim"],
                layerscale_init=cfg["layerscale_init"],
                qkv_bias=True,
                proj_bias=True,
                ffn_bias=True,
            )
            for _ in range(cfg["depth"])
        ])

        # Final normalization
        self.norm = nn.LayerNorm(cfg["embed_dim"], eps=1e-5)

    def prepare_tokens(self, x: torch.Tensor) -> Tuple[torch.Tensor, int, int]:
        """Patch embed + prepend CLS & register tokens."""
        B = x.shape[0]
        patches, H, W = self.patch_embed(x)        # (B, HW, D)
        cls = self.cls_token.expand(B, -1, -1)     # (B, 1, D)
        regs = self.register_tokens.expand(B, -1, -1)  # (B, R, D)
        tokens = torch.cat([cls, regs, patches], dim=1)  # (B, 1+R+HW, D)
        return tokens, H, W

    def _run_layer(self, layer, tokens, rope):
        if self.gradient_checkpointing and self.training and torch.is_grad_enabled():
            return checkpoint(
                layer, tokens, rope=rope, use_reentrant=False,
                preserve_rng_state=True,
            )
        return layer(tokens, rope=rope)

    def forward(self, x: torch.Tensor) -> Dict[str, torch.Tensor]:
        """Full forward pass — returns final outputs."""
        tokens, H, W = self.prepare_tokens(x)
        rope_enc = self.rope(H, W)  # (sin, cos) each [HW, head_dim]

        for layer in self.layers:
            tokens = self._run_layer(layer, tokens, rope_enc)

        tokens = self.norm(tokens)

        R = self.num_register_tokens
        return {
            "cls_token": tokens[:, 0],              # (B, D)
            "register_tokens": tokens[:, 1:1+R],    # (B, R, D)
            "patch_tokens": tokens[:, 1+R:],        # (B, HW, D)
            "all_tokens": tokens,                    # (B, 1+R+HW, D)
        }

    def forward_with_features(
        self, x: torch.Tensor, layers_to_save: Optional[List[int]] = None
    ) -> Dict[str, object]:
        """
        Forward pass that captures intermediate feature maps.

        Args:
            x: input image (B, 3, H, W)
            layers_to_save: indices of layers whose outputs to capture.
                            None = save ALL layer outputs.

        Returns:
            Dict with:
              - "final": same as forward() output
              - "intermediate": dict of layer_idx → feature tensor
              - "rope": the rope encoding
              - "patch_embed": raw patch embeddings before transformer
        """
        tokens, H, W = self.prepare_tokens(x)
        rope_enc = self.rope(H, W)

        if layers_to_save is None:
            layers_to_save = list(range(self.depth))

        features = {}
        features["patch_embed"] = tokens#.detach().clone()

        for i, layer in enumerate(self.layers):
            tokens = self._run_layer(layer, tokens, rope_enc)
            if i in layers_to_save:
                features[f"layer_{i}"] = tokens#.detach().clone()

        tokens = self.norm(tokens)
        features["final_norm"] = tokens#.detach().clone()

        R = self.num_register_tokens
        return {
            "final": {
                "cls_token": tokens[:, 0],
                "register_tokens": tokens[:, 1:1+R],
                "patch_tokens": tokens[:, 1+R:],
                "all_tokens": tokens,
            },
            "intermediate": features,
            "spatial_grid": (H, W),
        }


# ============================================================================
#  Weight Loading (safetensors → our model)
# ============================================================================

def hf_state_dict(sd: dict) -> dict:
    """Map a DINOv3 ViT-L state_dict in Hugging Face naming onto ``DINOv3ViTL16``'s keys."""
    # Build mapping from checkpoint keys → our model keys
    new_sd = {}
    for key, value in sd.items():
        # Embeddings
        if key == "embeddings.cls_token":
            new_sd["cls_token"] = value
        elif key == "embeddings.mask_token":
            # Our mask_token is (1, D), checkpoint may be (1, 1, D)
            new_sd["mask_token"] = value.squeeze(1) if value.dim() == 3 else value
        elif key == "embeddings.register_tokens":
            new_sd["register_tokens"] = value
        elif key == "embeddings.patch_embeddings.weight":
            new_sd["patch_embed.proj.weight"] = value
        elif key == "embeddings.patch_embeddings.bias":
            new_sd["patch_embed.proj.bias"] = value

        # Final norm
        elif key == "norm.weight":
            new_sd["norm.weight"] = value
        elif key == "norm.bias":
            new_sd["norm.bias"] = value

        # Transformer layers
        elif key.startswith("layer."):
            # Extract layer index and sub-key
            # e.g. "layer.0.attention.q_proj.weight" → layers.0.attention.q_proj.weight
            parts = key.split(".", 2)  # ["layer", "0", "attention.q_proj.weight"]
            layer_idx = parts[1]
            sub_key = parts[2]
            # Map HF naming: layer_scale.lambda1 → our layer_scale.gamma
            sub_key = sub_key.replace("layer_scale1.lambda1", "layer_scale1.gamma")
            sub_key = sub_key.replace("layer_scale2.lambda1", "layer_scale2.gamma")
            # No special-casing needed for K bias: the model has no k_proj.bias
            # (bias=False) and the checkpoint stores none either.
            new_key = f"layers.{layer_idx}.{sub_key}"
            new_sd[new_key] = value
    return new_sd


def load_weights_from_safetensors(model: DINOv3ViTL16, path: str):
    """Load DINOv3 ViT-L weights from a safetensors checkpoint (HF format)."""
    from safetensors.torch import load_file
    new_sd = hf_state_dict(load_file(path))

    missing, unexpected = model.load_state_dict(new_sd, strict=False)

    # Filter expected missing keys
    # - RoPE periods: pre-computed buffers, not stored in HF checkpoint
    expected_missing_patterns = ["rope.periods"]
    truly_missing = [
        k for k in missing
        if not any(pat in k for pat in expected_missing_patterns)
    ]
    if truly_missing:
        print(f"  ⚠  Unexpected missing keys: {truly_missing}")
    if unexpected:
        print(f"  ⚠  Unexpected checkpoint keys: {unexpected}")

    return model
