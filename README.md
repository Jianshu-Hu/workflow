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

Example:

```bash
python workflow/orchestrator.py \
  --workspace workflow_runs/my-task-rerun \
  --config workflow/configs/config.gemini.example.yaml \
  migrate \
  --from-workspace workflow_runs/my-task
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

- `WORKFLOW_BOOTSTRAP_CMD`: runs before the orchestrator starts. Use this for idempotent setup such as activating caches, downloading known public prerequisites, syncing asset mirrors, or materializing generated config files.
- `WORKFLOW_PREFLIGHT_CMD`: runs after the launcher prints host details but before the workflow loop begins. Use this for fast host validation such as checking GPU visibility, mounted paths, required binaries, or required files/directories.

Recommended pattern:

- Use `WORKFLOW_BOOTSTRAP_CMD` for actions that can repair missing prerequisites.
- Keep those actions idempotent and resumable so repeated workflow runs are cheap.
- Keep task-specific bootstrap logic outside the `workflow/` submodule, for example in repository-level scripts that the workflow invokes via environment variables.
- Stage large shared assets into stable paths or caches outside per-run workspaces when possible.
- Reserve `WORKFLOW_PREFLIGHT_CMD` for quick checks; it should fail fast, not perform long downloads.

Example:

```bash
export WORKFLOW_BOOTSTRAP_CMD='bash scripts/prepare_prereqs.sh'
export WORKFLOW_PREFLIGHT_CMD='test -f /path/to/required/file && test -d /path/to/required/dir'
python workflow/orchestrator.py --workspace workflow_runs/my-task --config workflow/configs/config.gemini.example.yaml loop
```

## Launcher Default

`workflow/scripts/run_workflow.sh` now defaults to:

```bash
python workflow/orchestrator.py --workspace workflow_runs/default --config workflow/configs/config.gemini.example.yaml loop
```
