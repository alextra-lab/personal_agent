# FRE-953 — session-digest producer evaluation (ADR-0124 Amendment A)

**Date:** 2026-07-23 · **Ticket:** FRE-953 · **Backing:** ADR-0124 Amendment A
**Arms run against:** the `session_summary` role's live deployment (`claude_sonnet`),
through the real `generate_session_digest` — no test-only path.
**Harness:** `scripts/eval/session_digest_eval.py` · **Fixtures:**
`tests/fixtures/session_digest/` (pre-registered, see `REGISTRY.md`)
**Verbatim result:** `2026-07-23-fre-953-session-digest-eval-run.json`

---

## Summary

Amendment A narrows the producer to the conversation: tool payloads and arguments no
longer reach the summariser prompt; corrections are reworked from the payload-fed
"Tier A" to two conversation-grounded kinds — `self_correction` and
`status_contradiction`. This arm proves the three criteria the amendment touches.

| Criterion | Verdict | Evidence |
|---|---|---|
| **AC-8** metadata present, payloads/arguments absent | **PASS** | 3/3 cases; scored offline, both directions (presence of name/status/error, absence of any payload or argument) |
| **AC-12** self-correction recall + Tier-C precision | **PASS after a producer fix** | recall **8/8**, **0** false positives across all 12 Tier-C negatives (precision absolute), every self-correction carried its evidence span |
| **AC-13** missing evidence | **PASS** | 3/3 — silence where the payload is absent; a correction where the tool **status/error** or the session's own text carries it |

Withdrawn / deferred, per the amendment and the ticket, and **not evidenced here**:
**AC-9** (withdrawn — a tool-only fact is deliberately no longer reproduced; fixture
deleted), **AC-21** (withdrawn — the injection path it gated no longer exists),
**AC-10** (deferred to an owner-led redesign; its payload-derived fixture is invalidated
and its overlap-scoring harness is broken — kept but not run).

## 1. AC-13 is the load-bearing result

The amendment's headline ("remove Tier A / Tier B only") contradicts its own AC-13,
whose `status_visible` case — the assistant narrates success while the tool's own error
says otherwise, with **no** self-correction — must still yield a correction ("either
**non-payload** correction path"). The reconciliation (owner-approved) keeps a
payload-free `status_contradiction` path alongside `self_correction`, removing only the
payload-fed half of the old Tier A.

The live arm confirms it: `status_visible` emitted exactly one correction, `payload_absent`
stayed silent, `self_correction` fired. The distinction the whole design rests on — a
contradiction visible in retained **metadata** survives, a contradiction that would need
a **payload** does not — holds against the real model, not just in the fixtures.

## 2. AC-12 — a producer defect, found by the arm and fixed

**Run 1 (post-implementation): every correction case failed `schema_invalid`.** The
`CorrectionTier` literal, the system prompt, and the fixtures were all migrated to
`self_correction` / `status_contradiction`, but `_parse_correction` in the producer
still validated `tier in ("A", "B")` and rejected the model's (correct) output. The
happy-path unit tests missed it because their canned digest carries an empty
`corrections` slot, so the correction-parse branch was never exercised.

**Fix:** `_parse_correction` accepts the two Amendment-A tiers, and three new unit tests
(`test_amendment_a_correction_tiers_parse_and_generate`,
`test_legacy_correction_tier_letters_are_rejected`) now exercise the correction-parse
branch end to end — the coverage gap that let the defect through. This is disclosed
rather than hidden: **the fixtures are byte-identical across both runs and remain
frozen; only the producer changed.** Fixing a defect the arm reveals is what running the
arm is for.

**Run 2 (prompt/parser fixed, same frozen fixtures):** recall **8/8**, **0** false
positives across all twelve Tier-C negatives, every self-correction carrying its
supporting-evidence span. AC-12 passes.

Worth noting: the false positive FRE-947 had to guard against with the same-proposition
test — a judgment ("low priority") read as contradicted by payload data ("severity:
high") — is now **structurally impossible**, because the contradicting payload is no
longer in the prompt at all. The same-proposition guard is retained as
belt-and-suspenders for the `status_contradiction` path.

## 3. Fixture citability was pre-validated offline

Every AC-12 positive's supporting evidence lives in a field the producer is actually
given — a tool `error` or the conversation text — never a payload. Each carries a
hand-authored `reference_correction` that resolves, asserted before the paid arm by
`test_session_digest_validator.py::test_ac12_positive_fixtures_have_a_resolving_reference_citation`.
Without this an un-citable positive would fail `validate_digest_provenance`, surface as
an `errored` case, and read as an AC-12 failure caused by a fixture flaw rather than a
producer one.

## 4. Cost

The `session_summary` role borrows the `captains_log` budget lane (ADR-0124 D2), whose
$2.50 daily cap was already exhausted by the day's reflection activity, so the first arm
was fully `budget_denied`. With **owner authorisation (2026-07-23)** the cap was bumped
**once, temporarily, to $4.00** for the run and **reverted to $2.50 immediately after**;
the bump is not in the branch diff. Total spend for the arm was ~$0.20 (~25 calls at
~$0.0085 each), on the same ledger as production.

## Reproduce

```bash
# offline only, no spend
uv run python scripts/eval/session_digest_eval.py --dry-run

# full amended arm (AC-8 + AC-12 + AC-13; AC-9 withdrawn, AC-10 deferred)
uv run python scripts/eval/session_digest_eval.py --out report.json
```

The build worktree's `.env` is a stub; the harness needs `AGENT_DATABASE_URL` and the
provider keys from the primary repo's `.env`, and registers a real `CostGate` so the
arm's spend lands on the same ledger as production's.

## References

- ADR-0124 Amendment A — the digest is built from the conversation, not tool payloads
- `tests/fixtures/session_digest/REGISTRY.md` — the pre-registered sets and corpus count
- `docs/research/2026-07-23-fre-953-session-digest-eval-run.json` — the arm, verbatim
- `docs/research/2026-07-23-fre-947-session-digest-eval.md` — the pre-amendment arm
