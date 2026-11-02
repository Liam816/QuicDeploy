#!/usr/bin/env bash
# Shared conda location; never add the old project's site-packages.
# 共用 conda 定位逻辑；不再把旧项目的 site-packages 加入环境。
quicdeploy_conda() {
  local candidate="${CONDA_EXE:-}"
  [[ -x "$candidate" ]] || candidate="$(command -v conda || true)"
  [[ -x "$candidate" ]] || candidate="$HOME/miniforge3/bin/conda"
  [[ -x "$candidate" ]] || { echo 'conda not found; set CONDA_EXE' >&2; return 1; }
  printf '%s\n' "$candidate"
}
quicdeploy_env_prefix() {
  if [[ -n "${QUICDEPLOY_ENV_PREFIX:-}" ]]; then
    printf '%s\n' "$QUICDEPLOY_ENV_PREFIX"
  else
    local conda_bin
    conda_bin="$(quicdeploy_conda)" || return
    printf '%s/envs/quic-deploy\n' "$("$conda_bin" info --base)"
  fi
}
