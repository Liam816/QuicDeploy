"""File, hashing and path helpers shared by every layer.
所有模块共用的文件读写、哈希与路径工具（不导入任何硬件代码）。
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def sha256(path) -> str:
    """Streamed SHA-256 of a file. / 分块计算文件 SHA-256。"""
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def canonical(value) -> str:
    """Deterministic JSON used for fingerprints and logs.
    稳定的 JSON 序列化：键排序、禁止 NaN，用于指纹与日志。
    """
    return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False)


def digest(value) -> str:
    """SHA-256 of the canonical JSON form. / 对规范化 JSON 取哈希。"""
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def load_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def load_yaml(path):
    import yaml

    value = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"Expected a configuration mapping: {path}")
    return value


def resolve_path(value, owner) -> Path:
    """Resolve a reference relative to the file that declares it.

    相对路径以声明它的文件所在目录为基准。只做词法规范化、不解析符号链接：
    venv 的 bin/python 是符号链接，resolve() 会悄悄换成系统解释器。
    """
    if not isinstance(value, (str, Path)) or not str(value):
        raise ValueError(f"Invalid file reference in {owner}")
    path = Path(value).expanduser()
    return Path(os.path.abspath(path if path.is_absolute() else Path(owner).parent / path))
