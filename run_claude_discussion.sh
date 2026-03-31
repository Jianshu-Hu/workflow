#!/bin/bash
set -euo pipefail

prompt_file=${1:?prompt_file is required}
explicit_model=${2:-}
discussion_model=${explicit_model:-${WORKFLOW_DISCUSSION_MODEL:-${WORKFLOW_CLAUDE_DISCUSSION_MODEL:-${WORKFLOW_CLAUDE_MODEL:-sonnet}}}}

if [[ ! -f "${prompt_file}" ]]; then
  echo "[workflow] prompt file not found: ${prompt_file}" >&2
  exit 1
fi

if [[ ! -t 0 || ! -t 1 ]]; then
  echo "[workflow] Discussion mode requires an interactive terminal." >&2
  exit 1
fi

echo "[workflow] launching Claude kickoff discussion from $(pwd)" >&2
echo "[workflow] before quitting, make sure discussion.md reflects the final summary." >&2

bootstrap_prompt="Read and follow the workflow kickoff instructions in ${prompt_file}. Start by opening that file, then continue the discussion interactively."

exec claude --model "${discussion_model}" "${bootstrap_prompt}"
