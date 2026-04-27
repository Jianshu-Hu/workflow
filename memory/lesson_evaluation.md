# Workflow Lesson Evaluation Protocol

Use this file when a workflow run proposes a reusable workflow-level lesson in
`lesson_candidates.yaml`, or when a human wants to promote a `confidence: 0`
candidate from `workflow/memory/lessons.yaml`.

The goal is to prevent hallucinated, overgeneralized, or weakly supported lessons
from becoming global workflow memory.

Lesson candidates should normally come from a concrete workflow event that showed
the reminder was needed: an implementation correction, review miss, workflow
mechanism gap, rerun repair, stopped/interrupted run that required human
intervention before rerun, direct human intervention, or a non-obvious constraint
discovered through failure. Do not promote lessons merely because an executor
implemented a step correctly on the first try or followed ordinary best practices.

## Confidence Scale

- `0`: candidate/init. Stored for review, not injected into future prompts.
- `1` to `10`: active confidence. Relevant lessons are injected into planner,
  executor, and reviewer prompts.
- `-1` to `-5`: rejected. Kept as an audit record, never injected.

Promotion requires independent Codex, Gemini, Claude, and human review.
Increasing confidence after activation requires evidence that the lesson helped
in a later workflow, followed by another review round.

## Human Workflow

1. Copy the candidate lesson and all cited evidence paths into a review packet.
2. Ask Codex, Gemini, and Claude to review the same packet independently.
3. Do not show one AI review to another reviewer until all three initial reviews
   are complete.
4. Compare their decisions and concerns.
5. If any reviewer rejects the lesson, either revise it and restart review, keep
   it at `confidence: 0`, or mark it rejected with `confidence: -1..-5`.
6. If all three AI reviewers approve and the human approves, either keep it as
   an audited candidate at `confidence: 0` or promote it to `confidence: 1`.
7. Promote above `1` only after a future workflow shows concrete usefulness and
   the promotion is reviewed again by Codex, Gemini, Claude, and human.

## Review Packet Template

```markdown
# Lesson Candidate Review Packet

## Candidate

```yaml
<paste one lesson candidate here>
```

## Source Workflow

- Workspace: `<workflow_runs/...>`
- Step(s): `<step ids if known>`
- Results file: `<path>`
- Progress file: `<path>`
- Discussion file: `<path>`

## Evidence To Inspect

- `<path>`: `<claim this artifact supports>`
- `<path>`: `<claim this artifact supports>`

## Human Question

Should this candidate become workflow-level memory?
If yes, what confidence should it have initially?
Use the numeric confidence scale:

- `0`: keep as candidate only
- `1`: activate as low-confidence advisory
- `-1..-5`: reject
```

## AI Review Prompt

Send the following prompt separately to Codex, Gemini, and Claude.

```text
You are reviewing a proposed workflow-level lesson for a coding/research workflow.

Your job is to decide whether this lesson is evidence-backed, scoped correctly,
actionable for future workflows, triggered by a concrete correction/repair/intervention
event, and falsifiable. Be conservative. Do not approve the lesson merely because
it sounds plausible.

Review rules:
- Treat project-specific handoff facts as unsuitable for global workflow memory.
- Approve only lessons that can help future unrelated runs.
- Reject or request revision if the lesson lacks an artifact-backed `trigger_event`
  and valid `reason_category`. Allowed reason categories:
  `implementation_correction`, `review_gap`, `workflow_mechanism_gap`,
  `rerun_repair`, `stopped_run_human_rerun`, `human_intervention`,
  `non_obvious_constraint_discovered_by_failure`.
- Reject or request revision if the lesson comes only from a successful first-pass
  implementation or ordinary best practice.
- Reject or request revision if the lesson overgeneralizes from one run.
- Reject or request revision if evidence does not directly support the claim.
- Reject or request revision if applies_when, does_not_apply_when, required_checks,
  or falsification_conditions are vague or missing.
- Keep confidence at 0 unless the lesson should actively influence future runs.
- Use confidence 1 only for a low-confidence active advisory after approval.
- Use negative confidence for rejected lessons.

Return YAML only with this schema:

reviewer: codex|gemini|claude
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
```

## Human Decision Template

After collecting all three AI reviews, record the final human decision beside
the lesson in `workflow/memory/lessons.yaml` or in the workspace-local
`lesson_candidates.yaml`.

```yaml
reviews:
  codex:
    decision: pending
    recommended_confidence: 0
    rationale: ""
  gemini:
    decision: pending
    recommended_confidence: 0
    rationale: ""
  claude:
    decision: pending
    recommended_confidence: 0
    rationale: ""
  human:
    decision: pending
    confidence: 0
    rationale: ""
```

## Promotion Checklist

Before setting confidence to `1` or higher:

- The lesson has concrete cited evidence.
- The lesson has a valid `reason_category` and artifact-backed `trigger_event`.
- The lesson is cross-run workflow knowledge, not just project state.
- The lesson has clear `applies_when` and `does_not_apply_when` scope.
- The lesson has actionable `required_checks`.
- The lesson has explicit `falsification_conditions`.
- Codex review approves.
- Gemini review approves.
- Claude review approves.
- Human review approves.

Before increasing confidence above `1`:

- A later workflow used the lesson.
- The lesson prevented, detected, or clarified a real issue.
- The usage event is recorded in `usage_events`.
- Codex, Gemini, Claude, and human reviewed the promotion.
