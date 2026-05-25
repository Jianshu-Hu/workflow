# Workflow Memory

This folder stores global workflow-level lessons and the process for evaluating
them. These memories are cross-run guidance for the workflow itself; normal
project handoff state should move through workspace files and migration.

## Files

- `lessons.yaml`: curated global workflow lessons.
- `lesson_evaluation.md`: detailed protocol for reviewing lesson candidates.
- `evaluate_lesson.py`: implementation used by `workflow/scripts/evaluate_lesson.sh`.
- `karpathy-code-instructions.md`: coding guidance that can be referenced by workflow prompts.

## Confidence Scale

- `0`: candidate/init. Stored for review, but not injected into planner,
  executor, or reviewer prompts.
- `1` to `10`: active confidence. Relevant lessons are selected by trigger
  terms and scope, then injected into prompts.
- `-1` to `-5`: rejected. Kept as an audit record, never injected.

## When A Lesson Is Valid

The reviewer can propose new workflow-level lessons only when a concrete run
event exposed a reusable failure mode or a non-obvious constraint.

Valid trigger categories:

- `implementation_correction`
- `review_gap`
- `workflow_mechanism_gap`
- `rerun_repair`
- `stopped_run_human_rerun`
- `human_intervention`
- `non_obvious_constraint_discovered_by_failure`

Successful first-pass implementations and ordinary best practices should not
become lesson candidates.

Reviewer proposals are written to the workspace-local `lesson_candidates.yaml`
with `confidence: 0`, a `reason_category`, and an artifact-backed
`trigger_event`. They are not added to global active memory automatically.

## Human Processing Flow

1. Inspect `lesson_candidates.yaml` and the cited artifacts.
2. Run `workflow/scripts/evaluate_lesson.sh <workflow-run-folder>` to ask Codex,
   Gemini, and Claude to independently review the lesson.
3. If any reviewer objects, revise the candidate or mark it rejected with
   negative confidence.
4. If all reviewers and the human approve, copy the lesson into
   `workflow/memory/lessons.yaml` at `confidence: 0`, or promote it to
   `confidence: 1` if it should become an active low-confidence advisory.
5. Increase confidence only after a future run shows the lesson was useful, then
   repeat Codex, Claude, Gemini, and human review before promotion.

The evaluator writes raw prompts, raw model outputs, parsed review summaries,
and `human_review.md` under `<workflow-run-folder>/lesson_reviews/`.

Useful options:

```bash
# Review one candidate.
bash workflow/scripts/evaluate_lesson.sh <workflow-run-folder> --lesson-id <id>

# Verify report generation without calling model CLIs.
bash workflow/scripts/evaluate_lesson.sh <workflow-run-folder> --dry-run
```

See `lesson_evaluation.md` for the review packet template, AI review prompt,
human decision template, and promotion checklist.
