"""Create model cards, runtime profile and launcher from an uploaded flat checkpoint.
从上传的八文件 checkpoint 自动生成模型卡、推理配置与启动入口；不连接机械臂。
"""
import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sys
import tempfile

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))

from quicdeploy.common.io import load_json, sha256
from quicdeploy.config.resolve import resolve
from tools import prepare_quicforge as importer

INPUT_FILES = ("model.pt", "preproc.pt", "model_config.json", "inference_config.json", "normalization.json",
               "config.resolved.json", "checkpoint_manifest.json", "conversion_meta.json")


def json_file(path, value):
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def model_name(source, directory_name):
    checkpoint = load_json(source / "checkpoint_manifest.json")
    for key in ("epoch", "global_step"):
        if type(checkpoint.get(key)) is not int or checkpoint[key] < 0:
            raise ValueError("Checkpoint requires a nonnegative integer " + key)
    if not isinstance(checkpoint.get("name"), str) or not checkpoint["name"]:
        raise ValueError("Checkpoint requires its original name")
    # Keep the supplied directory name verbatim. Reject unsafe names rather than
    # silently lowercasing, truncating or appending checkpoint/precision suffixes.
    # 原样保留传入目录名；不安全的名字直接拒绝，不改大小写、截断或拼接断点/精度后缀。
    if not re.fullmatch(r"\w[\w.-]*", directory_name) or len(directory_name.encode("utf-8")) > 240:
        raise ValueError("Checkpoint directory name must use letters/digits/underscore/dot/hyphen (max 240 UTF-8 bytes)")
    return directory_name, checkpoint


def targets(name):
    return {"bundle": PROJECT / "models/quicforge" / name,
            "card": PROJECT / f"configs/models/{name}.json",
            "config": PROJECT / f"configs/{name}.yaml",
            "launcher": PROJECT / f"scripts/start_{name}.sh",
            "receipt": PROJECT / "outputs/imports" / name}


def check_destinations(paths):
    for path in paths.values():
        if path.exists() or path.is_symlink():
            raise FileExistsError(f"Already exists, refusing to overwrite: {path}; choose another checkpoint directory name")


@contextmanager
def project_lock():
    folder = PROJECT / "outputs/imports"
    folder.mkdir(parents=True, exist_ok=True)
    with (folder / ".import.lock").open("a+") as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("Another model import is running") from exc
        try:
            yield
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


def source_manifest(source, temporary):
    """Honor an existing digest; absence creates an observed local baseline only.
    已有清单必须核验；缺少清单时只建立当前本地基线，不宣称此前传输已验证。
    """
    path = source / "download_manifest.json"
    if path.exists() or path.is_symlink():
        if not path.is_file() or path.is_symlink():
            raise ValueError("download_manifest.json must be a regular file")
        value = load_json(path)
        if not set(INPUT_FILES) <= {entry["name"] for entry in value["files"]}:
            raise ValueError("The provided download manifest must pin all eight checkpoint files")
        return path, "provided_manifest_verified"
    path = temporary / "source_manifest.json"
    json_file(path, {"schema_version": 1, "kind": "local_observed_baseline",
                     "files": [{"name": name, "bytes": (source / name).stat().st_size,
                                "sha256": sha256(source / name)} for name in INPUT_FILES]})
    return path, "local_baseline_created"


def launcher(name):
    return '''#!/usr/bin/env bash
# Generated model entry point; shared runtime, RTC, IK and guards.
# 自动生成的模型入口；共用运行时、RTC、IK 和指令保护。
set -euo pipefail
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
export QUICDEPLOY_CONFIG="$ROOT/configs/%s.yaml"
unset QUICDEPLOY_MODEL
exec bash "$ROOT/scripts/start_live.sh" "$@"
''' % name


def run_import(input_dir, *, precision="fp16", dry_run=False, hardlink_weights=False):
    if precision not in ("fp16", "fp32"):
        raise ValueError("Precision must be fp16 or fp32")
    supplied = Path(os.path.abspath(Path(input_dir).expanduser()))
    source = Path(input_dir).expanduser().resolve()
    for filename in INPUT_FILES:
        path = source / filename
        if not path.is_file() or path.is_symlink():
            raise ValueError(f"Missing/linked checkpoint file: {path}")
    name, checkpoint = model_name(source, supplied.name)
    paths = targets(name)
    for destination in paths.values():
        canonical = destination.resolve()
        if source.is_relative_to(canonical) or canonical.is_relative_to(source):
            raise ValueError("Checkpoint source must be separate from generated destinations")
    check_destinations(paths)
    with tempfile.TemporaryDirectory(prefix="quicdeploy-import-") as temporary:
        manifest, integrity = source_manifest(source, Path(temporary))
        manifest_bytes = manifest.read_bytes()
        manifest_hash = hashlib.sha256(manifest_bytes).hexdigest()
        inspection = importer.prepare(source, checkpoint["name"], source / "conversion_meta.json",
                                      paths["bundle"], name, transfer_manifest=manifest, dry_run=True,
                                      card_precision=precision, hardlink_weights=hardlink_weights)
        plan = {"name": name, "source": str(source), "precision": precision,
                "checkpoint": checkpoint["name"], **inspection, "source_integrity": integrity,
                "weights_storage": "hardlink" if hardlink_weights else "copy",
                "paths": {key: str(path) for key, path in paths.items()},
                "check_command": f"bash scripts/start_{name}.sh --check",
                "run_command": f"QUICDEPLOY_WEBVIZ=1 bash scripts/start_{name}.sh --run"}
        if dry_run:
            return {**plan, "status": "dry_run", "hardware_connected": False}
        with project_lock():
            # Recheck under the lock: a competing import may have finished while
            # this one validated its input. Cleanup owns only these new targets.
            # 获取锁后重新检查重名；失败时只回滚本次新建目标，不触碰已有模型。
            check_destinations(paths)
            try:
                importer.prepare(source, checkpoint["name"], source / "conversion_meta.json",
                                 paths["bundle"], name, transfer_manifest=manifest, card_precision=precision,
                                 hardlink_weights=hardlink_weights)
                if sha256(manifest) != manifest_hash:
                    raise ValueError("Source manifest changed during import")
                inputs = {item["name"]: item for item in json.loads(manifest_bytes)["files"]}
                for filename in INPUT_FILES:
                    entry, path = inputs[filename], source / filename
                    if path.stat().st_size != entry["bytes"] or sha256(path) != entry["sha256"]:
                        raise ValueError("Checkpoint source changed during import: " + filename)
                weights_hash = inputs["model.pt"]["sha256"]
                if sha256(paths["bundle"] / "weights.pt") != weights_hash:
                    raise ValueError("Checkpoint weights changed during import")
                import yaml
                profile = {"schema_version": 2, "extends": "defaults.yaml",
                           "model": f"models/{name}.json"}
                if inspection["temporal_sampling"]:
                    profile["execution"] = {"prefetch_remaining_steps": 23}
                paths["config"].write_text("# Generated model profile / 自动生成的模型配置\n" +
                                           yaml.safe_dump(profile, sort_keys=False), encoding="utf-8")
                paths["launcher"].write_text(launcher(name), encoding="utf-8")
                paths["launcher"].chmod(0o755)
                config = resolve(paths["config"])
                paths["receipt"].mkdir(parents=True)
                (paths["receipt"] / "source_manifest.json").write_bytes(manifest_bytes)
                result = {**plan, "status": "passed", "fingerprint": config["fingerprint"],
                          "created_at_utc": datetime.now(timezone.utc).isoformat(),
                          "source_manifest_sha256": manifest_hash, "hardware_connected": False,
                          "validation": "artifact hashes and resolved configuration; no GPU or task-quality test"}
                json_file(paths["receipt"] / "report.json", result)
                return result
            except BaseException:
                for path in paths.values():
                    if path.is_dir() and not path.is_symlink():
                        shutil.rmtree(path)
                    elif path.exists() or path.is_symlink():
                        path.unlink()
                raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("checkpoint_dir", type=Path)
    parser.add_argument("--precision", choices=("fp16", "fp32"), default="fp16")
    parser.add_argument("--dry-run", action="store_true", help="validate and preview only / 只校验和预览")
    parser.add_argument("--hardlink-weights", action="store_true",
                        help="share the source weight inode on the same filesystem / 同盘硬链接复用权重，源文件须保持只读")
    args = parser.parse_args()
    try:
        result = run_import(args.checkpoint_dir, precision=args.precision, dry_run=args.dry_run,
                            hardlink_weights=args.hardlink_weights)
    except (OSError, ValueError, KeyError, RuntimeError) as exc:
        parser.exit(1, f"Import failed / 导入失败: {exc}\n")
    print(json.dumps(result, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
