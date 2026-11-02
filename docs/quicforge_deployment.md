# QuicForge LVA 部署 / Deployment

当前已接入 **pick_kun_once_e50_bs20，epoch 50 / step 43700，model 原始权重**。
这份是最后一轮，不是训练目录中指向 epoch 25 的 best；没有伪造旧 checkpoint 外层字段。
支持范围：单帧、无 task conditioning、action-GT、50×20、RTC 最长 10 步。
Temporal/multi-task/state-GT 导出会明确报错。

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
* `LVAHalfPrecisionMixin`：从旧 FP16 加载器提取，精度行为不变；旧模型回归输出逐位一致。
* 冻结网络来自分析时 QuicForge c17f4b0 工作区。只将 3 处绝对 import 改成相对 import，
  网络计算不改；`PROVENANCE.json` 同时保存原始与迁移后的 SHA-256。
* `config/quicforge.py`：在不导入 torch 的配置阶段检查模型类型、采样、相机、action-GT、单位/坐标、
  数据身份、模型卡精度与包文件清单。推理子进程再次验证包哈希与全部权重的有限性/FP32 类型。
* `config/resolve.py`：额外检查训练 conversion_meta 的 URDF 哈希与实际部署 URDF 一致。
* 模型卡固定包内 8 个文件及 9 个代码/溯源文件。路径相对模型卡，可随项目整体迁移；
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
当前 v1 仅导入本文这一类单数据源 LVA checkpoint；不把任意 QuicForge 模型都当成 LVA。

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
