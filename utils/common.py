from __future__ import annotations

import dataclasses
import json
import os
import shlex
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml


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

SUMMARY_TEMPLATE = """# Workflow Summary

Summary is generated automatically when the workflow finishes successfully or stops in a
terminal blocked state that requires human intervention.
"""


@dataclasses.dataclass
class WorkflowPaths:
    root: Path

    @property
    def repo_root(self) -> Path:
        configured = os.environ.get("WORKFLOW_REPO_ROOT")
        if configured:
            return Path(configured).expanduser().resolve()
        return Path(__file__).resolve().parents[2]

    @property
    def workflow_root(self) -> Path:
        return Path(__file__).resolve().parents[1]

    @property
    def task_md(self) -> Path:
        return self.root / "task.md"

    @property
    def discussion_md(self) -> Path:
        return self.root / "discussion.md"

    @property
    def lesson_candidates_yaml(self) -> Path:
        return self.root / "lesson_candidates.yaml"

    @property
    def plan_md(self) -> Path:
        return self.root / "plan.md"

    @property
    def results_md(self) -> Path:
        return self.root / "results.md"

    @property
    def migration_md(self) -> Path:
        return self.root / "migration.md"

    @property
    def progress_md(self) -> Path:
        return self.root / "progress.md"

    @property
    def summary_md(self) -> Path:
        return self.root / "summary.md"

    @property
    def state_json(self) -> Path:
        return self.root / "state.json"

    @property
    def artifact_index_json(self) -> Path:
        return self.root / "artifact_index.json"

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

    @property
    def global_lessons_yaml(self) -> Path:
        return self.workflow_root / "memory" / "lessons.yaml"


@dataclasses.dataclass
class StepResult:
    approved: bool
    summary: str
    required_changes: list[str]
    raw_output: str
    outcome_status: str = "pass"
    outcome_reason: str = ""
    human_intervention_required: bool = False
    human_intervention_reason: str = ""
    acceptance_results: list[dict[str, str]] = dataclasses.field(default_factory=list)
    verification_results: list[dict[str, str]] = dataclasses.field(default_factory=list)
    lesson_candidates: list[dict[str, Any]] = dataclasses.field(default_factory=list)


SUMMARY_STATUS_DONE = "done"
SUMMARY_STATUS_BLOCKED = "blocked"
SUMMARY_STATUS_FAILED = "failed"
SUMMARY_STATUS_INTERRUPTED = "interrupted"
VALID_SUMMARY_STATUSES = {
    SUMMARY_STATUS_DONE,
    SUMMARY_STATUS_BLOCKED,
    SUMMARY_STATUS_FAILED,
    SUMMARY_STATUS_INTERRUPTED,
}


class WorkflowError(RuntimeError):
    """Raised for workflow-specific failures."""


def load_artifact_index(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"artifacts": []}
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise WorkflowError(f"Artifact index must be a JSON object: {path}")
    artifacts = data.get("artifacts", [])
    if not isinstance(artifacts, list):
        raise WorkflowError(f"Artifact index 'artifacts' field must be a list: {path}")
    return data


def save_artifact_index(path: Path, data: dict[str, Any]) -> None:
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


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
        "workflow_root": str(paths.root),
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


def read_optional_text(path: Path) -> str:
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8")


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
