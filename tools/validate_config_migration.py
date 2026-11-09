"""Capture/compare effective configs and recorded inference, without hardware.
保存/对比配置迁移前后的有效参数和记录样本推理，全程不连接机械臂。
Run capture before editing, then compare with the same baseline directory.
先运行 capture，再修改配置并运行 compare；基线目录必须保持不变。
"""
import argparse
import gc
import json
import sys
from copy import deepcopy
from pathlib import Path

import numpy as np

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))

from quicdeploy.common.io import canonical, sha256
from quicdeploy.config.resolve import resolve

DESCRIPTIVE = {"name", "architecture", "demo", "dataset_split", "training_source_commit",
               "noise_generator", "checkpoint_sha256"}


def effective(config):
    """Exclude bookkeeping only; retain every numerical/safety setting.
    仅排除文件指纹和描述信息，保留全部数值、模型身份和保护设置。
    """
    result = deepcopy(config)
    for key in ("fingerprint", "config_sources", "schema_version", "metadata"):
        result.pop(key, None)
    for key in DESCRIPTIVE:
        result["model"].pop(key, None)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("capture", "compare"))
    parser.add_argument("--baseline", required=True, type=Path)
    parser.add_argument("--sample", type=Path, help="recorded NPZ; enables real model inference / 记录样本，启用真实模型推理")
    parser.add_argument("--allow-missing", action="store_true", help="report already-missing legacy assets / 显式记录已有的历史文件缺失")
    args = parser.parse_args()
    baseline = args.baseline
    if args.mode == "capture":
        baseline.mkdir(parents=True, exist_ok=False)
    expected = json.loads((baseline / "snapshot.json").read_text()) if args.mode == "compare" else None
    snapshot = {"configs": {}, "inference": {}, "unavailable": {}, "physical_commands_sent": 0}
    configurations = {}
    for path in sorted((PROJECT / "configs").glob("*.yaml")):
        if path.name.startswith(".") or path.name == "defaults.yaml":
            continue
        key = str(path.relative_to(PROJECT))
        snapshot["configs"][key] = effective(resolve(path))
    for card in sorted((PROJECT / "configs/models").glob("*.json")):
        if card.name.startswith("."):
            continue
        key = str(card.relative_to(PROJECT))
        try:
            config = resolve(PROJECT / "configs/runtime.yaml", model_card=card)
        except FileNotFoundError as exc:
            if not args.allow_missing:
                raise
            snapshot["unavailable"][key] = str(exc.filename)
            print(f"UNAVAILABLE (existing missing file): {key}: {exc.filename}", flush=True)
            continue
        snapshot["configs"][key] = effective(config)
        configurations[card.stem] = config
    if expected is not None:
        assert snapshot["unavailable"] == expected["unavailable"], "Model availability changed"
    if expected is not None and snapshot["configs"] != expected["configs"]:
        (baseline / "actual_configs.json").write_text(canonical(snapshot["configs"]) + "\n")
        raise AssertionError("Effective configuration changed; inspect actual_configs.json")
    if args.sample:
        import torch
        from quicdeploy.policy.base import load_adapter

        snapshot["sample_sha256"] = sha256(args.sample)
        if expected is not None:
            assert snapshot["sample_sha256"] == expected["sample_sha256"], "Recorded sample changed"
        sample = dict(np.load(args.sample, allow_pickle=False))
        for name, config in configurations.items():
            adapter = load_adapter(dict(config["model"]))
            observation = {"images": {c: sample["image_" + c] for c in config["model"]["cameras"]},
                           "eef_state": sample["anchor"], "timestamp": 0.0}
            for mode, prefix in (("no_prefix", None), ("rtc", sample["prefix_absolute"])):
                context = {"seed": 0, "episode": 0, "anchor_index": int(sample["frame"]), "prefix_absolute": prefix}
                raw = adapter.infer(observation, context)
                key = name + "_" + mode
                if expected is None:
                    np.save(baseline / (key + ".npy"), raw)
                else:
                    before = np.load(baseline / (key + ".npy"), allow_pickle=False)
                    assert np.array_equal(raw, before), f"Inference changed: {key}, max_abs={np.max(abs(raw-before))}"
                assert raw.shape == (50, 20) and np.isfinite(raw).all()
                snapshot["inference"][key] = {"shape": list(raw.shape), "bit_exact": expected is not None}
                print(key + ": passed", flush=True)
            del adapter
            gc.collect()
            torch.cuda.empty_cache()
    if expected is not None:
        assert set(snapshot["inference"]) == set(expected["inference"]), "Inference coverage changed"
    snapshot["status"] = "passed"
    destination = "snapshot.json" if expected is None else "comparison.json"
    (baseline / destination).write_text(json.dumps(snapshot, indent=2, ensure_ascii=False) + "\n")
    print(f"PASS: {len(snapshot['configs'])} configs, {len(snapshot['inference'])} inference cases; no hardware")


if __name__ == "__main__":
    main()
