# Workflow State And Contracts

The modules in this folder implement shared workflow mechanics: state files,
manifest rendering, migration, discussion cleanup, and common helpers.

## File Roles

- `plan.md`: operational plan. Completed steps are summarized; the current and
  upcoming steps stay detailed.
- `progress.md`: compact resume note. It should contain only status, blockers,
  decisive evidence, and the next action.
- `summary.md`: terminal handoff file. It records workflow execution status
  (`done`, `blocked`, `failed`, or `interrupted`) and objective outcome (`pass`,
  `fail`, `inconclusive`, or `pending`).
- `results.md`: append-only execution and review journal.
- `artifacts/`: bulky command output and other raw evidence that should not live
  in workflow state files.

The runner enforces this structure so `plan.md` and `progress.md` do not grow
without bound.

## Migration Behavior

Use migration when an existing workflow run should hand off into a fresh
workspace instead of resuming in place. This is useful after human intervention,
repaired prerequisites, or when you want the planner to re-scope unfinished work
without carrying over terminal execution state.

`init --migrate-from-workspace` imports the prior workflow first, writes
`migration.md`, and starts the kickoff discussion from the imported context.

The lower-level `migrate` command:

- Creates a new destination workspace and refuses to overwrite existing workflow
  state.
- With `--in-place`, refreshes workflow state inside the same workspace. Existing
  workflow-state files are snapshotted first, and run-local payload stays in
  place.
- Copies durable task and discussion context from the source workspace.
- Records `--task-summary` as the new destination objective when provided.
- Writes `migration.md` with completed work, latest review, open issues,
  unfinished step, and next action from the source workflow.
- Resets destination status to fresh planning so `plan` or `loop` can continue
  from the imported handoff.
- Copies `runtime.env` by default. Pass `--skip-runtime-env` to opt out.

In-place example:

```bash
python workflow/orchestrator.py \
  --workspace workflow_runs/my-task \
  --config workflow/configs/config.gemini.example.yaml \
  migrate \
  --from-workspace workflow_runs/my-task \
  --in-place
```

## Step Verification Contract

After an executor run, the orchestrator checks the newest `results.md` section
for the current step before allowing review. The section must be headed:

```markdown
## Step <id> - <title>
```

It must include these subsections:

- `### Acceptance Evidence`: every acceptance criterion mapped to `pass`,
  `fail`, or `inconclusive` with concrete evidence. Use criterion ids from the
  executor prompt (`AC1`, `AC2`, ...).
- `### Verification Evidence`: every verification requirement mapped to the
  command or check performed, working directory, exit or return code when
  command-based, artifact path when available, and result. Use verification ids
  from the executor prompt (`V1`, `V2`, ...).
- `### Changed Files`: every changed file and why it changed, or an explicit
  statement that no files changed.
- `### Outcome`: `pass`, `fail`, or `inconclusive`, plus remaining risks.

If evidence is missing, vague, still running, deferred, or lacks command exit
codes for command-based checks, the step remains `needs_changes`. Evidence may
state that a check was gated off, blocked, or not applicable only when it records
a terminal `fail` or `inconclusive` decision with concrete artifact or command
evidence for that gate.

The workflow writes an evidence contract report under
`artifacts/command_failures/` so the same step can be resumed with the missing
proof.

When a step uses an explicit checkpoint, dataset, log, or other artifact path,
the executor must validate that exact path and must not silently fall back to an
older default artifact. Review should reject evidence that relies on an explicit
artifact path without proving which artifact was used.

## Outcome Follow-Ups

Reviews can approve a step while still marking its outcome as `fail` or
`inconclusive`. This is useful for benchmarks and evaluations where the run
completed, but the measured result is still unacceptable or ambiguous.

When a step is approved with `outcome_status=fail`, the workflow automatically
inserts a follow-up investigation step if one does not already exist. Failed
benchmark results stay visible as explicit workflow work instead of living only
in `results.md`.

Automatically generated follow-up steps do not recursively create more
follow-ups when they are approved with `outcome_status=fail`. If the
investigation documents that no workflow-side remediation remains, the workflow
can finish as `done` while the objective outcome remains `fail`.

Failed gate steps can also block downstream work. Set
`blocks_downstream_on_fail: true` on an explicit gate step, or rely on the
built-in smoke-evaluation heuristic for steps whose id, title, or objective
contain smoke plus evaluation, benchmark, or test language. When such a step is
approved with `outcome_status=fail`, pending downstream evaluation or benchmark
steps are marked `blocked` with the failure reason. Automatically generated
follow-up investigation steps remain runnable even if their objective mentions
evaluation artifacts.

Workflow completion and objective achievement are tracked separately:

- Workflow execution status answers whether the workflow has remaining
  actionable steps.
- Objective outcome answers whether the user-facing goal was achieved.
- A workflow can finish execution as `done` while still reporting objective
  outcome `fail` or `inconclusive` if approved steps documented unresolved
  benchmark or quality failures.

## Evaluation Readiness Gates

Expensive evaluations should be protected by a cheap gate whenever possible. For
policy-learning tasks, this usually means a replay audit, one-episode
validation, or 1-5 episode smoke test before a full benchmark. Add
`blocks_downstream_on_fail: true` to any manifest step whose failed measured
outcome should prevent later evaluation or benchmark steps from running.

Before launching a full policy evaluation, the plan should include evidence for
the relevant items below:

- Explicit checkpoint, dataset, zarr, config, and log paths exist and are the
  exact artifacts used by the command.
- Expert action replay succeeds from stored initial states in the same
  evaluation environment.
- Restored observations, point clouds, `agent_pos`, camera or extrinsic
  handling, and action dimensions match the processed training samples.
- Teacher-forced policy predictions on validation or demo observations have low
  per-action-dimension error, with gripper, base, and control-mode groups
  inspected separately.
- A smallest-useful closed-loop validation has nonzero success or otherwise
  shows a new behavior worth evaluating at larger scale.
- If a smoke or replay gate fails, replan a remediation or diagnostic before
  running the full benchmark.
