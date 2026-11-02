# 模型文件实际依赖 / Model file audit

本文记录旧三份 LVA 模型。新 QuicForge 包的组织和验证见 [QuicForge 部署](quicforge_deployment.md)。

2026-11-02，在新的 `quic-deploy` conda 环境，对当前 3 个 LVA Action-GT FP16 模型逐个审计。
区分模型卡校验、适配器加载、无前缀与 RTC 推理三个阶段；再用只含 4 个文件的临时目录重新推理，
并禁止该进程访问原模型目录。三者的两种推理输出均与原目录逐位一致。

## 每个模型实际推理只需 4 个文件

```text
<模型目录>/
  model.pt                 # 完整骨干 + 解码器权重、结构 config 与 epoch/step
  normalization.json       # EEF20 归一化统计与监督契约
  source/
    model.py               # LVA Decoder 结构
    dinov3_vitl.py          # DINOv3 视觉骨干结构
```

这份清单只描述当前适配器的模型文件依赖；项目代码、模型卡、运行配置、Python/CUDA 依赖仍然需要。
最小包验证直接调用适配器；**当前正式启动仍须读取模型卡登记的全部文件**。
若要精简实际部署目录，还必须更新模型卡路径、implementation_files 和 artifacts，再重新登记哈希。
本次未修改模型卡或删除原模型目录里的文件。

## 推理与校验的区别

| 文件 | 模型推理读取 | 当前启动完整性校验 | 说明 |
|---|---|---|---|
| model.pt、normalization.json、model.py、dinov3_vitl.py | 是 | 是 | 必须保留且使用配套版本 |
| deployment_manifest.json | 否 | 是 | 导出来源记录 |
| source/configs/*.py、source/scripts/eval/eval_model.py | 否 | 是 | 训练/评估溯源 |
| dataset.py、model_checkpoint_compat.py、run_config.py | 否 | 是 | 当前 adapter 不导入它们 |
| model_package.py（存在于两份 ep050 导出） | 否 | 是 | 当前 adapter 不导入它 |
| preproc.pt | 否 | 否 | 部署读取 normalization.json；其中 source_sha256 与模型卡配对，运行不打开 preproc.pt |
| SHA256.json、offline_validation.json、data_metadata/ | 否 | 否 | 导出/离线验证溯源，不是当前部署输入 |
| __pycache__/ | 可能作为 .py 的字节码缓存 | 否 | 无需分发，保留源码即可 |

不需要额外 DINO safetensors 权重：model.pt 已包含完整骨干，适配器构建模型时禁用了额外骨干权重加载。
每个权重仍约 1.25 GB；精简主要减少文件数，节省空间很少。

## 三个模型的实际路径

### lva_pick2box_1026_ep050_fp16.json

- 权重/归一化目录：`/home/ih/workspace/yihuigou/openarm_workspace/models/lva_pick2box_1026_ep050_step51850`
- 网络源码目录：`/home/ih/workspace/yihuigou/openarm_workspace/models/lva_pick2box_1026_ep050_step51850/source/src`
- 正式模型卡登记 11 个文件；其中 7 个仅作哈希校验。

| 实际推理文件 | 字节数 | SHA-256 |
|---|---:|---|
| model.pt | 1254255531 | `6baa904f803c5fe9308be0f9c540e8fd0692012874dfe08cbcf141e4046a48a4` |
| normalization.json | 1052 | `5cceb93d89d487713437c41d67fff40e714da25320ee59be999214d65db7caea` |
| model.py | 18186 | `49e7885649c58bb17f3ea601d1a3e162a0d0c05e2a76fc0b7ebdb1d939086bfb` |
| dinov3_vitl.py | 35071 | `a3f2a3527558b9e1a6211c822af2633c5070c509db250d7dc90e4ce6b2bbd897` |

仅作完整性校验：

- `/home/ih/workspace/yihuigou/openarm_workspace/models/lva_pick2box_1026_ep050_step51850/deployment_manifest.json`
- `/home/ih/workspace/yihuigou/openarm_workspace/models/lva_pick2box_1026_ep050_step51850/source/configs/pick2box_action_gt_full_50ep_1026.py`
- `/home/ih/workspace/yihuigou/openarm_workspace/models/lva_pick2box_1026_ep050_step51850/source/scripts/eval/eval_model.py`
- `/home/ih/workspace/yihuigou/openarm_workspace/models/lva_pick2box_1026_ep050_step51850/source/src/dataset.py`
- `/home/ih/workspace/yihuigou/openarm_workspace/models/lva_pick2box_1026_ep050_step51850/source/src/model_checkpoint_compat.py`
- `/home/ih/workspace/yihuigou/openarm_workspace/models/lva_pick2box_1026_ep050_step51850/source/src/model_package.py`
- `/home/ih/workspace/yihuigou/openarm_workspace/models/lva_pick2box_1026_ep050_step51850/source/src/run_config.py`

本次运行未访问（不含 __pycache__）：

- `/home/ih/workspace/yihuigou/openarm_workspace/models/lva_pick2box_1026_ep050_step51850/SHA256.json`
- `/home/ih/workspace/yihuigou/openarm_workspace/models/lva_pick2box_1026_ep050_step51850/data_metadata/lerobot_20261026_150725_908853_kvdb/conversion_meta.json`
- `/home/ih/workspace/yihuigou/openarm_workspace/models/lva_pick2box_1026_ep050_step51850/data_metadata/lerobot_20261026_154349_567421_kvdb/conversion_meta.json`
- `/home/ih/workspace/yihuigou/openarm_workspace/models/lva_pick2box_1026_ep050_step51850/offline_validation.json`
- `/home/ih/workspace/yihuigou/openarm_workspace/models/lva_pick2box_1026_ep050_step51850/preproc.pt`

### lva_pick2box_ep057_fp16.json

- 权重/归一化目录：`/home/ih/workspace/openarm/models/pick2box_action_gt_36datasets_best_ep057`
- 网络源码目录：`/home/ih/workspace/piper_pipeline/models/pick2box_action_gt_best_20261022_190909/source/src`
- 正式模型卡登记 10 个文件；其中 6 个仅作哈希校验。

| 实际推理文件 | 字节数 | SHA-256 |
|---|---:|---|
| model.pt | 1254256107 | `66c48a12d74347467b9ebfa8e04eb865895b845097f758177b7e231491f15e1a` |
| normalization.json | 1059 | `a0d39e574a8eae33cf989863c203810cb2d33b0d04125cfebbc83a9d45481806` |
| model.py | 18130 | `4bff36fee89aaa9e1eb394a20d4a1fe69ff153fd7f4e4ac11fadeb43445c8a37` |
| dinov3_vitl.py | 35071 | `a3f2a3527558b9e1a6211c822af2633c5070c509db250d7dc90e4ce6b2bbd897` |

仅作完整性校验：

- `/home/ih/workspace/openarm/models/pick2box_action_gt_36datasets_best_ep057/deployment_manifest.json`
- `/home/ih/workspace/piper_pipeline/models/pick2box_action_gt_best_20261022_190909/source/configs/pick2box_action_gt_full_50ep.py`
- `/home/ih/workspace/piper_pipeline/models/pick2box_action_gt_best_20261022_190909/source/scripts/eval/eval_model.py`
- `/home/ih/workspace/piper_pipeline/models/pick2box_action_gt_best_20261022_190909/source/src/dataset.py`
- `/home/ih/workspace/piper_pipeline/models/pick2box_action_gt_best_20261022_190909/source/src/model_checkpoint_compat.py`
- `/home/ih/workspace/piper_pipeline/models/pick2box_action_gt_best_20261022_190909/source/src/run_config.py`

本次运行未访问（不含 __pycache__）：

- `/home/ih/workspace/openarm/models/pick2box_action_gt_36datasets_best_ep057/preproc.pt`

### lva_rotating_box_ep050_fp16.json

- 权重/归一化目录：`/home/ih/workspace/yihuigou/openarm_workspace/models/lva_rotating_box_ep050_step38000`
- 网络源码目录：`/home/ih/workspace/yihuigou/openarm_workspace/models/lva_rotating_box_ep050_step38000/source/src`
- 正式模型卡登记 11 个文件；其中 7 个仅作哈希校验。

| 实际推理文件 | 字节数 | SHA-256 |
|---|---:|---|
| model.pt | 1254255211 | `746915d55ea523280a921be66283b019dc51878f607caafaaf3d1596d1322765` |
| normalization.json | 1059 | `a90dc46b41020579ce8c32d67c8ef9e9f27ee38538b9a22b20503f41ba19a626` |
| model.py | 18186 | `49e7885649c58bb17f3ea601d1a3e162a0d0c05e2a76fc0b7ebdb1d939086bfb` |
| dinov3_vitl.py | 35071 | `a3f2a3527558b9e1a6211c822af2633c5070c509db250d7dc90e4ce6b2bbd897` |

仅作完整性校验：

- `/home/ih/workspace/yihuigou/openarm_workspace/models/lva_rotating_box_ep050_step38000/deployment_manifest.json`
- `/home/ih/workspace/yihuigou/openarm_workspace/models/lva_rotating_box_ep050_step38000/source/configs/rotating_box_action_gt_full_50ep.py`
- `/home/ih/workspace/yihuigou/openarm_workspace/models/lva_rotating_box_ep050_step38000/source/scripts/eval/eval_model.py`
- `/home/ih/workspace/yihuigou/openarm_workspace/models/lva_rotating_box_ep050_step38000/source/src/dataset.py`
- `/home/ih/workspace/yihuigou/openarm_workspace/models/lva_rotating_box_ep050_step38000/source/src/model_checkpoint_compat.py`
- `/home/ih/workspace/yihuigou/openarm_workspace/models/lva_rotating_box_ep050_step38000/source/src/model_package.py`
- `/home/ih/workspace/yihuigou/openarm_workspace/models/lva_rotating_box_ep050_step38000/source/src/run_config.py`

本次运行未访问（不含 __pycache__）：

- `/home/ih/workspace/yihuigou/openarm_workspace/models/lva_rotating_box_ep050_step38000/SHA256.json`
- `/home/ih/workspace/yihuigou/openarm_workspace/models/lva_rotating_box_ep050_step38000/data_metadata/lerobot_20261026_171641_579680_kvdb/conversion_meta.json`
- `/home/ih/workspace/yihuigou/openarm_workspace/models/lva_rotating_box_ep050_step38000/offline_validation.json`
- `/home/ih/workspace/yihuigou/openarm_workspace/models/lva_rotating_box_ep050_step38000/preproc.pt`

## 需要注意的源码差异

三个模型的 `dinov3_vitl.py` 哈希一致；两份 ep050 的 `model.py` 哈希一致。
**ep057 的 model.py 不同**，不能拿 ep050 的 model.py 替代。ep057 的权重和源码当前还位于两个不同目录，
所以只拷贝它的权重子目录并不完整。

## 复查

```bash
conda activate quic-deploy
cd /home/ih/heping/projects/QuicDeploy
python tools/audit_model_files.py --sample /path/to/chunk_000003.npz
```

工具按顺序运行三个模型，每次临时复制约 1.25 GB，完成后自动清理临时包。
原始审计事件、文件大小、哈希和逐位对比结果保存在 `outputs/model_audit/*.json`（git 忽略）。
结论适用于当前三份模型卡、当前 LVA FP16 适配器和记录样本；换适配器或权重需重新审计。
