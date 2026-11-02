#!/usr/bin/env bash
# Pick2Box 1027 epoch39/step91377 FP16; reuse the checked standard launcher.
# Pick2Box 1027 专用入口：复用标准启动器及现有观测、调度、IK、指令保护。
#   bash scripts/start_pick2box_1027.sh --check
#   bash scripts/start_pick2box_1027.sh --run [--duration SECONDS]
# Always select this model config; ignore old shell config/model overrides.
# 固定选择本模型配置，清除终端遗留的其他配置/模型覆盖。
# Use scripts/start_live.sh directly for manual configuration overrides.
# 如需手动覆盖配置或模型，请直接使用 scripts/start_live.sh。
set -euo pipefail
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
export QUICDEPLOY_CONFIG="$ROOT/configs/lva_pick2box_1027_ep039_fp16.yaml"
unset QUICDEPLOY_MODEL
exec bash "$ROOT/scripts/start_live.sh" "$@"
