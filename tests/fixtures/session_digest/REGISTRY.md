# Session-digest fixture registry (ADR-0124 Phase 0, FRE-947)

**Pre-registered.** This file and every fixture it lists were written and committed
**before** the producer was tuned and **before** any evaluation arm was run. ADR-0124's
fixture discipline: sets are selected by a stated rule and written down in advance,
never chosen after seeing output. A criterion evaluated on a post-hoc sample has not
been met.

---

## 1. Corpus feasibility (required before any criterion naming a sample size)

Counted 2026-07-23 against the live graph (`cloud-sim-neo4j`) and the on-disk capture
store the producer actually reads (`telemetry/captains_log/captures`).

| Population | Count |
|---|---:|
| `Session` nodes in the graph | 121 |
| ...multi-turn (`turn_count >= 2`) | 59 |
| ...carrying the legacy `session_summary` | 121 |
| ...carrying `summary_generated_at` (pre-deploy) | 0 |
| **Graph multi-turn sessions with ANY capture on disk** | **0** |
| Capture files on disk | 1,541 |
| Distinct `session_id`s on disk | 553 |
| ...multi-turn | 13 |
| ...multi-turn **and** carrying tool results | 2 |
| ...multi-turn **and** carrying tool **arguments** | 0 |

The graph numbers reproduce ADR-0124's Context table exactly (121 / 59), which is why
the ADR's own estimate of evaluation population looked workable.

**The finding that changes the answer:** the ADR reasoned about the *graph*
population, but the producer reads *captures from disk*, and those are two different
populations. Retention has purged the captures for **every one of the 59 multi-turn
sessions in the graph** — the intersection is empty. The on-disk store holds a
different, mostly single-turn set (553 sessions, 13 multi-turn, 2 with tool results,
0 with tool arguments, since arguments only began being captured in this ticket).

**Consequences, stated rather than worked around:**

1. **The real corpus cannot supply AC-9, AC-10, AC-12 or AC-13.** AC-9 alone wants
   ≥5 multi-turn sessions across different tools, each holding a decision-relevant
   fact present only in tool output; there are 2 candidate sessions in total, and
   neither carries tool arguments. AC-12 wants ≥6 Tier-A contradictions and ≥4 Tier-B
   evidenced self-corrections, which do not exist in a 13-session pool.
2. ADR-0124 anticipates exactly this: *"If the corpus cannot supply it, that is a
   finding to surface, not a reason to shrink the criterion — the permitted response
   is a pre-registered synthetic supplement, labelled as such in the result."* Every
   set below is therefore **synthetic and labelled as such**, and no criterion has
   been shrunk to fit.
3. **On deploy, the first sweep will digest nothing.** It will find 121 dirty
   sessions, read zero captures for each, and mark them clean with no digest. That is
   correct — a session whose evidence is gone cannot be digested, and its legacy
   `session_summary` is preserved (D-d) — but it means digests appear only for
   sessions created *after* deploy. The sweep counts `no_captures` separately from
   `skipped` so this is visible rather than reading as a successful floor application.

---

## 2. Selection rule

Real sessions were **exhaustively** examined (all 553 on-disk `session_id`s, all 59
graph multi-turn ids) rather than sampled — the population is small enough that
sampling would add noise without reducing work. Having established the intersection
is empty, the sets below are hand-authored to the per-class minima ADR-0124 states,
with each item's ground truth fixed here before any arm ran.

Synthetic sessions are written as real `TaskCapture` records so they traverse the
exact production path — `build_prompt`, the parser, and
`validate_digest_provenance` — with no test-only shortcut.

---

## 3. The sets

### `ac8_input_completeness.json` — AC-8
Three sessions exercising the input dimensions AC-8 names: a multi-result turn, a
failed call, and a long assistant response. Includes one gate-blocked and one
malformed-argument invocation, which exist in a capture only after this ticket's
capture-completeness fix.

*Ground truth:* the assembled prompt must contain every turn, the full untruncated
user and assistant text of each, and for every invocation its name, arguments, status,
error and payload.

### `ac9_tool_only_facts.json` — AC-9
**5 sessions**, one per distinct tool: `query_elasticsearch`, `read_file`,
`web_search`, `search_memory`, `system_metrics_snapshot`. Each contains exactly one
decision-relevant fact present **only** in tool output and never restated in the
assistant text — so a narration-only producer cannot reproduce it.

*Ground truth:* `expected_fact` per session, plus the locator it must be citable at.
Facts are chosen to be consequential for the session outcome, so marginal-utility
filtering is not a legitimate reason to omit them.

*Threshold:* the digest reproduces the fact in **all 5**.

### `ac10_basis_labelling.json` — AC-10
**40 items** across 8 sessions, labelled with their true `basis`, deliberately
balanced so no single value can dominate: 10 `tool_evidence`, 10 `user_statement`,
10 `assistant_reasoning`, 10 `mixed`.

*Thresholds:* agreement with labelled truth **≥85%**, and **no single emitted tag
exceeding 60%** of emissions. The balanced ground truth is what makes the second
threshold meaningful — a collapse onto one value cannot be excused as matching a
skewed truth.

### `ac12_corrections.json` — AC-12
**22 cases**, ground truth fixed per case.

*Positives (10):*
- **6 Tier-A** direct contradictions — authoritative evidence contradicts the same
  proposition the assistant asserted.
- **4 Tier-B** evidenced self-corrections — the assistant corrected the record within
  the session, with evidence in the capture.

*Negatives (12 Tier-C)*, spanning the full range D3 names — and each is a case a
careless producer would plausibly flag:
- 3 weak/partial conflict
- 3 failed or incomplete tool calls
- 2 ambiguous readings
- 2 legitimately changed state
- 2 disagreement with a subjective judgment

*Thresholds:* **zero** negatives yield a correction (precision is absolute), **≥80%**
of positives do, and every emitted Tier-B correction carries the located span of its
**supporting evidence**, not merely of the self-correction sentence.

### `ac13_missing_evidence.json` — AC-13
The fixture **triple**, on captures with deliberately incomplete records:
1. `payload_absent` — the only possible contradiction lives in a payload missing from
   the capture. Must yield **no** correction.
2. `status_visible` — the contradiction is visible in tool **status/error**. Must
   yield **one**.
3. `self_correction` — an explicit evidenced self-correction in the session's own
   text. Must yield **one**.

Both directions matter: a producer that invents contradictions from gaps fails (1),
and one that goes mute whenever any evidence is missing fails (2) and (3).

---

## 4. What these sets do not prove

AC-11's located-span validation is a **necessary** condition, not a sufficient one: it
proves a citation resolves, not that the span *supports* the proposition. A fabricated
item citing a real but irrelevant span at a valid locator passes it. Semantic support
is carried by AC-12's labelled cases here and by AC-16's human review in Phase 1.
Nothing in this registry should be read as closing that gap.

Because these sets are synthetic, they measure whether the producer *can* do the job
on well-formed evidence. They do not measure its behaviour on the messy real corpus,
which is unavailable at Phase 0 and is exactly what Phase 1's human review exists to
supply once post-deploy sessions accumulate.
