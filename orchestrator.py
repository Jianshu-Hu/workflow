from __future__ import annotations

import argparse
import copy
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

This file is rewritten after each review.
It should summarize the current state so a later workflow run can resume from here.

## Current Status

- No reviews yet.

## Completed Steps

- None yet.

## Latest Review

- No review has been recorded yet.

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

The planner should rewrite this file while preserving the manifest block markers above.
Each step should explain what to build and how success will be verified.
"""

COMMAND_FAILURE_STDOUT_CHARS = 4000
COMMAND_FAILURE_STDERR_CHARS = 12000
MANIFEST_HISTORY_PROMPT_ENTRIES = 12
MANIFEST_HISTORY_DETAIL_CHARS = 2000
MANIFEST_HISTORY_SAVE_ENTRIES = 40
PLAN_PROMPT_CHARS = 32000
PROGRESS_PROMPT_CHARS = 12000
RESULTS_PROMPT_CHARS = 24000
APPROVED_STEP_SUMMARY_CHARS = 500
RESULTS_SUMMARY_CHARS = 400
DISCUSSION_TRANSCRIPT_PROMPT_CHARS = 60000


def normalize_related_links(related_links: list[str] | None) -> list[str]:
    if not related_links:
        return []
    normalized: list[str] = []
    seen: set[str] = set()
    for item in related_links:
        value = item.strip()
        if not value or value.lower() == "none" or value in seen:
            continue
        normalized.append(value)
        seen.add(value)
    return normalized


def prompt_for_related_links() -> list[str]:
    print("Add related links for this workflow, one per line.")
    print("Supported examples: GitHub repos, arXiv papers, local file paths.")
    print("Press Enter on an empty line when finished, or type 'none' to skip.")
    links: list[str] = []
    while True:
        try:
            response = input("Related link: ").strip()
        except EOFError:
            print()
            break
        if not response:
            break
        if response.lower() == "none":
            return []
        links.append(response)
    return normalize_related_links(links)


def render_task_template(task_summary: str = "", related_links: list[str] | None = None) -> str:
    summary = task_summary.strip()
    normalized_links = normalize_related_links(related_links)
    if not summary:
        sections = [
            "# Task",
            "",
            "Describe the goal, constraints, and acceptance criteria here.",
        ]
        if normalized_links:
            sections.extend(
                [
                    "",
                    "## Related Links",
                    "",
                    *[f"- {item}" for item in normalized_links],
                ]
            )
        return "\n".join(sections) + "\n"

    return "\n".join(
        [
            "# Task",
            "",
            "## Summary",
            "",
            summary,
            "",
            "## Related Links",
            "",
            *([f"- {item}" for item in normalized_links] or ["- None provided."]),
            "",
            "## Acceptance Criteria",
            "",
            "- Refine this brief with the concrete constraints, deliverables, and success criteria.",
            "- Use `discussion.md` to capture the kickoff discussion and open questions.",
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
            "## Problem Statement",
            "",
            "Clarify the problem to solve and the intended deliverable.",
            "",
            "## Constraints",
            "",
            "- None recorded yet.",
            "",
            "## Current Understanding",
            "",
            "- None recorded yet.",
            "",
            "## Promising Directions",
            "",
            "- None recorded yet.",
            "",
            "## Rejected Ideas",
            "",
            "- None recorded yet.",
            "",
            "## Open Questions",
            "",
            "- None recorded yet.",
            "",
            "## Next Actions",
            "",
            "- Continue the kickoff discussion. This summary will be refreshed from the discussion transcript after the session.",
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

    @property
    def artifacts_dir(self) -> Path:
        return self.root / "artifacts"

    @property
    def command_artifacts_dir(self) -> Path:
        return self.artifacts_dir / "command_failures"

    @property
    def discussion_transcript(self) -> Path:
        return self.artifacts_dir / "discussion_transcript.txt"

    @property
    def discussion_input_log(self) -> Path:
        return self.artifacts_dir / "discussion_input.log"

    @property
    def discussion_output_log(self) -> Path:
        return self.artifacts_dir / "discussion_output.log"


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


def parse_runtime_env_line(line: str) -> tuple[str, str] | None:
    stripped = line.strip()
    if not stripped or stripped.startswith("#"):
        return None

    if stripped.startswith("export "):
        stripped = stripped[len("export ") :].strip()

    if "=" not in stripped:
        return None

    key, value = stripped.split("=", 1)
    key = key.strip()
    value = value.strip()
    if not key:
        return None

    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        value = value[1:-1]

    return key, value


def load_runtime_env_file(path: Path) -> None:
    if not path.exists():
        return

    for line in path.read_text(encoding="utf-8").splitlines():
        parsed = parse_runtime_env_line(line)
        if parsed is None:
            continue
        key, value = parsed
        os.environ.setdefault(key, value)


def upsert_runtime_env_file(path: Path, assignments: dict[str, str]) -> None:
    normalized = {key: value for key, value in assignments.items() if value}
    if not normalized:
        return

    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    updated_lines: list[str] = []
    seen: set[str] = set()

    for line in lines:
        parsed = parse_runtime_env_line(line)
        if parsed is None:
            updated_lines.append(line)
            continue

        key, _ = parsed
        if key in normalized:
            updated_lines.append(f"export {key}={shlex.quote(normalized[key])}")
            seen.add(key)
        else:
            updated_lines.append(line)

    missing_items = [(key, value) for key, value in normalized.items() if key not in seen]
    if missing_items:
        if updated_lines and updated_lines[-1].strip():
            updated_lines.append("")
        if not path.exists():
            updated_lines.append("# Workflow model overrides for this workspace.")
        for key, value in missing_items:
            updated_lines.append(f"export {key}={shlex.quote(value)}")

    path.write_text("\n".join(updated_lines).rstrip() + "\n", encoding="utf-8")


def apply_runtime_env_overrides(assignments: dict[str, str]) -> None:
    for key, value in assignments.items():
        if value:
            os.environ[key] = value


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


def step_label(step: dict[str, Any]) -> str:
    return f"`{step['id']}` - {step['title']}"


def summarize_step_review(step: dict[str, Any]) -> str:
    review_summary = step.get("review_summary")
    if isinstance(review_summary, str) and review_summary.strip():
        return clip_text(review_summary.strip(), APPROVED_STEP_SUMMARY_CHARS, from_end=True)
    return "No review summary recorded."


def render_step_summary(step: dict[str, Any]) -> list[str]:
    lines = [f"- {step_label(step)} [{step.get('status', 'pending')}]"]
    if step.get("status") == "approved":
        lines.append(f"  Review: {summarize_step_review(step)}")
    elif step.get("status") == "done":
        lines.append("  Completed.")
    elif step.get("status") == "needs_changes":
        lines.append("  Needs changes before the workflow can continue.")
    return lines


def render_step_detail(step: dict[str, Any]) -> str:
    implementation_lines = [f"- {item}" for item in step.get("implementation", [])] or ["- None recorded."]
    verification_lines = [f"- {item}" for item in step.get("verification", [])] or ["- None recorded."]
    objective = str(step.get("objective", "")).strip() or "No objective recorded."
    return "\n".join(
        [
            f"### Step {step_label(step)}",
            "",
            f"- Status: `{step.get('status', 'pending')}`",
            "",
            "Objective:",
            objective,
            "",
            "Implementation:",
            *implementation_lines,
            "",
            "Verification:",
            *verification_lines,
        ]
    )


def render_plan_document(manifest: dict[str, Any]) -> str:
    approved_steps = [step for step in manifest.get("steps", []) if step.get("status") in {"approved", "done"}]
    active_step = next(
        (
            step
            for step in manifest.get("steps", [])
            if step.get("status") in {"in_progress", "needs_changes", "awaiting_review"}
        ),
        None,
    )
    if active_step is None:
        active_step = next((step for step in manifest.get("steps", []) if step.get("status") == "pending"), None)
    upcoming_steps = [
        step
        for step in manifest.get("steps", [])
        if step is not active_step and step.get("status") in {"pending", "needs_changes", "in_progress", "awaiting_review"}
    ]

    sections = [
        "# Workflow Plan",
        "",
        render_manifest(manifest),
        "",
        "## Workflow Summary",
        "",
        f"- Task: {manifest.get('task') or '(not set)'}",
        f"- Workflow status: `{manifest.get('status', 'unknown')}`",
        f"- Current step: `{manifest.get('current_step') or 'none'}`",
        "",
        "## Completed Steps",
        "",
    ]
    if approved_steps:
        for step in approved_steps:
            sections.extend(render_step_summary(step))
    else:
        sections.append("- None yet.")

    sections.extend(["", "## Current Step", ""])
    if active_step is not None:
        sections.append(render_step_detail(active_step))
    else:
        sections.append("No active step is recorded.")

    sections.extend(["", "## Upcoming Steps", ""])
    if upcoming_steps:
        for index, step in enumerate(upcoming_steps):
            if index:
                sections.extend(["", render_step_detail(step)])
            else:
                sections.append(render_step_detail(step))
    else:
        sections.append("No upcoming steps are recorded.")

    return "\n".join(sections).rstrip() + "\n"


def create_default_manifest(task_summary: str = "") -> dict[str, Any]:
    return {
        "task": task_summary,
        "status": "planning",
        "current_step": None,
        "steps": [],
        "history": [],
        "updated_at": utc_now(),
    }


def ensure_workflow_files(
    paths: WorkflowPaths,
    task_summary: str = "",
    related_links: list[str] | None = None,
) -> None:
    paths.root.mkdir(parents=True, exist_ok=True)
    paths.prompts_dir.mkdir(parents=True, exist_ok=True)
    paths.artifacts_dir.mkdir(parents=True, exist_ok=True)
    paths.command_artifacts_dir.mkdir(parents=True, exist_ok=True)

    if not paths.task_md.exists():
        paths.task_md.write_text(
            render_task_template(task_summary, related_links=related_links),
            encoding="utf-8",
        )

    if not paths.discussion_md.exists():
        paths.discussion_md.write_text(render_discussion_template(task_summary), encoding="utf-8")

    if not paths.plan_md.exists():
        manifest = create_default_manifest(task_summary=task_summary)
        paths.plan_md.write_text(render_plan_document(manifest), encoding="utf-8")

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


def compact_manifest_for_storage(manifest: dict[str, Any]) -> dict[str, Any]:
    compact = copy.deepcopy(manifest)

    history = compact.get("history")
    if isinstance(history, list):
        trimmed_history = history[-MANIFEST_HISTORY_SAVE_ENTRIES:]
        for entry in trimmed_history:
            if isinstance(entry, dict) and isinstance(entry.get("details"), str):
                entry["details"] = clip_history_details(entry["details"])
        compact["history"] = trimmed_history

    for step in compact.get("steps", []):
        if not isinstance(step, dict):
            continue
        status = step.get("status")
        if isinstance(step.get("review_summary"), str):
            step["review_summary"] = clip_text(step["review_summary"], APPROVED_STEP_SUMMARY_CHARS, from_end=True)
        if status in {"approved", "done"}:
            step["implementation"] = []
            step["verification"] = []
            objective = str(step.get("objective", "")).strip()
            if objective:
                step["objective"] = clip_text(objective, RESULTS_SUMMARY_CHARS)
    return compact


def save_plan_manifest(plan_path: Path, manifest: dict[str, Any], plan_text: str) -> None:
    del plan_text
    compact_manifest = compact_manifest_for_storage(manifest)
    validate_manifest(compact_manifest)
    compact_manifest["updated_at"] = utc_now()
    plan_path.write_text(render_plan_document(compact_manifest), encoding="utf-8")


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


def clipped_or_placeholder(text: str, max_chars: int, *, from_end: bool = False) -> str:
    stripped = text.strip()
    if not stripped:
        return "(empty)"
    return clip_text(stripped, max_chars, from_end=from_end)


def format_command_failure(
    message: str,
    result: subprocess.CompletedProcess[str],
    *,
    stdout_chars: int = COMMAND_FAILURE_STDOUT_CHARS,
    stderr_chars: int = COMMAND_FAILURE_STDERR_CHARS,
) -> str:
    stdout_text = clipped_or_placeholder(result.stdout, stdout_chars, from_end=True)
    stderr_text = clipped_or_placeholder(result.stderr, stderr_chars, from_end=True)
    return (
        f"{message}\n"
        f"stdout (clipped):\n{stdout_text}\n\n"
        f"stderr (clipped):\n{stderr_text}"
    )


def write_command_failure_artifacts(
    paths: WorkflowPaths,
    *,
    stage: str,
    result: subprocess.CompletedProcess[str],
    step_id: str | None = None,
) -> tuple[Path, Path]:
    paths.command_artifacts_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    name_parts = [timestamp, stage]
    if step_id:
        name_parts.append(step_id)
    base_name = "_".join(name_parts)
    stdout_path = paths.command_artifacts_dir / f"{base_name}.stdout.txt"
    stderr_path = paths.command_artifacts_dir / f"{base_name}.stderr.txt"
    stdout_path.write_text(result.stdout, encoding="utf-8")
    stderr_path.write_text(result.stderr, encoding="utf-8")
    return stdout_path, stderr_path


def summarize_command_failure(
    paths: WorkflowPaths,
    *,
    stage: str,
    message: str,
    result: subprocess.CompletedProcess[str],
    step_id: str | None = None,
) -> str:
    stdout_path, stderr_path = write_command_failure_artifacts(
        paths,
        stage=stage,
        result=result,
        step_id=step_id,
    )
    formatted = format_command_failure(message, result)
    return (
        f"{formatted}\n\n"
        f"full stdout artifact: {stdout_path}\n"
        f"full stderr artifact: {stderr_path}"
    )


def summarize_workflow_error_for_console(message: str) -> str:
    summary = clip_history_details(message, max_chars=1200)
    artifact_lines: list[str] = []
    for raw_line in message.splitlines():
        line = raw_line.strip()
        if line.startswith("full stdout artifact:") or line.startswith("full stderr artifact:"):
            artifact_lines.append(line)

    if not artifact_lines:
        return summary

    merged_lines = [summary]
    for line in artifact_lines:
        if line not in merged_lines:
            merged_lines.append(line)
    return "\n".join(merged_lines)


def clip_history_details(details: str, *, max_chars: int = MANIFEST_HISTORY_DETAIL_CHARS) -> str:
    stripped = details.strip()
    if not stripped:
        return "(empty)"
    if "Input exceeds the maximum length of 1048576 characters." in stripped:
        return "Executor prompt exceeded the 1,048,576-character input limit."
    if len(stripped) <= max_chars:
        return stripped

    lines = [line.strip() for line in stripped.splitlines() if line.strip()]
    summary_lines: list[str] = []
    if lines:
        summary_lines.append(lines[0])

    error_line = next(
        (line for line in reversed(lines) if "error" in line.lower() or "failed" in line.lower()),
        "",
    )
    if error_line and error_line not in summary_lines:
        summary_lines.extend(["...", error_line])

    if summary_lines:
        summary = "\n".join(summary_lines)
        if len(summary) <= max_chars:
            return summary

    return clip_text(stripped, max_chars, from_end=True)


def compact_manifest_for_prompt(manifest: dict[str, Any]) -> dict[str, Any]:
    compact = copy.deepcopy(manifest)
    history = compact.get("history")
    if isinstance(history, list):
        compact["history"] = history[-MANIFEST_HISTORY_PROMPT_ENTRIES:]
        for entry in compact["history"]:
            if isinstance(entry, dict) and isinstance(entry.get("details"), str):
                entry["details"] = clip_history_details(entry["details"])

    for step in compact.get("steps", []):
        if isinstance(step, dict) and isinstance(step.get("review_summary"), str):
            step["review_summary"] = clip_text(step["review_summary"], 1000, from_end=True)

    return compact


def planner_model_name(config: dict[str, Any]) -> str:
    return (
        os.environ.get("WORKFLOW_PLANNER_MODEL")
        or os.environ.get("WORKFLOW_GEMINI_MODEL")
        or config.get("planner", {}).get("model")
        or "gemini-3.1-pro-preview"
    )


def reviewer_model_name(config: dict[str, Any]) -> str:
    return (
        os.environ.get("WORKFLOW_REVIEWER_MODEL")
        or config.get("reviewer", {}).get("model")
        or os.environ.get("WORKFLOW_PLANNER_MODEL")
        or os.environ.get("WORKFLOW_GEMINI_MODEL")
        or config.get("planner", {}).get("model")
        or "gemini-3.1-pro-preview"
    )


def discussion_model_name(config: dict[str, Any]) -> str:
    return (
        os.environ.get("WORKFLOW_DISCUSSION_MODEL")
        or os.environ.get("WORKFLOW_GEMINI_DISCUSSION_MODEL")
        or config.get("discussion", {}).get("model")
        or planner_model_name(config)
    )


def build_planner_prompt(paths: WorkflowPaths, config: dict[str, Any]) -> str:
    task_text = paths.task_md.read_text(encoding="utf-8")
    discussion_text = paths.discussion_md.read_text(encoding="utf-8")
    existing_plan = clip_text(paths.plan_md.read_text(encoding="utf-8"), PLAN_PROMPT_CHARS, from_end=True)
    progress_text = clip_text(paths.progress_md.read_text(encoding="utf-8"), PROGRESS_PROMPT_CHARS, from_end=True)
    model_hint = planner_model_name(config)
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
- Treat `plan.md` as an operational plan, not an archive.
- Keep completed steps summarized. Do not include long retrospectives, command transcripts, diffs, or raw logs for finished steps.
- Keep the current step and pending future steps concrete and detailed enough to execute.
- If detailed evidence matters, reference `results.md` or workflow artifacts instead of embedding bulk output in the plan.
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
    planner_model = discussion_model_name(config or {})
    summary_line = task_summary.strip() or "No short task summary was provided."

    return f"""You are kicking off the research discussion for a coding workflow.

Your job in this session is to help the user scope the work before planning begins.
This session is for clarification and durable note-taking, not for solving the task end-to-end.
The workflow will save the raw chat transcript and later summarize it into `{paths.discussion_md.name}` automatically.
Do not claim that you edited `{paths.discussion_md.name}` yourself.
Work in a conversational style: clarify the goal, ask targeted follow-up questions, challenge weak assumptions, and help the user converge on a well-scoped approach.

Session requirements:
- Start by restating the current task summary and asking the user what research problem or implementation goal they want to solve.
- Your first substantive reply must contain at least one targeted follow-up question for the user.
- Do not reply with only a promise like "I’ll read/open/update/check this". If you take an action, report the result briefly after the action.
- Do not treat this session as an execution task, implementation task, or autonomous research run. The primary deliverable here is a useful discussion transcript that can be summarized into `{paths.discussion_md.name}` plus clarified open questions and next actions.
- Use the chat to explore goals, constraints, prior attempts, risks, candidate approaches, evaluation criteria, and unknowns.
- Do not generate or rewrite `{paths.plan_md.name}` in this kickoff discussion.
- If the user wants codebase-specific grounding, inspect the repository as needed before making strong claims, but keep that inspection narrowly scoped to informing the discussion.
- Do not start implementing, running benchmarks, editing source code, or producing final conclusions unless the user explicitly asks for that and it is necessary for the discussion.
- If the user shares a link, first clarify what they want extracted from it before expanding into detailed analysis, unless immediate inspection is clearly necessary to answer the user.
- The later planner and progress stages will read `{paths.task_md.name}` and the summarized `{paths.discussion_md.name}` verbatim.
- Never say that a file was updated unless you actually updated it yourself in this session.

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

Operational requirements:
- Success in this session means the user leaves with a clarified scope and the transcript contains the information needed to produce a strong `{paths.discussion_md.name}`.
- If you cannot access an external link or repository from this environment, say that directly and ask the user for the relevant contents or a local path instead of pretending to inspect it.
- Prefer short factual progress updates after actions are completed; avoid placeholder status messages that merely announce intended future work.
- Prefer asking the user targeted questions and recording the answers over independently trying to complete the task during this kickoff stage.

Use {planner_model} level reasoning, but keep the interaction practical and iterative.
Before ending the session, ensure the conversation clearly captures the final discussion summary that should appear in `{paths.discussion_md.name}` after summarization.
"""


def strip_terminal_control_sequences(text: str) -> str:
    ansi_pattern = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1B\\))")
    cleaned = ansi_pattern.sub("", text)
    cleaned = cleaned.replace("\r\n", "\n").replace("\r", "\n")
    cleaned = cleaned.replace("\x07", "").replace("\xa0", " ")
    lines = []
    previous_blank = False
    for raw_line in cleaned.splitlines():
        line = raw_line.strip("\x00")
        if line.startswith("Script started on ") or line.startswith("Script done on "):
            continue
        is_blank = not line.strip()
        if is_blank and previous_blank:
            continue
        lines.append(line.rstrip())
        previous_blank = is_blank
    return "\n".join(lines).strip() + ("\n" if lines else "")


def strip_script_log_markers(text: str) -> str:
    text = re.sub(r"^Script started on .*(?:\n|$)", "", text, count=1, flags=re.MULTILINE)
    text = re.sub(r"(?:\r?\n)?Script done on .*$", "", text, count=1, flags=re.MULTILINE)
    return text


def skip_osc_sequence(text: str, start: int) -> int:
    index = start + 2
    while index < len(text):
        if text[index] == "\x07":
            return index + 1
        if text[index] == "\x1b" and index + 1 < len(text) and text[index + 1] == "\\":
            return index + 2
        index += 1
    return len(text)


def skip_dcs_sequence(text: str, start: int) -> int:
    index = start + 2
    while index < len(text):
        if text[index] == "\x1b" and index + 1 < len(text) and text[index + 1] == "\\":
            return index + 2
        index += 1
    return len(text)


def parse_csi_sequence(text: str, start: int) -> tuple[str, int]:
    index = start + 2
    while index < len(text):
        ch = text[index]
        if "@" <= ch <= "~":
            return text[start + 2 : index + 1], index + 1
        index += 1
    return "", len(text)


def normalize_discussion_text_line(line: str) -> str:
    return re.sub(r"\s+", " ", line.replace("\xa0", " ")).strip()


def clean_discussion_input_log(text: str) -> str:
    text = strip_script_log_markers(text)
    submitted_lines: list[str] = []
    buffer: list[str] = []
    cursor = 0

    def commit_line() -> None:
        nonlocal buffer, cursor
        line = normalize_discussion_text_line("".join(buffer))
        if line:
            submitted_lines.append(line)
        buffer = []
        cursor = 0

    index = 0
    while index < len(text):
        ch = text[index]
        if ch == "\x1b":
            if index + 1 >= len(text):
                break
            marker = text[index + 1]
            if marker == "[":
                sequence, next_index = parse_csi_sequence(text, index)
                index = next_index
                if not sequence:
                    continue
                final = sequence[-1]
                params = sequence[:-1]
                amount = 1
                if params and params.split(";")[0].isdigit():
                    amount = int(params.split(";")[0])
                if final == "C":
                    cursor = min(len(buffer), cursor + amount)
                elif final == "D":
                    cursor = max(0, cursor - amount)
                continue
            if marker == "]":
                index = skip_osc_sequence(text, index)
                continue
            if marker == "P":
                index = skip_dcs_sequence(text, index)
                continue
            index += 2
            continue
        if ch in {"\x08", "\x7f"}:
            if cursor > 0:
                cursor -= 1
                del buffer[cursor]
            index += 1
            continue
        if ch in {"\r", "\n"}:
            commit_line()
            index += 1
            continue
        if ord(ch) < 32:
            index += 1
            continue
        if ch == "\t":
            ch = " "
        if cursor == len(buffer):
            buffer.append(ch)
        else:
            buffer.insert(cursor, ch)
        cursor += 1
        index += 1

    commit_line()
    return "\n".join(submitted_lines).strip() + ("\n" if submitted_lines else "")


def extract_user_turns_from_input_log(text: str) -> list[str]:
    cleaned = clean_discussion_input_log(text)
    if not cleaned:
        return []
    return [line.strip() for line in cleaned.splitlines() if line.strip()]


def normalize_assistant_message_lines(lines: list[str]) -> str:
    if not lines:
        return ""
    normalized: list[str] = []
    previous_blank = False
    for raw_line in lines:
        line = re.sub(r"\s+", " ", raw_line.strip())
        if not line:
            if normalized and not previous_blank:
                normalized.append("")
            previous_blank = True
            continue
        starts_new_block = bool(re.match(r"^(-|\*|•|\d+\.)\s", line))
        if normalized and normalized[-1] and not previous_blank and not starts_new_block:
            normalized[-1] = f"{normalized[-1]} {line}"
        else:
            normalized.append(line)
        previous_blank = False
    return "\n".join(part for part in normalized if part is not None).strip()


def clean_discussion_output_log(text: str) -> str:
    text = strip_script_log_markers(text)
    text = re.sub(r"\x1b\[(\d*)C", lambda match: " " * int(match.group(1) or "1"), text)
    return strip_terminal_control_sequences(text)


def is_discussion_status_line(line: str) -> bool:
    stripped = line.strip()
    compact = re.sub(r"\s+", "", stripped).lower()
    if not compact:
        return False
    if stripped[0] in {"✢", "✶", "✻", "✽", "·", "*"} and ("…" in stripped or "..." in stripped or "tokens" in stripped):
        return True
    if stripped.startswith("⎿ "):
        return True
    if stripped.startswith("Tip: "):
        return True
    if stripped in {"Press Ctrl-C again to exit", "PressCtrl-C again to exit", "Resume this session with:"}:
        return True
    if compact.startswith("claude--resume"):
        return True
    if compact.startswith("0;") or compact.startswith("9;"):
        return True
    return False


def is_fragmented_discussion_noise(line: str) -> bool:
    stripped = line.strip()
    if not stripped:
        return False
    if stripped in {"~", "=", ","}:
        return True
    if stripped.startswith("+ "):
        return True
    if stripped in {"Checking for updates", "Tip: ctrl+s to stash"}:
        return True
    if len(stripped) == 1 and stripped.isalnum():
        return True
    if len(stripped.split()) == 1 and len(stripped) < 12 and stripped[-1] not in ".!?:":
        return True
    tokens = stripped.split()
    short_tokens = sum(1 for token in tokens if len(token) <= 2)
    if len(tokens) >= 4 and short_tokens * 5 >= len(tokens) * 4:
        return True
    if re.fullmatch(r"[\d\s().]+", stripped):
        return True
    return False


def compact_discussion_line(line: str) -> str:
    return re.sub(r"\s+", "", line).lower()


def prefer_discussion_line(existing: str, candidate: str) -> str:
    existing_score = (existing.count(" "), sum(ch.isalpha() for ch in existing), len(existing))
    candidate_score = (candidate.count(" "), sum(ch.isalpha() for ch in candidate), len(candidate))
    return candidate if candidate_score > existing_score else existing


def dedupe_assistant_message_lines(lines: list[str]) -> list[str]:
    deduped: list[str] = []
    for raw_line in lines:
        line = normalize_discussion_text_line(raw_line)
        if not line:
            if deduped and deduped[-1] != "":
                deduped.append("")
            continue
        compact = compact_discussion_line(line)
        replaced = False
        for index in range(max(0, len(deduped) - 3), len(deduped)):
            existing = deduped[index]
            if not existing:
                continue
            existing_compact = compact_discussion_line(existing)
            if compact == existing_compact:
                deduped[index] = prefer_discussion_line(existing, line)
                replaced = True
                break
            if compact and existing_compact and compact in existing_compact:
                deduped[index] = prefer_discussion_line(existing, line)
                replaced = True
                break
            if compact and existing_compact and existing_compact in compact:
                deduped[index] = prefer_discussion_line(existing, line)
                replaced = True
                break
        if not replaced:
            deduped.append(line)
    return deduped


def is_plausible_assistant_content_line(line: str) -> bool:
    stripped = normalize_discussion_text_line(line)
    if not stripped:
        return False
    if is_discussion_status_line(stripped) or is_fragmented_discussion_noise(stripped):
        return False
    if re.match(r"^(-|\*|•|\d+\.)\s", stripped):
        return True
    if stripped.endswith((".", "?", "!", ":")):
        return True
    words = re.findall(r"[A-Za-z0-9_/~.-]+", stripped)
    long_words = sum(1 for word in words if len(word) >= 3)
    return len(words) >= 4 and long_words >= max(2, len(words) // 2)


def is_substantive_assistant_turn(message: str) -> bool:
    text = message.strip()
    if not text:
        return False
    if "\n- " in text or "\n1. " in text:
        return True
    words = re.findall(r"[A-Za-z]{3,}", text)
    return len(words) >= 12


def discussion_word_set(text: str) -> set[str]:
    return {word.lower() for word in re.findall(r"[A-Za-z]{3,}", text)}


def reflected_user_overlap_ratio(text: str, user_turn: str) -> float:
    words = discussion_word_set(text)
    if not words:
        return 0.0
    overlap = words & discussion_word_set(user_turn)
    return len(overlap) / len(words)


def sanitize_assistant_turn_against_user_turns(message: str, user_turns: list[str]) -> str:
    lines = [line for line in message.splitlines()]
    sanitized_lines: list[str] = []
    for line in lines:
        stripped = line.strip()
        if not stripped:
            if sanitized_lines and sanitized_lines[-1] != "":
                sanitized_lines.append("")
            continue
        if re.match(r"^(-|\*|•|\d+\.)\s", stripped) or stripped.endswith("?"):
            sanitized_lines.append(stripped)
            continue
        overlap = max((reflected_user_overlap_ratio(stripped, user_turn) for user_turn in user_turns), default=0.0)
        if overlap >= 0.85:
            continue
        sanitized_lines.append(stripped)
    sanitized_message = normalize_assistant_message_lines(sanitized_lines)
    if not sanitized_message:
        return ""
    if "?" not in sanitized_message and "\n- " not in sanitized_message:
        overlap = max((reflected_user_overlap_ratio(sanitized_message, user_turn) for user_turn in user_turns), default=0.0)
        if overlap >= 0.85:
            return ""
    return sanitized_message


def extract_assistant_turns_from_output_log(text: str) -> list[str]:
    cleaned = clean_discussion_output_log(text)
    lines: list[str] = []
    noise_substrings = (
        "claudecode",
        "tipsforgettingstarted",
        "welcomeback",
        "recentactivity",
        "norecentactivity",
        "?forshortcuts",
        "esctointerrupt",
        "apiusagebilling",
        "roosting",
        "schlepping",
        "/effort",
    )
    for raw_line in cleaned.splitlines():
        line = raw_line.rstrip()
        compact = re.sub(r"\s+", "", line).lower()
        if not compact:
            lines.append("")
            continue
        if compact.startswith("scriptstartedon") or compact.startswith("scriptdoneon"):
            continue
        if any(token in compact for token in noise_substrings):
            continue
        if all(ch in "-─╭╮╰╯│└┘┌┐┆┊━═ " for ch in line):
            continue
        if compact in {"❯", "●", "✢", "*", "✶", "✻", "✽", "·"}:
            continue
        if compact.startswith("0;"):
            continue
        lines.append(line)

    first_assistant_index = next((i for i, line in enumerate(lines) if line.lstrip().startswith("● ")), None)
    if first_assistant_index is None:
        return []

    assistant_turns: list[str] = []
    current_lines: list[str] = []
    for raw_line in lines[first_assistant_index:]:
        stripped = raw_line.strip()
        if not stripped:
            if current_lines and current_lines[-1] != "":
                current_lines.append("")
            continue
        if stripped.startswith("● "):
            if current_lines:
                message = normalize_assistant_message_lines(dedupe_assistant_message_lines(current_lines))
                if message and is_substantive_assistant_turn(message):
                    assistant_turns.append(message)
            current_lines = [stripped[2:].strip()]
            continue
        if stripped.startswith("❯"):
            continue
        if is_discussion_status_line(stripped):
            if current_lines:
                message = normalize_assistant_message_lines(dedupe_assistant_message_lines(current_lines))
                if message and is_substantive_assistant_turn(message):
                    assistant_turns.append(message)
                current_lines = []
            continue
        if not is_plausible_assistant_content_line(stripped):
            continue
        current_lines.append(stripped)
    if current_lines:
        message = normalize_assistant_message_lines(dedupe_assistant_message_lines(current_lines))
        if message and is_substantive_assistant_turn(message):
            assistant_turns.append(message)
    return assistant_turns


def build_discussion_transcript(paths: WorkflowPaths) -> str:
    user_turns: list[str] = []
    if paths.discussion_input_log.exists():
        user_turns = extract_user_turns_from_input_log(paths.discussion_input_log.read_text(encoding="utf-8"))
    assistant_turns: list[str] = []
    if paths.discussion_output_log.exists():
        assistant_turns = extract_assistant_turns_from_output_log(paths.discussion_output_log.read_text(encoding="utf-8"))
    if assistant_turns:
        assistant_turns = [
            sanitized
            for sanitized in (
                sanitize_assistant_turn_against_user_turns(turn, user_turns) for turn in assistant_turns
            )
            if sanitized and is_substantive_assistant_turn(sanitized)
        ]

    sections = ["# Discussion Transcript", ""]
    assistant_index = 0
    user_index = 0
    turn_number = 1

    if assistant_turns:
        sections.extend(
            [
                "## Assistant Opening",
                "",
                "Assistant:",
                "",
                assistant_turns[0],
                "",
            ]
        )
        assistant_index = 1

    while user_index < len(user_turns) or assistant_index < len(assistant_turns):
        if user_index < len(user_turns):
            sections.extend(
                [
                    f"## User Turn {turn_number}",
                    "",
                    "User:",
                    "",
                    user_turns[user_index],
                    "",
                ]
            )
            user_index += 1
        if assistant_index < len(assistant_turns):
            sections.extend(
                [
                    f"## Assistant Reply {turn_number}",
                    "",
                    "Assistant:",
                    "",
                    assistant_turns[assistant_index],
                    "",
                ]
            )
            assistant_index += 1
        turn_number += 1

    return "\n".join(sections).rstrip() + "\n"


def build_discussion_summary_prompt(paths: WorkflowPaths, config: dict[str, Any]) -> str:
    task_text = paths.task_md.read_text(encoding="utf-8")
    existing_discussion = paths.discussion_md.read_text(encoding="utf-8")
    transcript_raw = paths.discussion_transcript.read_text(encoding="utf-8")
    transcript_text = clip_text(
        strip_terminal_control_sequences(transcript_raw),
        DISCUSSION_TRANSCRIPT_PROMPT_CHARS,
        from_end=True,
    )
    model_hint = discussion_model_name(config)

    return f"""You are summarizing a kickoff discussion for a coding workflow.

Rewrite `{paths.discussion_md.name}` as the durable structured summary for later workflow stages.
Use the transcript as the ground truth. Preserve concrete user decisions, constraints, links, and open questions.
If the transcript contains assistant claims about edits or actions that were not actually performed, ignore those claims and summarize only the substantive discussion content.

Requirements:
- Return the full contents of `{paths.discussion_md.name}` only. No surrounding explanation.
- Organize the file with these sections in order:
  1. `# Discussion`
  2. `## Task Summary`
  3. `## Problem Statement`
  4. `## Constraints`
  5. `## Current Understanding`
  6. `## Promising Directions`
  7. `## Rejected Ideas`
  8. `## Open Questions`
  9. `## Next Actions`
- Keep the summary concise but specific.
- Prefer flat bullet lists under the section headings when listing multiple items.
- Include related links or references only if they were discussed or are already present in the task file.
- Do not invent facts that are not supported by the transcript or task file.
- If the transcript is ambiguous, capture that ambiguity as an open question instead of guessing.

Model hint: use {model_hint} level reasoning for synthesis, but keep the output practical and compact.

Task file:
```markdown
{task_text.strip()}
```

Existing discussion file:
```markdown
{existing_discussion.strip()}
```

Discussion transcript:
```text
{transcript_text.strip()}
```
"""


def build_codex_prompt(
    paths: WorkflowPaths,
    manifest: dict[str, Any],
    step: dict[str, Any],
) -> str:
    verification_lines = "\n".join(f"- {item}" for item in step.get("verification", [])) or "- None listed"
    implementation_lines = "\n".join(f"- {item}" for item in step.get("implementation", [])) or "- No implementation notes provided"
    progress_text = clip_text(paths.progress_md.read_text(encoding="utf-8"), PROGRESS_PROMPT_CHARS, from_end=True)
    manifest_text = yaml.safe_dump(
        compact_manifest_for_prompt(manifest),
        sort_keys=False,
        allow_unicode=False,
    ).strip()
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
{manifest_text}
```
"""


def build_review_prompt(paths: WorkflowPaths, step: dict[str, Any]) -> str:
    manifest, _ = load_plan_manifest(paths.plan_md)
    plan_text = yaml.safe_dump(compact_manifest_for_prompt(manifest), sort_keys=False, allow_unicode=False).strip()
    results_text = clip_text(paths.results_md.read_text(encoding="utf-8"), RESULTS_PROMPT_CHARS, from_end=True)
    progress_text = clip_text(paths.progress_md.read_text(encoding="utf-8"), PROGRESS_PROMPT_CHARS, from_end=True)
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
- Keep {paths.progress_md.name} compact. It is a handoff note, not an archive.
- Include only current status, completed steps, open issues, decisive evidence, and next action.
- Never copy full logs, prompts, manifests, or long command output into {paths.progress_md.name}.
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
            f"- Step `{active_step['id']}` ({active_step['title']}) completed implementation and is awaiting review."
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
            latest_review_lines = ["- No review has been recorded yet for the current workflow state."]
        else:
            approved = review_event.get("event") == "approved"
            latest_review_lines = [
                f"- **Step:** `{review_event.get('step_id', 'unknown')}`",
                f"- **Approved:** `{str(approved).lower()}`",
                f"- **Rationale:** {clip_history_details(review_event.get('details', 'No review summary recorded.'))}",
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
                open_issues.append(f"- {clip_history_details(details)}")
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
    template = (
        os.environ.get("WORKFLOW_PLANNER_CMD")
        or os.environ.get("WORKFLOW_GEMINI_CMD")
        or planner.get("command_template")
    )
    if not template:
        raise WorkflowError(
            "Planner command template is not configured. "
            "Set planner.command_template in the config file or WORKFLOW_PLANNER_CMD."
        )
    return template


def discussion_command_config(config: dict[str, Any]) -> str:
    discussion = config.get("discussion", {})
    template = (
        os.environ.get("WORKFLOW_DISCUSSION_CMD")
        or os.environ.get("WORKFLOW_GEMINI_DISCUSSION_CMD")
        or discussion.get("command_template")
    )
    if not template:
        raise WorkflowError(
            "Discussion command template is not configured. "
            "Set discussion.command_template in the config file or WORKFLOW_DISCUSSION_CMD."
        )
    return template


def reviewer_command_config(config: dict[str, Any]) -> str:
    reviewer = config.get("reviewer", {})
    template = (
        os.environ.get("WORKFLOW_REVIEWER_CMD")
        or reviewer.get("command_template")
        or planner_command_config(config)
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
        model=discussion_model_name(config),
    )
    result = run_interactive_command(command, cwd=paths.repo_root)
    if result.returncode != 0:
        raise WorkflowError(f"Discussion command failed with exit code {result.returncode}.")

    update_state_timestamp(paths.state_json, "last_discussion_launch_at")
    if not paths.discussion_output_log.exists():
        raise WorkflowError(
            f"Discussion output log was not captured at {paths.discussion_output_log}. "
            "The discussion launcher must save the interactive session logs before summarization."
        )

    transcript_text = build_discussion_transcript(paths)
    paths.discussion_transcript.write_text(transcript_text, encoding="utf-8")
    transcript_text = transcript_text.strip()
    if not transcript_text:
        raise WorkflowError(
            f"Discussion transcript at {paths.discussion_transcript} was empty after cleanup."
        )

    summary_prompt = build_discussion_summary_prompt(paths, config)
    summary_prompt_path = paths.prompts_dir / "discussion_summary_prompt.txt"
    write_prompt_file(summary_prompt_path, summary_prompt)
    summary_command = parse_command_template(
        planner_command_config(config),
        prompt_file=str(summary_prompt_path),
        workspace=str(paths.root),
        repo_root=str(paths.repo_root),
        plan=str(paths.plan_md),
        results=str(paths.results_md),
        progress=str(paths.progress_md),
        task=str(paths.task_md),
        discussion=str(paths.discussion_md),
        model=discussion_model_name(config),
    )
    summary_result = run_external_command(summary_command, cwd=paths.root)
    if summary_result.returncode != 0:
        raise WorkflowError(
            summarize_command_failure(
                paths,
                stage="discussion_summary",
                message="Discussion summary command failed.",
                result=summary_result,
            )
        )

    summary_text = summary_result.stdout.strip()
    if not summary_text:
        raise WorkflowError("Discussion summary command returned empty output.")
    paths.discussion_md.write_text(summary_text.rstrip() + "\n", encoding="utf-8")
    after_text = paths.discussion_md.read_text(encoding="utf-8")
    return after_text != before_text


def run_progress_update(paths: WorkflowPaths, config: dict[str, Any], step: dict[str, Any], review: StepResult) -> None:
    del config
    write_progress_snapshot(
        paths,
        build_manifest_progress(paths, latest_step=step, review=review),
    )


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
        model=planner_model_name(config),
    )
    result = run_external_command(command, cwd=paths.root)
    if result.returncode != 0:
        raise WorkflowError(
            summarize_command_failure(
                paths,
                stage="planner",
                message="Planner command failed.",
                result=result,
            )
        )

    planner_output = result.stdout.strip()
    if not planner_output:
        raise WorkflowError("Planner command returned empty output.")

    manifest_text, _, _ = extract_manifest_block(planner_output)
    manifest = yaml.safe_load(manifest_text) or {}
    if not isinstance(manifest, dict):
        raise WorkflowError("Planner output did not contain a valid manifest mapping.")
    validate_manifest(manifest)
    if not manifest["steps"]:
        raise WorkflowError("Planner did not populate any steps in the manifest.")
    save_plan_manifest(paths.plan_md, manifest, planner_output)

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
        failure_summary = summarize_command_failure(
            paths,
            stage="executor",
            message="Executor command failed.",
            result=result,
            step_id=step["id"],
        )
        mark_step_status(
            paths.plan_md,
            step["id"],
            "needs_changes",
            event="executor_failed",
            details=clip_history_details(failure_summary),
        )
        write_progress_snapshot(
            paths,
            build_manifest_progress(paths, latest_step=step),
        )
        raise WorkflowError(failure_summary)

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
        reviewer_command_config(config),
        prompt_file=str(prompt_path),
        workspace=str(paths.root),
        repo_root=str(paths.repo_root),
        plan=str(paths.plan_md),
        results=str(paths.results_md),
        progress=str(paths.progress_md),
        task=str(paths.task_md),
        discussion=str(paths.discussion_md),
        step_id=step["id"],
        model=reviewer_model_name(config),
    )
    result = run_external_command(command, cwd=paths.root)
    if result.returncode != 0:
        raise WorkflowError(
            summarize_command_failure(
                paths,
                stage="reviewer",
                message="Reviewer command failed.",
                result=result,
                step_id=step["id"],
            )
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
        f"Review - {step['id']}",
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
    parser = argparse.ArgumentParser(description="Planner / Codex executor workflow runner.")
    parser.add_argument(
        "--workspace",
        default="workflow_runs/default",
        help="Directory containing task.md, discussion.md, plan.md, and results.md.",
    )
    parser.add_argument(
        "--config",
        default="workflow/config.gemini.example.yaml",
        help="Workflow config file with planner and executor command templates.",
    )

    subparsers = parser.add_subparsers(dest="command", required=True)

    init_parser = subparsers.add_parser("init", help="Create workflow files in the workspace.")
    init_parser.add_argument("--task-summary", default="", help="Short task summary for the initial manifest.")
    init_parser.add_argument(
        "--related-link",
        action="append",
        default=[],
        help="Related GitHub, arXiv, or file link to record in task.md. Repeat for multiple links.",
    )
    init_parser.add_argument(
        "--model",
        default="",
        help="Default model to persist for both planner and reviewer in this workspace.",
    )
    init_parser.add_argument(
        "--planner-model",
        default="",
        help="Planner model to persist for this workspace. Overrides --model for planning.",
    )
    init_parser.add_argument(
        "--reviewer-model",
        default="",
        help="Reviewer model to persist for this workspace. Overrides --model for review.",
    )
    init_parser.add_argument(
        "--discussion-model",
        default="",
        help="Discussion model to persist for this workspace. Overrides --model for kickoff discussion.",
    )
    init_parser.add_argument(
        "--no-discussion",
        action="store_true",
        help="Only initialize the workspace files; do not launch the interactive kickoff discussion.",
    )

    subparsers.add_parser("plan", help="Generate or refresh plan.md using the configured planner.")

    run_step_parser = subparsers.add_parser("run-step", help="Run Codex for the current or specified step.")
    run_step_parser.add_argument("--step-id", default=None, help="Explicit step id to execute.")

    review_parser = subparsers.add_parser("review", help="Run review for the current or specified step.")
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
    load_runtime_env_file(paths.root / "runtime.env")
    config = load_yaml_file(Path(args.config).resolve())

    try:
        if args.command == "init":
            task_md_already_exists = paths.task_md.exists()
            related_links = normalize_related_links(args.related_link)
            if not task_md_already_exists and not related_links and sys.stdin.isatty():
                related_links = prompt_for_related_links()
            ensure_workflow_files(paths, task_summary=args.task_summary, related_links=related_links)
            planner_model = args.planner_model.strip() or args.model.strip()
            reviewer_model = args.reviewer_model.strip() or args.model.strip()
            discussion_model = args.discussion_model.strip() or args.model.strip()
            runtime_model_overrides = {
                "WORKFLOW_PLANNER_MODEL": planner_model,
                "WORKFLOW_REVIEWER_MODEL": reviewer_model,
                "WORKFLOW_DISCUSSION_MODEL": discussion_model,
            }
            if any(runtime_model_overrides.values()):
                upsert_runtime_env_file(
                    paths.root / "runtime.env",
                    runtime_model_overrides,
                )
                apply_runtime_env_overrides(runtime_model_overrides)
            print(f"Initialized workflow workspace at {paths.root}")
            if any(runtime_model_overrides.values()):
                print(f"Saved workspace model overrides in {paths.root / 'runtime.env'}")
            if task_md_already_exists:
                print(f"Kept existing {paths.task_md}; related links prompt was skipped.")
            if args.no_discussion:
                return 0

            if not sys.stdin.isatty() or not sys.stdout.isatty():
                print("Interactive discussion skipped because stdin/stdout is not a TTY.")
                return 0

            print(
                f"Launching kickoff discussion. Keep {paths.discussion_md.name} updated before you exit the chat."
            )
            discussion_changed = run_discussion_session(paths, config, args.task_summary)
            if discussion_changed:
                print(f"Updated {paths.discussion_md}")
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
        print(
            f"Workflow error: {summarize_workflow_error_for_console(str(exc))}",
            file=sys.stderr,
        )
        return 1
    except KeyboardInterrupt:
        print("Workflow interrupted.", file=sys.stderr)
        return 130

    raise AssertionError(f"Unhandled command: {args.command}")


if __name__ == "__main__":
    raise SystemExit(main())
