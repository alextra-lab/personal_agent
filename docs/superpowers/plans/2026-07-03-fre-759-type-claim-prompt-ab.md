# FRE-759 — entity-type + claim-emission prompt A/B (few-shot exemplars, flag-gated)

**Ticket:** [FRE-759](https://linear.app/frenchforest/issue/FRE-759) · **Approved** · Tier-1:Opus · parent [FRE-630](https://linear.app/frenchforest/issue/FRE-630) · project Memory Recall Quality
**Backing:** FRE-630 pre-write extraction-quality benchmark (`scripts/eval/fre630_extraction_quality/`) + its gpt-5.4-mini baseline (`docs/research/2026-07-03-fre-630-extraction-quality-sota.md` Part 4).
**Method posture:** measure-don't-assert (ADR-0087; FRE-433/434). Flag → verify → roll out.
**Codex plan-review:** 2026-07-03, "Approve with P1 fixes" — all P1/P2 folded in below (§Measurement discipline).

> **OUTCOME (A/B run 2026-07-03 — see research §4.2):** the hand-drafted exemplars are **DISPROVEN**.
> entity_type 0.76→0.77 (AC-1 not met), claim 3/12→5/12 case-level (AC-2 not met), and
> relationship_type_correctness **regressed** 0.99→0.77 (AC-3 pin violated). **Flag stays default-OFF**
> (zero live impact). The **permanent instrument + mechanism ship** (powered gold, case-level metric,
> flag-aware hash, dormant flag seam); the lever hands off to **DSPy-compiled extraction (FRE-764)**.
> AC-4/AC-5 (mechanism) are met and unit-tested; AC-1/2/3 (outcome) are not — this is the measure-don't-
> assert result, not a defect.

**Owner decisions (2026-07-03, pre-plan):**
1. **Lever = prompt few-shot exemplars first** (not DSPy). DSPy is a fast-follow only if this doesn't clear the bar.
2. **Expand the gold set + certify** — the claim metric is measured on only 2/24 gold cases; grow claim coverage so `claim_emission_recall ≥0.8` is *trustworthy*, then re-baseline and A/B.

---

## Problem (measured, FRE-630 baseline gpt-5.4-mini — historical, 24-case set)

Two evidence-backed weak spots; every other metric is at/near ideal (class 1.00, hallucination 0.00, dedup 1.00, stance 1.00, rel-type 0.89):
- **entity_type_accuracy 0.80±0.35** — wrong one of the 7 types ~20% of the time; per-case diffs point at **Concept↔Topic** and **Concept↔Technology** boundaries.
- **claim_emission_recall 0.33±0.47** — personal situational *claims* under-emitted; measured on only **2** claim-bearing cases → underpowered.

FRE-758 already ruled out temperature as the variance lever. This ticket changes the **prompt**, the lever the baseline implicates.

---

## Approach

Add a **flag-gated few-shot exemplar block** to the extraction prompt: (a) contrastive **type-disambiguation** exemplars for Concept↔Topic and Concept↔Technology, and (b) **claim-emphasis** exemplars. Default OFF. A/B on the **expanded** FRE-630 benchmark: freshly-run flag-OFF baseline vs flag-ON candidate, **paired per-case deltas** + case-level claim pass/fail. Ship default-off; master flips on a verified pass.

**Blast radius:** zero until the flag is flipped — inert at `default=False`. The no-regression guard (hard per-metric thresholds, below) is part of the A/B pass criteria; if the exemplars perturb a near-ideal metric, the flag stays off and the exemplars are revised. Fully reversible.

---

## Measurement discipline (codex P1/P2 — the credibility spine)

1. **Powered claim set (P1.1).** Grow claim-bearing gold cases to **≥10 distinct (target 12)**, up from 2. `claim_emission_recall ≥0.8` is read **case-level over distinct cases** (majority ≥2/3 samples per case), i.e. **≥8/10 distinct claim cases pass** — never a sample-flattened `n=cases×3`.
2. **Fresh baseline (P1.2).** Gold expansion **invalidates** the historical 0.80/0.33 (they were the 24-case set). AC-1/AC-2 compare flag-ON only against a **freshly-run flag-OFF baseline on the expanded gold set**. The curated table reports the **old-24-case slice** and the **new-case slice** separately, so "gold got harder/easier" is never conflated with "prompt got better."
3. **Hard no-regression thresholds (P1.3).** Not "within noise" — **exact pins** on the flag-ON run: `knowledge_class_accuracy = 1.00`, `hallucination_rate = 0.00`, `dedup_convergence = 1.00`, `stance_emission_recall = 1.00`, `empty_fallback_rate = 0.00`; `relationship_type_correctness ≥ 0.89` (baseline). Any miss ⇒ manual per-case diff review **before** the flag may roll out.
4. **PR bar ≠ Done bar (P1.4).** The PR merges **flag-dark**, proving only **AC-4 (mechanism)** + **AC-5 (gold powered)** via unit tests. FRE-759 stays **In Review / DEPLOY-HOLD** until the owner-gated A/B produces AC-1/2/3 evidence; the flag decision (ship-on / revert) is then recorded and master moves the ticket to Done. This is stated in the master-handoff comment so the proof gate doesn't bounce it.

---

## Acceptance criteria (the definition of done)

| # | Criterion | Proof | Bar |
|---|-----------|-------|-----|
| AC-1 | **entity_type_accuracy ≥0.95** (flag ON) vs the fresh flag-OFF expanded-gold baseline; old-24 & new slices reported separately | A/B run, curated into research §4.2 | Done bar |
| AC-2 | **claim_emission_recall ≥0.8 case-level** (≥8/10 distinct claim cases pass, majority-of-3) under flag ON | A/B run | Done bar |
| AC-3 | **Hard no-regression** pins hold (class 1.00 · hallucination 0.00 · dedup 1.00 · stance 1.00 · empty_fallback 0.00 · rel-type ≥0.89) on the flag-ON run | A/B run (paired) | Done bar |
| AC-4 | Change ships **behind a default-off flag**; flag toggles the prompt deterministically via one shared seam; `prompt_material_for_hash()` reflects the rendered block; JSON-brace exemplars don't break `.format()` | `make test-k K=entity_extraction_contract` | **PR bar** |
| AC-5 | Gold-set claim coverage **powered** (≥10 distinct claim cases), still PII-clean, schema-valid, failure-modes represented | `make test-k K=fre630_gold_set` | **PR bar** |

AC-4/5 are unit-tested and green **in the PR** (flag-dark merge). AC-1/2/3 are the owner-gated A/B outcome, proven post-merge (in-session with explicit OK, or master from the runbook), gating the flag flip and the move to Done.

---

## Files touched

| File | Change |
|------|--------|
| `src/personal_agent/config/settings.py` | + `entity_extraction_fewshot_exemplars_enabled: bool = Field(default=False, …)` |
| `src/personal_agent/second_brain/entity_extraction.py` | + `_EXTRACTION_FEWSHOT_EXEMPLARS` constant; `{fewshot_exemplars}` placeholder in `_EXTRACTION_PROMPT_TEMPLATE`; **one shared** `_fewshot_block()` (flag decision) feeding both `_build_extraction_prompt(user, assistant)` (used by the executor at line 499) and `prompt_material_for_hash()`. Exemplar text is passed as a `.format()` **value**, never re-formatted, so its literal JSON braces are inert |
| `scripts/eval/fre630_extraction_quality/harness.py` | `_prompt_hash()` hashes `entity_extraction.prompt_material_for_hash()` (renders the actual flag-ON block) → flag-ON/OFF runs get distinct `prompt_hash` |
| `scripts/eval/fre630_extraction_quality/report.py` + `metrics.py` | + a **pure** `claim_case_level_recall(report)` (fraction of distinct claim cases passing majority-of-samples) surfaced in the render — makes AC-2 a hard number, not manual curation |
| `scripts/eval/fre630_extraction_quality/gold_extraction.yaml` | + ~8–10 claim-bearing cases (→ ≥10 distinct total) + 2 explicit type-boundary cases (Concept↔Topic, Concept↔Technology); paraphrased, no PII |
| `tests/test_config/test_settings.py` | flag exists + defaults False |
| `tests/test_second_brain/test_entity_extraction_contract.py` | flag OFF → no exemplar sentinel + `prompt_material_for_hash()==system+template`; flag ON → sentinel present + hash differs; **brace-safety**: flag-ON render with JSON-looking exemplar content raises no `KeyError`/`ValueError`; existing contract tests stay green |
| `tests/evaluation/test_fre630_gold_set.py` | + `test_claim_coverage_is_powered` (≥10 distinct claim cases); existing size/PII/coverage stay green |
| `tests/evaluation/test_fre630_metrics.py` | + unit test for `claim_case_level_recall` (case-level, majority-of-samples) |
| `docs/research/2026-07-03-fre-630-extraction-quality-sota.md` | + §4.2 the FRE-759 A/B table (curated; old-24 & new slices separate), after the A/B is run |

No new ADR-0074 identity surfaces (eval harness + a prompt constant + a flag — no new prod `log.*`/`bus.publish`/`MERGE`). No schema/migration. No cost/budget change.

---

## Build order (TDD)

1. **Flag (RED→GREEN).** `test_settings.py` assertion (exists, default False) → add the Field. `make test-k K=test_settings`.
2. **Prompt seam (RED→GREEN).** Contract tests: flag-OFF excludes the sentinel and `prompt_material_for_hash()==system+template`; flag-ON includes the sentinel and the hash differs; brace-safety render. → add `_EXTRACTION_FEWSHOT_EXEMPLARS`, the `{fewshot_exemplars}` placeholder (spliced so empty→no material change), the shared `_fewshot_block()`, `_build_extraction_prompt()`, `prompt_material_for_hash()`. Confirm existing contract tests (temperature, class, stance/claim, facet, description-kind) stay green. `make test-k K=entity_extraction_contract`.
3. **Harness hash + case-level claim metric (RED→GREEN).** Add `test_fre630_metrics` case for `claim_case_level_recall` → implement it (pure) + surface in `report.py`; repoint `_prompt_hash()` at `prompt_material_for_hash()`. `make test-k K=fre630_metrics`.
4. **Gold expansion (RED→GREEN).** `test_claim_coverage_is_powered` (≥10 distinct claim cases) → author ~8–10 claim cases + 2 type-boundary cases in `gold_extraction.yaml`. `make test-k K=fre630_gold_set`. Update the "N≈24" header note.
5. **A/B run (owner-gated spend).** `make test-infra-up`; run flag-OFF (fresh baseline, `--samples 3`) then flag-ON (candidate, `--samples 3`) on the expanded gold; `make test-infra-down`. Curate the paired table (old-24 & new slices separate; case-level claim pass/fail; hard-pin regression check) into research §4.2. Proves AC-1/2/3. *(Explicit per-action OK before firing; else hand master the runbook.)*
6. **Follow-ups (Step 5).** File surfaced work under Memory Recall Quality — e.g. DSPy-compiled extraction (its own Needs-Approval flag→verify→A/B), a `seed`-parameter determinism spike, and a closed-world entity-precision subset if the type A/B is ambiguous.
7. **Quality gates:** `make test` (module then full) · `make mypy` · `make ruff-check`/`format` · `pre-commit run --all-files`.

## Exemplar content (draft — the actual lever)

- **Type: Concept vs Topic vs Technology.** 3 contrastive one-liners: e.g. *"GraphRAG" → Concept (a named technique/idea)*; *"knowledge graphs" as the subject area under discussion → Topic*; *"Neo4j" → Technology (a concrete tool/product)*. Targets exactly the confused boundaries from the per-case diffs.
- **Claim emphasis.** 2 exemplars where a first-person situational fact (*"I just started a new job at …", "we're relocating to … next month"*) MUST emit a `claims` entry with a stable `facet`, contrasted with a near-miss that is a World entity, not a claim.

Kept short (the prompt is already ~1.8k tokens); additive, contrastive, targeting only the two weak boundaries so the near-ideal metrics are not disturbed. Exemplar text is a `.format()` value — its literal JSON braces never reach the format scanner.

## Risks / halt conditions

- **Still finite power** — even at ≥10 claim cases the set is a calibration probe; lead with **paired per-case deltas** and **case-level pass/fail**, report mean/std as secondary, state the power limit plainly (as FRE-630/758 did). An ambiguous A/B is a reportable outcome, not a forced "pass."
- **Regression on a hard-pin metric** — flag stays off; per-case diff review; revise exemplars; re-A/B. Default-off ⇒ no live impact.
- **Spend** — every harness run is real gpt-5.4-mini spend; expanded set ~36 cases × 3 samples × 2 runs ≈ $0.25–0.50 total. Explicit OK at Step 5, or master runs post-merge.
