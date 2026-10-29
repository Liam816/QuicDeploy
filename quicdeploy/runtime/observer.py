"""Build one head-camera-aligned observation from joints and three cameras.

把关节反馈与三路相机对齐为一份以头部相机为时间基准的观测。

sample()  is called every control tick: reads joints, appends any new camera
          frame (by reference, no copy) and checks freshness.
          每个控制节拍调用：读取关节、记录新到的相机帧（只存引用不拷贝）并检查新鲜度。
capture() picks the newest head frame, the nearest frame of every other camera
          and the nearest joint sample, checks skew budgets and copies only the
          selected images.
          选最新的头部帧、其余相机与关节中时间最近的样本，校验时间偏差，只拷贝选中的图像。
"""
import collections
import time

import numpy as np

from ..common.validation import validate_gripper_feedback


class SynchronizedObserver:
    def __init__(self, robot, ik, config):
        self.robot, self.ik = robot, ik
        self.limits, self.guard = config["observation"], config["guard"]
        self.joints = collections.deque(maxlen=96)
        self.frames = {name: collections.deque(maxlen=12) for name in config["model"]["cameras"]}
        self.feedback_metadata = {}

    def _sample_joints(self):
        before = time.monotonic()
        joints = self.robot.read_joints()
        after = time.monotonic()
        if joints.shape != (14,) or not np.isfinite(joints).all():
            raise RuntimeError("Invalid Piper joint/gripper observation")
        age_ms = (after - before) * 1000
        if age_ms > self.limits["max_joint_age_ms"]:
            raise RuntimeError(f"Joint feedback acquisition exceeded age budget: {age_ms:.1f}ms > {self.limits['max_joint_age_ms']}ms")
        validate_gripper_feedback(joints[[6, 13]], self.guard["gripper_widths_mm"],
                                  self.guard.get("gripper_feedback_tolerance_mm", 0))
        # Feedback time = midpoint of the round-trip. / 反馈时间取读数往返的中点。
        self.feedback_metadata = {
            "feedback_time": (before + after) / 2,
            "feedback_read_started_at": before, "feedback_read_finished_at": after,
        }
        self.joints.append((self.feedback_metadata["feedback_time"], joints.copy()))
        return joints

    def sample(self):
        joints = self._sample_joints()
        for name, queue in self.frames.items():
            camera = self.robot.cameras[name]
            if camera.thread is None or not camera.thread.is_alive():
                raise RuntimeError("Camera read thread stopped: " + name)
            with camera.frame_lock:
                image, stamp = camera.latest_frame, camera.latest_timestamp
            if image is not None and stamp is not None and (not queue or stamp > queue[-1][0]):
                queue.append((float(stamp), image))
            if not queue:
                raise RuntimeError("Stale camera frame: " + name + " (no captured frame)")
            age_ms = (time.monotonic() - queue[-1][0]) * 1000
            if age_ms > self.limits["max_camera_age_ms"]:
                raise RuntimeError(f"Stale camera frame: {name}, age={age_ms:.1f}ms > {self.limits['max_camera_age_ms']}ms")
        return joints

    def capture(self):
        """Return (observation, joints) aligned to the newest head frame.
        返回以最新头部帧对齐的 (观测, 关节)。
        """
        heads = self.frames["head"]
        if not heads or not self.joints:
            raise RuntimeError("Synchronized observation buffers are empty")
        head_stamp = heads[-1][0]
        age_ms = (time.monotonic() - head_stamp) * 1000
        if not 0 <= age_ms <= self.limits["max_camera_age_ms"]:
            raise RuntimeError(f"Head camera age {age_ms:.1f}ms exceeds budget {self.limits['max_camera_age_ms']}ms")
        selected = {}
        for name, queue in self.frames.items():
            if not queue:
                raise RuntimeError("Missing camera frame: " + name)
            selected[name] = min(queue, key=lambda frame: abs(frame[0] - head_stamp))
        stamps = {name: frame[0] for name, frame in selected.items()}
        skew_ms = (max(stamps.values()) - min(stamps.values())) * 1000
        if skew_ms > self.limits["max_camera_skew_ms"]:
            raise RuntimeError(f"Camera timestamps exceed skew budget: {skew_ms:.1f}ms > {self.limits['max_camera_skew_ms']}ms, stamps={stamps}")
        joint_stamp, joints = min(self.joints, key=lambda frame: abs(frame[0] - head_stamp))
        joint_age_ms = abs(joint_stamp - head_stamp) * 1000
        if joint_age_ms > self.limits["max_joint_age_ms"]:
            raise RuntimeError(f"No joint feedback aligned with camera observation: delta={joint_age_ms:.1f}ms > {self.limits['max_joint_age_ms']}ms")
        return {
            "images": {name: image.copy() for name, (_, image) in selected.items()},
            "eef_state": self.ik.wrist(joints), "timestamp": head_stamp,
            "joint_timestamp": joint_stamp, "camera_timestamps": stamps, "joint_state": joints.copy(),
        }, joints.copy()
