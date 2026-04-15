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

gemini_model=${explicit_model:-${role_model:-${WORKFLOW_GEMINI_MODEL:-gemini-3.1-pro-preview}}}

if [[ ! -f "${prompt_file}" ]]; then
  echo "[workflow] prompt file not found: ${prompt_file}" >&2
  exit 1
fi

# Feed the prompt on stdin so large workflow prompts do not overflow argv.
exec gemini -m "${gemini_model}" < "${prompt_file}"
