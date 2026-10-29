"""Timestamped EEF20 trajectory with SO(3)-aware interpolation.
带时间戳的 EEF20 轨迹，支持按任意时刻插值（手腕旋转走球面插值）。
"""
from dataclasses import dataclass

import numpy as np
from scipy.spatial.transform import Rotation

from ..common.geometry import rot6d, slerp_quaternions, validate_pose


@dataclass(frozen=True)
class TimedTrajectory:
    times: np.ndarray   # strictly increasing monotonic-clock seconds / 严格递增的单调时钟秒
    poses: np.ndarray   # [N, 20] absolute world EEF20 / 绝对 world 系位姿

    def __post_init__(self):
        times = np.asarray(self.times, dtype=np.float64)
        poses = validate_pose(self.poses).copy()
        if times.ndim != 1 or len(times) != len(poses) or not np.isfinite(times).all():
            raise ValueError("Trajectory times and EEF20 rows differ")
        if len(times) > 1 and not np.all(np.diff(times) > 0):
            raise ValueError("Target timestamps must increase strictly")
        object.__setattr__(self, "times", times.copy())
        object.__setattr__(self, "poses", poses)
        # Cache wrist quaternions once: [left rows..., right rows...].
        # 预先缓存两只手腕的四元数，排列为 [左臂所有行, 右臂所有行]。
        wrists = np.concatenate((poses[:, 3:9], poses[:, 13:19]), axis=0)
        object.__setattr__(self, "_wrist_quaternions", Rotation.from_matrix(rot6d(wrists)).as_quat())

    def at(self, timestamp):
        """Pose at an absolute time inside coverage, else ValueError.
        返回覆盖范围内某一时刻的位姿；超出范围抛 ValueError。
        """
        t = float(timestamp)
        if not np.isfinite(t) or t < self.times[0] - 1e-5 or t > self.times[-1] + 1e-5:
            raise ValueError("Target time is outside trajectory coverage")
        if len(self.times) == 1:
            return self.poses[0].copy()
        index = int(np.searchsorted(self.times, t))
        if index == 0:
            return self.poses[0].copy()
        if index == len(self.times):
            return self.poses[-1].copy()
        if abs(self.times[index] - t) <= 1e-9:
            return self.poses[index].copy()
        left, right = index - 1, index
        alpha = (t - self.times[left]) / (self.times[right] - self.times[left])
        out = (1 - alpha) * self.poses[left] + alpha * self.poses[right]
        count = len(self.times)
        starts = self._wrist_quaternions[[left, count + left]]
        ends = self._wrist_quaternions[[right, count + right]].copy()
        out[3:9], out[13:19] = slerp_quaternions(starts, ends, alpha)
        return out
