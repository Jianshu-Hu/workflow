#!/bin/bash
set -euo pipefail

prompt_file=${1:?prompt_file is required}

script_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
repo_root=$(cd "${script_dir}/.." && pwd)
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
