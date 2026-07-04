# FRE-770 — ADR-0109 V2 step 2: re-label the FRE-630 gold with multiple independent annotators + measured IAA

**Ticket:** FRE-770 (Approved, stream:build1, Tier-2:Sonnet)
**Backing ADR:** ADR-0109 (Accepted 2026-07-03), Implementation Notes step 2. Gated by step 1 (FRE-769, Done — recall/pedagogy keys on entity type: `dedup.py`, `_ENTITY_TYPE_KEYWORDS`, `search_memory` schema all key on the literal type string, so the type values themselves matter and must be re-labeled carefully, not treated as free-to-coarsen).

## Acceptance criteria (from the ticket, verbatim)

Gold re-labeled to the eight V2 types by at least two independent annotators; a reported inter-annotator agreement (IAA) figure per type; disagreements adjudicated; Phenomenon/boundary coverage grown beyond the five already spot-checked; benchmark re-baselined on the agreed gold (ADR-0109 AC-5, strengthened). **A single-author re-label does NOT satisfy this ticket.**

## Scope boundary (what this ticket is NOT)

- **Not** the prompt swap (FRE-771) — the extractor keeps emitting V1 types until FRE-771 lands.
- **Not** the KG migration (FRE-772).
- **Not** the relationship-vocab validation (FRE-773).

This matters for the central design decision below: the gold set cannot simply *replace* its `type:` field with V2 labels, because the current harness (`gold.py` `ALLOWED_ENTITY_TYPES`, `bench.py`/`harness.py` scoring, and the live V1 extractor) all still speak V1. Flipping the vocab now would silently break every existing scored run and pre-empt FRE-771's own "swap the prompt, keep the knowledge-class + stance/claim contract" scope.

## Design decision: dual-type the gold, don't replace

Add a **new, optional** `v2_type` field alongside the existing `type` field on every gold entity. `type` (V1, 7-type vocab) is untouched and keeps scoring the current extractor. `v2_type` (V2, 8-type vocab) is the ADR-0109 label this ticket produces, validated against a new `ALLOWED_ENTITY_TYPES_V2` frozenset in `gold.py`. Bump `GOLD_SCHEMA_VERSION` "1.0" → "1.1" (shape changed, per the module's own bump discipline).

This keeps `make test` / the live harness green throughout, and cleanly separates "what the ADR says the type *should* be" (this ticket) from "what the extractor *actually* emits" (FRE-771).

**Cleanup gate for FRE-771 (codex finding #7, recorded here so it isn't lost):** when FRE-771 swaps the prompt and the extractor starts emitting V2 labels, `v2_type` must be *promoted* to `type` (V1 `type` retired, not kept as a second column), `ALLOWED_ENTITY_TYPES` flips to the 8-type set, and `GOLD_SCHEMA_VERSION` bumps again. This is FRE-771's job, noted here for continuity, not implemented by this ticket.

## Method — 3 independent blind annotators + measured IAA

Per the ADR's own precedent (FRE-766 spot-check: "gpt-5.4-mini (temp 0) + claude-sonnet-5 (adaptive)", cross-referenced against a `gpt-5.4` full-model leg in the same session) and the ticket's own suggested practical form ("two to three frontier models label the set blind"), use **3 model raters across two provider families**: `gpt-5.4-mini` (temp 0.0, OpenAI), `gpt-5.4` (full, temp 0.0 or provider default, OpenAI), `claude-sonnet-5` (adaptive, Anthropic). These are the same 3 model legs already registered in `cells.py` (`CELLS_BY_NAME`), so their id/provider/pricing are already known-good in this codebase.

**Codex finding #6 — don't overclaim independence.** Two of the three raters are same-provider (OpenAI) siblings; the ADR's strongest convergent-failure evidence used four *architecturally* distinct systems (including GLiNER's encoder). Framed honestly as "three model raters across two provider families," not "three independent annotators" in the strong sense. The IAA report includes a **pairwise agreement table by rater pair** (mini↔full, mini↔sonnet, full↔sonnet) so OpenAI-internal agreement is visible separately from cross-family agreement — this is what makes the method's actual signal legible rather than overclaimed.

**Blind** = each rater sees only: the entity's canonical name, the owning case's `source_user` + `source_assistant` text (context), and the 8 V2 GoLLIE definitions (inclusion + exclusion + example) copied verbatim from ADR-0109. Raters do **not** see the current V1 label, each other's answers, or the other raters' identity. Each rater returns exactly one of the 8 type keys + a one-line rationale.

**Why direct API calls, not the app's cost-gated `LiteLLMClient` (codex finding #4):** this is an offline research/labeling task, not a production code path — there is no extraction happening, just single-turn classification, and it doesn't go through `entity_extraction.py`'s `model_override` DI seam the way `bench.py`/`harness.py` do (those replay the *production extraction prompt*; this replays a *classification-only* prompt that doesn't exist in prod). Mirrors the ADR's own "direct API" spot-check precedent. This is a **deliberate, called-out exception** to the house "always route through LiteLLM + the cost gate" convention (`harness.py`'s own docstring) — flagged explicitly for owner approval below, not silently assumed. API keys are read via `from personal_agent.config import settings` (`settings.openai_api_key` / `settings.anthropic_api_key`) — never `os.getenv()`. Every run stamps: run-id, timestamp, exact prompt hash (the classification prompt, not the extraction prompt), model ids, and per-call token/cost totals in the raw (gitignored) telemetry — same provenance discipline as `harness.py`'s `RunMeta`, so a relabel run is never misread against a different prompt/model revision later.

Cost is trivial: ~65-75 entities (53 existing + ~12-15 new) × 3 raters × one short classification call each ≈ 200 calls of a few hundred tokens — well under a dollar at the registered per-token rates in `cells.py`. **Flagged for explicit owner approval before the real (non-dry-run) spend, per the open questions below** — this is new, unbudgeted API spend outside the cost-gate's visibility.

### New code

- `scripts/eval/fre630_extraction_quality/iaa.py` — **pure, unit-tested.**
  - `pairwise_agreement(rater_labels: Sequence[Sequence[str]]) -> float` — exact-match agreement rate across all rater pairs.
  - `pairwise_agreement_by_pair(rater_labels, rater_names) -> Mapping[tuple[str, str], float]` — per-rater-pair agreement (codex finding #6 — surfaces OpenAI-internal vs cross-family agreement separately).
  - `fleiss_kappa(rater_labels: Sequence[Sequence[str]], categories: Sequence[str]) -> KappaResult` — corrects for chance agreement given category prevalence (more honest than raw % per the ADR's own IAA framing); computed overall and per-type (one-vs-rest).
  - **Codex finding #5 — explicit undefined-kappa contract.** `KappaResult` is a frozen dataclass: `kappa: float | None`, `status: Literal["ok", "undefined_zero_variance"]`, `n_items: int`, `n_positive: int` (label prevalence), `raw_agreement: float`. When the expected chance-agreement term makes kappa's denominator zero (e.g. a one-vs-rest row with only one category ever observed across all raters — likely for sparse V2 types at n=3 raters), `kappa=None` and `status="undefined_zero_variance"` — **never** silently coerced to 0.0 or 1.0. The per-type report table renders `status` alongside `kappa` so an undefined row is legible, not hidden.
  - `IAAReport` frozen dataclass: overall `KappaResult` + `per_type: Mapping[str, KappaResult]` + `by_rater_pair: Mapping[tuple[str, str], float]` + `disagreements: Sequence[str]` (entity/case ids where raters split, tagged 2/3 vs 3/3-way).
  - Unit tests: synthetic fixtures — all-agree (kappa well-defined, agreement=1.0), a fixed disagreement pattern with a hand-computed expected kappa, and the zero-variance edge case asserting `status == "undefined_zero_variance"` and `kappa is None` (not just "doesn't raise").

- `scripts/eval/fre630_extraction_quality/relabel_v2_types.py` — **I/O driver** (like `harness.py`/`bench.py`, no dedicated unit tests beyond a `--dry-run` smoke that skips real API calls):
  - Loads `gold_extraction.yaml`, iterates every `(case, entity)` pair.
  - Builds the blind prompt per entity, fires the 3 raters concurrently (`asyncio.gather`), parses each rater's `{type, rationale}` (a small `TypedDict`/dataclass + tolerant JSON parse — same tolerant-parse spirit as the extractor's own fallback path, not a hard schema in a research script).
  - Writes the raw per-entity/per-rater records to `telemetry/evaluation/fre630-extraction-quality/v2-relabel-<run-id>.json` (**gitignored** — matches the existing convention that raw runs never land in git).
  - Computes `IAAReport` via `iaa.py`, prints a curated per-type table to stdout for pasting into the research doc.
  - CLI: `--run-id`, `--dry-run` (stub raters with fixed labels, for a fast smoke / CI-safe path), `--limit N` (subset for a quick check).

### Adjudication

**Codex finding #3 — adjudication metadata belongs in the schema, not just the research doc.** `GoldEntity` gains three new fields alongside `v2_type`: `v2_adjudicated: bool = False`, `v2_adjudication_rationale: str = ""`, `v2_needs_owner_signoff: bool = False`. The research doc's adjudication table is the *narrative* record; these fields are the *machine-checkable* record (a test can assert no entity is left with `v2_needs_owner_signoff=True` and no comment, etc.).

- **Unanimous (3/3 agree):** `v2_type` = the agreed label. `v2_adjudicated=False`, `v2_needs_owner_signoff=False`.
- **Majority (2/3):** the builder (this session) reads the ADR-0109 inclusion/exclusion/example text against the specific entity + context and either confirms the majority or overrides it with the minority pick when it is textually more correct against the GoLLIE definitions. `v2_adjudicated=True`, `v2_adjudication_rationale` set (one line), `v2_needs_owner_signoff=False` — a majority vote plus a reasoned builder ruling against explicit ADR criteria is treated as resolved, not provisional.
- **3-way split (codex finding #2 — resolves the plan's internal contradiction):** the builder still rules (so `v2_type` is never left empty — the harness/tests need a concrete value), records `v2_adjudicated=True` + rationale, but **additionally** sets `v2_needs_owner_signoff=True`. The AC-proof table below is worded accordingly: "disagreements adjudicated" means *every* entity gets a reasoned `v2_type` + rationale (true for both majority and 3-way cases); it does **not** mean *every* adjudication is final without follow-up — 3-way splits are explicitly flagged as provisional-pending-owner-confirmation, both in the gold file (`v2_needs_owner_signoff`) and called out by name in the final Linear ticket comment. Expected rare, given the ADR's own 9/10 and 5/5 cross-model agreement measurements on comparable cases.

### Coverage growth

**Codex finding #1 — the plan's first draft only re-added the ADR's own 5 spot-checked examples, which is regression coverage, not growth "beyond" them as the ticket requires.** Split explicitly into two buckets:

1. **Regression anchors (the existing 5, unchanged):** gravity, photosynthesis, the greenhouse effect, a black hole, the Maillard reaction — added as new small cases, each expected to reproduce the ADR's own 5/5 agreement finding. These confirm the pipeline reproduces known-good results; they are not the "growth."
2. **New coverage (≥5 additional cases, not in the ADR spot-check):** `spacetime` (explicitly named in the ADR as an unresolved risk case — "is spacetime a Phenomenon or a DomainOrTopic?", ADR-0109 Risks section) and at least one acoustics example (an owner domain named in the ADR's `Phenomenon` rationale but untested — e.g. resonance or the Doppler effect), plus 3-4 more MethodOrConcept↔DomainOrTopic boundary pairs beyond the existing Game Theory/Nash-Equilibrium and Behavioral-Economics/RAG cases: cybersecurity (DomainOrTopic) vs. a specific technique such as penetration testing or a buffer overflow (MethodOrConcept); cosmology (DomainOrTopic) vs. a specific method such as redshift measurement (MethodOrConcept).
3. **Relabel existing candidates, not just add new ones:** at least two *already-present* gold entities read as likely Phenomenon under the V2 definitions and should go through the rater pipeline as such rather than being ignored — `Cosmic Microwave Background` (`cosmology-cmb` case) and `Rayleigh Scattering` (`physics-scattering` case). These get a `v2_type` like every other entity; no YAML restructuring needed, just inclusion in the rater pass.

New cases carry a best-effort V1 `type` (so they load/score under the still-live V1 harness without breaking `make test`) and go through the same blind 3-rater + adjudication pipeline as every pre-existing entity for their `v2_type`.

### Re-baseline

After the gold file changes (new cases + populated `v2_type`, `type` unchanged), run the existing harness (`make test-infra-up` → `harness.py --run-id fre770-rebaseline-<date> --samples 3` → `make test-infra-down`) to produce a fresh V1 baseline reflecting the new case count — required regardless of the V1/V2 question, per the file's own header convention ("a gold change invalidates the historical baseline — re-baseline before any A/B"). Curated summary (not the raw JSON) goes into the research doc; raw output stays gitignored.

### Tests (written first, TDD)

- `tests/evaluation/test_fre630_gold_set.py` (extend): `test_all_entities_have_v2_type` (every entity's `v2_type` is set and in `ALLOWED_ENTITY_TYPES_V2`), `test_phenomenon_coverage` (≥5 entities with `v2_type == "Phenomenon"`, covering both the regression anchors and at least one of the relabeled pre-existing entities), `test_no_unresolved_signoff_without_rationale` (every `v2_needs_owner_signoff=True` entity has a non-empty `v2_adjudication_rationale`), bump `MIN_CASES` if the new count requires it.
- `tests/evaluation/test_iaa.py` (new): pure unit tests for `pairwise_agreement`, `pairwise_agreement_by_pair`, and `fleiss_kappa` per above, including the explicit `status == "undefined_zero_variance"` / `kappa is None` assertion.
- `gold.py`: `GoldEntity.v2_type: str = ""`, `v2_adjudicated: bool = False`, `v2_adjudication_rationale: str = ""`, `v2_needs_owner_signoff: bool = False` (all optional, defaulting empty/false until relabeled — but by ticket completion every entity must have `v2_type` set, enforced by the new test). `_parse_entity` validates `v2_type` against `ALLOWED_ENTITY_TYPES_V2` **only when non-empty** (keeps the loader tolerant during incremental authoring, while the committed final gold has 100% coverage per the test).

### Deliverable: research doc

`docs/research/2026-07-04-fre-770-gold-relabel-iaa.md` — method, the 8 V2 definitions used (verbatim from ADR-0109), the per-type IAA/kappa table, the adjudication table (every majority/split case + rationale), the new cases added, the re-baseline numbers, and any residual open questions flagged for owner review (the 3-way splits, if any).

## Acceptance-criteria proof plan

| AC (ticket) | Proof |
|---|---|
| Gold re-labeled to 8 types by ≥2 independent annotators | `v2_type` populated on every entity via the 3-model-rater (2-provider-family) pipeline; `test_all_entities_have_v2_type` |
| Reported IAA per type | `iaa.py` per-type `KappaResult` table + rater-pair agreement table in the research doc |
| Disagreements adjudicated | Every majority/split entity gets `v2_adjudicated=True` + rationale; 3-way splits additionally flagged `v2_needs_owner_signoff=True` and named individually in the final ticket comment |
| Boundary coverage grown | ≥5 new-beyond-ADR Phenomenon/boundary cases (spacetime, acoustics, +boundary pairs) plus 2 relabeled pre-existing entities (CMB, Rayleigh Scattering); `test_phenomenon_coverage` |
| Benchmark re-baselined | Fresh `harness.py` run recorded in the research doc |

## Decisions needed before coding (surfaced to the owner, not decided unilaterally)

1. **Dual-field design** (`type` stays V1, `v2_type` added) — sound per codex review; FRE-771 promotes `v2_type`→`type` later. No objection raised; proceeding unless told otherwise.
2. **Real API spend approval:** this plan proposes ~200 direct (non-cost-gated) model calls to gpt-5.4-mini, gpt-5.4, and claude-sonnet-5 for blind classification — well under $1 at registered rates, but outside the ADR-0065 cost gate's visibility since it bypasses `LiteLLMClient` entirely (codex finding #4 flags this as a deliberate exception needing explicit sign-off, not a silent assumption). **Requesting confirmation before making real calls** — a `--dry-run` path (stubbed raters) is available to build/test everything else first.
3. **3-way-split handling:** builder rules on every entity (so `v2_type` is never empty) but flags 3-way splits with `v2_needs_owner_signoff=True` for post-hoc confirmation, named individually in the final ticket comment. This resolves the earlier internal contradiction (codex finding #2) — flagging for awareness, not asking permission, since it's a mechanical consequence of "adjudicate now, confirm later."
