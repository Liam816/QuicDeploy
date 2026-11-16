# CachedTemporal-LVA ViT-L/16 部署

本次模型为 `CachedTemporalLVADecoder`，使用训练过的原生 DINOv3 ViT-L/16、
16 帧历史、原始视频帧间隔 8、训练窗口间隔 1。缓存训练的模型类型和特征提取方式
保留在部署包中，不改写成普通 `TemporalLVADecoder`。

## 模型与精度

骨干取第 20–23 层的最终归一化前特征之和，再去掉 CLS 和 4 个 register tokens。
`backbone_precision=bf16` 要求骨干保留原始 FP32 参数，用 BF16 autocast 计算并将
提取的 patch 特征量化为 BF16。FP32 参考卡指空间/时序模块和动作解码器的 FP32
路径，骨干仍遵循训练声明的 BF16 计算方式。

混合精度卡只将非骨干参数转为 FP16，不先对骨干做 half 再恢复 float。
部署从原始 RGB 图像计算空间 token，复用既有时序 token 缓存、当前帧启动填充和 RTC，
不依赖训练服务器上的 `/dev/shm` patch cache。配置、checkpoint、归一化之间的
模型类型、backbone、冻结开关、骨干精度和历史采样参数不一致时拒绝加载。

## 导入与离线推理

```bash
cd /home/ih/heping/projects/QuicDeploy
PY=/home/ih/miniforge3/envs/quic-deploy/bin/python
CKPT_NAME=pick2box_1027_hf16_hs8_ws1_frozen_vitl16_cache_e50_update_00078400

# 本实例已导入，重复导入会拒绝覆盖已有目标。
$PY tools/import_quicforge.py /home/ih/heping/checkpoints/$CKPT_NAME --hardlink-weights

$PY -m quicdeploy --config configs/$CKPT_NAME.yaml check
$PY -m quicdeploy --config configs/$CKPT_NAME.yaml infer --repeat 3
```

`infer` 不连接相机、CAN 或机械臂；无样本参数时使用黑色图像和当前帧启动填充。
传入 `--sample /path/to/sample.npz --with-prefix` 可使用离线录制图像和 RTC 前缀。
新配置独立存在，使用时显式指定 `--config`。

`--hardlink-weights` 默认关闭。本实例磁盘剩余空间不足以再复制 1.26 GB 权重，因此
显式使用同文件系统硬链接。源 `model.pt` 与部署 `weights.pt` 共享 inode，删除一个
路径不影响另一路径；原地修改内容会影响两者，并被模型卡 SHA-256 校验拒绝。
源权重须保持只读。跨文件系统硬链接失败会回滚，不静默改为复制。

## update_00078400 验证记录（2026-11-16）

- checkpoint：epoch 50 / step 78400，701 个 state_dict 键严格加载。
- 权重 SHA-256：`122b262ff6735ac673b68fcffe24d52898e2c96cfabb1e8bdcf82b190eaa0fbd`，
  与训练服务器原始 `backend/model.pt` 一致。
- RTX 3060 Laptop GPU（6 GB），torch `2.11.0+cu128`。
- 完整 `1×16×3×448×1824`、16 个不同的合成输入帧，对照未改写的训练源码：
  context、无前缀动作、RTC 动作和启动填充 context 最大绝对误差均为 0。
  RTC 前 10 行逐位一致。
- 16 帧编码约 3.15 秒；该数值比较阶段峰值已分配显存约 1499 MiB。
- 离线录制 RGB 图像搭配合成时序编号，覆盖 FP32/混合 FP16 的启动、完整历史、
  缓存复用、滑动 RTC 和新会话重置，共 10 个场景。混合精度最大差异为
  位置 0.267 mm、旋转 0.0294°、夹爪 0.117 mm，通过预设 1 mm / 0.5° / 1 mm 阈值。
- 正式 `tests` 目录 173 项测试通过；CLI `check` 通过，独立 worker 用录制图像和
  RTC 前缀连续推理 3 次，输出均为 `50×20`，缓存重复推理约 40 ms。

报告位于 `outputs/cached-temporal-vitl16-update78400/validation.json`。
独立参考源码位于 `tools/reference/quicforge_cached_temporal/`，逐文件哈希和来源版本
记录在 `PROVENANCE.json`。快照来自训练仓库 `e2685c87955d7360d90d6553c61e095ce513fa1f`，
不将该版本声明为历史训练时的源码 commit。

```bash
$PY tools/validate_cached_temporal.py \
  --config configs/$CKPT_NAME.yaml \
  --fp32-card configs/models/${CKPT_NAME}_fp32.json \
  --fp16-card configs/models/$CKPT_NAME.json \
  --sample outputs/pick2box-1027-quicforge-117150-reference/episode000_frame0040.npz \
  --output outputs/cached-temporal-vitl16-update78400
```

以上验证覆盖加载、数值和适配器生命周期。合成输入和单帧录制样本不是完整时序
数据集回放，不用于声明实机任务成功率。正式自动化测试使用 `python -m pytest tests -q`，
避免收集 `outputs` 中历史源码备份的同名测试。
