# QuicForge LVA 部署 / Deployment

首个接入模型为 **pick_kun_once_e50_bs20，epoch 50 / step 43700，model 原始权重**。
这份是最后一轮，不是训练目录中指向 epoch 25 的 best；没有伪造旧 checkpoint 外层字段。
单帧适配器支持范围：ViT-L/16 或 ViT-B/16、无 task conditioning、action-GT、50×20、RTC 最长 10 步。
Temporal 使用独立适配器，见 [时序部署](temporal_lva_deployment.md)；multi-task/state-GT 仍拒绝。
ViT-B 的架构差异与新 checkpoint 验证见 [ViT-B/16 部署](vitb16_deployment.md)。

## 运行方式

```bash
cd /home/ih/heping/projects/QuicDeploy
conda activate quic-deploy
bash scripts/start_quicforge.sh --check   # 配置、文件哈希、硬件在位检查，不运动
# 离线录制样本推理（不运动）
python -m quicdeploy --model configs/models/quicforge_pick_kun_ep050_step43700_fp16.json infer \
  --sample outputs/quicforge_reference/episode000_frame0040.npz --step 40 --with-prefix --repeat 3
# 真机运行需另行有人值守验收；本次未执行
# bash scripts/start_quicforge.sh --run --duration 30
```

`start_quicforge.sh` 只设置 `QUICDEPLOY_MODEL` 后调用现有 `start_live.sh`，不复制 runtime.yaml。
标准启动器全局默认仍为原模型。新模型复用同一套观测、调度、IK、运动限制和指令保护，
依旧是 30 Hz policy / 50 Hz control。FP32 参考卡可用 `--model ..._fp32.json` 显式选择。

## 部署所需文件

```text
QuicDeploy/
  models/quicforge/pick_kun_once_e50_bs20/        # 约 1.2 GiB，git 忽略
    weights.pt                                # 纯 FP32 state_dict，642 个张量
    normalization.json                        # 原始 mu/std；不改写统计
    model_config.json
    inference_config.json                     # 原始 FP32 参考配置
    deployment_manifest.json                  # 部署包 v1，文件哈希和物理契约
    provenance/
      training_config.json
      checkpoint.json
      conversion_meta.json
  configs/models/
    quicforge_pick_kun_ep050_step43700_fp32.json # 参考模型卡
    quicforge_pick_kun_ep050_step43700_fp16.json # 部署模型卡
  quicdeploy/policy/backends/quicforge_lva/     # 版本控制的最小网络代码
    model.py / decoder.py / dinov3.py / task_ids.py / __init__.py / PROVENANCE.json
```

上传来源是 `/home/ih/workspace/yihuigou/openarm_workspace_hp/models/quicforge/pick_kun_once_e50_bs20`。
整理后运行只读 QuicDeploy 自己的模型包和网络代码，不读上传目录中的模型文件。
`provenance` 中保留旧路径作溯源字符串，不会从这些路径加载数据或代码。
raw 权重 SHA-256：`9d140d643f04af82e36b4c9e2b336e13baf0e13bb7b5976b49b28fbb26c4ce81`。

不需要带 optimizer/scheduler/rank state，也不需要 preproc.pt、训练数据、旧兼容源码或完整 QuicForge 包。
后四者中，数据和开发环境只在**生成离线参考样本**时使用，运行推理不依赖它们。
IK/MIT 子进程仍使用此前单独固定的 quest3 环境，边界未变。

## 适配器结构

* `quicdeploy/policy/quicforge.py`：校验部署包，加载纯 state_dict，在 meta 上建网络后 strict assign。
  FP32 与 FP16 两类适配器复用 `LVAAdapter` 的图像预处理、确定性噪声、RTC 重锚定与输出后处理。
* `LVAHalfPrecisionMixin`：保留旧 ViT-L FP16 行为，通过精度转换方法分派 ViT-B；旧模型回归输出逐位一致。
* ViT-L 冻结网络来自分析时 QuicForge c17f4b0 工作区。ViT-B 增加可选视觉骨干和动态输入投影，
  特征使用训练实现的最后四层、final norm 前 patch 求和。`PROVENANCE.json` 保存原始与迁移后的 SHA-256，
  并记录 ViT-B 独立参考快照 0e75350 的源码哈希。
* `config/quicforge.py`：在不导入 torch 的配置阶段检查模型类型、采样、相机、action-GT、单位/坐标、
  数据身份、模型卡精度与包文件清单。推理子进程再次验证包哈希与全部权重的有限性/FP32 类型。
* `config/resolve.py`：额外检查训练 conversion_meta 的 URDF 哈希与实际部署 URDF 一致。
* 模型卡固定包内文件及对应代码/溯源文件；ViT-B 还固定共享构造器及官方本地源码。路径相对模型卡，可随项目整体迁移；
  旧的绝对路径模型卡继续兼容。

## 重新整理包

```bash
python tools/prepare_quicforge.py \
  --input /path/to/uploaded_run \
  --checkpoint update_00043700 \
  --conversion-meta /path/to/original/conversion_meta.json \
  --output /path/to/QuicDeploy/models/quicforge/new_bundle \
  --card-stem new_quicforge_model
```

导入工具独立复算上传清单中的哈希，复制原始权重和统计，不修改源文件；拒绝覆盖已有包或模型卡。
`conversion_meta.json` 必须来自同一训练数据集，不能拿另一个数据集只因 URDF 相同就替代。
当前导入器同时支持已验证的单数据源和多数据源元数据；每个来源的身份和物理契约都必须一致。
八文件上传布局推荐使用 [通用导入命令](import_quicforge.md)，仍拒绝不支持的 QuicForge 架构。

修改适配器或冻结代码后，应审查变化、重新验证，再登记模型卡（不要绕过哈希检查）：

```bash
python -m quicdeploy register-model configs/models/quicforge_pick_kun_ep050_step43700_fp32.json --relative
python -m quicdeploy register-model configs/models/quicforge_pick_kun_ep050_step43700_fp16.json --relative
```

## 离线验证与复现

参考在部署机本地生成，使用本地已有的同任务 LeRobot 数据 episode 0 的 0/40/80 帧。
没有跨服务器搬运真实录制图像。独立参考保留原始 QuicForge 网络源码及逐字提取的预处理/重锚定函数，
位于 `tools/reference/quicforge_lva/`；不导入生产 backend。

生成参考需要已有的 pyarrow/torchvision 开发环境。这些包没有加入 quic-deploy 推理环境：

```bash
/home/ih/workspace/yihuigou/openarm_workspace_hp/.venv/bin/python tools/build_local_quicforge_reference.py \
  --bundle models/quicforge/pick_kun_once_e50_bs20 \
  --dataset /home/ih/workspace/yihuigou/openarm_workspace/data/lerobot_20261027_144156 \
  --output outputs/quicforge_reference

# 以下用独立 quic-deploy conda 环境运行
python tools/validate_quicforge.py --fixtures outputs/quicforge_reference --ik-check
python -m pytest -q tests
```

每帧比较无前缀、10 步 RTC 两种情况，共 12 组（6 组 FP32 + 6 组 FP16）：

* QuicForge 参考 → 部署 FP32：预处理和物理动作数组逐位一致。
* 部署 FP32 → FP16：本次最大位置差 0.227 mm、旋转差 0.019°、夹爪差 0.108 mm。
* RTC：FP16 写回精确相对前缀，解码后绝对前缀保持到浮点误差量级。
* 3 个无前缀 FP16 动作块串联现有队列、运动限制、原生 IK 和 CommandGuard，
  共 195 条理想执行器软件指令通过检查；真实 CAN 命令数为 0。
* 26 个单元及模拟控制环测试通过，包括旧/新模型、sync/async+RTC、异常模型/篡改包拒绝。
* 旧模型的调度、900 条保护、FK/IK、GPU 推理回归全部通过，推理仍逐位一致。

原始证据在 `outputs/quicforge_validation/report.json`、`outputs/quicforge_reference/`，均不进 Git；
无图像的数值摘要见 `docs/validation/quicforge_pick_kun.json`。

这些数值是加载/预处理等价性和精度测试，不是任务成功率。参考和适配器本次均在部署机
PyTorch 2.11.0+cu128 / NumPy 2.2.6 上运行，不把结果写成与训练机 2.10 的逐位对比。
IK 使用理想软件执行模型，不包含真实跟踪误差、碰撞/负载或相机实时延迟。
尚未执行这份模型的真机验收。


## LVA update_00100000（2026-11-09）

已接入 `/home/ih/heping/checkpoints/LVA_update_00100000`：**epoch 132 / step 100000**，
单帧 `LVADecoder`，history_frames / history_stride / window_stride 为 1 / 1 / 1。
权重 SHA-256 为 `aeda82b3687a29ff486dd0ea628a5ba903b7fc24348246400eb36d20977db070`。

- 运行配置：`configs/lva_update_00100000.yaml`，默认 FP16，继承公共配置。
- 模型卡：`configs/models/lva_update_00100000_fp16.json` 和 `_fp32.json`。
- 部署包：`models/quicforge/lva_update_00100000/`，运行时不读上传目录。
- 专用脚本：`scripts/start_lva_100000.sh`，固定选择本配置并清除旧模型环境覆盖。

```bash
cd /home/ih/heping/projects/QuicDeploy
bash scripts/start_lva_100000.sh --check
# 会连接、使能并回零机械臂，需操作者值守。
bash scripts/start_lva_100000.sh --run
```

复用现有单帧加载器、RTC、调度、IK 与指令保护：控制 50 Hz、动作播放 30 Hz；
async、RTC 前缀 10 行，每块选用 40 行，剩余 20 行时预取（约 0.67 秒）。
实际子进程热启动推理测得约 0.50 秒，因此该模型单独提前预取；该测量不代表实机延迟保证。未更改全局默认模型。

平铺导入会重新核对 8 个源文件的大小/哈希、训练配置规范哈希、checkpoint 名称和导出侧车一致性。
训练侧的 `backbone_flash_attention=true` 处理从 Temporal-LVA 扩展到已支持的单帧 `LVADecoder`：
核对布尔类型和训练/checkpoint 一致性后，将原值保留在 `source.training_model_options`，
从部署构造参数中移除；复用冻结的 CUDA 实现，不引入 MUSA 训练补丁。原始上传资产不改写。

```bash
PY=/home/ih/miniforge3/envs/quic-deploy/bin/python
# 已执行；已有包和模型卡会触发拒绝覆盖。
$PY tools/prepare_quicforge.py \
  --input /home/ih/heping/checkpoints/LVA_update_00100000 \
  --checkpoint update_00100000 \
  --conversion-meta /home/ih/heping/checkpoints/LVA_update_00100000/conversion_meta.json \
  --output models/quicforge/lva_update_00100000 --card-stem lva_update_00100000
```

### 本模型的离线验证

为这份权重重新生成专属参考，使用独立保存的原 QuicForge 网络及部署机已有录像 episode 0 的 0/40/80 帧。
参考权重哈希与模型卡绑定，不复用其他模型的旧参考输出。生成参考所用 pyarrow/torchvision 开发环境不属于部署运行依赖。

- 接入前完整基线 78 项；接入后 **81 passed**，含单帧/时序导入及原控制、指标和可视化回归。
  此工作区包含 5 项已有未提交指标测试；另外仅使用已提交 HEAD 和本次代码/配置的干净临时副本为 **76 passed**，不依赖这些未提交功能。
- 三份样本 × FP32/FP16 × 普通/RTC，共 12 组通过既有门槛。
- FP32 预处理和物理动作数组最大分量差均为 0。
- FP16 相对 FP32 最大位置差 0.2047 mm、旋转差 0.02131°、夹爪差 0.07207 mm。
- 三组实际无前缀预测经原生 IK 和指令保护，共 195 条软件目标通过，物理指令数 0。
- 专用脚本在错误旧环境覆盖存在时仍选中新模型，配置和硬件在位检查通过。
- 实际 CLI/spawn 连续两次 RTC 推理通过；未执行真实机械臂运行。

```bash
# 生成这份权重的独立参考（开发环境）。
/home/ih/workspace/yihuigou/openarm_workspace_hp/.venv/bin/python tools/build_local_quicforge_reference.py \
  --bundle models/quicforge/lva_update_00100000 \
  --dataset /home/ih/workspace/yihuigou/openarm_workspace/data/lerobot_20261027_144156 \
  --output outputs/lva-update100000-reference
# 对比部署精度及离线控制链（运行环境）。
$PY tools/validate_quicforge.py --fixtures outputs/lva-update100000-reference \
  --config configs/lva_update_00100000.yaml \
  --fp32-card configs/models/lva_update_00100000_fp32.json \
  --fp16-card configs/models/lva_update_00100000_fp16.json \
  --output outputs/lva-update100000-validation --ik-check
```

摘要见 [update_00100000 验证记录](validation/lva_update_00100000.json)。
详细数组、参考和报告位于上述 `outputs/` 目录，按现有规则不提交 Git。
本次只提交 LVA 接入相关文件；此前的推理指标/可视化未提交工作保留，不纳入本次提交。
