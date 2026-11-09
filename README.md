# QuicDeploy

模型推理 → 驱动真实双臂 Piper-X 的独立部署项目。
Model inference driving real dual Piper-X arms, extracted from
`openarm_workspace_hp/examples/piper_deployment/start_runtime_live.sh`.

原链路分散在 `lerobot.deployment.*`、`scripts/offline_policy_eval`、`lerobot.robots.bi_piper_native`、
`lerobot.cameras`、`scripts/lerobot_dataset_viewer` 等多处；本项目只保留实机推理这一条链路，
不依赖 `lerobot` 包，并与旧实现做了逐项数值对比（见「验证」）。

## 快速开始 / Quick start

```bash
cd /home/ih/heping/projects/QuicDeploy
bash scripts/setup_env.sh                       # 创建 quic-deploy conda 环境（首次）
conda activate quic-deploy
bash scripts/start_live.sh --check              # 配置 + 模型完整性 + 硬件在位，不运动
bash scripts/start_live.sh --run                # 使能双臂、回初始位、运行策略
PIPER_DIAGNOSTICS=1 bash scripts/start_live.sh --run --duration 120   # 保存每次预测
QUICDEPLOY_MODEL=configs/models/lva_pick2box_ep057_fp16.json bash scripts/start_live.sh --run
```

运行时按键：`←` 平滑回初始位并暂停；`→` 从新观测恢复；`Ctrl+C` 停止。

Pick2Box 1027（epoch39 / step91377）专用配置：`configs/lva_pick2box_1027_ep039_fp16.yaml`。
启动检查：`bash scripts/start_pick2box_1027.sh --check`；值守运行：`bash scripts/start_pick2box_1027.sh --run`。


Rotating Box（epoch50 / step38000）专用配置：`configs/lva_rotating_box_ep050_fp16.yaml`。
启动：`bash scripts/start_rotating_box.sh --check`；值守运行：`bash scripts/start_rotating_box.sh --run`。
其 YAML 可独立调整此模型参数；专用启动器固定选用该配置，忽略遗留的模型/配置环境变量。


新接入 QuicForge pick_kun（epoch50 / step43700）：`bash scripts/start_quicforge.sh --check`。
适配器、文件组织、FP32/FP16 卡和离线验证见 [QuicForge 部署](docs/quicforge_deployment.md)。
新模型只通过该薄启动器或 `--model` 显式选择，共用 runtime.yaml，原默认模型不变。

## 链路 / Pipeline

```
start_live.sh
  ├─ quicdeploy check       配置解析 + 模型产物哈希校验（不连硬件）
  ├─ quicdeploy preflight   CAN 1Mbps / USB 序列号左右映射 / 相机设备节点（不发指令）
  └─ quicdeploy live --run  LiveSession
        ├─ Kinematics       IK 子进程（quest3 env 运行 third_party/ 中的 piper_differential_ik）+ FK/IK 自检
        ├─ InferenceWorker  spawn 子进程，GPU 加载 LVA FP16 模型
        ├─ PiperDualArm     2 个原生 MIT 子进程（can0/can1）+ 3 路相机读帧线程；连接即回初始位
        └─ 50Hz 控制环:
             observer.sample → [到预取阈值? capture → scheduler.prepare(RTC 前缀) → worker.submit]
             → worker.poll → scheduler.accept (U/E 轨迹) → scheduler.target(t+dt)
             → ik.step → guard.apply → robot.send
```

## 目录结构 / Layout

```
quicdeploy/
  common/      geometry(rot6d/decode/插值) · layout(向量排布) · io · validation
  config/      model.py 模型卡与产物哈希 · resolve.py 运行参数+硬件包+模型 → 一份完整配置
  policy/      base.py 适配器接口 · lva.py LVA FP16/FP32 + RTC · worker.py 推理子进程
  planning/    trajectory.py 带时间戳轨迹 · scheduler.py sync/async/RTC 动作队列
  kinematics/  urdf.py FK · ik.py IK 客户端 · ik_worker.py IK 子进程 · selfcheck.py 启动自检
  robot/       guard.py 关节指令保护
    piper/     robot.py 双臂驱动 · native_worker.py/startup.py（原样迁移的 MIT 子进程）
               camera.py 相机 · config.py · preflight.py
  runtime/     live.py 实时会话 · observer.py 观测对齐 · safety.py · recorder.py · controls.py
configs/
  defaults.yaml              公共策略和显式保护阈值
  runtime.yaml / lva_*.yaml   模型选择 + 按需参数覆盖
  models/*.json              模型契约 + metadata 溯源 + 产物 SHA-256
  hardware/piper_x/          hardware.yaml · robot.json · ik.yaml · initial_pose.json
                             piper_bimanual.urdf（FK 与原生 IK 共用的双臂 URDF）
scripts/  setup_env.sh · start_live.sh
tests/    test_core.py（纯单元）· test_live_simulated.py（模拟机械臂端到端）
tools/    compare_with_legacy.py（与旧实现逐项对比）· audit_model_files.py（实际模型文件审计）
docs/     python_environment.md · model_files.md
third_party/piper_differential_ik/   部署专用差分 IK 扩展 .so + build_manifest.json（来源与源码哈希）
outputs/  live/<时间>_<模式>/ 每次运行的配置快照、commands.jsonl、report.json（git 忽略）
```

## 配置 / Configuration

* v2 运行配置通过 `extends: defaults.yaml` 继承公共策略，仅填写模型选择和差异；相对路径归声明文件所有。
  未知字段、循环继承和缺失保护阈值仍报错；旧 v1 完整配置继续支持。
* `policy_hz` 缺省从模型步长推导，夹爪宽度缺省从硬件读取；显式不一致仍报错。
  切换 sync 时需同时设 `prefetch_remaining_steps: 0`、`prefix_steps: 0`、`blend_s: 0.0`。
* v2 模型卡将名称、训练来源等放入 `metadata`；checkpoint 轮次/步数仍参与加载身份校验。
  固定采样参数、精度和首目标偏移按适配器/训练契约展开；权重、归一化和产物哈希仍逐项校验。
  规则、默认值和覆盖示例见 [配置说明](docs/configuration.md)。
* 模型选择：`runtime.yaml` 的 `model`，或命令行 `--model` / 环境变量 `QUICDEPLOY_MODEL`。
* 新增模型：写一份模型卡（参考现有 `configs/models/*.json` 的 `model` 段），然后
  `python -m quicdeploy register-model configs/models/<new>.json` 冻结产物哈希。
  新的模型结构：继承 `quicdeploy.policy.base.PolicyAdapter`，在模型卡 `adapter` 写 `模块:类名`。

## 外部依赖 / External dependencies

双臂 URDF（旧链路中 FK 与 IK 各一份，内容逐字节相同）和 IK 扩展 `.so` 已迁入本项目，
由 `hardware.yaml` 的 `artifacts` 锁定 SHA-256；启动时还会校验 `ik.yaml` 指向的 URDF 与 FK URDF 一致。
以下仍在原位置，由配置引用并做哈希/版本校验：

| 内容 | 位置 | 校验 |
|---|---|---|
| 上游 MIT 驱动 `vr_teleop` | `/home/ih/workspace/yihuigou/teleop_platform_xk` | git commit `5942fff…` 且相关目录无本地修改 |
| quest3 Python（MIT 子进程 + IK 子进程） | `/home/ih/workspace/piper_pipeline/envs/quest3` | — |
| quest3 env 中的 pinocchio/coal 等动态库（IK 扩展链接） | `.../envs/quest3/.../cmeel.prefix/lib` | — |
| 模型权重 / 归一化 / 冻结源码 | `/home/ih/workspace/yihuigou/openarm_workspace/models/…` | 模型卡 SHA-256 |
| torch 2.11.0+cu128 / numpy 2.2.6 | 独立 conda 环境 `quic-deploy` | 模型卡严格比对版本；完整依赖见 requirements.lock |

主推理环境已独立安装，不再依赖旧项目的 `.venv`。详见 [Python 环境](docs/python_environment.md)。
`bash scripts/setup_env.sh --offline --dev` 可利用本机缓存复现安装。
硬件侧 quest3 环境因 NumPy/cmeel 版本要求不同，仍单独使用。

三个模型的实际推理都只使用 4 个模型文件，但当前模型卡还会校验 6–7 个额外溯源文件。
完整路径及最小包验证见 [模型文件审计](docs/model_files.md)。原模型文件未删除。

## 安全机制 / Safety (与原实现一致)

* 启动：配置/模型/硬件文件哈希校验 → 硬件在位检查 → FK/IK 一致性自检 → 与遥操作工具共用的
  `/tmp/piper-vr-oneclick-<uid>.lock` 互斥锁、检测 Dora CAN follower 未运行。
* 推理：首块（及 sync 每块）在静止状态下推理，推理后校验关节漂移、夹爪包络、画面变化。
* 每块：手腕位移/旋转上限；RTC 前缀逐位校验；过期行丢弃。
* 每拍：关节限位、关节速度、夹爪二值化/限幅；确认值与下发值必须一致；失败先保持再报错。
* 超时：`late_policy: hold` 时节拍超时只保持并暂停，不补发；MIT 子进程目标超时自动原地保持。

## 验证 / Verification

```bash
python -m pytest -q tests
# 可选：历史对比工具还会导入旧项目的数据转换模块，需要用旧环境运行
/home/ih/workspace/yihuigou/openarm_workspace_hp/.venv/bin/python tools/compare_with_legacy.py --sample <旧实机运行目录>/chunk_000003.npz
```

`compare_with_legacy.py` 对比结果（2026-10-29，本机）：配置各段一致；调度器 4 种模式
（async+RTC / async 无前缀 / 超前补偿 / sync）U/E 轨迹与目标采样逐位一致；指令保护 900 条一致；
FK、IK 自检、50 步 IK 一致；GPU 推理（无前缀 / RTC 前缀）输出逐位一致。

## 与旧实现的差异 / Differences from legacy

* **修复**：sync 模式下静止推理请求使用 `requested_at=now`。旧实现用头部帧时间，
  通常早于上一块最后一行，导致 sync 第二块大概率被调度器拒绝（模拟测试复现）。
* 未迁移：旧的 `request_every_steps` 调度路径（当前所有配置都已是队列模式）、离线 `replay`、
  ACT 与 lva72 模型（旧版 `check-config` 已无法通过）、VR 实时预览发布。
* 日志与临时目录改为 `outputs/`、`quicdeploy-*` 前缀；MIT 子进程的 CAN 锁路径保持不变，
  以便继续与录制工具互斥。
