# ruff: noqa: E402
"""Isolated deployment IK worker. No robot, SDK or CAN import.

隔离的部署 IK 子进程：不导入机器人 SDK、不接触 CAN。
Run as a standalone script by quicdeploy.kinematics.ik.Kinematics with the
Python env that owns the compiled `piper_differential_ik` extension.
由 Kinematics 用编译了 piper_differential_ik 扩展的那个 Python 环境以独立脚本方式启动。

Protocol / 协议: one JSON object per line over the inherited socket.
    {"op": "info" | "reset" | "fk" | "step", ...} -> {"ok": bool, "result" | "error"}
"""
import json
import socket
import sys
import tempfile
import time
import xml.etree.ElementTree as ET
from pathlib import Path

import numpy as np
import yaml

sock = socket.socket(fileno=int(sys.argv[1]))
stream = sock.makefile("rwb")
config = json.loads(sys.argv[2])
sys.path.insert(0, config["extension_dir"])
import piper_differential_ik as native_ik

if getattr(native_ik, "deployment_api_version", None) != 1:
    raise RuntimeError("Deployment requires its isolated IK extension; recording extension is unsupported")

temporary = None
solver_yaml = config["yaml"]
native = yaml.safe_load(Path(solver_yaml).read_text())
urdf = Path(native["model"]["urdf_path"])
base_proxies = config.get("native_base_proxies", False)
if "control_dt_s" in config or not urdf.is_absolute() or base_proxies:
    # Write a runtime copy: control-rate dt, and the URDF path made absolute
    # (the native solver would otherwise resolve it against the cwd).
    # 写一份运行时副本：dt 取控制频率，URDF 改为绝对路径（否则原生求解器会相对 cwd 解析）。
    if "control_dt_s" in config:
        dt = float(config["control_dt_s"])
        if not np.isfinite(dt) or not 0 < dt <= 0.05:
            raise ValueError("Invalid control timestep")
        native["ik"]["dt"] = dt
    if not urdf.is_absolute():
        urdf = (Path(solver_yaml).parent / urdf).resolve()
    temporary = tempfile.TemporaryDirectory(prefix="piper-ik-runtime-")
    if base_proxies:
        # The training URDF has only kinematics. The extension requires two
        # base proxies at construction even with collision checks disabled.
        # 训练 URDF 只包含运动学。原生扩展即使关闭碰撞检查，初始化仍要求两底座代理。
        if native["ik"]["check_collisions"] is not False:
            raise ValueError("PiperX EE base proxies are not a full collision model")
        tree = ET.parse(urdf)
        for link_name in native["model"]["shoulder_frames"]:
            base = tree.find(f"link[@name='{link_name}']")
            if base is None or base.find("collision") is not None:
                raise ValueError(f"Unexpected PiperX EE base link: {link_name}")
            collision = ET.SubElement(base, "collision", name=link_name + "_proxy")
            ET.SubElement(collision, "origin", xyz="0 0 0.06", rpy="0 0 0")
            ET.SubElement(ET.SubElement(collision, "geometry"), "box", size="0.04 0.04 0.04")
        urdf = Path(temporary.name) / "native.urdf"
        tree.write(urdf, encoding="utf-8", xml_declaration=True)
    native["model"]["urdf_path"] = str(urdf)
    solver_yaml = str(Path(temporary.name) / "ik.yaml")
    Path(solver_yaml).write_text(yaml.safe_dump(native, sort_keys=False))
solver = native_ik.PiperDifferentialIk(solver_yaml)
for line in stream:
    try:
        request = json.loads(line)
        op = request["op"]
        q = np.asarray(request.get("q", []), dtype=float)
        if op == "info":
            result = {"names": solver.joint_names(), "initial_q": solver.initial_q().tolist(),
                      "joint_damping": solver.joint_damping()}
        elif op == "reset":
            solver.reset(q)
            result = {}
        elif op == "fk":
            result = {"poses": [p.tolist() for p in solver.tcp_poses(q)]}
        elif op == "step":
            started = time.perf_counter()
            result = solver.step(q, np.array(request["left"]), np.array(request["right"]), True, True)
            result["q"] = np.asarray(result["q"]).tolist()
            if "measured_q" in request:
                # Diagnostics only: never reject or pause based on these values.
                result["measured_conditioning"] = solver.conditioning(np.asarray(request["measured_q"], dtype=float))
            result["worker_step_ms"] = (time.perf_counter() - started) * 1000
        else:
            raise ValueError("Unknown IK operation")
        reply = {"ok": True, "result": result}
    except Exception as exc:
        reply = {"ok": False, "error": str(exc)}
    stream.write((json.dumps(reply, allow_nan=False) + "\n").encode())
    stream.flush()

if temporary is not None:
    temporary.cleanup()
