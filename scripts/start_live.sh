#!/usr/bin/env bash
# Start model inference driving the real dual Piper-X arms.
# 启动模型推理并驱动真实双臂 Piper-X。
#
#   bash scripts/start_live.sh --check                  # config + hardware presence, no motion / 只检查，不运动
#   bash scripts/start_live.sh --run [--duration SEC]   # enable + home both arms, run policy / 使能、回零并运行策略
#
# Environment / 环境变量:
#   QUICDEPLOY_CONFIG   runtime YAML (default configs/runtime.yaml) / 运行参数文件
#   QUICDEPLOY_MODEL    model card overriding the YAML model / 覆盖 YAML 中模型的模型卡
#   PIPER_DIAGNOSTICS  1 = save every prediction (npz), 0/unset = off / 1 保存每次预测
#   PYTHON_BIN         interpreter (default .venv/bin/python) / Python 解释器
# Keys while running / 运行时按键: LEFT = smooth return + pause, RIGHT = resume, Ctrl+C = stop.
set -euo pipefail
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
export PYTHONDONTWRITEBYTECODE=1
PYTHON_BIN="${PYTHON_BIN:-$ROOT/.venv/bin/python}"
CONFIG="${QUICDEPLOY_CONFIG:-$ROOT/configs/runtime.yaml}"

if [[ $# -lt 1 || ( "$1" != "--check" && "$1" != "--run" ) ]]; then
  echo "Usage: bash $0 --check | --run [--duration SECONDS]" >&2
  exit 2
fi
case "${PIPER_DIAGNOSTICS:-0}" in
  0) DIAG=() ;;
  1) DIAG=(--diagnostics) ;;
  *) echo "PIPER_DIAGNOSTICS must be 0 or 1" >&2; exit 2 ;;
esac

QI=("$PYTHON_BIN" -m quicdeploy --config "$CONFIG")
[[ -n "${QUICDEPLOY_MODEL:-}" ]] && QI+=(--model "$QUICDEPLOY_MODEL")

# 1) Config + model artifact integrity, no hardware. / 配置与模型产物完整性，不连硬件。
"${QI[@]}" check
# 2) Physical presence before any arm is connected or homed. / 连接或回零之前检查硬件在位。
"${QI[@]}" preflight

if [[ "$1" == "--check" ]]; then
  exec "${QI[@]}" live
fi
shift
exec "${QI[@]}" live --run "${DIAG[@]}" "$@"
