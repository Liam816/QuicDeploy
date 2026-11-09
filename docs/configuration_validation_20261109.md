# 配置精简验证记录（2026-11-09）

范围：QuicDeploy 配置解析、默认值、模型卡元数据和导入器输出格式。基于提交 `68bc862`。
分支：`refactor/compact-deployment-config`。实际推理、调度器、IK、MIT 驱动和指令保护实现未改动。
主环境为服务器 `quic-deploy` conda 环境，torch `2.11.0+cu128`、numpy `2.2.6`，GPU RTX 3060 Laptop。

## 配置和产物

- 三份运行 YAML 从约 83 行变为 5 行，通过 `configs/defaults.yaml` 共享策略与显式保护阈值。
- 六张模型卡的 `model` 字段由 42–44 项缩为 24–25 项；描述和 checkpoint 轮次/步数归入 `metadata`。
- 六张卡全部与旧卡逐字段对比：展开后的模型契约一致，`artifacts` 路径和哈希映射完全相同。
- 八组可解析配置（三份运行配置、五张可用模型卡覆盖）与修改前基线的有效参数一致，包含调度、
  机械臂、IK 步长、观测预算、指令保护、静止检查和动作块限制。
- v1 完整配置和模型卡继续支持；v2 支持继承、字段覆盖、缺省补齐及完整来源指纹。

## 回归与真实模型推理

修改前：`python -m pytest -q tests webviz/tests`，31 passed。
修改后同一命令：52 passed，包含新旧配置等价、相对路径、默认值、显式 null、循环继承、未知字段、
缺失保护阈值、非法频率/前缀/夹爪宽度、元数据身份错配和产物完整性回归。
原有模拟端到端测试使用真实调度、原生 IK、指令保护及观测模块；替换机械臂、相机和推理执行端。

修改前先运行 `tools/validate_config_migration.py capture`，修改后运行 `compare`，均使用记录样本
`outputs/quicforge_reference/episode000_frame0040.npz`。以下每张卡各执行普通推理、RTC 推理：

| 模型卡 | 前后数值比较 |
|---|---|
| `lva_pick2box_1026_ep050_fp16` | 两种模式逐元素完全相同 |
| `lva_pick2box_1027_ep039_fp16` | 两种模式逐元素完全相同 |
| `lva_rotating_box_ep050_fp16` | 两种模式逐元素完全相同 |
| `quicforge_pick_kun_ep050_step43700_fp16` | 两种模式逐元素完全相同 |
| `quicforge_pick_kun_ep050_step43700_fp32` | 两种模式逐元素完全相同 |

合计 10 组真实 GPU 输出，均为有限的 `[50,20]` 动作块。
基线和比较结果：`outputs/config_migration_20261109_baseline/{snapshot,comparison}.json`，
该目录还保存各模型原始 NPY 输出。报告和大数组为本机验证产物，按现有规则不提交 Git。

## QuicForge 训练参考与 IK

命令：

```bash
python tools/validate_quicforge.py --fixtures outputs/quicforge_reference \
  --output outputs/config_migration_20261109_quicforge --ik-check
```

3 个原始记录样本 × FP32/FP16 × 普通/RTC，共 12 组通过已有精度门槛。
预处理差值全部为 0；部署 FP32 与训练参考的动作分量差值全部为 0。
FP16 相对 FP32 最大位置差 0.227 mm、旋转差 0.01894°、夹爪差 0.108 mm，均在既有门槛内。
3 组 FP16 普通推理经原生 IK 和指令保护，每组检查 65 条软件目标，共 195 条；FK 锚点误差为 0。
报告：`outputs/config_migration_20261109_quicforge/report.json`。

## 已有缺失与适用范围

历史 `lva_pick2box_ep057_fp16.json` 引用的目录
`/home/ih/workspace/openarm/models/pick2box_action_gt_36datasets_best_ep057` 中，
`deployment_manifest.json` 和 `normalization.json` 在本次修改前已缺失。
该卡完成静态契约/哈希映射迁移检查，但无法运行加载和推理；未伪造统计量或重新注册缺失文件。
迁移比较工具显式使用 `--allow-missing` 记录此项，并要求前后可用性记录一致。

本轮物理机械臂指令数为 0，未连接、使能或回零机械臂。离线验证证明配置和模型数值等价，
并覆盖模拟控制链及原生 IK；不将其描述为真实硬件运行测试。
