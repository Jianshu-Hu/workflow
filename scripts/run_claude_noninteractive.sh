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

claude_args=(
  --print
  --output-format text
  --model "${claude_model}"
  --permission-mode bypassPermissions
  --tools ""
)

if [[ "${workflow_role}" == "reviewer" ]]; then
  review_json_schema=$(cat <<'EOF'
{"type":"object","properties":{"approved":{"type":"boolean"},"outcome_status":{"type":"string","enum":["pass","fail","inconclusive"]},"outcome_reason":{"type":"string"},"summary":{"type":"string"},"required_changes":{"type":"array","items":{"type":"string"}},"human_intervention_required":{"type":"boolean"},"human_intervention_reason":{"type":"string"}},"required":["approved","outcome_status","outcome_reason","summary","required_changes","human_intervention_required","human_intervention_reason"],"additionalProperties":false}
EOF
)
  claude_args+=(--json-schema "${review_json_schema}")
fi

# Feed the prompt on stdin so large workflow prompts do not overflow argv.
# Disable tools for noninteractive workflow stages so Claude emits text output
# instead of attempting edits or permission-gated tool actions. Reviewer runs
# additionally use a JSON schema so the response is machine-parseable.
exec claude "${claude_args[@]}" < "${prompt_file}"
