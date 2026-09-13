# FRE-1325 — show the reader when a delivered answer carries unsourced assertions

Ticket: [FRE-1325](https://linear.app/frenchforest/issue/FRE-1325) · Backing: ADR-0138 D3/D4
(the contract), ADR-0101 §6 / FRE-690 (the deterministic disclosure path this reuses).

## Decision (AC-4)

A turn that verification ran on, and that has one or more non-exempt spans that did not pass,
gets one system-written line appended to the reply:

> Note: N of M factual statements in this answer are not backed by a source Seshat verified
> this turn. Check them before you rely on them.

Why this surface:

- The reply is the point of reading. `service/app.py:439` takes `result["reply"]` from
  `execute_task_safe` and pushes it as one `TEXT_DELTA` (`app.py:460`), then persists the same
  string. The HTTP path and the CLI read the same field. So the line reaches the PWA live, on
  reload, and the CLI, with no PWA change and no new event type.
- The line uses the existing `Note:` disclosure format (ADR-0101 §6). It is built from the turn's
  own verification record, never generated.
- **Placement (revised after codex plan review):** `step_synthesis` stores the line on
  `ctx.grounding_disclosure`. `execute_task_safe` appends it to `result["reply"]`. That happens
  after `execute_task` writes the capture from `ctx.final_reply`, so `TaskCapture.assistant_response`
  and memory consolidation never read the system-written line as model output. The capture
  already carries the same fact structurally in `grounding`. An errored turn gets no line.

What it does **not** cover (to be stated in the handoff):

- It is not blocking. A reader can ignore it. The confabulation problem is made visible, not
  solved. `enforce` stays gated on FRE-1316 / FRE-1328 and the D5 amendment.
- It fires on tool-driven turns even when the answer is correct, because D2 makes tool results
  uncitable (FRE-1328). The wording says "not backed by a source Seshat verified", which is true
  in both cases. It does not say "wrong".
- A turn that verification could not run on (`unavailable`) gets no line. That turn is
  unmeasured. The line covers every measured turn with one or more failed spans, not only
  0-of-N turns.
- The persisted reply carries the line, so the next turn's model context contains it as
  ordinary assistant text. Verification does not re-check history. The model can imitate the
  line. This is accepted: the attachment disclosures behave the same way, and separating display
  text from model context needs a persistence and PWA change outside this ticket.
- A stopped turn (`turn_stopped_early`) and `mode == "off"` get no line. Verification does not
  run there.
- `enforce` never gets the line: `decide` delivers only compliant or unavailable turns, and the
  terminal no-source statement already says so.
- No WARNING log with span texts (span texts are user content), no insights anomaly.

## Rule

Add the line iff all hold:

1. `mode == "observe"` (the only mode that delivers a verified turn with failures),
2. `verification.available`,
3. `verification.failures` is non-empty.

N = `len(verification.failures)`, M = `len(verification.spans)`. Keyed on the delivered
verification, not on `first_generation_compliant`, which is the metric's numerator (it also
turns false on a retried turn) and is not a property of the text the reader sees.

## Steps

1. **Failing tests** — `tests/personal_agent/orchestrator/test_executor_grounding.py`:
   - `test_observe_a_zero_of_nine_turn_carries_the_unsourced_note` (AC-1): patch
     `executor._verify_grounding` to return a `TurnVerification` with 9 `UNCITED` spans (three
     carry the quoted spans from trace `dba5b2cba1e0bece6c8b9396465a265c`). Assert the reply
     starts with the original text and ends with `Note: 9 of 9 factual statements …`.
   - `test_observe_a_fully_cited_turn_carries_no_note` (AC-2 seeded negative): the real
     citation path from `test_enforce_delivers_a_turn_that_verified`, run under `observe`.
     Assert `final_reply == f"{CLAIM}."` and `first_generation_compliant is True`.
   - `test_observe_a_turn_with_no_assertions_carries_no_note` (AC-2): zero spans.
   - `test_observe_partial_counts`: 3 spans, 1 passed → `2 of 3`.
   - Update `test_observe_mode_records_the_failure_and_still_delivers` (line 149): the reply
     now ends with `1 of 1`.
   - Existing `unavailable`, `off`, enforce-terminal, enforce-deliver tests stay as they are and
     prove no line there. Add `assert "Note:" not in ctx.final_reply` to the terminal test.
   - Run: `make test-file FILE=tests/personal_agent/orchestrator/test_executor_grounding.py`
     → new tests fail.
2. **Implement** in `src/personal_agent/orchestrator/executor.py`:
   - a private pure helper `_unsourced_assertion_disclosure(verification: TurnVerification)
     -> str | None`;
   - `ExecutionContext.grounding_disclosure: str | None` in `orchestrator/types.py`;
   - in `step_synthesis`, set it in the grounding block when `mode == "observe"`;
   - in `execute_task_safe`, append `Note: <line>` to `result["reply"]` when it is set and
     `ctx.error is None`.
3. **Verify** the test file passes, then `make test`, `make mypy`, `make ruff-check`,
   `make ruff-format`, `pre-commit run --all-files`.
4. **AC-3 check**: `git diff origin/main...HEAD --stat` lists no `grounding/verification.py`,
   `grounding/extractor.py`, `grounding/spans.py`, or `grounding/compliance.py`.

## Acceptance criteria

| AC | Evidence |
|----|----------|
| AC-1 0-of-N distinguishable at reading | 9/9 replay test; reply string is what `app.py:460` pushes |
| AC-2 compliant turn shows nothing | fully-cited and no-assertion tests assert the reply is unchanged |
| AC-3 no change to verification/extraction/metric | diff stat |
| AC-4 decision + non-coverage stated | this document + handoff comment |

## Post-deploy

Deploy class: `seshat-gateway` rebuild (master). Live check needs an owner turn (never fire a live
gateway turn without an explicit OK): a turn that asserts facts from a `bash` result shows the
note; a greeting shows none.
