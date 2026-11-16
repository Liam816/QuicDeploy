# 单帧 ViT-B/16 部署

模型目录：`/home/ih/heping/checkpoints/pick2box_action_gt_full_50ep_1027_vitb16_quicforge_update00117150`。
这是 epoch 50 / step 117150 的单帧 `LVADecoder`，不含 Temporal 空间/时序重采样模块。

## 导入与运行

在工控机 QuicDeploy 根目录执行：

```bash
MODEL=pick2box_action_gt_full_50ep_1027_vitb16_quicforge_update00117150
# Initial import only; existing targets are never overwritten.
# 仅首次导入；已生成的目标文件拒绝覆盖。
bash scripts/import_quicforge_model_ckpt.sh "/home/ih/heping/checkpoints/$MODEL"
bash "scripts/start_${MODEL}.sh" --check
# Real motion is a separate operation. / 真机运动单独启动。
QUICDEPLOY_WEBVIZ=1 bash "scripts/start_${MODEL}.sh" --run
```

本次已完成导入，不需重复执行首条命令。正式配置如下，均按传入目录原名生成：

- `configs/models/pick2box_action_gt_full_50ep_1027_vitb16_quicforge_update00117150.json`：FP16 模型卡。
- `configs/pick2box_action_gt_full_50ep_1027_vitb16_quicforge_update00117150.yaml`：继承公共默认值。
- `scripts/start_pick2box_action_gt_full_50ep_1027_vitb16_quicforge_update00117150.sh`：固定该模型的入口。
- `models/quicforge/pick2box_action_gt_full_50ep_1027_vitb16_quicforge_update00117150/`：部署包，Git 忽略大权重。

沿用 async、RTC 10、horizon 50、每块执行 40 行、剩余 15 行预取、policy 30 Hz / control 50 Hz。
归一化、相机顺序、裁剪、锚定坐标、IK、指令保护均沿用既有实现。

## 架构与兼容

| 项目 | ViT-L/16 | ViT-B/16 |
| --- | --- | --- |
| `model_config.json` 的 `backbone` | 缺省或 `dinov3_vitl16` | `dinov3_vitb16` |
| 编码器 | 24 层、1024 维、16 heads | 官方本地实现，12 层、768 维、12 heads |
| 视觉到动作投影 | 1024 → 256 | 768 → 256 |
| patch 特征 | 第 20–23 层求和 | 第 8–11 层求和 |
| LayerScale 个数 | 48 | 24 |

两者都使用 final norm **之前**的最后四层求和，不取均值，也不单独使用最后层。
官方 B 的 API 已去除 CLS/storage tokens，不能再切掉前五个 patch。
ViT-B 的 LayerScale 和 RoPE periods 在 half 转换前保存，之后恢复原始 FP32 值。
单帧和 Temporal 共用这段转换；旧 ViT-L 的转换行为保留。

导出、训练配置和 checkpoint 元数据分别核对 backbone；未知值和不一致值拒绝加载。
完整模型权重严格加载，不用 `strict=False` 忽略结构错误，也不从形状猜 backbone。
B 模型卡额外固定官方源码和共享构造/精度代码。已有模型卡只更新本次改过源码的摘要，
更新前核对旧摘要及其他产物，模型参数、元数据和权重身份保持原值。

## 离线复验

独立参考位于 `tools/reference/quicforge_vitb/`：从当前训练框架 0e75350 保存的原始源码，
未经部署适配修改；这不等于证明它就是历史训练时的精确 revision。
参考使用独立原生网络和既有训练/评估图像、坐标函数，部署实现使用 QuicDeploy 适配器。
两者加载相同完整权重，在本地录制数据 episode 0 的 0/40/80 帧比较，图像不跨服务器传输。

```bash
MODEL=pick2box_action_gt_full_50ep_1027_vitb16_quicforge_update00117150
/home/ih/miniforge3/envs/lerobot/bin/python tools/build_local_quicforge_reference.py \
  --bundle "models/quicforge/$MODEL" \
  --dataset /home/ih/workspace/yihuigou/openarm_workspace/data/lerobot_20261027_144156 \
  --frames 0,40,80 --output outputs/vitb16-validation/reference

# Create a validation-only FP32 card sharing the imported weights.
# 生成只用于验证的 FP32 卡，共用已导入的权重。
/home/ih/miniforge3/envs/quic-deploy/bin/python - <<'PY'
import json
from pathlib import Path
from quicdeploy.config.model import load_model_card, compact_card, register
name = "pick2box_action_gt_full_50ep_1027_vitb16_quicforge_update00117150"
card = load_model_card(Path("configs/models") / (name + ".json"))
card["model"].update(adapter="quicdeploy.policy.quicforge:QuicForgeLVAAdapter", weight_dtype="float32")
out = Path("outputs/vitb16-validation/fp32_card.json")
out.parent.mkdir(parents=True, exist_ok=True)
out.write_text(json.dumps(compact_card({"schema_version": 1, "model": card["model"],
                                      "metadata": card["metadata"]}), indent=2) + "\n")
register(out, relative=True)
PY

/home/ih/miniforge3/envs/quic-deploy/bin/python tools/validate_quicforge.py \
  --fixtures outputs/vitb16-validation/reference --config "configs/$MODEL.yaml" \
  --fp32-card outputs/vitb16-validation/fp32_card.json \
  --fp16-card "configs/models/$MODEL.json" \
  --output outputs/vitb16-validation/numerical --ik-check
```

`fp32_card.json` 是本轮离线验证卡，与正式 FP16 卡共用权重，未另建生产配置或追加精度后缀。
首次导入也可使用 `--precision fp32`；已存在的目录名仍拒绝覆盖。
数值阈值沿用既有门槛：FP32 相对原生参考不超过 0.05 mm / 0.02° / 0.05 mm；
FP16 相对部署 FP32 不超过 1 mm / 0.5° / 1 mm（分别为位置、旋转、夹爪）。
RTC 另检查前 10 步绝对前缀保持，软件 IK/调度/保护检查不发送物理命令。

本次结果见 [验证摘要](validation/vitb16_update00117150.json)。这些检查验证数值和软件链路，
实机任务成功率仍需单独评估；本次未执行 `--run`。

2026-11-16 验证结果：

- 167 项完整软件/WebViz 回归通过，重点测试 131 项通过。
- 新 B 模型 12 组数值对比通过：FP32 输出与独立参考数组完全一致；FP16 最大位置误差 0.2281 mm、
  旋转误差 0.0309°、夹爪误差 0.0771 mm，均低于原有门槛。
- FP32/FP16 的普通/RTC worker 四组、每组重复两次，与直接适配器输出一致；RTC 前缀保持通过。
- ViT-L 和 Temporal 的两种精度、普通/RTC 共八组，修改前后数组完全一致；扩展后的参考工具仍复现旧 L 参考。
- 三组软件 IK/调度/保护共检查 195 条指令；启动脚本 `--check` 通过，发送物理指令 0 条。

Temporal 的兼容性检查使用既有录制图像和合成启动帧索引，不将其称为完整真实历史序列回放。
