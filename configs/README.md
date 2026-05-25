# Workflow Configs

Configs define which model wrapper the planner, reviewer, and discussion phases
use. They share the same Python orchestrator and Codex executor.

## Available Configs

- `config.gemini.example.yaml`: Gemini planner, reviewer, and discussion.
- `config.claude.example.yaml`: Claude planner, reviewer, and discussion.

The only meaningful difference is which shell wrappers are called:

- Gemini uses `workflow/scripts/run_gemini_noninteractive.sh` and
  `workflow/scripts/run_gemini_discussion.sh`.
- Claude uses `workflow/scripts/run_claude_noninteractive.sh` and
  `workflow/scripts/run_claude_discussion.sh`.
- Both configs use `workflow/scripts/run_codex_executor.sh` for execution.

## Model Selection

`init` can persist model choices into `workflow_runs/<name>/runtime.env`.

- `--model`: sets planner, reviewer, and discussion together.
- `--planner-model`: overrides only the planner.
- `--reviewer-model`: overrides only the reviewer.
- `--discussion-model`: overrides only the kickoff discussion.

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

Later `plan`, `review`, and `loop` runs in the same workspace reuse these
runtime values automatically.

## Wrapper Auto-Detection

`workflow/scripts/run_workflow.sh` chooses a default config when `--config` is
not provided:

1. It checks the workspace `runtime.env`.
2. It checks model-related environment variables.
3. It falls back to `config.gemini.example.yaml`.

Pass `--config workflow/configs/config.claude.example.yaml` or
`--config workflow/configs/config.gemini.example.yaml` when you need to override
that detection.
