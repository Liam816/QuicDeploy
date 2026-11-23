#!/usr/bin/env bash
# Import an uploaded checkpoint; no robot connection or motion.
# 导入上传的 checkpoint，不连接或运动机械臂。
#   bash scripts/import_quicforge_model_ckpt.sh /path/to/checkpoint [--dry-run] [--object-id ID]
set -euo pipefail
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
source "$ROOT/scripts/conda_env.sh"
if [[ -z "${PYTHON_BIN:-}" ]]; then
  PYTHON_BIN="$(quicdeploy_env_prefix)/bin/python"
fi
[[ -x "$PYTHON_BIN" ]] || { echo "Missing Python: $PYTHON_BIN" >&2; exit 1; }
export PYTHONDONTWRITEBYTECODE=1
exec "$PYTHON_BIN" "$ROOT/tools/import_quicforge.py" "$@"
