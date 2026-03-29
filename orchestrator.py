from __future__ import annotations

import argparse
import dataclasses
import json
import os
import re
import shlex
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml


MANIFEST_START = "<!-- WORKFLOW_MANIFEST_START -->"
MANIFEST_END = "<!-- WORKFLOW_MANIFEST_END -->"

RESULTS_HEADER = """# Workflow Results

This file is appended by Codex and the workflow runner after each step attempt.
Keep older entries for history.
"""

PROGRESS_TEMPLATE = """# Workflow Progress

This file is rewritten by Gemini after each review.
It should summarize the current state so a later workflow run can resume from here.

## Current Status

- No reviews yet.

## Completed Steps

- None yet.

## Latest Review

- No Gemini review has been recorded yet.

## Open Issues

- None recorded.

## Next Step

- Generate a plan and begin the first pending step.

## Resume Instructions

- Read this file together with `plan.md` and `results.md` before continuing.
"""


PLAN_TEMPLATE = """# Workflow Plan

{manifest_block}

## Planner Notes

Gemini should rewrite this file while preserving the manifest block markers above.
Each step should explain what to build and how success will be verified.
"""


def render_task_template(task_summary: str = "") -> str:
    summary = task_summary.strip()
    if not summary:
        return "# Task\n\nDescribe the goal, constraints, and acceptance criteria here.\n"

    return "\n".join(
        [
            "# Task",
            "",
            "## Summary",
            "",
            summary,
            "",
            "## Acceptance Criteria",
            "",
            "- Refine this brief with the concrete constraints, deliverables, and success criteria.",
            "- Use `discussion.md` to capture the Gemini kickoff discussion and open questions.",
        ]
    ) + "\n"


def render_discussion_template(task_summary: str = "") -> str:
    summary = task_summary.strip() or "Add the research problem summary here."
    return "\n".join(
        [
            "# Discussion",
            "",
            "## Task Summary",
            "",
            summary,
            "",
            "## Discussion Notes",
            "",
            "Use this file as the durable summary of the Gemini kickoff discussion.",
            "Capture clarified goals, constraints, hypotheses, experiment ideas, decisions, and open questions here.",
        ]
    ) + "\n"


@dataclasses.dataclass
class WorkflowPaths:
    root: Path

    @property
    def repo_root(self) -> Path:
        return Path(__file__).resolve().parent.parent

    @property
    def task_md(self) -> Path:
        return self.root / "task.md"

    @property
    def discussion_md(self) -> Path:
        return self.root / "discussion.md"

    @property
    def plan_md(self) -> Path:
        return self.root / "plan.md"

    @property
    def results_md(self) -> Path:
        return self.root / "results.md"

    @property
    def progress_md(self) -> Path:
        return self.root / "progress.md"

    @property
    def state_json(self) -> Path:
        return self.root / "state.json"

    @property
    def prompts_dir(self) -> Path:
        return self.root / "prompts"


@dataclasses.dataclass
class StepResult:
    approved: bool
    summary: str
    required_changes: list[str]
    raw_output: str
    human_intervention_required: bool = False
    human_intervention_reason: str = ""


class WorkflowError(RuntimeError):
    """Raised for workflow-specific failures."""


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def parse_bool(value: Any, *, field_name: str) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off"}:
            return False
    raise WorkflowError(f"Expected boolean value for {field_name}, got {value!r}.")


def config_int(
    config: dict[str, Any],
    *,
    section: str,
    key: str,
    env_var: str,
    default: int,
) -> int:
    section_data = config.get(section, {})
    raw_value = section_data.get(key, os.environ.get(env_var, default))
    try:
        value = int(raw_value)
    except (TypeError, ValueError) as exc:
        raise WorkflowError(
            f"Expected integer for {section}.{key} / {env_var}, got {raw_value!r}."
        ) from exc
    if value < 0:
        raise WorkflowError(f"{section}.{key} / {env_var} must be >= 0.")
    return value


def runtime_context(paths: WorkflowPaths) -> str:
    details = {
        "repo_root": str(paths.repo_root),
        "workflow_workspace": str(paths.root),
        "orchestrator_cwd": os.getcwd(),
        "hostname": os.uname().nodename,
        "python_executable": sys.executable,
        "conda_env": os.environ.get("CONDA_DEFAULT_ENV", "<unset>"),
        "cuda_visible_devices": os.environ.get("CUDA_VISIBLE_DEVICES", "<unset>"),
        "workflow_sapien_shader_dir": os.environ.get("WORKFLOW_SAPIEN_SHADER_DIR", "<unset>"),
        "workflow_render_preflight_status": os.environ.get("WORKFLOW_RENDER_PREFLIGHT_STATUS", "<unset>"),
        "workflow_render_preflight_host": os.environ.get("WORKFLOW_RENDER_PREFLIGHT_HOSTNAME", "<unset>"),
        "workflow_render_preflight_cuda_visible_devices": os.environ.get(
            "WORKFLOW_RENDER_PREFLIGHT_CUDA_VISIBLE_DEVICES",
            "<unset>",
        ),
        "workflow_codex_sandbox": os.environ.get("WORKFLOW_CODEX_SANDBOX", "<unset>"),
        "workflow_codex_bypass_approvals": os.environ.get("WORKFLOW_CODEX_BYPASS_APPROVALS", "<unset>"),
        "workflow_codex_inherit_env": os.environ.get("WORKFLOW_CODEX_INHERIT_ENV", "<unset>"),
    }
    return "\n".join(f"- {key}: {value}" for key, value in details.items())


def load_yaml_file(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise WorkflowError(f"Expected mapping in {path}, got {type(data).__name__}.")
    return data


def render_manifest(manifest: dict[str, Any]) -> str:
    yaml_text = yaml.safe_dump(manifest, sort_keys=False, allow_unicode=False).strip()
    return "\n".join(
        [
            MANIFEST_START,
            "```yaml",
            yaml_text,
            "```",
            MANIFEST_END,
        ]
    )


def create_default_manifest(task_summary: str = "") -> dict[str, Any]:
    return {
        "task": task_summary,
        "status": "planning",
        "current_step": None,
        "steps": [],
        "history": [],
        "updated_at": utc_now(),
    }


def ensure_workflow_files(paths: WorkflowPaths, task_summary: str = "") -> None:
    paths.root.mkdir(parents=True, exist_ok=True)
    paths.prompts_dir.mkdir(parents=True, exist_ok=True)

    if not paths.task_md.exists():
        paths.task_md.write_text(render_task_template(task_summary), encoding="utf-8")

    if not paths.discussion_md.exists():
        paths.discussion_md.write_text(render_discussion_template(task_summary), encoding="utf-8")

    if not paths.plan_md.exists():
        manifest = create_default_manifest(task_summary=task_summary)
        paths.plan_md.write_text(
            PLAN_TEMPLATE.format(manifest_block=render_manifest(manifest)),
            encoding="utf-8",
        )

    if not paths.results_md.exists():
        paths.results_md.write_text(RESULTS_HEADER + "\n", encoding="utf-8")

    if not paths.progress_md.exists():
        paths.progress_md.write_text(PROGRESS_TEMPLATE + "\n", encoding="utf-8")

    if not paths.state_json.exists():
        initial_state = {
            "created_at": utc_now(),
            "last_discussion_launch_at": None,
            "last_planner_run_at": None,
            "last_codex_run_at": None,
            "last_review_at": None,
            "last_progress_update_at": None,
        }
        paths.state_json.write_text(
            json.dumps(initial_state, indent=2) + "\n",
            encoding="utf-8",
        )
    else:
        state = load_state(paths.state_json)
        changed = False
        for key in (
            "created_at",
            "last_discussion_launch_at",
            "last_planner_run_at",
            "last_codex_run_at",
            "last_review_at",
            "last_progress_update_at",
        ):
            if key not in state:
                state[key] = utc_now() if key == "created_at" else None
                changed = True
        if changed:
            save_state(paths.state_json, state)


def extract_manifest_block(plan_text: str) -> tuple[str, int, int]:
    pattern = re.compile(
        re.escape(MANIFEST_START)
        + r"\s*```ya?ml\s*(.*?)\s*```\s*"
        + re.escape(MANIFEST_END),
        re.DOTALL,
    )
    match = pattern.search(plan_text)
    if not match:
        raise WorkflowError(
            "Could not find workflow manifest block in plan.md. "
            "Keep the marker comments and fenced YAML block intact."
        )
    return match.group(1), match.start(), match.end()


def load_plan_manifest(plan_path: Path) -> tuple[dict[str, Any], str]:
    plan_text = plan_path.read_text(encoding="utf-8")
    manifest_text, _, _ = extract_manifest_block(plan_text)
    manifest = yaml.safe_load(manifest_text) or {}
    if not isinstance(manifest, dict):
        raise WorkflowError("Plan manifest must be a YAML mapping.")
    validate_manifest(manifest)
    return manifest, plan_text


def validate_manifest(manifest: dict[str, Any]) -> None:
    steps = manifest.get("steps")
    if not isinstance(steps, list):
        raise WorkflowError("Plan manifest must contain a list under 'steps'.")

    seen_ids: set[str] = set()
    valid_statuses = {
        "planning",
        "pending",
        "in_progress",
        "awaiting_review",
        "approved",
        "needs_changes",
        "done",
    }

    for index, step in enumerate(steps, start=1):
        if not isinstance(step, dict):
            raise WorkflowError(f"Step {index} must be a mapping.")

        step_id = step.get("id")
        if not step_id or not isinstance(step_id, str):
            raise WorkflowError(f"Step {index} is missing a string 'id'.")
        if step_id in seen_ids:
            raise WorkflowError(f"Duplicate step id '{step_id}' in plan manifest.")
        seen_ids.add(step_id)

        title = step.get("title")
        if not title or not isinstance(title, str):
            raise WorkflowError(f"Step {step_id} is missing a string 'title'.")

        status = step.get("status", "pending")
        if status not in valid_statuses:
            raise WorkflowError(
                f"Step {step_id} has unsupported status '{status}'. "
                f"Expected one of: {sorted(valid_statuses)}."
            )

        verification = step.get("verification", [])
        if not isinstance(verification, list):
            raise WorkflowError(f"Step {step_id} verification must be a list.")


def save_plan_manifest(plan_path: Path, manifest: dict[str, Any], plan_text: str) -> None:
    validate_manifest(manifest)
    manifest["updated_at"] = utc_now()
    _, start, end = extract_manifest_block(plan_text)
    updated_text = plan_text[:start] + render_manifest(manifest) + plan_text[end:]
    plan_path.write_text(updated_text, encoding="utf-8")


def get_step(manifest: dict[str, Any], step_id: str) -> dict[str, Any]:
    for step in manifest["steps"]:
        if step["id"] == step_id:
            return step
    raise WorkflowError(f"Step '{step_id}' not found in plan manifest.")


def get_active_step(manifest: dict[str, Any]) -> dict[str, Any]:
    for step in manifest["steps"]:
        if step.get("status") in {"in_progress", "awaiting_review", "needs_changes"}:
            return step
    for step in manifest["steps"]:
        if step.get("status") == "pending":
            return step
    raise WorkflowError("No pending or active steps remain in the plan manifest.")


def mark_step_status(
    plan_path: Path,
    step_id: str,
    new_status: str,
    *,
    event: str,
    details: str,
) -> dict[str, Any]:
    manifest, plan_text = load_plan_manifest(plan_path)
    step = get_step(manifest, step_id)
    step["status"] = new_status
    manifest["current_step"] = step_id
    manifest["status"] = new_status
    manifest.setdefault("history", []).append(
        {
            "step_id": step_id,
            "event": event,
            "details": details,
            "timestamp": utc_now(),
        }
    )
    save_plan_manifest(plan_path, manifest, plan_text)
    return manifest


def append_history_event(
    plan_path: Path,
    step_id: str,
    *,
    event: str,
    details: str,
) -> dict[str, Any]:
    manifest, plan_text = load_plan_manifest(plan_path)
    manifest.setdefault("history", []).append(
        {
            "step_id": step_id,
            "event": event,
            "details": details,
            "timestamp": utc_now(),
        }
    )
    save_plan_manifest(plan_path, manifest, plan_text)
    return manifest


def approve_step(plan_path: Path, step_id: str, review_summary: str) -> dict[str, Any]:
    manifest, plan_text = load_plan_manifest(plan_path)
    step = get_step(manifest, step_id)
    step["status"] = "approved"
    step["review_summary"] = review_summary
    manifest.setdefault("history", []).append(
        {
            "step_id": step_id,
            "event": "approved",
            "details": review_summary,
            "timestamp": utc_now(),
        }
    )

    next_step_id = None
    for candidate in manifest["steps"]:
        if candidate["id"] == step_id:
            continue
        if candidate.get("status") == "pending":
            next_step_id = candidate["id"]
            break

    if next_step_id is None:
        manifest["current_step"] = None
        manifest["status"] = "done"
    else:
        manifest["current_step"] = next_step_id
        manifest["status"] = "pending"

    save_plan_manifest(plan_path, manifest, plan_text)
    return manifest


def parse_command_template(template: str, **kwargs: str) -> list[str]:
    if not template.strip():
        raise WorkflowError("Command template is empty.")
    expanded = template.format(**kwargs)
    return shlex.split(expanded)


def run_external_command(
    command: list[str],
    *,
    cwd: Path,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            command,
            cwd=str(cwd),
            env=env,
            check=False,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError as exc:
        raise WorkflowError(
            f"Could not execute '{command[0]}'. Install the CLI or adjust the workflow config."
        ) from exc


def run_interactive_command(
    command: list[str],
    *,
    cwd: Path,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[Any]:
    try:
        return subprocess.run(command, cwd=str(cwd), env=env, check=False)
    except FileNotFoundError as exc:
        raise WorkflowError(
            f"Could not execute '{command[0]}'. Install the CLI or adjust the workflow config."
        ) from exc


def append_results_section(results_path: Path, heading: str, body: str) -> None:
    with results_path.open("a", encoding="utf-8") as handle:
        handle.write(f"\n## {heading}\n\n{body.rstrip()}\n")


def clip_text(text: str, max_chars: int, *, from_end: bool = False) -> str:
    stripped = text.strip()
    if len(stripped) <= max_chars:
        return stripped
    if from_end:
        return f"...\n{stripped[-max_chars:]}"
    return f"{stripped[:max_chars]}\n..."


def build_planner_prompt(paths: WorkflowPaths, config: dict[str, Any]) -> str:
    task_text = paths.task_md.read_text(encoding="utf-8")
    discussion_text = paths.discussion_md.read_text(encoding="utf-8")
    existing_plan = paths.plan_md.read_text(encoding="utf-8")
    progress_text = paths.progress_md.read_text(encoding="utf-8")
    model_hint = config.get("planner", {}).get("model", "Gemini Pro")
    parent_runtime = runtime_context(paths)

    return f"""You are the planning agent for a coding workflow.

Use {model_hint} style planning. Rewrite {paths.plan_md.name} as a concrete implementation plan.
Preserve the workflow manifest block markers and keep the YAML manifest machine-readable.

Requirements:
- Fill manifest.task with a short task summary.
- Create ordered steps under manifest.steps.
- Every step must include:
  - id: stable kebab-case id
  - title: concise label
  - status: set to pending
  - objective: short paragraph
  - implementation: list of concrete build actions
  - verification: list of commands or checks that prove the step is finished
- If this is the first plan, set manifest.current_step to the first step id and manifest.status to pending.
- If this is a replan, preserve approved steps and set manifest.current_step / manifest.status to the next actionable step instead of restarting from the beginning.
- Keep the human-readable sections below the manifest in sync with the manifest.
- Do not mark any step approved before review.
- Inspect {paths.progress_md.name} and continue from the latest recorded workflow state instead of restarting completed work.
- If the existing manifest and {paths.progress_md.name} disagree, prefer the more recent concrete execution evidence in {paths.results_md.name} and reconcile the plan.
- If the latest review rejected a step but did not require human intervention, update the plan so the next loop iteration attempts a concrete fix instead of repeating the same failed action blindly.
- Preserve already approved steps unless the evidence shows they are invalid.
- If remediation requires debugging the workflow itself, add explicit repair or diagnostic steps rather than treating the issue as a permanent external blocker.
- Prefer workflow-owned helper scripts and artifacts under the workflow workspace when automation glue is needed.
- Avoid modifying tracked files inside submodules unless there is no viable workflow-local or repository-local alternative.
- Only leave the workflow blocked on human intervention if the latest evidence shows a permission, credential, quota, unavailable external resource, or operator-owned environment change that cannot be solved from this repository.

Parent workflow runtime snapshot:
```text
{parent_runtime}
```

Task file:
```markdown
{task_text.strip()}
```

Discussion file:
```markdown
{discussion_text.strip()}
```

Current plan file:
```markdown
{existing_plan.strip()}
```

Current progress file:
```markdown
{progress_text.strip()}
```

Return the full contents of {paths.plan_md.name} only. No surrounding explanation.
"""


def build_discussion_prompt(paths: WorkflowPaths, task_summary: str = "", config: dict[str, Any] | None = None) -> str:
    task_text = paths.task_md.read_text(encoding="utf-8")
    discussion_text = paths.discussion_md.read_text(encoding="utf-8")
    parent_runtime = runtime_context(paths)
    planner_model = (config or {}).get("planner", {}).get("model", "Gemini")
    summary_line = task_summary.strip() or "No short task summary was provided."

    return f"""You are kicking off the research discussion for a coding workflow.

Use this interactive session to help the user think through the problem before any implementation plan is generated.
Work in a conversational style: clarify the goal, ask targeted follow-up questions, challenge weak assumptions, and help the user converge on a well-scoped approach.

Session requirements:
- Start by restating the current task summary and asking the user what research problem or implementation goal they want to solve.
- Use the chat to explore goals, constraints, prior attempts, risks, candidate approaches, evaluation criteria, and unknowns.
- Treat `{paths.discussion_md}` as the durable summary for later workflow runs.
- Keep `{paths.discussion_md.name}` updated during the conversation whenever material conclusions are reached, and make sure it is up to date before the user quits the chat.
- Organize `{paths.discussion_md.name}` around: problem statement, constraints, current understanding, promising directions, rejected ideas, open questions, and next actions.
- Do not generate or rewrite `{paths.plan_md.name}` in this kickoff discussion.
- If the user wants codebase-specific grounding, inspect the repository as needed before making strong claims.
- Assume the later planner/reviewer stages will read `{paths.task_md.name}` and `{paths.discussion_md.name}` verbatim.

Current short task summary:
```text
{summary_line}
```

Task file:
```markdown
{task_text.strip()}
```

Current discussion file:
```markdown
{discussion_text.strip()}
```

Workflow runtime snapshot:
```text
{parent_runtime}
```

Use {planner_model} level reasoning, but keep the interaction practical and iterative.
Before ending the session, ensure `{paths.discussion_md}` captures the final discussion summary for the workflow.
"""


def build_codex_prompt(
    paths: WorkflowPaths,
    manifest: dict[str, Any],
    step: dict[str, Any],
) -> str:
    verification_lines = "\n".join(f"- {item}" for item in step.get("verification", [])) or "- None listed"
    implementation_lines = "\n".join(f"- {item}" for item in step.get("implementation", [])) or "- No implementation notes provided"
    progress_text = paths.progress_md.read_text(encoding="utf-8")
    parent_runtime = runtime_context(paths)
    return f"""Implement exactly one approved workflow step in this repository.

Current step:
- id: {step['id']}
- title: {step['title']}
- objective: {step.get('objective', '')}

Implementation requirements:
{implementation_lines}

Verification requirements:
{verification_lines}

Inputs to read:
- Repository root: {paths.repo_root}
- {paths.plan_md}
- {paths.results_md}
- {paths.progress_md}
- {paths.task_md}
- {paths.discussion_md}

Parent workflow runtime snapshot:
```text
{parent_runtime}
```

Required behavior:
- Work only on step `{step['id']}`.
- Read `{paths.progress_md}` first and use it to avoid redoing already completed work.
- Use `{paths.repo_root}` as the repository working root when you run commands or edit files.
- Treat submodule-owned areas as read-only by default, and prefer creating helper scripts or artifacts under `{paths.root}` when you need workflow-specific glue.
- Before concluding that the host GPU, renderer, or environment is broken, compare your own execution environment against the parent workflow snapshot above. If they differ, treat that as a workflow-launch or sandbox mismatch and fix the workflow/scripts/config so future runs use the same environment as the parent workflow shell.
- Make the necessary repository changes.
- Run the verification listed for this step.
- If the first attempt fails but the failure appears fixable from this repository, repair the issue and rerun verification within the same step instead of stopping at the first error.
- Reserve requests for human intervention for cases that truly require operator action outside the repository, such as missing permissions, credentials, cluster allocation, or external services you cannot control.
- Append a new section to `{paths.results_md}` titled `Step {step['id']} - {step['title']}`.
- In that section include: summary of changes, files changed, verification performed, outcome, and any remaining risks.
- Do not modify step statuses in `{paths.plan_md}`.
- Do not continue to the next step.

Current workflow progress:
```markdown
{progress_text.strip()}
```

Current workflow manifest:
```yaml
{yaml.safe_dump(manifest, sort_keys=False, allow_unicode=False).strip()}
```
"""


def build_review_prompt(paths: WorkflowPaths, step: dict[str, Any]) -> str:
    plan_text = paths.plan_md.read_text(encoding="utf-8")
    results_text = paths.results_md.read_text(encoding="utf-8")
    progress_text = paths.progress_md.read_text(encoding="utf-8")
    parent_runtime = runtime_context(paths)
    return f"""You are the review gate for a coding workflow.

Review whether Codex completed the current step well enough to continue.
Current step:
- id: {step['id']}
- title: {step['title']}
- objective: {step.get('objective', '')}

Step verification criteria:
{yaml.safe_dump(step.get("verification", []), sort_keys=False, allow_unicode=False).strip()}

Plan file:
```markdown
{plan_text.strip()}
```

Results file:
```markdown
{results_text.strip()}
```

Current progress file:
```markdown
{progress_text.strip()}
```

Parent workflow runtime snapshot:
```text
{parent_runtime}
```

Return JSON only with this schema:
{{
  "approved": true or false,
  "summary": "short review summary",
  "required_changes": ["change 1", "change 2"],
  "human_intervention_required": true or false,
  "human_intervention_reason": "short reason or empty string"
}}

Approve only if the step is implemented and verified well enough to move on.
Set `human_intervention_required` to `true` only when the blocker clearly requires operator action outside the repository, such as missing permissions, credentials, unavailable external services, or unavailable hardware/resource allocation that the workflow cannot repair itself.
Set `human_intervention_required` to `false` for workflow bugs, stale assumptions, launcher/sandbox mismatches, missing retries, weak diagnostics, bad scripts, or other issues that a replanned repository change could fix in a later loop iteration.
"""


def build_progress_prompt(paths: WorkflowPaths, step: dict[str, Any], review: StepResult) -> str:
    task_text = clip_text(paths.task_md.read_text(encoding="utf-8"), 12000)
    discussion_text = clip_text(paths.discussion_md.read_text(encoding="utf-8"), 12000)
    plan_text = clip_text(paths.plan_md.read_text(encoding="utf-8"), 16000, from_end=True)
    results_text = clip_text(paths.results_md.read_text(encoding="utf-8"), 20000, from_end=True)
    current_progress = clip_text(paths.progress_md.read_text(encoding="utf-8"), 8000, from_end=True)
    return f"""You are maintaining the workflow progress checkpoint for a coding workflow.

Rewrite {paths.progress_md.name} so a future run can resume from the latest state with minimal ambiguity.

Requirements:
- Return the full contents of {paths.progress_md.name} only. No surrounding explanation.
- Base the summary on the current task, plan, results, and latest review outcome.
- Keep the document concise but specific.
- Include these sections in order:
  1. # Workflow Progress
  2. ## Current Status
  3. ## Completed Steps
  4. ## Latest Review
  5. ## Open Issues
  6. ## Next Step
  7. ## Resume Instructions
- In `Completed Steps`, list approved steps only.
- In `Latest Review`, capture the reviewed step, whether it was approved, and the important rationale.
- In `Open Issues`, list blockers, unresolved risks, or required changes. If none, say so.
- In `Next Step`, identify the exact next step id/title if one is pending; otherwise state that the workflow is done.
- In `Resume Instructions`, explain what files or commands the next run should inspect first.

Task file:
```markdown
{task_text.strip()}
```

Discussion file:
```markdown
{discussion_text.strip()}
```

Plan file:
```markdown
{plan_text.strip()}
```

Results file:
```markdown
{results_text.strip()}
```

Existing progress file:
```markdown
{current_progress.strip()}
```

Latest review outcome:
```json
{json.dumps(
    {
        "step_id": step["id"],
        "step_title": step["title"],
        "approved": review.approved,
        "summary": review.summary,
        "required_changes": review.required_changes,
        "human_intervention_required": review.human_intervention_required,
        "human_intervention_reason": review.human_intervention_reason,
    },
    indent=2,
)}
```
"""


def latest_history_event(
    manifest: dict[str, Any],
    *,
    step_id: str | None = None,
    events: set[str] | None = None,
) -> dict[str, Any] | None:
    for entry in reversed(manifest.get("history", [])):
        if step_id is not None and entry.get("step_id") != step_id:
            continue
        if events is not None and entry.get("event") not in events:
            continue
        return entry
    return None


def build_manifest_progress(
    paths: WorkflowPaths,
    *,
    latest_step: dict[str, Any] | None = None,
    review: StepResult | None = None,
    progress_error: str | None = None,
) -> str:
    manifest, _ = load_plan_manifest(paths.plan_md)
    completed_steps = [f"- `{item['id']}`" for item in manifest["steps"] if item.get("status") == "approved"]
    active_step_id = manifest.get("current_step")
    active_step = get_step(manifest, active_step_id) if active_step_id else None
    latest_step = latest_step or active_step

    current_status_lines: list[str] = []
    if manifest.get("status") == "done":
        current_status_lines.append("- The workflow is complete.")
    elif active_step is None:
        current_status_lines.append("- No active step is recorded in the manifest.")
    elif active_step.get("status") == "awaiting_review":
        current_status_lines.append(
            f"- Step `{active_step['id']}` ({active_step['title']}) completed implementation and is awaiting Gemini review."
        )
    elif active_step.get("status") == "in_progress":
        current_status_lines.append(
            f"- Step `{active_step['id']}` ({active_step['title']}) is currently in progress."
        )
    elif active_step.get("status") == "needs_changes":
        current_status_lines.append(
            f"- Step `{active_step['id']}` ({active_step['title']}) needs changes before the workflow can continue."
        )
    else:
        current_status_lines.append(
            f"- The next actionable step is `{active_step['id']}` ({active_step['title']})."
        )
    current_status_lines.append(f"- **Workflow Status:** `{manifest.get('status', 'unknown')}`")

    if review is not None and latest_step is not None:
        latest_review_lines = [
            f"- **Step:** `{latest_step['id']}`",
            f"- **Approved:** `{str(review.approved).lower()}`",
            f"- **Rationale:** {review.summary}",
        ]
    else:
        review_event = latest_history_event(manifest, events={"approved", "changes_requested"})
        if review_event is None:
            latest_review_lines = ["- No Gemini review has been recorded yet for the current workflow state."]
        else:
            approved = review_event.get("event") == "approved"
            latest_review_lines = [
                f"- **Step:** `{review_event.get('step_id', 'unknown')}`",
                f"- **Approved:** `{str(approved).lower()}`",
                f"- **Rationale:** {review_event.get('details', 'No review summary recorded.')}",
            ]

    open_issues: list[str] = []
    if progress_error:
        open_issues.append(f"- Progress checkpoint fallback was used: {progress_error}")
    if review is not None and not review.approved:
        open_issues.extend(f"- {item}" for item in review.required_changes)
        if review.human_intervention_required:
            reason = review.human_intervention_reason or review.summary
            open_issues.append(f"- Human intervention required: {reason}")
    elif active_step is not None:
        active_status = active_step.get("status")
        if active_status == "awaiting_review":
            open_issues.append(f"- Review for `{active_step['id']}` has not run yet.")
        elif active_status == "needs_changes":
            latest_failure = latest_history_event(manifest, step_id=active_step["id"])
            details = latest_failure.get("details") if latest_failure else None
            if details:
                open_issues.append(f"- {details}")
    if not open_issues:
        open_issues.append("- None recorded.")

    if manifest.get("status") == "done":
        next_step_line = "- Workflow is complete."
        resume_lines = [
            f"- Inspect `{paths.results_md.name}` and `{paths.plan_md.name}` for the final approved record.",
        ]
    elif active_step is None:
        next_step_line = "- No actionable step is recorded."
        resume_lines = [
            f"- Inspect `{paths.plan_md.name}` and `{paths.results_md.name}` to repair the workflow manifest before continuing.",
        ]
    elif active_step.get("status") == "awaiting_review":
        next_step_line = f"- Review `{active_step['id']}` ({active_step['title']})."
        resume_lines = [
            f"- Inspect `{paths.results_md.name}` and `{paths.plan_md.name}` first.",
            f"- Run `python workflow/orchestrator.py --workspace {paths.root} review --step-id {active_step['id']}` or resume `loop` from the same workspace.",
        ]
    else:
        next_step_line = f"- `{active_step['id']}` ({active_step['title']})"
        resume_lines = [
            f"- Inspect `{paths.progress_md.name}`, `{paths.results_md.name}`, and `{paths.plan_md.name}` first.",
            f"- Resume from step `{active_step['id']}`.",
        ]

    return "\n".join(
        [
            "# Workflow Progress",
            "",
            "## Current Status",
            *current_status_lines,
            "",
            "## Completed Steps",
            *(completed_steps or ["- None yet."]),
            "",
            "## Latest Review",
            *latest_review_lines,
            "",
            "## Open Issues",
            *open_issues,
            "",
            "## Next Step",
            next_step_line,
            "",
            "## Resume Instructions",
            *resume_lines,
        ]
    )


def build_fallback_progress(paths: WorkflowPaths, step: dict[str, Any], review: StepResult) -> str:
    return build_manifest_progress(paths, latest_step=step, review=review)


def write_progress_snapshot(paths: WorkflowPaths, progress_output: str) -> None:
    paths.progress_md.write_text(progress_output.rstrip() + "\n", encoding="utf-8")
    update_state_timestamp(paths.state_json, "last_progress_update_at")


def write_prompt_file(prompt_path: Path, prompt_text: str) -> None:
    prompt_path.parent.mkdir(parents=True, exist_ok=True)
    prompt_path.write_text(prompt_text, encoding="utf-8")


def load_state(state_path: Path) -> dict[str, Any]:
    return json.loads(state_path.read_text(encoding="utf-8"))


def save_state(state_path: Path, state: dict[str, Any]) -> None:
    state_path.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")


def update_state_timestamp(state_path: Path, key: str) -> None:
    state = load_state(state_path)
    state[key] = utc_now()
    save_state(state_path, state)


def planner_command_config(config: dict[str, Any]) -> str:
    planner = config.get("planner", {})
    template = os.environ.get("WORKFLOW_GEMINI_CMD") or planner.get("command_template")
    if not template:
        raise WorkflowError(
            "Planner command template is not configured. "
            "Set planner.command_template in the config file or WORKFLOW_GEMINI_CMD."
        )
    return template


def discussion_command_config(config: dict[str, Any]) -> str:
    discussion = config.get("discussion", {})
    template = os.environ.get("WORKFLOW_GEMINI_DISCUSSION_CMD") or discussion.get("command_template")
    if not template:
        raise WorkflowError(
            "Discussion command template is not configured. "
            "Set discussion.command_template in the config file or WORKFLOW_GEMINI_DISCUSSION_CMD."
        )
    return template


def executor_command_config(config: dict[str, Any]) -> str:
    executor = config.get("executor", {})
    template = os.environ.get("WORKFLOW_CODEX_CMD") or executor.get("command_template")
    if not template:
        raise WorkflowError(
            "Executor command template is not configured. "
            "Set executor.command_template in the config file or WORKFLOW_CODEX_CMD."
        )
    return template


def run_discussion_session(paths: WorkflowPaths, config: dict[str, Any], task_summary: str = "") -> bool:
    prompt_text = build_discussion_prompt(paths, task_summary, config)
    prompt_path = paths.prompts_dir / "discussion_prompt.txt"
    write_prompt_file(prompt_path, prompt_text)

    before_text = paths.discussion_md.read_text(encoding="utf-8")
    command = parse_command_template(
        discussion_command_config(config),
        prompt_file=str(prompt_path),
        workspace=str(paths.root),
        repo_root=str(paths.repo_root),
        plan=str(paths.plan_md),
        results=str(paths.results_md),
        progress=str(paths.progress_md),
        task=str(paths.task_md),
        discussion=str(paths.discussion_md),
    )
    result = run_interactive_command(command, cwd=paths.repo_root)
    if result.returncode != 0:
        raise WorkflowError(f"Discussion command failed with exit code {result.returncode}.")

    update_state_timestamp(paths.state_json, "last_discussion_launch_at")
    after_text = paths.discussion_md.read_text(encoding="utf-8")
    return after_text != before_text


def run_progress_update(paths: WorkflowPaths, config: dict[str, Any], step: dict[str, Any], review: StepResult) -> None:
    prompt_text = build_progress_prompt(paths, step, review)
    prompt_path = paths.prompts_dir / f"progress_{step['id']}.txt"
    write_prompt_file(prompt_path, prompt_text)

    fallback_reason: str | None = None
    result: subprocess.CompletedProcess[str] | None = None
    try:
        command = parse_command_template(
            planner_command_config(config),
            prompt_file=str(prompt_path),
            workspace=str(paths.root),
            repo_root=str(paths.repo_root),
            plan=str(paths.plan_md),
            results=str(paths.results_md),
            progress=str(paths.progress_md),
            task=str(paths.task_md),
            discussion=str(paths.discussion_md),
            step_id=step["id"],
        )
        result = run_external_command(command, cwd=paths.root)
        if result.returncode != 0:
            fallback_reason = (
                "Progress summarizer command failed.\n"
                f"stdout:\n{result.stdout}\n\nstderr:\n{result.stderr}"
            )
        else:
            progress_output = result.stdout.strip()
            if not progress_output:
                fallback_reason = "The planner command returned empty stdout during progress checkpoint generation."
            elif not progress_output.lstrip().startswith("# Workflow Progress"):
                fallback_reason = (
                    "The planner command returned output that did not start with '# Workflow Progress', "
                    "so a deterministic checkpoint was written instead."
                )
    except WorkflowError as exc:
        fallback_reason = str(exc)

    if fallback_reason is not None:
        progress_output = build_manifest_progress(
            paths,
            latest_step=step,
            review=review,
            progress_error=fallback_reason,
        )
        append_results_section(
            paths.results_md,
            f"Progress Fallback - {step['id']}",
            "\n".join(
                [
                    "Summary:",
                    "The orchestrator wrote a deterministic progress checkpoint because the planner-based progress update was unavailable or invalid.",
                    "A deterministic fallback progress.md was written from the manifest and latest review so the workflow can continue.",
                    "",
                    "Fallback reason:",
                    "```text",
                    fallback_reason,
                    "```",
                    "",
                    "Planner stderr:",
                    "```text",
                    (result.stderr.strip() if result is not None else "") or "(empty)",
                    "```",
                ]
            ),
        )

    write_progress_snapshot(paths, progress_output)


def run_planner(paths: WorkflowPaths, config: dict[str, Any]) -> None:
    prompt_text = build_planner_prompt(paths, config)
    prompt_path = paths.prompts_dir / "planner_prompt.txt"
    write_prompt_file(prompt_path, prompt_text)

    command = parse_command_template(
        planner_command_config(config),
        prompt_file=str(prompt_path),
        workspace=str(paths.root),
        repo_root=str(paths.repo_root),
        plan=str(paths.plan_md),
        results=str(paths.results_md),
        progress=str(paths.progress_md),
        task=str(paths.task_md),
        discussion=str(paths.discussion_md),
    )
    result = run_external_command(command, cwd=paths.root)
    if result.returncode != 0:
        raise WorkflowError(
            "Planner command failed.\n"
            f"stdout:\n{result.stdout}\n\nstderr:\n{result.stderr}"
        )

    planner_output = result.stdout.strip()
    if not planner_output:
        raise WorkflowError("Planner command returned empty output.")

    paths.plan_md.write_text(planner_output + "\n", encoding="utf-8")
    manifest, _ = load_plan_manifest(paths.plan_md)
    if not manifest["steps"]:
        raise WorkflowError("Planner did not populate any steps in the manifest.")

    update_state_timestamp(paths.state_json, "last_planner_run_at")
    append_results_section(
        paths.results_md,
        "Planner Update",
        f"Generated or refreshed `{paths.plan_md.name}` at {utc_now()}.",
    )


def run_executor(paths: WorkflowPaths, config: dict[str, Any], step_id: str | None = None) -> dict[str, Any]:
    manifest, _ = load_plan_manifest(paths.plan_md)
    step = get_step(manifest, step_id) if step_id else get_active_step(manifest)
    if step.get("status") == "approved":
        raise WorkflowError(f"Step '{step['id']}' is already approved.")

    mark_step_status(
        paths.plan_md,
        step["id"],
        "in_progress",
        event="started",
        details="Codex execution started for this step.",
    )
    write_progress_snapshot(
        paths,
        build_manifest_progress(paths, latest_step=step),
    )
    manifest, _ = load_plan_manifest(paths.plan_md)
    step = get_step(manifest, step["id"])

    prompt_text = build_codex_prompt(paths, manifest, step)
    prompt_path = paths.prompts_dir / f"codex_{step['id']}.txt"
    write_prompt_file(prompt_path, prompt_text)

    command = parse_command_template(
        executor_command_config(config),
        prompt_file=str(prompt_path),
        workspace=str(paths.root),
        repo_root=str(paths.repo_root),
        plan=str(paths.plan_md),
        results=str(paths.results_md),
        progress=str(paths.progress_md),
        task=str(paths.task_md),
        discussion=str(paths.discussion_md),
        step_id=step["id"],
    )
    result = run_external_command(command, cwd=paths.repo_root)
    if result.returncode != 0:
        mark_step_status(
            paths.plan_md,
            step["id"],
            "needs_changes",
            event="executor_failed",
            details=result.stderr.strip() or "Executor command failed.",
        )
        write_progress_snapshot(
            paths,
            build_manifest_progress(paths, latest_step=step),
        )
        raise WorkflowError(
            "Executor command failed.\n"
            f"stdout:\n{result.stdout}\n\nstderr:\n{result.stderr}"
        )

    mark_step_status(
        paths.plan_md,
        step["id"],
        "awaiting_review",
        event="implementation_complete",
        details="Codex completed implementation and verification for this step.",
    )
    write_progress_snapshot(
        paths,
        build_manifest_progress(paths, latest_step=step),
    )
    update_state_timestamp(paths.state_json, "last_codex_run_at")
    return get_step(load_plan_manifest(paths.plan_md)[0], step["id"])


def parse_review_json(raw_output: str) -> StepResult:
    stripped = raw_output.strip()
    if not stripped:
        raise WorkflowError("Reviewer returned empty output.")

    try:
        payload = json.loads(stripped)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", stripped, re.DOTALL)
        if not match:
            raise WorkflowError("Reviewer output did not contain parseable JSON.")
        payload = json.loads(match.group(0))

    if not isinstance(payload, dict):
        raise WorkflowError("Reviewer JSON must be an object.")

    approved = payload.get("approved")
    if not isinstance(approved, bool):
        raise WorkflowError("Reviewer JSON must include boolean field 'approved'.")

    summary = payload.get("summary", "")
    if not isinstance(summary, str):
        raise WorkflowError("Reviewer JSON field 'summary' must be a string.")

    required_changes = payload.get("required_changes", [])
    if not isinstance(required_changes, list):
        raise WorkflowError("Reviewer JSON field 'required_changes' must be a list.")

    human_intervention_required_raw = payload.get("human_intervention_required", False)
    human_intervention_required = parse_bool(
        human_intervention_required_raw,
        field_name="human_intervention_required",
    )

    human_intervention_reason = payload.get("human_intervention_reason", "")
    if not isinstance(human_intervention_reason, str):
        raise WorkflowError("Reviewer JSON field 'human_intervention_reason' must be a string.")

    return StepResult(
        approved=approved,
        summary=summary or "No review summary provided.",
        required_changes=[str(item) for item in required_changes],
        raw_output=stripped,
        human_intervention_required=human_intervention_required,
        human_intervention_reason=human_intervention_reason.strip(),
    )


def run_review(paths: WorkflowPaths, config: dict[str, Any], step_id: str | None = None) -> StepResult:
    manifest, _ = load_plan_manifest(paths.plan_md)
    step = get_step(manifest, step_id) if step_id else get_active_step(manifest)
    if step.get("status") not in {"awaiting_review", "needs_changes", "in_progress"}:
        raise WorkflowError(
            f"Step '{step['id']}' is not ready for review; current status is '{step.get('status')}'."
        )

    prompt_text = build_review_prompt(paths, step)
    prompt_path = paths.prompts_dir / f"review_{step['id']}.txt"
    write_prompt_file(prompt_path, prompt_text)

    command = parse_command_template(
        planner_command_config(config),
        prompt_file=str(prompt_path),
        workspace=str(paths.root),
        repo_root=str(paths.repo_root),
        plan=str(paths.plan_md),
        results=str(paths.results_md),
        progress=str(paths.progress_md),
        task=str(paths.task_md),
        discussion=str(paths.discussion_md),
        step_id=step["id"],
    )
    result = run_external_command(command, cwd=paths.root)
    if result.returncode != 0:
        raise WorkflowError(
            "Reviewer command failed.\n"
            f"stdout:\n{result.stdout}\n\nstderr:\n{result.stderr}"
        )

    review = parse_review_json(result.stdout)
    review_body = [
        f"Step: `{step['id']}`",
        f"Approved: `{str(review.approved).lower()}`",
        "",
        "Summary:",
        review.summary,
    ]
    if review.required_changes:
        review_body.extend(
            [
                "",
                "Required changes:",
                *[f"- {item}" for item in review.required_changes],
            ]
        )
    if review.human_intervention_required:
        review_body.extend(
            [
                "",
                "Human intervention required:",
                review.human_intervention_reason or review.summary,
            ]
        )
    append_results_section(
        paths.results_md,
        f"Gemini Review - {step['id']}",
        "\n".join(review_body),
    )

    if review.approved:
        approve_step(paths.plan_md, step["id"], review.summary)
    else:
        mark_step_status(
            paths.plan_md,
            step["id"],
            "needs_changes",
            event="changes_requested",
            details=review.summary,
        )

    update_state_timestamp(paths.state_json, "last_review_at")
    run_progress_update(paths, config, step, review)
    return review


def workflow_status(paths: WorkflowPaths) -> str:
    manifest, _ = load_plan_manifest(paths.plan_md)
    lines = [
        f"Task: {manifest.get('task') or '(not set)'}",
        f"Workflow status: {manifest.get('status', '(unknown)')}",
        f"Current step: {manifest.get('current_step') or '(none)'}",
        "",
        "Steps:",
    ]
    for step in manifest["steps"]:
        lines.append(f"- {step['id']}: {step['title']} [{step.get('status', 'pending')}]")
    return "\n".join(lines)


def run_auto_replan(
    paths: WorkflowPaths,
    config: dict[str, Any],
    step: dict[str, Any],
    review: StepResult,
    *,
    attempt: int,
    max_attempts: int,
) -> None:
    append_results_section(
        paths.results_md,
        f"Auto Replan - {step['id']}",
        "\n".join(
            [
                f"Step: `{step['id']}`",
                f"Attempt: `{attempt}` of `{max_attempts}`",
                "",
                "Summary:",
                "The latest review rejected this step, but did not require human intervention.",
                "The workflow is replanning automatically so the next loop iteration can attempt a concrete fix instead of stopping immediately.",
                "",
                "Latest review summary:",
                review.summary,
                "",
                "Required changes:",
                *([f"- {item}" for item in review.required_changes] or ["- None provided."]),
            ]
        ),
    )
    run_planner(paths, config)
    manifest, _ = load_plan_manifest(paths.plan_md)
    append_history_event(
        paths.plan_md,
        step["id"],
        event="auto_replanned",
        details=(
            f"Automatic replanning attempt {attempt} of {max_attempts} after rejected review. "
            f"Workflow current_step is now '{manifest.get('current_step')}'."
        ),
    )
    run_progress_update(paths, config, step, review)


def run_loop(
    paths: WorkflowPaths,
    config: dict[str, Any],
    max_steps: int | None = None,
    max_auto_replans_per_step: int | None = None,
) -> None:
    manifest, _ = load_plan_manifest(paths.plan_md)
    if not manifest["steps"]:
        run_planner(paths, config)

    approved_steps = 0
    auto_replans_by_step: dict[str, int] = {}
    replan_limit = (
        max_auto_replans_per_step
        if max_auto_replans_per_step is not None
        else config_int(
            config,
            section="workflow",
            key="max_auto_replans_per_step",
            env_var="WORKFLOW_MAX_AUTO_REPLANS_PER_STEP",
            default=2,
        )
    )
    while True:
        manifest, _ = load_plan_manifest(paths.plan_md)
        if manifest.get("status") == "done":
            return

        step = get_active_step(manifest)
        if step.get("status") == "awaiting_review":
            review = run_review(paths, config, step["id"])
            if not review.approved:
                if review.human_intervention_required:
                    reason = review.human_intervention_reason or review.summary
                    raise WorkflowError(
                        f"Review rejected step '{step['id']}' and requires human intervention: {reason}"
                    )

                prior_attempts = auto_replans_by_step.get(step["id"], 0)
                if prior_attempts >= replan_limit:
                    raise WorkflowError(
                        f"Review rejected step '{step['id']}' after {prior_attempts} automatic replans. "
                        "Auto-replan limit reached; inspect results.md and progress.md."
                    )

                attempt = prior_attempts + 1
                auto_replans_by_step[step["id"]] = attempt
                run_auto_replan(
                    paths,
                    config,
                    step,
                    review,
                    attempt=attempt,
                    max_attempts=replan_limit,
                )
                continue
            auto_replans_by_step.pop(step["id"], None)
            approved_steps += 1
        else:
            run_executor(paths, config, step["id"])

        if max_steps is not None and approved_steps >= max_steps:
            return


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Gemini planner / Codex executor workflow runner.")
    parser.add_argument(
        "--workspace",
        default="workflow_runs/default",
        help="Directory containing task.md, discussion.md, plan.md, and results.md.",
    )
    parser.add_argument(
        "--config",
        default="workflow/config.example.yaml",
        help="Workflow config file with planner and executor command templates.",
    )

    subparsers = parser.add_subparsers(dest="command", required=True)

    init_parser = subparsers.add_parser("init", help="Create workflow files in the workspace.")
    init_parser.add_argument("--task-summary", default="", help="Short task summary for the initial manifest.")
    init_parser.add_argument(
        "--no-discussion",
        action="store_true",
        help="Only initialize the workspace files; do not launch the interactive Gemini kickoff discussion.",
    )

    subparsers.add_parser("plan", help="Generate or refresh plan.md using Gemini.")

    run_step_parser = subparsers.add_parser("run-step", help="Run Codex for the current or specified step.")
    run_step_parser.add_argument("--step-id", default=None, help="Explicit step id to execute.")

    review_parser = subparsers.add_parser("review", help="Run Gemini review for the current or specified step.")
    review_parser.add_argument("--step-id", default=None, help="Explicit step id to review.")

    loop_parser = subparsers.add_parser("loop", help="Run the full execute-review loop until blocked or done.")
    loop_parser.add_argument("--max-steps", type=int, default=None, help="Stop after this many approved steps.")
    loop_parser.add_argument(
        "--max-auto-replans-per-step",
        type=int,
        default=None,
        help="Override how many times the loop may automatically replan a rejected step before stopping.",
    )

    subparsers.add_parser("status", help="Print workflow status.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    paths = WorkflowPaths(root=Path(args.workspace).resolve())
    config = load_yaml_file(Path(args.config).resolve())

    try:
        if args.command == "init":
            ensure_workflow_files(paths, task_summary=args.task_summary)
            print(f"Initialized workflow workspace at {paths.root}")
            if args.no_discussion:
                return 0

            if not sys.stdin.isatty() or not sys.stdout.isatty():
                print("Interactive Gemini discussion skipped because stdin/stdout is not a TTY.")
                return 0

            print(
                f"Launching Gemini kickoff discussion. Keep {paths.discussion_md.name} updated before you exit the chat."
            )
            discussion_changed = run_discussion_session(paths, config, args.task_summary)
            if discussion_changed:
                print(f"Updated {paths.discussion_md}")
            else:
                print(f"Gemini discussion exited without changing {paths.discussion_md}")
            return 0

        ensure_workflow_files(paths)

        if args.command == "plan":
            run_planner(paths, config)
            print(f"Updated {paths.plan_md}")
            return 0

        if args.command == "run-step":
            step = run_executor(paths, config, args.step_id)
            print(f"Step {step['id']} is ready for review.")
            return 0

        if args.command == "review":
            review = run_review(paths, config, args.step_id)
            status = "approved" if review.approved else "changes_requested"
            print(status)
            return 0 if review.approved else 2

        if args.command == "loop":
            run_loop(paths, config, args.max_steps, args.max_auto_replans_per_step)
            print("Workflow loop completed.")
            return 0

        if args.command == "status":
            print(workflow_status(paths))
            return 0
    except WorkflowError as exc:
        print(f"Workflow error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("Workflow interrupted.", file=sys.stderr)
        return 130

    raise AssertionError(f"Unhandled command: {args.command}")


if __name__ == "__main__":
    raise SystemExit(main())
