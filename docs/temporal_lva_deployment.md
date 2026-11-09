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
