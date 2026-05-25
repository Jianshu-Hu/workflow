#!/bin/bash
set -euo pipefail

if [[ $# -lt 1 ]]; then
  echo "Usage: $0 <workflow-run-folder> [--lesson-id ID] [--timeout SECONDS] [--dry-run]" >&2
  exit 2
fi

run_folder=$1
shift

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

script_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
launch_dir=$(pwd)
workflow_root=$(cd "${script_dir}/.." && pwd)
repo_root=$(resolve_repo_root "${workflow_root}" "${launch_dir}")

export WORKFLOW_REPO_ROOT="${repo_root}"

if [[ -z "${WORKFLOW_LESSON_CODEX_CMD:-}" ]]; then
  export WORKFLOW_LESSON_CODEX_CMD="bash ${workflow_root}/scripts/run_codex_executor.sh {prompt_file}"
fi
if [[ -z "${WORKFLOW_LESSON_GEMINI_CMD:-}" ]]; then
  export WORKFLOW_LESSON_GEMINI_CMD="bash ${workflow_root}/scripts/run_gemini_noninteractive.sh {prompt_file} lesson {model}"
fi
if [[ -z "${WORKFLOW_LESSON_CLAUDE_CMD:-}" ]]; then
  export WORKFLOW_LESSON_CLAUDE_CMD="bash ${workflow_root}/scripts/run_claude_noninteractive.sh {prompt_file} lesson {model}"
fi
export WORKFLOW_CODEX_SANDBOX="${WORKFLOW_CODEX_SANDBOX:-workspace-write}"

if command -v python3 >/dev/null 2>&1; then
  python_bin=python3
else
  python_bin=python
fi

exec "${python_bin}" "${workflow_root}/memory/evaluate_lesson.py" "${run_folder}" "$@"
