# QuicForge 1027：epoch50 / step117150 部署

工控机上传目录：`/home/ih/heping/checkpoints/pick2box_action_gt_full_50ep_1027_quicforge`。八个原始文件保持原字节，上传前后均独立计算并核对 SHA-256。
上传 preproc.pt 的 mu/std 与 normalization.json 也已逐元素核对一致。
上传清单 `download_manifest.json` 由本地文件的实际长度和哈希生成，描述本次传输完整性。
checkpoint 为 `update_00117150`，epoch 50 / step 117150，使用 raw model 权重。
权重 SHA-256：`0246295f5d41f40f494cad683ff36dbdc2cf661ff8b9e556466354f3600091f8`。

## 配置与启动

以下路径相对于 `/home/ih/heping/projects/QuicDeploy`：

| 路径 | 用途 |
| --- | --- |
| `models/quicforge/lva_pick2box_1027_quicforge_ep050_step117150/` | 模型加载使用的项目内权重、归一化、结构与溯源 bundle |
| `configs/models/lva_pick2box_1027_quicforge_ep050_step117150_fp16.json` | 默认 FP16 模型卡 |
| `configs/models/lva_pick2box_1027_quicforge_ep050_step117150_fp32.json` | FP32 数值参考模型卡 |
| `configs/lva_pick2box_1027_quicforge_ep050_step117150.yaml` | 继承公共默认值，与旧 1027 对齐的运行 profile |
| `scripts/start_pick2box_1027_quicforge.sh` | 固定新 profile，清除终端旧模型覆盖的启动入口 |

```bash
cd /home/ih/heping/projects/QuicDeploy
bash scripts/start_pick2box_1027_quicforge.sh --check
# Drive the real arms. / 启动推理并驱动真实机械臂。
QUICDEPLOY_WEBVIZ=1 bash scripts/start_pick2box_1027_quicforge.sh --run
```

自动使用 `quic-deploy` 环境。默认 FP16、LayerScale FP32，async RTC 前缀 10、horizon 50、
每块选用 40 行、剩余 15 行预取，动作播放 30 Hz / IK 与 MIT 控制 50 Hz。
execution、postprocess、observation、guard、hold、motion_limits、kinematics 七节与旧 1027 完全相同。
配套 mean/std 与旧 1027 的数值也逐元素相同；两份权重的训练进度不同，不能据此假设预测相同。
新模型使用同五份 Oct26/27 数据：155 段演示、63,981 帧，global batch 为 4×6=24。

## 汇总转换元数据导入

上传 `conversion_meta.json` 以 `quicforge_sources` 保存五份 metadata 与对应 label_file，
`quicforge_primary_source` 为 0。导入工具要求其为整数 0，源数量及顺序与训练 datasets 匹配，
每份 metadata_available 严格为 true 且 metadata 为对象。
在保留原汇总字段的基础上，部署 provenance 文件补充既有 `dataset_sources` 表示。
原有运行时校验继续逐源核对身份、URDF、坐标/单位、相机映射与帧率一致；原上传 JSON 不改写。
本次不改变模型计算、RTC、IK 或指令保护代码，旧模型卡无需重新登记。

可复现导入（已有目标时拒绝覆盖）：

```bash
PY=/home/ih/miniforge3/envs/quic-deploy/bin/python
CKPT=/home/ih/heping/checkpoints/pick2box_action_gt_full_50ep_1027_quicforge
$PY tools/prepare_quicforge.py --input "$CKPT" --checkpoint update_00117150 \
  --conversion-meta "$CKPT/conversion_meta.json" \
  --output models/quicforge/lva_pick2box_1027_quicforge_ep050_step117150 --card-stem lva_pick2box_1027_quicforge_ep050_step117150
```

## 离线验证

- 58 项相关回归通过；完整 `tests webviz/tests` 125 passed，相关回归属于完整回归子集。
- 针对本次权重，用独立原 QuicForge 网络与 evaluator 函数重新生成三帧参考；
  输入为工控机已有录像 `lerobot_20261027_144156` episode0 的第 0 / 40 / 80 帧。
- FP32/FP16 × 普通/RTC 共 12 组通过；FP32 输出分量最大差为 0，
  预处理最大差为 0。
  FP16 最大位置差 0.205594 mm、旋转差 0.031295°、夹爪差 0.028574 mm，
  均通过既有 1 mm / 0.5° / 1 mm 门槛。
- native IK / guard 共 195 条软件目标通过，物理指令为 0。
- CLI/spawn 连续三次 RTC 推理通过，输出与直接 FP16 adapter 逐元素相同；
  三次 inference_s 为 0.516419, 0.240053, 0.240979 秒，不包含 worker 模型初始化。
- 专用脚本带错误旧 model/config 环境覆盖执行 `--check` 通过；两路 1 Mbps CAN、三相机在位。
- 原有 33 个配置/脚本的哈希保持不变，包括两份原有手动修改的运行 YAML；旧 1027 入口保留。

验证摘要见 [记录](validation/lva_pick2box_1027_quicforge_ep050_step117150.json)，详细输出位于
`outputs/pick2box-1027-quicforge-117150-validation/`，参考位于同名前缀的 `-reference/`。
本次验证覆盖数值迁移与软件链路，未执行实机运动或验证任务成功率。
