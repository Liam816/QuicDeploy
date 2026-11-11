#!/usr/bin/env bash
# QuicForge 1027 epoch50/step117150: shared RTC, IK and command guards.
# QuicForge 1027 专用入口：复用 RTC、IK 和指令保护。
#   bash scripts/start_pick2box_1027_quicforge.sh --check
#   bash scripts/start_pick2box_1027_quicforge.sh --run [--duration SECONDS]
set -euo pipefail
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
# Keep model selection independent of stale terminal overrides.
# 固定本模型配置，清除终端中遗留的模型覆盖。
export QUICDEPLOY_CONFIG="$ROOT/configs/lva_pick2box_1027_quicforge_ep050_step117150.yaml"
unset QUICDEPLOY_MODEL
exec bash "$ROOT/scripts/start_live.sh" "$@"
