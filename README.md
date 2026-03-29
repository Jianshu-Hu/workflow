# Gemini Plan, Codex Execute Workflow

This workflow adds a small orchestrator for the loop you described:

This directory is the canonical home for the workflow documentation that was previously linked from the repository root README.

1. Gemini writes a structured `plan.md` with explicit verification criteria for each step.
2. Codex implements one step at a time and appends progress to `results.md`.
3. Gemini reviews the latest result and either approves the step or requests changes.
4. After each review, Gemini rewrites `progress.md` with a resume-oriented checkpoint.
5. `init` can launch an interactive Gemini kickoff discussion and keep `discussion.md` as the durable summary.
6. The loop repeats until all steps are approved.

## Files

- `workflow/orchestrator.py`: CLI runner for the workflow.
- `workflow/config.example.yaml`: command-template config for your local Gemini and Codex CLIs.
- `workflow_runs/<name>/task.md`: task brief and acceptance criteria.
- `workflow_runs/<name>/discussion.md`: planning discussion notes you want Gemini to use.
- `workflow_runs/<name>/plan.md`: Gemini-authored plan with a machine-readable manifest block.
- `workflow_runs/<name>/results.md`: implementation and review log.
- `workflow_runs/<name>/progress.md`: Gemini-authored checkpoint summary used to resume later runs.
- `workflow_runs/<name>/state.json`: timestamps for planner / executor / reviewer runs.

## Why The Manifest Exists

`plan.md` includes a YAML manifest wrapped in marker comments. Gemini is asked to keep that block intact while updating the plan. The runner parses that block to know:

- which step is active
- what the verification requirements are
- whether a step is pending, under review, approved, or needs changes

That gives you a human-readable `plan.md` without losing reliable automation.

## Setup

Adjust `workflow/config.example.yaml` to match the actual CLI syntax on your machine.

Example:

```yaml
workflow:
  max_auto_replans_per_step: 3

planner:
  model: gemini-pro
  command_template: bash {repo_root}/workflow/run_gemini_noninteractive.sh {prompt_file}

discussion:
  command_template: bash workflow/run_gemini_discussion.sh {prompt_file}

executor:
  command_template: bash workflow/run_codex_executor.sh {prompt_file}
```

You can also override the command templates with environment variables:

```bash
export WORKFLOW_GEMINI_CMD='bash {repo_root}/workflow/run_gemini_noninteractive.sh {prompt_file}'
export WORKFLOW_GEMINI_DISCUSSION_CMD='bash workflow/run_gemini_discussion.sh {prompt_file}'
export WORKFLOW_CODEX_CMD='bash workflow/run_codex_executor.sh {prompt_file}'
```

The bundled `workflow/run_codex_executor.sh` wrapper launches `codex exec` from the repository root, inherits the parent shell environment, and can optionally bypass Codex approvals when the outer environment is already controlled, such as a Slurm batch job. It reads these environment variables:

- `WORKFLOW_EXECUTOR_CWD`: repository working root passed to `codex exec -C`
- `WORKFLOW_CODEX_SANDBOX`: sandbox mode used when approvals are not bypassed
- `WORKFLOW_CODEX_INHERIT_ENV`: when `1`, passes `-c shell_environment_policy.inherit=all`
- `WORKFLOW_CODEX_BYPASS_APPROVALS`: when `1`, uses `--dangerously-bypass-approvals-and-sandbox`
- `WORKFLOW_MAX_AUTO_REPLANS_PER_STEP`: auto-replan limit before the loop gives up

The bundled `workflow/run_gemini_noninteractive.sh` wrapper feeds the planner / reviewer prompt to Gemini on stdin instead of expanding the full prompt into a shell argument. This avoids `Argument list too long` failures once `plan.md`, `results.md`, and `progress.md` get large. It reads:

- `WORKFLOW_GEMINI_MODEL`: Gemini model passed as `gemini -m ...`

The bundled `workflow/run_workflow.sh` launcher is a generic workflow entrypoint for repositories that mount this workflow under `workflow/`. By default it detects the host repo root from the script location and runs:

```bash
python workflow/orchestrator.py --workspace workflow_runs/default --config workflow/config.example.yaml loop
```

You can override its behavior with environment variables:

- `WORKFLOW_REPO_ROOT`: host repository root if auto-detection is not appropriate
- `WORKFLOW_PYTHON`: Python executable to use
- `WORKFLOW_CONFIG`: workflow config file passed to `--config`
- `WORKFLOW_WORKSPACE`: default workspace used when no explicit CLI args are provided
- `WORKFLOW_BOOTSTRAP_CMD`: shell snippet evaluated before running the orchestrator, useful for environment activation
- `WORKFLOW_PREFLIGHT_CMD`: optional shell command to run before the workflow starts

If you pass arguments to `workflow/run_workflow.sh`, they are forwarded directly to `workflow/orchestrator.py`. That lets host repos keep a thin local wrapper while reusing the shared launcher.

## Usage

Normal flow for most tasks:

1. Initialize a workspace once.
2. Let `init` launch the interactive Gemini kickoff discussion, or pass `--no-discussion` if you only want scaffolding.
3. Use that chat to refine the research problem and keep `discussion.md` current.
4. Run `loop` and let the orchestrator handle planning, execution, review, progress checkpointing, and automatic replanning until the workflow finishes or a blocker truly requires human intervention.

Initialize a workspace:

```bash
python workflow/orchestrator.py --workspace workflow_runs/my-task init \
  --task-summary "Build feature X with Gemini planning and Codex execution"
```

When `init` is run from an interactive terminal, it now launches a Gemini chat session in the same terminal after scaffolding the workspace.
That kickoff prompt tells Gemini to discuss the problem with you and keep `workflow_runs/my-task/discussion.md` updated as the durable summary for later planner runs.

Write any extra task detail into `workflow_runs/my-task/task.md` if needed.
If you want scaffolding only, skip the chat launch:

```bash
python workflow/orchestrator.py --workspace workflow_runs/my-task init \
  --task-summary "Build feature X with Gemini planning and Codex execution" \
  --no-discussion
```

For the normal workflow, you can now start the whole process with a single command:

```bash
python workflow/orchestrator.py --workspace workflow_runs/my-task loop
```

If `plan.md` does not contain any steps yet, `loop` will call Gemini to generate the plan first, then continue with Codex execution and Gemini review automatically.
If the workspace already has a `progress.md` from an earlier run, Gemini is prompted to inspect it so the workflow can continue from the previously recorded state instead of starting over blindly.
If review rejects a step but does not mark it as requiring human intervention, the runner automatically replans and retries instead of stopping immediately.

The commands below are still available when you want manual control over a specific stage.

Generate the plan:

```bash
python workflow/orchestrator.py --workspace workflow_runs/my-task plan
```

Run the current step with Codex:

```bash
python workflow/orchestrator.py --workspace workflow_runs/my-task run-step
```

Ask Gemini to review the current step:

```bash
python workflow/orchestrator.py --workspace workflow_runs/my-task review
```

Run the whole loop until the workflow finishes or encounters a blocker that truly needs human intervention:

```bash
python workflow/orchestrator.py --workspace workflow_runs/my-task loop
```

Check status at any time:

```bash
python workflow/orchestrator.py --workspace workflow_runs/my-task status
```

To resume a prior run, use the same workspace and run:

```bash
python workflow/orchestrator.py --workspace workflow_runs/my-task loop
```

The planner, executor, and reviewer prompts all include `progress.md`, so reruns can pick up the latest checkpointed context.

## Expected Plan Shape

Gemini is instructed to preserve this machine-readable block inside `plan.md`:

````markdown
<!-- WORKFLOW_MANIFEST_START -->
```yaml
task: Build feature X
status: pending
current_step: step-1
steps:
  - id: step-1
    title: Add workflow scaffolding
    status: pending
    objective: Create the basic runner, docs, and state handling.
    implementation:
      - Add the workflow runner module.
      - Add configuration and documentation.
    verification:
      - python -m unittest discover -s tests -p 'test_workflow_orchestrator.py'
  - id: step-2
    title: Integrate executor prompts
    status: pending
    objective: Ensure Codex only works on one step at a time.
    implementation:
      - Create focused step prompts.
    verification:
      - Run one step end-to-end and inspect results.md
history: []
updated_at: 2026-03-24T00:00:00+00:00
```
<!-- WORKFLOW_MANIFEST_END -->
````

The rest of the file can be normal Markdown with expanded explanations.

## Review Behavior

Gemini review returns JSON only:

```json
{
  "approved": true,
  "summary": "Step is complete and verified.",
  "required_changes": [],
  "human_intervention_required": false,
  "human_intervention_reason": ""
}
```

If review approves the step, the runner marks it `approved` and advances to the next `pending` step.
If review rejects the step but the blocker is still repository-fixable, the runner marks it `needs_changes`, replans, and retries.
If review rejects the step and explicitly marks the blocker as requiring human intervention, the runner stops.

After every review, the runner asks Gemini to rewrite `progress.md` so the next workflow invocation can inspect the latest approved work, blockers, and next-step guidance.

## Notes

- The runner does not assume exact Gemini or Codex CLI syntax. Use command templates.
- The default Gemini wrapper uses stdin for non-interactive prompts so large workflow prompts do not overflow the shell argument limit.
- The bundled executor wrapper runs `codex exec` from the repository root and passes the prompt file through stdin because the current `codex exec` CLI reads the prompt from stdin or a positional argument, not `-f`.
- The shared workflow repo does not need to ship renderer or simulator preflight scripts. Use a host-local wrapper or set `WORKFLOW_PREFLIGHT_CMD` if your environment needs a pre-run check.
- Host repos that need environment activation can keep that logic in a thin wrapper, or set `WORKFLOW_BOOTSTRAP_CMD` for the shared `workflow/run_workflow.sh` launcher.
- Codex is instructed to update `results.md`, but the runner also appends Gemini review records there.
- Gemini also rewrites `progress.md` after each review. That file is intended to be the human-readable resume checkpoint for future runs.
- `loop` is conservative about external blockers, not local ones: it now auto-replans rejected steps up to the configured limit and stops only for explicit human-intervention blockers or after the retry budget is exhausted.
