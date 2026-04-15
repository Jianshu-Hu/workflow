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
echo "[workflow] transcript will be saved for later discussion summarization." >&2

prompt_dir=$(cd "$(dirname "${prompt_file}")" && pwd)
workspace_root=$(cd "${prompt_dir}/.." && pwd)
artifacts_dir="${workspace_root}/artifacts"
input_log="${artifacts_dir}/discussion_input.log"
output_log="${artifacts_dir}/discussion_output.log"
mkdir -p "${artifacts_dir}"
: > "${input_log}"
: > "${output_log}"

if ! command -v script >/dev/null 2>&1; then
  echo "[workflow] 'script' is required to capture the discussion transcript." >&2
  exit 1
fi

launcher_script=$(mktemp "${artifacts_dir}/claude_discussion_launch.XXXXXX.sh")
cleanup() {
  rm -f "${launcher_script}"
}
trap cleanup EXIT

cat > "${launcher_script}" <<'EOF'
#!/bin/bash
set -euo pipefail

prompt_file=${1:?prompt_file is required}
discussion_model=${2:?discussion_model is required}
bootstrap_prompt=$(<"${prompt_file}")

exec claude --model "${discussion_model}" "${bootstrap_prompt}"
EOF
chmod 700 "${launcher_script}"

cmd=("${launcher_script}" "${prompt_file}" "${discussion_model}")
command_string=$(printf '%q ' "${cmd[@]}")
exec script -qef -E never -I "${input_log}" -O "${output_log}" -c "${command_string}"
