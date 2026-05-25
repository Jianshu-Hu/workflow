# Workflow Scripts

This folder contains shell wrappers used by the orchestrator and the daily loop
runner.

## Main Loop Wrapper

Use `run_workflow.sh` for execute / review loop runs:

```bash
bash workflow/scripts/run_workflow.sh \
  --workspace workflow_runs/my-task \
  loop
```

Default behavior:

- Resolves `python`, falling back to `python3` when needed.
- Changes to the repository root that contains `workflow/`.
- Auto-selects the workflow config when `--config` is not provided.
- Runs loop commands in the background unless `WORKFLOW_DETACH=0` is set.
- Writes `workflow.output.log` and `workflow.pid` under the workspace.
- Archives stale previous loop logs under `artifacts/workflow_logs/`.
- Prints host, Python, config, workspace, CUDA, and Codex execution settings.

The wrapper also accepts the orchestrator commands directly, including `init`,
`plan`, `run-step`, `review`, `loop`, and `status`.

With no arguments, the wrapper defaults to:

```bash
python workflow/orchestrator.py \
  --workspace workflow_runs/default \
  --config workflow/configs/config.gemini.example.yaml \
  loop
```

## Common Environment Variables

- `WORKFLOW_WORKSPACE`: default workspace when `--workspace` is omitted.
- `WORKFLOW_CONFIG`: default config when `--config` is omitted.
- `WORKFLOW_PYTHON`: Python executable to use.
- `WORKFLOW_DETACH=0`: run the loop in the foreground.
- `WORKFLOW_APPEND_LOG=1`: append to the existing loop log instead of archiving it.
- `WORKFLOW_LOG_FILE`: override the detached loop log path.
- `WORKFLOW_PID_FILE`: override the detached loop pid path.
- `WORKFLOW_REPO_ROOT`: override the repository root used by the wrapper.
- `WORKFLOW_EXECUTOR_CWD`: working directory for Codex execution.
- `WORKFLOW_CODEX_SANDBOX`: Codex sandbox setting passed to the executor wrapper.
- `WORKFLOW_CODEX_INHERIT_ENV`: whether Codex inherits environment variables.
- `WORKFLOW_CODEX_BYPASS_APPROVALS`: whether Codex bypasses approvals.
- `WORKFLOW_MAX_AUTO_REPLANS_PER_STEP`: default automatic replans per rejected step.

## Bootstrap And Preflight Hooks

The wrapper can run host-side setup before the orchestrator starts:

- `WORKFLOW_BOOTSTRAP_SCRIPT`: executable script to run first.
- `WORKFLOW_PREFLIGHT_SCRIPT`: executable script to run before loop work.
- `WORKFLOW_SKIP_RENDER_PREFLIGHT=1`: skip preflight entirely.

Legacy shell-string hooks also exist:

- `WORKFLOW_BOOTSTRAP_CMD`
- `WORKFLOW_PREFLIGHT_CMD`

Prefer script hooks for repeatable usage.

Recommended pattern:

- Use `WORKFLOW_BOOTSTRAP_SCRIPT` for actions that can repair missing
  prerequisites.
- Keep bootstrap actions idempotent and resumable so repeated workflow runs are
  cheap.
- Keep task-specific bootstrap logic outside the `workflow/` submodule, for
  example in repository-level scripts invoked through environment variables.
- Stage large shared assets into stable paths or caches outside per-run
  workspaces when possible.
- Reserve `WORKFLOW_PREFLIGHT_SCRIPT` for quick checks; it should fail fast, not
  perform long downloads.

Example:

```bash
export WORKFLOW_BOOTSTRAP_SCRIPT="$PWD/scripts/prepare_prereqs.sh"
export WORKFLOW_PREFLIGHT_SCRIPT="$PWD/scripts/preflight_check.sh"
bash workflow/scripts/run_workflow.sh \
  --workspace workflow_runs/my-task \
  loop
```

## Model And Executor Wrappers

- `run_gemini_noninteractive.sh`: Gemini planner and reviewer calls.
- `run_gemini_discussion.sh`: Gemini interactive kickoff discussion.
- `run_claude_noninteractive.sh`: Claude planner and reviewer calls.
- `run_claude_discussion.sh`: Claude interactive kickoff discussion.
- `run_codex_executor.sh`: Codex executor calls.
- `evaluate_lesson.sh`: lesson candidate review helper.

The Gemini and Claude wrappers read prompts from files so large prompts do not
overflow shell argument limits.
