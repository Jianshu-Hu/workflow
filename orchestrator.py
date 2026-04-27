from __future__ import annotations

import argparse
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

from utils.common import (
    PROGRESS_TEMPLATE,
    RESULTS_HEADER,
    SUMMARY_STATUS_BLOCKED,
    SUMMARY_STATUS_DONE,
    SUMMARY_STATUS_FAILED,
    SUMMARY_STATUS_INTERRUPTED,
    SUMMARY_TEMPLATE,
    StepResult,
    WorkflowError,
    WorkflowPaths,
    apply_runtime_env_overrides,
    clip_text,
    clipped_or_placeholder,
    config_int,
    load_runtime_env_file,
    load_state,
    load_artifact_index,
    load_yaml_file,
    normalize_related_links,
    parse_bool,
    prompt_for_related_links,
    render_discussion_template,
    render_task_template,
    runtime_context,
    save_state,
    save_artifact_index,
    update_state_timestamp,
    upsert_runtime_env_file,
    utc_now,
    write_prompt_file,
)
from utils.discussion import (
    build_discussion_summary_prompt,
    build_discussion_transcript,
    is_valid_discussion_summary,
)
from utils.manifest import (
    append_history_event,
    approve_step,
    build_fallback_progress,
    build_manifest_progress,
    build_workflow_summary,
    clip_history_details,
    compact_manifest_for_prompt,
    create_default_manifest,
    extract_manifest_block,
    get_active_step,
    get_step,
    load_plan_manifest,
    mark_step_status,
    normalize_manifest,
    render_plan_document,
    save_plan_manifest,
    validate_manifest,
)
from utils.migration import run_migration

COMMAND_FAILURE_STDOUT_CHARS = 4000
COMMAND_FAILURE_STDERR_CHARS = 12000
PLAN_PROMPT_CHARS = 32000
PROGRESS_PROMPT_CHARS = 12000
RESULTS_PROMPT_CHARS = 24000
MIGRATION_PROMPT_CHARS = 20000
EXECUTOR_INCOMPLETE_STDOUT_CHARS = 8000
EXECUTOR_INCOMPLETE_STDERR_CHARS = 16000
EXECUTOR_EVIDENCE_REPORT_CHARS = 12000
EXECUTOR_REQUIRED_EVIDENCE_HEADINGS = (
    "Acceptance Evidence",
    "Verification Evidence",
    "Changed Files",
    "Outcome",
)
EVIDENCE_MATCH_STOPWORDS = {
    "about",
    "above",
    "after",
    "again",
    "against",
    "all",
    "also",
    "before",
    "being",
    "check",
    "completion",
    "condition",
    "confirms",
    "concrete",
    "criteria",
    "criterion",
    "done",
    "every",
    "from",
    "have",
    "into",
    "must",
    "outcome",
    "pass",
    "passed",
    "require",
    "required",
    "requires",
    "result",
    "results",
    "run",
    "runs",
    "script",
    "should",
    "shows",
    "step",
    "that",
    "the",
    "this",
    "through",
    "updated",
    "verification",
    "with",
    "without",
}
INCOMPLETE_EVIDENCE_PATTERNS = (
    r"\bstill\s+running\b",
    r"\bin\s+progress\b",
    r"\bnot\s+run\b",
    r"\bnot\s+tested\b",
    r"\buntested\b",
    r"\bskipped\b",
    r"\bto\s+be\s+verified\b",
    r"\bneeds?\s+verification\b",
)
LESSON_CONTEXT_CHARS = 24000
MAX_RELEVANT_LESSONS = 5
LESSON_REJECTED_MIN = -5
LESSON_REJECTED_MAX = -1
LESSON_ACTIVE_MIN = 1
LESSON_ACTIVE_MAX = 10


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
    if not paths.summary_md.exists():
        paths.summary_md.write_text(SUMMARY_TEMPLATE + "\n", encoding="utf-8")

    if not paths.artifact_index_json.exists():
        save_artifact_index(
            paths.artifact_index_json,
            {
                "updated_at": utc_now(),
                "artifacts": [],
            },
        )

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

    index_existing_workspace_artifacts(paths)


def refresh_placeholder_workspace(
    paths: WorkflowPaths,
    *,
    task_summary: str,
    related_links: list[str] | None = None,
) -> bool:
    summary = task_summary.strip()
    if not summary:
        return False

    changed = False
    default_task_template = render_task_template()
    desired_task_template = render_task_template(summary, related_links=related_links)
    if paths.task_md.exists() and paths.task_md.read_text(encoding="utf-8") == default_task_template:
        paths.task_md.write_text(desired_task_template, encoding="utf-8")
        changed = True

    default_discussion_template = render_discussion_template()
    desired_discussion_template = render_discussion_template(summary)
    if paths.discussion_md.exists() and paths.discussion_md.read_text(encoding="utf-8") == default_discussion_template:
        paths.discussion_md.write_text(desired_discussion_template, encoding="utf-8")
        changed = True

    if paths.plan_md.exists():
        manifest, plan_text = load_plan_manifest(paths.plan_md)
        if not str(manifest.get("task", "")).strip() and not manifest.get("steps"):
            manifest["task"] = summary
            save_plan_manifest(paths.plan_md, manifest, plan_text)
            changed = True

    return changed


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


def count_results_sections(results_text: str, heading: str) -> int:
    pattern = re.compile(rf"^## {re.escape(heading)}\s*$", re.MULTILINE)
    return len(pattern.findall(results_text))


def latest_results_section(results_text: str, heading: str) -> str | None:
    pattern = re.compile(
        rf"^## {re.escape(heading)}\s*$\n?(?P<body>.*?)(?=^## |\Z)",
        re.MULTILINE | re.DOTALL,
    )
    matches = list(pattern.finditer(results_text))
    if not matches:
        return None
    return matches[-1].group("body").strip()


def append_results_section_with_index(
    paths: WorkflowPaths,
    heading: str,
    body: str,
    *,
    step_id: str | None = None,
) -> None:
    append_results_section(paths.results_md, heading, body)
    append_artifact_record(
        paths,
        category="results_section",
        path=paths.results_md,
        label=heading,
        step_id=step_id,
        metadata={"heading": heading},
    )


def append_artifact_record(
    paths: WorkflowPaths,
    *,
    category: str,
    path: Path,
    label: str,
    step_id: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> None:
    index = load_artifact_index(paths.artifact_index_json)
    artifacts = index.setdefault("artifacts", [])
    artifacts.append(
        {
            "timestamp": utc_now(),
            "category": category,
            "label": label,
            "path": str(path.resolve()),
            "step_id": step_id,
            "metadata": metadata or {},
        }
    )
    index["artifacts"] = artifacts[-200:]
    index["updated_at"] = utc_now()
    save_artifact_index(paths.artifact_index_json, index)


def artifact_index_summary(paths: WorkflowPaths, *, max_entries: int = 12) -> str:
    index = load_artifact_index(paths.artifact_index_json)
    entries = index.get("artifacts", [])
    if not entries:
        return "No indexed artifacts yet."

    lines: list[str] = []
    for entry in entries[-max_entries:]:
        if not isinstance(entry, dict):
            continue
        label = str(entry.get("label", "artifact"))
        category = str(entry.get("category", "unknown"))
        path = str(entry.get("path", ""))
        step_id = str(entry.get("step_id", "") or "")
        prefix = f"- [{category}] {label}"
        if step_id:
            prefix += f" (step `{step_id}`)"
        if path:
            prefix += f": {path}"
        lines.append(prefix)

    return "\n".join(lines) if lines else "No indexed artifacts yet."


def load_workflow_lessons(paths: WorkflowPaths) -> list[dict[str, Any]]:
    if not paths.global_lessons_yaml.exists():
        return []
    data = yaml.safe_load(paths.global_lessons_yaml.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise WorkflowError(f"Global lessons file must contain a YAML mapping: {paths.global_lessons_yaml}")
    lessons = data.get("lessons", [])
    if lessons is None:
        return []
    if not isinstance(lessons, list):
        raise WorkflowError(f"Global lessons file 'lessons' field must be a list: {paths.global_lessons_yaml}")
    valid_lessons: list[dict[str, Any]] = []
    for index, lesson in enumerate(lessons, start=1):
        if not isinstance(lesson, dict):
            raise WorkflowError(f"Global lesson {index} must be a mapping.")
        confidence = lesson.get("confidence", 0)
        if not isinstance(confidence, int) or confidence < LESSON_REJECTED_MIN or confidence > LESSON_ACTIVE_MAX:
            raise WorkflowError(
                f"Global lesson {lesson.get('id', index)!r} confidence must be an integer from -5 to 10."
            )
        valid_lessons.append(lesson)
    return valid_lessons


def lesson_text_values(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        values: list[str] = []
        for item in value:
            values.extend(lesson_text_values(item))
        return values
    if isinstance(value, dict):
        values = []
        for item in value.values():
            values.extend(lesson_text_values(item))
        return values
    return []


def lesson_trigger_terms(lesson: dict[str, Any]) -> list[str]:
    terms: list[str] = []
    for key in ("trigger_terms", "domains", "applies_when"):
        terms.extend(lesson_text_values(lesson.get(key, [])))
    return [term.strip().lower() for term in terms if term.strip()]


def lesson_relevance_score(lesson: dict[str, Any], context_text: str) -> int:
    confidence = lesson.get("confidence", 0)
    if not isinstance(confidence, int) or confidence <= 0:
        return 0
    lowered_context = context_text.lower()
    terms = lesson_trigger_terms(lesson)
    score = sum(2 for term in terms if term and term in lowered_context)
    lesson_id = str(lesson.get("id", "")).strip().lower()
    title = str(lesson.get("title", "")).strip().lower()
    if lesson_id and lesson_id in lowered_context:
        score += 3
    if title and title in lowered_context:
        score += 2
    return score


def lesson_context_for_planner(paths: WorkflowPaths) -> str:
    parts = [
        paths.task_md.read_text(encoding="utf-8") if paths.task_md.exists() else "",
        paths.discussion_md.read_text(encoding="utf-8") if paths.discussion_md.exists() else "",
        paths.migration_md.read_text(encoding="utf-8") if paths.migration_md.exists() else "",
        paths.progress_md.read_text(encoding="utf-8") if paths.progress_md.exists() else "",
    ]
    return clip_text("\n\n".join(parts), LESSON_CONTEXT_CHARS, from_end=True)


def lesson_context_for_step(paths: WorkflowPaths, step: dict[str, Any]) -> str:
    step_text = yaml.safe_dump(step, sort_keys=False, allow_unicode=False)
    parts = [
        paths.task_md.read_text(encoding="utf-8") if paths.task_md.exists() else "",
        paths.discussion_md.read_text(encoding="utf-8") if paths.discussion_md.exists() else "",
        paths.progress_md.read_text(encoding="utf-8") if paths.progress_md.exists() else "",
        step_text,
    ]
    return clip_text("\n\n".join(parts), LESSON_CONTEXT_CHARS, from_end=True)


def select_relevant_lessons(paths: WorkflowPaths, context_text: str) -> list[dict[str, Any]]:
    scored: list[tuple[int, int, dict[str, Any]]] = []
    for lesson in load_workflow_lessons(paths):
        score = lesson_relevance_score(lesson, context_text)
        confidence = lesson.get("confidence", 0)
        if score > 0 and isinstance(confidence, int) and confidence > 0:
            scored.append((score, confidence, lesson))
    scored.sort(key=lambda item: (item[0], item[1]), reverse=True)
    return [lesson for _, _, lesson in scored[:MAX_RELEVANT_LESSONS]]


def render_lesson_list(items: Any) -> list[str]:
    values = lesson_text_values(items)
    return [f"  - {value}" for value in values if value.strip()]


def render_relevant_lessons(lessons: list[dict[str, Any]]) -> str:
    if not lessons:
        return "No relevant active workflow-level lessons were selected."

    lines = [
        "Relevant active workflow-level lessons:",
        "",
        "Confidence scale: 0=candidate/init, 1..10=active confidence, -1..-5=rejected. "
        "Use these lessons only within their stated scope.",
    ]
    for lesson in lessons:
        lesson_id = str(lesson.get("id", "unnamed-lesson"))
        title = str(lesson.get("title", "")).strip()
        confidence = lesson.get("confidence", 0)
        mode = str(lesson.get("mode", "advisory")).strip() or "advisory"
        lines.extend(
            [
                "",
                f"- id: {lesson_id}",
                f"  title: {title or lesson_id}",
                f"  confidence: {confidence}",
                f"  mode: {mode}",
            ]
        )
        claim = str(lesson.get("claim", "") or lesson.get("lesson", "")).strip()
        if claim:
            lines.append(f"  claim: {claim}")
        applies = render_lesson_list(lesson.get("applies_when", []))
        if applies:
            lines.append("  applies_when:")
            lines.extend(applies)
        required_checks = render_lesson_list(lesson.get("required_checks", []))
        if required_checks:
            lines.append("  required_checks:")
            lines.extend(required_checks)
        anti_patterns = render_lesson_list(lesson.get("anti_patterns", []))
        if anti_patterns:
            lines.append("  anti_patterns:")
            lines.extend(anti_patterns)
        falsification = render_lesson_list(lesson.get("falsification_conditions", []))
        if falsification:
            lines.append("  falsification_conditions:")
            lines.extend(falsification)
    return "\n".join(lines)


def markdown_heading_present(section_text: str, heading: str) -> bool:
    pattern = re.compile(rf"^###\s+{re.escape(heading)}\s*$", re.MULTILINE | re.IGNORECASE)
    return bool(pattern.search(section_text))


def markdown_subsection(section_text: str, heading: str) -> str:
    pattern = re.compile(
        rf"^###\s+{re.escape(heading)}\s*$\n?(?P<body>.*?)(?=^### |\Z)",
        re.MULTILINE | re.DOTALL | re.IGNORECASE,
    )
    match = pattern.search(section_text)
    return match.group("body").strip() if match else ""


def has_meaningful_evidence(text: str) -> bool:
    stripped = text.strip()
    if not stripped:
        return False
    placeholder_patterns = (
        r"^\s*(?:-|_|\*|n/?a|none|tbd|todo|not applicable)\s*\.?\s*$",
        r"\bno\s+evidence\b",
        r"\bnone\s+recorded\b",
    )
    return not any(re.search(pattern, stripped, re.IGNORECASE) for pattern in placeholder_patterns)


def evidence_reference_present(section_text: str, prefix: str, index: int) -> bool:
    """Return true when a section explicitly references an acceptance/check id.

    The executor prompt asks for stable ids such as `AC1` and `V2`.  Matching
    these ids is much less brittle than trying to infer criterion coverage from
    paraphrased prose.  Keep this permissive about punctuation so markdown list
    styles like `- AC1:` and `- **AC1** -` are both accepted.
    """
    pattern = re.compile(
        rf"(?<![A-Za-z0-9]){re.escape(prefix)}\s*0*{index}(?![A-Za-z0-9])",
        re.IGNORECASE,
    )
    return bool(pattern.search(section_text))


def evidence_match_tokens(text: str) -> list[str]:
    tokens = []
    for token in re.findall(r"[A-Za-z0-9_.:/=-]+", text.lower()):
        token = token.strip("`'\".,;:()[]{}")
        if len(token) < 4:
            continue
        if token in EVIDENCE_MATCH_STOPWORDS:
            continue
        tokens.append(token)
    return tokens


def section_mentions_item(section_text: str, item: Any) -> bool:
    item_text = str(item).strip()
    if not item_text:
        return True
    lowered_section = section_text.lower()
    lowered_item = item_text.lower()
    if lowered_item in lowered_section:
        return True
    section_tokens = set(evidence_match_tokens(lowered_section))
    tokens = evidence_match_tokens(lowered_item)
    if not tokens:
        return False
    strong_tokens = [token for token in tokens if any(ch.isdigit() for ch in token) or "." in token or "/" in token]
    if strong_tokens and any(token in section_tokens or token in lowered_section for token in strong_tokens):
        needed = 1 if len(tokens) <= 2 else min(2, len(tokens))
    else:
        needed = 1 if len(tokens) <= 2 else min(3, len(tokens))
    return sum(1 for token in tokens if token in section_tokens or token in lowered_section) >= needed


def evidence_maps_item(section_text: str, item: Any, *, prefix: str, index: int) -> bool:
    return evidence_reference_present(section_text, prefix, index) or section_mentions_item(section_text, item)


def validate_executor_evidence(section_text: str, step: dict[str, Any]) -> list[str]:
    issues: list[str] = []
    for heading in EXECUTOR_REQUIRED_EVIDENCE_HEADINGS:
        if not markdown_heading_present(section_text, heading):
            issues.append(f"Missing required `### {heading}` subsection.")

    acceptance_section = markdown_subsection(section_text, "Acceptance Evidence")
    verification_section = markdown_subsection(section_text, "Verification Evidence")
    changed_files_section = markdown_subsection(section_text, "Changed Files")
    outcome_section = markdown_subsection(section_text, "Outcome")

    evidence_sections = {
        "Acceptance Evidence": acceptance_section,
        "Verification Evidence": verification_section,
        "Changed Files": changed_files_section,
        "Outcome": outcome_section,
    }
    for label, body in evidence_sections.items():
        if not has_meaningful_evidence(body):
            issues.append(f"`### {label}` does not contain meaningful evidence.")

    for index, criterion in enumerate(step.get("acceptance_criteria", []), start=1):
        if not evidence_maps_item(acceptance_section, criterion, prefix="AC", index=index):
            issues.append(
                f"Acceptance criterion AC{index} is not explicitly mapped in `### Acceptance Evidence`: "
                f"{criterion}"
            )

    for index, check in enumerate(step.get("verification", []), start=1):
        if not evidence_maps_item(verification_section, check, prefix="V", index=index):
            issues.append(
                f"Verification requirement V{index} is not explicitly mapped in `### Verification Evidence`: "
                f"{check}"
            )

    if re.search(
        r"\b(?:exit|return)\s*(?:code)?\s*[:=]\s*-?[0-9]+\b|\b(?:exit|return)\s+code\b|\bexited?\s+[0-9]+\b",
        verification_section,
        re.IGNORECASE,
    ):
        pass
    elif re.search(
        r"\b(?:python|pytest|npm|yarn|pnpm|uv|cargo|go|bash|sh|make|cmake)\b",
        verification_section,
    ):
        issues.append(
            "`### Verification Evidence` mentions command-like checks but does not record an exit/return code."
        )

    combined_evidence = "\n".join([acceptance_section, verification_section, outcome_section])
    for pattern in INCOMPLETE_EVIDENCE_PATTERNS:
        if re.search(pattern, combined_evidence, re.IGNORECASE):
            issues.append(
                "Evidence contains incomplete-verification language such as still running, skipped, or not tested."
            )
            break

    return issues


def write_executor_evidence_failure_artifact(
    paths: WorkflowPaths,
    *,
    step_id: str,
    expected_heading: str,
    section_text: str,
    issues: list[str],
) -> Path:
    paths.command_artifacts_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    safe_step = re.sub(r"[^A-Za-z0-9_.-]+", "-", step_id).strip("-") or "step"
    report_path = paths.command_artifacts_dir / f"{timestamp}_executor_evidence_{safe_step}.report.md"
    report_path.write_text(
        "\n".join(
            [
                f"# Executor Evidence Contract Failure - {step_id}",
                "",
                "The executor appended the required step section, but the section did not include complete verification evidence.",
                "",
                f"- Expected heading: `## {expected_heading}`",
                f"- Results file: `{paths.results_md}`",
                "",
                "## Issues",
                "",
                *[f"- {issue}" for issue in issues],
                "",
                "## Required Result Subsections",
                "",
                *[f"- `### {heading}`" for heading in EXECUTOR_REQUIRED_EVIDENCE_HEADINGS],
                "",
                "## Captured Step Section",
                "",
                "```markdown",
                clip_text(section_text, EXECUTOR_EVIDENCE_REPORT_CHARS, from_end=True),
                "```",
                "",
            ]
        ),
        encoding="utf-8",
    )
    append_artifact_record(
        paths,
        category="executor_evidence_report",
        path=report_path,
        label=f"{step_id} evidence contract report",
        step_id=step_id,
        metadata={"stage": "executor_evidence_contract"},
    )
    return report_path


def index_existing_workspace_artifacts(paths: WorkflowPaths) -> None:
    index = load_artifact_index(paths.artifact_index_json)
    artifacts = index.setdefault("artifacts", [])
    existing_paths = {
        str(item.get("path"))
        for item in artifacts
        if isinstance(item, dict) and item.get("path")
    }

    known_artifacts: list[tuple[str, Path, str, dict[str, Any]]] = []
    if paths.discussion_input_log.exists():
        known_artifacts.append(("discussion_input", paths.discussion_input_log, "discussion input log", {}))
    if paths.discussion_output_log.exists():
        known_artifacts.append(("discussion_output", paths.discussion_output_log, "discussion output log", {}))
    if paths.discussion_transcript.exists():
        known_artifacts.append(("discussion_transcript", paths.discussion_transcript, "discussion transcript", {}))
    for path in sorted(paths.command_artifacts_dir.glob("*.stdout.txt")):
        known_artifacts.append(("command_failure_stdout", path, path.name, {}))
    for path in sorted(paths.command_artifacts_dir.glob("*.stderr.txt")):
        known_artifacts.append(("command_failure_stderr", path, path.name, {}))

    changed = False
    for category, path, label, metadata in known_artifacts:
        resolved_path = str(path.resolve())
        if resolved_path in existing_paths:
            continue
        artifacts.append(
            {
                "timestamp": utc_now(),
                "category": category,
                "label": label,
                "path": resolved_path,
                "step_id": None,
                "metadata": metadata,
            }
        )
        existing_paths.add(resolved_path)
        changed = True

    if changed:
        index["artifacts"] = artifacts[-200:]
        index["updated_at"] = utc_now()
        save_artifact_index(paths.artifact_index_json, index)


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
    append_artifact_record(
        paths,
        category="command_failure_stdout",
        path=stdout_path,
        label=f"{stage} stdout",
        step_id=step_id,
        metadata={"stage": stage},
    )
    append_artifact_record(
        paths,
        category="command_failure_stderr",
        path=stderr_path,
        label=f"{stage} stderr",
        step_id=step_id,
        metadata={"stage": stage},
    )
    return stdout_path, stderr_path


def write_executor_incomplete_artifacts(
    paths: WorkflowPaths,
    *,
    step_id: str,
    result: subprocess.CompletedProcess[str],
    expected_heading: str,
) -> tuple[Path, Path, Path]:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    safe_step = re.sub(r"[^A-Za-z0-9_.-]+", "-", step_id).strip("-") or "step"
    base_name = f"{timestamp}_executor_incomplete_{safe_step}"
    stdout_path = paths.command_artifacts_dir / f"{base_name}.stdout.txt"
    stderr_path = paths.command_artifacts_dir / f"{base_name}.stderr.txt"
    report_path = paths.command_artifacts_dir / f"{base_name}.report.md"

    stdout_path.write_text(result.stdout, encoding="utf-8")
    stderr_path.write_text(result.stderr, encoding="utf-8")
    report_path.write_text(
        "\n".join(
            [
                f"# Incomplete Executor Step - {step_id}",
                "",
                "The executor command exited with code 0 but did not append the required results section.",
                "",
                f"- Expected heading: `## {expected_heading}`",
                f"- Results file: `{paths.results_md}`",
                f"- Stdout artifact: `{stdout_path}`",
                f"- Stderr artifact: `{stderr_path}`",
                "",
                "## Stdout Tail",
                "",
                "```text",
                clip_text(result.stdout, EXECUTOR_INCOMPLETE_STDOUT_CHARS, from_end=True),
                "```",
                "",
                "## Stderr Tail",
                "",
                "```text",
                clip_text(result.stderr, EXECUTOR_INCOMPLETE_STDERR_CHARS, from_end=True),
                "```",
                "",
            ]
        ),
        encoding="utf-8",
    )

    for category, path, label in [
        ("executor_incomplete_stdout", stdout_path, f"{step_id} incomplete stdout"),
        ("executor_incomplete_stderr", stderr_path, f"{step_id} incomplete stderr"),
        ("executor_incomplete_report", report_path, f"{step_id} incomplete report"),
    ]:
        append_artifact_record(
            paths,
            category=category,
            path=path,
            label=label,
            step_id=step_id,
            metadata={"stage": "executor_incomplete"},
        )

    return stdout_path, stderr_path, report_path


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
    migration_text = (
        clip_text(paths.migration_md.read_text(encoding="utf-8"), MIGRATION_PROMPT_CHARS, from_end=True)
        if paths.migration_md.exists()
        else ""
    )
    existing_plan = clip_text(paths.plan_md.read_text(encoding="utf-8"), PLAN_PROMPT_CHARS, from_end=True)
    progress_text = clip_text(paths.progress_md.read_text(encoding="utf-8"), PROGRESS_PROMPT_CHARS, from_end=True)
    model_hint = planner_model_name(config)
    parent_runtime = runtime_context(paths)
    lessons_text = render_relevant_lessons(select_relevant_lessons(paths, lesson_context_for_planner(paths)))
    migration_requirements = ""
    migration_block = ""
    if migration_text.strip():
        migration_requirements = (
            f"- If `{paths.migration_md.name}` exists, treat it as the authoritative handoff from an earlier "
            "workflow workspace and continue from it instead of restarting from scratch.\n"
        )
        migration_block = f"""
Imported migration handoff:
```markdown
{migration_text.strip()}
```
"""

    return f"""You are the planning agent for a coding workflow.

Use {model_hint} style planning. Rewrite {paths.plan_md.name} as a concrete implementation plan.
Preserve the workflow manifest block markers and keep the YAML manifest machine-readable.

Requirements:
- Fill manifest.task with a short task summary.
- Create ordered steps under manifest.steps.
- Keep the YAML manifest parseable by `yaml.safe_load`.
- Use YAML block scalars (`|-`) or quoted strings for any value that spans multiple lines.
- Do not wrap a plain scalar onto a new line unless it is inside a list item or block scalar.
- Every step must include:
  - id: stable kebab-case id
  - title: concise label
  - status: set to pending
  - objective: short paragraph
  - acceptance_criteria: YAML list of strings; each item is one concrete condition that defines acceptable outcome for the step
  - implementation: YAML list of strings; each item is one concrete build action
  - verification: YAML list of strings; each item is one command or check that proves the step is finished
- If this is the first plan, set manifest.current_step to the first step id and manifest.status to pending.
- If this is a replan, preserve approved steps and set manifest.current_step / manifest.status to the next actionable step instead of restarting from the beginning.
- Keep the human-readable sections below the manifest in sync with the manifest.
- Treat `plan.md` as an operational plan, not an archive.
- Keep completed steps summarized. Do not include long retrospectives, command transcripts, diffs, or raw logs for finished steps.
- Keep the current step and pending future steps concrete and detailed enough to execute.
- If detailed evidence matters, reference `results.md` or workflow artifacts instead of embedding bulk output in the plan.
- Do not mark any step approved before review.
- Keep `acceptance_criteria`, `implementation`, and `verification` as separate sibling YAML keys. Never let text such as `implementation:` or `verification:` appear inside an acceptance criterion item.
- If a step includes implementation_summary, it must be a YAML list of strings. Do not emit implementation_summary as a plain string or block scalar.
- Inspect {paths.progress_md.name} and continue from the latest recorded workflow state instead of restarting completed work.
- Inspect `{paths.migration_md.name}` too when it exists.
- If the existing manifest and {paths.progress_md.name} disagree, prefer the more recent concrete execution evidence in {paths.results_md.name} and reconcile the plan.
- If the latest review rejected a step but did not require human intervention, update the plan so the next loop iteration attempts a concrete fix instead of repeating the same failed action blindly.
- Preserve already approved steps unless the evidence shows they are invalid.
- If remediation requires debugging the workflow itself, add explicit repair or diagnostic steps rather than treating the issue as a permanent external blocker.
- Prefer workflow-owned helper scripts and artifacts under the actual workflow root `{paths.root}` when automation glue is needed.
- Use the exact workflow root `{paths.root}` in all implementation and verification paths. Do not invent, create, symlink, or reference a repo-root `workflow_workspace` alias.
- If a failure is caused by missing public checkpoints, datasets, assets, or packages and the repository already documents or scripts how to acquire them, treat that as workflow work. Add an explicit prerequisite-staging or cache-population step instead of immediately requiring human intervention.
- For large or slow prerequisite downloads, prefer idempotent, resumable staging steps that materialize stable shared paths or caches before the expensive evaluation step runs.
- Avoid modifying tracked files inside submodules unless there is no viable workflow-local or repository-local alternative.
- Only leave the workflow blocked on human intervention if the latest evidence shows a permission, credential, quota, unavailable external resource with no workflow-controlled acquisition path, or operator-owned environment change that cannot be solved from this repository.
- Apply relevant active workflow-level lessons when their scope matches this task. If a selected lesson is only advisory, use it as a planning caution. If it is a planning gate, include concrete steps or acceptance criteria that satisfy its required checks before expensive or irreversible work.
- Do not invent new global lessons in the plan. New lessons must be proposed through review and human approval.
{migration_requirements}

Parent workflow runtime snapshot:
```text
{parent_runtime}
```

Workflow-level lessons:
```text
{lessons_text}
```

Path placeholders:
- `{{workspace}}`: {paths.root}
- `{{repo_root}}`: {paths.repo_root}
- `{{results}}`: {paths.results_md}

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
{migration_block}

Return the full contents of {paths.plan_md.name} only. No surrounding explanation.
"""


def build_planner_repair_prompt(
    paths: WorkflowPaths,
    broken_output: str,
    parse_error: str,
) -> str:
    existing_plan = clip_text(paths.plan_md.read_text(encoding="utf-8"), PLAN_PROMPT_CHARS, from_end=True)
    progress_text = clip_text(paths.progress_md.read_text(encoding="utf-8"), PROGRESS_PROMPT_CHARS, from_end=True)
    results_text = clip_text(paths.results_md.read_text(encoding="utf-8"), RESULTS_PROMPT_CHARS, from_end=True)
    return f"""You are repairing malformed workflow plan output from a planner.

The previous planner response could not be parsed as the workflow manifest YAML.
Repair it into a valid full `{paths.plan_md.name}` document.

Requirements:
- Preserve the `<!-- WORKFLOW_MANIFEST_START -->` / `<!-- WORKFLOW_MANIFEST_END -->` markers.
- Preserve the fenced YAML manifest block and make it parseable by `yaml.safe_load`.
- Return the full contents of `{paths.plan_md.name}` only. No explanation.
- Keep already approved/completed history intact unless it is obviously malformed.
- Keep the plan aligned with the latest workflow state from `{paths.progress_md.name}` and `{paths.results_md.name}`.
- Use YAML block scalars (`|-`) or quoted strings for any multi-line value.
- Every step's `acceptance_criteria`, `implementation`, and `verification` fields must be YAML lists of strings. Keep them as separate sibling keys.
- If a step includes implementation_summary, it must be a YAML list of strings. Do not emit implementation_summary as a plain string or block scalar.
- Do not insert prose inside the YAML manifest except as valid YAML string content.

Parser error:
```text
{parse_error.strip()}
```

Current plan file:
```markdown
{existing_plan.strip()}
```

Current progress file:
```markdown
{progress_text.strip()}
```

Current results file:
```markdown
{results_text.strip()}
```

Malformed planner output to repair:
```markdown
{broken_output.strip()}
```
"""


def parse_planner_manifest(planner_output: str) -> dict[str, Any]:
    manifest_text, _, _ = extract_manifest_block(planner_output)
    manifest = yaml.safe_load(manifest_text) or {}
    if not isinstance(manifest, dict):
        raise WorkflowError("Planner output did not contain a valid manifest mapping.")
    normalize_manifest(manifest)
    validate_manifest(manifest)
    if not manifest["steps"]:
        raise WorkflowError("Planner did not populate any steps in the manifest.")
    return manifest


def run_planner_command(
    paths: WorkflowPaths,
    config: dict[str, Any],
    prompt_text: str,
) -> str:
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
        raise WorkflowError(
            summarize_command_failure(
                paths,
                stage="planner_empty_output",
                message="Planner command returned empty output.",
                result=result,
            )
        )
    return planner_output


def build_discussion_prompt(paths: WorkflowPaths, task_summary: str = "", config: dict[str, Any] | None = None) -> str:
    task_text = paths.task_md.read_text(encoding="utf-8")
    discussion_text = paths.discussion_md.read_text(encoding="utf-8")
    migration_text = (
        clip_text(paths.migration_md.read_text(encoding="utf-8"), MIGRATION_PROMPT_CHARS, from_end=True)
        if paths.migration_md.exists()
        else ""
    )
    parent_runtime = runtime_context(paths)
    planner_model = discussion_model_name(config or {})
    summary_line = task_summary.strip() or "No short task summary was provided."
    migration_requirements = ""
    migration_block = ""
    if migration_text.strip():
        migration_requirements = f"""
- Read `{paths.migration_md.name}` first and use it as the starting context for this discussion.
- Treat this session as a continuation handoff, not a greenfield kickoff.
- Base the discussion on the imported prior progress, unresolved blockers, and the likely next step.
- If the imported handoff leaves ambiguity, inspect the related files in the source workflow workspace referenced by `{paths.migration_md.name}` before making strong claims.
- Ask at least one targeted question about what changed since the source workflow stopped, what was repaired manually, or what should be retried now.
"""
        migration_block = f"""
Imported migration handoff:
```markdown
{migration_text.strip()}
```
"""

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
- Never send a standalone progress-only message such as "Let me check/read/look at X". If you inspect something, do it first and then reply with the concrete finding plus at least one targeted question or decision-relevant summary.
- Do not treat this session as an execution task, implementation task, or autonomous research run. The primary deliverable here is a useful discussion transcript that can be summarized into `{paths.discussion_md.name}` plus clarified open questions and next actions.
- Use the chat to explore goals, constraints, prior attempts, risks, candidate approaches, evaluation criteria, and unknowns.
- Do not generate or rewrite `{paths.plan_md.name}` in this kickoff discussion.
- If the user wants codebase-specific grounding, inspect the repository as needed before making strong claims, but keep that inspection narrowly scoped to informing the discussion.
- Do not start implementing, running benchmarks, editing source code, or producing final conclusions unless the user explicitly asks for that and it is necessary for the discussion.
- If the user shares a link, first clarify what they want extracted from it before expanding into detailed analysis, unless immediate inspection is clearly necessary to answer the user.
- The later planner and progress stages will read `{paths.task_md.name}` and the summarized `{paths.discussion_md.name}` verbatim.
- Never say that a file was updated unless you actually updated it yourself in this session.
{migration_requirements}

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
{migration_block}

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

def build_codex_prompt(
    paths: WorkflowPaths,
    manifest: dict[str, Any],
    step: dict[str, Any],
) -> str:
    verification_lines = "\n".join(
        f"- V{index}: {item}" for index, item in enumerate(step.get("verification", []), start=1)
    ) or "- None listed"
    acceptance_lines = "\n".join(
        f"- AC{index}: {item}" for index, item in enumerate(step.get("acceptance_criteria", []), start=1)
    ) or "- None listed"
    implementation_lines = "\n".join(
        f"- I{index}: {item}" for index, item in enumerate(step.get("implementation", []), start=1)
    ) or "- No implementation notes provided"
    progress_text = clip_text(paths.progress_md.read_text(encoding="utf-8"), PROGRESS_PROMPT_CHARS, from_end=True)
    manifest_text = yaml.safe_dump(
        compact_manifest_for_prompt(manifest),
        sort_keys=False,
        allow_unicode=False,
    ).strip()
    parent_runtime = runtime_context(paths)
    lessons_text = render_relevant_lessons(select_relevant_lessons(paths, lesson_context_for_step(paths, step)))
    return f"""Implement exactly one approved workflow step in this repository.

Current step:
- id: {step['id']}
- title: {step['title']}
- objective: {step.get('objective', '')}

Implementation requirements:
{implementation_lines}

Acceptance criteria:
{acceptance_lines}

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

Workflow-level lessons:
```text
{lessons_text}
```

Required behavior:
- Work only on step `{step['id']}`.
- Read `{paths.progress_md}` first and use it to avoid redoing already completed work.
- Use `{paths.repo_root}` as the repository working root when you run commands or edit files.
- Treat submodule-owned areas as read-only by default, and prefer creating helper scripts or artifacts under `{paths.root}` when you need workflow-specific glue.
- Use `{paths.root}` directly for workflow-local files in implementation and verification commands. Do not create, update, or depend on a repo-root `workflow_workspace` directory or symlink.
- Before concluding that the host GPU, renderer, or environment is broken, compare your own execution environment against the parent workflow snapshot above. If they differ, treat that as a workflow-launch or sandbox mismatch and fix the workflow/scripts/config so future runs use the same environment as the parent workflow shell.
- Make the necessary repository changes.
- Ensure the acceptance criteria for this step are satisfied, or clearly record why they are not satisfied.
- Run the verification listed for this step.
- Do not finish your executor run while a command required for this step is still running in the background. Wait for it to finish, inspect its exit status and logs, then complete verification.
- Do not append the required step section until the step is actually complete or you have confirmed and documented a terminal blocker. A progress update like "training is still running" is not a valid completion of the executor contract.
- If the first attempt fails but the failure appears fixable from this repository, repair the issue and rerun verification within the same step instead of stopping at the first error.
- If verification fails because a public prerequisite is missing but the repository contains a documented or scriptable way to fetch or materialize it, implement that prerequisite staging in the workflow and rerun instead of treating it as immediate operator work.
- Prefer committed, idempotent setup helpers over one-off shell history. If a prerequisite is large, make the setup resumable and cache-aware so later workflow runs do not repeat the download or extraction.
- Reserve requests for human intervention for cases that truly require operator action outside the repository, such as missing permissions, credentials, cluster allocation, or external services you cannot control.
- Apply selected workflow-level lessons only when their stated scope matches the current step. If a lesson requires checks relevant to this step, perform them or record concrete evidence explaining why they do not apply.
- Append a new section to `{paths.results_md}` titled `Step {step['id']} - {step['title']}`.
- In that section include these exact third-level subsections:
  - `### Acceptance Evidence`: include one bullet for every acceptance criterion, labeled with its id (`AC1`, `AC2`, ...), and mark each `pass`, `fail`, or `inconclusive` with concrete evidence.
  - `### Verification Evidence`: include one bullet for every verification requirement, labeled with its id (`V1`, `V2`, ...), and record the command/check performed, working directory, exit/return code when command-based, artifact path when available, and result.
  - `### Changed Files`: list every changed file and why it changed, or state that no files changed.
  - `### Outcome`: state `pass`, `fail`, or `inconclusive`, with remaining risks.
- Do not write the step result section until required commands/checks have finished and the evidence above is complete.
- If a required verification is skipped, still record it under `### Verification Evidence` with `fail` or `inconclusive` and a concrete blocker; do not present skipped or still-running work as complete.
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
    lessons_text = render_relevant_lessons(select_relevant_lessons(paths, lesson_context_for_step(paths, step)))
    return f"""You are the review gate for a coding workflow.

Review whether Codex completed the current step well enough to continue.
Current step:
- id: {step['id']}
- title: {step['title']}
- objective: {step.get('objective', '')}

Step verification criteria:
{yaml.safe_dump(step.get("verification", []), sort_keys=False, allow_unicode=False).strip()}

Step acceptance criteria:
{yaml.safe_dump(step.get("acceptance_criteria", []), sort_keys=False, allow_unicode=False).strip()}

Plan file:
```markdown
{plan_text.strip()}
```

Results file:
```markdown
{results_text.strip()}
```

Indexed artifacts:
```text
{artifact_index_summary(paths)}
```

Current progress file:
```markdown
{progress_text.strip()}
```

Parent workflow runtime snapshot:
```text
{parent_runtime}
```

Workflow-level lessons:
```text
{lessons_text}
```

Return JSON only with this schema:
{{
  "approved": true or false,
  "outcome_status": "pass" | "fail" | "inconclusive",
  "outcome_reason": "short explanation of the outcome status",
  "summary": "short review summary",
  "acceptance_results": [
    {{"criterion": "criterion text", "status": "pass|fail|inconclusive", "evidence": "specific evidence from results/artifacts"}}
  ],
  "verification_results": [
    {{"check": "verification text", "status": "pass|fail|inconclusive", "evidence": "command/check, exit code when applicable, and artifact reference"}}
  ],
  "lesson_candidates": [
    {{
      "id": "stable-kebab-case-id",
      "title": "short title",
      "confidence": 0,
      "claim": "carefully scoped lesson candidate",
      "applies_when": ["scope condition"],
      "does_not_apply_when": ["out-of-scope condition"],
      "required_checks": ["future workflow check"],
      "evidence": ["artifact-backed claim"],
      "falsification_conditions": ["what would weaken or disprove it"],
      "review_required": ["codex", "claude", "gemini", "human"]
    }}
  ],
  "required_changes": ["change 1", "change 2"],
  "human_intervention_required": true or false,
  "human_intervention_reason": "short reason or empty string"
}}

Approve only if the step is implemented, verified, and evaluated against its acceptance criteria well enough to move on.
Use selected workflow-level lessons as review context only within their stated scope.
If the run exposes a reusable workflow-level lesson, return it in `lesson_candidates` with `confidence=0`; do not mark it active or globally approved.
Propose a lesson candidate only when it is supported by concrete artifacts and includes scope, required checks, and falsification conditions. Do not propose lessons from weak hypotheses or one-off local details.
Reject if the latest step result section is missing any of these subsections: `### Acceptance Evidence`, `### Verification Evidence`, `### Changed Files`, or `### Outcome`.
Reject if any acceptance criterion or verification requirement lacks specific evidence in the latest step result section.
Reject if command-based verification lacks an exit/return code, unless the result section explains why no command was applicable for that requirement.
Reject if required verification is described as still running, skipped, not tested, or to be verified later.
Set `outcome_status` to `pass` when the step completed and achieved its intended outcome, `fail` when the step executed but the measured outcome is unacceptable, and `inconclusive` when the step completed but the result cannot yet be judged confidently.
If any acceptance criterion is unmet, do not use `outcome_status=pass`; either reject the step or approve it with `outcome_status=fail` / `inconclusive` so follow-up work remains visible.
Use `approved=true` with `outcome_status=fail` when the workflow should continue but the poor result must remain visible as a follow-on issue instead of blocking step completion.
Set `human_intervention_required` to `true` only when the blocker clearly requires operator action outside the repository, such as missing permissions, credentials, unavailable external services, or unavailable hardware/resource allocation that the workflow cannot repair itself.
Set `human_intervention_required` to `false` for workflow bugs, stale assumptions, launcher/sandbox mismatches, missing retries, weak diagnostics, bad scripts, or other issues that a replanned repository change could fix in a later loop iteration.
Set `human_intervention_required` to `false` when the failure is a missing public checkpoint, dataset, asset bundle, or package that can be fetched or materialized by adding repository-local automation, even if the first attempt did not yet include that automation.
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

Indexed artifacts:
```text
{artifact_index_summary(paths)}
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
        "outcome_status": review.outcome_status,
        "outcome_reason": review.outcome_reason,
        "summary": review.summary,
        "required_changes": review.required_changes,
        "human_intervention_required": review.human_intervention_required,
        "human_intervention_reason": review.human_intervention_reason,
    },
    indent=2,
)}
```
"""

def write_progress_snapshot(paths: WorkflowPaths, progress_output: str) -> None:
    paths.progress_md.write_text(progress_output.rstrip() + "\n", encoding="utf-8")
    update_state_timestamp(paths.state_json, "last_progress_update_at")


def write_workflow_summary(
    paths: WorkflowPaths,
    *,
    summary_status: str,
    terminal_error: str | None = None,
    human_intervention_required: bool = False,
    human_intervention_reason: str | None = None,
) -> None:
    paths.summary_md.write_text(
        build_workflow_summary(
            paths,
            summary_status=summary_status,
            terminal_error=terminal_error,
            human_intervention_required=human_intervention_required,
            human_intervention_reason=human_intervention_reason,
        ),
        encoding="utf-8",
    )


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

    summary_prompt = build_discussion_summary_prompt(paths, discussion_model_name(config))
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
    if not is_valid_discussion_summary(summary_text):
        raise WorkflowError(
            summarize_command_failure(
                paths,
                stage="discussion_summary_invalid",
                message=(
                    "Discussion summary output did not match the required structured format. "
                    "The existing discussion.md was preserved."
                ),
                result=summary_result,
            )
        )
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
    planner_output = run_planner_command(paths, config, prompt_text)
    try:
        manifest = parse_planner_manifest(planner_output)
    except Exception as exc:
        repair_prompt = build_planner_repair_prompt(paths, planner_output, str(exc))
        repaired_output = run_planner_command(paths, config, repair_prompt)
        try:
            manifest = parse_planner_manifest(repaired_output)
            planner_output = repaired_output
        except Exception as repair_exc:
            planner_failure_summary = summarize_command_failure(
                paths,
                stage="planner_parse_error",
                message=(
                    "Planner returned malformed manifest output and the automatic repair retry also failed. "
                    f"First error: {exc}. Repair error: {repair_exc}."
                ),
                result=subprocess.CompletedProcess(
                    args=["planner_parse_error"],
                    returncode=0,
                    stdout=planner_output,
                    stderr="",
                ),
            )
            repair_failure_summary = summarize_command_failure(
                paths,
                stage="planner_repair_parse_error",
                message="Automatic planner repair returned malformed manifest output.",
                result=subprocess.CompletedProcess(
                    args=["planner_repair_parse_error"],
                    returncode=0,
                    stdout=repaired_output,
                    stderr="",
                ),
            )
            raise WorkflowError(
                f"{planner_failure_summary}\n\n{repair_failure_summary}"
            ) from repair_exc
    save_plan_manifest(paths.plan_md, manifest, planner_output)

    update_state_timestamp(paths.state_json, "last_planner_run_at")
    append_results_section_with_index(
        paths,
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
    step_results_heading = f"Step {step['id']} - {step['title']}"
    results_before = paths.results_md.read_text(encoding="utf-8")
    results_section_count_before = count_results_sections(results_before, step_results_heading)

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

    results_after = paths.results_md.read_text(encoding="utf-8")
    results_section_count_after = count_results_sections(results_after, step_results_heading)
    if results_section_count_after <= results_section_count_before:
        stdout_path, stderr_path, report_path = write_executor_incomplete_artifacts(
            paths,
            step_id=step["id"],
            result=result,
            expected_heading=step_results_heading,
        )
        failure_summary = summarize_command_failure(
            paths,
            stage="executor_contract",
            message=(
                "Executor command completed but did not append the required step section to "
                f"`{paths.results_md.name}` with heading `## {step_results_heading}`. "
                "This usually means the executor stopped early, reported partial progress, "
                "or wrote the wrong heading/path instead of satisfying the step contract. "
                f"Review the incomplete-step report at `{report_path}`."
            ),
            result=result,
            step_id=step["id"],
        )
        append_results_section_with_index(
            paths,
            f"Incomplete Executor Attempt - {step['id']}",
            "\n".join(
                [
                    f"Step: `{step['id']}`",
                    "",
                    "The executor exited successfully at the process level but did not append the required step result section.",
                    f"Expected heading: `## {step_results_heading}`",
                    "",
                    "Artifacts:",
                    f"- Incomplete attempt report: `{report_path}`",
                    f"- Full stdout: `{stdout_path}`",
                    f"- Full stderr: `{stderr_path}`",
                    "",
                    "Next action:",
                    "- Resume this same step and complete the acceptance criteria before writing the required step section.",
                ]
            ),
            step_id=step["id"],
        )
        mark_step_status(
            paths.plan_md,
            step["id"],
            "needs_changes",
            event="executor_contract_failed",
            details=clip_history_details(failure_summary),
        )
        write_progress_snapshot(
            paths,
            build_manifest_progress(paths, latest_step=step),
        )
        raise WorkflowError(failure_summary)

    latest_section = latest_results_section(results_after, step_results_heading)
    evidence_issues = validate_executor_evidence(latest_section or "", step)
    if evidence_issues:
        report_path = write_executor_evidence_failure_artifact(
            paths,
            step_id=step["id"],
            expected_heading=step_results_heading,
            section_text=latest_section or "",
            issues=evidence_issues,
        )
        failure_summary = (
            "Executor command completed and appended the step section, but the section failed the "
            "verification evidence contract.\n\n"
            "Issues:\n"
            + "\n".join(f"- {issue}" for issue in evidence_issues)
            + "\n\n"
            f"Evidence contract report: {report_path}"
        )
        append_results_section_with_index(
            paths,
            f"Incomplete Verification Evidence - {step['id']}",
            "\n".join(
                [
                    f"Step: `{step['id']}`",
                    "",
                    "The executor appended a step result section, but it did not provide enough structured evidence for review.",
                    "",
                    "Issues:",
                    *[f"- {issue}" for issue in evidence_issues],
                    "",
                    "Next action:",
                    "- Resume this same step and update the step result section with complete acceptance and verification evidence.",
                    f"- Evidence contract report: `{report_path}`",
                ]
            ),
            step_id=step["id"],
        )
        mark_step_status(
            paths.plan_md,
            step["id"],
            "needs_changes",
            event="executor_evidence_contract_failed",
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

    outcome_status = payload.get("outcome_status", "pass")
    if not isinstance(outcome_status, str) or outcome_status not in {"pass", "fail", "inconclusive"}:
        raise WorkflowError(
            "Reviewer JSON field 'outcome_status' must be one of: pass, fail, inconclusive."
        )

    outcome_reason = payload.get("outcome_reason", "")
    if not isinstance(outcome_reason, str):
        raise WorkflowError("Reviewer JSON field 'outcome_reason' must be a string.")

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

    acceptance_results = parse_review_result_list(payload.get("acceptance_results", []), "acceptance_results")
    verification_results = parse_review_result_list(payload.get("verification_results", []), "verification_results")
    lesson_candidates = parse_lesson_candidates(payload.get("lesson_candidates", []))

    return StepResult(
        approved=approved,
        summary=summary or "No review summary provided.",
        required_changes=[str(item) for item in required_changes],
        raw_output=stripped,
        outcome_status=outcome_status,
        outcome_reason=outcome_reason.strip(),
        human_intervention_required=human_intervention_required,
        human_intervention_reason=human_intervention_reason.strip(),
        acceptance_results=acceptance_results,
        verification_results=verification_results,
        lesson_candidates=lesson_candidates,
    )


def normalize_lesson_candidate(candidate: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(candidate)
    lesson_id = str(normalized.get("id", "")).strip()
    if not lesson_id:
        title = str(normalized.get("title", "") or normalized.get("claim", "")).strip().lower()
        lesson_id = re.sub(r"[^a-z0-9]+", "-", title).strip("-")[:80]
    normalized["id"] = lesson_id or "lesson-candidate"
    normalized["confidence"] = 0
    normalized.setdefault("review_required", ["codex", "claude", "gemini", "human"])
    normalized.setdefault("reviews", {"codex": "pending", "claude": "pending", "gemini": "pending", "human": "pending"})
    normalized.setdefault("status", "candidate")
    normalized.setdefault("created_at", utc_now())
    return normalized


def parse_lesson_candidates(value: Any) -> list[dict[str, Any]]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise WorkflowError("Reviewer JSON field 'lesson_candidates' must be a list when present.")

    candidates: list[dict[str, Any]] = []
    for index, item in enumerate(value, start=1):
        if not isinstance(item, dict):
            raise WorkflowError(f"Reviewer JSON field 'lesson_candidates' item {index} must be an object.")
        candidates.append(normalize_lesson_candidate(item))
    return candidates


def parse_review_result_list(value: Any, field_name: str) -> list[dict[str, str]]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise WorkflowError(f"Reviewer JSON field '{field_name}' must be a list when present.")

    parsed: list[dict[str, str]] = []
    for index, item in enumerate(value, start=1):
        if isinstance(item, dict):
            parsed.append({str(key): str(val) for key, val in item.items()})
        elif isinstance(item, str):
            parsed.append({"item": item})
        else:
            raise WorkflowError(
                f"Reviewer JSON field '{field_name}' item {index} must be an object or string."
            )
    return parsed


def format_review_result_matrix(items: list[dict[str, str]]) -> list[str]:
    if not items:
        return ["- None provided."]
    lines: list[str] = []
    for item in items:
        status = item.get("status", "unknown")
        label = item.get("criterion") or item.get("check") or item.get("item") or "item"
        evidence = item.get("evidence", "").strip()
        suffix = f" - {evidence}" if evidence else ""
        lines.append(f"- `{status}` {label}{suffix}")
    return lines


def load_lesson_candidate_file(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"lesson_candidates": []}
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise WorkflowError(f"Lesson candidate file must contain a YAML mapping: {path}")
    candidates = data.get("lesson_candidates", [])
    if candidates is None:
        data["lesson_candidates"] = []
    elif not isinstance(candidates, list):
        raise WorkflowError(f"Lesson candidate file 'lesson_candidates' field must be a list: {path}")
    return data


def save_lesson_candidates(paths: WorkflowPaths, candidates: list[dict[str, Any]], step: dict[str, Any]) -> None:
    if not candidates:
        return
    data = load_lesson_candidate_file(paths.lesson_candidates_yaml)
    existing = data.setdefault("lesson_candidates", [])
    for candidate in candidates:
        item = normalize_lesson_candidate(candidate)
        item.setdefault("source", {})
        if isinstance(item["source"], dict):
            item["source"].setdefault("workspace", str(paths.root))
            item["source"].setdefault("step_id", step["id"])
            item["source"].setdefault("results", str(paths.results_md))
        existing.append(item)
    data["updated_at"] = utc_now()
    paths.lesson_candidates_yaml.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=False), encoding="utf-8")


def format_lesson_candidate_summary(candidates: list[dict[str, Any]]) -> list[str]:
    if not candidates:
        return []
    lines = ["", "Lesson candidates:"]
    for candidate in candidates:
        lesson_id = str(candidate.get("id", "lesson-candidate"))
        confidence = candidate.get("confidence", 0)
        title = str(candidate.get("title", "") or candidate.get("claim", "")).strip()
        lines.append(f"- `{lesson_id}` confidence={confidence}: {title}")
    return lines


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

    try:
        review = parse_review_json(result.stdout)
    except WorkflowError as exc:
        raise WorkflowError(
            summarize_command_failure(
                paths,
                stage="reviewer_parse_error",
                message=f"Reviewer command succeeded but returned invalid JSON: {exc}",
                result=result,
                step_id=step["id"],
            )
        ) from exc

    review_body = [
        f"Step: `{step['id']}`",
        f"Approved: `{str(review.approved).lower()}`",
        f"Outcome status: `{review.outcome_status}`",
        "",
        "Summary:",
        review.summary,
    ]
    if review.outcome_reason:
        review_body.extend(
            [
                "",
                "Outcome detail:",
                review.outcome_reason,
            ]
        )
    if review.acceptance_results:
        review_body.extend(
            [
                "",
                "Acceptance results:",
                *format_review_result_matrix(review.acceptance_results),
            ]
        )
    if review.verification_results:
        review_body.extend(
            [
                "",
                "Verification results:",
                *format_review_result_matrix(review.verification_results),
            ]
        )
    if review.lesson_candidates:
        review_body.extend(format_lesson_candidate_summary(review.lesson_candidates))
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
    append_results_section_with_index(
        paths,
        f"Review - {step['id']}",
        "\n".join(review_body),
        step_id=step["id"],
    )
    save_lesson_candidates(paths, review.lesson_candidates, step)

    if review.approved:
        approve_step(
            paths.plan_md,
            step["id"],
            review.summary,
            outcome_status=review.outcome_status,
            outcome_reason=review.outcome_reason,
        )
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
    append_results_section_with_index(
        paths,
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
        step_id=step["id"],
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
    if manifest.get("status") == "planning" or not manifest["steps"]:
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
            write_workflow_summary(paths, summary_status=SUMMARY_STATUS_DONE)
            return

        step = get_active_step(manifest)
        if step.get("status") == "awaiting_review":
            review = run_review(paths, config, step["id"])
            if not review.approved:
                if review.human_intervention_required:
                    reason = review.human_intervention_reason or review.summary
                    write_workflow_summary(
                        paths,
                        summary_status=SUMMARY_STATUS_BLOCKED,
                        terminal_error=(
                            f"Review rejected step '{step['id']}' and requires human intervention: {reason}"
                        ),
                        human_intervention_required=True,
                        human_intervention_reason=reason,
                    )
                    raise WorkflowError(
                        f"Review rejected step '{step['id']}' and requires human intervention: {reason}"
                    )

                prior_attempts = auto_replans_by_step.get(step["id"], 0)
                if prior_attempts >= replan_limit:
                    write_workflow_summary(
                        paths,
                        summary_status=SUMMARY_STATUS_FAILED,
                        terminal_error=(
                            f"Review rejected step '{step['id']}' after {prior_attempts} automatic replans. "
                            "Auto-replan limit reached; inspect results.md and progress.md."
                        ),
                    )
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
        default="workflow/configs/config.gemini.example.yaml",
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
    init_parser.add_argument(
        "--migrate-from-workspace",
        default="",
        help="Import context from an existing workflow workspace before starting discussion.",
    )
    init_parser.add_argument(
        "--skip-migrate-runtime-env",
        action="store_true",
        help="When used with --migrate-from-workspace, do not copy runtime.env from the source workspace.",
    )

    migrate_parser = subparsers.add_parser(
        "migrate",
        help="Create a fresh workflow workspace from an existing workflow workspace.",
    )
    migrate_parser.add_argument(
        "--from-workspace",
        required=True,
        help="Existing workflow workspace to summarize and import.",
    )
    migrate_parser.add_argument(
        "--skip-runtime-env",
        action="store_true",
        help="Do not copy runtime.env from the source workspace into the new workspace.",
    )
    migrate_parser.add_argument(
        "--in-place",
        action="store_true",
        help="Refresh workflow state inside the same workspace instead of creating a new one. Run-local payload stays in place and prior workflow-state files are snapshotted.",
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
            migration_source = args.migrate_from_workspace.strip()
            migrated_during_init = False
            refreshed_placeholders = False

            if migration_source:
                source_paths = WorkflowPaths(root=Path(migration_source).resolve())
                run_migration(
                    paths,
                    source_paths=source_paths,
                    copy_runtime_env=not args.skip_migrate_runtime_env,
                    ensure_workflow_files_fn=ensure_workflow_files,
                )
                task_md_already_exists = True
                migrated_during_init = True
            else:
                if not task_md_already_exists and not related_links and sys.stdin.isatty():
                    related_links = prompt_for_related_links()
                ensure_workflow_files(paths, task_summary=args.task_summary, related_links=related_links)
                refreshed_placeholders = refresh_placeholder_workspace(
                    paths,
                    task_summary=args.task_summary,
                    related_links=related_links,
                )
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
            if migrated_during_init:
                print(f"Imported migration handoff from {Path(migration_source).resolve()}")
                print(f"Saved migration summary in {paths.migration_md}")
            if any(runtime_model_overrides.values()):
                print(f"Saved workspace model overrides in {paths.root / 'runtime.env'}")
            if refreshed_placeholders:
                print(f"Updated placeholder workflow files in {paths.root} with the provided task summary.")
            if task_md_already_exists and not migrated_during_init and not refreshed_placeholders:
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

        if args.command == "migrate":
            source_paths = WorkflowPaths(root=Path(args.from_workspace).resolve())
            run_migration(
                paths,
                source_paths=source_paths,
                copy_runtime_env=not args.skip_runtime_env,
                ensure_workflow_files_fn=ensure_workflow_files,
                in_place=args.in_place,
            )
            print(f"Migrated workflow workspace to {paths.root}")
            print(f"Imported handoff summary written to {paths.migration_md}")
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
        if args.command == "loop":
            try:
                write_workflow_summary(paths, summary_status=SUMMARY_STATUS_FAILED, terminal_error=str(exc))
            except WorkflowError:
                pass
        print(
            f"Workflow error: {summarize_workflow_error_for_console(str(exc))}",
            file=sys.stderr,
        )
        return 1
    except KeyboardInterrupt:
        if args.command == "loop":
            try:
                write_workflow_summary(
                    paths,
                    summary_status=SUMMARY_STATUS_INTERRUPTED,
                    terminal_error="Workflow interrupted by operator.",
                )
            except WorkflowError:
                pass
        print("Workflow interrupted.", file=sys.stderr)
        return 130

    raise AssertionError(f"Unhandled command: {args.command}")


if __name__ == "__main__":
    raise SystemExit(main())
