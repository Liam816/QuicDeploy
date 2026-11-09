"""QuicDeploy command line. / QuicDeploy 命令行入口。

    python -m quicdeploy check          resolve + validate config, no hardware / 校验配置，不连硬件
    python -m quicdeploy show-config    print the resolved config / 打印完整解析后的配置
    python -m quicdeploy preflight      CAN / USB serial / camera presence, no commands / 硬件在位检查
    python -m quicdeploy infer          offline GPU inference on a recorded sample / 离线推理自测
    python -m quicdeploy live --run     REAL ROBOT: enable, home, run the policy / 真机：使能、回零、运行策略
    python -m quicdeploy register-model CARD   freeze model artifact hashes / 冻结模型产物哈希
"""
import argparse
import json
import time

from .common.io import PROJECT_ROOT, canonical

DEFAULT_CONFIG = PROJECT_ROOT / "configs/runtime.yaml"


def _summary(config, **extra):
    return canonical({
        "fingerprint": config["fingerprint"], "mode": config["execution"]["mode"],
        "prefix_steps": config["execution"]["prefix_steps"], "model_id": config["model"]["id"],
        "hardware_connected": False, **extra,
    })


def cmd_infer(config, args):
    """Run the deployed adapter on recorded images, without any robot code.
    用记录下来的图像跑一遍部署适配器，完全不涉及机械臂代码。
    """
    import numpy as np

    from .policy.worker import InferenceWorker

    if args.sample:
        data = np.load(args.sample)
        images = {name: data["image_" + name] for name in config["model"]["cameras"]}
        anchor = data["anchor"]
        prefix = data["prefix_absolute"] if args.with_prefix and len(data["prefix_absolute"]) else None
        step = int(args.step)
    else:
        from .common.geometry import to6

        images = {name: np.zeros((480, 640, 3), np.uint8) for name in config["model"]["cameras"]}
        eye = to6(np.eye(3))
        anchor = np.r_[0, 0, 0, eye, 50, 0, 0, 0, eye, 50].astype(float)
        prefix, step = None, 0
    worker = InferenceWorker(dict(config["model"]))
    try:
        for index in range(args.repeat):
            context = {"seed": 0, "episode": 0, "anchor_index": step,
                       "rng": np.random.default_rng(np.random.SeedSequence([0, step])), "prefix_absolute": prefix}
            started = time.monotonic()
            observation = {"images": images, "eef_state": anchor, "timestamp": 0.0}
            if config["model"].get("temporal_sampling"):
                sampling = config["model"]["temporal_sampling"]
                count, stride = sampling["history_frames"], sampling["history_stride"]
                observation["vision_history"] = {
                    "session": "offline-current-padding", "frame_index": 0, "first_frame_index": 0,
                    "time_offsets": list(range(-(count - 1) * stride, 1, stride)),
                    "samples": [{"frame_index": 0, "images": images}] * count}
            worker.submit(observation, context)
            reply = worker.wait(config["execution"]["max_inference_wait_s"])
            raw = reply["raw"]
            print(canonical({"repeat": index, "roundtrip_s": time.monotonic() - started,
                             "inference_s": reply["inference_s"], "shape": list(raw.shape),
                             "raw_first_row": raw[0].round(6).tolist()}), flush=True)
            if args.output:
                np.save(args.output, raw)
    finally:
        worker.close()


def main(argv=None):
    parser = argparse.ArgumentParser(prog="quicdeploy", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", default=str(DEFAULT_CONFIG), help="runtime YAML / 运行参数 YAML")
    parser.add_argument("--model", help="model card overriding the YAML 'model' entry / 覆盖 YAML 中的模型")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("check")
    sub.add_parser("show-config")
    sub.add_parser("preflight")
    infer = sub.add_parser("infer")
    infer.add_argument("--sample", help="chunk_*.npz with image_<camera> and anchor arrays")
    infer.add_argument("--step", type=int, default=0, help="anchor_index for the noise seed / 噪声种子序号")
    infer.add_argument("--with-prefix", action="store_true", help="use prefix_absolute from the sample")
    infer.add_argument("--repeat", type=int, default=3)
    infer.add_argument("--output", help="save the last raw prediction (.npy)")
    live = sub.add_parser("live")
    live.add_argument("--run", action="store_true", help="connect, enable and home BOTH real arms / 连接并使能双臂")
    live.add_argument("--duration", type=float, default=0, help="seconds, 0 = until Ctrl+C / 运行秒数，0 表示直到 Ctrl+C")
    live.add_argument("--diagnostics", action="store_true", help="save every prediction as npz / 保存每次预测")
    register = sub.add_parser("register-model")
    register.add_argument("card")
    register.add_argument("--relative", action="store_true", help="store portable card-relative artifact paths / 使用可迁移的相对路径")
    args = parser.parse_args(argv)

    if args.command == "register-model":
        from .config.model import register as register_model

        card = register_model(args.card, relative=args.relative)
        print(canonical({"registered": args.card, "artifacts": len(card["artifacts"])}))
        return

    from .config.resolve import resolve

    config = resolve(args.config, model_card=args.model)
    if args.command == "check":
        print(_summary(config, status="passed"))
    elif args.command == "show-config":
        print(json.dumps(config, ensure_ascii=False, indent=2))
    elif args.command == "preflight":
        from .robot.piper.preflight import check_hardware_presence

        print(check_hardware_presence(config["robot"], config["hardware_identity"]["can_adapter_serials"]))
    elif args.command == "infer":
        cmd_infer(config, args)
    elif args.command == "live":
        if not args.run:
            print(_summary(config, status="preflight_only"))
            return
        print(canonical({"selected_model": config["model"]["id"], "mode": config["execution"]["mode"],
                         "prefix_steps": config["execution"]["prefix_steps"]}), flush=True)
        from .runtime.live import LiveSession

        LiveSession(config, diagnostics=args.diagnostics, duration_s=args.duration).run()


if __name__ == "__main__":
    main()
