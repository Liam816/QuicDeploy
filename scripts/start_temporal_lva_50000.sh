#!/usr/bin/env bash
# Temporal-LVA update_00050000: 16 frames / stride 8 / FP16 / cached history.
# 时序模型专用入口：16 帧、步长 8、FP16；复用历史缓存、RTC、IK 与指令保护。
#   bash scripts/start_temporal_lva_50000.sh --check
#   bash scripts/start_temporal_lva_50000.sh --run [--duration SECONDS]
set -euo pipefail
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
# Pin this profile even when the shell still contains overrides for older models.
# 固定本模型配置，清除终端中遗留的其他模型覆盖。
export QUICDEPLOY_CONFIG="$ROOT/configs/temporal_lva_update_00050000.yaml"
unset QUICDEPLOY_MODEL
exec bash "$ROOT/scripts/start_live.sh" "$@"
