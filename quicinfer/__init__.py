"""QuicInfer: model inference driving real dual Piper-X arms.
QuicInfer：模型推理驱动真实双臂 Piper-X 的部署框架。

Layers (each depends only on the ones above it) / 分层（只依赖上层）:
    common     geometry, layouts, IO, validators / 几何、向量排布、IO、校验
    config     model cards + runtime YAML + hardware bundle / 配置解析
    policy     adapter interface, LVA adapters, inference process / 推理
    planning   timed trajectories + action-queue scheduler / 轨迹与动作队列调度
    kinematics URDF FK + native differential IK client / 运动学
    robot      command guard + Piper driver, cameras, preflight / 指令保护与机械臂驱动
    runtime    observer, safety, recorder, live session / 实时控制会话
"""
__version__ = "0.1.0"
