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

- `workflow/config.gemini.example.yaml`: Gemini planner, reviewer, and discussion.
- `workflow/config.claude.example.yaml`: Claude planner, reviewer, and discussion.

Both configs use the same orchestrator. The only difference is which wrapper scripts they call.

## Quick Start

Gemini:

```bash
python workflow/orchestrator.py \
  --workspace workflow_runs/my-task \
  --config workflow/config.gemini.example.yaml \
  init \
  --task-summary "Build feature X" \
  --model gemini-2.5-pro
```

Claude:

```bash
python workflow/orchestrator.py \
  --workspace workflow_runs/my-task \
  --config workflow/config.claude.example.yaml \
  init \
  --task-summary "Build feature X" \
  --model sonnet
```

Then run:

```bash
python workflow/orchestrator.py \
  --workspace workflow_runs/my-task \
  --config workflow/config.gemini.example.yaml \
  loop
```

If that workspace uses Claude, swap the config path to `workflow/config.claude.example.yaml`.

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
  --config workflow/config.claude.example.yaml \
  init \
  --task-summary "Build feature X" \
  --planner-model sonnet \
  --reviewer-model opus \
  --discussion-model sonnet
```

Those values are reused automatically by later `plan`, `review`, and `loop` runs in the same workspace.

## Common Commands

```bash
python workflow/orchestrator.py --workspace workflow_runs/my-task --config workflow/config.gemini.example.yaml plan
python workflow/orchestrator.py --workspace workflow_runs/my-task --config workflow/config.gemini.example.yaml run-step
python workflow/orchestrator.py --workspace workflow_runs/my-task --config workflow/config.gemini.example.yaml review
python workflow/orchestrator.py --workspace workflow_runs/my-task --config workflow/config.gemini.example.yaml status
```

## Workspace Files

- `task.md`: task brief
- `discussion.md`: kickoff discussion summary
- `plan.md`: generated workflow plan plus compact manifest-backed overview
- `results.md`: append-only execution and review log
- `progress.md`: deterministic resume checkpoint
- `artifacts/`: raw logs and failure artifacts
- `runtime.env`: per-workspace model overrides

## Wrappers

- `workflow/run_gemini_noninteractive.sh`
- `workflow/run_gemini_discussion.sh`
- `workflow/run_claude_noninteractive.sh`
- `workflow/run_claude_discussion.sh`
- `workflow/run_codex_executor.sh`

The Gemini and Claude wrappers read prompts from files so large prompts do not overflow shell argument limits.

## Launcher Default

`workflow/run_workflow.sh` now defaults to:

```bash
python workflow/orchestrator.py --workspace workflow_runs/default --config workflow/config.gemini.example.yaml loop
```
