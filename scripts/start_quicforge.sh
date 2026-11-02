#!/usr/bin/env bash
# Select the imported QuicForge model and reuse the standard live launcher.
# 选择本次 QuicForge 模型，复用标准启动器及同一份 runtime.yaml（不复制保护参数）。
set -euo pipefail
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
export QUICDEPLOY_MODEL="${QUICDEPLOY_MODEL:-$ROOT/configs/models/quicforge_pick_kun_ep050_step43700_fp16.json}"
exec bash "$ROOT/scripts/start_live.sh" "$@"
