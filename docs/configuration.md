# 配置分层与迁移 / Configuration layers

## 运行参数

`configs/runtime.yaml` 是默认入口；模型专用启动器仍选用各自 YAML，命令不变。
入口通过 `extends: defaults.yaml` 读取公共策略，每个模型可覆盖任意已知节中的字段：

```yaml
schema_version: 2
extends: defaults.yaml
model: models/lva_pick2box_1027_ep039_fp16.json
execution:
  n_action_steps: 35
postprocess:
  blend_s: 0.05
```

覆盖次序：父配置 → 当前配置 → 缺省补齐（只填缺项）；`--model` 最后覆盖模型选择。
节内逐字段合并，列表整体替换，显式 `null` 保留原意，不会重新套用父值。
相对路径以声明该路径的文件为基准，不随启动目录或子配置位置变化。
继承链各文件都进入 `config_sources` 和最终指纹；未知字段、循环继承立即报错。

公共策略在 `configs/defaults.yaml`；保护阈值没有隐藏代码默认值，缺少仍拒绝启动。
以下非保护项可省略，解析器补齐：

| 字段 | 默认值 / 来源 |
|---|---|
| `runtime.device` / `cpu_threads` | `cuda` / `4` |
| `execution.policy_hz` | `1 / model.action_dt_s`，动作行播放频率 |
| `execution.policy_overclock_enabled` | `false`；改变播放频率必须显式开启 |
| `postprocess.arm_delay_s` / `gripper_delay_s` | `0.0`，关闭超前补偿 |
| `guard.gripper_widths_mm` | 硬件左、右夹爪宽度；显式填写时仍校验一致性 |
| `guard.gripper_speed_mm_s` | `null`，关闭额外软件限速 |
| `guard.max_tracking_error_deg` | `null`，跟踪误差只记录，不触发中止 |
| `guard.gripper_binary_thresholds_mm` | `null`；公共配置显式设为 `[40, 75]`，沿用现有二值化 |

`horizon` 是模型输出长度，`n_action_steps` 是每块选用长度；模型 `fixed_prefix_steps` 是 RTC 支持上限，
运行 `prefix_steps` 是本次实际前缀长度；`prefetch_remaining_steps` 决定提前推理时机。
这些参数不可相互替代。`control_hz` 决定 IK/MIT 频率，`policy_hz` 不代表推理调用频率。

切换 sync 需显式设置 `mode: sync`、`prefix_steps: 0`、`prefetch_remaining_steps: 0` 和 `blend_s: 0.0`。
使用 `python -m quicdeploy --config <配置> show-config` 查看补齐后的完整参数及来源；该命令不连接硬件。

## 模型卡与溯源

v2 模型卡包含 `schema_version`、`model`、`metadata`、`artifacts` 四部分。

- `model`：权重/归一化/冻结源码位置、坐标单位、输出语义、时间步长、输出长度、RTC 支持和运行库版本。
- `metadata`：名称、结构说明、训练代码提交、数据划分、采样算法说明等溯源信息。
- `metadata.checkpoint.epoch/global_step`：归档后仍展开为内部模型身份字段，与真实 checkpoint 交叉核对。
- `artifacts`：全部产物的路径和 SHA-256，继续在每次启动时校验。迁移不修改产物文件或其哈希。

`device/cpu_threads` 归运行配置所有；旧 LVA 的重复 `checkpoint_sha256` 已去掉。
QuicForge 的 `weights_sha256`、LVA 的 `preproc_sha256` 和 `runtime_versions` 均保留原有校验。
归档的描述性元数据不会被展开为运行参数；checkpoint 身份是明确列出的例外。

当前已支持 LVA/QuicForge 适配器的固定能力由解析器补齐：`num_steps=10`、`guidance_scale=0`、
`noise_std=1`、`weights_variant=model`、`uses_state=false`、`uses_language=false`，执行精度取自适配器类，
LayerScale 保持 FP32。显式传入非兼容值仍会被适配器/契约校验拒绝，不会被默认值覆盖。
`crop/mean/std` 仍显式指定；输入固定 RGB 480×640，`resize/color` 不再伪装成转换开关。
Action-GT 的首目标位于观测后一个训练步，缺省 `first_target_offset_s=action_dt_s`。

新 QuicForge 导入器直接输出 v2 卡；旧 v1 卡仍可使用，模型张量和推理代码未改动。
注册卡仍使用 `python -m quicdeploy register-model configs/models/<模型>.json`。
修改卡之前应确认产物来源，注册不会修复缺失产物或兼容性错误。

## 离线验证与回滚

```bash
# 修改前保存真实参数及 GPU 推理基线；基线目录必须不存在。
python tools/validate_config_migration.py capture --baseline outputs/my_config_baseline \
  --sample outputs/quicforge_reference/episode000_frame0040.npz
# 修改后比较：有效参数一致，普通/RTC 推理要求逐元素完全相等。
python tools/validate_config_migration.py compare --baseline outputs/my_config_baseline \
  --sample outputs/quicforge_reference/episode000_frame0040.npz
python -m pytest -q tests webviz/tests
```

历史模型产物本来就缺失时，可以显式传 `--allow-missing` 记录不可验证项；前后缺失记录也必须一致。
完整结果在基线目录的 `snapshot.json`、`comparison.json` 和每个模型的输出 NPY 中。
比较不包含描述性元数据和配置文件指纹，但保留全部有效模型、调度、IK、机械臂及保护参数。
这些命令不发送物理机械臂指令。真实控制路径由现有模拟端到端测试和原生 IK 进程验证。

回滚时同时回退本次配置和解析器提交即可。旧 v1 完整配置可直接使用；v2 卡需要本次 v2 解析器。
2026-11-09 的验证范围及已有缺失项见 [迁移验证记录](configuration_validation_20261109.md)。
