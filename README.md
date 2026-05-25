# Planner, Codex Execute Workflow

This workflow runs a planner / executor / reviewer loop:

1. A planner writes or refreshes `plan.md`.
2. Codex executes one planned step.
3. A reviewer approves the step or asks for changes.
4. The orchestrator writes compact resume state.
5. The loop continues until the workflow is done, blocked, failed, or interrupted.

The runner keeps the durable workflow state in a workspace directory such as
`workflow_runs/my-task`.

## Daily Commands

Run these commands from the repository root that contains the `workflow/`
directory.

### 1. Start a workflow with a kickoff discussion

Use `init` to create the workspace files and launch the interactive discussion.
The discussion summary is saved to `discussion.md`; raw and normalized logs are
saved under `artifacts/`.

Gemini:

```bash
python workflow/orchestrator.py \
  --workspace workflow_runs/my-task \
  --config workflow/configs/config.gemini.example.yaml \
  init \
  --task-summary "Build feature X" \
  --model gemini-3.1-pro-preview
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

During `init`, the workflow asks for related links to store in `task.md`. Enter
one link per line, press Enter on an empty line when finished, or type `none` to
skip. For scripted use, pass `--related-link` multiple times.

### 2. Continue from an existing workflow run

Use `init --migrate-from-workspace` when you want a fresh workspace that imports
the durable context from an older run, then starts a new kickoff discussion from
that imported handoff.

```bash
python workflow/orchestrator.py \
  --workspace workflow_runs/my-task-rerun \
  --config workflow/configs/config.gemini.example.yaml \
  init \
  --migrate-from-workspace workflow_runs/my-task
```

This writes `migration.md` in the destination workspace, imports durable task and
discussion context, resets workflow status to planning, and copies `runtime.env`
by default so model choices follow the rerun. Use
`--skip-migrate-runtime-env` to opt out.

### 3. Run the execute / review loop

Use the wrapper for loop runs:

```bash
bash workflow/scripts/run_workflow.sh \
  --workspace workflow_runs/my-task \
  loop
```

By default, loop runs start in the background and write:

- `workflow_runs/my-task/workflow.output.log`
- `workflow_runs/my-task/workflow.pid`

The wrapper auto-selects the Gemini or Claude config from `runtime.env` or model
environment variables. Pass `--config` only when you need to force a specific
config.

Useful loop variants:

```bash
# Stop after one approved step.
bash workflow/scripts/run_workflow.sh \
  --workspace workflow_runs/my-task \
  loop --max-steps 1

# Run in the foreground.
WORKFLOW_DETACH=0 bash workflow/scripts/run_workflow.sh \
  --workspace workflow_runs/my-task \
  loop
```

## Workspace Files

- `task.md`: task brief and related links.
- `discussion.md`: kickoff discussion summary.
- `migration.md`: optional handoff imported from a previous workflow workspace.
- `plan.md`: generated workflow plan with compact manifest-backed state.
- `results.md`: append-only execution and review journal.
- `progress.md`: deterministic resume checkpoint.
- `summary.md`: terminal workflow summary.
- `artifact_index.json`: structured index of recent workflow artifacts.
- `artifacts/`: raw logs, command output, and failure artifacts.
- `runtime.env`: per-workspace model overrides.

## Other Commands

The orchestrator also exposes lower-level commands for manual control:

```bash
python workflow/orchestrator.py --workspace workflow_runs/my-task --config workflow/configs/config.gemini.example.yaml plan
python workflow/orchestrator.py --workspace workflow_runs/my-task --config workflow/configs/config.gemini.example.yaml run-step
python workflow/orchestrator.py --workspace workflow_runs/my-task --config workflow/configs/config.gemini.example.yaml review
python workflow/orchestrator.py --workspace workflow_runs/my-task --config workflow/configs/config.gemini.example.yaml status
```

Use `migrate` directly only when you want to import an existing workflow without
launching a kickoff discussion:

```bash
python workflow/orchestrator.py \
  --workspace workflow_runs/my-task-rerun \
  --config workflow/configs/config.gemini.example.yaml \
  migrate \
  --from-workspace workflow_runs/my-task
```

## More Detail

- `workflow/configs/README.md`: provider configs and model selection.
- `workflow/scripts/README.md`: wrapper scripts, loop behavior, and environment hooks.
- `workflow/memory/README.md`: workflow-level lessons and lesson review process.
- `workflow/utils/README.md`: workspace state, migration behavior, verification contract, and evaluation gates.
