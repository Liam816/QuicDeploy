# Temporal-LVA 部署适配

## 训练契约与缓存

已核对 QuicForge 的 `configs/temporal-lva/pick_kun_once_hf16_hs8_ws1_e50_bs20.py` 实际参数，配置开头的 10 帧说明已经过时。部署读取完整 checkpoint 的训练配置、模型配置、归一化元数据和数据转换元数据，三者不一致时拒绝加载。

| 参数 | 本次配置 |
|---|---|
| 模型 | 官方 DINOv3 ViT-B/16 → 空间 queries → 时序 queries → LVA 动作解码器 |
| history_frames / history_stride / window_stride | 16 / 8 / 1 |
| 原始视频帧率 / 历史采样频率 | 30 Hz / 3.75 Hz |
| 历史索引（由旧到新） | t−120, t−112, …, t−8, t |
| 历史覆盖 | 4 秒，含当前帧 |
| 单帧空间 token | 96 × 256 |
| 图像 | 左腕、头部、右腕，各中心裁剪 448×608，ImageNet 归一化，沿宽拼接 |
| 输出 | 50×20，实测 state 为锚点的相对 EEF，物理量纲 |

相机读帧线程使用原始采集计数，独立保留至少 121 帧，推理阻塞期间仍持续采集。观测器按头部帧索引选择历史，其余相机按时间戳匹配，并检查偏差预算。只在工作进程中缓存 `_encode_frame()` 的空间 token；每次推理重新添加相对帧索引时间编码，再做时序融合和 flow sampling。缓存按原始帧龄裁剪，最多保留当前历史跨度内的 token，计算过的帧不会重复编码。

部署请求锚点对齐到本会话首帧起每 8 帧的网格，并保留原有相机新鲜度检查。这样相邻请求的历史处于同一采样相位，通常只需编码新增的 2–4 帧。没有新鲜网格帧时控制环继续播放原动作队列，等下一帧到来再提交。训练 `window_stride=1` 控制训练候选窗口，不表示部署需要每 1/30 秒重算动作块。实际新增帧数取决于请求间隔；推理停顿超过整个历史跨度时仍需重建历史。

## 启动和恢复

用户指定启动策略：目标历史帧早于本会话首帧时，使用**本次当前帧**的空间 token 补齐，保持完整 16 个有效槽位和 `[-120,-112,…,0]` 时间偏移。启动只编码当前帧一次。随着真实历史积累，逐槽替换 padding，不用最早帧永久占位。

这与训练数据的边界策略有意不同：训练 `history_policy=complete` 跳过每段前 120 个 anchor，训练样本没有启动 padding。部署不等待四秒暖机，也不使用 masked/zero padding。

暂停、回初始位、恢复会重置原图窗口与 token 会话；恢复首块重新采用当前帧 padding。运行期间真实采集帧丢失、历史相机时间偏差超限会报错，不伪造为启动 padding。

## 导入和选择模型

新增 `quicdeploy.policy.temporal:TemporalLVAAdapter`（FP32）和 `TemporalLVAFP16Adapter`。已有单帧 LVA / QuicForge 卡继续走原适配器。官方骨干推理源码冻结在 `third_party/dinov3`，包含许可和文件哈希；未声称该源码快照就是历史训练 commit。完整权重必须严格匹配全部键，不加载额外预训练文件，也不联网下载。

使用完整训练 run（包含 resolved config、checkpoint manifest、backend/model.pt、assets/normalization.json）导入；只拿一个 model.pt 不足以推断完整部署契约：

```bash
cd /home/ih/heping/projects/QuicDeploy
PY=/home/ih/miniforge3/envs/quic-deploy/bin/python
$PY tools/prepare_quicforge.py \
  --input /path/to/temporal-training-run \
  --checkpoint update_XXXXXXXX \
  --conversion-meta /path/to/matching/conversion_meta.json \
  --output models/quicforge/temporal_lva_pick_kun \
  --card-stem temporal_lva_pick_kun
$PY -m quicdeploy --config configs/temporal_lva_pick_kun.yaml check
$PY -m quicdeploy --config configs/temporal_lva_pick_kun.yaml infer --repeat 3
```

导入器自动按模型类型路由，生成 FP32 / FP16 模型卡和独立时序 runtime。时序 runtime 提前到剩余 23 步时预取，以容纳网格等待与新增帧编码；原 `runtime.yaml` 不变。模型卡同时绑定训练采样参数、权重、归一化、转换几何以及推理源码哈希。`infer` 的单帧输入明确执行启动 padding；它不是完整历史录像回放。

实机运行仍通过 `live --run`，需使用新生成的时序配置并由操作者值守。本次没有启动机械臂、相机或 CAN，也未替换默认运行模型。

## 离线验证与边界（2026-11-09）

使用本地已下载的 `pick_kun_once_hf16_hs8_ws1_bs10_iter10w/update_00050000` 原始权重，SHA-256 为 `858a75173434952bc08583502cd2797e06cab984c0a7129586a6f764c1185876`，473 个权重键严格加载。它与用户指定的 e50 配置属于不同训练 run，仅用于同结构验证，不能冒充目标 e50 权重。

在工控机 RTX 3060 Laptop GPU、torch 2.11.0+cu128、完整 `1×16×3×448×1824` 输入下，用独立保存的本地 QuicForge 模型源码对比：

| 检查 | 结果 |
|---|---|
| FP32 缓存 context / 动作输出 / 当前帧 padding | 最大绝对误差均为 0 |
| RTC 10 步干净前缀 | 逐位一致 |
| FP32 完整 16 帧编码 / 缓存融合与采样 | 约 3.91 s / 28 ms |
| FP16 完整 16 帧编码 / 缓存融合与采样 | 约 1.06 s / 46 ms |
| FP16 相对 FP32 的 normalized action 差异 | max 约 0.005055，mean 约 0.000525 |

FP16 保留官方 ViT-B 的 24 个 LayerScale gamma 及 RoPE periods 为 FP32，转换前先保存原值，避免先转 half 溢出或舍入后再转回。FP16 与 FP32 不是逐位等价，表中误差不等于实际位置/旋转误差，也不代表实机任务成功率。

验证报告保存在 `outputs/temporal-adaptation-20261109/numerical_validation.json`。复验工具为 `tools/validate_temporal.py`；启动 padding、缓存复用/淘汰、source-frame 采样、相位与新鲜度、模型卡契约覆盖在 `tests/test_temporal.py`。实机控制周期和具体目标 e50 权重仍须在完整资产导入后验证。


## update_00050000：已接入配置（2026-11-09）

本次基于 `feat/adapt_temporal_vla_infer` 的 `dc07791` 接入实际上传资产：
`/home/ih/heping/checkpoints/Temporal-LVA_update_00050000`。checkpoint 身份为 **epoch 51 / step 50000**。
生成的 FP32/FP16 卡分别是 `configs/models/temporal_lva_update_00050000_{fp32,fp16}.json`，
运行配置为 `configs/temporal_lva_update_00050000.yaml`，部署包为 `models/quicforge/temporal_lva_update_00050000/`。
原始上传目录保留；默认 `runtime.yaml` 的模型选择未改动。

```bash
cd /home/ih/heping/projects/QuicDeploy
conda activate quic-deploy
bash scripts/start_temporal_lva_50000.sh --check
# 下列命令会连接、使能并回零机械臂，需操作者值守。
bash scripts/start_temporal_lva_50000.sh --run
```

专用脚本固定使用本模型配置，并清除终端遗留的模型覆盖。
实际参数：16 帧 / stride 8 / window 1；控制 50 Hz、动作行播放 30 Hz；async + RTC 前缀 10；
每块选用 40 行，剩余 23 行时预取；启动/恢复采用当前帧 padding，复用现有 token 缓存、IK 与指令保护。
硬件在位检查已通过（两路 1 Mbps CAN、三路相机），未连接或运动真实机械臂。

### 从平铺上传目录导入

```bash
PY=/home/ih/miniforge3/envs/quic-deploy/bin/python
$PY tools/prepare_quicforge.py \
  --input /home/ih/heping/checkpoints/Temporal-LVA_update_00050000 \
  --checkpoint update_00050000 \
  --conversion-meta /home/ih/heping/checkpoints/Temporal-LVA_update_00050000/conversion_meta.json \
  --output models/quicforge/temporal_lva_update_00050000 \
  --card-stem temporal_lva_update_00050000
```

该命令已执行；目标包/模型卡存在时会拒绝覆盖。平铺导入要求 `download_manifest.json` 覆盖全部必要资产，
逐文件重算大小与 SHA-256，拒绝重复/越界路径，并核对 checkpoint 名称、训练配置规范哈希、导出构造参数和推理参数。
本次 15 个源文件全部通过，配置哈希为 `2954e9b1d280cef8f7b686d798e45ef3dd05c9fc0b792e3ead69ae17585b1f50`。
原始 run/checkpoints 层级的导入方式继续支持。

`backbone_flash_attention=true` 是该训练环境的 MUSA 加速开关，不增加权重键。
导入器核对其布尔类型和训练配置一致性后，将原值记入部署清单的 `source.training_model_options`，
从部署构造参数中移除；原始训练/ checkpoint 元数据不变。训练机 `backbone_repo` 路径同样由已有逻辑替换为项目冻结源码。
推理使用已适配的 CUDA/DINOv3 实现，没有引入 MUSA 训练补丁。配置、训练来源、归一化和权重身份校验继续执行。

### 本 checkpoint 的验证

- 修改前 60 项回归通过；修改后 `python -m pytest -q tests webviz/tests` 为 **73 passed**。
- 473 个权重键严格加载；完整 16 帧输入与独立 QuicForge 源码对比，FP32 context、action、启动 padding 最大误差均为 0，RTC 前缀逐位一致。
- 两张实际模型卡各运行启动、完整缓存、缓存复用、RTC 移窗、会话重置 5 个场景，共 10 个通过。
  使用一份记录图像和合成源帧号验证缓存生命周期；它不是完整真实时序录像回放。
- 该输入下 FP16 相对 FP32 最大位置差 0.8351 mm、旋转差 0.1737°、夹爪差 0.1609 mm，
  通过 1 mm / 0.5° / 1 mm 门槛；缓存复用输出和会话重置输出各自逐元素一致。
- FP16 两组实际预测通过原生 IK 和指令保护，共检查 130 条软件目标；物理指令数 0。
- CLI/spawn 推理进程连续两次 RTC 推理通过；专用脚本 `--check` 在设置错误旧环境变量后仍选择本模型并通过检查。

复验实际模型卡与控制目标：

```bash
$PY tools/validate_temporal_deployment.py \
  --config configs/temporal_lva_update_00050000.yaml \
  --fp32-card configs/models/temporal_lva_update_00050000_fp32.json \
  --fp16-card configs/models/temporal_lva_update_00050000_fp16.json \
  --sample outputs/quicforge_reference/episode000_frame0040.npz \
  --output outputs/temporal-update50000-validation --ik-check
```

摘要见 [模型验证记录](validation/temporal_lva_update_00050000.json)。详细报告和数组位于
`outputs/temporal-update50000-validation/`，按项目规则不提交 Git。数值精度、缓存及软件控制检查不代表实机任务成功率。

## 显式覆盖部署 history_stride（2026-11-09）

`update_00050000` 的训练、归一化和部署包清单仍保持 `history_stride=8`，不可为消除报错而改写这些原件。
需要部署采样 stride=1 时，在模型卡的 `model` 段设置：

```json
{
  "allow_history_stride_override": true,
  "temporal_sampling": {
    "history_frames": 16,
    "history_stride": 1,
    "window_stride": 1,
    "source_fps": 30,
    "padding": "current",
    "history_time_units": "video_frames",
    "anchor_alignment": "history_stride"
  }
}
```

以上仅展示相关字段，模型卡的其他字段保留。当前 FP16 和 FP32 卡均已配置为 stride 1。
此开关只授权覆盖 `history_stride`，且必须为正整数；帧数、window_stride、源帧率、padding、时间单位和对齐规则仍需匹配训练契约。
不开启时仍要求训练/部署采样完全一致。原始训练参数继续从经过校验的 bundle 读取；
`load_bundle()` 分别返回有效 `sampling` 和原始 `training_sampling`。

观测器、缓存适配器和 CLI 共用模型卡的有效采样值。stride 1 时取相邻 16 帧，真实偏移为 `[-15,-14,...,0]`，
覆盖 15/30=0.5 秒；训练 stride 8 的原始偏移仍是 `[-120,-112,...,0]`，覆盖 4 秒。
不能采集相邻帧却继续伪报 stride 8 的时间编码。归一化统计及权重不随部署 stride 改写。

本次修改后重新登记两张卡的源码哈希，原始部署包八个文件的哈希全部不变。
用户另一份 LVA 运行 YAML 的手动修改保留，未纳入本次提交。原启动命令继续使用：

```bash
QUICDEPLOY_WEBVIZ=1 bash scripts/start_temporal_lva_50000.sh --check
QUICDEPLOY_WEBVIZ=1 bash scripts/start_temporal_lva_50000.sh --run
```

### 验证及测得的运行成本

- 相关回归 31 passed，完整 `tests webviz/tests` 为 104 passed。
- stride 1 的全 16 帧与独立 QuicForge 源码比较，FP32 context、action、启动 padding 最大误差均为 0，RTC 前缀逐位一致。
- 实际 FP32/FP16 卡各验证启动、完整缓存、复用、移窗、无历史重叠、新会话六个场景，共 12 组通过。
  记录图像配合合成源帧号用于验证缓存生命周期，不将其称为完整真实时序录像回放。
- FP16 相对 FP32 最大位置差约 0.872 mm、旋转差 0.172°、夹爪差 0.135 mm，均通过原有门槛。
- 两组实际 FP16 输出经原生 IK 和指令保护，共 130 条软件目标通过；真实机械臂未连接、未运动。

在本次 RTX 3060 Laptop 上，FP16 只新增一帧的移窗约 0.116 秒；无重叠而重编码 16 帧的实际适配器调用约 1.19 秒。
相邻推理请求若相隔超过这 16 帧的时间窗口，会失去历史 token 重叠；stride 变小不保证更快。
当前预取窗口为 23/30≈0.767 秒，小于本次无重叠调用耗时，因此可能触发现有 `late_policy: hold` 等待保护。
本次按用户要求仅开放部署间距覆盖，没有擅自改变动作播放速度、控制频率或保护阈值。
数值等价和接口验证不代表 stride 1 的实机任务效果已经验证。

复验命令：

```bash
PY=/home/ih/miniforge3/envs/quic-deploy/bin/python
$PY tools/validate_temporal.py \
  --weights models/quicforge/temporal_lva_update_00050000/weights.pt \
  --reference-package outputs/temporal-adaptation-20261109/reference \
  --history-stride 1 --output outputs/temporal-stride1-validation/numerical_validation.json
$PY tools/validate_temporal_deployment.py \
  --config configs/temporal_lva_update_00050000.yaml \
  --fp32-card configs/models/temporal_lva_update_00050000_fp32.json \
  --fp16-card configs/models/temporal_lva_update_00050000_fp16.json \
  --sample outputs/quicforge_reference/episode000_frame0040.npz \
  --output outputs/temporal-stride1-validation --ik-check
```

摘要见 [stride 1 验证记录](validation/temporal_stride1.json)，详细 JSON 和 NPY 保存在上述 outputs 目录。
