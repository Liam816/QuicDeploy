#!/usr/bin/env bash
# Action-GT pick2box epoch50/step37250: shared async RTC, IK and command guards.
# action-GT pick2box 专用入口：复用异步 RTC、IK 和指令保护。
#   bash scripts/start_lva_pick2box_37250.sh --check
#   bash scripts/start_lva_pick2box_37250.sh --run [--duration SECONDS]
set -euo pipefail
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
# Pin this profile even when the terminal holds a stale model/config override.
# 固定本模型配置，清除终端遗留的其他模型覆盖。
export QUICDEPLOY_CONFIG="$ROOT/configs/lva_pick2box_quicforge_ep050_step37250.yaml"
unset QUICDEPLOY_MODEL
exec bash "$ROOT/scripts/start_live.sh" "$@"
