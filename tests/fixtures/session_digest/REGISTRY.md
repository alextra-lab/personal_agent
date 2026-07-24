# Session-digest fixture registry (ADR-0124 Phase 0, FRE-947 → FRE-953 / Amendment A →
FRE-956 / Amendment B)

**Pre-registered.** This file and every fixture it lists were written and committed
**before** the producer was tuned and **before** any evaluation arm was run. ADR-0124's
fixture discipline: sets are selected by a stated rule and written down in advance,
never chosen after seeing output. A criterion evaluated on a post-hoc sample has not
been met.

**Amendment A (FRE-953) update.** The producer is now built from the conversation, not
from tool payloads. Consequently: **AC-8** additionally asserts payloads/arguments are
*absent* from the prompt; **AC-9 is withdrawn** (a tool-only fact is deliberately no
longer reproduced — its fixture is deleted); the **AC-12 positives are rebuilt as
self_correction only** (the six payload-fed contradiction cases are invalidated and removed,
the self_correction set grown to eight; the payload-free `status_contradiction` kind is
exercised by AC-13); **AC-13 is unchanged**; **AC-10 is deferred**
to an owner-led redesign (its fixture is payload-derived and invalidated — kept for that
rework, not run by the amended arm). The eight new AC-12 positives were written down here
and committed before the amended arm was run.

**Amendment B (FRE-956) update.** Tool metadata (name/status/error), not only payloads,
is removed from the producer's input entirely; `tool_evidence` and `status_contradiction`
are retired from the schema. Consequently: **AC-8** additionally asserts *zero* tool
metadata (not only payloads/arguments) reaches the prompt, plus a positive control — a
tool name the user themselves typed survives; **AC-10 is un-deferred** (nothing
tool-sourced remains to label, so the fixture is rebuilt over the three conversation
bases and runs by default again); **AC-12's positives are rebuilt so ALL EIGHT** cite the
assistant's own text for both the claim and its supporting evidence — the locator grammar
is `assistant_text` only, so the four cases that used to cite `user_text` are restructured
around the assistant restating the correcting fact in its own reply; **AC-13 drops the
`status_visible` case**, reducing the fixture from a triple to a pair.

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

1. **The real corpus cannot supply AC-10, AC-12 or AC-13.** AC-12 (amended) wants ≥8
   evidenced self-corrections and ≥12 Tier-C negatives, which do not exist in a
   13-session pool; AC-10 and AC-13 are likewise unsupplied. (AC-9's corpus problem is
   moot — the criterion is withdrawn.)
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

### `ac8_input_completeness.json` — AC-8 *(amended by Amendment B)*
Four sessions exercising the input dimensions AC-8 names: a multi-result turn, a
failed call, a long assistant response, and (new in Amendment B) a positive control.
Includes one gate-blocked and one malformed-argument invocation.

*Ground truth (Amendment B):* the assembled prompt must contain every turn, the full
untruncated user and assistant text of each, and **must NOT contain any tool name,
status, error, payload or argument** — Amendment A only withheld payloads/arguments;
Amendment B withholds the metadata too. The absence direction is the one that catches a
regression back to feeding tool data to the generator, so the scorer asserts it both
structurally (no `output:`/`arguments:` block, no `Tool invocations` header) and by
value (no tool name/error/payload/argument token leaks). The fourth case,
`user_typed_tool_name`, is the criterion's stated positive control: a tool name the user
themselves typed in prose is legitimate conversation content and must survive. The
fixture captures still carry tool results in full (storage is unaffected); the
criterion is entirely about what reaches the prompt.

### `ac9_tool_only_facts.json` — AC-9 *(WITHDRAWN — fixture deleted)*
Withdrawn by Amendment A: it required a fact present only in tool output to reach the
digest, which the amendment deliberately prevents (an unnarrated fact is not part of
the conversation and so is not the user's memory). The fixture is removed and the
criterion must not be evidenced.

### `ac10_basis_labelling.json` — AC-10 *(un-deferred by Amendment B)*
**42 items** across 9 sessions, labelled with their true `basis`, balanced 14 per value
across the three conversation bases (`user_statement`, `assistant_reasoning`, `mixed`).
**Rebuilt for Amendment B:** the retired `tool_evidence` spec row is dropped entirely —
with nothing tool-sourced left to label, the payload/tool-derived fixture problem that
deferred this criterion under Amendment A dissolves, and `mixed` is redefined as a
conversation-only combination (the assistant blends what the user said with its own
reasoning, no tool output anywhere in the input). The fixture runs by default again.

### `ac12_corrections.json` — AC-12 *(amended by Amendment B)*
**20 cases**, ground truth fixed per case.

*Positives (8 `self_correction`)* — the assistant corrected the record within the
conversation, and **both** the claim and its supporting evidence are cited from the
assistant's own t2 message (Amendment B narrows the locator grammar to `assistant_text`
only — the user's own message is no longer a valid locator target):
- **4** where the assistant's own corrective message re-narrates, in its own words, a
  tool error it previously observed (the tool call is still captured/stored for realism
  but is never cited — the citable text is the assistant's restatement);
- **4** where the correcting fact originated with the user, but the assistant's
  corrective reply explicitly restates it in its own words, and that restatement — not
  the user's original message — is what is cited.

Each positive carries a `reference_correction` — a hand-authored span/locator +
evidence-span/locator that resolves, both fields `assistant_text` — pre-validated
offline by `tests/personal_agent/memory/test_session_digest_validator.py` before any
paid run, so an un-citable positive cannot silently turn into an `errored` case.

*Negatives (12 Tier-C)*, unchanged, spanning the full range D3 names — each a case a
careless producer would plausibly flag:
- 3 weak/partial conflict
- 3 failed or incomplete tool calls
- 2 ambiguous readings
- 2 legitimately changed state
- 2 disagreement with a subjective judgment

*Thresholds:* **zero** negatives yield a correction (precision is absolute), **≥80%**
of positives do, and every emitted `self_correction` carries the located span of its
**supporting evidence**, not merely of the self-correction sentence.

### `ac13_missing_evidence.json` — AC-13 *(reduced to a pair by Amendment B)*
The fixture **pair** — Amendment B removes the `status_visible` case, since
`status_contradiction` is retired to the verification oracle — on captures with
deliberately incomplete records:
1. `payload_absent` — the only possible contradiction lives in a payload missing from
   the capture (and is tool-derived in any case). Must yield **no** correction.
2. `self_correction` — an explicit evidenced self-correction, entirely in the
   assistant's own text. Must yield **one**.

Both directions matter: a producer that invents contradictions from gaps fails (1), and
one that goes mute whenever any evidence is missing fails (2).

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
