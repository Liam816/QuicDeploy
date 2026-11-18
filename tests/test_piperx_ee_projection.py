"""PiperX EE regression at the nearly coaxial training wrist zero.

回归验证 PiperX EE 训练链腕部轴接近同轴时的投影。
"""
import numpy as np
import pytest
from scipy.spatial.transform import Rotation

from quicdeploy.common.geometry import rot6d
from quicdeploy.common.io import PROJECT_ROOT
from quicdeploy.common.layout import GRIPPER_COLUMNS, IK_JOINT_NAMES, JOINT_COLUMNS, SIDES
from quicdeploy.config.model import PHYSICAL_CONTRACT, PIPERX_EE_CONTRACT, PIPERX_EE_JOINT_NAMES
from quicdeploy.config.resolve import PIPER_CONTROL_URDF_SHA256, load_hardware
from quicdeploy.kinematics.ik import Kinematics
from quicdeploy.kinematics.piperx_ee import PiperXEEProjection
from quicdeploy.kinematics.urdf import URDFFK

HARDWARE = PROJECT_ROOT / "configs/hardware/piper_x"


@pytest.fixture
def mapper():
    label = URDFFK(HARDWARE / "ee/piperx_bimanual.urdf")
    control = URDFFK(HARDWARE / "piper_bimanual.urdf")
    ends = [PIPERX_EE_CONTRACT["ends"][side] for side in SIDES]
    return label, ends, PiperXEEProjection(label, PIPERX_EE_CONTRACT["frame"], ends,
                                          PIPERX_EE_JOINT_NAMES, control, IK_JOINT_NAMES)


def label_pose(mapper, joints):
    return Kinematics._wrist(joints, mapper[0], PIPERX_EE_CONTRACT["frame"], mapper[1], PIPERX_EE_JOINT_NAMES)


def test_training_and_dashboard_geometry_are_distinct_and_pinned():
    sources = {}
    resolved = load_hardware(HARDWARE / "ee/hardware.yaml", sources)
    config = resolved["kinematics"]
    model = resolved["ik_parameters"]["model"]
    assert config["project_piperx_ee"]
    assert config["joint_names"] == PIPERX_EE_JOINT_NAMES
    assert model["controlled_joints"] == IK_JOINT_NAMES
    assert model["tcp_frames"] == [PHYSICAL_CONTRACT["ends"][side] for side in SIDES]
    assert sources[config["native_urdf"]] == PIPER_CONTROL_URDF_SHA256
    assert config["native_urdf"] != config["urdf"]


def test_encoder_zero_and_continuity_across_j5_zero(mapper):
    q = np.array([-28, 40, -35, 32, 0, -7, 70, 20, 40, -33, 35, 0, -3, 100], dtype=float)
    seed = np.deg2rad(q[JOINT_COLUMNS]).reshape(2, 6)
    projection = mapper[2]
    projected, seed, _ = projection.project(label_pose(mapper, q), seed)
    np.testing.assert_allclose(projected, q, atol=1e-10)

    # Both arms traverse J5=0 while J4 moves. Check that ambiguous J6 stays
    # near its encoder branch and the grippers bypass the pose projection.
    # 双臂 J4 运动且 J5 穿过零位，J6 不应跳分支，夹爪毫米值直接透传。
    for i in range(1, 41):
        goal = q.copy()
        goal[[3, 10]] += i / 10.0
        goal[[4, 11]] += 4 * np.sin(i * np.pi / 20)
        goal[GRIPPER_COLUMNS] = [0, 65]
        target = label_pose(mapper, goal)
        projected, seed, _ = projection.project(target, seed)
        np.testing.assert_allclose(projected[JOINT_COLUMNS], goal[JOINT_COLUMNS], atol=0.3)
        np.testing.assert_array_equal(projected[GRIPPER_COLUMNS], goal[GRIPPER_COLUMNS])
        recovered = label_pose(mapper, projected)
        for start in (0, 10):
            assert np.linalg.norm(recovered[start:start + 3] - target[start:start + 3]) < 0.0005
            delta = rot6d(recovered[start + 3:start + 9]) @ rot6d(target[start + 3:start + 9]).T
            assert Rotation.from_matrix(delta).magnitude() < 0.005


def test_rejects_projection_residual_without_updating_seed(mapper):
    q = np.array([-28, 40, -35, 32, 0, -7, 70, 20, 40, -33, 35, 0, -3, 100], dtype=float)
    seed = np.deg2rad(q[JOINT_COLUMNS]).reshape(2, 6)
    before = seed.copy()
    target = label_pose(mapper, q)
    target[10] += 1.0
    with pytest.raises(ValueError, match="right target projection residual"):
        mapper[2].project(target, seed)
    np.testing.assert_array_equal(seed, before)
