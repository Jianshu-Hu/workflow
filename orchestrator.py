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
  - acceptance_criteria: list of concrete conditions that define acceptable outcome for the step
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
- Inspect `{paths.migration_md.name}` too when it exists.
- If the existing manifest and {paths.progress_md.name} disagree, prefer the more recent concrete execution evidence in {paths.results_md.name} and reconcile the plan.
- If the latest review rejected a step but did not require human intervention, update the plan so the next loop iteration attempts a concrete fix instead of repeating the same failed action blindly.
- Preserve already approved steps unless the evidence shows they are invalid.
- If remediation requires debugging the workflow itself, add explicit repair or diagnostic steps rather than treating the issue as a permanent external blocker.
- Prefer workflow-owned helper scripts and artifacts under the workflow workspace when automation glue is needed.
- If a failure is caused by missing public checkpoints, datasets, assets, or packages and the repository already documents or scripts how to acquire them, treat that as workflow work. Add an explicit prerequisite-staging or cache-population step instead of immediately requiring human intervention.
- For large or slow prerequisite downloads, prefer idempotent, resumable staging steps that materialize stable shared paths or caches before the expensive evaluation step runs.
- Avoid modifying tracked files inside submodules unless there is no viable workflow-local or repository-local alternative.
- Only leave the workflow blocked on human intervention if the latest evidence shows a permission, credential, quota, unavailable external resource with no workflow-controlled acquisition path, or operator-owned environment change that cannot be solved from this repository.
{migration_requirements}

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
        raise WorkflowError("Planner command returned empty output.")
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
    verification_lines = "\n".join(f"- {item}" for item in step.get("verification", [])) or "- None listed"
    acceptance_lines = "\n".join(f"- {item}" for item in step.get("acceptance_criteria", [])) or "- None listed"
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

Required behavior:
- Work only on step `{step['id']}`.
- Read `{paths.progress_md}` first and use it to avoid redoing already completed work.
- Use `{paths.repo_root}` as the repository working root when you run commands or edit files.
- Treat submodule-owned areas as read-only by default, and prefer creating helper scripts or artifacts under `{paths.root}` when you need workflow-specific glue.
- Before concluding that the host GPU, renderer, or environment is broken, compare your own execution environment against the parent workflow snapshot above. If they differ, treat that as a workflow-launch or sandbox mismatch and fix the workflow/scripts/config so future runs use the same environment as the parent workflow shell.
- Make the necessary repository changes.
- Ensure the acceptance criteria for this step are satisfied, or clearly record why they are not satisfied.
- Run the verification listed for this step.
- If the first attempt fails but the failure appears fixable from this repository, repair the issue and rerun verification within the same step instead of stopping at the first error.
- If verification fails because a public prerequisite is missing but the repository contains a documented or scriptable way to fetch or materialize it, implement that prerequisite staging in the workflow and rerun instead of treating it as immediate operator work.
- Prefer committed, idempotent setup helpers over one-off shell history. If a prerequisite is large, make the setup resumable and cache-aware so later workflow runs do not repeat the download or extraction.
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

Return JSON only with this schema:
{{
  "approved": true or false,
  "outcome_status": "pass" | "fail" | "inconclusive",
  "outcome_reason": "short explanation of the outcome status",
  "summary": "short review summary",
  "required_changes": ["change 1", "change 2"],
  "human_intervention_required": true or false,
  "human_intervention_reason": "short reason or empty string"
}}

Approve only if the step is implemented, verified, and evaluated against its acceptance criteria well enough to move on.
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
        failure_summary = summarize_command_failure(
            paths,
            stage="executor_contract",
            message=(
                "Executor command completed but did not append the required step section to "
                f"`{paths.results_md.name}` with heading `## {step_results_heading}`."
            ),
            result=result,
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

    return StepResult(
        approved=approved,
        summary=summary or "No review summary provided.",
        required_changes=[str(item) for item in required_changes],
        raw_output=stripped,
        outcome_status=outcome_status,
        outcome_reason=outcome_reason.strip(),
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
