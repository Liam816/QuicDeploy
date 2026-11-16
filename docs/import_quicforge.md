# 从上传的 checkpoint 生成部署配置

在工控机 `/home/ih/heping/projects/QuicDeploy` 执行：

```bash
bash scripts/import_quicforge_model_ckpt.sh /home/ih/heping/checkpoints/你的模型目录
```

名称直接使用传入 ckpt 目录的最后一级名字，原样保留，不追加 epoch、step、`_fp16` 或 `_fp32`。
每次只生成所选精度的一张同名模型卡，默认 FP16；epoch、step 保留在 JSON 的 metadata 中，精度由适配器配置表达。
导入只读源文件，完成后打印对应的 `--check`、`--run` 命令，不连接机械臂。

## 输入与选项

上传目录沿用当前八文件结构：

```text
checkpoint_dir/
  model.pt
  preproc.pt
  model_config.json
  inference_config.json
  normalization.json
  config.resolved.json
  checkpoint_manifest.json
  conversion_meta.json
```

支持现有适配器已验证的单帧 `LVADecoder` 和 `TemporalLVADecoder`，
包括单数据集、`dataset_sources` 多来源和 `quicforge_sources` 汇总转换元数据。
模型结构、采样或坐标契约不受支持时，导入会报错，不会从文件名猜测兼容性。

单帧 `LVADecoder` 支持 `backbone: dinov3_vitl16` 和 `backbone: dinov3_vitb16`。
旧导出缺少 `backbone` 时仍按 ViT-L/16 加载；导出、训练配置和 checkpoint 清单中的有效 backbone 必须一致。
单帧 ViT-B 复用本地官方构造器，保持单帧特征，不启用 Temporal 的历史缓存或重采样。
完整 `model.pt` 已含视觉权重，不额外读取训练服务器的 timm 预训练文件，也不在线下载源码。
两种 backbone 的 FP16 均保持所需 LayerScale 为 FP32；ViT-B 同时保留原始 FP32 RoPE periods。
ViT-B 模型卡额外固定官方 `third_party/dinov3` 和共享构造/精度代码的哈希。
当前实例与复验命令见 [ViT-B/16 部署](vitb16_deployment.md)。

```bash
# Preview validated names and paths, without generated files.
# 校验并预览名称、路径，不生成部署文件。
bash scripts/import_quicforge_model_ckpt.sh /path/to/ckpt --dry-run

# Select FP32 when first importing; the filename remains the directory name.
# 首次导入时可选 FP32，文件名仍为目录名。
bash scripts/import_quicforge_model_ckpt.sh /path/to/ckpt --precision fp32
```

不再使用 `--name` 覆盖目录名。目录名允许字母、数字、中文、下划线、点和连字符，首字符须为字母、数字、中文或下划线，
最多 240 个 UTF-8 字节；不安全的目录名报错，不会自动改名。输入路径是目录符号链接时，名称使用用户传入的链接名。
相对输入路径以调用命令时的目录为基准。Shell 入口复用 `quic-deploy` 环境定位，
也可通过既有 `PYTHON_BIN` / `QUICDEPLOY_ENV_PREFIX` 选择解释器。

## 生成结果与检查

传入 `/home/ih/heping/checkpoints/pick2box_v2` 时：

| 生成路径 | 内容 |
| --- | --- |
| `models/quicforge/pick2box_v2/` | 精简部署包，含权重、归一化、结构、采样与来源信息 |
| `configs/models/pick2box_v2.json` | 所选精度模型卡及产物哈希，默认 FP16 |
| `configs/pick2box_v2.yaml` | 继承 defaults.yaml 的独立运行配置 |
| `scripts/start_pick2box_v2.sh` | 固定该配置并清除旧模型环境覆盖的入口 |
| `outputs/imports/pick2box_v2/` | 导入回执与此次使用的源文件哈希清单 |

单帧配置继承公共调度：async、RTC 10、每块选用 40 行、剩余 15 行预取，
动作播放 30 Hz、IK/MIT 控制 50 Hz。时序配置使用既有时序预取值 23，
保持训练声明的历史帧数和间距，不自动覆盖 history_stride。

```bash
bash scripts/start_pick2box_v2.sh --check
QUICDEPLOY_WEBVIZ=1 bash scripts/start_pick2box_v2.sh --run
```

导入完成表示模型卡哈希、物理契约和完整配置解析通过。GPU 数值对比和实机任务效果
仍需按模型另行验证；导入工具没有执行这些操作。
生成的卡、YAML、启动脚本由 Git 管理；大权重目录和 outputs 按项目现有规则忽略，工具不自动提交 Git。

所有目标路径都拒绝覆盖。源目录与输出目录不能存在包含关系，符号链接解析后也会检查。
相同目录名已经导入时，重新运行会报错；新模型版本请使用不同的 ckpt 目录名。
命令通过项目导入锁避免并发写入，失败时移除本次新建目标，保留原始 checkpoint 和已有模型。
不要在本命令执行期间同时用其他工具手动写入同名目标。

## 源文件完整性

如果上传目录有 `download_manifest.json`，它必须覆盖八个文件，文件长度和 SHA-256 全部匹配。
已有清单不匹配时直接报错，不会重新计算后覆盖原清单来消除错误。

如果只有八个源文件，工具在临时目录为当前文件建立 `local_observed_baseline`，
导入后保存在回执目录，不往源目录添加或修改文件。这个基线用于约束本次复制与后续部署，
不证明文件在本次导入之前的下载/传输过程正确。导入过程中源文件发生变化也会失败并回滚。

## 模型卡的 artifacts

顶层 `artifacts` 是“文件路径 → SHA-256”映射，由模型登记逻辑自动生成：

- 权重、归一化：确认模型与配套动作统计没有替换或混用。
- 结构、采样、导出清单和来源元数据：锁定这套模型的接口与身份。
- 加载适配器、冻结网络实现等关键源码：锁定对应的推理实现版本。

相对路径以模型卡所在目录为基准。启动加载模型卡时，程序重新计算文件哈希；
缺失文件、清单不完整或内容改变都会阻止启动。推理 worker 加载时还会复核 bundle，
每次控制节拍和单次 infer 不重复对整套大文件计算哈希。

`model.artifact_dir` 是部署包所在目录，`model.artifact_format` 声明加载格式，
例如 `quicforge_lva_v1`；顶层 `artifacts` 负责记录依赖文件的内容指纹。

不需要手工填写这些哈希。确需更新依赖文件时，先确认变更并完成对应验证，再重新登记：

```bash
python -m quicdeploy register-model configs/models/pick2box_v2.json --relative
```

完整性校验与模型能力评估分别进行；哈希一致不代表模型已经学好任务。

当前命名规则验证见 [目录原名验证](validation/import_quicforge_directory_names.json)；
初版流程记录见 [通用入口验证](validation/import_quicforge_command.json)。
