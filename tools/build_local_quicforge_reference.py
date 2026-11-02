"""Independent QuicForge FP32 reference using data already on the deployment server.
独立 QuicForge FP32 参考：只使用部署机本地已有录制数据，不跨服务器传输图像。
Run under an existing developer env with pyarrow/torchvision, not the lean runtime.
在已有 pyarrow/torchvision 的开发环境中运行；这些依赖不加入精简推理环境。
"""
import argparse
import hashlib
import importlib
import json
import sys
import types
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", required=True, type=Path)
    parser.add_argument("--dataset", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--frames", default="0,40,80")
    args = parser.parse_args()
    import cv2
    import numpy as np
    import pyarrow as pa
    import pyarrow.compute as pc
    import pyarrow.parquet as pq
    import torch
    from PIL import Image
    project = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(project))
    reference = Path(__file__).parent / "reference/quicforge_lva"
    proof = json.loads((reference / "PROVENANCE.json").read_text())
    for file in ("model.py", "decoder.py", "dinov3.py", "task_ids.py"):
        if hashlib.sha256((reference / file).read_bytes()).hexdigest() != proof["files"]["models/lva/" + file]:
            raise ValueError("Original QuicForge reference code changed: " + file)
    if hashlib.sha256((reference / "reference_ops.py").read_bytes()).hexdigest() != proof["reference_ops_sha256"]:
        raise ValueError("Original QuicForge reference operations changed")
    for name in ("quicforge", "quicforge.models", "quicforge.models.lva"):
        module = types.ModuleType(name); module.__path__ = [str(reference)]; sys.modules[name] = module
    # Original imports and original network code, not the deployment backend.
    # 原始 import 和原始网络实现，不使用部署 backend。
    NativeModel = importlib.import_module("quicforge.models.lva.model").LVADecoder
    sys.path.insert(0, str(reference))
    import reference_ops as ops
    from quicdeploy.kinematics.urdf import URDFFK
    from quicdeploy.common.layout import ACTION_KEYS, IK_JOINT_NAMES, JOINT_COLUMNS
    from quicdeploy.common.geometry import to6

    torch.set_num_threads(4)
    torch.backends.cuda.matmul.allow_tf32 = False
    torch.backends.cudnn.allow_tf32 = False
    model_config = json.loads((args.bundle / "model_config.json").read_text()); model_config.pop("type")
    weights_path = args.bundle / "weights.pt"
    weights = torch.load(weights_path, weights_only=True, mmap=True, map_location="cpu")
    with torch.device("meta"):
        model = NativeModel(**model_config)
    model.load_state_dict(weights, strict=True, assign=True)
    model.to("cuda").eval().requires_grad_(False)
    stats = json.loads((args.bundle / "normalization.json").read_text())
    mean, std = np.asarray(stats["mu"], float), np.asarray(stats["std"], float)
    info = json.loads((args.dataset / "meta/info.json").read_text())
    if tuple(info["features"]["observation.state"]["names"]) != ACTION_KEYS:
        raise ValueError("Unexpected measured joint order")
    data = pa.concat_tables([pq.read_table(p, columns=["observation.state", "action", "episode_index", "frame_index", "timestamp"])
                             for p in sorted((args.dataset / "data").rglob("*.parquet"))])
    rows = {r["frame_index"]: r for r in data.filter(pc.equal(data["episode_index"], 0)).to_pylist()}
    meta = pa.concat_tables([pq.read_table(p) for p in sorted((args.dataset / "meta/episodes").rglob("*.parquet"))])
    episode = meta.filter(pc.equal(meta["episode_index"], 0)).to_pylist()[0]
    fk = URDFFK(project / "configs/hardware/piper_x/piper_bimanual.urdf")
    transform = ops.eval_transforms([448, 608])
    args.output.mkdir(parents=True, exist_ok=True)
    proof = json.loads((reference / "PROVENANCE.json").read_text())
    report = {"reference": "Original QuicForge network and evaluator functions on deployment-local task recordings",
              "torch": torch.__version__, "numpy": np.__version__, "device": torch.cuda.get_device_name(),
              "checkpoint_weights_sha256": hashlib.file_digest(weights_path.open("rb"), "sha256").hexdigest(),
              "source_sha256": proof["files"], "dataset": str(args.dataset), "samples": []}
    with torch.inference_mode():
        for frame in map(int, args.frames.split(",")):
            row = rows[frame]; joints = np.asarray(row["observation.state"], float)
            q = dict(zip(IK_JOINT_NAMES, np.deg2rad(joints[JOINT_COLUMNS]), strict=True))
            pose = []
            for side, grip in (("left", 6), ("right", 13)):
                t = fk.link_fk("world", side + "_Link6", q)
                pose.extend(t[:3, 3]); pose.extend(to6(t[:3, :3])); pose.append(row["action"][grip])
            anchor = np.asarray(pose, float)
            images, views = {}, []
            for camera in ("left_wrist", "head", "right_wrist"):
                key = "observation.images." + camera; prefix = "videos/" + key + "/"
                path = args.dataset / info["video_path"].format(video_key=key,
                    chunk_index=int(episode[prefix + "chunk_index"]), file_index=int(episode[prefix + "file_index"]))
                position = round((episode[prefix + "from_timestamp"] + row["timestamp"]) * info["fps"])
                capture = cv2.VideoCapture(str(path)); capture.set(cv2.CAP_PROP_POS_FRAMES, position)
                ok, image = capture.read(); capture.release()
                if not ok:
                    raise RuntimeError(f"Failed to decode {path}, frame {position}")
                image = cv2.cvtColor(image, cv2.COLOR_BGR2RGB)
                images["image_" + camera] = image; views.append(transform(Image.fromarray(image)))
            image = torch.cat(views, dim=-1)[None].cuda()
            noise = torch.randn((1, 50, 20), generator=torch.Generator().manual_seed(frame)).cuda()
            raw = model.inference(image, noise=noise, delay=0, num_steps=10, guidance_scale=0.0)[0].float().cpu().numpy()
            physical = raw * std + mean
            absolute = ops.relative_to_absolute(anchor, physical)[25:35]
            relative = ops.eef_states_relative_to_observation(anchor, absolute)
            prefix = torch.as_tensor((relative - mean) / std, dtype=torch.float32, device="cuda")[None]
            rtc_noise = noise.clone(); rtc_noise[:, :10] = prefix
            rtc = model.inference(image, noise=rtc_noise, action_prefix=prefix, delay=10,
                                  num_steps=10, guidance_scale=0.0)[0].float().cpu().numpy()
            target = args.output / f"episode000_frame{frame:04d}.npz"
            np.savez_compressed(target, **images, anchor=anchor, prefix_absolute=absolute, frame=frame,
                                joint_state=joints, reference_image=image.cpu().numpy(),
                                reference_no_prefix=physical, reference_rtc=rtc * std + mean,
                                reference_normalized=raw)
            report["samples"].append({"file": target.name, "frame": frame,
                                      "sha256": hashlib.file_digest(target.open("rb"), "sha256").hexdigest(),
                                      "rtc_prefix_source": "same-anchor no-prefix prediction rows 25:35"})
            print(target, flush=True)
    (args.output / "reference_provenance.json").write_text(json.dumps(report, indent=2) + "\n")


if __name__ == "__main__":
    main()
