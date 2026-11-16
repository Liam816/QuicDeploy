"""Initial weight loading (``model.weights`` of a training config).

Loaders only read and transform weights into a plain CPU state_dict; they never touch sharding.
Writing them into the model is the Strategy's job (load_state_dict for Native / DDP, per-parameter
gathering for ZeRO-3), after the model is built. Missing / unexpected keys and shape mismatches after
structural changes are reported, never silently ignored.

A loader covers either the whole model (``covers`` None, e.g. a full training checkpoint) or the keys under
some prefixes (e.g. ``DINOv3Loader``: only the LVA backbone ``_backbone.``); the keys no loader covers keep
their random initialization and are listed in the report. Several loaders are applied in order
(``CompositeWeightsLoader``), later ones overriding earlier ones.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

import torch
from torch import nn


@dataclass
class WeightsPayload:
    """A loaded state_dict plus the loading rules (strictness, keys allowed to be missing) and a report."""

    state_dict: dict[str, torch.Tensor]
    strict: bool = True
    # keys allowed to be missing even under strict loading (e.g. a newly added task embedding)
    allowed_missing: frozenset[str] = frozenset()
    report: dict[str, Any] = field(default_factory=dict)
    # state_dict key prefixes this payload is responsible for; None: the whole model
    covers: tuple[str, ...] | None = None


class WeightsLoader(Protocol):
    """Protocol for warm-start loaders: ``read(model)`` returns a WeightsPayload."""

    def read(self, model: nn.Module) -> WeightsPayload:
        """Read weights for ``model`` (shapes may be inspected, parameters must not be modified)."""
        ...


def logical_shapes(model: nn.Module) -> dict[str, tuple[int, ...]]:
    """state_dict key -> logical shape; ZeRO-3 shards report ``ds_shape`` instead of the local shard shape."""
    shapes = {}
    for name, tensor in model.state_dict(keep_vars=True).items():
        shape = getattr(tensor, "ds_shape", None)
        shapes[name] = tuple(shape if shape is not None else tensor.shape)
    return shapes


def check_payload(model: nn.Module, payload: WeightsPayload) -> dict[str, Any]:
    """Check keys and shapes before writing; returns the missing / unexpected report or raises.

    Only the keys the payload covers must be present; the others keep their initialization (``not_covered_keys``).
    """
    shapes = logical_shapes(model)
    source = payload.state_dict
    if payload.covers is None:
        covered = set(shapes)
    else:
        covered = {name for name in shapes if name.startswith(tuple(payload.covers))}
    unexpected = sorted(set(source) - set(shapes))
    outside = sorted(set(source) & set(shapes) - covered)
    missing = sorted(covered - set(source))
    mismatched = sorted(
        f"{name}: checkpoint {tuple(source[name].shape)} vs model {shapes[name]}"
        for name in set(source) & set(shapes) if tuple(source[name].shape) != shapes[name]
    )
    problems = []
    if mismatched:
        problems.append(f"形状不匹配 {mismatched[:16]}")
    if unexpected:
        problems.append(f"checkpoint 中有模型不存在的键 {unexpected[:16]}")
    if outside:
        problems.append(f"加载器声明只覆盖 {list(payload.covers)}，却给出了其外的键 {outside[:16]}")
    disallowed_missing = sorted(set(missing) - set(payload.allowed_missing))
    if payload.strict and disallowed_missing:
        problems.append(f"模型键在 checkpoint 中缺失 {disallowed_missing[:16]}")
    if problems:
        raise ValueError("初始化权重与模型结构不一致：" + "；".join(problems)
                         + "。修改结构后请在加载器中显式处理映射规则")
    return {
        **payload.report,
        "loaded_keys": len(source),
        "missing_keys": missing,
        "unexpected_keys": unexpected,
        "strict": payload.strict,
        "covers": None if payload.covers is None else list(payload.covers),
        "not_covered_keys": sorted(set(shapes) - covered),       # random initialization
    }


class CompositeWeightsLoader:
    """Several loaders applied in order (``model.weights`` given as a list); a later loader's keys override an
    earlier one's. The result covers the union of what the loaders cover."""

    def __init__(self, loaders: list[WeightsLoader]):
        self.loaders = list(loaders)

    def read(self, model: nn.Module) -> WeightsPayload:
        state: dict[str, torch.Tensor] = {}
        sources, overridden = [], 0
        payloads = [loader.read(model) for loader in self.loaders]
        for payload in payloads:
            overridden += len(set(state) & set(payload.state_dict))
            state.update(payload.state_dict)
            sources.append({**payload.report, "keys": len(payload.state_dict),
                            "covers": None if payload.covers is None else list(payload.covers)})
        whole = any(payload.covers is None for payload in payloads)
        covers = None if whole else tuple(sorted({prefix for payload in payloads for prefix in payload.covers}))
        return WeightsPayload(
            state, strict=all(payload.strict for payload in payloads),
            allowed_missing=frozenset().union(*(payload.allowed_missing for payload in payloads)),
            covers=covers, report={"loader": "composite", "sources": sources, "overridden_keys": overridden})


COMMON_PREFIXES = ("module.", "model.", "_orig_mod.")


def compare_state_dict(model: nn.Module, state: dict[str, Any]) -> dict[str, Any]:
    """Full key / shape / finiteness comparison of ``state`` against ``model`` (nothing is loaded).

    ``ok`` is true only when every model key is present, there are no extra keys, every shape matches and
    every floating tensor is finite; the lists are complete so a report file can show all problems.
    """
    shapes = logical_shapes(model)
    common = set(state) & set(shapes)
    mismatched = sorted(name for name in common if tuple(state[name].shape) != shapes[name])
    nonfinite = sorted(name for name in common if torch.is_tensor(state[name]) and state[name].is_floating_point()
                       and not bool(torch.isfinite(state[name]).all()))
    report: dict[str, Any] = {
        "model_keys": len(shapes),
        "source_keys": len(state),
        "loaded_keys": len(common) - len(mismatched),
        "missing_keys": sorted(set(shapes) - set(state)),
        "unexpected_keys": sorted(set(state) - set(shapes)),
        "shape_mismatches": {name: {"weights": list(state[name].shape), "model": list(shapes[name])}
                             for name in mismatched},
        "nonfinite_keys": nonfinite,
    }
    report["ok"] = not (report["missing_keys"] or report["unexpected_keys"] or mismatched or nonfinite)
    if not report["ok"]:
        for prefix in COMMON_PREFIXES:
            stripped = {key[len(prefix):] for key in state if key.startswith(prefix)}
            if stripped and len(stripped & set(shapes)) > len(common):
                report["hint"] = f"权重键带前缀 {prefix!r}，去掉后可匹配 {len(stripped & set(shapes))} 个模型键"
                break
    return report


def describe_mismatch(report: dict[str, Any], limit: int = 8) -> str:
    """One-line summary of a failed ``compare_state_dict`` report (first ``limit`` names of each kind)."""
    parts = []
    for key, label in (("missing_keys", "模型需要但权重中缺失"), ("unexpected_keys", "权重中多余（模型没有）"),
                       ("shape_mismatches", "形状不符"), ("nonfinite_keys", "含 NaN / Inf")):
        names = list(report[key])
        if names:
            parts.append(f"{label} {len(names)} 个 {names[:limit]}{' ...' if len(names) > limit else ''}")
    hint = f"；{report['hint']}" if report.get("hint") else ""
    return "；".join(parts) + hint


def load_torch_file(path: str | Path) -> Any:
    """Load a ``torch.save`` file (mmap, CPU) or a ``.safetensors`` file."""
    path = Path(path).expanduser()
    if not path.is_file():
        raise FileNotFoundError(f"初始化权重文件不存在: {path}")
    if path.suffix == ".safetensors":
        from safetensors.torch import load_file

        return load_file(str(path))
    return torch.load(path, map_location="cpu", mmap=True, weights_only=False)


def load_state_dict_file(path: str | Path) -> tuple[dict[str, torch.Tensor], str | None]:
    """The state_dict in a ``.pt`` / ``.safetensors`` file, and the entry it was taken from.

    The file is the state_dict itself (a dict of tensors; entry None), or a training checkpoint saved around it
    (``{"model": state_dict, "optimizer": ..., ...}``, e.g. another framework's), whose ``"model"`` entry is used.
    Which weights are evaluated (raw or EMA) is decided by the file: other entries such as ``"ema"`` are never
    read; to evaluate EMA weights, give a file that holds them as its state_dict (``quicforge export --weights
    ema`` writes ``weights.pt``).
    """
    loaded = load_torch_file(path)

    def is_state(value: Any) -> bool:
        return isinstance(value, dict) and bool(value) and all(torch.is_tensor(v) for v in value.values())

    entry = None
    if not is_state(loaded) and isinstance(loaded, dict) and is_state(loaded.get("model")):
        loaded, entry = loaded["model"], "model"
    if not is_state(loaded):
        top = sorted(loaded)[:16] if isinstance(loaded, dict) else type(loaded).__name__
        raise TypeError(f"{path} 不是 state_dict（张量字典），也不是带 \"model\" 键的训练 checkpoint，顶层为 {top}")
    return dict(loaded), entry


class TorchStateDictLoader:
    """Generic loader for a ``torch.save``d state_dict (optionally a sub-key) or a safetensors file."""

    def __init__(self, path: str, key: str | None = None, strict: bool = True,
                 strip_prefix: str | None = None):
        self.path = str(path)
        self.key = key
        self.strict = strict
        self.strip_prefix = strip_prefix

    def read(self, model: nn.Module) -> WeightsPayload:
        """Load the file, optionally take a sub-key and strip a prefix, and check for non-finite values."""
        state = load_torch_file(self.path)
        if self.key is not None:
            if not isinstance(state, dict) or self.key not in state:
                raise KeyError(f"{self.path} 中没有键 {self.key!r}")
            state = state[self.key]
        if not isinstance(state, dict):
            raise TypeError(f"{self.path} 不是 state_dict")
        if self.strip_prefix:
            state = {k[len(self.strip_prefix):] if k.startswith(self.strip_prefix) else k: v
                     for k, v in state.items()}
        for name, value in state.items():
            if torch.is_tensor(value) and value.is_floating_point() and not bool(torch.isfinite(value).all()):
                raise ValueError(f"初始化权重 {name} 含非有限值")
        return WeightsPayload(dict(state), strict=self.strict,
                              report={"path": str(Path(self.path).resolve()), "loader": "torch_state_dict"})
