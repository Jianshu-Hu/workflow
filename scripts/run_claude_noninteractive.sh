#!/bin/bash
set -euo pipefail

prompt_file=${1:?prompt_file is required}
workflow_role=${2:-planner}
explicit_model=${3:-}

case "${workflow_role}" in
  planner)
    role_model=${WORKFLOW_PLANNER_MODEL:-}
    ;;
  reviewer)
    role_model=${WORKFLOW_REVIEWER_MODEL:-}
    ;;
  *)
    role_model=
    ;;
esac

claude_model=${explicit_model:-${role_model:-${WORKFLOW_CLAUDE_MODEL:-sonnet}}}

if [[ ! -f "${prompt_file}" ]]; then
  echo "[workflow] prompt file not found: ${prompt_file}" >&2
  exit 1
fi

# Feed the prompt on stdin so large workflow prompts do not overflow argv.
# Disable tools for noninteractive workflow stages so Claude emits plain text
# output instead of attempting edits or permission-gated tool actions.
exec claude \
  --print \
  --output-format text \
  --model "${claude_model}" \
  --permission-mode bypassPermissions \
  --tools "" \
  < "${prompt_file}"
