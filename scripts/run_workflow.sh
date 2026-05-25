#!/bin/bash
set -euo pipefail

resolve_python_bin() {
  local configured="${1:-python}"
  local resolved=""

  if [[ "${configured}" != "python" ]]; then
    printf '%s\n' "${configured}"
    return 0
  fi

  resolved=$(command -v python 2>/dev/null || true)
  if [[ -n "${resolved}" ]]; then
    printf '%s\n' "${resolved}"
    return 0
  fi

  resolved=$(command -v python3 2>/dev/null || true)
  if [[ -n "${resolved}" ]]; then
    printf '%s\n' "${resolved}"
    return 0
  fi

  echo "[workflow] error: neither 'python' nor 'python3' is available. Set WORKFLOW_PYTHON explicitly." >&2
  return 1
}

extract_cli_option_value() {
  local option="$1"
  shift

  local arg
  local expect_value=0

  for arg in "$@"; do
    if [[ "${expect_value}" == "1" ]]; then
      printf '%s\n' "${arg}"
      return 0
    fi

    case "${arg}" in
      "${option}")
        expect_value=1
        ;;
      "${option}"=*)
        printf '%s\n' "${arg#*=}"
        return 0
        ;;
    esac
  done

  return 1
}

resolve_workflow_command() {
  local arg
  local skip_next=0

  for arg in "$@"; do
    if [[ "${skip_next}" == "1" ]]; then
      skip_next=0
      continue
    fi

    case "${arg}" in
      --workspace|--config)
        skip_next=1
        continue
        ;;
      --workspace=*|--config=*)
        continue
        ;;
      -h|--help)
        printf '%s\n' "__help__"
        return 0
        ;;
      init|plan|run-step|review|loop|status)
        printf '%s\n' "${arg}"
        return 0
        ;;
    esac
  done

  return 1
}

resolve_workspace_dir() {
  local repo_root_local="$1"
  local workspace_path="$2"

  if [[ "${workspace_path}" = /* ]]; then
    printf '%s\n' "${workspace_path}"
  else
    printf '%s\n' "${repo_root_local}/${workspace_path}"
  fi
}

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

detect_provider_from_hint() {
  local hint="${1:-}"

  hint=$(printf '%s' "${hint}" | tr '[:upper:]' '[:lower:]')

  case "${hint}" in
    *claude*|*sonnet*|*opus*|*haiku*)
      printf 'claude\n'
      return 0
      ;;
    *gemini*)
      printf 'gemini\n'
      return 0
      ;;
  esac

  return 1
}

detect_provider_from_runtime_env() {
  local runtime_env_path="$1"
  local line
  local variable_name
  local variable_value
  local provider=""

  if [[ ! -f "${runtime_env_path}" ]]; then
    return 1
  fi

  while IFS= read -r line || [[ -n "${line}" ]]; do
    case "${line}" in
      export\ *=*|*=*)
        ;;
      *)
        continue
        ;;
    esac

    line="${line#export }"
    variable_name="${line%%=*}"
    variable_value="${line#*=}"

    case "${variable_name}" in
      WORKFLOW_*MODEL)
        ;;
      *)
        continue
        ;;
    esac

    provider=$(detect_provider_from_hint "${variable_name}" || true)
    if [[ -n "${provider}" ]]; then
      printf '%s\n' "${provider}"
      return 0
    fi

    variable_value="${variable_value%\"}"
    variable_value="${variable_value#\"}"
    variable_value="${variable_value%\'}"
    variable_value="${variable_value#\'}"

    provider=$(detect_provider_from_hint "${variable_value}" || true)
    if [[ -n "${provider}" ]]; then
      printf '%s\n' "${provider}"
      return 0
    fi
  done < "${runtime_env_path}"

  return 1
}

detect_provider_from_environment() {
  local variable_name
  local variable_value
  local provider=""

  for variable_name in \
    WORKFLOW_PLANNER_MODEL \
    WORKFLOW_REVIEWER_MODEL \
    WORKFLOW_DISCUSSION_MODEL \
    WORKFLOW_GEMINI_MODEL \
    WORKFLOW_GEMINI_DISCUSSION_MODEL \
    WORKFLOW_CLAUDE_MODEL \
    WORKFLOW_CLAUDE_DISCUSSION_MODEL
  do
    variable_value="${!variable_name:-}"
    if [[ -z "${variable_value}" ]]; then
      continue
    fi

    provider=$(detect_provider_from_hint "${variable_name}" || true)
    if [[ -n "${provider}" ]]; then
      printf '%s\n' "${provider}"
      return 0
    fi

    provider=$(detect_provider_from_hint "${variable_value}" || true)
    if [[ -n "${provider}" ]]; then
      printf '%s\n' "${provider}"
      return 0
    fi
  done

  return 1
}

detect_provider_from_discussion_artifacts() {
  local workspace_dir_local="$1"
  local path
  local provider=""

  for path in \
    "${workspace_dir_local}/prompts/discussion_prompt.txt" \
    "${workspace_dir_local}/prompts/discussion_summary_prompt.txt" \
    "${workspace_dir_local}/artifacts/discussion_transcript.txt" \
    "${workspace_dir_local}/artifacts/discussion_input.log"
  do
    if [[ ! -f "${path}" ]]; then
      continue
    fi
    provider=$(detect_provider_from_hint "$(sed -n '1,80p' "${path}")" || true)
    if [[ -n "${provider}" ]]; then
      printf '%s\n' "${provider}"
      return 0
    fi
  done

  return 1
}

print_path_summary() {
  echo "[workflow] launch_dir=${launch_dir}"
  echo "[workflow] workflow_root=${workflow_root}"
  echo "[workflow] repo_root=${repo_root}"
  echo "[workflow] config=${config_path}"
  echo "[workflow] workspace=${workspace_dir}"
}

run_hook_command() {
  local label="$1"
  shift
  local command=("$@")

  if [[ "${#command[@]}" -eq 0 ]]; then
    return 0
  fi

  echo "[workflow] running ${label}: ${command[*]}"
  "${command[@]}"
}

resolve_default_config_path() {
  local workflow_root_local="$1"
  local workspace_dir_local="$2"
  local provider=""

  provider=$(detect_provider_from_runtime_env "${workspace_dir_local}/runtime.env" || true)
  if [[ -z "${provider}" ]]; then
    provider=$(detect_provider_from_environment || true)
  fi
  if [[ -z "${provider}" ]]; then
    provider=$(detect_provider_from_discussion_artifacts "${workspace_dir_local}" || true)
  fi

  case "${provider}" in
    claude)
      printf '%s\n' "${workflow_root_local}/configs/config.claude.example.yaml"
      ;;
    *)
      printf '%s\n' "${workflow_root_local}/configs/config.gemini.example.yaml"
      ;;
  esac
}

prepare_detached_workspace() {
  local workspace_dir_local="$1"
  local log_file="$2"
  local pid_file="$3"
  local archive_dir="${workspace_dir_local}/artifacts/workflow_logs"
  local timestamp
  local archived_log=""
  local archived_pid=""
  local existing_pid=""

  mkdir -p "${workspace_dir_local}"

  if [[ -f "${pid_file}" ]]; then
    existing_pid="$(tr -d '[:space:]' < "${pid_file}" || true)"
    if [[ -n "${existing_pid}" ]] && kill -0 "${existing_pid}" 2>/dev/null; then
      return 0
    fi
  fi

  if [[ "${WORKFLOW_APPEND_LOG:-0}" != "1" && -f "${log_file}" ]]; then
    timestamp=$(date -u +%Y%m%dT%H%M%SZ)
    mkdir -p "${archive_dir}"
    archived_log="${archive_dir}/$(basename "${log_file}").${timestamp}"
    mv "${log_file}" "${archived_log}"
    echo "[workflow] archived previous log=${archived_log}"
  fi

  if [[ -f "${pid_file}" ]]; then
    timestamp=${timestamp:-$(date -u +%Y%m%dT%H%M%SZ)}
    mkdir -p "${archive_dir}"
    archived_pid="${archive_dir}/$(basename "${pid_file}").${timestamp}"
    mv "${pid_file}" "${archived_pid}"
    echo "[workflow] archived stale pid=${archived_pid}"
  fi
}

launch_dir=$(pwd)
script_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
script_path="${BASH_SOURCE[0]}"
if command -v realpath >/dev/null 2>&1; then
  script_path=$(realpath "${script_path}" 2>/dev/null || printf '%s\n' "${script_path}")
elif command -v readlink >/dev/null 2>&1; then
  script_path=$(readlink -f "${script_path}" 2>/dev/null || printf '%s\n' "${script_path}")
fi

workflow_root=$(cd "${script_dir}/.." && pwd)
repo_root=$(resolve_repo_root "${workflow_root}" "${launch_dir}")
python_bin=${WORKFLOW_PYTHON:-python}
default_workspace=${WORKFLOW_WORKSPACE:-workflow_runs/default}
cli_workspace=$(extract_cli_option_value --workspace "$@" || true)
workspace_path=${cli_workspace:-${default_workspace}}
workspace_dir=$(resolve_workspace_dir "${repo_root}" "${workspace_path}")
cli_config=$(extract_cli_option_value --config "$@" || true)
config_path=${cli_config:-${WORKFLOW_CONFIG:-$(resolve_default_config_path "${workflow_root}" "${workspace_dir}")}}
bootstrap_cmd=${WORKFLOW_BOOTSTRAP_CMD:-}
preflight_cmd=${WORKFLOW_PREFLIGHT_CMD:-}
bootstrap_script=${WORKFLOW_BOOTSTRAP_SCRIPT:-}
preflight_script=${WORKFLOW_PREFLIGHT_SCRIPT:-}
workflow_command=$(resolve_workflow_command "$@" || true)

cd "${repo_root}"

python_bin=$(resolve_python_bin "${python_bin}")

if [[ "${WORKFLOW_DETACHED:-0}" != "1" && "${WORKFLOW_DETACH:-1}" == "1" ]]; then
  if [[ $# -eq 0 || "${workflow_command}" == "loop" ]]; then
    print_path_summary
    mkdir -p "${workspace_dir}"
    log_file=${WORKFLOW_LOG_FILE:-${workspace_dir}/workflow.output.log}
    pid_file=${WORKFLOW_PID_FILE:-${workspace_dir}/workflow.pid}
    prepare_detached_workspace "${workspace_dir}" "${log_file}" "${pid_file}"

    nohup env \
      WORKFLOW_DETACHED=1 \
      WORKFLOW_LOG_FILE="${log_file}" \
      WORKFLOW_PID_FILE="${pid_file}" \
      bash "${script_path}" "$@" >"${log_file}" 2>&1 < /dev/null &
    workflow_pid=$!
    printf '%s\n' "${workflow_pid}" > "${pid_file}"

    echo "[workflow] started in background"
    echo "[workflow] pid=${workflow_pid}"
    echo "[workflow] log=${log_file}"
    exit 0
  fi
fi

if [[ -n "${bootstrap_script}" ]]; then
  run_hook_command "bootstrap script" "${bootstrap_script}"
elif [[ -n "${bootstrap_cmd}" ]]; then
  echo "[workflow] running bootstrap command via legacy shell hook"
  eval "${bootstrap_cmd}"
fi

export WORKFLOW_EXECUTOR_CWD="${WORKFLOW_EXECUTOR_CWD:-${repo_root}}"
export WORKFLOW_CODEX_SANDBOX="${WORKFLOW_CODEX_SANDBOX:-danger-full-access}"
export WORKFLOW_CODEX_INHERIT_ENV="${WORKFLOW_CODEX_INHERIT_ENV:-1}"
export WORKFLOW_CODEX_BYPASS_APPROVALS="${WORKFLOW_CODEX_BYPASS_APPROVALS:-1}"
export WORKFLOW_MAX_AUTO_REPLANS_PER_STEP="${WORKFLOW_MAX_AUTO_REPLANS_PER_STEP:-3}"
export WORKFLOW_RENDER_PREFLIGHT_HOSTNAME="$(hostname)"
export WORKFLOW_RENDER_PREFLIGHT_CUDA_VISIBLE_DEVICES="${CUDA_VISIBLE_DEVICES:-<unset>}"
export WORKFLOW_RENDER_PREFLIGHT_STATUS="not-run"

echo "[workflow] hostname=$(hostname)"
print_path_summary
echo "[workflow] python=${python_bin}"
echo "[workflow] log_file=${WORKFLOW_LOG_FILE:-<stdout>}"
echo "[workflow] CUDA_VISIBLE_DEVICES=${CUDA_VISIBLE_DEVICES:-<unset>}"
echo "[workflow] WORKFLOW_EXECUTOR_CWD=${WORKFLOW_EXECUTOR_CWD}"
echo "[workflow] WORKFLOW_CODEX_SANDBOX=${WORKFLOW_CODEX_SANDBOX}"
echo "[workflow] WORKFLOW_CODEX_INHERIT_ENV=${WORKFLOW_CODEX_INHERIT_ENV}"
echo "[workflow] WORKFLOW_CODEX_BYPASS_APPROVALS=${WORKFLOW_CODEX_BYPASS_APPROVALS}"
echo "[workflow] WORKFLOW_MAX_AUTO_REPLANS_PER_STEP=${WORKFLOW_MAX_AUTO_REPLANS_PER_STEP}"

if command -v nvidia-smi >/dev/null 2>&1; then
  echo "[workflow] nvidia-smi -L"
  nvidia-smi -L || true
fi

if [[ "${WORKFLOW_SKIP_RENDER_PREFLIGHT:-0}" != "1" ]]; then
  if [[ -n "${preflight_script}" ]]; then
    run_hook_command "host preflight script" "${preflight_script}"
    export WORKFLOW_RENDER_PREFLIGHT_STATUS="passed"
  elif [[ -n "${preflight_cmd}" ]]; then
    echo "[workflow] running host preflight via legacy shell hook: ${preflight_cmd}"
    bash -lc "${preflight_cmd}"
    export WORKFLOW_RENDER_PREFLIGHT_STATUS="passed"
  else
    export WORKFLOW_RENDER_PREFLIGHT_STATUS="not-configured"
  fi
elif [[ "${WORKFLOW_SKIP_RENDER_PREFLIGHT:-0}" == "1" ]]; then
  export WORKFLOW_RENDER_PREFLIGHT_STATUS="skipped"
fi

if [[ $# -eq 0 ]]; then
  set -- --workspace "${default_workspace}" --config "${config_path}" loop
elif [[ -z "${cli_config}" ]]; then
  set -- --config "${config_path}" "$@"
fi

exec "${python_bin}" "${workflow_root}/orchestrator.py" "$@"
