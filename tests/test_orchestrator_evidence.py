from __future__ import annotations

import unittest

import orchestrator


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


if __name__ == "__main__":
    unittest.main()
