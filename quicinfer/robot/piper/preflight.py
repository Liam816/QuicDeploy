"""Hardware presence check that never sends a robot command.
硬件在位检查：只读取系统信息，绝不向机械臂发送任何指令。

Checks / 检查项:
  * both CAN interfaces exist, are UP at 1 Mbps / 两路 CAN 存在且以 1Mbps UP
  * each CAN's USB adapter serial matches the left/right mapping / USB-CAN 序列号与左右映射一致
  * every camera device path exists / 每个相机设备节点存在
"""
import re
import subprocess
from pathlib import Path


def check_hardware_presence(robot_spec, can_adapter_serials):
    errors = []
    for side in ("left", "right"):
        device = robot_spec[f"{side}_can"]
        result = subprocess.run(["ip", "-details", "link", "show", device], capture_output=True, text=True)
        if result.returncode:
            errors.append(f"{side} CAN {device}: device absent")
            continue
        detail = result.stdout
        if not re.search(r"\bstate UP\b", detail) or not re.search(r"\bbitrate 1000000\b", detail):
            errors.append(f"{side} CAN {device}: require UP at 1 Mbps")
        identity = subprocess.run(
            ["udevadm", "info", "-q", "property", "-p", f"/sys/class/net/{device}"],
            capture_output=True, text=True,
        )
        if f"ID_SERIAL_SHORT={can_adapter_serials[side]}" not in identity.stdout.splitlines():
            errors.append(f"{side} CAN {device}: USB adapter serial does not match left/right mapping")
    for name, camera in robot_spec["cameras"].items():
        path = Path(camera["index_or_path"])
        if not path.exists():
            errors.append(f"{name} camera absent: {path}")
    if errors:
        raise SystemExit("Hardware preflight failed; no robot commands sent:\n" + "\n".join(errors))
    return f"Hardware presence passed: two 1 Mbps CAN interfaces and {len(robot_spec['cameras'])} cameras"
