# piper_differential_ik (deployment build)

部署专用的 Piper 双臂差分 IK Python 扩展（C++/pinocchio），原位于
`/home/ih/workspace/openarm/outputs/deployment_ik/a98292e24fac8b99/`，原样复制，未重新编译。
Deployment-only build of the Piper dual-arm differential IK extension, copied verbatim.

* `build_manifest.json`：原始构建记录：源码目录、各源文件 SHA-256、补丁哈希。其中 `extension` 字段是旧路径，保留作来源记录。
* SHA-256 `a98292e2…afe23` 锁定在 `configs/hardware/piper_x/hardware.yaml` 的 `artifacts` 中。
* 运行环境：CPython 3.12，`deployment_api_version == 1`；动态链接
  `/home/ih/workspace/piper_pipeline/envs/quest3` 中 cmeel 的 pinocchio / coal / boost / urdfdom
  （RUNPATH 指向该环境），因此只能由 `hardware.yaml` 中的 `kinematics.python`（quest3）加载。
  It links pinocchio/coal from the quest3 env (absolute RUNPATH) and is loaded only by that Python.
* 与遥操作录制使用的 IK 扩展不同：录制版本不提供 `deployment_api_version`，会被 `ik_worker.py` 拒绝。
