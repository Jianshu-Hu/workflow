from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Callable

from utils.common import (
    RESULTS_HEADER,
    WorkflowError,
    WorkflowPaths,
    clip_text,
    load_state,
    read_optional_text,
    render_discussion_template,
    render_task_template,
    save_state,
    utc_now,
)
from utils.manifest import (
    clip_history_details,
    create_default_manifest,
    get_active_step,
    latest_history_event,
    load_plan_manifest,
    summarize_step_review,
)


RESULTS_SUMMARY_CHARS = 400


def extract_markdown_section(markdown_text: str, heading: str) -> str:
    pattern = re.compile(
        rf"(?ms)^## {re.escape(heading)}\s*\n(.*?)(?=^## |\Z)",
    )
    match = pattern.search(markdown_text)
    if not match:
        return ""
    return match.group(1).strip()


def extract_markdown_bullets(section_text: str) -> list[str]:
    bullets: list[str] = []
    for line in section_text.splitlines():
        stripped = line.strip()
        if stripped.startswith("- "):
            bullets.append(stripped)
    return bullets


def issues_list_is_placeholder(lines: list[str]) -> bool:
    if not lines:
        return True
    placeholders = {
        "- none recorded.",
        "- none recorded in the source progress file.",
    }
    return all(line.strip().lower() in placeholders for line in lines)


def extract_task_summary(task_text: str, manifest_task: str = "") -> str:
    candidates = [extract_markdown_section(task_text, "Summary"), task_text, manifest_task]
    for candidate_text in candidates:
        stripped = candidate_text.strip()
        if not stripped:
            continue
        for block in re.split(r"\n\s*\n", stripped):
            lines = [
                line.strip()
                for line in block.splitlines()
                if line.strip() and not line.lstrip().startswith("#")
            ]
            prose_lines = [line for line in lines if not line.startswith("- ")]
            candidate = " ".join(prose_lines).strip()
            if candidate:
                return clip_text(candidate, RESULTS_SUMMARY_CHARS)
    return "Imported workflow task"


def format_source_step_summary(step: dict[str, Any]) -> str:
    label = f"`{step['id']}` ({step['title']}) [{step.get('status', 'pending')}]"
    if step.get("status") == "approved":
        return f"- {label}: {summarize_step_review(step)}"
    if step.get("status") == "done":
        return f"- {label}: Completed in the source workspace."
    objective = str(step.get("objective", "")).strip()
    if objective:
        return f"- {label}: {clip_text(objective, RESULTS_SUMMARY_CHARS)}"
    return f"- {label}"


def derive_open_issues_from_manifest(source_manifest: dict[str, Any]) -> list[str]:
    derived: list[str] = []
    latest_review = latest_history_event(source_manifest, events={"approved", "changes_requested"})
    if latest_review and isinstance(latest_review.get("details"), str):
        details = clip_history_details(latest_review["details"])
        lowered = details.lower()
        keywords = (
            "missing",
            "block",
            "blocked",
            "prevent",
            "prerequisite",
            "rerun",
            "repair",
            "human intervention",
            "operator-owned",
        )
        if any(keyword in lowered for keyword in keywords):
            derived.append(f"- Derived from latest review: {details}")

    try:
        active_step = get_active_step(source_manifest)
    except WorkflowError:
        active_step = None
    if active_step is not None and active_step.get("status") in {"needs_changes", "in_progress", "awaiting_review"}:
        derived.append(
            f"- Active source step `{active_step['id']}` ({active_step['title']}) still needs attention."
        )
    return derived


def append_import_note(markdown_text: str, *, source_root: Path, imported_at: str, note_heading: str) -> str:
    base = markdown_text.rstrip()
    if not base:
        base = f"# {note_heading}"
    note_lines = [
        "",
        f"## {note_heading}",
        "",
        f"- Imported from `{source_root}` at `{imported_at}`.",
        "- Continue from `migration.md` in this workspace instead of restarting completed work.",
    ]
    return "\n".join([base, *note_lines]).rstrip() + "\n"


def load_source_manifest(paths: WorkflowPaths) -> tuple[dict[str, Any], str | None]:
    if not paths.plan_md.exists():
        return create_default_manifest(), f"Missing source plan file: {paths.plan_md}"
    try:
        manifest, _ = load_plan_manifest(paths.plan_md)
        return manifest, None
    except WorkflowError as exc:
        return create_default_manifest(), str(exc)


def build_migration_handoff(
    *,
    source_paths: WorkflowPaths,
    source_manifest: dict[str, Any],
    source_manifest_error: str | None,
    imported_at: str,
) -> str:
    source_task_text = read_optional_text(source_paths.task_md)
    source_progress_text = read_optional_text(source_paths.progress_md)
    source_results_path = source_paths.results_md
    source_state = load_state(source_paths.state_json) if source_paths.state_json.exists() else {}

    task_summary = extract_task_summary(source_task_text, str(source_manifest.get("task", "")))
    completed_steps = [
        format_source_step_summary(step)
        for step in source_manifest.get("steps", [])
        if step.get("status") in {"approved", "done"}
    ] or ["- None recorded in the source workspace."]

    active_step = None
    try:
        active_step = get_active_step(source_manifest)
    except WorkflowError:
        active_step = None

    unfinished_lines = ["- No active unfinished step was recorded in the source manifest."]
    if active_step is not None:
        unfinished_lines = [format_source_step_summary(active_step)]

    latest_review_lines = extract_markdown_bullets(
        extract_markdown_section(source_progress_text, "Latest Review")
    ) or ["- No latest review section was available in the source progress file."]
    open_issue_lines = extract_markdown_bullets(
        extract_markdown_section(source_progress_text, "Open Issues")
    )
    if issues_list_is_placeholder(open_issue_lines):
        open_issue_lines = derive_open_issues_from_manifest(source_manifest)
    if not open_issue_lines:
        open_issue_lines = ["- None recorded in the source progress file."]
    next_step_lines = extract_markdown_bullets(
        extract_markdown_section(source_progress_text, "Next Step")
    ) or ["- No explicit next step was recorded in the source progress file."]

    manifest_warning_lines = []
    if source_manifest_error:
        manifest_warning_lines = [
            "## Import Warnings",
            "",
            f"- Source manifest could not be parsed cleanly: {source_manifest_error}",
            "",
        ]

    history_lines: list[str] = []
    for entry in source_manifest.get("history", [])[-5:]:
        timestamp = entry.get("timestamp", "unknown")
        event = entry.get("event", "unknown")
        step_id = entry.get("step_id", "unknown")
        details = clip_history_details(str(entry.get("details", "")))
        history_lines.append(f"- `{timestamp}` `{step_id}` `{event}`: {details}")
    if not history_lines:
        history_lines = ["- No recent source history entries were available."]

    return "\n".join(
        [
            "# Migration Handoff",
            "",
            "## Source Workspace",
            "",
            f"- Source workspace: `{source_paths.root}`",
            f"- Imported at: `{imported_at}`",
            f"- Source workflow status: `{source_manifest.get('status', 'unknown')}`",
            f"- Source current step: `{source_manifest.get('current_step') or 'none'}`",
            f"- Source task summary: {task_summary}",
            f"- Source results log: `{source_results_path}`",
            f"- Source progress file: `{source_paths.progress_md}`",
            f"- Source plan file: `{source_paths.plan_md}`",
            (
                f"- Source created_at: `{source_state.get('created_at')}`"
                if source_state.get("created_at")
                else "- Source created_at: not recorded."
            ),
            (
                f"- Source last review at: `{source_state.get('last_review_at')}`"
                if source_state.get("last_review_at")
                else "- Source last review at: not recorded."
            ),
            "",
            *manifest_warning_lines,
            "## Completed Work",
            "",
            *completed_steps,
            "",
            "## Unfinished Work",
            "",
            *unfinished_lines,
            "",
            "## Latest Review",
            "",
            *latest_review_lines,
            "",
            "## Open Issues To Revisit",
            "",
            *open_issue_lines,
            "",
            "## Next Step From Source Workflow",
            "",
            *next_step_lines,
            "",
            "## Recent Source History",
            "",
            *history_lines,
            "",
            "## Planning Guidance",
            "",
            "- Continue from the imported unfinished work instead of recreating already approved steps.",
            "- Re-validate any source blocker that may already have been fixed outside the workflow.",
            "- Read the source workspace directly if more detail is needed than this handoff captures.",
        ]
    ).rstrip() + "\n"


def build_migrated_progress(
    *,
    source_paths: WorkflowPaths,
    source_manifest: dict[str, Any],
    imported_at: str,
) -> str:
    source_progress_text = read_optional_text(source_paths.progress_md)
    latest_review_lines = extract_markdown_bullets(
        extract_markdown_section(source_progress_text, "Latest Review")
    ) or ["- No latest review section was available in the source progress file."]
    open_issue_lines = extract_markdown_bullets(
        extract_markdown_section(source_progress_text, "Open Issues")
    )
    if issues_list_is_placeholder(open_issue_lines):
        open_issue_lines = derive_open_issues_from_manifest(source_manifest)
    if not open_issue_lines:
        open_issue_lines = ["- None recorded in the source progress file."]

    return "\n".join(
        [
            "# Workflow Progress",
            "",
            "## Current Status",
            f"- This workspace was migrated from `{source_paths.root}` at `{imported_at}`.",
            "- No execution has happened in this destination workspace yet.",
            "- **Workflow Status:** `planning`",
            "",
            "## Completed Steps",
            "- None yet in this workspace.",
            "- Prior completed work is summarized in `migration.md`.",
            "",
            "## Latest Review",
            *latest_review_lines,
            "",
            "## Open Issues",
            *open_issue_lines,
            "- A fresh plan still needs to be generated in this workspace before execution resumes.",
            "",
            "## Next Step",
            "- Generate a new plan that continues from `migration.md` and the imported source evidence.",
            "",
            "## Resume Instructions",
            "- Read `migration.md`, `task.md`, and `discussion.md` in this workspace first.",
            f"- Inspect the source workspace at `{source_paths.root}` directly if more detail is needed.",
            (
                f"- The source workflow last recorded status was `{source_manifest.get('status', 'unknown')}`."
            ),
        ]
    ).rstrip() + "\n"


def ensure_destination_workspace_is_fresh(paths: WorkflowPaths) -> None:
    if not paths.root.exists():
        return

    existing_files = [
        candidate
        for candidate in (
            paths.task_md,
            paths.discussion_md,
            paths.plan_md,
            paths.results_md,
            paths.migration_md,
            paths.progress_md,
            paths.summary_md,
            paths.state_json,
            paths.root / "runtime.env",
        )
        if candidate.exists()
    ]
    if existing_files:
        raise WorkflowError(
            "Destination workspace already contains workflow state: "
            + ", ".join(str(path) for path in existing_files)
        )


def run_migration(
    dest_paths: WorkflowPaths,
    *,
    source_paths: WorkflowPaths,
    copy_runtime_env: bool,
    ensure_workflow_files_fn: Callable[..., None],
) -> None:
    if dest_paths.root == source_paths.root:
        raise WorkflowError("Source and destination workspaces must be different.")
    if not source_paths.root.exists():
        raise WorkflowError(f"Source workspace does not exist: {source_paths.root}")
    for required in (
        source_paths.task_md,
        source_paths.discussion_md,
        source_paths.progress_md,
    ):
        if not required.exists():
            raise WorkflowError(f"Source workspace is missing required file: {required}")

    ensure_destination_workspace_is_fresh(dest_paths)

    source_manifest, source_manifest_error = load_source_manifest(source_paths)
    source_task_text = read_optional_text(source_paths.task_md)
    source_discussion_text = read_optional_text(source_paths.discussion_md)
    task_summary = extract_task_summary(source_task_text, str(source_manifest.get("task", "")))
    imported_at = utc_now()

    ensure_workflow_files_fn(dest_paths, task_summary=task_summary)

    dest_paths.task_md.write_text(
        append_import_note(
            source_task_text or render_task_template(task_summary),
            source_root=source_paths.root,
            imported_at=imported_at,
            note_heading="Imported Workflow Context",
        ),
        encoding="utf-8",
    )
    dest_paths.discussion_md.write_text(
        append_import_note(
            source_discussion_text or render_discussion_template(task_summary),
            source_root=source_paths.root,
            imported_at=imported_at,
            note_heading="Imported Workflow Context",
        ),
        encoding="utf-8",
    )
    dest_paths.migration_md.write_text(
        build_migration_handoff(
            source_paths=source_paths,
            source_manifest=source_manifest,
            source_manifest_error=source_manifest_error,
            imported_at=imported_at,
        ),
        encoding="utf-8",
    )
    dest_paths.progress_md.write_text(
        build_migrated_progress(
            source_paths=source_paths,
            source_manifest=source_manifest,
            imported_at=imported_at,
        ),
        encoding="utf-8",
    )
    dest_paths.results_md.write_text(
        (
            RESULTS_HEADER
            + "\n"
            + "## Migration Import\n\n"
            + f"- Imported from `{source_paths.root}` at `{imported_at}`.\n"
            + f"- Source workflow status: `{source_manifest.get('status', 'unknown')}`.\n"
            + f"- Source current step: `{source_manifest.get('current_step') or 'none'}`.\n"
            + "- This destination workspace starts cleanly in planning state; see `migration.md` for the handoff summary.\n"
        ),
        encoding="utf-8",
    )

    state = load_state(dest_paths.state_json)
    state["migrated_from"] = str(source_paths.root)
    state["migrated_at"] = imported_at
    save_state(dest_paths.state_json, state)

    if copy_runtime_env and (source_paths.root / "runtime.env").exists():
        (dest_paths.root / "runtime.env").write_text(
            (source_paths.root / "runtime.env").read_text(encoding="utf-8"),
            encoding="utf-8",
        )
