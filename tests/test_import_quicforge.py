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
from quicdeploy.config.model import PIPERX_EE_CONTRACT, PIPERX_EE_JOINT_NAMES, PIPERX_EE_URDF_SHA256, load_model_card
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


def complete(source, *, manifest=True, name=None):
    if name is not None:
        source = source.rename(source.with_name(name))
    (source / "preproc.pt").write_bytes(b"preprocessing fixture")
    pin(source)
    if not manifest:
        (source / "download_manifest.json").unlink()
    return source


@pytest.fixture
def flat_piperx_ee(flat_lva):
    path = flat_lva / "conversion_meta.json"
    meta = json.loads(path.read_text())
    meta.update(schema_version=2, source_robot_type="bi_piper_native", robot_profile="piperx")
    meta["kinematics"] = {"urdf_sha256": PIPERX_EE_URDF_SHA256, "input_joint_angle_unit": "degrees"}
    for side, start in (("left", 0), ("right", 6)):
        meta["kinematics"][side] = {"base_link": PIPERX_EE_CONTRACT["frame"],
                                    "end_link": PIPERX_EE_CONTRACT["ends"][side],
                                    "joint_names": PIPERX_EE_JOINT_NAMES[start:start + 6]}
    meta["pose_labels"] = {"position_unit": "meters", "rotation_representation": "rotation_6d_first_two_columns",
                           "transform_direction": "base_link_to_end_link", "gripper_source_feature": "action"}
    path.write_text(json.dumps(meta))
    return flat_lva


def test_piperx_ee_import_selects_matching_hardware(project, flat_piperx_ee, monkeypatch):
    from quicdeploy.config.resolve import resolve

    shutil.copyfile(PROJECT_ROOT / "configs/defaults.yaml", project / "configs/defaults.yaml")
    (project / "configs/hardware").symlink_to(PROJECT_ROOT / "configs/hardware", target_is_directory=True)
    (project / "third_party").symlink_to(PROJECT_ROOT / "third_party", target_is_directory=True)
    monkeypatch.setattr(workflow, "resolve", resolve)
    source = complete(flat_piperx_ee, name="piperx_ee_run")
    result = workflow.run_import(source)
    assert result["hardware"] == "hardware/piper_x/ee/hardware.yaml"
    paths = workflow.targets(source.name)
    config = resolve(paths["config"])
    assert config["model"]["frame"] == PIPERX_EE_CONTRACT["frame"]
    assert config["model"]["ends"] == PIPERX_EE_CONTRACT["ends"]
    assert config["kinematics"]["joint_names"] == PIPERX_EE_JOINT_NAMES
    assert sha256(config["kinematics"]["urdf"]) == PIPERX_EE_URDF_SHA256
    override = yaml.safe_load(paths["config"].read_text())
    override.pop("hardware")
    paths["config"].write_text(yaml.safe_dump(override))
    with pytest.raises(ValueError, match="Model and hardware coordinate contracts differ"):
        resolve(paths["config"])


@pytest.mark.parametrize("mismatch,match", [
    ("urdf", "training URDF SHA-256"),
    ("joint_order", "right joint order"),
    ("gripper_unit", "gripper_unit"),
])
def test_piperx_ee_import_rejects_mismatched_training_metadata(project, flat_piperx_ee, mismatch, match):
    path = flat_piperx_ee / "conversion_meta.json"
    meta = json.loads(path.read_text())
    if mismatch == "urdf":
        meta["kinematics"]["urdf_sha256"] = "0" * 64
    elif mismatch == "joint_order":
        meta["kinematics"]["right"]["joint_names"].reverse()
    else:
        meta["pose_labels"]["gripper_unit"] = "meters"
    path.write_text(json.dumps(meta))
    source = complete(flat_piperx_ee, name="piperx_ee_run")
    with pytest.raises(ValueError, match=match):
        workflow.run_import(source, dry_run=True)


def test_import_without_manifest_preserves_originals_and_pins_artifacts(project, flat_lva):
    source = complete(flat_lva, manifest=False, name="new_pick2box")
    before = snapshot(source)
    result = workflow.run_import(source)
    paths = workflow.targets(result["name"])
    assert result["status"] == "passed" and result["hardware_connected"] is False
    assert result["source_integrity"] == "local_baseline_created"
    assert snapshot(source) == before
    assert yaml.safe_load(paths["config"].read_text())["model"] == "models/new_pick2box.json"
    assert 'unset QUICDEPLOY_MODEL' in paths["launcher"].read_text()
    assert paths["launcher"].stat().st_mode & 0o111
    inventory = paths["receipt"] / "source_manifest.json"
    assert json.loads(inventory.read_text())["kind"] == "local_observed_baseline"
    bundle = json.loads((paths["bundle"] / "deployment_manifest.json").read_text())
    assert bundle["source"]["transfer_manifest_sha256"] == sha256(inventory)
    card = load_model_card(paths["card"])
    assert card["model"]["global_step"] == 100000 and card["model"]["id"] == source.name
    assert card["model"]["weight_dtype"] == "float16"
    assert list((project / "configs/models").glob("*.json")) == [paths["card"]]
    assert all(not Path(p).is_absolute() for p in json.loads(paths["card"].read_text())["artifacts"])


@pytest.mark.parametrize("precision", ["fp16", "fp32"])
def test_aggregate_and_precision_selection_keep_one_unsuffixed_card(project, aggregated_flat, precision):
    source = complete(aggregated_flat, name="multi_source")
    result = workflow.run_import(source, precision=precision)
    assert result["source_integrity"] == "provided_manifest_verified"
    paths = workflow.targets(result["name"])
    assert yaml.safe_load(paths["config"].read_text())["model"] == "models/multi_source.json"
    assert load_model_card(paths["card"])["model"]["weight_dtype"] == ("float16" if precision == "fp16" else "float32")
    assert list((project / "configs/models").glob("*.json")) == [paths["card"]]
    assert len(json.loads((paths["bundle"] / "provenance/conversion_meta.json").read_text())["dataset_sources"]) == 2


def test_temporal_preserves_training_stride_and_uses_temporal_prefetch(project, flat):
    source = complete(flat, name="Temporal-Pick")
    result = workflow.run_import(source, precision="fp32")
    paths = workflow.targets(result["name"])
    profile = yaml.safe_load(paths["config"].read_text())
    assert profile["execution"] == {"prefetch_remaining_steps": 23}
    assert profile["model"] == "models/Temporal-Pick.json"
    model = load_model_card(paths["card"])["model"]
    assert model["id"] == "Temporal-Pick" and model["weight_dtype"] == "float32"
    assert model["temporal_sampling"]["history_stride"] == 8
    assert not model.get("allow_history_stride_override", False)


def test_dry_run_uses_directory_name_instead_of_experiment_and_checkpoint_suffixes(project, flat_lva):
    source = complete(flat_lva, manifest=False, name="Pick2Box_1027-Run.2")
    training = json.loads((source / "config.resolved.json").read_text())
    training["experiment"] = {"name": "My Pick2Box-中文"}
    checkpoint = json.loads((source / "checkpoint_manifest.json").read_text())
    checkpoint["config_hash"] = hashlib.sha256(json.dumps(training, sort_keys=True, ensure_ascii=False,
                                                           separators=(",", ":")).encode()).hexdigest()
    (source / "config.resolved.json").write_text(json.dumps(training))
    (source / "checkpoint_manifest.json").write_text(json.dumps(checkpoint))
    before, project_before = snapshot(source), snapshot(project)
    result = workflow.run_import(source, dry_run=True)
    assert result["name"] == source.name and result["status"] == "dry_run"
    assert result["epoch"] == 132 and result["global_step"] == 100000
    assert Path(result["paths"]["card"]).name == source.name + ".json"
    assert Path(result["paths"]["config"]).name == source.name + ".yaml"
    assert Path(result["paths"]["launcher"]).name == f"start_{source.name}.sh"
    assert snapshot(source) == before and snapshot(project) == project_before
    assert not (project / "outputs").exists()


@pytest.mark.parametrize("name", ["x$(touch_pwn)", 'bad"name', "bad name"])
def test_rejects_unsafe_directory_names_before_creating_outputs(project, flat_lva, name):
    source = complete(flat_lva, name=name)
    with pytest.raises(ValueError, match="directory name"):
        workflow.run_import(source)
    assert not (project / "models").exists()


@pytest.mark.parametrize("key", ["bundle", "card", "config", "launcher", "receipt"])
def test_existing_output_is_never_overwritten(project, flat_lva, key):
    source = complete(flat_lva, name="existing")
    path = workflow.targets("existing")[key]
    path.parent.mkdir(parents=True, exist_ok=True); path.write_bytes(b"existing owner")
    with pytest.raises(FileExistsError, match="refusing to overwrite"):
        workflow.run_import(source)
    assert path.read_bytes() == b"existing owner"


def test_provided_manifest_cannot_be_silently_rebaselined(project, flat_lva):
    source = complete(flat_lva)
    (source / "model.pt").write_bytes(b"replacement")
    with pytest.raises(ValueError, match="Downloaded asset changed"):
        workflow.run_import(source)
    assert not (project / "models").exists()


def test_manifest_must_cover_preproc_too(project, flat_lva):
    source = complete(flat_lva)
    p = source / "download_manifest.json"; value = json.loads(p.read_text())
    value["files"] = [item for item in value["files"] if item["name"] != "preproc.pt"]
    p.write_text(json.dumps(value))
    with pytest.raises(ValueError, match="all eight"):
        workflow.run_import(source)


def test_missing_file_and_semantic_mismatch_fail_before_generation(project, flat_lva):
    source = complete(flat_lva)
    (source / "preproc.pt").unlink()
    with pytest.raises(ValueError, match="preproc.pt"):
        workflow.run_import(source)
    complete(source)
    p = source / "normalization.json"; stats = json.loads(p.read_text()); stats["history_frames"] = 16
    p.write_text(json.dumps(stats)); pin(source)
    with pytest.raises(ValueError, match="history_frames"):
        workflow.run_import(source)
    assert not (project / "models").exists()


def test_resolve_failure_rolls_back_and_can_retry(project, flat_lva, monkeypatch):
    source = complete(flat_lva, name="recoverable")
    original_check = workflow.resolve
    def fail(path):
        raise RuntimeError("hardware contract mismatch")
    monkeypatch.setattr(workflow, "resolve", fail)
    with pytest.raises(RuntimeError, match="hardware contract mismatch"):
        workflow.run_import(source)
    assert all(not p.exists() for p in workflow.targets("recoverable").values())
    monkeypatch.setattr(workflow, "resolve", original_check)
    assert workflow.run_import(source)["status"] == "passed"


def test_destination_created_while_validating_is_preserved(project, flat_lva, monkeypatch):
    source = complete(flat_lva, name="race")
    path = workflow.targets("race")["config"]
    @contextmanager
    def competing_import():
        path.write_text("other import owns this")
        yield
    monkeypatch.setattr(workflow, "project_lock", competing_import)
    with pytest.raises(FileExistsError):
        workflow.run_import(source)
    assert path.read_text() == "other import owns this"


def test_parallel_import_fails_without_touching_model_targets(project, flat_lva):
    source = complete(flat_lva, name="locked")
    with workflow.project_lock(), pytest.raises(RuntimeError, match="Another model import"):
        workflow.run_import(source)
    assert all(not p.exists() for p in workflow.targets("locked").values())


def test_source_change_during_import_rolls_back(project, flat_lva, monkeypatch):
    source = complete(flat_lva, manifest=False, name="mutating")
    original = workflow.importer.prepare
    def change_after_copy(*args, **kwargs):
        result = original(*args, **kwargs)
        if not kwargs.get("dry_run"):
            (source / "model.pt").write_bytes(b"concurrent upload")
        return result
    monkeypatch.setattr(workflow.importer, "prepare", change_after_copy)
    with pytest.raises(ValueError, match="source changed during import"):
        workflow.run_import(source)
    assert all(not p.exists() for p in workflow.targets("mutating").values())


def test_output_inside_source_is_rejected_before_source_directory_changes(project, flat_lva):
    source = complete(flat_lva)
    for filename in workflow.INPUT_FILES:
        shutil.copyfile(source / filename, project / filename)
    before = snapshot(project)
    with pytest.raises(ValueError, match="source must be separate"):
        workflow.run_import(project)
    assert snapshot(project) == before


@pytest.mark.parametrize("name", ["9Model-v2", "拾取模型_1027"])
def test_directory_name_is_not_lowercased_or_renamed(project, flat_lva, name):
    source = complete(flat_lva, name=name)
    result = workflow.run_import(str(source) + "/")
    assert result["name"] == name
    paths = workflow.targets(name)
    assert paths["card"].name == name + ".json"
    assert paths["config"].name == name + ".yaml"
    assert paths["launcher"].name == f"start_{name}.sh"
    assert load_model_card(paths["card"])["model"]["id"] == name


def test_symlink_uses_supplied_directory_name(project, flat_lva):
    source = complete(flat_lva)
    alias = source.with_name("Checkpoint_Alias")
    alias.symlink_to(source, target_is_directory=True)
    result = workflow.run_import(alias, dry_run=True)
    assert result["name"] == alias.name and result["source"] == str(source.resolve())


def test_symlink_parent_navigation_keeps_original_source_resolution(project, flat_lva):
    source = complete(flat_lva, name="Actual-Checkpoint")
    alias = source.parent / "links/Alias"
    alias.parent.mkdir(); alias.symlink_to(source, target_is_directory=True)
    result = workflow.run_import(alias / ".." / source.name, dry_run=True)
    assert result["source"] == str(source) and result["name"] == source.name
