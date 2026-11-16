#!/usr/bin/env bash
# Generated model entry point; shared runtime, RTC, IK and guards.
# 自动生成的模型入口；共用运行时、RTC、IK 和指令保护。
set -euo pipefail
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
export QUICDEPLOY_CONFIG="$ROOT/configs/pick2box_1027_hf16_hs8_ws1_frozen_vitl16_cache_e50_update_00078400.yaml"
unset QUICDEPLOY_MODEL
exec bash "$ROOT/scripts/start_live.sh" "$@"
