"""Observe model file access, then verify an adapter-only four-file bundle.

记录每个模型在校验、加载、推理三个阶段打开的文件，并验证只包含四个文件的临时部署包。
The temporary bundle is checked against the original model on the SAME images and
RTC prefix; existing model directories and cards are never modified.
用同一图像和 RTC 前缀对比原模型与最小包输出；不会修改现有模型目录或模型卡。

    python tools/audit_model_files.py --sample /path/chunk_000003.npz
"""
import argparse
import gc
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))


def worker(job_path):
    job = json.loads(Path(job_path).read_text())
    stage = "validation" if job.get("card") else "load"
    opened = {name: set() for name in ("validation", "load", "inference")}
    executed = set()
    scopes = [Path(p).resolve() for p in job["scopes"]]
    forbidden = [Path(p).resolve() for p in job.get("forbidden", [])]

    def audit(event, args):
        if event == "open" and isinstance(args[0], (str, bytes)):
            path = Path(os.fsdecode(args[0])).resolve()
            if any(path.is_relative_to(root) for root in forbidden):
                raise RuntimeError(f"Minimal bundle tried to read original directory: {path}")
            if any(path.is_relative_to(root) for root in scopes):
                opened[stage].add(str(path))
        elif event == "exec":
            filename = args[0].co_filename
            if filename.startswith("/"):
                path = Path(filename).resolve()
                if any(path.is_relative_to(root) for root in scopes):
                    executed.add(str(path))

    # CPython audit records Python-level file opens; binary/library access is
    # additionally checked with strace during deployment verification.
    # CPython 审计记录 Python 文件打开事件；部署验证时另用 strace 检查底层文件访问。
    sys.addaudithook(audit)
    if job.get("card"):
        from quicdeploy.config.model import load_model_card
        model = load_model_card(job["card"])["model"]
    else:
        model = job["model"]
    stage = "load"
    from quicdeploy.policy.base import load_adapter
    import numpy as np
    adapter = load_adapter(model)
    stage = "inference"
    with np.load(job["sample"]) as data:
        obs = {"images": {c: data["image_" + c] for c in model["cameras"]},
               "eef_state": data["anchor"], "timestamp": 0.0}
        prefix = data["prefix_absolute"]
    result = {}
    for label, value in (("no_prefix", None), ("rtc_prefix", prefix)):
        context = {"seed": 0, "episode": 0, "anchor_index": 3,
                   "prefix_absolute": value, "rng": np.random.default_rng(3)}
        result[label] = adapter.infer(obs, context)
    np.savez(job["arrays"], **result)
    del adapter
    gc.collect()
    # Include executed .py paths even when CPython loaded an existing .pyc.
    # 即使 CPython 命中旧 .pyc，也记录其实际执行的 .py 源码路径。
    logical = set(executed)
    for path in opened["load"] | opened["inference"]:
        p = Path(path)
        if p.is_file() and "__pycache__" not in p.parts:
            logical.add(str(p))
    Path(job["result"]).write_text(json.dumps({
        "opens": {k: sorted(p for p in v if Path(p).is_file()) for k, v in opened.items()},
        "executed_source": sorted(executed), "logical_runtime_files": sorted(logical),
    }, indent=2) + "\n")


def run_worker(job, directory, label):
    job.update(arrays=str(directory / f"{label}.npz"), result=str(directory / f"{label}.json"))
    path = directory / f"{label}-job.json"
    path.write_text(json.dumps(job))
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", PYTHONNOUSERSITE="1")
    env.pop("PYTHONPATH", None)
    subprocess.run([sys.executable, "-B", str(Path(__file__).resolve()), "--worker", str(path)],
                   env=env, cwd=PROJECT, check=True)
    return json.loads(Path(job["result"]).read_text())


def audit_card(card_path, sample):
    import numpy as np
    from quicdeploy.common.io import sha256

    card = json.loads(card_path.read_text())
    model = card["model"]
    required = [Path(model["weights"]), Path(model["normalization"]),
                Path(model["source_dir"]) / "model.py", Path(model["source_dir"]) / "dinov3_vitl.py"]
    scopes = sorted({str(p.parent.resolve()) for p in required})
    inventory = sorted({p.resolve() for root in scopes for p in Path(root).rglob("*")
                        if p.is_file() and "__pycache__" not in p.parts})
    with tempfile.TemporaryDirectory(prefix="quicdeploy-model-audit-") as tmp:
        directory = Path(tmp)
        original = run_worker({"card": str(card_path), "sample": str(sample), "scopes": scopes},
                              directory, "original")
        bundle = directory / "minimal"
        (bundle / "source").mkdir(parents=True)
        mapping = {required[0]: bundle / "model.pt", required[1]: bundle / "normalization.json",
                   required[2]: bundle / "source/model.py", required[3]: bundle / "source/dinov3_vitl.py"}
        for src, dest in mapping.items():
            shutil.copyfile(src, dest)
            assert sha256(src) == sha256(dest), src
        compact = dict(model, weights=str(bundle / "model.pt"), normalization=str(bundle / "normalization.json"),
                       source_dir=str(bundle / "source"), implementation_files=[])
        minimal = run_worker({"model": compact, "sample": str(sample), "scopes": [str(bundle)],
                              "forbidden": scopes}, directory, "minimal")
        if set(minimal["logical_runtime_files"]) != {str(p) for p in mapping.values()}:
            raise RuntimeError("Minimal bundle opened an unexpected file set")
        with np.load(directory / "original.npz") as a, np.load(directory / "minimal.npz") as b:
            comparison = {k: {"equal": bool(np.array_equal(a[k], b[k])),
                              "max_abs_difference": float(np.max(np.abs(a[k] - b[k])))} for k in a.files}
        if not all(c["equal"] for c in comparison.values()):
            raise RuntimeError(f"Compact bundle prediction differs: {card_path}")
        runtime = sorted({str(p.resolve()) for p in required})
        observed = sorted(str(Path(p).resolve()) for p in original["logical_runtime_files"])
        if observed != runtime:
            raise RuntimeError(f"Unexpected adapter file dependency: {observed}")
        integrity_only = sorted(set(card["artifacts"]) - set(runtime))
        unused = sorted(str(p) for p in inventory if str(p) not in set(runtime) | set(card["artifacts"]))
        return {"card": str(card_path), "model_id": model["id"], "python": sys.executable,
                "model_directory": str(required[0].parent), "source_directory": str(required[2].parent),
                "runtime_files": [{"path": str(p), "bytes": p.stat().st_size, "sha256": sha256(p)} for p in required],
                "integrity_only": integrity_only, "unused_in_audited_run": unused,
                "file_open_audit": original, "minimal_bundle_file_count": len(mapping),
                "minimal_bundle_verified": comparison,
                "scope": "Current LVA FP16 adapter; one recorded sample with and without 10-step RTC prefix. "
                         "The four-file test bypasses card registration only; existing cards still require all registered artifacts."}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sample", type=Path)
    parser.add_argument("--card", action="append", type=Path)
    parser.add_argument("--output", type=Path, default=PROJECT / "outputs/model_audit")
    parser.add_argument("--worker", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.worker:
        worker(args.worker)
        return
    if not args.sample:
        parser.error("--sample is required")
    args.output.mkdir(parents=True, exist_ok=True)
    cards = args.card or sorted((PROJECT / "configs/models").glob("*.json"))
    reports = []
    for card in cards:
        if json.loads(card.read_text())["model"].get("artifact_format") == "quicforge_lva_v1":
            print(f"SKIP {card.name}: QuicForge bundles are covered by tools/validate_quicforge.py", flush=True)
            continue
        result = audit_card(card.resolve(), args.sample.resolve())
        target = args.output / f"{card.stem}.json"
        target.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n")
        reports.append(result)
        print(f"PASS {card.name}: 4 runtime files; {len(result['integrity_only'])} integrity-only; "
              f"minimal bundle outputs bit-identical (no prefix / RTC)", flush=True)
    (args.output / "summary.json").write_text(json.dumps(reports, indent=2, ensure_ascii=False) + "\n")


if __name__ == "__main__":
    main()
