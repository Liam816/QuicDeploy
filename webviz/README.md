# QuicDeploy Web 可视化

实时查看头部、左腕、右腕三路相机，以及当前和上一帧模型预测的双臂 3D 轨迹。这个目录包含前端源码、已构建静态文件和独立 Python HTTP 服务。服务不加载模型、不打开相机、不连接 CAN，也不提供机械臂控制接口。

## 日常启动

在项目根目录使用原来的命令：

```bash
bash scripts/start_pick2box_1027.sh --run
```

然后打开：**http://192.168.10.74:8765**。

启动器在原有配置检查和硬件 preflight 之后启动或复用可视化服务，推理启动后自动传入数据。`--check` 不启动可视化。原有键盘操作和机械臂保护保持不变；浏览器仅提供显示操作。服务不可用时会打印提示并继续推理。

相机预览默认 **8 fps、最大宽度 640 px、JPEG quality 75**，与模型输入分辨率及采集帧率独立。预测轨迹在每次被运行时接受后更新。服务会保留最后一份数据，推理退出后显示结束状态；重启推理时以新 session 清除旧相机缓存和旧预测。

```bash
# 关闭遥测及自动启动
QUICDEPLOY_WEBVIZ=0 bash scripts/start_pick2box_1027.sh --run

# 更换端口；浏览器也使用此端口
QUICDEPLOY_WEBVIZ_PORT=8875 bash scripts/start_pick2box_1027.sh --run

# 降低预览负载；也可按需要设置 FPS（1..20）和宽度（160..1280）
QUICDEPLOY_WEBVIZ_FPS=5 QUICDEPLOY_WEBVIZ_WIDTH=480 \
  bash scripts/start_pick2box_1027.sh --run
```

服务日志位于 `outputs/webviz/server.log`，自动启动进程 PID 位于 `outputs/webviz/server.pid`。默认监听 `0.0.0.0`，用于同一可信局域网访问；服务本身没有登录系统。可用 `QUICDEPLOY_WEBVIZ_HOST=127.0.0.1` 改为仅本机监听，再通过 SSH 转发访问。绑定地址、URDF 或包映射变化需要启动新的服务；已运行的服务不会自动替换。

## 页面操作与数据含义

- 三路相机：显示预览分辨率、预览帧率上限、采集时间距现在的年龄。超过 1 秒的图像标为过期，不会冒充实时画面。
- 当前 / 上一帧：指最近两次被运行时接受的完整预测 chunk，不是视频相邻帧。左臂绿色、右臂蓝色，上一帧用半透明虚线显示。
- U：模型输出以真实观测锚点解码得到的完整绝对轨迹，包括未执行的 horizon 行。
- E：裁剪过期行、选择执行窗口并融合后的轨迹；控制环采样的就是 E。
- 坐标：world 坐标系，XYZ 为米，Z 向上；旋转使用 EEF20 的 rot6d 前两列，夹爪单位为毫米。左右臂块分别位于 EEF20 的 0 和 10 列。
- 预测步：可拖动逐步检查末端坐标、朝向、夹爪宽度和预测骨架。默认跟随服务端当前执行时间。
- 上一帧末端位姿：按当前选中步的绝对时间匹配最近一行，超出上一帧时间覆盖范围时取边界；两段完整轨迹仍同时显示。
- 冻结轨迹：冻结 3D 和预测检查器，三路相机继续更新；恢复后显示最新数据。
- 鼠标左键拖动旋转、右键平移、滚轮缩放；也可复位、俯视、侧视或全屏。

## URDF（可选）

自动启动时默认加载运行配置里的 `kinematics.urdf`，目前为 `configs/hardware/piper_x/piper_bimanual.urdf`。此文件只有运动学结构，没有 `<visual>` 外观网格，因此显示关节骨架，这是预期行为。

灰色骨架使用实测 14D 关节反馈；绿色和琥珀色分别预览当前、上一帧预测。模型预测的是末端位姿而非完整关节角，因此预测骨架由浏览器阻尼最小二乘 IK 近似重建，以各次预测的实测关节为初值。界面显示位置/旋转残差，无法收敛时明确标记；**预览 IK 不等于部署 IK 的输出，也不回写真实机械臂**。末端两条短线表达预测夹爪宽度，任意 URDF 的夹爪关节未自动映射。

不提供 URDF 也可显示两帧轨迹。带 `<visual>` 的 URDF 支持 loader 提供的 STL、DAE 网格和 box/cylinder/sphere 几何体。相对网格路径必须位于 URDF 所在目录内；`package://` 引用用 `--package` 指定可访问目录。

例如在一个单独终端启动自定义 URDF 服务：

```bash
cd /home/ih/heping/projects/QuicDeploy
/home/ih/miniforge3/envs/quic-deploy/bin/python -m webviz.server \
  --host 0.0.0.0 --port 8875 \
  --urdf /absolute/path/to/robot.urdf \
  --package piper_description=/absolute/path/to/piper_description
```

随后在另一个终端启动推理：

```bash
QUICDEPLOY_WEBVIZ_PORT=8875 bash scripts/start_pick2box_1027.sh --run
```

默认关节映射是 `left_joint1..6`、`right_joint1..6`，末端是 `left_Link6`、`right_Link6`。自定义命名时在服务启动命令中添加 `--joint-names` 后的 12 个名字（先左后右），以及 `--end-links LEFT RIGHT`。URDF 的根坐标与安装变换必须与模型的 world 坐标、Link6 控制帧一致；加载器不会猜测或校准它们。只替换 URDF 文件时也可通过 `QUICDEPLOY_WEBVIZ_URDF=/absolute/file.urdf` 指定新启动服务的文件。已经运行的服务采用不同 URDF 时，启动器会提示。

## 离线预览与回放

下面两种模式不启动推理或硬件。用独立端口，避免覆盖正式遥测。

```bash
# 合成相机画面及 FK 生成的双臂轨迹，页面明确标记模拟数据
/home/ih/miniforge3/envs/quic-deploy/bin/python -m webviz.server \
  --host 0.0.0.0 --port 8876 --demo \
  --urdf configs/hardware/piper_x/piper_bimanual.urdf

# 回放诊断文件。每秒一份观测及预测，循环播放；不是原始执行速度
/home/ih/miniforge3/envs/quic-deploy/bin/python -m webviz.server \
  --host 0.0.0.0 --port 8876 \
  --replay outputs/live/实际运行目录 \
  --urdf outputs/live/实际运行目录/kinematics.urdf
```

回放需要 `chunk_*.npz`，实机运行时用 `PIPER_DIAGNOSTICS=1 bash scripts/start_pick2box_1027.sh --run` 保存。诊断保存与实时遥测彼此独立，日常可视化不要求开启 diagnostics。

## 前端开发

服务运行仅用 Python 标准库；实时生产端和离线数据生成使用项目已有 numpy / OpenCV。前端使用 Three.js、urdf-loader、Vite，精确版本由 `frontend/package-lock.json` 固定；已构建的 `dist/` 不依赖 CDN，运行时不需要安装 Node。

```bash
cd webviz/frontend
npm ci
npm run build          # 输出 ../dist，Python 服务直接使用

# 热更新开发：先在另一个终端启动 webviz.server（默认 8765）
npm run dev            # /api、/robot、/packages 代理至 127.0.0.1:8765
```

前端目录为 `frontend/src/`：`main.js` 管理 SSE 和界面状态，`scene.js` 绘制轨迹和 URDF，`kinematics.js` 执行浏览器预览 IK，`style.css` 为响应式样式。

## 后端与运行时接入

```text
LiveSession / RunRecorder
  ├─ save_prediction：只替换最近两个不可变预测引用
  └─ Telemetry 后台线程：读取已有相机缓存和已缓存关节
        → JPEG / JSON → loopback HTTP → 独立 webviz.server
              ├─ SSE：状态、两帧 U/E 轨迹、关节和相机时间戳
              └─ MJPEG：每路相机最新图像
```

控制线程不进行可视化 JPEG 编码、JSON 序列化、网络传输或额外硬件读取。生产端使用非阻塞相机锁，只保留两帧预测；网络错误在后台消化，服务离线会重试。HTTP 客户端不会反向对推理施加网络背压。CPU/GIL 和网络仍有资源开销，实机时可降低预览 FPS 或关闭遥测。

只修改了三个原有文件：`scripts/start_live.sh` 的可选旁路启动、`runtime/live.py` 的生命周期接入、`runtime/recorder.py` 的预测引用发布。模型、调度器、IK 与关节保护逻辑未改动。

接口：`GET /api/health`、`/api/config`、`/api/state`、`/api/events`、`/api/camera/{head,left_wrist,right_wrist}`。`POST /api/ingest` 仅接受 loopback 生产端和专用请求头；不启用 CORS。URDF 和包静态路由限制在显式目录内。

## 验证

```bash
PYTHONDONTWRITEBYTECODE=1 /home/ih/miniforge3/envs/quic-deploy/bin/python -m pytest -q tests webviz/tests
```

2026-11-02：31 项测试通过。另在原有模拟 LiveSession 测试中启用真实遥测线程和临时 HTTP 服务，验证三路 JPEG、连续两帧 U/E、14D 反馈与最终结束状态。浏览器验证三路图像均为 640×360、URDF 骨架与 IK 残差、冻结、U/E 切换、步进、上一帧开关；375px 页面没有水平溢出，控制台无错误。前端构建通过，安装时审计为 0 已知漏洞。未启动真实硬件，实机额外资源开销与延迟需要在实际运行中观察。

## 实时推理 latency 与频率（2026-11-09）

终端默认逐次输出完整推理请求的计时；无需开启 diagnostics 或 webviz：

```text
[inference #000012 step=11] latency=125.0 ms model=100.0 ms equivalent=8.00 Hz completed_5s=5 frequency_5s=1.00 Hz
```

上例仅说明输出格式，不代表实机性能。右上角同步显示完整推理 latency、最近 5 秒实测完成频率，并补充单次等效频率和模型调用耗时；原有 30 Hz 明确标为“动作目标”。

- `latency`：主进程 `InferenceWorker.submit()` 提交请求至 `poll()/wait()` 收到完整结果的 monotonic 往返耗时，包括辅助线程排队、观测序列化/IPC、模型完整调用（输入处理、历史缓存、全部去噪步骤、输出反归一化等）以及返回和轮询等待。相机采集/历史预热、模型首次加载、调度器验收/轨迹后处理和 IK/执行不在该计时内。
- `model`：worker 内 `adapter.infer()` 与输出位姿校验耗时。当前 LVA/Temporal-LVA 返回前已执行 `.cpu().numpy()`，结果到达 CPU 后才结束；不是只量 CUDA 异步提交时间，也不是纯 GPU kernel 时间。
- `equivalent = 1000 / latency_ms`：单次完整往返的等效处理频率，不能当成调度后的实测推理吞吐。
- `frequency_5s = completed_5s / 5`：固定最近 5 秒 `(now-5, now]` 内完整请求完成数除以 5，包含调度空闲/暂停时间；启动未满 5 秒时也使用固定分母，因此初始值较低。计数包括返回后未被调度器接受或被丢弃的结果，不统计未完成/失败请求。后台遥测每次发送时重新计算，无新完成时会在 5 秒后衰减到 0；断连或会话结束时页面显示频率 `—` 并保留最近一次 latency。

冻结轨迹不会冻结右上角性能指标。暂停时最近 5 秒窗口仍保留暂停前完成次数，随后衰减为 0。老的推理进程需要退出后重新运行启动命令，再刷新浏览器；单独刷新页面不能给旧进程补上新计时逻辑。

终端输出使用容量 128 的后台队列，慢终端会丢弃多余日志行而不会阻塞控制循环；报告 `report.json` 的 `inference_events` 和指标计数仍记录所有已返回请求。metrics 在 sync、async/RTC 和等待丢弃结果路径均更新，且独立于是否接受轨迹、是否开启可视化。合成/回放页面仍明确标记离线来源，其中 latency 是合成或历史值，频率是离线生产者的回放完成节奏，不代表实机性能。
