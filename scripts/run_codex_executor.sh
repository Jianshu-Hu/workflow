#!/bin/bash
set -euo pipefail

prompt_file=${1:?prompt_file is required}

resolve_repo_root() {
  local workflow_root_local="$1"
  local launch_dir_local="$2"
  local launch_git_root=""
  local parent_git_root=""

  if [[ -n "${WORKFLOW_REPO_ROOT:-}" ]]; then
    printf '%s\n' "${WORKFLOW_REPO_ROOT}"
    return 0
  fi

  launch_git_root=$(git -C "${launch_dir_local}" rev-parse --show-toplevel 2>/dev/null || true)
  if [[ -n "${launch_git_root}" && "${launch_git_root}" != "${workflow_root_local}" ]]; then
    printf '%s\n' "${launch_git_root}"
    return 0
  fi

  parent_git_root=$(git -C "${workflow_root_local}/.." rev-parse --show-toplevel 2>/dev/null || true)
  if [[ -n "${parent_git_root}" && "${parent_git_root}" != "${workflow_root_local}" ]]; then
    printf '%s\n' "${parent_git_root}"
    return 0
  fi

  printf '%s\n' "$(cd "${workflow_root_local}/.." && pwd)"
}

launch_dir=$(pwd)
script_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
workflow_root=$(cd "${script_dir}/.." && pwd)
repo_root=$(resolve_repo_root "${workflow_root}" "${launch_dir}")
executor_root=${WORKFLOW_EXECUTOR_CWD:-${repo_root}}
sandbox_mode=${WORKFLOW_CODEX_SANDBOX:-danger-full-access}
inherit_env=${WORKFLOW_CODEX_INHERIT_ENV:-1}
bypass_approvals=${WORKFLOW_CODEX_BYPASS_APPROVALS:-0}

cmd=(codex exec --skip-git-repo-check -C "${executor_root}")

if [[ "${bypass_approvals}" == "1" ]]; then
  cmd+=(--dangerously-bypass-approvals-and-sandbox)
else
  cmd+=(-s "${sandbox_mode}")
fi

if [[ "${inherit_env}" == "1" ]]; then
  cmd+=(-c shell_environment_policy.inherit=all)
fi

exec "${cmd[@]}" - < "${prompt_file}"
