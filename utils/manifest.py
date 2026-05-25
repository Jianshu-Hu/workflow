from __future__ import annotations

import copy
import re
from pathlib import Path
from typing import Any

import yaml

from utils.common import (
    SUMMARY_STATUS_BLOCKED,
    SUMMARY_STATUS_DONE,
    SUMMARY_STATUS_FAILED,
    SUMMARY_STATUS_INTERRUPTED,
    VALID_SUMMARY_STATUSES,
    StepResult,
    WorkflowError,
    WorkflowPaths,
    clip_text,
    workflow_now,
)


MANIFEST_START = "<!-- WORKFLOW_MANIFEST_START -->"
MANIFEST_END = "<!-- WORKFLOW_MANIFEST_END -->"

MANIFEST_HISTORY_PROMPT_ENTRIES = 12
MANIFEST_HISTORY_DETAIL_CHARS = 2000
MANIFEST_HISTORY_SAVE_ENTRIES = 40
APPROVED_STEP_SUMMARY_CHARS = 500
RESULTS_SUMMARY_CHARS = 400
VALID_WORKFLOW_OUTCOME_STATUSES = {"pending", "pass", "fail", "inconclusive"}
BLOCKING_OUTCOME_KEY = "blocks_downstream_on_fail"


def step_blocks_downstream_on_fail(step: dict[str, Any]) -> bool:
    explicit_gate = step.get(BLOCKING_OUTCOME_KEY)
    if explicit_gate is not None:
        return explicit_gate is True

    step_id = str(step.get("id", "")).lower()
    title = str(step.get("title", "")).lower()
    objective = str(step.get("objective", "")).lower()
    haystack = " ".join([step_id, title, objective])
    if "smoke" in haystack and any(token in haystack for token in ("eval", "evaluation", "benchmark", "test")):
        return True
    return False


def _normalize_followup_step_id(source_step_id: str) -> str:
    return f"followup-{source_step_id}"


def is_auto_followup_step_id(step_id: str) -> bool:
    return step_id.startswith("followup-")


def _default_followup_title(step: dict[str, Any]) -> str:
    return f"Investigate failed outcome for {step['title']}"


def _default_followup_objective(step: dict[str, Any]) -> str:
    outcome_reason = str(step.get("outcome_reason", "")).strip()
    summary = outcome_reason or "The approved step completed but produced an unacceptable outcome."
    return (
        "Investigate the failed approved outcome, determine the likely cause, "
        "and either implement a remediation or document why the benchmark/result remains unsatisfied. "
        f"Current failure signal: {summary}"
    )


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


def summarize_step_outcome(step: dict[str, Any]) -> str | None:
    outcome_status = str(step.get("outcome_status", "")).strip()
    if not outcome_status:
        return None

    outcome_reason = str(step.get("outcome_reason", "")).strip()
    if outcome_reason:
        return clip_text(
            f"Outcome `{outcome_status}`: {outcome_reason}",
            APPROVED_STEP_SUMMARY_CHARS,
            from_end=True,
        )
    return f"Outcome `{outcome_status}`."


def summarize_workflow_outcome(manifest: dict[str, Any]) -> tuple[str, str]:
    approved_steps = [
        step
        for step in manifest.get("steps", [])
        if isinstance(step, dict) and step.get("status") == "approved"
    ]
    unresolved_failures = [
        step
        for step in approved_steps
        if str(step.get("outcome_status", "")).strip() == "fail"
    ]
    unresolved_inconclusive = [
        step
        for step in approved_steps
        if str(step.get("outcome_status", "")).strip() == "inconclusive"
    ]

    if manifest.get("status") != "done":
        return (
            "pending",
            "Workflow execution is still in progress, so the overall objective outcome is not final yet.",
        )
    if unresolved_failures:
        labels = ", ".join(f"`{step['id']}`" for step in unresolved_failures[:3])
        suffix = " and others" if len(unresolved_failures) > 3 else ""
        return (
            "fail",
            f"Approved steps remain unresolved with outcome `fail`: {labels}{suffix}.",
        )
    if unresolved_inconclusive:
        labels = ", ".join(f"`{step['id']}`" for step in unresolved_inconclusive[:3])
        suffix = " and others" if len(unresolved_inconclusive) > 3 else ""
        return (
            "inconclusive",
            f"Approved steps remain unresolved with outcome `inconclusive`: {labels}{suffix}.",
        )
    if approved_steps:
        return (
            "pass",
            "Workflow execution is complete and no approved step records a failing or inconclusive outcome.",
        )
    return (
        "inconclusive",
        "Workflow execution is complete, but no approved steps were recorded to prove the objective was achieved.",
    )


def sync_workflow_outcome(manifest: dict[str, Any]) -> dict[str, Any]:
    outcome, reason = summarize_workflow_outcome(manifest)
    manifest["workflow_outcome"] = outcome
    manifest["workflow_outcome_reason"] = reason
    return manifest


def unresolved_outcome_issue_lines(manifest: dict[str, Any]) -> list[str]:
    issues: list[str] = []
    for step in manifest.get("steps", []):
        if step.get("status") != "approved":
            continue
        outcome_status = str(step.get("outcome_status", "")).strip()
        if outcome_status not in {"fail", "inconclusive"}:
            continue
        reason = str(step.get("outcome_reason", "")).strip() or str(step.get("review_summary", "")).strip()
        issues.append(
            f"- `{step['id']}` remains unresolved with outcome `{outcome_status}`: "
            f"{clip_text(reason or 'No reason recorded.', RESULTS_SUMMARY_CHARS, from_end=True)}"
        )
    return issues


def blocked_step_issue_lines(manifest: dict[str, Any]) -> list[str]:
    issues: list[str] = []
    for step in manifest.get("steps", []):
        if step.get("status") != "blocked":
            continue
        reason = str(step.get("blocked_reason", "")).strip()
        issues.append(
            f"- `{step['id']}` is blocked: "
            f"{clip_text(reason or 'No reason recorded.', RESULTS_SUMMARY_CHARS, from_end=True)}"
        )
    return issues


def render_step_summary(step: dict[str, Any]) -> list[str]:
    lines = [f"- {step_label(step)} [{step.get('status', 'pending')}]"]
    if step.get("status") == "approved":
        lines.append(f"  Review: {summarize_step_review(step)}")
        outcome_summary = summarize_step_outcome(step)
        if outcome_summary:
            lines.append(f"  {outcome_summary}")
    elif step.get("status") == "done":
        lines.append("  Completed.")
    elif step.get("status") == "needs_changes":
        lines.append("  Needs changes before the workflow can continue.")
    elif step.get("status") == "blocked":
        reason = str(step.get("blocked_reason", "")).strip()
        rendered_reason = clip_text(reason, RESULTS_SUMMARY_CHARS, from_end=True) if reason else "No reason recorded."
        lines.append(f"  Blocked: {rendered_reason}")
    return lines


def render_step_detail(step: dict[str, Any]) -> str:
    implementation_lines = [f"- {item}" for item in step.get("implementation", [])] or ["- None recorded."]
    verification_lines = [f"- {item}" for item in step.get("verification", [])] or ["- None recorded."]
    acceptance_lines = [f"- {item}" for item in step.get("acceptance_criteria", [])] or ["- None recorded."]
    objective = str(step.get("objective", "")).strip() or "No objective recorded."
    return "\n".join(
        [
            f"### Step {step_label(step)}",
            "",
            f"- Status: `{step.get('status', 'pending')}`",
            *(
                [
                    f"- Blocked reason: {str(step.get('blocked_reason', '')).strip()}",
                ]
                if step.get("status") == "blocked" and str(step.get("blocked_reason", "")).strip()
                else []
            ),
            "",
            "Objective:",
            objective,
            "",
            "Acceptance Criteria:",
            *acceptance_lines,
            "",
            "Implementation:",
            *implementation_lines,
            "",
            "Verification:",
            *verification_lines,
        ]
    )


def render_plan_document(manifest: dict[str, Any]) -> str:
    manifest = sync_workflow_outcome(copy.deepcopy(manifest))
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
        if step is not active_step and step.get("status") in {"pending", "needs_changes", "in_progress", "awaiting_review", "blocked"}
    ]

    sections = [
        "# Workflow Plan",
        "",
        render_manifest(manifest),
        "",
        "## Workflow Summary",
        "",
        f"- Task: {manifest.get('task') or '(not set)'}",
        f"- Workflow execution status: `{manifest.get('status', 'unknown')}`",
        f"- Objective outcome: `{manifest.get('workflow_outcome', 'unknown')}`",
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
    manifest = {
        "task": task_summary,
        "status": "planning",
        "current_step": None,
        "steps": [],
        "history": [],
        "updated_at": workflow_now(),
    }
    return sync_workflow_outcome(manifest)


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
    normalize_manifest(manifest)
    validate_manifest(manifest)
    return sync_workflow_outcome(manifest), plan_text


def normalize_manifest(manifest: dict[str, Any]) -> None:
    steps = manifest.get("steps")
    if not isinstance(steps, list):
        return

    for step in steps:
        if not isinstance(step, dict):
            continue
        implementation_summary = step.get("implementation_summary")
        if isinstance(implementation_summary, str):
            summary = implementation_summary.strip()
            step["implementation_summary"] = [summary] if summary else []


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
        "blocked",
    }
    workflow_outcome = manifest.get("workflow_outcome")
    if workflow_outcome is not None:
        if not isinstance(workflow_outcome, str) or workflow_outcome not in VALID_WORKFLOW_OUTCOME_STATUSES:
            raise WorkflowError(
                f"Plan manifest has unsupported workflow_outcome {workflow_outcome!r}. "
                f"Expected one of: {sorted(VALID_WORKFLOW_OUTCOME_STATUSES)}."
            )
    workflow_outcome_reason = manifest.get("workflow_outcome_reason")
    if workflow_outcome_reason is not None and not isinstance(workflow_outcome_reason, str):
        raise WorkflowError("Plan manifest workflow_outcome_reason must be a string when present.")

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
        if not all(isinstance(item, str) and item.strip() for item in verification):
            raise WorkflowError(f"Step {step_id} verification must be a list of non-empty strings.")

        acceptance_criteria = step.get("acceptance_criteria", [])
        if not isinstance(acceptance_criteria, list):
            raise WorkflowError(f"Step {step_id} acceptance_criteria must be a list.")
        if not all(isinstance(item, str) and item.strip() for item in acceptance_criteria):
            raise WorkflowError(f"Step {step_id} acceptance_criteria must be a list of non-empty strings.")

        implementation = step.get("implementation", [])
        if not isinstance(implementation, list):
            raise WorkflowError(f"Step {step_id} implementation must be a list.")
        if not all(isinstance(item, str) and item.strip() for item in implementation):
            raise WorkflowError(f"Step {step_id} implementation must be a list of non-empty strings.")

        outcome_status = step.get("outcome_status")
        if outcome_status is not None:
            valid_outcome_statuses = {"pass", "fail", "inconclusive"}
            if not isinstance(outcome_status, str) or outcome_status not in valid_outcome_statuses:
                raise WorkflowError(
                    f"Step {step_id} has unsupported outcome_status {outcome_status!r}. "
                    f"Expected one of: {sorted(valid_outcome_statuses)}."
                )

        outcome_reason = step.get("outcome_reason")
        if outcome_reason is not None and not isinstance(outcome_reason, str):
            raise WorkflowError(f"Step {step_id} outcome_reason must be a string when present.")

        blocks_downstream_on_fail = step.get(BLOCKING_OUTCOME_KEY)
        if blocks_downstream_on_fail is not None and not isinstance(blocks_downstream_on_fail, bool):
            raise WorkflowError(f"Step {step_id} {BLOCKING_OUTCOME_KEY} must be a boolean when present.")

        blocked_reason = step.get("blocked_reason")
        if blocked_reason is not None and not isinstance(blocked_reason, str):
            raise WorkflowError(f"Step {step_id} blocked_reason must be a string when present.")

        implementation_summary = step.get("implementation_summary")
        if implementation_summary is not None:
            if not isinstance(implementation_summary, list) or not all(
                isinstance(item, str) for item in implementation_summary
            ):
                raise WorkflowError(
                    f"Step {step_id} implementation_summary must be a list of strings when present."
                )


def compact_manifest_for_storage(manifest: dict[str, Any]) -> dict[str, Any]:
    compact = sync_workflow_outcome(copy.deepcopy(manifest))
    normalize_manifest(compact)

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
        if isinstance(step.get("outcome_reason"), str):
            step["outcome_reason"] = clip_text(step["outcome_reason"], APPROVED_STEP_SUMMARY_CHARS, from_end=True)
        if status in {"approved", "done"}:
            implementation_summary = step.get("implementation_summary")
            if isinstance(implementation_summary, list):
                step["implementation_summary"] = [
                    clip_text(str(item).strip(), RESULTS_SUMMARY_CHARS, from_end=True)
                    for item in implementation_summary
                    if str(item).strip()
                ]
            step["implementation"] = []
            step["verification"] = []
            objective = str(step.get("objective", "")).strip()
            if objective:
                step["objective"] = clip_text(objective, RESULTS_SUMMARY_CHARS)
        if isinstance(step.get("blocked_reason"), str):
            step["blocked_reason"] = clip_text(step["blocked_reason"], RESULTS_SUMMARY_CHARS, from_end=True)
    if isinstance(compact.get("workflow_outcome_reason"), str):
        compact["workflow_outcome_reason"] = clip_text(
            compact["workflow_outcome_reason"],
            RESULTS_SUMMARY_CHARS,
            from_end=True,
        )
    return compact


def save_plan_manifest(plan_path: Path, manifest: dict[str, Any], plan_text: str) -> None:
    del plan_text
    compact_manifest = compact_manifest_for_storage(manifest)
    validate_manifest(compact_manifest)
    compact_manifest["updated_at"] = workflow_now()
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


def block_pending_downstream_steps_after_gate(
    manifest: dict[str, Any],
    step_id: str,
    *,
    reason: str,
) -> list[str]:
    blocked: list[str] = []
    seen_gate = False
    for candidate in manifest.get("steps", []):
        if candidate.get("id") == step_id:
            seen_gate = True
            continue
        if not seen_gate:
            continue
        if candidate.get("status") != "pending":
            continue

        candidate_id = str(candidate.get("id", ""))
        # Failed gates should block expensive downstream runs, not the
        # investigation/remediation step that explains or fixes the failure.
        if is_auto_followup_step_id(candidate_id):
            continue

        candidate_text = " ".join(
            [
                candidate_id,
                str(candidate.get("title", "")),
                str(candidate.get("objective", "")),
            ]
        ).lower()
        if not any(token in candidate_text for token in ("eval", "evaluation", "benchmark")):
            continue

        candidate["status"] = "blocked"
        candidate["blocked_reason"] = reason
        blocked.append(candidate_id)
    return blocked


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
            "timestamp": workflow_now(),
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
            "timestamp": workflow_now(),
        }
    )
    save_plan_manifest(plan_path, manifest, plan_text)
    return manifest


def approve_step(
    plan_path: Path,
    step_id: str,
    review_summary: str,
    *,
    outcome_status: str = "pass",
    outcome_reason: str = "",
) -> dict[str, Any]:
    manifest, plan_text = load_plan_manifest(plan_path)
    step = get_step(manifest, step_id)
    step["status"] = "approved"
    step["review_summary"] = review_summary
    step["outcome_status"] = outcome_status
    step["outcome_reason"] = outcome_reason.strip()
    step["implementation_summary"] = [
        text
        for item in step.get("implementation", [])
        if (text := str(item).strip())
    ]
    manifest.setdefault("history", []).append(
        {
            "step_id": step_id,
            "event": "approved",
            "details": (
                f"{review_summary}\nOutcome: {outcome_status}"
                + (f" - {outcome_reason.strip()}" if outcome_reason.strip() else "")
            ),
            "timestamp": workflow_now(),
        }
    )

    should_add_followup = outcome_status == "fail" and not is_auto_followup_step_id(step_id)
    if should_add_followup:
        followup_step_id = _normalize_followup_step_id(step_id)
        existing_followup = next(
            (
                candidate
                for candidate in manifest["steps"]
                if candidate.get("id") == followup_step_id
            ),
            None,
        )
        if existing_followup is None:
            insert_at = next(
                (index for index, candidate in enumerate(manifest["steps"]) if candidate["id"] == step_id),
                len(manifest["steps"]) - 1,
            )
            followup_step = {
                "id": followup_step_id,
                "title": _default_followup_title(step),
                "status": "pending",
                "objective": _default_followup_objective(step),
                "acceptance_criteria": [
                    "Root cause or strongest hypothesis is documented with direct evidence.",
                    "A concrete remediation is implemented and verified, or the remaining blocker is clearly documented.",
                    "Results.md references the decisive artifacts used for the investigation.",
                ],
                "implementation": [
                    "Inspect the latest failed benchmark/evaluation artifacts.",
                    "Identify the likely cause of the failed outcome.",
                    "Implement a remediation or document why the failure remains unresolved.",
                ],
                "verification": [
                    "Confirm the investigation references the latest relevant artifacts.",
                    "Run the smallest validation that proves whether the remediation changed the failed outcome.",
                ],
            }
            manifest["steps"].insert(insert_at + 1, followup_step)
            manifest.setdefault("history", []).append(
                {
                    "step_id": followup_step_id,
                    "event": "followup_added",
                    "details": (
                        f"Automatically added follow-up step after approved outcome_status=fail on '{step_id}'."
                    ),
                    "timestamp": workflow_now(),
                }
            )
    elif outcome_status == "fail" and is_auto_followup_step_id(step_id):
        manifest.setdefault("history", []).append(
            {
                "step_id": step_id,
                "event": "followup_chain_stopped",
                "details": (
                    "Approved failed outcome on an automatically generated follow-up step; "
                    "not adding another nested follow-up. The unresolved objective failure "
                    "will remain visible in workflow_outcome and summary.md."
                ),
                "timestamp": workflow_now(),
            }
        )

    if outcome_status == "fail" and step_blocks_downstream_on_fail(step):
        gate_reason = (
            outcome_reason.strip()
            or review_summary.strip()
            or f"Step '{step_id}' failed and is configured to block downstream evaluations."
        )
        blocked_steps = block_pending_downstream_steps_after_gate(
            manifest,
            step_id,
            reason=(
                f"Blocked because gate step '{step_id}' produced outcome_status=fail. "
                f"Resolve or replan a remediation before running this downstream evaluation. "
                f"Failure signal: {clip_text(gate_reason, RESULTS_SUMMARY_CHARS, from_end=True)}"
            ),
        )
        if blocked_steps:
            manifest.setdefault("history", []).append(
                {
                    "step_id": step_id,
                    "event": "downstream_steps_blocked",
                    "details": (
                        "Blocked pending downstream evaluation/benchmark steps after failed gate: "
                        + ", ".join(blocked_steps)
                    ),
                    "timestamp": workflow_now(),
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
    current_status_lines.append(f"- **Workflow Execution Status:** `{manifest.get('status', 'unknown')}`")
    current_status_lines.append(f"- **Objective Outcome:** `{manifest.get('workflow_outcome', 'unknown')}`")
    workflow_outcome_reason = str(manifest.get("workflow_outcome_reason", "")).strip()
    if workflow_outcome_reason:
        current_status_lines.append(f"- **Objective Outcome Detail:** {workflow_outcome_reason}")

    if review is not None and latest_step is not None:
        latest_review_lines = [
            f"- **Step:** `{latest_step['id']}`",
            f"- **Approved:** `{str(review.approved).lower()}`",
            f"- **Outcome Status:** `{review.outcome_status}`",
            f"- **Rationale:** {review.summary}",
        ]
        if review.outcome_reason:
            latest_review_lines.append(f"- **Outcome Detail:** {review.outcome_reason}")
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
    elif review is not None and review.outcome_status != "pass":
        reason = review.outcome_reason or review.summary
        open_issues.append(f"- Latest approved step outcome is `{review.outcome_status}`: {reason}")
    elif active_step is not None:
        active_status = active_step.get("status")
        if active_status == "awaiting_review":
            open_issues.append(f"- Review for `{active_step['id']}` has not run yet.")
        elif active_status == "needs_changes":
            latest_failure = latest_history_event(manifest, step_id=active_step["id"])
            details = latest_failure.get("details") if latest_failure else None
            if details:
                open_issues.append(f"- {clip_history_details(details)}")
    open_issues.extend(
        issue for issue in unresolved_outcome_issue_lines(manifest) if issue not in open_issues
    )
    open_issues.extend(issue for issue in blocked_step_issue_lines(manifest) if issue not in open_issues)
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


def build_workflow_summary(
    paths: WorkflowPaths,
    *,
    summary_status: str = SUMMARY_STATUS_DONE,
    terminal_error: str | None = None,
    human_intervention_required: bool = False,
    human_intervention_reason: str | None = None,
) -> str:
    if summary_status not in VALID_SUMMARY_STATUSES:
        raise WorkflowError(
            f"Unsupported workflow summary status {summary_status!r}. "
            f"Expected one of: {sorted(VALID_SUMMARY_STATUSES)}."
        )

    manifest, _ = load_plan_manifest(paths.plan_md)
    active_step_id = manifest.get("current_step")
    active_step = get_step(manifest, active_step_id) if active_step_id else None
    workflow_outcome = str(manifest.get("workflow_outcome", "unknown"))
    workflow_outcome_reason = str(manifest.get("workflow_outcome_reason", "")).strip()

    achieved: list[str] = []
    implemented: list[str] = []
    remaining_issues: list[str] = []
    next_steps: list[str] = []

    approved_steps = [
        step for step in manifest.get("steps", []) if step.get("status") == "approved"
    ]
    for step in approved_steps:
        outcome_status = str(step.get("outcome_status", "")).strip()
        outcome_reason = str(step.get("outcome_reason", "")).strip()
        review_summary = str(step.get("review_summary", "")).strip()

        achieved_line = f"- `{step['id']}` ({step['title']})"
        if outcome_status:
            achieved_line += f" with outcome `{outcome_status}`"
        if outcome_reason:
            achieved_line += f": {clip_text(outcome_reason, RESULTS_SUMMARY_CHARS, from_end=True)}"
        elif review_summary:
            achieved_line += f": {clip_text(review_summary, RESULTS_SUMMARY_CHARS, from_end=True)}"
        achieved.append(achieved_line)

        for item in step.get("implementation_summary", []):
            text = str(item).strip()
            if text:
                implemented.append(
                    f"- `{step['id']}`: {clip_text(text, RESULTS_SUMMARY_CHARS, from_end=True)}"
                )

    if terminal_error:
        remaining_issues.append(
            f"- Workflow stopped with terminal error: "
            f"{clip_text(terminal_error, MANIFEST_HISTORY_DETAIL_CHARS, from_end=True)}"
        )

    latest_review = latest_history_event(manifest, events={"approved", "changes_requested"})
    if latest_review is not None and latest_review.get("event") == "changes_requested":
        details = str(latest_review.get("details", "")).strip()
        if details:
            remaining_issues.append(
                f"- Latest review requested changes for `{latest_review.get('step_id', 'unknown')}`: "
                f"{clip_text(details, MANIFEST_HISTORY_DETAIL_CHARS, from_end=True)}"
            )

    remaining_issues.extend(unresolved_outcome_issue_lines(manifest))
    remaining_issues.extend(blocked_step_issue_lines(manifest))

    for step in manifest.get("steps", []):
        status = step.get("status")
        if status == "approved":
            continue
        if status in {"pending", "needs_changes", "in_progress", "awaiting_review", "blocked"}:
            remaining_issues.append(
                f"- `{step['id']}` ({step['title']}) is still `{status}`."
            )

    if summary_status == SUMMARY_STATUS_DONE:
        next_steps.append("- No further workflow action is required.")
        next_steps.append(
            f"- Inspect `{paths.results_md.name}` and `{paths.plan_md.name}` for the final detailed record."
        )
    elif summary_status == SUMMARY_STATUS_BLOCKED or human_intervention_required:
        reason = (human_intervention_reason or terminal_error or "").strip()
        if reason:
            next_steps.append(
                "- Human intervention is required before the workflow can continue: "
                + clip_text(reason, MANIFEST_HISTORY_DETAIL_CHARS, from_end=True)
            )
        else:
            next_steps.append("- Human intervention is required before the workflow can continue.")
        if active_step is not None:
            next_steps.append(
                f"- After intervention, resume from `{active_step['id']}` ({active_step['title']})."
            )
        next_steps.append(
            f"- Review `{paths.progress_md.name}`, `{paths.results_md.name}`, and `{paths.plan_md.name}` before resuming."
        )
    elif summary_status == SUMMARY_STATUS_INTERRUPTED:
        if active_step is not None:
            next_steps.append(
                f"- Resume from `{active_step['id']}` ({active_step['title']}) when ready."
            )
        next_steps.append(
            f"- Review `{paths.progress_md.name}`, `{paths.results_md.name}`, and `{paths.plan_md.name}` before resuming."
        )
    elif active_step is not None:
        next_steps.append(f"- Resume from `{active_step['id']}` ({active_step['title']}).")
        next_steps.append(
            f"- Review `{paths.progress_md.name}`, `{paths.results_md.name}`, and `{paths.plan_md.name}` before resuming."
        )
    else:
        next_steps.append("- Inspect the workflow manifest and results to determine the next action.")

    return "\n".join(
        [
            "# Workflow Summary",
            "",
            "## Final Status",
            "",
            f"- Workflow status: `{summary_status}`",
            f"- Manifest status: `{manifest.get('status', 'unknown')}`",
            f"- Objective outcome: `{workflow_outcome}`",
            (
                f"- Objective outcome detail: {workflow_outcome_reason}"
                if workflow_outcome_reason
                else "- Objective outcome detail: not recorded."
            ),
            f"- Current step: `{manifest.get('current_step') or 'none'}`",
            "",
            "## Achieved",
            *(achieved or ["- No approved steps were recorded."]),
            "",
            "## Implemented",
            *(implemented or ["- No implementation items were recorded in approved steps."]),
            "",
            "## Remaining Issues",
            *(remaining_issues or ["- None recorded."]),
            "",
            "## Next Steps",
            *(next_steps or ["- None recorded."]),
        ]
    ) + "\n"
