#!/usr/bin/env bash
# Independent conda inference environment. / 独立的 conda 推理环境。
# bash scripts/setup_env.sh [--offline] [--dev]
# Offline uses uv's cache + outputs/env/wheelhouse; no old environment is referenced.
# 离线模式使用 uv 缓存和 outputs/env/wheelhouse，不引用旧项目环境。
# Native MIT / IK workers retain their separately pinned quest3 environment.
# 原生 MIT / IK 子进程仍使用单独锁定版本的 quest3 环境。
set -euo pipefail
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
source "$ROOT/scripts/conda_env.sh"
OFFLINE=(); LOCK=requirements.lock
for arg in "$@"; do
  case "$arg" in
    --offline) OFFLINE=(--offline) ;;
    --dev) LOCK=requirements-dev.lock ;;
    *) echo "Usage: bash $0 [--offline] [--dev]" >&2; exit 2 ;;
  esac
done
CONDA_BIN="$(quicdeploy_conda)"
ENV_PREFIX="$(quicdeploy_env_prefix)"
if [[ ! -x "$ENV_PREFIX/bin/python" ]]; then
  if [[ ${#OFFLINE[@]} -gt 0 ]]; then
    "$CONDA_BIN" create -y -p "$ENV_PREFIX" --offline --file conda-linux-64.lock
  else
    "$CONDA_BIN" create -y -p "$ENV_PREFIX" python=3.12.13 pip 'setuptools<82' wheel
  fi
fi
"$ENV_PREFIX/bin/python" -c 'import sys; assert sys.version_info[:2] == (3, 12), sys.version'
WHEELS=()
[[ ! -d "$ROOT/outputs/env/wheelhouse" ]] || WHEELS=(--find-links "$ROOT/outputs/env/wheelhouse")
uv pip install --python "$ENV_PREFIX/bin/python" "${OFFLINE[@]}" "${WHEELS[@]}" \
  --index-strategy unsafe-best-match -r "$LOCK"
uv pip install --python "$ENV_PREFIX/bin/python" --offline --no-deps --no-build-isolation -e .
"$ENV_PREFIX/bin/python" -m pip check
"$ENV_PREFIX/bin/python" -c "import torch,numpy,sys; print('Python:',sys.executable,'torch:',torch.__version__,'CUDA:',torch.cuda.is_available(),'numpy:',numpy.__version__)"
