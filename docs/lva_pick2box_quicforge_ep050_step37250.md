# LVA pick2box epoch50 / step37250 部署

最新完整 checkpoint：`update_00037250`，epoch 50 / global_step 37250。
模型为单帧 action-GT LVADecoder，默认 FP16，另提供 FP32 参考模型卡。

## 文件与启动

工控机上传原件位于 `/home/ih/heping/checkpoints/lva_pick2box_action_gt_full_50ep_quicforge_20261023`。
原始权重是官方导出包的 `weights.pt`，model 分支、FP32、642 个张量；18 个下载源文件全部通过 SHA-256 校验。
权重 SHA-256 为 `7f8f5418d89cfa4c30d51e0d224ec532925ae9bfef3c104b2cb4230ac7200f5c`。

以下文件相对于 `/home/ih/heping/projects/QuicDeploy`：

| 文件 | 用途 |
| --- | --- |
| `models/quicforge/lva_pick2box_quicforge_ep050_step37250/` | 自持部署包：权重、归一化、结构/采样参数及溯源，不读取老训练项目 |
| `configs/models/lva_pick2box_quicforge_ep050_step37250_fp16.json` | 默认 FP16 模型卡 |
| `configs/models/lva_pick2box_quicforge_ep050_step37250_fp32.json` | FP32 数值参考模型卡 |
| `configs/lva_pick2box_quicforge_ep050_step37250.yaml` | 继承公共配置的本模型 profile |
| `scripts/start_lva_pick2box_37250.sh` | 固定上述 profile 并清除终端遗留的模型覆盖 |

```bash
cd /home/ih/heping/projects/QuicDeploy
bash scripts/start_lva_pick2box_37250.sh --check

# Run inference and control the real arms. / 启动推理并控制真实机械臂。
QUICDEPLOY_WEBVIZ=1 bash scripts/start_lva_pick2box_37250.sh --run
```

入口自动使用 `quic-deploy` 环境。调度为 async，RTC 前缀 10 步，模型 horizon 50，
每块选用 40 行，剩余 20 行时预取；动作播放 30 Hz、IK/MIT 控制 50 Hz。
预取窗口为 20/30≈0.667 秒；既有队列、暂停/恢复、回零、IK 与指令保护共用原实现。

## 官方导出与双数据集契约

本训练 run 使用 `lerobot_20261021_164001_251169` 和 `lerobot_20261021_164745_250005` 两份数据。
导入器支持带校验清单的官方 `weights.pt + assets/` 布局，同时继续支持原始 run 和旧平铺下载布局。
导出 manifest 必须与 checkpoint 的步数、配置哈希、model 权重分支和 FP32 权重哈希相符；
传输清单逐文件复算长度及 SHA-256，所有转换元数据均须被清单覆盖，拒绝越界路径和重复条目。

重复 `--conversion-meta` 按训练数据集顺序提供各自原件。数量、数据集身份与顺序必须对应，
且 fps、机器人类型、运动学/URDF、姿态单位、旋转表示和相机映射必须一致。
部署 `provenance/conversion_meta.json` 顶层保留共同物理字段，`dataset_sources` 保存两份完整原始 JSON 内容。
训练配置和上传原件保持不变。六份既有 QuicForge/Temporal 卡仅刷新校验器源码哈希，其他 artifact pins、model、metadata 均不变。

可复现导入命令（目标目录及模型卡已存在时会拒绝覆盖）：

```bash
PY=/home/ih/miniforge3/envs/quic-deploy/bin/python
CKPT=/home/ih/heping/checkpoints/lva_pick2box_action_gt_full_50ep_quicforge_20261023
$PY tools/prepare_quicforge.py --input "$CKPT" --checkpoint update_00037250 \
  --conversion-meta "$CKPT/provenance/datasets/lerobot_20261021_164001_251169_kvdb/conversion_meta.json" \
  --conversion-meta "$CKPT/provenance/datasets/lerobot_20261021_164745_250005_kvdb/conversion_meta.json" \
  --output models/quicforge/lva_pick2box_quicforge_ep050_step37250 \
  --card-stem lva_pick2box_quicforge_ep050_step37250
```

## 离线验证

- 相关回归 82 passed；完整 `tests webviz/tests` 为 118 passed，前者属于后者的子集。
- 使用独立原 QuicForge 网络和 evaluator 函数，针对本次权重重新生成参考。
  输入为工控机既有录像 `lerobot_20261027_144156` 的 episode0 第 0 / 40 / 80 帧。
- 三帧 × FP32/FP16 × 普通/RTC，共 12 组通过。FP32 输出分量及预处理最大差均为 0；
  FP16 最大位置差约 0.179 mm、旋转差 0.027°、夹爪差 0.612 mm，均通过原有门槛。
- 3 组 FP16 普通输出经过 native IK / guard，共 195 条软件目标通过，物理指令为 0。
- CLI/spawn 连续 3 次 RTC 推理通过，输出与直接 FP16 adapter 逐元素相同，绝对前缀误差约 3.33e-16。
  首请求推理约 0.518 秒，后续约 0.241 秒；该指标不包含 worker 模型初始化，也不代表实机延迟上界。
- 专用脚本在遗留错误 config/model 环境覆盖下 `--check` 通过，两路 1 Mbps CAN 和三相机在位。
  六份既有 QuicForge/Temporal 模型卡仍能解析，原有其他 profile 手动改动保留。

摘要见 [验证记录](validation/lva_pick2box_quicforge_ep050_step37250.json)。
详细报告位于 `outputs/lva-pick2box-37250-validation/`，参考位于 `outputs/lva-pick2box-37250-reference/`。
上述验证覆盖数值迁移和软件控制链路，没有执行实机运动或验证真实任务成功率。
