# Extracted verbatim from the original QuicForge functions; developer reference only.
# 从原始 QuicForge 函数逐字提取；只用于独立离线参考。
import numpy as np
from torchvision import transforms
ACTION_DIM = 20
POSITION_SLICES = (slice(0,3), slice(10,13))
ROTATION_SLICES = (slice(3,9), slice(13,19))
ROTATION_EPS = 1e-9
IMAGENET_MEAN = [0.485, 0.456, 0.406]
IMAGENET_STD = [0.229, 0.224, 0.225]

def rotation_6d_to_matrix(rotation_6d):
    """Decode first-two-column rotation-6D values with Gram-Schmidt.

    This is the NumPy equivalent of
    ``LvaCppIkDecoder::Rotation6dToMatrix`` in ``lva_ros2_ws``.
    """
    values = np.asarray(rotation_6d, dtype=np.float64)
    if values.ndim < 1 or values.shape[-1] != 6:
        raise ValueError(f"expected rotation-6D values ending in 6, got {values.shape}")
    if not np.all(np.isfinite(values)):
        raise ValueError("rotation-6D values must be finite")

    first = values[..., :3]
    first_norm = np.linalg.norm(first, axis=-1, keepdims=True)
    if np.any(first_norm < ROTATION_EPS):
        raise ValueError("rotation-6D first column is degenerate")
    column0 = first / first_norm

    second = values[..., 3:]
    orthogonal = second - np.sum(column0 * second, axis=-1, keepdims=True) * column0
    second_norm = np.linalg.norm(orthogonal, axis=-1, keepdims=True)
    if np.any(second_norm < ROTATION_EPS):
        raise ValueError("rotation-6D columns are parallel or degenerate")
    column1 = orthogonal / second_norm
    column2 = np.cross(column0, column1)
    return np.stack((column0, column1, column2), axis=-1)


def rotation_matrix_to_6d(rotation):
    """Encode the first two matrix columns in the runtime's 6-D layout."""
    values = np.asarray(rotation, dtype=np.float64)
    if values.ndim < 2 or values.shape[-2:] != (3, 3):
        raise ValueError(f"expected rotation matrices ending in (3, 3), got {values.shape}")
    if not np.all(np.isfinite(values)):
        raise ValueError("rotation matrices must be finite")
    return np.concatenate((values[..., :, 0], values[..., :, 1]), axis=-1)


def eef_states_relative_to_observation(observation, future_states):
    """Map future absolute EEF rows (measured states or commands) to the target domain.

    Args:
        observation: Measured EEF state shaped (20,), or one measured state per
            future row shaped (T, 20).
        future_states: Measured EEF rows shaped (T, 20), beginning one
            controller step after the observation.

    Returns:
        Array shaped (T, 20). Every position row uses the same corresponding
        observation anchor, rotations use ``R_rel = R_abs @ R_ref.T``, and
        measured claw states remain absolute. Rows are never differenced
        consecutively.
    """
    anchors = np.asarray(observation, dtype=np.float64)
    states = np.asarray(future_states, dtype=np.float64)
    if states.ndim != 2 or states.shape[1] != ACTION_DIM:
        raise ValueError(f"expected future states shaped (T, 20), got {states.shape}")
    if anchors.shape == (ACTION_DIM,):
        anchors = np.broadcast_to(anchors, states.shape)
    elif anchors.shape != states.shape:
        raise ValueError(
            f"expected observation shaped (20,) or {states.shape}, got {anchors.shape}"
        )
    if not np.all(np.isfinite(anchors)) or not np.all(np.isfinite(states)):
        raise ValueError("observation and future states must be finite")

    relative = states.copy()
    for position in POSITION_SLICES:
        relative[:, position] = states[:, position] - anchors[:, position]
    for rotation in ROTATION_SLICES:
        absolute_matrix = rotation_6d_to_matrix(states[:, rotation])
        anchor_matrix = rotation_6d_to_matrix(anchors[:, rotation])
        relative_matrix = absolute_matrix @ np.swapaxes(anchor_matrix, -1, -2)
        relative[:, rotation] = rotation_matrix_to_6d(relative_matrix)
    return relative


def relative_to_absolute(observation, relative):
    anchor = np.asarray(observation, dtype=np.float64)
    relative = np.asarray(relative, dtype=np.float64)
    absolute = relative.copy()
    for position in POSITION_SLICES:
        absolute[:, position] += anchor[position]
    for rotation in ROTATION_SLICES:
        absolute[:, rotation] = rotation_matrix_to_6d(
            rotation_6d_to_matrix(relative[:, rotation]) @ rotation_6d_to_matrix(anchor[rotation]))
    return absolute


def eval_transforms(crop_size: list[int]):
    """Per-camera evaluation preprocessing: CenterCrop -> ToTensor -> ImageNet Normalize."""
    from torchvision import transforms

    return transforms.Compose([transforms.CenterCrop(tuple(crop_size)), transforms.ToTensor(),
                               transforms.Normalize(mean=IMAGENET_MEAN, std=IMAGENET_STD)])

