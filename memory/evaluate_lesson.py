#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import re
import shlex
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError:  # pragma: no cover - exercised only in minimal environments.
    yaml = None


REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW_ROOT = REPO_ROOT / "workflow"
DEFAULT_OUTPUT_CHARS = 24000
DEFAULT_EVIDENCE_CHARS = 12000
DEFAULT_TIMEOUT_SECONDS = 1800
ALLOWED_REASON_CATEGORIES = (
    "implementation_correction",
    "review_gap",
    "workflow_mechanism_gap",
    "rerun_repair",
    "stopped_run_human_rerun",
    "human_intervention",
    "non_obvious_constraint_discovered_by_failure",
)


@dataclass
class AgentSpec:
    name: str
    command_template: str
    model: str


@dataclass
class AgentResult:
    agent: str
    returncode: int
    prompt_path: Path
    stdout_path: Path
    stderr_path: Path
    parsed_review: dict[str, Any] | None
    parse_error: str


def require_yaml() -> None:
    if yaml is None:
        raise SystemExit(
            "PyYAML is required to evaluate lesson candidates. Install PyYAML in the workflow environment."
        )


def utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def clip_text(text: str, max_chars: int, *, from_end: bool = False) -> str:
    if len(text) <= max_chars:
        return text
    marker = "\n...[clipped]...\n"
    keep = max(0, max_chars - len(marker))
    if from_end:
        return marker + text[-keep:]
    return text[:keep] + marker


def load_yaml_file(path: Path) -> dict[str, Any]:
    require_yaml()
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise SystemExit(f"YAML file must contain a mapping: {path}")
    return data


def yaml_dump(data: Any) -> str:
    require_yaml()
    return yaml.safe_dump(data, sort_keys=False, allow_unicode=False).strip()


def resolve_workspace(raw_workspace: str) -> Path:
    workspace = Path(raw_workspace).expanduser()
    if not workspace.is_absolute():
        workspace = (REPO_ROOT / workspace).resolve()
    else:
        workspace = workspace.resolve()
    if not workspace.exists():
        raise SystemExit(f"Workflow run folder does not exist: {workspace}")
    if not workspace.is_dir():
        raise SystemExit(f"Workflow run path is not a directory: {workspace}")
    return workspace


def load_candidates(workspace: Path, lesson_id: str | None = None) -> list[dict[str, Any]]:
    candidates_path = workspace / "lesson_candidates.yaml"
    if not candidates_path.exists():
        raise SystemExit(
            f"No lesson candidate file found at {candidates_path}. "
            "Run a workflow review that proposes lesson_candidates first, or create this file manually."
        )
    data = load_yaml_file(candidates_path)
    candidates = data.get("lesson_candidates", [])
    if not isinstance(candidates, list):
        raise SystemExit(f"`lesson_candidates` must be a list in {candidates_path}")

    normalized = [item for item in candidates if isinstance(item, dict)]
    if lesson_id:
        normalized = [item for item in normalized if str(item.get("id", "")) == lesson_id]
        if not normalized:
            raise SystemExit(f"No lesson candidate with id {lesson_id!r} found in {candidates_path}")
    if not normalized:
        raise SystemExit(f"No lesson candidates found in {candidates_path}")
    return normalized


def candidate_source_paths(candidate: dict[str, Any], workspace: Path) -> list[tuple[Path, str]]:
    paths: list[tuple[Path, str]] = []

    source = candidate.get("source", {})
    if isinstance(source, dict):
        for key in ("results", "progress", "plan", "discussion"):
            value = source.get(key)
            if isinstance(value, str) and value.strip():
                paths.append((resolve_reference_path(value, workspace), f"source.{key}"))

    evidence = candidate.get("evidence", [])
    if isinstance(evidence, list):
        for index, item in enumerate(evidence, start=1):
            if isinstance(item, dict):
                path_value = item.get("path")
                claim = str(item.get("claim", f"evidence {index}"))
                workspace_value = item.get("workspace")
                base = resolve_reference_path(str(workspace_value), REPO_ROOT) if workspace_value else workspace
                if isinstance(path_value, str) and path_value.strip():
                    paths.append((resolve_reference_path(path_value, base), claim))
            elif isinstance(item, str):
                match = re.search(r"([A-Za-z0-9_./-]+\.(?:md|json|yaml|yml|txt|log))", item)
                if match:
                    paths.append((resolve_reference_path(match.group(1), workspace), item))

    for default_name in ("task.md", "discussion.md", "progress.md", "results.md"):
        path = workspace / default_name
        if path.exists():
            paths.append((path, f"default workspace context: {default_name}"))

    deduped: list[tuple[Path, str]] = []
    seen: set[Path] = set()
    for path, claim in paths:
        try:
            resolved = path.resolve()
        except OSError:
            resolved = path
        if resolved in seen:
            continue
        seen.add(resolved)
        deduped.append((resolved, claim))
    return deduped


def resolve_reference_path(raw_path: str, base: Path) -> Path:
    path = Path(raw_path).expanduser()
    if path.is_absolute():
        return path
    if (base / path).exists():
        return (base / path)
    if (REPO_ROOT / path).exists():
        return (REPO_ROOT / path)
    return base / path


def render_evidence_bundle(paths: list[tuple[Path, str]], max_chars: int) -> str:
    sections: list[str] = []
    for path, claim in paths:
        sections.extend(
            [
                f"### {path}",
                "",
                f"Claim or relevance: {claim}",
                "",
            ]
        )
        if not path.exists():
            sections.extend(["Artifact status: missing", ""])
            continue
        if not path.is_file():
            sections.extend(["Artifact status: not a file", ""])
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            sections.extend([f"Artifact read error: {exc}", ""])
            continue
        sections.extend(
            [
                "```text",
                clip_text(text, max_chars, from_end=True),
                "```",
                "",
            ]
        )
    return "\n".join(sections).strip()


def build_review_prompt(candidate: dict[str, Any], workspace: Path, evidence_bundle: str, agent: str) -> str:
    allowed_categories = ", ".join(ALLOWED_REASON_CATEGORIES)
    return f"""You are reviewing a proposed workflow-level lesson for a coding/research workflow.

Reviewer identity: {agent}

Your job is to decide whether this lesson is evidence-backed, scoped correctly,
actionable for future workflows, triggered by a concrete correction/repair/intervention
event, and falsifiable. Be conservative. Do not approve the lesson merely because
it sounds plausible.

Review rules:
- Treat project-specific handoff facts as unsuitable for global workflow memory.
- Approve only lessons that can help future unrelated runs.
- Reject or request revision if the lesson lacks an artifact-backed `trigger_event`
  and valid `reason_category`. Allowed reason categories: {allowed_categories}.
- Reject or request revision if the lesson comes only from a successful first-pass
  implementation or ordinary best practice.
- Reject or request revision if the lesson overgeneralizes from one run.
- Reject or request revision if evidence does not directly support the claim.
- Reject or request revision if applies_when, does_not_apply_when, required_checks,
  or falsification_conditions are vague or missing.
- Keep confidence at 0 unless the lesson should actively influence future runs.
- Use confidence 1 only for a low-confidence active advisory after approval.
- Use negative confidence for rejected lessons.
- Do not edit files. Review only the candidate and evidence below.

Return YAML only with this schema:

reviewer: {agent}
decision: approve|revise|reject
recommended_confidence: <integer from -5 to 1>
evidence_assessment:
  supported_claims:
    - <claim supported by cited evidence>
  unsupported_claims:
    - <claim not sufficiently supported>
scope_assessment:
  overgeneralization_risks:
    - <risk or "none">
  missing_scope_limits:
    - <missing limit or "none">
actionability_assessment:
  useful_future_checks:
    - <check>
  vague_or_unactionable_parts:
    - <part or "none">
falsifiability_assessment:
  adequate: true|false
  concerns:
    - <concern or "none">
required_edits:
  - <edit or "none">
final_rationale: >
  <short rationale>

Workflow run folder:
```text
{workspace}
```

Lesson candidate:
```yaml
{yaml_dump(candidate)}
```

Evidence bundle:
```markdown
{evidence_bundle or "No evidence artifacts were found or readable."}
```
"""


def default_agent_specs() -> list[AgentSpec]:
    return [
        AgentSpec(
            name="codex",
            command_template=os.environ.get(
                "WORKFLOW_LESSON_CODEX_CMD",
                "bash {repo_root}/workflow/scripts/run_codex_executor.sh {prompt_file}",
            ),
            model=os.environ.get("WORKFLOW_LESSON_CODEX_MODEL", ""),
        ),
        AgentSpec(
            name="gemini",
            command_template=os.environ.get(
                "WORKFLOW_LESSON_GEMINI_CMD",
                "bash {repo_root}/workflow/scripts/run_gemini_noninteractive.sh {prompt_file} lesson {model}",
            ),
            model=os.environ.get(
                "WORKFLOW_LESSON_GEMINI_MODEL",
                os.environ.get("WORKFLOW_GEMINI_MODEL", "gemini-3.1-pro-preview"),
            ),
        ),
        AgentSpec(
            name="claude",
            command_template=os.environ.get(
                "WORKFLOW_LESSON_CLAUDE_CMD",
                "bash {repo_root}/workflow/scripts/run_claude_noninteractive.sh {prompt_file} lesson {model}",
            ),
            model=os.environ.get("WORKFLOW_LESSON_CLAUDE_MODEL", os.environ.get("WORKFLOW_CLAUDE_MODEL", "sonnet")),
        ),
    ]


def parse_command_template(template: str, *, prompt_file: Path, workspace: Path, model: str) -> list[str]:
    formatted = template.format(
        prompt_file=str(prompt_file),
        workspace=str(workspace),
        repo_root=str(REPO_ROOT),
        model=model,
    )
    return shlex.split(formatted)


def run_agent(
    spec: AgentSpec,
    *,
    prompt: str,
    workspace: Path,
    output_dir: Path,
    timeout: int,
    dry_run: bool,
) -> AgentResult:
    prompt_path = output_dir / f"{spec.name}_prompt.txt"
    stdout_path = output_dir / f"{spec.name}_review.yaml"
    stderr_path = output_dir / f"{spec.name}_stderr.txt"
    prompt_path.write_text(prompt, encoding="utf-8")

    if dry_run:
        stdout_path.write_text(
            yaml_dump(
                {
                    "reviewer": spec.name,
                    "decision": "revise",
                    "recommended_confidence": 0,
                    "final_rationale": "Dry run: no agent command was executed.",
                }
            )
            + "\n",
            encoding="utf-8",
        )
        stderr_path.write_text("", encoding="utf-8")
        parsed, parse_error = parse_review_output(stdout_path.read_text(encoding="utf-8"))
        return AgentResult(spec.name, 0, prompt_path, stdout_path, stderr_path, parsed, parse_error)

    command = parse_command_template(spec.command_template, prompt_file=prompt_path, workspace=workspace, model=spec.model)
    env = os.environ.copy()
    if spec.name == "codex":
        env.setdefault("WORKFLOW_CODEX_SANDBOX", "read-only")
        env.setdefault("WORKFLOW_CODEX_BYPASS_APPROVALS", "0")

    try:
        result = subprocess.run(
            command,
            cwd=str(REPO_ROOT),
            env=env,
            text=True,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
        stdout_path.write_text(result.stdout, encoding="utf-8")
        stderr_path.write_text(result.stderr, encoding="utf-8")
        parsed, parse_error = parse_review_output(result.stdout)
        return AgentResult(spec.name, result.returncode, prompt_path, stdout_path, stderr_path, parsed, parse_error)
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout if isinstance(exc.stdout, str) else ""
        stderr = exc.stderr if isinstance(exc.stderr, str) else ""
        stdout_path.write_text(stdout, encoding="utf-8")
        stderr_path.write_text((stderr + f"\nTimed out after {timeout} seconds.\n").strip() + "\n", encoding="utf-8")
        return AgentResult(spec.name, 124, prompt_path, stdout_path, stderr_path, None, "timeout")
    except OSError as exc:
        stdout_path.write_text("", encoding="utf-8")
        stderr_path.write_text(str(exc) + "\n", encoding="utf-8")
        return AgentResult(spec.name, 127, prompt_path, stdout_path, stderr_path, None, str(exc))


def parse_review_output(raw_output: str) -> tuple[dict[str, Any] | None, str]:
    require_yaml()
    text = raw_output.strip()
    if not text:
        return None, "empty output"
    fence = re.search(r"```(?:yaml|yml)?\s*(.*?)```", text, re.DOTALL | re.IGNORECASE)
    if fence:
        text = fence.group(1).strip()
    try:
        parsed = yaml.safe_load(text)
    except Exception as exc:  # noqa: BLE001 - preserve parse error text for human review.
        return None, str(exc)
    if not isinstance(parsed, dict):
        return None, "parsed output is not a mapping"
    return parsed, ""


def render_summary_report(
    *,
    workspace: Path,
    candidate: dict[str, Any],
    output_dir: Path,
    results: list[AgentResult],
    evidence_paths: list[tuple[Path, str]],
) -> str:
    lines = [
        "# Lesson Candidate Evaluation",
        "",
        f"- Workspace: `{workspace}`",
        f"- Candidate id: `{candidate.get('id', 'unknown')}`",
        f"- Output directory: `{output_dir}`",
        f"- Generated at: `{datetime.now(timezone.utc).isoformat()}`",
        "",
        "## Candidate",
        "",
        "```yaml",
        yaml_dump(candidate),
        "```",
        "",
        "## Review Summary",
        "",
        "| Reviewer | Command Exit | Parse | Decision | Recommended Confidence | Raw Output |",
        "|---|---:|---|---|---:|---|",
    ]
    for result in results:
        parsed = result.parsed_review or {}
        decision = str(parsed.get("decision", "unparsed"))
        recommended = parsed.get("recommended_confidence", "")
        parse_status = "ok" if result.parsed_review is not None and not result.parse_error else f"failed: {result.parse_error}"
        lines.append(
            "| {agent} | {code} | {parse_status} | {decision} | {recommended} | `{raw}` |".format(
                agent=result.agent,
                code=result.returncode,
                parse_status=parse_status.replace("|", "\\|"),
                decision=decision.replace("|", "\\|"),
                recommended=recommended,
                raw=result.stdout_path,
            )
        )

    lines.extend(
        [
            "",
            "## Suggested Human Action",
            "",
            suggested_human_action(results),
            "",
            "## Evidence Paths",
            "",
        ]
    )
    if evidence_paths:
        for path, claim in evidence_paths:
            lines.append(f"- `{path}`: {claim}")
    else:
        lines.append("- No evidence paths were found.")

    for result in results:
        lines.extend(
            [
                "",
                f"## {result.agent} Review",
                "",
                f"- Prompt: `{result.prompt_path}`",
                f"- Stdout: `{result.stdout_path}`",
                f"- Stderr: `{result.stderr_path}`",
                f"- Exit code: `{result.returncode}`",
                "",
            ]
        )
        if result.parsed_review:
            lines.extend(["```yaml", yaml_dump(result.parsed_review), "```"])
        else:
            raw = result.stdout_path.read_text(encoding="utf-8", errors="replace") if result.stdout_path.exists() else ""
            lines.extend(
                [
                    "Reviewer output could not be parsed as YAML.",
                    "",
                    "```text",
                    clip_text(raw, DEFAULT_OUTPUT_CHARS, from_end=True),
                    "```",
                ]
            )

    lines.extend(
        [
            "",
            "## Human Decision Template",
            "",
            "```yaml",
            "reviews:",
            "  codex:",
            "    decision: pending",
            "    recommended_confidence: 0",
            "    rationale: \"\"",
            "  gemini:",
            "    decision: pending",
            "    recommended_confidence: 0",
            "    rationale: \"\"",
            "  claude:",
            "    decision: pending",
            "    recommended_confidence: 0",
            "    rationale: \"\"",
            "  human:",
            "    decision: pending",
            "    confidence: 0",
            "    rationale: \"\"",
            "```",
            "",
        ]
    )
    return "\n".join(lines)


def suggested_human_action(results: list[AgentResult]) -> str:
    if any(result.returncode != 0 or result.parsed_review is None for result in results):
        return (
            "At least one agent failed or returned unparseable YAML. Inspect the raw outputs, "
            "repair the candidate or prompt if needed, and rerun evaluation before promotion."
        )
    decisions = [str(result.parsed_review.get("decision", "")).lower() for result in results if result.parsed_review]
    if all(decision == "approve" for decision in decisions):
        return (
            "All AI reviewers approved. Human may keep confidence at 0 for audit-only storage, "
            "or promote to confidence 1 as a low-confidence active advisory after checking the evidence."
        )
    if any(decision == "reject" for decision in decisions):
        return (
            "At least one AI reviewer rejected the lesson. Human should revise and rerun review, "
            "keep it at confidence 0, or mark it rejected with confidence -1..-5."
        )
    return (
        "At least one AI reviewer requested revision. Human should apply required edits and rerun "
        "independent evaluation before activating the lesson."
    )


def evaluate_candidate(
    *,
    workspace: Path,
    candidate: dict[str, Any],
    timeout: int,
    dry_run: bool,
) -> Path:
    candidate_id = str(candidate.get("id", "lesson-candidate"))
    safe_id = re.sub(r"[^A-Za-z0-9_.-]+", "-", candidate_id).strip("-") or "lesson-candidate"
    output_dir = workspace / "lesson_reviews" / f"{utc_stamp()}_{safe_id}"
    output_dir.mkdir(parents=True, exist_ok=False)

    evidence_paths = candidate_source_paths(candidate, workspace)
    evidence_bundle = render_evidence_bundle(evidence_paths, DEFAULT_EVIDENCE_CHARS)
    (output_dir / "evidence_bundle.md").write_text(evidence_bundle + "\n", encoding="utf-8")
    (output_dir / "candidate.yaml").write_text(yaml_dump(candidate) + "\n", encoding="utf-8")

    results: list[AgentResult] = []
    for spec in default_agent_specs():
        prompt = build_review_prompt(candidate, workspace, evidence_bundle, spec.name)
        results.append(
            run_agent(
                spec,
                prompt=prompt,
                workspace=workspace,
                output_dir=output_dir,
                timeout=timeout,
                dry_run=dry_run,
            )
        )

    report_path = output_dir / "human_review.md"
    report_path.write_text(
        render_summary_report(
            workspace=workspace,
            candidate=candidate,
            output_dir=output_dir,
            results=results,
            evidence_paths=evidence_paths,
        ),
        encoding="utf-8",
    )
    return report_path


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Evaluate workflow-level lesson candidates with Codex, Gemini, and Claude.")
    parser.add_argument("workspace", help="Workflow run folder containing lesson_candidates.yaml.")
    parser.add_argument("--lesson-id", default="", help="Evaluate only one candidate id from lesson_candidates.yaml.")
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT_SECONDS, help="Per-agent timeout in seconds.")
    parser.add_argument("--dry-run", action="store_true", help="Write prompts and a sample report without calling agents.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    workspace = resolve_workspace(args.workspace)
    lesson_id = args.lesson_id.strip() or None
    candidates = load_candidates(workspace, lesson_id=lesson_id)

    report_paths: list[Path] = []
    for candidate in candidates:
        report_paths.append(
            evaluate_candidate(
                workspace=workspace,
                candidate=candidate,
                timeout=args.timeout,
                dry_run=args.dry_run,
            )
        )

    print("Lesson evaluation reports:")
    for path in report_paths:
        print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
