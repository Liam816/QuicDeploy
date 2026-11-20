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

启动时以本次 token 会话的首次模型请求为历史起点。即使相机已缓存此前的原图，
首个请求也只编码当前最新帧一次，16 个槽位通过广播视图复用同一份空间 token。
后续尚未积累到的槽位继续使用本次当前帧 token，真实历史逐槽替换，保留
`[-120,-112,…,0]` 帧偏移和全有效槽位。暂停恢复或 token 会话重置采用同样规则，
不会回溯串行编码启动前的原图。会话开始后的真实采集缺帧仍报错，不作为启动填充。

实时 async/RTC 控制循环在动作请求之间，按 30 Hz 原始视频的 stride=8 网格，
持续向模型进程发送新增采样图像。模型进程串行处理视觉更新和动作请求，按原训练
语义维护 16 个空间 token；动作请求不再集中补算跨过的多张历史原图。
已排入同一 FIFO 通道的图像不会在动作请求中重复传输，只保留历史帧索引和偏移。
视觉发送队列有背压上限，不能追上采集时会保留待发帧号，真实历史丢失仍报错。

模型进程在报告 ready 之前完成 CUDA 前向和动作采样预热，此时尚未连接/使能机械臂。
预热不建立历史会话。实时日志增加 `vision_new`（动作请求内补算帧数）与
`background_new`（两次回复间独立编码的帧数）；正常连续运行时前者应为 0，
刚跨入新采样点或缓存未就绪时可补算当前帧。后台视觉成本没有消失，`latency`
仍包含等待视觉更新与 IPC 的端到端耗时，`model` 单独记录动作请求处理时间。

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

## 连续视觉缓存复验

`tools/validate_continuous_vision.py` 使用原始 RTC 调度器和真实 GPU worker，按实际
墙钟推进 30 Hz 源帧索引，保持 16/8 采样。输入为录制 RGB 图像经帧号相关平移生成的
不同图像，不作为真实任务成功率测试。离线工具与机械臂控制使用同一互斥锁。

```bash
$PY tools/validate_continuous_vision.py \
  --config configs/$CKPT_NAME.yaml \
  --sample outputs/pick2box-1027-quicforge-117150-reference/episode000_frame0040.npz \
  --output outputs/continuous-vision-validation --duration 24
```

隔离副本中 185 项正式测试通过。12 秒连续验证中 21 个稳态动作请求内 ViT 补算均为 0，
动作处理耗时中位数约 32 ms，端到端中位数约 262 ms，P95 约 319 ms；没有 RTC 重启
或覆盖空档。启动、早期 RTC 和完整历史三个输出与相同图像的未缓存推理逐位一致。
这些数据包含等待当前视觉更新的时间；不能用动作处理时间代替端到端视觉延迟。
