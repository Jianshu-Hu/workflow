# Planner, Codex Execute Workflow

This workflow runs a simple loop:

1. A planner writes `plan.md`.
2. Codex executes one step.
3. A reviewer approves or rejects it.
4. The orchestrator writes a compact `progress.md`.
5. The loop continues until done.

## File Roles

- `plan.md`: operational plan. Completed steps are summarized; the current and upcoming steps stay detailed.
- `progress.md`: compact resume note. It should contain only status, blockers, decisive evidence, and the next action.
- `summary.md`: terminal handoff file. It records both the workflow execution status (`done`, `blocked`, `failed`, or `interrupted`) and the separate objective outcome (`pass`, `fail`, `inconclusive`, or `pending`).
- `results.md`: append-only execution and review journal.
- `artifacts/`: bulky command output and other raw evidence that should not live in workflow state files.

The workflow runner now enforces this structure so `plan.md` and `progress.md` do not grow without bound.

## Configs

- `workflow/configs/config.gemini.example.yaml`: Gemini planner, reviewer, and discussion.
- `workflow/configs/config.claude.example.yaml`: Claude planner, reviewer, and discussion.

Both configs use the same orchestrator. The only difference is which wrapper scripts they call.

## Quick Start

Gemini:

```bash
python workflow/orchestrator.py \
  --workspace workflow_runs/my-task \
  --config workflow/configs/config.gemini.example.yaml \
  init \
  --task-summary "Build feature X" \
  --model gemini-3.1-pro-preview
```

Gemini with imported workflow context:

```bash
python workflow/orchestrator.py \
  --workspace workflow_runs/my-task-rerun \
  --config workflow/configs/config.gemini.example.yaml \
  init \
  --migrate-from-workspace workflow_runs/my-task
```

Claude:

```bash
python workflow/orchestrator.py \
  --workspace workflow_runs/my-task \
  --config workflow/configs/config.claude.example.yaml \
  init \
  --task-summary "Build feature X" \
  --model sonnet
```

During `init`, the workflow asks for related links to save in `task.md`.
Enter one link per line, press Enter on an empty line when finished, or type `none` to skip.
For scripted use, pass `--related-link` multiple times instead of using the prompt.
The kickoff discussion saves raw `artifacts/discussion_input.log` and `artifacts/discussion_output.log`, then generates a normalized `artifacts/discussion_transcript.txt` with extracted user/assistant turns. The transcript cleanup filters terminal status lines, approval prompts, and tool noise before summarization. `discussion.md` is regenerated from that normalized transcript after the interactive session ends, and malformed summary output is rejected instead of overwriting the existing file.
If `init` is passed `--migrate-from-workspace`, it imports the prior workflow into the new workspace first, writes `migration.md`, and the kickoff discussion starts from that imported context instead of a blank kickoff.

Then run:

```bash
python workflow/orchestrator.py \
  --workspace workflow_runs/my-task \
  --config workflow/configs/config.gemini.example.yaml \
  loop
```

If that workspace uses Claude, swap the config path to `workflow/configs/config.claude.example.yaml`.

## Model Selection

`init` can persist model choices into `workflow_runs/<name>/runtime.env`.

- `--model`: sets planner, reviewer, and discussion together
- `--planner-model`: overrides only the planner
- `--reviewer-model`: overrides only the reviewer
- `--discussion-model`: overrides only the kickoff discussion

Example:

```bash
python workflow/orchestrator.py \
  --workspace workflow_runs/my-task \
  --config workflow/configs/config.claude.example.yaml \
  init \
  --task-summary "Build feature X" \
  --related-link https://github.com/example/project \
  --related-link https://arxiv.org/abs/1234.5678 \
  --planner-model sonnet \
  --reviewer-model opus \
  --discussion-model sonnet
```

Those values are reused automatically by later `plan`, `review`, and `loop` runs in the same workspace.

## Common Commands

```bash
python workflow/orchestrator.py --workspace workflow_runs/my-task-rerun --config workflow/configs/config.gemini.example.yaml migrate --from-workspace workflow_runs/my-task
python workflow/orchestrator.py --workspace workflow_runs/my-task --config workflow/configs/config.gemini.example.yaml plan
python workflow/orchestrator.py --workspace workflow_runs/my-task --config workflow/configs/config.gemini.example.yaml run-step
python workflow/orchestrator.py --workspace workflow_runs/my-task --config workflow/configs/config.gemini.example.yaml review
python workflow/orchestrator.py --workspace workflow_runs/my-task --config workflow/configs/config.gemini.example.yaml status
```

## Migration

Use `migrate` when an existing workflow run should hand off into a fresh workspace instead of resuming in place.
This is useful after human intervention, repaired prerequisites, or when you want the planner to re-scope the unfinished work without carrying over terminal execution state.
If you want to refresh workflow state inside the same workspace, use `migrate --in-place` with `--workspace` and `--from-workspace` pointing at that same workspace.

Example:

```bash
python workflow/orchestrator.py \
  --workspace workflow_runs/my-task-rerun \
  --config workflow/configs/config.gemini.example.yaml \
  migrate \
  --from-workspace workflow_runs/my-task
```

In-place example:

```bash
python workflow/orchestrator.py \
  --workspace workflow_runs/my-task \
  --config workflow/configs/config.gemini.example.yaml \
  migrate \
  --from-workspace workflow_runs/my-task \
  --in-place
```

If you want the migration and the kickoff discussion in one command, prefer:

```bash
python workflow/orchestrator.py \
  --workspace workflow_runs/my-task-rerun \
  --config workflow/configs/config.gemini.example.yaml \
  init \
  --migrate-from-workspace workflow_runs/my-task
```

What `migrate` does:

- Creates a new destination workspace and refuses to overwrite an existing workflow state.
- With `--in-place`, refreshes workflow state inside the same workspace instead of creating a new one. Existing workflow-state files are snapshotted first, and run-local payload stays in place.
- Copies the durable task and discussion context from the source workspace.
- Writes `migration.md` in the destination with a summarized handoff: completed work, latest review, open issues, unfinished step, and the next action from the source workflow.
- Resets the destination workspace to fresh `planning` status so the next `plan` or `loop` run can continue from the imported handoff instead of pretending work already ran there.
- Copies `runtime.env` from the source workspace by default so planner/reviewer model choices follow the migration. Pass `--skip-runtime-env` to opt out.

## Workspace Files

- `task.md`: task brief
- `discussion.md`: kickoff discussion summary
- `migration.md`: optional handoff summary imported from a previous workflow workspace
- `plan.md`: generated workflow plan plus compact manifest-backed overview
- `results.md`: append-only execution and review log
- `progress.md`: deterministic resume checkpoint
- `summary.md`: terminal workflow summary covering final execution status, objective outcome, achieved work, implemented changes, remaining issues, and next steps
- `artifact_index.json`: structured index of recent workflow artifacts such as results sections, discussion logs, and command failure outputs
- `artifacts/`: raw logs and failure artifacts
- `runtime.env`: per-workspace model overrides

## Wrappers

- `workflow/scripts/run_gemini_noninteractive.sh`
- `workflow/scripts/run_gemini_discussion.sh`
- `workflow/scripts/run_claude_noninteractive.sh`
- `workflow/scripts/run_claude_discussion.sh`
- `workflow/scripts/run_codex_executor.sh`

The Gemini and Claude wrappers read prompts from files so large prompts do not overflow shell argument limits.

## Bootstrap And Preflight Hooks

`workflow/scripts/run_workflow.sh` supports two useful environment hooks:

- `WORKFLOW_BOOTSTRAP_SCRIPT`: preferred. Runs before the orchestrator starts. Point this at an executable script for idempotent setup such as activating caches, downloading known public prerequisites, syncing asset mirrors, or materializing generated config files.
- `WORKFLOW_PREFLIGHT_SCRIPT`: preferred. Runs after the launcher prints host details but before the workflow loop begins. Point this at an executable script for fast host validation such as checking GPU visibility, mounted paths, required binaries, or required files/directories.
- `WORKFLOW_BOOTSTRAP_CMD`: legacy shell-string fallback for bootstrap when a dedicated script is not yet available.
- `WORKFLOW_PREFLIGHT_CMD`: legacy shell-string fallback for preflight when a dedicated script is not yet available.

Recommended pattern:

- Use `WORKFLOW_BOOTSTRAP_SCRIPT` for actions that can repair missing prerequisites.
- Keep those actions idempotent and resumable so repeated workflow runs are cheap.
- Keep task-specific bootstrap logic outside the `workflow/` submodule, for example in repository-level scripts that the workflow invokes via environment variables.
- Stage large shared assets into stable paths or caches outside per-run workspaces when possible.
- Reserve `WORKFLOW_PREFLIGHT_SCRIPT` for quick checks; it should fail fast, not perform long downloads.
- Prefer script hooks over shell-string hooks so quoting is predictable and workflow setup remains easy to audit.

Example:

```bash
export WORKFLOW_BOOTSTRAP_SCRIPT="$PWD/scripts/prepare_prereqs.sh"
export WORKFLOW_PREFLIGHT_SCRIPT="$PWD/scripts/preflight_check.sh"
python workflow/orchestrator.py --workspace workflow_runs/my-task --config workflow/configs/config.gemini.example.yaml loop
```

## Launcher Default

`workflow/scripts/run_workflow.sh` now defaults to:

```bash
python workflow/orchestrator.py --workspace workflow_runs/default --config workflow/configs/config.gemini.example.yaml loop
```

## Outcome Follow-Ups

Reviews can now approve a step while still marking its outcome as `fail` or `inconclusive`.
This is useful for benchmarks and evaluations where the run completed, but the measured result is still unacceptable or ambiguous.

When a step is approved with `outcome_status=fail`, the workflow automatically inserts a follow-up investigation step if one does not already exist.
That keeps failed benchmark results visible as explicit workflow work instead of burying them only in `results.md`.

Workflow completion and objective achievement are tracked separately:

- Workflow execution status answers whether the workflow has any remaining actionable steps.
- Objective outcome answers whether the user-facing goal was actually achieved.
- A workflow can finish execution as `done` while still reporting objective outcome `fail` or `inconclusive` if approved steps documented unresolved benchmark or quality failures.
