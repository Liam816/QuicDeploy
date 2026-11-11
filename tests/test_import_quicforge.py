"""Import workflow integrity, collision, rollback and temporal-profile tests.
导入工作流的完整性、重名保护、失败回滚与时序配置测试；不加载 GPU 或机械臂。
"""
from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import shutil

import pytest
import yaml

from quicdeploy.common.io import PROJECT_ROOT, sha256
from quicdeploy.config.model import load_model_card
from test_quicforge import bundle as single_frame_bundle
from test_prepare_quicforge import flat, flat_lva, aggregated_flat, pin
from tools import import_quicforge as workflow


def snapshot(root):
    return {str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*") if p.is_file()}


@pytest.fixture
def project(tmp_path, monkeypatch):
    root = tmp_path / "project"
    (root / "configs/models").mkdir(parents=True)
    (root / "scripts").mkdir()
    (root / "quicdeploy").symlink_to(PROJECT_ROOT / "quicdeploy", target_is_directory=True)
    monkeypatch.setattr(workflow, "PROJECT", root)
    monkeypatch.setattr(workflow.importer, "PROJECT", root)

    # Unit fixtures isolate physical hardware discovery. Real-server smoke tests
    # use the actual resolver and native project hardware configuration.
    # 单元夹具隔离物理硬件查找；工控机实测使用真实解析器和硬件配置。
    def check(path):
        profile = yaml.safe_load(path.read_text())
        card = load_model_card(path.parent / profile["model"])
        assert profile["extends"] == "defaults.yaml"
        return {"fingerprint": "unit-checked", "model": card["model"]}
    monkeypatch.setattr(workflow, "resolve", check)
    return root


def complete(source, *, manifest=True):
    (source / "preproc.pt").write_bytes(b"preprocessing fixture")
    pin(source)
    if not manifest:
        (source / "download_manifest.json").unlink()
    return source


def test_import_without_manifest_preserves_originals_and_pins_artifacts(project, flat_lva):
    source = complete(flat_lva, manifest=False)
    before = snapshot(source)
    result = workflow.run_import(source, name="new_pick2box")
    paths = workflow.targets(result["name"])
    assert result["status"] == "passed" and result["hardware_connected"] is False
    assert result["source_integrity"] == "local_baseline_created"
    assert snapshot(source) == before
    assert yaml.safe_load(paths["config"].read_text())["model"] == "models/new_pick2box_fp16.json"
    assert 'unset QUICDEPLOY_MODEL' in paths["launcher"].read_text()
    assert paths["launcher"].stat().st_mode & 0o111
    inventory = paths["receipt"] / "source_manifest.json"
    assert json.loads(inventory.read_text())["kind"] == "local_observed_baseline"
    bundle = json.loads((paths["bundle"] / "deployment_manifest.json").read_text())
    assert bundle["source"]["transfer_manifest_sha256"] == sha256(inventory)
    for precision in ("fp16", "fp32"):
        card = load_model_card(paths[precision + "_card"])
        assert card["model"]["global_step"] == 100000
        assert all(not Path(p).is_absolute() for p in json.loads(paths[precision + "_card"].read_text())["artifacts"])


def test_aggregate_and_fp32_selection(project, aggregated_flat):
    source = complete(aggregated_flat)
    result = workflow.run_import(source, name="multi_source", precision="fp32")
    assert result["source_integrity"] == "provided_manifest_verified"
    paths = workflow.targets(result["name"])
    assert yaml.safe_load(paths["config"].read_text())["model"].endswith("_fp32.json")
    assert len(json.loads((paths["bundle"] / "provenance/conversion_meta.json").read_text())["dataset_sources"]) == 2


def test_temporal_preserves_training_stride_and_uses_temporal_prefetch(project, flat):
    source = complete(flat)
    result = workflow.run_import(source, name="temporal_pick", precision="fp32")
    paths = workflow.targets(result["name"])
    profile = yaml.safe_load(paths["config"].read_text())
    assert profile["execution"] == {"prefetch_remaining_steps": 23}
    assert profile["model"].endswith("_fp32.json")
    model = load_model_card(paths["fp32_card"])["model"]
    assert model["temporal_sampling"]["history_stride"] == 8
    assert not model.get("allow_history_stride_override", False)


def test_dry_run_validates_and_names_from_metadata_without_outputs(project, flat_lva):
    source = complete(flat_lva, manifest=False)
    training = json.loads((source / "config.resolved.json").read_text())
    training["experiment"] = {"name": "My Pick2Box-中文"}
    checkpoint = json.loads((source / "checkpoint_manifest.json").read_text())
    checkpoint["config_hash"] = hashlib.sha256(json.dumps(training, sort_keys=True, ensure_ascii=False,
                                                           separators=(",", ":")).encode()).hexdigest()
    (source / "config.resolved.json").write_text(json.dumps(training))
    (source / "checkpoint_manifest.json").write_text(json.dumps(checkpoint))
    before, project_before = snapshot(source), snapshot(project)
    result = workflow.run_import(source, dry_run=True)
    assert result["name"] == "my_pick2box_ep132_step100000" and result["status"] == "dry_run"
    assert snapshot(source) == before and snapshot(project) == project_before
    assert not (project / "outputs").exists()


@pytest.mark.parametrize("name", ["../escape", "x$(touch_pwn)", "9bad"])
def test_rejects_unsafe_names_before_creating_outputs(project, flat_lva, name):
    with pytest.raises(ValueError, match="--name"):
        workflow.run_import(complete(flat_lva), name=name)
    assert not (project / "models").exists()


@pytest.mark.parametrize("key", ["bundle", "fp16_card", "config", "launcher", "receipt"])
def test_existing_output_is_never_overwritten(project, flat_lva, key):
    source = complete(flat_lva)
    path = workflow.targets("existing")[key]
    path.parent.mkdir(parents=True, exist_ok=True); path.write_bytes(b"existing owner")
    with pytest.raises(FileExistsError, match="refusing to overwrite"):
        workflow.run_import(source, name="existing")
    assert path.read_bytes() == b"existing owner"


def test_provided_manifest_cannot_be_silently_rebaselined(project, flat_lva):
    source = complete(flat_lva)
    (source / "model.pt").write_bytes(b"replacement")
    with pytest.raises(ValueError, match="Downloaded asset changed"):
        workflow.run_import(source, name="changed")
    assert not (project / "models").exists()


def test_manifest_must_cover_preproc_too(project, flat_lva):
    source = complete(flat_lva)
    p = source / "download_manifest.json"; value = json.loads(p.read_text())
    value["files"] = [item for item in value["files"] if item["name"] != "preproc.pt"]
    p.write_text(json.dumps(value))
    with pytest.raises(ValueError, match="all eight"):
        workflow.run_import(source, name="incomplete")


def test_missing_file_and_semantic_mismatch_fail_before_generation(project, flat_lva):
    source = complete(flat_lva)
    (source / "preproc.pt").unlink()
    with pytest.raises(ValueError, match="preproc.pt"):
        workflow.run_import(source, name="missing")
    complete(source)
    p = source / "normalization.json"; stats = json.loads(p.read_text()); stats["history_frames"] = 16
    p.write_text(json.dumps(stats)); pin(source)
    with pytest.raises(ValueError, match="history_frames"):
        workflow.run_import(source, name="invalid_contract")
    assert not (project / "models").exists()


def test_resolve_failure_rolls_back_and_can_retry(project, flat_lva, monkeypatch):
    source = complete(flat_lva)
    original_check = workflow.resolve
    def fail(path):
        raise RuntimeError("hardware contract mismatch")
    monkeypatch.setattr(workflow, "resolve", fail)
    with pytest.raises(RuntimeError, match="hardware contract mismatch"):
        workflow.run_import(source, name="recoverable")
    assert all(not p.exists() for p in workflow.targets("recoverable").values())
    monkeypatch.setattr(workflow, "resolve", original_check)
    assert workflow.run_import(source, name="recoverable")["status"] == "passed"


def test_destination_created_while_validating_is_preserved(project, flat_lva, monkeypatch):
    source = complete(flat_lva)
    path = workflow.targets("race")["config"]
    @contextmanager
    def competing_import():
        path.write_text("other import owns this")
        yield
    monkeypatch.setattr(workflow, "project_lock", competing_import)
    with pytest.raises(FileExistsError):
        workflow.run_import(source, name="race")
    assert path.read_text() == "other import owns this"


def test_parallel_import_fails_without_touching_model_targets(project, flat_lva):
    source = complete(flat_lva)
    with workflow.project_lock(), pytest.raises(RuntimeError, match="Another model import"):
        workflow.run_import(source, name="locked")
    assert all(not p.exists() for p in workflow.targets("locked").values())


def test_source_change_during_import_rolls_back(project, flat_lva, monkeypatch):
    source = complete(flat_lva, manifest=False)
    original = workflow.importer.prepare
    def change_after_copy(*args, **kwargs):
        result = original(*args, **kwargs)
        if not kwargs.get("dry_run"):
            (source / "model.pt").write_bytes(b"concurrent upload")
        return result
    monkeypatch.setattr(workflow.importer, "prepare", change_after_copy)
    with pytest.raises(ValueError, match="source changed during import"):
        workflow.run_import(source, name="mutating")
    assert all(not p.exists() for p in workflow.targets("mutating").values())


def test_output_inside_source_is_rejected_before_source_directory_changes(project, flat_lva):
    source = complete(flat_lva)
    for filename in workflow.INPUT_FILES:
        shutil.copyfile(source / filename, project / filename)
    before = snapshot(project)
    with pytest.raises(ValueError, match="source must be separate"):
        workflow.run_import(project, name="overlap")
    assert snapshot(project) == before
