from __future__ import annotations

import unittest
import tempfile
from pathlib import Path
from unittest import mock

import orchestrator
from utils.common import WorkflowPaths
from utils.manifest import build_workflow_summary, render_plan_document


class ExecutorEvidenceValidationTest(unittest.TestCase):
    def test_command_evidence_allows_missing_artifact_path_marker(self) -> None:
        step = {
            "acceptance_criteria": ["dry run completes"],
            "verification": ["python plan_eval.py --dry_run"],
        }
        section = """
### Acceptance Evidence

- AC1: pass - dry run completed without a shape error.

### Verification Evidence

- V1: pass - command: `python plan_eval.py --dry_run`; working directory: `/repo`; exit code: 0; artifact path: not applicable; result: printed `summary: ok`.

### Changed Files

- `plan_eval.py` - updated frame-buffer handling.

### Outcome

pass - Verification completed.
"""

        self.assertEqual(orchestrator.validate_executor_evidence(section, step), [])

    def test_not_run_evidence_without_terminal_blocker_is_rejected(self) -> None:
        step = {
            "acceptance_criteria": ["dry run completes"],
            "verification": ["python plan_eval.py --dry_run"],
        }
        section = """
### Acceptance Evidence

- AC1: inconclusive - dry run status is unknown.

### Verification Evidence

- V1: inconclusive - command: `python plan_eval.py --dry_run`; not run yet.

### Changed Files

- No files changed.

### Outcome

inconclusive - Verification still needs to be run.
"""

        issues = orchestrator.validate_executor_evidence(section, step)
        self.assertIn(
            "Evidence contains incomplete-verification language such as skipped, not run, not applicable, or not tested without a terminal gate/blocker decision and concrete evidence.",
            issues,
        )


class ReviewJsonParsingTest(unittest.TestCase):
    def test_parse_review_json_accepts_json_with_trailing_text(self) -> None:
        raw_output = """
{"approved": true, "outcome_status": "pass", "outcome_reason": "ok", "summary": "done", "required_changes": [], "human_intervention_required": false, "human_intervention_reason": ""}
extra reviewer commentary
"""

        result = orchestrator.parse_review_json(raw_output)

        self.assertTrue(result.approved)
        self.assertEqual(result.outcome_status, "pass")
        self.assertEqual(result.summary, "done")

    def test_parse_review_json_reports_unparseable_json_as_workflow_error(self) -> None:
        raw_output = "reviewer said: {not valid json}"

        with self.assertRaises(orchestrator.WorkflowError) as ctx:
            orchestrator.parse_review_json(raw_output)

        self.assertIn("Reviewer output did not contain parseable JSON", str(ctx.exception))


class CanonicalContractPromptTest(unittest.TestCase):
    def test_planner_prompt_requires_canonical_declared_contract_checks(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            paths = WorkflowPaths(root=root)
            _write_minimal_workspace(paths)

            prompt = orchestrator.build_planner_prompt(paths, {})

            self.assertIn("Canonical contract requirements for declared external contracts", prompt)
            self.assertIn("canonical local reader, writer, validator, public API, or downstream command", prompt)
            self.assertIn("A file-presence check is not sufficient", prompt)

    def test_executor_prompt_rejects_bypassing_canonical_consumer(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            paths = WorkflowPaths(root=root)
            _write_minimal_workspace(paths)
            manifest = _minimal_manifest()
            step = manifest["steps"][0]

            prompt = orchestrator.build_codex_prompt(paths, manifest, step)

            self.assertIn("Canonical contract requirements for declared external contracts", prompt)
            self.assertIn("bypassing the canonical consumer", prompt)
            self.assertIn("mark the step failed or inconclusive", prompt)

    def test_review_prompt_rejects_superficial_declared_contract_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            paths = WorkflowPaths(root=root)
            _write_minimal_workspace(paths)
            manifest = _minimal_manifest()
            paths.plan_md.write_text(render_plan_document(manifest), encoding="utf-8")

            with mock.patch.object(orchestrator, "select_relevant_lessons", return_value=[]):
                prompt = orchestrator.build_review_prompt(paths, manifest["steps"][0])

            self.assertIn("Canonical contract requirements for declared external contracts", prompt)
            self.assertIn("Reject if the step claims compatibility with a declared external contract", prompt)
            self.assertIn("Reject if downstream code was changed to bypass the canonical consumer/API", prompt)


class WorkflowSummaryRenderingTest(unittest.TestCase):
    def test_done_workflow_with_failed_objective_mentions_unresolved_state(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            paths = WorkflowPaths(root=root)
            manifest = {
                "task": "test task",
                "status": "done",
                "current_step": None,
                "workflow_outcome": "fail",
                "workflow_outcome_reason": "Approved steps remain unresolved with outcome fail: full-robotwin-eval.",
                "steps": [
                    {
                        "id": "full-robotwin-eval",
                        "title": "Full RoboTwin environment evaluation",
                        "status": "approved",
                        "outcome_status": "fail",
                        "outcome_reason": "Evaluation completed without crashes, but success rate remained 0.0.",
                        "implementation_summary": [],
                    }
                ],
            }
            root.joinpath("plan.md").write_text(render_plan_document(manifest), encoding="utf-8")

            summary = build_workflow_summary(paths, summary_status="done")
            self.assertIn("The workflow is complete, but the objective remains `fail`.", summary)
            self.assertIn("Review `progress.md`, `results.md`, and `plan.md`", summary)
            self.assertNotIn("No further workflow action is required.", summary)


def _minimal_manifest() -> dict:
    return {
        "task": "produce an artifact with a declared external contract",
        "status": "pending",
        "current_step": "produce-artifact",
        "workflow_outcome": "pending",
        "workflow_outcome_reason": "",
        "steps": [
            {
                "id": "produce-artifact",
                "title": "Produce artifact",
                "status": "pending",
                "objective": "Produce an artifact compatible with a declared external contract.",
                "acceptance_criteria": ["Artifact loads through the canonical consumer API"],
                "implementation": ["Implement the producer"],
                "verification": ["Run a canonical consumer smoke test"],
            }
        ],
    }


def _write_minimal_workspace(paths: WorkflowPaths) -> None:
    paths.root.mkdir(parents=True, exist_ok=True)
    paths.prompts_dir.mkdir(parents=True, exist_ok=True)
    paths.artifacts_dir.mkdir(parents=True, exist_ok=True)
    paths.command_artifacts_dir.mkdir(parents=True, exist_ok=True)
    paths.task_md.write_text("Produce an artifact for a declared external contract.\n", encoding="utf-8")
    paths.discussion_md.write_text("The requested output must be compatible with the canonical consumer.\n", encoding="utf-8")
    paths.results_md.write_text("# Workflow Results\n", encoding="utf-8")
    paths.progress_md.write_text("# Workflow Progress\n", encoding="utf-8")
    paths.artifact_index_json.write_text('{"artifacts": []}\n', encoding="utf-8")
    paths.state_json.write_text("{}\n", encoding="utf-8")
    paths.plan_md.write_text(render_plan_document(_minimal_manifest()), encoding="utf-8")


if __name__ == "__main__":
    unittest.main()
