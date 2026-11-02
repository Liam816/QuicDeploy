# Python 运行环境 / Python environments

2026-11-02 在服务器验证。主环境由 conda 创建，包独立安装，不再通过 `.pth` 引用旧项目的 `.venv`。
Verified on the server on 2026-11-02; the conda environment owns its packages.

## 主进程、相机、推理子进程

```bash
conda activate quic-deploy
cd /home/ih/heping/projects/QuicDeploy
python -m quicdeploy check
bash scripts/start_live.sh --check
# 有人值守时运行 / Attended real-robot run:
bash scripts/start_live.sh --run
```

环境位置：`/home/ih/miniforge3/envs/quic-deploy`，CPython 3.12.13。
`start_live.sh` 会自动定位该环境，不依赖当前终端有没有 activate。
可用 `QUICDEPLOY_ENV_PREFIX` 指定另一安装位置，`PYTHON_BIN` 仍可显式覆盖解释器。

| 直接依赖 | 固定版本 | 用途 |
|---|---|---|
| torch | 2.11.0+cu128 | CUDA 推理；模型卡严格校验 |
| numpy | 2.2.6 | 观测、几何、动作处理；模型卡严格校验 |
| scipy | 1.17.1 | 旋转、插值和运动学 |
| PyYAML | 6.0.3 | 配置解析 |
| opencv-python-headless | 4.13.0.92 | 真实相机读帧；不需要 GUI 版 |
| safetensors | 0.8.0 | 冻结视觉骨干源码中备用权重加载接口；当前 adapter 将其替换，推理不读额外 safetensors |
| pillow | 12.3.0 | 冻结源码中备用图像接口；当前部署预处理直接使用 NumPy |

`requirements.txt` 是直接依赖清单；`requirements.lock` 固定 37 个运行/安装包，
包含 torch 的 CUDA 12.8 动态库依赖、setuptools、wheel。测试另加 4 个包，见 `requirements-dev.lock`。
不要只复制 torch 目录：其依赖包括 CUDA runtime、cuBLAS、cuDNN、cuFFT、cuSPARSE、NCCL、Triton 等。

## 复现安装

```bash
# 有网络：conda 创建环境，然后按完整 lock 安装。
bash scripts/setup_env.sh
# 本机离线：conda 包缓存 + uv 缓存 + 已保留的两个 CUDA wheel。
bash scripts/setup_env.sh --offline --dev
```

`environment.yml` 描述 conda 基础环境；`conda-linux-64.lock` 保存本机 conda 精确包列表。
`setup_env.sh --offline` 创建新环境时使用后者，然后安装 Python 锁文件。
离线安装仍需把 conda/uv 缓存及 wheelhouse 一起带到目标机器，锁文件本身不含二进制包。

首次整理时，服务器仍无法访问 PyPI/PyTorch 下载源。37 个运行包主要从 uv 缓存安装；
缓存中缺失的 `cuda-bindings==12.9.7` 和 `cuda-pathfinder==1.6.0`，从已有安装按各自 RECORD
逐文件验证哈希后恢复为 wheel，保存在 `outputs/env/wheelhouse/`（git 忽略）。
新环境包含独立安装文件，运行不需要旧环境或该 wheelhouse。依赖检查 `python -m pip check` 通过。
系统调用跟踪确认主推理没有访问旧 `.venv` 或 `openarm_workspace_hp`。

旧的项目 `.venv/` 暂时保留以便回退；启动脚本已经不使用它。

## IK 和机械臂原生子进程

这两个边界仍由硬件配置显式指定：

* IK：`configs/hardware/piper_x/hardware.yaml` 的 `kinematics.python`。
* MIT：`configs/hardware/piper_x/robot.json` 的 `quest_python`。

当前都指向 `/home/ih/workspace/piper_pipeline/envs/quest3/bin/python`。其依赖清单记录在
`requirements-hardware.lock`（只供单独的硬件环境参考，**不能安装进主环境**）。
驱动代码仍从锁定 commit 的 `teleop_platform_xk` 导入。

| 硬件依赖 | 版本 |
|---|---|
| numpy | 2.3.5 |
| PyYAML | 6.0.3 |
| dora-rs / pyarrow | 1.1.0 / 26.0.0 |
| pyAgxArm / piper-sdk / python-can | 1.0.0 / 0.6.1 / 4.6.1 |
| libpinocchio / libcoal / cmeel-boost | 3.8.0 / 3.0.2 / 1.89.0 |

`cmeel-boost==1.89.0` 在 Python 3.12 上要求 `numpy>=2.3,<2.4`，与推理的 NumPy 2.2.6
严格版本锁冲突。因此保留硬件进程隔离；它们与主进程通过 socket 通信。
IK `.so` 的 RUNPATH 也仍指向 quest3 的 cmeel 动态库目录。仅安装主推理依赖不能替代这个环境。

## 验证记录

* pip 依赖校验、CUDA 可用性、主环境包路径和 Python 路径检查。
* 新环境既有单元及模拟控制环测试：8 个通过；spawn GPU 推理正常。
* 新旧 Python 环境同输入的 GPU 输出逐位一致。历史回归工具在旧环境执行（它额外导入旧数据转换包），全部通过。
* 三个模型原始目录与四文件临时包的无前缀/RTC 推理逐位对比。
* 检查和模拟过程中未使能、回零或运动真实机械臂。

运行证据：`outputs/env/`；模型文件审计证据：`outputs/model_audit/`。

## QuicForge 适配器

新适配器只使用本环境已有的 PyTorch/NumPy，不安装完整 QuicForge、decord 或 torchvision。
原生参考生成工具使用已有的开发环境读取本地录制数据；生成后数值对比、IK/保护验证都在 quic-deploy 中运行。
详见 [QuicForge 部署](quicforge_deployment.md)。
