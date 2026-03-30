#!/bin/bash
set -euo pipefail

script_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
repo_root=${WORKFLOW_REPO_ROOT:-$(cd "${script_dir}/.." && pwd)}
python_bin=${WORKFLOW_PYTHON:-python}
config_path=${WORKFLOW_CONFIG:-${script_dir}/config.gemini.example.yaml}
default_workspace=${WORKFLOW_WORKSPACE:-workflow_runs/default}
bootstrap_cmd=${WORKFLOW_BOOTSTRAP_CMD:-}
preflight_cmd=${WORKFLOW_PREFLIGHT_CMD:-}

cd "${repo_root}"

if [[ -n "${bootstrap_cmd}" ]]; then
  echo "[workflow] running bootstrap command"
  eval "${bootstrap_cmd}"
fi

export WORKFLOW_EXECUTOR_CWD="${WORKFLOW_EXECUTOR_CWD:-${repo_root}}"
export WORKFLOW_CODEX_SANDBOX="${WORKFLOW_CODEX_SANDBOX:-danger-full-access}"
export WORKFLOW_CODEX_INHERIT_ENV="${WORKFLOW_CODEX_INHERIT_ENV:-1}"
export WORKFLOW_CODEX_BYPASS_APPROVALS="${WORKFLOW_CODEX_BYPASS_APPROVALS:-1}"
export WORKFLOW_MAX_AUTO_REPLANS_PER_STEP="${WORKFLOW_MAX_AUTO_REPLANS_PER_STEP:-3}"
export WORKFLOW_RENDER_PREFLIGHT_HOSTNAME="$(hostname)"
export WORKFLOW_RENDER_PREFLIGHT_CUDA_VISIBLE_DEVICES="${CUDA_VISIBLE_DEVICES:-<unset>}"
export WORKFLOW_RENDER_PREFLIGHT_STATUS="not-run"

echo "[workflow] hostname=$(hostname)"
echo "[workflow] repo_root=${repo_root}"
echo "[workflow] python=${python_bin}"
echo "[workflow] config=${config_path}"
echo "[workflow] CUDA_VISIBLE_DEVICES=${CUDA_VISIBLE_DEVICES:-<unset>}"
echo "[workflow] WORKFLOW_EXECUTOR_CWD=${WORKFLOW_EXECUTOR_CWD}"
echo "[workflow] WORKFLOW_CODEX_SANDBOX=${WORKFLOW_CODEX_SANDBOX}"
echo "[workflow] WORKFLOW_CODEX_INHERIT_ENV=${WORKFLOW_CODEX_INHERIT_ENV}"
echo "[workflow] WORKFLOW_CODEX_BYPASS_APPROVALS=${WORKFLOW_CODEX_BYPASS_APPROVALS}"
echo "[workflow] WORKFLOW_MAX_AUTO_REPLANS_PER_STEP=${WORKFLOW_MAX_AUTO_REPLANS_PER_STEP}"

if command -v nvidia-smi >/dev/null 2>&1; then
  echo "[workflow] nvidia-smi -L"
  nvidia-smi -L || true
fi

if [[ -n "${preflight_cmd}" && "${WORKFLOW_SKIP_RENDER_PREFLIGHT:-0}" != "1" ]]; then
  echo "[workflow] running host preflight: ${preflight_cmd}"
  bash -lc "${preflight_cmd}"
  export WORKFLOW_RENDER_PREFLIGHT_STATUS="passed"
elif [[ "${WORKFLOW_SKIP_RENDER_PREFLIGHT:-0}" == "1" ]]; then
  export WORKFLOW_RENDER_PREFLIGHT_STATUS="skipped"
else
  export WORKFLOW_RENDER_PREFLIGHT_STATUS="not-configured"
fi

if [[ $# -eq 0 ]]; then
  set -- --workspace "${default_workspace}" --config "${config_path}" loop
fi

exec "${python_bin}" "${script_dir}/orchestrator.py" "$@"
