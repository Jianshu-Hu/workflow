#!/bin/bash
set -euo pipefail

prompt_file=${1:?prompt_file is required}
discussion_model=${WORKFLOW_GEMINI_DISCUSSION_MODEL:-gemini-3.1-pro-preview}

if [[ ! -t 0 || ! -t 1 ]]; then
  echo "[workflow] Gemini discussion requires an interactive terminal." >&2
  exit 1
fi

echo "[workflow] launching Gemini kickoff discussion from $(pwd)" >&2
echo "[workflow] before quitting, make sure discussion.md reflects the final summary." >&2

exec gemini -m "${discussion_model}" -i "$(cat "${prompt_file}")"
