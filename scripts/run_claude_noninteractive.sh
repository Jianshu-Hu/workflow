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

# Some local proxy layers return malformed payloads for Claude Code's
# noninteractive transport even when the configured API endpoint itself works.
# Preserve the custom Anthropic endpoint/token from the user's shell, but
# avoid routing workflow CLI calls through HTTP/SOCKS proxies.
unset HTTP_PROXY HTTPS_PROXY ALL_PROXY http_proxy https_proxy all_proxy

claude_args=(
  --print
  --output-format text
  --model "${claude_model}"
  --permission-mode bypassPermissions
  --tools ""
)

if [[ "${workflow_role}" == "reviewer" ]]; then
  review_instruction=$(cat <<'EOF'
Return exactly one JSON object and nothing else.
The JSON object must follow this schema:
{"type":"object","properties":{"approved":{"type":"boolean"},"outcome_status":{"type":"string","enum":["pass","fail","inconclusive"]},"outcome_reason":{"type":"string"},"summary":{"type":"string"},"required_changes":{"type":"array","items":{"type":"string"}},"human_intervention_required":{"type":"boolean"},"human_intervention_reason":{"type":"string"}},"required":["approved","outcome_status","outcome_reason","summary","required_changes","human_intervention_required","human_intervention_reason"],"additionalProperties":false}
EOF
)
  prompt_dir=$(dirname "${prompt_file}")
  reviewer_prompt=$(mktemp "${prompt_dir}/reviewer_prompt.XXXXXX.txt")
  cleanup() {
    rm -f "${reviewer_prompt}"
  }
  trap cleanup EXIT
  {
    cat "${prompt_file}"
    printf '\n\n%s\n' "${review_instruction}"
  } > "${reviewer_prompt}"
  prompt_file="${reviewer_prompt}"
fi

# Feed the prompt on stdin so large workflow prompts do not overflow argv.
# Disable tools for noninteractive workflow stages so Claude emits text output
# instead of attempting edits or permission-gated tool actions. Reviewer runs
# append an explicit schema contract to the prompt instead of relying on the
# CLI's --json-schema flag, because some Anthropic-compatible gateways hang on
# structured-output mode even when plain --print succeeds.
exec claude "${claude_args[@]}" < "${prompt_file}"
