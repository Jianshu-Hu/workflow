#!/bin/bash
set -euo pipefail

if [[ $# -lt 1 ]]; then
  echo "Usage: $0 <workflow-run-folder> [--lesson-id ID] [--timeout SECONDS] [--dry-run]" >&2
  exit 2
fi

run_folder=$1
shift

script_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
repo_root=$(cd "${script_dir}/../.." && pwd)

if command -v python3 >/dev/null 2>&1; then
  python_bin=python3
else
  python_bin=python
fi

exec "${python_bin}" "${repo_root}/workflow/memory/evaluate_lesson.py" "${run_folder}" "$@"
