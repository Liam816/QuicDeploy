"""Minimal URDF forward kinematics (revolute / continuous / prismatic / fixed).
最小化的 URDF 正运动学实现，用于把关节角换算为 world 系 Link6 位姿。
Same FK as the dataset converter, so model anchors match training data.
与数据集转换器的 FK 完全一致，保证部署时的锚点与训练数据同源。
"""
from __future__ import annotations

import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path

import numpy as np


def rot_x(angle: float) -> np.ndarray:
    c, s = np.cos(angle), np.sin(angle)
    return np.array([[1, 0, 0], [0, c, -s], [0, s, c]], dtype=np.float64)


def rot_y(angle: float) -> np.ndarray:
    c, s = np.cos(angle), np.sin(angle)
    return np.array([[c, 0, s], [0, 1, 0], [-s, 0, c]], dtype=np.float64)


def rot_z(angle: float) -> np.ndarray:
    c, s = np.cos(angle), np.sin(angle)
    return np.array([[c, -s, 0], [s, c, 0], [0, 0, 1]], dtype=np.float64)


def rpy_to_rot(roll: float, pitch: float, yaw: float) -> np.ndarray:
    return rot_z(yaw) @ rot_y(pitch) @ rot_x(roll)


def make_transform(xyz: list[float], rpy: list[float]) -> np.ndarray:
    transform = np.eye(4, dtype=np.float64)
    transform[:3, :3] = rpy_to_rot(*rpy)
    transform[:3, 3] = xyz
    return transform


def rot_axis_angle(axis: list[float], angle: float) -> np.ndarray:
    """Rodrigues rotation about a unit axis. / Rodrigues 公式：绕单位轴旋转。"""
    axis_arr = np.asarray(axis, dtype=np.float64)
    axis_arr = axis_arr / np.linalg.norm(axis_arr)
    k = np.array(
        [
            [0, -axis_arr[2], axis_arr[1]],
            [axis_arr[2], 0, -axis_arr[0]],
            [-axis_arr[1], axis_arr[0], 0],
        ],
        dtype=np.float64,
    )
    return np.eye(3, dtype=np.float64) + np.sin(angle) * k + (1 - np.cos(angle)) * (k @ k)


@dataclass(frozen=True)
class Joint:
    name: str
    joint_type: str
    parent: str
    child: str
    origin: np.ndarray
    axis: list[float]
    position_limits: tuple[float, float] | None = None


class URDFFK:
    def __init__(self, urdf_path: Path) -> None:
        self.joints: dict[str, Joint] = {}
        self.joint_by_child: dict[str, Joint] = {}
        self._parse_urdf(urdf_path)

    def _parse_urdf(self, urdf_path: Path) -> None:
        root = ET.parse(urdf_path).getroot()
        for joint_elem in root.iter("joint"):
            name = str(joint_elem.get("name"))
            joint_type = str(joint_elem.get("type"))
            parent_elem = joint_elem.find("parent")
            child_elem = joint_elem.find("child")
            origin_elem = joint_elem.find("origin")
            axis_elem = joint_elem.find("axis")
            limit = joint_elem.find("limit")
            xyz = (
                [float(value) for value in origin_elem.get("xyz", "0 0 0").split()]
                if origin_elem is not None
                else [0.0, 0.0, 0.0]
            )
            rpy = (
                [float(value) for value in origin_elem.get("rpy", "0 0 0").split()]
                if origin_elem is not None
                else [0.0, 0.0, 0.0]
            )
            axis = (
                [float(value) for value in axis_elem.get("xyz", "0 0 1").split()]
                if axis_elem is not None
                else [0.0, 0.0, 1.0]
            )
            joint = Joint(
                name=name,
                joint_type=joint_type,
                parent=parent_elem.get("link") if parent_elem is not None else "",
                child=child_elem.get("link") if child_elem is not None else "",
                origin=make_transform(xyz, rpy),
                axis=axis,
                position_limits=(float(limit.attrib["lower"]), float(limit.attrib["upper"]))
                if limit is not None and {"lower", "upper"} <= limit.attrib.keys() else None,
            )
            if joint.child in self.joint_by_child:
                raise ValueError(f"URDF child link {joint.child!r} has more than one parent joint")
            self.joints[name] = joint
            self.joint_by_child[joint.child] = joint

    def link_chain(self, base_link: str, end_link: str) -> list[Joint]:
        chain = []
        current_link = end_link
        visited = set()
        while current_link != base_link:
            if current_link in visited:
                raise ValueError(f"URDF cycle while resolving {base_link!r} -> {end_link!r}")
            visited.add(current_link)
            joint = self.joint_by_child.get(current_link)
            if joint is None:
                raise ValueError(
                    f"No URDF chain from {base_link!r} to {end_link!r}; stopped at {current_link!r}"
                )
            chain.append(joint)
            current_link = joint.parent
        return list(reversed(chain))

    def link_fk(self, base_link: str, end_link: str, values: dict[str, float]) -> np.ndarray:
        """4x4 transform of end_link in base_link. / end_link 相对 base_link 的齐次变换。"""
        transform = np.eye(4, dtype=np.float64)
        for joint in self.link_chain(base_link, end_link):
            transform = transform @ joint.origin
            if joint.joint_type in {"revolute", "continuous"}:
                if joint.name not in values:
                    raise ValueError(f"Missing value for movable URDF joint {joint.name!r}")
                angle = float(values[joint.name])
                rot = np.eye(4, dtype=np.float64)
                rot[:3, :3] = rot_axis_angle(joint.axis, angle)
                transform = transform @ rot
            elif joint.joint_type == "prismatic":
                if joint.name not in values:
                    raise ValueError(f"Missing value for movable URDF joint {joint.name!r}")
                translation = np.eye(4, dtype=np.float64)
                translation[:3, 3] = np.asarray(joint.axis, dtype=np.float64) * float(values[joint.name])
                transform = transform @ translation
            elif joint.joint_type != "fixed":
                raise ValueError(f"Unsupported URDF joint type {joint.joint_type!r} for {joint.name!r}")
        return transform
