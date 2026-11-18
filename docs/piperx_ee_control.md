# PiperX EE 模型的 Link6 控制适配

## 关节与坐标约定

训练使用 QuicDataPipe 的 `piperx_bimanual.urdf`（`4b2652c2…`），将 LeRobot 录制的原生编码器角转换成
`piperx_dual_root → left_ee_link / right_ee_link`。训练末端含 `link_6 → ee_link` 的 135.03 mm 工具偏移。
实测锚点、相对动作解码、队列插值、RTC 前缀和 WebViz 训练轨迹都使用这套坐标。

dashboard infer 的控制 URDF 为 `piper_bimanual.urdf`（`b403c8b8…`），在 `world` 中求
`left_Link6 / right_Link6`，原生差分 IK 的根为 `arm_origin`。两套链的输入均为左右各 J1–J6 的原始度数。
`{side}_joint_{i}.pos → {side}_joint_{i}`（训练）和 `{side}_joint{i}`（控制）按相同的顺序和符号转为弧度，
零位偏置为 0。关节轴和几何零位由两份 URDF 中各自的 `axis` 和 `origin` 定义。

以右臂为例，训练链 J3/J4 夹角约 90°，最短轴线距离约 21.99 mm。控制链对应夹角约 0°，最短距离为 272.64 mm。
固定坐标变换、符号翻转和关节零位偏置都保留相邻旋转轴的方向夹角和距离关系。旧实现 `26242d2`
直接在训练几何上控制硬件，J5 接近 0° 时训练链的 J4/J6 近乎同轴，末端仅有微小变化时也可能得到差异很大的关节解。

## 部署路径

1. 观察和 RTC 使用原始训练 FK。继续使用当前 checkpoint、归一化与动作契约。
2. 以实测编码器角初始化投影，随后对每个训练末端目标做有界阻尼最小二乘求解。以最近一次投影作连续性先验，
   对弱可辨识的 J6 使用更大的先验权重。投影解受 dashboard URDF 的物理关节限位约束。单臂的局部拟合位置残差
   超过 50 mm 或姿态残差超过 12° 时中止该控制请求，走运行时的保持流程。
3. 将求得的编码器目标代入 dashboard 的 Link6 FK，再由同一套原生差分 IK 积分到关节指令。
   `ee/hardware.yaml` 固定两份 URDF、共享的 `../ik.yaml` 和原生扩展。差分 IK 使用 50 Hz、30°/s 和 180°/s²，
   结果经过现有的关节、速度和夹爪保护后交给 MIT 控制。

`wrist_target` 和 `wrist_measured` 记录训练坐标，`ik_diagnostics.control_wrist_target` 记录 dashboard Link6 坐标。
`ik_diagnostics.label_projection_residual` 记录各臂回代到训练 FK 的位置和姿态残差。
会话保存训练 `kinematics.urdf`，解析后的配置快照在 `config_sources` 中记录两份几何的路径和哈希。

## 离线复验

`tools/replay_piperx_ee.py` 读取 LeRobot 的 `observation.state` 和 `action`，在相同实测起点、
30 Hz 录制输入和 50 Hz 控制下，用放行指令模拟下一拍反馈。比较指标统一评估在 dashboard Link6 坐标下，
参考为录制 `action` 关节角的 dashboard FK。“dashboard 直通”直接回放该参考末端目标，包含相同的差分 IK 限速。
本次验证分别使用 `lerobot_20261116_163929`、`lerobot_20261116_165717` 两批录制。模型已使用这两批录制训练，
这里验证的是运动学回放一致性。

| 录制批次 | 轨迹 / 改后通过周期 | 右臂姿态 P95 改前 / 改后 / 直通 | 右臂位置 P50 改前 / 改后 / 直通 |
| --- | ---: | ---: | ---: |
| `163929` | 51 / 14,337 | 37.22° / 6.82° / 5.73° | 69.31 mm / 62.53 mm / 62.42 mm |
| `165717` | 100 / 30,520 | 38.28° / 8.23° / 6.93° | 75.58 mm / 67.20 mm / 67.20 mm |

两批共 151 条轨迹、44,857 个控制周期。改前原生链路拒绝 1 次，改后所有周期通过指令保护。
并行回放中，转换加原生 IK 的耗时 P95 不超过 4.28 ms。

姿态误差的差异主要来自腕部关节分支。回放中的平移跟踪误差也受 30°/s 原生限速影响，尤其是示教快速段。
64 组控制 FK 自检的 Python 与原生末端最大位置差为 4.46e-16 m。两臂在 J5 零位附近执行 J1、J4 小幅动作后，
控制末端残差约 0.076 mm / 0.055°。

当前 `pick_kun_once_right_arm_iter12w_bs24_update_00120000` 的 3 组录制图像，分别做 FP32、FP16 普通与 RTC 推理，
共 12 组原始输出与 `26242d2` 逐元素一致。6 组 FP16 普通与 RTC 输出各独立回放 65 个软件控制周期，共 390 个，
指令保护全部通过。历史真机轨迹 `20261118_002200_async`、`20261118_002459_async` 的已记录目标分别回放
118/133 和 90/174 步后触发投影残差保护。这些目标来自旧控制闭环，回放可确认局部拟合超限及保持路径的触发条件。
真机运动尚未复验。

在工控机上，可用已安装 PyArrow 的 `lerobot` Python 复验录制数据。省略 `--episodes` 则回放数据集内全部轨迹。

```bash
/home/ih/miniforge3/envs/lerobot/bin/python tools/replay_piperx_ee.py \
  --dataset /home/ih/workspace/yihuigou/openarm_workspace/data/lerobot_20261116_163929 \
  --config configs/pick_kun_once_right_arm_iter12w_bs24_update_00120000.yaml \
  --output outputs/piperx_ee_control_validation/replay_b1.json
```

本次对照汇总位于 `outputs/piperx_ee_control_validation/report.json`。
