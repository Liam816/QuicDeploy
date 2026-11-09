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
import uuid

import numpy as np

from ..common.validation import validate_gripper_feedback


class SynchronizedObserver:
    def __init__(self, robot, ik, config):
        self.robot, self.ik = robot, ik
        self.limits, self.guard = config["observation"], config["guard"]
        self.joints = collections.deque(maxlen=96)
        self.temporal = config["model"].get("temporal_sampling")
        self.session = uuid.uuid4().hex
        self.head_indices = {}
        self.first_frame_index = None
        capacity = 12
        if self.temporal:
            capacity += (self.temporal["history_frames"] - 1) * self.temporal["history_stride"]
            for camera in robot.cameras.values():
                if camera.fps != self.temporal["source_fps"]:
                    raise ValueError("Temporal camera fps must equal the training source fps")
                camera.enable_history(capacity)
        self.frames = {name: collections.deque(maxlen=capacity) for name in config["model"]["cameras"]}
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
                acquired = tuple(camera.frame_history) if self.temporal else ()
            if self.temporal:
                for index, frame_stamp, frame in acquired:
                    if not queue or frame_stamp > queue[-1][0]:
                        queue.append((float(frame_stamp), frame))
                        if name == "head":
                            self.head_indices[float(frame_stamp)] = index
                            if self.first_frame_index is None:
                                self.first_frame_index = index
                if name == "head" and queue:
                    oldest = queue[0][0]
                    self.head_indices = {s: i for s, i in self.head_indices.items() if s >= oldest}
            elif image is not None and stamp is not None and (not queue or stamp > queue[-1][0]):
                queue.append((float(stamp), image))
            if not queue:
                raise RuntimeError("Stale camera frame: " + name + " (no captured frame)")
            age_ms = (time.monotonic() - queue[-1][0]) * 1000
            if age_ms > self.limits["max_camera_age_ms"]:
                raise RuntimeError(f"Stale camera frame: {name}, age={age_ms:.1f}ms > {self.limits['max_camera_age_ms']}ms")
        return joints

    def _prediction_stamp(self):
        if not self.frames["head"]:
            return None
        if not self.temporal:
            return self.frames["head"][-1][0]
        stride = self.temporal["history_stride"]
        for stamp, _ in reversed(self.frames["head"]):
            if (self.head_indices[stamp] - self.first_frame_index) % stride == 0:
                if (time.monotonic() - stamp) * 1000 <= self.limits["max_camera_age_ms"]:
                    return stamp
                return None
        return None

    def prediction_ready(self):
        """Wait for a fresh anchor on one source-stride phase, preserving cache reuse."""
        return self._prediction_stamp() is not None

    def capture(self, *, for_prediction=False):
        """Return (observation, joints) aligned to the newest head frame.
        返回以最新头部帧对齐的 (观测, 关节)。
        """
        heads = self.frames["head"]
        if not heads or not self.joints:
            raise RuntimeError("Synchronized observation buffers are empty")
        head_stamp = self._prediction_stamp() if for_prediction else heads[-1][0]
        if head_stamp is None:
            raise RuntimeError("No fresh temporal anchor on the sampling grid")
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
        observation = {
            "images": {name: image.copy() for name, (_, image) in selected.items()},
            "eef_state": self.ik.wrist(joints), "timestamp": head_stamp,
            "joint_timestamp": joint_stamp, "camera_timestamps": stamps, "joint_state": joints.copy(),
        }
        if self.temporal:
            observation["vision_history"] = self._history(head_stamp, observation["images"])
        return observation, joints.copy()

    def _history(self, head_stamp, current_images):
        current = self.head_indices[head_stamp]
        count, stride = self.temporal["history_frames"], self.temporal["history_stride"]
        offsets = list(range(-(count - 1) * stride, 1, stride))
        heads = {self.head_indices[stamp]: (stamp, image) for stamp, image in self.frames["head"]}
        samples = []
        for offset in offsets:
            wanted = current + offset
            if wanted < self.first_frame_index:
                samples.append({"frame_index": current, "images": current_images})
                continue
            if wanted not in heads:
                raise RuntimeError(f"Temporal acquisition gap at source frame {wanted}; restart the session")
            stamp, head = heads[wanted]
            images, stamps = {"head": head}, [stamp]
            for name, queue in self.frames.items():
                if name == "head":
                    continue
                other_stamp, image = min(queue, key=lambda f: abs(f[0] - stamp))
                images[name] = image
                stamps.append(other_stamp)
            if (max(stamps) - min(stamps)) * 1000 > self.limits["max_camera_skew_ms"]:
                raise RuntimeError(f"Temporal historical camera skew at source frame {wanted}")
            samples.append({"frame_index": wanted, "images": images})
        # Camera frames are immutable arrays after publication; retain references
        # here. Pickling/copying happens on InferenceWorker's sender thread.
        return {"session": self.session, "frame_index": current, "first_frame_index": self.first_frame_index,
                "time_offsets": offsets, "samples": samples}
