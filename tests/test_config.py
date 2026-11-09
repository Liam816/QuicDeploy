"""Configuration migration semantics; no models, CAN, cameras or GPU required.
配置迁移语义回归：不需要模型、CAN、相机或 GPU。
"""
from copy import deepcopy
from pathlib import Path

import pytest
import yaml

from quicdeploy.common.io import PROJECT_ROOT, load_yaml
from quicdeploy.config.defaults import OPTIONAL_DEFAULTS
from quicdeploy.config.resolve import SECTION_KEYS, load_runtime_profile, resolve


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(value))
    return path


@pytest.fixture
def profile(tmp_path, monkeypatch):
    import quicdeploy.config.resolve as resolver
    model = {"id": "dummy", "action_dt_s": 1 / 30, "first_target_offset_s": 1 / 30,
             "fixed_prefix_steps": 10, "horizon": 50}
    hardware = {"robot": {"left_gripper_width_mm": 70, "right_gripper_width_mm": 100},
                "kinematics": {}, "ik_parameters": {"ik": {"dt": 0.01}}}
    monkeypatch.setattr(resolver, "load_model_card", lambda p: {"model": deepcopy(model), "metadata": {"name": "test"}})
    monkeypatch.setattr(resolver, "load_hardware", lambda p, s: deepcopy(hardware))
    card = tmp_path / "card.json"; card.write_text("{}")
    parent = load_yaml(PROJECT_ROOT / "configs/defaults.yaml")
    parent.update(hardware="hardware.yaml", model="card.json")
    write(tmp_path / "defaults.yaml", parent)
    path = write(tmp_path / "nested/profile.yaml", {"schema_version": 2, "extends": "../defaults.yaml"})
    return path, parent


def test_compact_profile_matches_legacy_and_keeps_all_guards(profile, tmp_path):
    path, parent = profile
    actual = resolve(path)
    legacy = {"schema_version": 1, "hardware": str(tmp_path / "hardware.yaml"), "model": str(tmp_path / "card.json")}
    legacy.update({key: actual[key] for key in SECTION_KEYS})
    before = resolve(write(tmp_path / "legacy.yaml", legacy))
    for key in (*SECTION_KEYS, "timing", "robot", "kinematics", "ik_parameters", "model"):
        assert actual[key] == before[key]
    assert actual["execution"]["policy_hz"] == 30
    assert actual["guard"]["gripper_widths_mm"] == [70, 100]
    assert actual["guard"]["gripper_binary_thresholds_mm"] == [40, 75]
    assert actual["metadata"] == {"name": "test"}
    assert {str(path), str(tmp_path / "defaults.yaml"), str(tmp_path / "card.json")} <= actual["config_sources"].keys()


def test_owner_relative_paths_and_leaf_overrides(profile, tmp_path, monkeypatch):
    path, _ = profile
    write(path, {"schema_version": 2, "extends": "../defaults.yaml", "execution": {"n_action_steps": 35},
                 "runtime": {"cpu_threads": 2}, "postprocess": {"arm_delay_s": 0.02}})
    monkeypatch.chdir(tmp_path.parent)
    actual = resolve(path)
    assert actual["model_card"] == str(tmp_path / "card.json")
    assert actual["execution"]["n_action_steps"] == 35
    assert actual["execution"]["prefetch_remaining_steps"] == 15
    assert actual["runtime"] == {"device": "cuda", "cpu_threads": 2}
    assert actual["postprocess"] == {"blend_s": 0.1, "arm_delay_s": 0.02, "gripper_delay_s": 0.0}


@pytest.mark.parametrize("section,values,match", [
    ("execution", {"policy_hzz": 30}, "unknown"),
    ("execution", {"policy_hz": 40}, "overclock"),
    ("execution", {"prefix_steps": 11}, "training support"),
    ("execution", {"prefetch_remaining_steps": 5}, "remaining queue"),
    ("guard", {"gripper_widths_mm": [70, 70]}, "widths disagree"),
    ("guard", {"max_joint_speed_deg_s": 0}, "guard limit"),
    ("runtime", {"cpu_threads": 0}, "CPU thread"),
    ("postprocess", {"arm_delay_s": None}, "Invalid"),
])
def test_bad_override_cannot_silently_change_behavior(profile, section, values, match):
    path, _ = profile
    write(path, {"schema_version": 2, "extends": "../defaults.yaml", section: values})
    with pytest.raises(ValueError, match=match):
        resolve(path)


def test_explicit_null_disables_optional_guard(profile):
    path, _ = profile
    write(path, {"schema_version": 2, "extends": "../defaults.yaml",
                 "guard": {"gripper_binary_thresholds_mm": None, "gripper_speed_mm_s": 10}})
    actual = resolve(path)
    assert actual["guard"]["gripper_binary_thresholds_mm"] is None
    assert actual["guard"]["gripper_speed_mm_s"] == 10


def test_missing_protection_is_not_filled_by_hidden_defaults(profile, tmp_path):
    path, parent = profile
    del parent["motion_limits"]
    write(tmp_path / "defaults.yaml", parent)
    with pytest.raises(ValueError, match="motion_limits.*missing"):
        resolve(path)


def test_cycle_unknown_root_and_legacy_required_fields(tmp_path):
    a = write(tmp_path / "a.yaml", {"schema_version": 2, "extends": "b.yaml"})
    write(tmp_path / "b.yaml", {"schema_version": 2, "extends": "a.yaml"})
    with pytest.raises(ValueError, match="cycle"):
        load_runtime_profile(a, {})
    write(a, {"schema_version": 2, "extends": "b.yaml", "executon": {}})
    with pytest.raises(ValueError, match="unknown"):
        load_runtime_profile(a, {})
    write(a, {"schema_version": 1, "hardware": "hardware.yaml", "model": "card.json"})
    with pytest.raises(ValueError, match="missing"):
        load_runtime_profile(a, {})


def test_defaults_are_not_mutated_across_resolutions(profile):
    path, _ = profile
    before = deepcopy(OPTIONAL_DEFAULTS)
    result = resolve(path)
    result["runtime"]["cpu_threads"] = 99
    assert resolve(path)["runtime"]["cpu_threads"] == 4
    assert OPTIONAL_DEFAULTS == before
