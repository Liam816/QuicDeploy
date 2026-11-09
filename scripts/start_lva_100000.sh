#!/usr/bin/env bash
# LVA update_00100000: FP16, shared async/RTC, IK and command guards.
# 单帧 LVA 专用入口：FP16，复用异步 RTC、IK 与指令保护。
#   bash scripts/start_lva_100000.sh --check
#   bash scripts/start_lva_100000.sh --run [--duration SECONDS]
set -euo pipefail
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
# Pin this model even when the terminal still holds an older model override.
# 固定本模型配置，清除终端中遗留的其他模型覆盖。
export QUICDEPLOY_CONFIG="$ROOT/configs/lva_update_00100000.yaml"
unset QUICDEPLOY_MODEL
exec bash "$ROOT/scripts/start_live.sh" "$@"
