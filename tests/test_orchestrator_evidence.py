from __future__ import annotations

import unittest
import tempfile
from pathlib import Path

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


if __name__ == "__main__":
    unittest.main()
