# FRE-712 — Structured claim-facet key + extractor contradiction signal

**Ticket:** FRE-712 (Approved, Tier-1) · **Backing ADR:** ADR-0098 D2 · **Builds on:** FRE-637 (extractor contract) + FRE-638 (Claim supersession)
**Raised by:** the Codex plan-review of FRE-638 (findings #2 fuzzy matching, #3 heuristic correction/evolution label).

## Problem (from the ticket)
FRE-638 matches a new Claim to the current one by **content-embedding similarity alone** (no predicate/facet),
and labels correction-vs-evolution by a **confidence-delta heuristic**. Two consequences:
1. **False collide** — two genuinely distinct but embedding-similar facts ("rent is $2000" vs "lease ends March")
   can be treated as the same slot and wrongly supersede.
2. **Heuristic label** — correction vs evolution is guessed from confidence delta, not a real signal.

## Design (additive, backward-compatible)

Two new **per-claim** fields the extractor emits; two consumers honor them. Both default so pre-existing
behavior (FRE-638 embedding matching) is preserved when the fields are absent.

### Extractor emits (entity_extraction.py)
Each claim object gains:
- **`facet`** — a short, normalized, *stable* slot key for the fact (`lease_end_date`, `employer`,
  `current_city`). Same underlying fact → same facet across turns. Lowercase snake, subject+attribute.
  `""` when the model can't name one.
- **`update_kind`** — `new` | `correction` | `evolution`. `correction` when the user's language signals
  *fixing a prior mistake* ("actually", "I was wrong", "not X, it's Y"); `evolution` when the fact
  *changed* ("now", "as of", "we moved to", "extended to"); `new` (default) otherwise.

Python (`_finalize_extraction`) owns defaulting/validation (like `class`): `facet` → normalized str or `""`;
`update_kind` → validated against `{new, correction, evolution}`, default `new`.

### Matching: facet as *weighted evidence*, not an absolute gate (supersession.py)
`ClaimRecord` gains `facet`. Codex #2 killed the naive "different facets never match" hard-block — LLM
facet strings **drift across turns** ("lease_end_date" vs "current_lease_expiration" for the same fact),
so a hard block leaves the stale + new fact both current (the very bug we fix). Instead facet sets a
**per-pair embedding threshold** — agreement lowers the bar, disagreement raises it, absence is neutral:

| new.facet vs cand.facet | match threshold | rationale |
|---|---|---|
| equal (non-empty) | `SAME_FACET_FLOOR = 0.60` | facet agreement → deterministic same-slot grouping at low semantic bar |
| differ (both non-empty) | `DIFF_FACET_FLOOR = 0.95` | facet disagreement is evidence-against; only near-identical content overrides drift |
| either empty | `CLAIM_MATCH_THRESHOLD = 0.83` | neutral — exactly FRE-638 behavior (legacy claims, no-facet claims) |

`matching_candidates(new_facet, new_embedding, candidates)` returns every current claim whose cosine ≥ its
per-pair threshold. This blocks a *moderate*-similarity different-facet false-collide (0.85 < 0.95) yet
still merges a *near-identical* different-facet drift case (≥ 0.95).

**Adjudicate against the strongest safety blocker (Codex #1):** among the matched set, pick the candidate
with the **highest confidence** (ties → freshest `observed_at`) for the `adjudicate` comparison. So if *any*
matched claim outranks the new one on confidence, REJECT fires — we never supersede past a stronger claim.
On SUPERSEDE, invalidate the **whole matched set** (≤1 current per slot self-heals).

`adjudicate` gains `new_update_kind`: on SUPERSEDE the **reason is the explicit signal** when it is
`correction`/`evolution`; only when `new`/absent does it fall back to the FRE-638 confidence-delta heuristic.
The explicit signal drives the *label only* — the FRESH/REJECT (weaker, stale) **safety** decision is
unchanged. This is the honest scope: FRE-712 makes the correction-vs-evolution **label** signal-driven
instead of guessed; the bitemporal/contradiction *mechanism* is FRE-638's and stays as-is (Codex #3).

### Storage + wiring (default-safe — Codex #5)
- `Claim` model (models.py): add `facet: str = ""`, `update_kind: str = "new"`.
- `assert_claim` (service.py): fetch `cl.facet`; **coerce `row["facet"] or ""`** so a legacy `:Claim` with a
  missing property (reads back as `None`) is treated as empty, not the string `"None"`; build `ClaimRecord`
  with facet; use `matching_candidates` for the invalidate-set and the highest-confidence match for
  adjudication; pass `new_update_kind`; store `facet`/`update_kind` on the new `:Claim`.
- `_build_claim` (consolidator.py): thread `facet`/`update_kind` (defaulted) from the extractor dict.
- `_finalize_extraction`: `facet` → normalized str or `""`; `update_kind` → validated against
  `_VALID_UPDATE_KINDS = {new, correction, evolution}`, off-vocabulary → `new`.

## Acceptance criteria (the definition of done — derived from the ticket's acceptance direction + ADR-0098 D2)
- **AC-A (facet groups the same slot):** two claims with the **same** `facet` and modest similarity
  (≥ 0.60) supersede (deterministic slot grouping). *Fails if* the same-facet re-assertion creates a 2nd current claim.
- **AC-B (facet disambiguates — the #2 fix):** two claims with **moderate** embedding similarity (≈0.85, above
  the FRE-638 base) but **different non-empty** `facet` do **NOT** supersede — both remain current.
  *Fails if* they collide into one slot.
- **AC-C (drift recovery):** two same-fact claims with **different** `facet` strings but **near-identical**
  content (similarity ≥ 0.95) **do** supersede — facet drift does not strand the stale fact.
- **AC-C2 (base/legacy preserved):** claims with **empty/absent** `facet` (e.g. FRE-638 legacy rows) match at
  the base 0.83 threshold; a new facet'd claim still supersedes an old no-facet claim. *Fails if* fetched
  `None` facet is treated as non-empty and breaks FRE-638-compatible matching.
- **AC-D (explicit correction label):** `update_kind=correction` supersedes with
  `supersession_reason='correction'` **even at equal confidence** (where the heuristic would say 'evolution').
- **AC-E (explicit evolution label):** `update_kind=evolution` → `supersession_reason='evolution'`.
- **AC-F (safety-blocker + FRE-638 regression):** a matched set containing a **higher-confidence** current claim
  makes a weaker new claim REJECT (never supersede past the stronger one, Codex #1); FRE-638 AC-1/AC-2 still pass.
- **AC-G (contract):** the extractor output carries `facet` (str) + a validated `update_kind` on every claim;
  off-vocabulary values normalize to `new`.

## Files
| File | Change |
|------|--------|
| `src/personal_agent/second_brain/entity_extraction.py` | prompt (CLAIMS block + JSON template) + `_finalize_extraction` default/validate `facet`/`update_kind`; `_VALID_UPDATE_KINDS` const |
| `src/personal_agent/memory/models.py` | `Claim`: +`facet`, +`update_kind` |
| `src/personal_agent/memory/supersession.py` | `ClaimRecord.facet`; `matching_candidates(...)`; `adjudicate(new_update_kind=...)` |
| `src/personal_agent/memory/service.py` | `assert_claim`: fetch/store facet+update_kind; facet-aware match/invalidate |
| `src/personal_agent/second_brain/consolidator.py` | `_build_claim`: thread facet+update_kind |

## Steps (TDD — failing test first each)
1. **Model** — add fields; `uv run python -c "from personal_agent.memory.models import Claim"`.
2. **supersession** — `tests/personal_agent/memory/test_supersession.py` (extend): facet-exact match; cross-facet block; empty-facet embedding backstop; `update_kind` drives reason; heuristic fallback when `new`. Implement `matching_candidates` + `adjudicate(new_update_kind)`. Keep existing FRE-638 cases green.
3. **Extractor contract** — `tests/test_second_brain/test_entity_extraction_contract.py` (extend): every claim gets `facet` (str) + `update_kind` ∈ valid set (default `new`); bad `update_kind` → `new`. Implement prompt + `_finalize_extraction`.
4. **service `assert_claim`** — extend `tests/personal_agent/memory/test_claims_stance_cypher.py`: fetch selects `cl.facet`; write stores `facet`/`update_kind`; supersede invalidate-set uses facet.
5. **consolidator** — extend `tests/test_second_brain/test_consolidator_claims_wiring.py`: facet+update_kind threaded onto the Claim.
6. **Behavioural AC proof (integration, live :7688, embedder patched)** — extend `tests/personal_agent/memory/test_claims_stance_storage.py`: AC-A, AC-B, AC-C, AC-D, AC-E; AC-F = the untouched FRE-638 AC-1/AC-2 tests still pass.

## Quality gates
`make test` (module then full) · `make mypy` · `make ruff-check`+`ruff-format` · `pre-commit run --all-files`.
ADR-0074 identity threading unchanged (no new emit sites; facet/update_kind ride the existing Claim write).

## Out of scope / follow-ups
- World-fact-as-Claim migration is FRE-711 (separate). This ticket only enriches the **Personal** claim path.
- Facet-key **stability** across turns is best-effort (LLM-emitted); the embedding backstop (AC-C) is the
  safety net for drift. A learned/canonicalized facet registry is a later refinement if drift proves costly.
