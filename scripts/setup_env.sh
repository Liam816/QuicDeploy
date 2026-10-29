#!/usr/bin/env bash
# Create the project venv (.venv). / 创建项目虚拟环境 .venv。
#
#   bash scripts/setup_env.sh            # reuse a validated runtime (default, offline) / 复用已验证的运行时（默认，离线）
#   bash scripts/setup_env.sh --install  # fresh pinned install, needs PyPI + PyTorch index / 全新安装，需要外网
#
# Reuse mode links QUICINFER_RUNTIME_SITE (the site-packages holding the exact
# torch/numpy build named in the model cards' runtime_versions) via a .pth file.
# 复用模式通过 .pth 链接 QUICINFER_RUNTIME_SITE（包含模型卡 runtime_versions 指定的 torch/numpy）。
#
# The IK worker and native MIT workers do NOT use this env: they run under the
# external envs in configs/hardware/piper_x (kinematics.python / quest_python).
# IK 子进程与原生 MIT 子进程不使用本环境，而使用 configs/hardware/piper_x 中指定的外部 Python。
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
BASE_PYTHON="${QUICINFER_BASE_PYTHON:-/home/ih/miniforge3/envs/lerobot/bin/python}"
RUNTIME_SITE="${QUICINFER_RUNTIME_SITE:-/home/ih/workspace/yihuigou/openarm_workspace/.venv/lib/python3.12/site-packages}"

if [[ "${1:-}" == "--install" ]]; then
  uv venv --python 3.12 .venv
  uv pip install --python .venv/bin/python --index-strategy unsafe-best-match -r requirements.txt
else
  [[ -d "$RUNTIME_SITE" ]] || { echo "Missing runtime site-packages: $RUNTIME_SITE" >&2; exit 1; }
  uv venv --python "$BASE_PYTHON" .venv
  SITE="$(.venv/bin/python -c 'import sysconfig; print(sysconfig.get_paths()["purelib"])')"
  echo "import site; site.addsitedir('$RUNTIME_SITE')" > "$SITE/quicinfer_runtime.pth"
fi
uv pip install --python .venv/bin/python --no-deps --no-build-isolation --offline -e . \
  || echo "$(pwd)" > "$(.venv/bin/python -c 'import sysconfig; print(sysconfig.get_paths()["purelib"])')/quicinfer_src.pth"
.venv/bin/python -c "import torch, numpy, quicinfer; print('torch', torch.__version__, 'cuda', torch.cuda.is_available(), 'numpy', numpy.__version__, 'quicinfer', quicinfer.__file__)"
