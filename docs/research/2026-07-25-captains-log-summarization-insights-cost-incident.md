# Incident — background-process cost spiral: Captain's Log, Summarization, Insights

**Date:** 2026-07-25
**Status:** Contained. All development halted; investigation open (FRE-987).
**Scope:** Three background streams sharing the `captains_log` budget lineage.

---

## 1. Summary

Three scheduled background processes — **Captain's Log reflection**, **Summarization
(session digest)**, and **Insights** — consumed more model budget over 14 days than all
user-driven inference, while producing almost no delivered output.

The proximate trigger was a non-convergent retry loop in the Summarization sweep, live
from 2026-07-23. The loop is bounded today only because a budget cap denies it.

| | 14-day spend | Delivered output (14d) |
|---|---|---|
| `captains_log` (2 processes) | **$10.52** | 6 digests written · 2 proposals merged |
| `main_inference` (user turns) | $8.77 | — |
| `artifact_builder` | $1.30 | — |
| `entity_extraction` | $1.21 | — |
| `skill_routing` | $0.11 | — |
| `insights` | **unmeasurable** | — |

Background processes: **$11.84+**. User-driven: **$10.07**.

---

## 2. Timeline

| Date | Event |
|---|---|
| ≤ 2026-07-22 | `captains_log` daily spend $0.00–$0.10 |
| 2026-07-23 | Spend jumps to **$2.76** (~100×). FRE-947 (ADR-0124 digest producer rewrite, incl. idle sweep) is the change landing in this window |
| 2026-07-24 | $2.51. Daily cap raised $2.50 → $5.00, sized against the pre-incident peak of $2.76. First denials appear (64) |
| 2026-07-25 | **$5.02** — 100.3% of cap. 259 denials. Cap becomes the binding constraint |
| 2026-07-25 20:37Z | Owner halts all development. Kill switch engaged |
| 2026-07-25 | Cap-raise PR (#677, $5→$10) **withdrawn** — see §6 |

30-day total spend $25.51, of which **$15.35 (60%) occurred in the final three days**.

---

## 3. The three streams

### 3.1 Summarization (session digest) — ADR-0124

**Purpose.** Produce a per-session digest for the session browser: a conversation-only
summary of what a session contained.

**Mechanism.** An idle sweep runs every `session_summary_sweep_interval_seconds = 300`.
Each pass calls `find_dirty_idle_sessions()` and, for each session returned, regenerates
the digest **wholesale** — `capture.py` states this explicitly as `f(canonical captures)`
rather than `f(previous digest, delta)`, justified as self-correcting: "a bad generation
is fixed by the next sweep".

**Billing.** `second_brain/session_summary.py:447` — `budget_role="captains_log"`.
Note the model role *was* split out as `session_summary` by FRE-947; the **cost** split
was explicitly deferred (ADR-0124 D2), and the code comment says so.

**Output, 14 days.**

```
session_summary_started              1164
session_summary_failed               1098   (94% failure)
session_summary_generated              66
session_digest_written                  6   ← delivered artifacts
session_digest_view_parse_failed       78   ← written digests that cannot be read back
session_summary_no_captures_on_disk   101   ← source data absent
session_summary_skipped_below_floor   112
```

**1,164 attempts → 6 digests written.**

### 3.2 Captain's Log reflection → proposals

**Purpose.** Self-improvement: reflect on turns, generate improvement proposals, feed the
Linear feedback loop.

**Mechanism.** Per-session cadence floor of
`captains_log_reflection_min_interval_seconds = 1800` (FRE-710).

**Billing.** `captains_log/feedback.py:316` — `budget_role="captains_log"`.

**Output, 14 days.**

```
dspy_reflection_succeeded                              32
insights_captains_log_proposals_created                75
insights_proposal_suppressed_by_read_before_emit       85
reflection_proposal_suppressed_by_read_before_emit     22
graph_quality_anomaly_proposal_suppressed_by_read...   15
captains_log_proposal_merged                            2   ← delivered
```

**75 proposals created, 122 suppression events, 2 merged.**

### 3.3 Insights

**Purpose.** Analyse feedback (`captains_log/feedback.py:274`).

**Billing.** `budget_role="insights"`.

**Governance status.** `insights` is declared in `budget.yaml`'s roles section (with an
`on_denial` behaviour) but has **no daily and no weekly cap**. Because the cost gate only
maintains counters for capped roles, there is **no counter for `insights`**, and a query
for cost events tagged `role=insights` returns zero documents.

**Its spend is therefore currently unknown and unmeasurable.** This is a measurement gap,
not a claim that it is large or small.

---

## 4. Root cause of the spiral

The Summarization sweep is **convergent on success and divergent on failure**.

```
success → SET s.summary_generated_at → dirty predicate false → session drops out → DONE
failure → session left dirty and eligible → re-selected next pass → forever
```

The intended escape hatch is in `find_dirty_idle_sessions()`:

```cypher
AND NOT (s.summary_failure_reason IN $terminal_reasons
         AND coalesce(s.summary_attempt_count, 0) >= $max_attempts)
```

Exclusion requires **both** a terminal reason **and** the attempt ceiling. The docstring
states the design intent: *"Transient reasons (a budget denial above all) are never
terminal, so those sessions keep coming back until they succeed."*

`budget_denied` is classified transient. The first conjunct is therefore never satisfied,
and **the attempt ceiling is unreachable regardless of the counter's value**.

**Evidence.** Over 24 hours: 358 started, 358 failed, 0 succeeded, across **5 distinct
sessions**. The attempt counter increments correctly and monotonically, reaching **311**
against a configured `session_summary_max_attempts = 2`. One session is re-selected on
essentially every sweep pass.

**Failure reasons (24h):** `budget_denied` 303, `empty_output` 55. The `empty_output`
attempts reached the model, consumed budget, returned nothing usable, and were retried.

**Two design choices intersect to make this expensive:**

1. **Wholesale regeneration** — every retry costs a full session summarisation. The
   self-correction rationale assumes the next sweep is cheap; it is not.
2. **"Transient" without a ceiling** — a reason that recurs deterministically is
   indistinguishable from a permanent one, but is retried without bound.

**The budget cap is not the cause and cannot be the fix.** Raising it produces more
billable regenerations before exhaustion; removing it allows unbounded spend. The cap
converted unbounded spend into unbounded churn.

---

## 5. Cost-governance findings

1. **Two processes, one budget role.** Summarization and Captain's Log reflection both
   bill to `captains_log`. They have different purposes, cadences, consumers and cost
   shapes. Until this investigation they were indistinguishable in every cost figure; the
   split was deliberately deferred by ADR-0124 D2.

2. **Three declared roles are uncapped** — `insights`, `promotion`, `freshness`.

3. **`captains_log` has no weekly cap**, therefore no weekly counter. Current weekly
   `_total` is $20.20; the named per-role weekly counters sum to $9.74. The **$10.46
   difference — 52% of weekly spend — is unattributed in that view**.

4. **Two different defaults for un-passed `budget_role`.**
   `llm_client/factory.py:158` defaults to `skill_routing`;
   `llm_client/litellm_client.py:296` documents a default of `main_inference`.

5. **673 of 994 budget denials (7d) carry no `budget_role` field**, so they cannot be
   attributed to a process.

6. **No anomaly detection.** A ~100× cost increase ran for three days without alerting.
   Caps enforce; nothing notices a change in shape.

---

## 6. Actions taken

- **All development halted** 2026-07-25 20:37Z. Kill switch `telemetry/dispatch.disabled`
  written (read by both `orchestrator.py:160` and `gating_watcher.py:181`, so dispatch and
  PR-gating are both gated). Both build seats idle; both worktrees clean, nothing
  uncommitted.
- **PR #677 withdrawn** (raise `captains_log` daily $5 → $10). The sizing had been derived
  from spend-plus-denials treated as legitimate demand; once the denials were shown to be
  one non-convergent loop, that figure was unsound. Raising the cap would have funded the
  loop at double the rate.
- **Cap left at $5.00**, which is currently the only effective bound on the loop.
- **FRE-987** filed (Urgent) with the root cause.

---

## 7. Open questions for the per-stream study

Each stream will be studied separately, objective first, in dedicated sessions.

**Summarization**
- Should a finished session's digest be a one-shot terminal operation rather than a
  continuously-refreshed projection? Sessions can resume, which is the stated reason for
  the projection model.
- What is the cost per delivered digest, and what is that worth?
- Why do 78 written digests fail to parse on read, and why are 101 sweeps run against
  sessions whose captures are absent from disk?

**Captain's Log reflection**
- What is the intended proposal yield, against 75 created / 122 suppressed / 2 merged?
- Is `read_before_emit` suppression working as designed, or discarding valid output after
  it has already been paid for?

**Insights**
- What does it cost? Currently unmeasurable.
- Should it be capped, and against what?

**Cross-cutting**
- Does the unbounded-transient-retry pattern exist in other sweeps or consumers sharing
  this predicate shape?
- Should the budget role be the blast-radius boundary — i.e. one budget per process rather
  than per lineage?

---

## 8. References

- FRE-987 (root cause, Urgent) · FRE-947 (ADR-0124 producer rewrite) · FRE-710
  (reflection cadence) · PR #677 (withdrawn)
- ADR-0124 (session summary), D1 and D2 · ADR-0040 lineage (Captain's Log)
- `config/governance/budget.yaml` · `second_brain/session_summary.py` ·
  `memory/service.py` (`find_dirty_idle_sessions`) · `captains_log/capture.py` ·
  `captains_log/feedback.py`
- MASTER_PLAN §0b — records prod digests as budget-denied; this incident is that cause.
