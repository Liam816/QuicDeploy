"""Synthetic and diagnostic replay producers. Neither opens hardware nor loads weights."""
import base64
import time
import uuid
from pathlib import Path

import cv2
import numpy as np

from quicdeploy.common.geometry import to6
from quicdeploy.kinematics.urdf import URDFFK
from quicdeploy.runtime.inference_metrics import InferenceMetrics

ROOT = Path(__file__).resolve().parent.parent


def jpeg(rgb):
    ok, data = cv2.imencode(".jpg", cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR), [cv2.IMWRITE_JPEG_QUALITY, 80])
    if not ok:
        raise RuntimeError("JPEG encoding failed")
    return base64.b64encode(data).decode("ascii")


def demo_prediction(step, now, fk):
    joints = np.zeros((50, 14))
    for k in range(50):
        phase = step * 0.4 + k * 0.025
        for base in (0, 7):
            joints[k, base:base + 6] = [8 * np.sin(phase), 40 + 10 * np.sin(phase * .8),
                                       -50 + 8 * np.cos(phase), 40, 5 * np.sin(phase), 0]
            joints[k, base + 6] = 35 + 15 * np.sin(phase)
    poses = []
    for row in joints:
        values = {f"{side}_joint{i + 1}": float(np.deg2rad(row[base + i]))
                  for side, base in (("left", 0), ("right", 7)) for i in range(6)}
        pose = []
        for side, base in (("left", 0), ("right", 7)):
            transform = fk.link_fk("world", f"{side}_Link6", values)
            pose.extend([*transform[:3, 3], *to6(transform[:3, :3]), row[base + 6]])
        poses.append(pose)
    times = (now + np.arange(50) / 30).tolist()
    return {"step": step, "observation_time": now, "received_at": now,
            "anchor": poses[0], "joint_state": joints[0].tolist(),
            "u": {"times": times, "poses": poses}, "e": {"times": times[:40], "poses": poses[:40]},
            "roundtrip_ms": 123, "inference_ms": 110, "expired_steps": 0,
            "fused_steps": 3, "selected_steps": 40, "truncated_steps": 10, "prefix_source_rows": []}


def demo_camera(name, step, stamp):
    image = np.full((360, 640, 3), (23, 31, 38), np.uint8)
    for x in range(0, 640, 40):
        cv2.line(image, (x, 0), (x, 360), (36, 47, 56), 1)
    for y in range(0, 360, 40):
        cv2.line(image, (0, y), (640, y), (36, 47, 56), 1)
    offset = int(65 * np.sin(step / 12))
    cv2.rectangle(image, (270 + offset, 145), (345 + offset, 220), (205, 151, 63), 2)
    cv2.line(image, (90, 330), (220 + offset, 235), (83, 196, 179), 8)
    cv2.line(image, (550, 330), (385 + offset, 235), (107, 156, 223), 8)
    cv2.putText(image, "SYNTHETIC / NO CAMERA", (24, 34), cv2.FONT_HERSHEY_SIMPLEX, .65, (230, 234, 238), 1)
    cv2.putText(image, name.upper(), (24, 325), cv2.FONT_HERSHEY_SIMPLEX, .6, (154, 172, 186), 1)
    return {"timestamp": stamp, "width": 640, "height": 360, "preview_fps": 8, "jpeg": jpeg(image)}


def replay_prediction(file, now):
    with np.load(file, allow_pickle=False) as data:
        origin = float(data["observation_time"])
        offset = now - origin
        result = {"step": int(file.stem.split("_")[-1]), "observation_time": now,
                  "received_at": float(data["received_at"]) + offset,
                  "anchor": data["anchor"].tolist(),
                  "joint_state": data["observation_joint_state"].tolist() if "observation_joint_state" in data else [],
                  "roundtrip_ms": float(np.nan_to_num(data.get("roundtrip_s", 0))) * 1000,
                  "inference_ms": float(np.nan_to_num(data.get("inference_s", 0))) * 1000}
        for key in ("u", "e"):
            result[key] = {"times": (data[f"{key}_times"] + offset).tolist(),
                           "poses": data[f"{key}_poses"].tolist()}
        for key in ("expired_steps", "fused_steps", "selected_steps", "truncated_steps"):
            result[key] = int(data.get(key, 0))
        result["prefix_source_rows"] = data.get("prefix_source_rows", np.array([])).tolist()
        cameras = {str(name): {"timestamp": now, "width": int(data["image_" + str(name)].shape[1]),
                              "height": int(data["image_" + str(name)].shape[0]), "preview_fps": 1,
                              "jpeg": jpeg(data["image_" + str(name)])}
                   for name in data["camera_names"]}
    return result, cameras


def feed(state, replay=None):
    try:
        files = sorted(Path(replay).glob("chunk_*.npz")) if replay else []
        if replay and not files:
            raise ValueError(f"No chunk_*.npz files in {replay}; record with PIPER_DIAGNOSTICS=1")
        fk = None if replay else URDFFK(ROOT / "configs/hardware/piper_x/piper_bimanual.urdf")
        session_id = uuid.uuid4().hex
        current = previous = None
        metrics = InferenceMetrics(log=False)
        tick = 0
        while True:
            now = time.monotonic()
            if tick % 8 == 0:
                previous = current
                if replay:
                    current, cameras = replay_prediction(files[(tick // 8) % len(files)], now)
                else:
                    current = demo_prediction(tick // 8, now, fk)
                metrics.record({"roundtrip_s": current["roundtrip_ms"] / 1000,
                                "inference_s": current["inference_ms"] / 1000},
                               step=current["step"])
            if not replay:
                cameras = {name: demo_camera(name, tick, now) for name in ("head", "left_wrist", "right_wrist")}
            state.ingest({"session_id": session_id, "source": "replay" if replay else "demo", "status": "offline",
                          "model": Path(replay).name if replay else "Synthetic dual Piper-X",
                          "mode": "replay" if replay else "demo", "control_hz": 0, "policy_hz": 30,
                          "finished": False, "inference": metrics.snapshot(),
                          "current": current, "previous": previous, "cameras": cameras,
                          "robot": {"joints": current["joint_state"], "command": None}})
            tick += 1
            time.sleep(0.125)
    except Exception as error:
        state.ingest({"session_id": "offline-error", "source": "replay" if replay else "demo",
                      "status": "failed", "finished": True, "current": None, "previous": None,
                      "cameras": {}, "robot": None, "error": str(error)})
