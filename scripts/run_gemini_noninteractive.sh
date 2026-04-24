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

# Gemini CLI defaults to interactive mode unless --prompt is provided.
# Pass the prompt file contents through --prompt so workflow stages run
# headlessly and terminate with a single response.
prompt_text=$(<"${prompt_file}")
exec gemini \
  -m "${gemini_model}" \
  --approval-mode plan \
  --output-format text \
  --prompt "${prompt_text}"
