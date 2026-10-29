"""Reuse the reviewed tech-piper startup routines; importing never connects CAN.

复用经审核的 tech-piper 启动流程；导入本模块不会连接 CAN。
Standard library only: native_worker.py imports it as a top-level module.
只依赖标准库：native_worker.py 会把它当作顶层模块导入。
Vendored verbatim from the original backend. / 从原后端原样迁移。
"""

import importlib.util
import json
import logging
import math
import subprocess
import time
from pathlib import Path


def verify_reference(reference, expected):
    ref = Path(reference)
    head = subprocess.check_output(["git", "-C", str(ref), "rev-parse", "HEAD"], text=True).strip()
    if head != expected:
        raise RuntimeError("Native reference commit changed; review adapter compatibility before startup")
    dirty = subprocess.check_output(
        [
            "git",
            "-C",
            str(ref),
            "diff",
            "HEAD",
            "--",
            "src/vr_teleop",
            "robots/piper_x",
            "scripts/vr_startup.py",
            "run/piper_x",
            "scripts/quest3_usb_setup.sh",
        ],
        text=True,
    )
    if dirty:
        raise RuntimeError("Native control source has local changes; review before using this adapter")


def load_startup(reference):
    path = Path(reference) / "scripts/vr_startup.py"
    spec = importlib.util.spec_from_file_location("piper_reference_startup", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def resolve_startup(config):
    if not config.initial_pose_file:
        return None
    startup = load_startup(config.reference_dir)
    pose = startup.load_pose(config.initial_pose_file, config.joint_limits_deg)
    for side in ("left", "right"):
        width = pose["arms"][side]["gripper_max_width_m"] * 1000
        if not math.isclose(width, getattr(config, f"{side}_gripper_width_mm"), abs_tol=1e-6):
            raise ValueError(f"{side}: initial-pose gripper width differs from robot configuration")
        if "initial_gripper_openness" not in pose["arms"][side]:
            raise ValueError(f"{side}: explicit initial gripper opening required")
    ports = startup.resolve_ports(pose)
    for side, port in ports.items():
        setattr(config, f"{side}_can", port)
    return pose


def check_can(ports):
    for port in ports:
        if not (Path("/sys/class/net") / port).is_dir():
            raise RuntimeError(f"Missing CAN interface: {port}")
        link = json.loads(
            subprocess.check_output(
                ["ip", "-details", "-json", "link", "show", "dev", port],
                text=True,
            )
        )[0]
        bitrate = link.get("linkinfo", {}).get("info_data", {}).get("bittiming", {}).get("bitrate")
        if "UP" not in link.get("flags", []) or link.get("mtu") != 16 or bitrate != 1_000_000:
            raise RuntimeError(f"{port} must be UP with classic CAN at 1 Mbps before startup")


def connect_camera(camera, attempts=5):
    """Bounded USB camera recovery before any robot worker is started.

    Reopening a UVC stream immediately after preflight can fail transiently.
    Give the driver 1/2/3/4 seconds between attempts; never substitute devices.
    """
    if attempts < 1:
        raise ValueError("Camera attempts must be positive")
    for attempt in range(1, attempts + 1):
        try:
            camera.connect()
            return
        except (TimeoutError, ConnectionError) as exc:
            if camera.is_connected:
                camera.disconnect()
            if attempt == attempts:
                source = getattr(camera, "index_or_path", None)
                detail = ""
                if isinstance(source, (str, Path)) and str(source).startswith("/dev/"):
                    path = Path(source)
                    detail = f"; device={path}, exists={path.exists()}"
                    if path.exists():
                        detail += f", resolved={path.resolve()}"
                raise ConnectionError(
                    f"Camera startup failed after {attempts} attempts: {camera}{detail}. "
                    "Check USB cable/Hub power and other camera users (fuser /dev/video*). "
                    "No robot worker has been started by this camera check. "
                    f"Last error: {exc}"
                ) from exc
            delay = float(attempt)
            logging.warning(
                "Camera startup failed (%s/%s); retrying %s in %.1fs: %s",
                attempt, attempts, camera, delay, exc,
            )
            time.sleep(delay)
