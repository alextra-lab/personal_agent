# FRE-725 — World description enrichment signal (ADR-0098 D2)

**Ticket:** FRE-725 (Approved, Tier-1:Opus, project Memory Recall Quality)
**Backing ADR:** ADR-0098 D2 (`docs/architecture_decisions/ADR-0098-memory-substrate-and-lifecycle-architecture.md`)
**Builds on:** FRE-711 (World-fact living descriptions / correction gate, #308) · mirrors FRE-712 (claim `update_kind` contradiction signal, #303)

---

## Problem

FRE-711 made the World-fact `description` a **living** value: `create_entity` corrects it when the
new write has **strictly greater** confidence, archiving the prior value to a
`(:Entity)-[:HAD_DESCRIPTION]->(:EntityDescriptionVersion)` node; an eval write can never overwrite a
non-eval description.

But every conversation extraction lands at the same source confidence —
`KnowledgeWeight.from_source("conversation").confidence == 0.8 == _DEFAULT_DESCRIPTION_CONFIDENCE`.
So the strict `>` gate **never fires** for normal same-source re-extraction. A **thin but non-empty**
first description (e.g. `"A database"`) can never be enriched by a later, richer, same-confidence
description (e.g. `"A graph database management system used for the knowledge store"`). ADR-0098 line 29
names this exact bug ("a wrong or **thin** first description is permanent"); line 151 flags the
equal-confidence description-improvement path as the tuning decision D2 deferred.

## Fix (borrow FRE-712's vocabulary+defaulting pattern — but the gate relaxation is new)

Let the extractor emit an explicit **per-entity description signal**; honor it in the correction gate so
an explicitly-flagged enrichment/correction supersedes a non-empty description **at equal confidence**,
still archiving the old value and still blocked for eval-over-non-eval traffic.

> **Not identical to FRE-712.** FRE-712's `update_kind` only selects the *supersession-reason label*
> **after** the FRESH/REJECT safety checks pass (`supersession.py:172,188`) — it never relaxes a write
> gate. FRE-725 genuinely **relaxes the write gate** (equal-confidence supersession that strict-`>`
> would block). What we borrow from FRE-712 is only the **vocabulary + Python-owned-defaulting**
> pattern, not its safety semantics. Because the signal here is *write-authorizing*, it must be
> **normalized/validated server-side** (below), and the "enrichment" arm carries a deterministic
> non-shrinking guard so a noisy signal cannot degrade a good description.

- **Vocabulary:** `description_update_kind ∈ {"new", "enrichment", "correction"}`, default `"new"`.
  Explicit set that unlocks equal-confidence supersession: `{"enrichment", "correction"}`.
- **Signal semantics are IN-TURN observable** (Codex finding 2): the extractor runs per-turn on
  `(user_message, assistant_response)` and **never sees the stored KG description**, so the signal must
  be judgeable from the turn alone — it cannot mean "richer than the stored value":
  - `correction` — the turn **explicitly corrects/contradicts** an earlier statement about this entity
    ("actually X is Y, not Z"; "I was wrong, it's …").
  - `enrichment` — the turn **substantively defines/explains** the entity (a real definition or
    characterization), as opposed to a passing mention.
  - `new` — default: a passing mention with no correction/definition intent.
- **Gate relaxation** — the FRE-711 `_do_correct` predicate keeps its strict-`>` arm and gains a
  signal arm; the two are OR-ed:
  ```
  new non-empty AND old non-empty AND new <> old AND NOT(eval clobber of non-eval)
  AND (
        new_conf > coalesce(old_conf, default)                       -- FRE-711 (unchanged, unsignaled)
     OR ( kind ∈ explicit_kinds
          AND new_conf >= coalesce(old_conf, default)                -- FRE-725 (signal, equal-or-higher)
          AND ( kind = 'correction' OR size(new) >= size(old) ) )    -- enrichment must not SHRINK
      )
  ```
  Faithful invariants preserved:
  - **No downgrade.** The signal arm requires `>=`, so a *lower*-confidence write can never clobber even
    with a signal.
  - **Enrichment cannot degrade.** At equal confidence, an `enrichment` may only land if it does not
    shrink the description (`size(new) >= size(old)`) — faithful to the ticket's "thin → *richer*"
    framing and killing the lateral/shorter-churn vector (Codex findings 2 & 6). `correction` is
    length-free (a genuine fix may be shorter). The strict-`>` arm is unaffected — a higher-confidence
    source may still shorten.
  - **Eval gate intact.** `NOT ($eval_mode AND old_eval = false)` is outside the OR — an eval write still
    cannot clobber a non-eval description, signalled or not.
  - **FRE-711 preserved for unsignaled writes.** `description_update_kind = "new"` (the default) still
    requires strict `>` — a same-confidence re-extraction with no signal is still a no-op.
  - **Empty-fill (`_do_fill`) untouched.**

## Design decision — signal-driven, no length *heuristic* but a length *guard*

The gate keys on the extractor's explicit in-turn signal (no arbitrary "thinness" *threshold* on the old
value). The one deterministic constraint is the **enrichment non-shrinking guard** above — not a
heuristic classifier, just an information-preservation invariant that makes "enrichment" mean what it
says. The signal is **write-authorizing**, so unlike FRE-712 it is **normalized server-side** in
`create_entity` (not only in the extractor) — a direct/test caller passing an off-vocabulary or `None`
kind is coerced to `"new"` **before** it reaches Cypher (Codex finding 3).

---

## Files & changes

### 1. Extractor contract — `src/personal_agent/second_brain/entity_extraction.py`
- Add constant near `_VALID_UPDATE_KINDS`:
  `_VALID_DESCRIPTION_UPDATE_KINDS = frozenset({"new", "enrichment", "correction"})`.
- Add `_normalize_description_update_kind(value) -> str` (off-vocabulary → `"new"`), mirroring
  `_normalize_update_kind`.
- Prompt: add `"description_update_kind": "new|enrichment|correction"` to the entity JSON schema block;
  add an EXTRACTION RULE (14) with the **in-turn** semantics (§Fix): `correction` = the turn explicitly
  corrects an earlier statement about the entity; `enrichment` = the turn substantively defines/explains
  the entity (not a passing mention); `new` = default. Explicitly state the model is judging **this
  turn's intent**, never comparing to a stored description. Add one GOOD example. Keep it a
  *description-only* signal — never conflated with stance/claim.
- `_finalize_extraction`: in the entity loop, stamp
  `entity["description_update_kind"] = _normalize_description_update_kind(entity.get("description_update_kind"))`
  (Python owns defaulting, like `class`).

### 2. Correction gate — `src/personal_agent/memory/service.py`
- Module constants near `_DEFAULT_DESCRIPTION_CONFIDENCE`:
  `_DEFAULT_DESCRIPTION_UPDATE_KIND = "new"` and
  `_EXPLICIT_DESCRIPTION_UPDATE_KINDS = ("enrichment", "correction")`.
- `create_entity` signature: add `description_update_kind: str = _DEFAULT_DESCRIPTION_UPDATE_KIND`;
  document it in the docstring (Args) — the FRE-725 equal-confidence enrichment lever.
- **Server-side normalization (Codex finding 3)** — before binding, coerce a caller-supplied kind to the
  valid vocabulary so an off-vocabulary/`None` value from a direct caller can never authorize a write:
  `kind = description_update_kind if description_update_kind in _VALID_DESCRIPTION_UPDATE_KINDS else _DEFAULT_DESCRIPTION_UPDATE_KIND`
  where `_VALID_DESCRIPTION_UPDATE_KINDS = frozenset({_DEFAULT_DESCRIPTION_UPDATE_KIND, *_EXPLICIT_DESCRIPTION_UPDATE_KINDS})`.
  (service.py must not import from `second_brain` — keep this a local inline guard, no cross-layer dep.)
- Bind params: `"description_update_kind": kind` (the normalized value),
  `"explicit_description_update_kinds": list(_EXPLICIT_DESCRIPTION_UPDATE_KINDS)`.
- Extend the `_do_correct` Cypher expression to the exact OR form:
  ```
  ... AND ($description_confidence > coalesce(_old_conf, $default_description_confidence)
           OR ($description_update_kind IN $explicit_description_update_kinds
               AND $description_confidence >= coalesce(_old_conf, $default_description_confidence)
               AND ($description_update_kind = 'correction' OR size($description) >= size(_old_desc))))
  ```
  **Verify during TDD:** Neo4j 5.26 `size()` on a string — confirm it returns character length in the
  integration run; if the version rejects `size()` on a string, fall back to the non-deprecated
  string-length function. The cypher-shape unit test locks the expression; the integration test proves
  runtime behaviour.

### 3. Consolidator wiring — `src/personal_agent/second_brain/consolidator.py`
- In the entity loop's `create_entity(...)` call, thread
  `description_update_kind=entity_data.get("description_update_kind", "new")`.

---

## Tests (TDD — write failing first)

### A. Gate shape (unit, mocked driver) — `tests/personal_agent/memory/test_entity_description_correction_cypher.py`
- Extend `test_description_uses_gated_correction_not_first_write_freeze`: assert the Cypher still contains
  the strict-`>` arm **and** now the signal arm (`IN $explicit_description_update_kinds`, a `>=` on
  `$description_confidence`, and the `size(...) >= size(...)` enrichment guard). **Replace** the brittle
  line-69 `">=" not in ...` assertion (FRE-725 adds a legitimate `>=`) with: the FRE-711 strict arm
  `$description_confidence > coalesce` is present, AND the signal arm `$description_confidence >= coalesce`
  is present, AND the enrichment guard `size($description) >= size(_old_desc)` is present.
- Extend `test_new_params_are_bound`: assert `merge_params["description_update_kind"]` and
  `merge_params["explicit_description_update_kinds"]` are bound.
- **New** `test_off_vocabulary_kind_is_normalized_server_side`: call `create_entity` with
  `description_update_kind="bogus"` → the bound `merge_params["description_update_kind"] == "new"`
  (Codex finding 3 — write-authorizing signal validated in the service, not just the extractor).

### B. Extractor contract (unit) — `tests/test_second_brain/test_entity_extraction_contract.py`
- `description_update_kind` defaults to `"new"` when the model omits it.
- A model-emitted `"enrichment"` / `"correction"` is preserved on the entity.
- Off-vocabulary (`"enriched"`, `"updated"`) normalizes to `"new"`.

### B2. Consolidator wiring (unit) — `tests/test_second_brain/test_consolidator_claims_wiring.py`
- Extend the existing entity-wiring assertion (currently checks `eval_mode`/`description_confidence`) to
  assert `create_entity` receives `description_update_kind` threaded from the extracted entity dict
  (Codex additional risk).

### C. Behavioural ACs (integration, live test-Neo4j :7688) — `tests/personal_agent/memory/test_world_description_correction.py` (append FRE-725 block)
- **AC-725-1** enrichment lands at equal confidence: seed `"A database"` @0.8 (`new`); write a **longer**
  `"A graph database management system"` @0.8 with `description_update_kind="enrichment"` → current
  description is the enriched value.
- **AC-725-1b** correction lands at equal confidence: seed `"A document database"` @0.8; write
  `"A graph database"` @0.8 with `description_update_kind="correction"` → current = corrected value
  (length-free arm; the new value is shorter, still lands).
- **AC-725-2** original retained: the superseded value is archived as an `EntityDescriptionVersion`
  with `valid_to` + provenance.
- **AC-725-3** eval still cannot clobber: seed non-eval @0.8; eval write @0.8 with
  `description_update_kind="correction"` → unchanged, no version node.
- **AC-725-4 (no-downgrade / FRE-711 preserved)**: (a) same-confidence `description_update_kind="new"`
  is still a no-op; (b) `description_update_kind="enrichment"` at **lower** confidence (0.5 < 0.8) does
  **not** overwrite.
- **AC-725-5 (enrichment non-shrinking guard)**: seed a **rich** `"A graph database management system
  used as the knowledge store"` @0.8; write a **shorter** lateral `"A graph DB"` @0.8 with
  `description_update_kind="enrichment"` → **unchanged**, no version node (Codex churn negative case).
- **AC-725-6 (new-entity default path)**: first-ever `create_entity` with the default kind stamps the
  description via `ON CREATE` and creates **no** `HAD_DESCRIPTION` node (no archive on birth).
- FRE-711 AC-1..AC-6 remain green (unchanged) — they are the broader regression.

---

## Acceptance criteria (proof of Done)

| # | Criterion (ADR-0098 D2 / ticket acceptance direction) | Proof |
|---|---|---|
| AC-725-1 | Thin non-empty description enriched by a later **same-confidence** description with an explicit enrichment signal | integration `test_ac725_1_enrichment_at_equal_confidence` |
| AC-725-1b | Explicit **correction** supersedes at equal confidence (length-free) | integration `..._1b_correction_at_equal_confidence` |
| AC-725-2 | The superseded value is retained as a version node (audit trail) | integration `..._2_original_retained_as_version` |
| AC-725-3 | Eval writes still cannot clobber a non-eval description, even with a signal | integration `..._3_eval_cannot_clobber_with_signal` |
| AC-725-4 | No downgrade / FRE-711 preserved: unsignaled same-conf no-op; signaled lower-conf no-op | integration `..._4_no_downgrade_and_unsignaled_noop` |
| AC-725-5 | Enrichment **non-shrinking guard**: shorter lateral enrichment at equal conf does NOT overwrite | integration `..._5_enrichment_cannot_shrink` |
| AC-725-6 | New-entity default path stamps via `ON CREATE`, no archive on birth | integration `..._6_new_entity_no_archive` |
| AC-725-7 | Signal validated **server-side** (write-authorizing): off-vocab kind → `new` before Cypher | unit `test_off_vocabulary_kind_is_normalized_server_side` |
| AC-725-8 | Extractor emits + Python-normalizes the per-entity signal (default `new`, off-vocab → `new`) | unit contract tests (B) |
| AC-725-9 | Consolidator threads `description_update_kind` into `create_entity` | unit wiring test (B2) |
| AC-725-10 | FRE-711 AC-1..AC-6 still hold | existing integration file green |

---

## Quality gates
`make test-file FILE=tests/personal_agent/memory/test_entity_description_correction_cypher.py` →
`make test-file FILE=tests/test_second_brain/test_entity_extraction_contract.py` → `make test` (full) →
`make mypy` → `make ruff-check` + `make ruff-format` → `pre-commit run --all-files`.
Integration ACs run against test-Neo4j (`make test-infra-up`; :7688) — they carry `@pytest.mark.integration`
(out of `make test`), run explicitly.

## Identity threading (ADR-0074)
No new `log.*` / `bus.publish` / Cypher `MERGE|CREATE` beyond the existing FRE-711 gate (the
`HAD_DESCRIPTION` archive `CREATE` is unchanged and already allowlisted). The gate change is predicate-only.
Re-check the allowlist only if line numbers shift the pre-existing Entity `MERGE` entry.
