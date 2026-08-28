# Entity class fail-open: the "100%" premise does not survive a wider window

**Ticket:** FRE-1013 · **Date:** 2026-08-28 · **Author:** build2 session (Sonnet)
**Related:** FRE-997 (the signal that opened this ticket) · FRE-995 §5.1/§8.3 (the audit that first
flagged this coercion as unmeasured) · FRE-996 (the digest structured-output pilot) · ADR-0115 D4
(fail-open, unchanged here) · ADR-0109 (entity **type**, a different axis — not this ticket's subject)
**Status:** measurement + one precisely-scoped observability fix. No schema/contract change shipped —
the evidence does not support one yet (see §4).

---

## 1. What the ticket asked

FRE-1013 opened on a 7-event / 2-turn / one-session sample (2026-07-26, the day the FRE-997 signal
shipped): every entity's `class` field came back empty, 7/7, and was fail-defaulted to `World`. The
ticket explicitly flagged its own sample as too small to generalize, and asked three things to be
settled **before** choosing a fix:

1. The rate of the empty-`class` default, over a window wide enough to be meaningful.
2. Whether `class` is even present in the prompt the extractor sends.
3. (Post-fix criterion) Whether entities in the live graph carry more than one distinct `class` value.

## 2. Is `class` present in the prompt?

**Yes — and has been since before the 7-event sample.** `entity_extraction.py`'s
`_EXTRACTION_PROMPT_TEMPLATE` carries a dedicated "KNOWLEDGE CLASS" block (lines 141–151) plus a
restated rule (rule 11) and five good/bad few-shot examples showing `"class": "World"` /
`"class": "Personal"`. This landed in commit `91a614ec` (FRE-863, "ADR-0115 two-axis emission
contract"), which predates `fb7e0913` (FRE-997, the signal itself) in the same file's history. So
every one of the 7 original events fired against a prompt that already asked for the field — this
was never a "the model was never asked" situation, ruling out the cheap prompt-defect fix the
ticket named as the alternative.

## 3. The measured rate, over the full retained window

`agent-logs-*` only retains data back to **2026-07-30** (a retention/loss ceiling, not a code
artifact — see the telemetry-read-traps memory on ES event loss), so "the full retained window" is
2026-07-30 → 2026-08-28, 221 `entity_extraction_completed` calls, all on `gpt-5.4-mini` (cloud;
the config has not switched extraction models in this window).

| Measure | Value |
|---|---|
| `entity_extraction_fail_open_default{field_name=class}` events | **101** |
| ...distinct `trace_id`s carrying at least one | **27 / 221 calls (12.2%)** |
| Total entities extracted (`entities_found` sum) | **2,664** |
| Per-entity fail-open rate | **101 / 2,664 = 3.8%** |
| `rejected_value` distribution | **100% empty string** (`""` — never a wrong-but-present value) |

This is not 100%. It is a real, non-trivial, but modest rate, concentrated unevenly by day (58 of
the 101 events landed in a 3-day span, 08-04–08-06; 08-23–08-24 processed 1,031 entities — 39% of
the month's volume — with only 3 fail-opens). The day-level variance looks like it tracks
conversation content (how much of a given day's turns are knowledge-bearing at all), not a model or
config regression — no commit touched `entity_extraction.py`'s prompt or class logic in this window
(only `d8c39864`, an unrelated description-wording fix).

**Cross-check against the live graph** (criterion 3, the post-fix bar) — it already passes, with no
fix:

```
MATCH (e:Entity) RETURN e.class, count(*)
"World", 7777
"Personal", 437
NULL, 70   -- pre-dates the ADR-0115 field; not part of this measurement
```

Entities already carry two distinct, meaningfully-distributed `class` values in production. The
ticket's "carrying zero information while presenting as populated" concern does not hold at this
scale — a 5.3% Personal share across 8,214 classed entities is a real signal, not a constant.

**Conclusion: the opening 7/7 sample did not generalize.** It was a burst on the day the signal
first went live, not a representative baseline — exactly the risk the ticket itself named
("enough to establish the failure happens, not enough to establish it always happens"). At the
measured 3.8%/12.2% rate, and given `rejected_value` is always empty string (never a coerced wrong
value), the FRE-996 pattern's own documented caveat applies: LiteLLM 1.89.2 cannot reach Anthropic's
`strict: true` tool-use, so even a full structured-output contract only *guides* the model, and
Python-side fail-open validation would stay regardless. Building that contract now — across
entities *and* relationships *and* stances *and* claims, a materially bigger lift than the
single-object digest pilot (FRE-995 §8.3 rated this "Medium — the schema-design work is the bulk of
it") — is not supported by a 3.8% residual that fails open to a directionally-plausible default.
**No structured-output contract is shipped by this ticket.** If the owner wants one anyway (e.g. to
also close the `entity_type` gap FRE-995 §5.1 flagged as unvalidated), it should be its own
appropriately-sized ticket, not folded into this one.

## 4. What FRE-1013 found instead: the signal over-counts its own denominator

Investigating the 101 events surfaced a real, small, well-scoped defect — not in the extraction
prompt or the fail-open default, but in the **FRE-997 signal itself**.

`_finalize_extraction` calls `_normalize_entity_class` on *every* entity unconditionally, before
checking `output_kind`. But the prompt tells the model, in the KNOWLEDGE CLASS block: *"for
'knowledge' items ONLY... omit or ignore for finding/ephemeral items."* A `finding`/`ephemeral`
entity correctly following that instruction has no `class` key at all — and the existing test
`test_operational_turn_emits_finding_output_kind` already documents and asserts exactly this
("class is not meaningful for finding items but must still fail open to World"). Before this fix,
that *compliant* omission still fired the same `entity_extraction_fail_open_default` warning as a
genuine gap on a `knowledge` entity — the one case the signal exists to catch. The two are
indistinguishable in the 101-event count above, so the measured 3.8%/12.2% is itself an
**overestimate** of the semantically meaningful rate (a `knowledge` entity that should carry a real
`class` and doesn't) — the true rate is unknown and no higher than what's reported here.

**Fix:** normalize `output_kind` first, then only log the fail-open signal for `class` when
`output_kind == "knowledge"`. The default value (`World`) is unchanged and still applied
unconditionally — every entity still carries a `class` property downstream, exactly as before;
only the *signal's* precision changes. This does not touch ADR-0115 D4's fail-open decision (still
fail-open, still `World`), and does not change persisted data. See the accompanying diff and
`TestFailOpenDefaultSignal` additions in `tests/test_second_brain/test_entity_extraction_contract.py`.

## 5. Proof against the ticket's stated requirements

| Requirement | Result |
|---|---|
| Rate of the empty-`class` default, over a meaningful window | **3.8% of entities / 12.2% of extraction calls**, 2026-07-30→2026-08-28 (221 calls, 2,664 entities) — not 100%. `rejected_value` is always `""`. |
| Is `class` present in the extraction prompt? | **Yes**, since FRE-863, predating the FRE-997 signal itself. |
| After [this ticket], entities carry more than one distinct `class` value in the live graph | **Already true without any fix**: 7,777 `World` / 437 `Personal` (+70 legacy `NULL` pre-dating the field). |

## 6. Follow-up not taken here

- **`entity_type` validation** (FRE-995 §5.1's "worse" finding — the 10-type ADR-0109 vocabulary is
  validated nowhere) is a separate axis from `class` and out of this ticket's scope; still open.
- **A structured-output contract for entity extraction** (FRE-995 §8.3 row A2) remains a candidate
  but is not justified by this measurement. If pursued, it needs its own receiving-side Pydantic
  model across entities/relationships/stances/claims — sequence it as its own ticket, sized for the
  real scope FRE-995 already flagged.
