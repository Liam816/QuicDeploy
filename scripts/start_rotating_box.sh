#!/usr/bin/env bash
# Rotating Box epoch50/step38000 FP16; reuse the checked standard launcher.
# Rotating Box 专用入口：复用标准启动器及现有观测、调度、IK、指令保护。
#   bash scripts/start_rotating_box.sh --check
#   bash scripts/start_rotating_box.sh --run [--duration SECONDS]
# This entry always selects its own YAML, regardless of old shell model overrides.
# 本入口固定选择对应 YAML，清除旧终端遗留的模型覆盖（例如先前的 QuicForge）。
# To use a different configuration/model override, use scripts/start_live.sh directly.
# 如需覆盖配置或选择其他模型，请直接使用 scripts/start_live.sh。
set -euo pipefail
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
export QUICDEPLOY_CONFIG="$ROOT/configs/lva_rotating_box_ep050_fp16.yaml"
unset QUICDEPLOY_MODEL
exec bash "$ROOT/scripts/start_live.sh" "$@"
