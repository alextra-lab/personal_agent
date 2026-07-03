# FRE-766 — model × reasoning extraction benchmark on FRE-630 (data-gathering, sync)

**Ticket:** [FRE-766](https://linear.app/frenchforest/issue/FRE-766) · **Approved** · Tier-1:Opus · parent [FRE-630](https://linear.app/frenchforest/issue/FRE-630) · project Memory Recall Quality
**Backing:** FRE-630 pre-write extraction-quality benchmark (`scripts/eval/fre630_extraction_quality/`), extended to the model axis (FRE-656 embedder-benchmark precedent).
**Posture:** measure-don't-assert / verify-don't-assume. **This is a benchmark that produces DATA, not a winner pick.**
**Codex plan-review:** 2026-07-03, "directionally sound; not safe as-is" — all P0/P1/P2 folded in (§Codex fixes).
**Codex code-review:** 2026-07-03, **P0 = none** (prod-safe); 3 eval-harness P1s (gold_set stamp · baseline-compat keys · mini-none label) folded in.

> **OUTCOME (see research §4.3):** benchmark complete. **No cell clears the bar** (best entity_type 0.89 = sonnet, with zero reasoning); **reasoning is a poor trade** (flat-to-negative, xhigh a runaway). The rel-type/claim "regressions" are largely **gold/vocab artifacts** (harness-removed spot-checks). **Decisive finding: convergent failure across 4 model families + GLiNER ⇒ the entity taxonomy is the root cause**, captured as **ADR-0109** (8-type redesign, Proposed). **No prod config change** — mini@none stands. This PR ships the reusable mechanism (config-driven reasoning_effort + the eval DI seam + the FRE-766 matrix harness).

**Owner decisions (2026-07-03, pre-plan):**
1. **Sync now, Batch API as a follow-up** — no batch infra exists; batch latency is the wrong signal for the sync-production freshness decision; 540 calls sync-with-concurrency is minutes. (litellm supports native OpenAI/Anthropic batch → the follow-up ticket is feasible there.)
2. **No rollout in this PR.** "Winning model is a misnomer — we want cost/quality/efficiency; the production choice comes LATER after batch + DSPy + cost analysis. Assume nothing, we are learning." The deliverable is the **data table + neutral analysis**, one input to a later holistic decision. Do NOT edit the prod extractor config or crown a winner.
3. **Model-appropriate default sampling** per cell (GPT cells let reasoning_effort drive, don't force temp; sonnet-5 adaptive at default). mini stays temp 0.0 (FRE-758) for its own cells.

---

## The matrix — 5 new cells + the existing baseline

| # | Model | Reasoning | Sampling |
|---|-------|-----------|----------|
| 1 | gpt-5.4-mini | high | temp 0.0 (mini's pin) |
| 2 | gpt-5.4-mini | xhigh | temp 0.0 |
| 3 | gpt-5.4 (full) | medium (default) | model default |
| 4 | gpt-5.4 (full) | high | model default |
| 5 | claude-sonnet-5 | adaptive (default) | model default |
| — | **baseline: gpt-5.4-mini @ medium** | — | **REUSE the FRE-759 flag-OFF 36-case run** (mini@medium@temp0: entity_type 0.76, claim 3/12) — the current-gold baseline, NOT the stale 24-case FRE-630 row |

36-case FRE-630 gold, **3 samples/cell**. 5 × 36 × 3 = 540 extractions + smokes.

## Codex fixes (folded in)

- **P0.1 cell-derived metadata** — `RunMeta` is built from the **active cell object** (model_id + reasoning), NOT from `load_model_config()` (which the DI seam no longer mutates). Each cell's report asserts `meta.extractor_model == cell.model_id` before scoring.
- **P0.2 pricing registration** — the standalone harness registers each cell's pricing into `litellm.model_cost` before the run (app startup does this in prod; the harness doesn't), and asserts the prefixed key resolves; the **reported cost is computed directly from `usage × cell rates`** (authoritative; not reliant on `completion_cost`/`model_cost`).
- **P1.2 budget lane** — the DI override builds its client with `budget_role="entity_extraction"` so cost reservations + telemetry stay in the intended lane (an unknown role would default to `main_inference`).
- **P1.3 baseline-reuse gate** — reuse the FRE-759 flag-OFF run as the mini@medium row **only if** `gold_schema` + `matcher_version` + `prompt_hash` + `samples` + effective-flag-state all match this environment (hard assert). On any mismatch, re-run mini@medium as a 6th cell in-run. (Off main-with-759, flag-OFF `prompt_hash` should still be `8a1bdd11` — verified, not assumed.)
- **P1.4 smoke classification** — the extractor swallows exceptions into an empty fallback; the smoke gate instead **classifies** the failure mode (empty-fallback · provider-rejection · parse-failure · schema/contract-failure) and records an effort/param rejection as a **cell finding**, never a quality-zero. `drop_params` is left OFF so rejections surface (not silently dropped).
- **P2.1/2.2/2.3** — `reasoning_effort` validates to the litellm set (`low|medium|high|xhigh` + None; sonnet stays None); reasoning-token capture parses both object and dict shapes and reports `null` explicitly (never a pass/fail); the analysis copy states the bars are **screening thresholds, not significance tests** — no ranking cells by tiny n=3 deltas.

## Machinery gaps found (must build)
- `reasoning_effort` is **dead** on `LiteLLMClient.respond()` — declared + documented but never added to `litellm_kwargs`/`acompletion` (line 507). Must wire it.
- The extraction path never passes reasoning_effort; `ModelDefinition` has no reasoning field.
- `gpt-5.4` (full) is not defined in config (only nano + mini). sonnet-5 exists (`claude_sonnet`).
- No usage/latency/cost is surfaced out of the extractor.

---

## Acceptance criteria (the definition of done)

| # | Criterion | Proof |
|---|-----------|-------|
| AC-1 | `reasoning_effort` is wired end to end: a value on `ModelDefinition` reaches `litellm.acompletion` for the extraction call; `None` (prod default) leaves it absent | unit tests (litellm_kwargs + extractor pass-through, mocked) |
| AC-2 | Each cell runs the **real extractor** unchanged (same prompt/parse/provenance) at its (model, reasoning, sampling); a per-cell **smoke** asserts schema-valid JSON honoring the 7-type + class + stance/claim contract **before** the full run | harness smoke gate + a bounded live run (owner-gated) |
| AC-3 | Per cell: all FRE-630 quality metrics + **case-level claim recall** + per-metric **std** + **cost** + **latency** + **reasoning-token counts** captured and curated | research doc §4.3 table |
| AC-4 | 5-cell table + the reused baseline row; **neutral** cost/quality/efficiency analysis noting which cells clear entity_type ≥0.95 AND claim ≥0.8 with no near-ideal regression — **NOT a winner pick / no config rollout** | research doc §4.3 |
| AC-5 | Prod extraction behaviour unchanged (reasoning_effort defaults None; call_stats capture is opt-in) — no live config edit | `make test` (contract tests green) + no models.*.yaml role change |

AC-1/2/5 are mechanism (unit-tested, PR bar). AC-3/4 are the owner-gated benchmark run (the data deliverable).

---

## Files touched

| File | Change |
|------|--------|
| `src/personal_agent/llm_client/models.py` | + `reasoning_effort: str \| None = None` on `ModelDefinition` (validated to {low,medium,high,xhigh} or None) |
| `src/personal_agent/llm_client/litellm_client.py` | wire `reasoning_effort` into `litellm_kwargs` (fix dead param); add `reasoning_tokens` to the `usage` dict from `completion_tokens_details.reasoning_tokens` (closes the ES observability gap the ticket names) |
| `src/personal_agent/second_brain/entity_extraction.py` | **DI override seam (concurrency-safe):** optional eval-only `model_override: ExtractionModelCell \| None = None` — when passed, the cloud path builds a `LiteLLMClient` from the override (model_id/provider/max_tokens) with **`budget_role="entity_extraction"`** (P1.2 — keeps the reservation/telemetry lane) instead of the global `load_model_config()`, and passes its `reasoning_effort`+`temperature`; default None → prod path unchanged. + optional `call_stats_sink: list[dict] \| None = None` that appends {usage, reasoning_tokens, latency_ms, cost, error_class} from the cloud call (records the failure mode for the smoke classifier — P1.4). Prod also gains `reasoning_effort=model_def.reasoning_effort` on its own (non-override) path so the config field is live. |
| `config/models.cloud.yaml` | + `gpt-5.4` (full) pricing entry so the ADR-0065 cost-gate reservation resolves for the new id (unassigned to any prod role). Reported cost is computed directly from usage×cell-rates (robust; not reliant on `completion_cost`). |
| `scripts/eval/fre630_extraction_quality/cells.py` | NEW — the 5-cell matrix as data (`ExtractionModelCell`: name, model_id, provider, reasoning_effort, temperature, input/output rate); pure |
| `scripts/eval/fre630_extraction_quality/harness.py` | `--cell <name>`/`--all-cells`; **parallelize BY MODEL** — one async worker per cell via `asyncio.gather`, each worker runs its 108 extractions (36×3) **serially** with its `model_override` (production-faithful per-call latency; 5 models overlap for ~5× wall-clock). Before any run: **register each cell's pricing** into `litellm.model_cost` (assert key resolves). Per-cell **smoke gate** (1 case) that **classifies** the failure mode (empty-fallback/provider-rejection/parse/schema) and only proceeds on a schema-valid contract-honoring result. `RunMeta` **built from the cell object** (assert `extractor_model == cell.model_id`). Capture per-call latency (wall-clock) + call_stats (usage/reasoning-tokens/cost=usage×cell-rates). No global config mutation → cells concurrency-safe. |
| `scripts/eval/fre630_extraction_quality/report.py` | + cost/latency/reasoning-token aggregates in the render (per-cell) |
| tests | `test_litellm_client` (reasoning_effort in kwargs; None→absent; reasoning_tokens parsed), `test_entity_extraction_contract` (extractor forwards model_def.reasoning_effort; call_stats_sink populated; default None unchanged), `test_fre630_*` (cell-matrix structure + smoke-gate predicate, pure), ModelDefinition reasoning_effort validation |
| `docs/research/2026-07-03-fre-630-extraction-quality-sota.md` | + §4.3 the 5-cell table + neutral analysis (after the run) |

**No prod role reassignment** (gpt-5.4-full is defined but unassigned). **No deploy.** No ADR-0074 new prod surfaces (reasoning_effort is a passthrough; call_stats is eval-only). Pricing for gpt-5.4-full added so the cost gate estimate + the cost capture both resolve.

---

## Build order (TDD)

1. **reasoning_effort wiring (RED→GREEN).** Test: `respond(reasoning_effort="high")` puts it in litellm_kwargs; `None` omits it. → add the 2-line wire + `reasoning_tokens` usage parse. `make test-k K=litellm`.
2. **ModelDefinition field.** Test: reasoning_effort validates {low,medium,high,xhigh}/None. → add the field.
3. **Extractor pass-through + call_stats (RED→GREEN).** Contract tests: cloud path forwards `model_def.reasoning_effort`; when a `call_stats_sink` is passed it gets usage+latency+cost; default None → unchanged (all existing contract tests green). → implement.
4. **Cell matrix + harness (RED→GREEN).** Pure tests on: the cell list; the smoke **failure-classifier** (empty-fallback/provider-rejection/parse/schema); pricing-registration + cost=usage×rates; RunMeta-from-cell; the baseline-compatibility gate; the by-model worker fan-out (each uses its own `model_override`, no shared state). → `cells.py` + harness `--cell`/`--all-cells` + pricing register + per-model `asyncio.gather` + classifying smoke gate + capture. `make test-k K=fre630`.
5. **Benchmark run (owner-gated spend).** Register cell pricing; all 5 cells concurrent (one worker/model), serial within each; each cell smokes (1 case, classified) before its full `--samples 3` run. Baseline: reuse the FRE-759 flag-OFF row **iff** the compatibility gate passes, else re-run mini@medium as a 6th cell. Curate §4.3 (quality + std + cost[usage×rates] + latency + reasoning tokens; **screening-threshold** analysis, no tiny-delta ranking). *(Explicit OK before firing.)*
6. **Follow-ups (Step 5):** Batch API extraction capability (litellm native batch) — Needs Approval, Memory Recall Quality.
7. **Quality gates:** `make test` · `make mypy` · `make ruff-check`/`format` · `pre-commit`.

## Risks / halt conditions
- **A cell ignores the structured-output contract** (a stronger model that emits prose/loose JSON scores worse) — the smoke gate catches this before the full run; a cell that fails smoke is recorded as "contract-incompatible as-prompted," not silently scored 0.
- **`xhigh` / `gpt-5.4` id may be rejected by the provider** — smoke surfaces it as a finding, not an assumption (verify-don't-assume).
- **No shared-state bleed across concurrent cells** — the DI `model_override` seam means each worker carries its own (model, reasoning) with zero global mutation, so the 5 cells run concurrently without config races; each cell's `RunMeta` stamp (model+reasoning) is asserted to match its cell.
- **Spend** — 540 extractions + smokes, real cloud (budget not a constraint per owner); still an explicit OK at Step 5. Latency is captured wall-clock (production-faithful), not batch.
- **No rollout / no prod config change** — the winning-cell decision is explicitly deferred (batch + DSPy + cost analysis pending).
