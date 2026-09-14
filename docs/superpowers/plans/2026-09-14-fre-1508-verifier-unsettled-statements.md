# FRE-1508 — Let the inline judge reject entity-free partial misses

**Ticket:** FRE-1508 (Approved 2026-09-14). **Design intent:** ADR-0138 D3(c), D3(d) and Option 5.
ADR-0151 D2 and Context finding 4. **Related:** FRE-1286 (the inline arm), FRE-1282 (containment).

## Cause (AC-1, posted on the ticket)

- `apply_entailment` judges only `ENTAILMENT_REQUIRED` (`grounding/verification.py:528-534`). The
  executor skips the pass unless such a span exists (`orchestrator/executor.py:1765-1773`).
- Containment gives `ENTAILMENT_REQUIRED` only to an entity-free span that misses no token
  (`grounding/containment.py:825-829`). An entity-free span that misses some predicate words gets
  `UNVERIFIABLE` (`containment.py:810-824`). Nothing sends it to the judge.
- Sample (91 turns, 2026-08-31 to 2026-09-13): 264 unverifiable spans. 140 are entity-free
  (91 with content words, 49 with none). 124 are entity-bearing.

## Owner decisions, 2026-09-14 (after codex plan-review)

1. **Reject only.** The judge may settle an entity-free partial miss as `not_entailed` or
   `contradicted_by_source`. A `supported` verdict leaves the span `unverifiable_by_containment`.
   D3(c) stays as written: a span passes only when the source has every content word. No ADR
   amendment. Reason: 20 of the 91 eligible spans share one content word with their source, so a
   pass on a judge verdict alone would weaken containment (codex blocker 1).
2. **A separate cap.** A new setting, `grounding_entailment_max_partial_miss_checks` (default 8,
   cumulative across D4 attempts), bounds the new checks. The existing class keeps its own cap of
   8, so a new check can never make an existing judged span `entailment_unavailable` (codex blocker
   2: 2 of 25 replay turns would exceed one shared cap).

## Design

**Eligible span** (`SpanVerification.partial_miss`): outcome `UNVERIFIABLE_BY_CONTAINMENT`,
`entity_free_predicate` true, and `missing` not empty. A span with no content words has
`missing == ()`, so it is not eligible.

**`apply_entailment`** gains `max_partial_miss_checks` (default 0, so existing callers do not
change) and `partial_miss_checks_already_used`. One `asyncio.gather` judges both classes.

| Verdict on an eligible span | Outcome |
|---|---|
| `not_supported` | `NOT_ENTAILED` (settled) |
| `contradicted` | `CONTRADICTED_BY_SOURCE` (settled) |
| `supported` | unchanged, `UNVERIFIABLE_BY_CONTAINMENT`; the detail records the verdict |
| `undecided`, judge raised, or past the cap | unchanged |

The existing class keeps its code path, its cap and its over-cap conversion exactly.

**Counters.** `TurnVerification.entailment_checks` stays the total judge calls on the pass (the
recorded cost). New field `TurnVerification.partial_miss_checks` holds the subset. The executor
adds the difference to `ctx.grounding_entailment_checks` and the subset to the new
`ctx.grounding_partial_miss_checks`. The turn record and the ES event do not change shape.

**Detail text.** The unverifiable detail says "states the claim's entities and figures". It stays
for an entity-bearing span. An entity-free partial miss says "states some of the claim's predicate
words". A span with no content words says "the span has no content words, so containment cannot
check it".

**What does not change:** `check_containment`, `ContainmentOutcome` and
`ContainmentResult.contained` (FRE-1282 AC-4); the total-miss rule; entity-bearing spans (ADR-0138
Option 5); spans with no content words (an extractor defect); the normaliser defects (`'s` →
`second`, sentence-initial capitals), which move settled outcomes; the offline sampler.

**Enforcement (corrected after codex).** `decide()` retries on every failure today, unsettled ones
included (`enforcement.py:94-106`). ADR-0151 D2 is not implemented. So a rejected partial miss
blocks as it did before, with a judge reason in place of a paraphrase reason. Only the reason in the
retry directive changes. Live mode is `observe`, so nothing blocks today.

**Cost.** Worst case 8 more `claude_sonnet` judge calls per turn (6,000-character excerpt, 512
output tokens), in the `entity_extraction` budget lane. On the sample: about 81 extra calls over 23
of 91 turns. The replay records the measured cost from the test cost ledger.

## Steps

1. **Tests first.**
   - `tests/personal_agent/grounding/test_verification.py`: an entity-free partial miss stays
     `UNVERIFIABLE_BY_CONTAINMENT` with `partial_miss` true and the predicate-word detail; a total
     miss stays `NOT_CONTAINED`; an entity-bearing partial miss stays unverifiable with
     `partial_miss` false; a no-content-word span (`"and so on"`) keeps its own detail.
   - `tests/personal_agent/grounding/test_inline_entailment.py`: a rejection settles the span; a
     `supported` verdict cannot pass it, on a source that shares only the word `high`; an
     `undecided` verdict leaves it unchanged; 8 existing-class spans and 3 partial misses in both
     orders, with a partial cap of 2, judge all 8 existing spans; the partial cap is cumulative
     across attempts; a partial cap of 0 costs no judge call.
   - An executor test that the two context counters grow apart.
   - Run: `make test-file FILE=tests/personal_agent/grounding/test_verification.py`, the same for
     `test_inline_entailment.py`. The new behaviour tests fail before step 2.
2. **Implement.** `grounding/verification.py` (property, detail, `apply_entailment`, field,
   docstrings), `orchestrator/executor.py` (`_apply_inline_entailment`), `orchestrator/types.py`
   (counter), `config/settings.py` (setting and the D3(d) comment). Docstrings that state the old
   rule: `verification.py` module docstring and `SpanVerification.entity_free_predicate`,
   `containment.py:31-38` and `825-829`, `entailment.py:8-17`. `.env.example` if it lists the
   entailment settings.
3. **Replay script** `scripts/eval/fre1508_unsettled_replay.py`, amended for codex blocker 3:
   - Rows carry the containment result (`required`, `missing`, `entity_free_predicate`), the
     verdict, the reason, and the final outcome.
   - The verdict cache key adds the trace identifier.
   - `compare` fails unless both runs hold the same span set, and fewer than 10 turns remain after
     dropping every turn with a "before" outcome that differs from the capture.
   - The replay reports the ordinal kind of each cited source.
   Measured before the fix: with a stub judge, 566 of 575 included spans match the capture, all 264
   unverifiable spans included. The 9 mismatches are captured judge outcomes that a stub cannot
   give, and 2 `passed` spans.
4. **Record the AC-2 bar on the ticket**, then run the replay: "before" on `origin/main`
   (`PYTHONPATH` to a detached worktree), "after" on the branch, one shared verdict file.
5. **Gates:** `make test`, `make mypy`, `make ruff-check`, `make ruff-format`,
   `pre-commit run --all-files`. Self-review with `feature-dev:code-reviewer` on
   `git diff origin/main...HEAD`.

## Acceptance criteria

| AC | Proof | Pass condition |
|---|---|---|
| AC-1 cause named with evidence | Ticket comment 2026-09-14 | Posted |
| AC-2 fewer unsettled on the same inputs | `compare` on the replay | Unsettled falls by 20 or more. Zero unsettled spans reach `passed` |
| AC-3 no settled outcome changes | `compare`, same run | Zero changes among spans settled before the fix |
| Replay validity | `compare` gates | Same span set in both runs, 10 or more turns after the fidelity filter |
| Unit | New tests, grounding suite | `make test` passes |

## Diff class

Escalated. The change adds model calls on the turn path (cost, a new cost setting). Flag for the
owner's `/code-review ultra` before merge.
