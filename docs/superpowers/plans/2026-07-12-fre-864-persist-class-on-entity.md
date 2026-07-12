# FRE-864 — Persist Personal/World class on the Entity node at write

**Backing ADR:** ADR-0115 (Accepted 2026-07-11), D2 (persistence seam). Acceptance criteria carried by
this ticket: **AC-1** (class persists with the right value) and **AC-4** (fail-open preserves the
uncertain item). Dispatch (AC-2/AC-3/AC-5) is FRE-728 — out of scope here. Class-aware ranking is
unowned follow-up (D6) — out of scope here.

**Clarification (master, post FRE-863 merge):** the Entity class enum is `{World, Personal}` ONLY.
Stance is not an entity class — it persists on the `HAS_STANCE` edge already. Do not add `Stance` to
the Entity class field.

## Scope

FRE-863 (merged) already made the extractor emit `entity["class"] ∈ {World, Personal}` (fail-open to
`World`) per entity in `entity_extraction.py`. This ticket closes the persistence gap the ADR
describes: the consolidator's `Entity(...)` construction drops `entity_data["class"]`, the `Entity`
model has no field for it, and `create_entity`'s MERGE never sets it — only `Claim` and the
`HAS_STANCE` edge currently persist a class. ~7,992 existing `:Entity` nodes carry no class (backfill
of those is a separate, later ticket — not this one).

## Plan

1. **`src/personal_agent/memory/models.py`** (`Entity`, ~line 31) — add
   `knowledge_class: Literal["World", "Personal"] | None = None` (named `knowledge_class` not `class`
   — reserved word in Python; matches `Claim.knowledge_class`'s naming, writes to the Neo4j property
   `class` just like `Claim` does). `Literal` (not bare `str`) per this project's discriminated-union
   standard — makes `System`/`Stance`/typos unrepresentable at construction instead of needing a
   defensive check in `create_entity` (codex plan-review flagged the validation gap; `Claim.
   knowledge_class` stays untouched — bare `str` — since it's out of this ticket's scope). Docstring
   note referencing ADR-0115 D2.

2. **`src/personal_agent/second_brain/consolidator.py`** (~line 682, the `Entity(...)` construction in
   the entity-creation loop) — thread `knowledge_class=entity_data.get("class")` through so the
   extractor's per-entity class reaches the model.

3. **`src/personal_agent/memory/service.py`** (`create_entity`, ~line 1236):
   - When `entity.knowledge_class is not None`, append to `set_clauses`:
     `"e.class = CASE WHEN e.class IS NULL OR e.class = '' THEN $class ELSE e.class END"` and set
     `params["class"] = entity.knowledge_class`. First-write-wins, mirroring the existing
     `entity_type`/`properties` clauses (FRE-375 convention) — conditionally appended like
     `coordinates`/`geocoded` so non-extraction callers (gateway `store_fact`) that never set
     `knowledge_class` don't write a stray `None`.
   - Add `ensure_entity_class_index()` (mirrors `ensure_fulltext_index()`'s shape exactly): idempotent
     `CREATE INDEX entity_class_index IF NOT EXISTS FOR (e:Entity) ON (e.class)`.
   - Note (codex plan-review): `create_conversation`'s inline `DISCUSSES`-edge MERGE
     (`service.py:1056`) can create the `:Entity` node first, without a class, before this ticket's
     `create_entity` call runs later in the same consolidation pass. Harmless — the class SET clause
     here is not `ON CREATE`-only, so it still applies first-write-wins on that already-existing node
     — but worth a one-line comment at the new SET clause so a future reader doesn't assume the node
     is always fresh.

4. **`src/personal_agent/service/app.py`** (~line 649, alongside the `ensure_fulltext_index()` startup
   call) — add a matching non-fatal `try`/`except` block calling
   `await memory_service.ensure_entity_class_index()`.

5. **Tests** (TDD — write failing first). Note on coverage boundary (codex plan-review): FRE-863
   already landed thorough unit coverage of the *extraction-normalization* half of AC-1/AC-4 —
   `tests/test_second_brain/test_entity_extraction_contract.py::test_every_entity_has_valid_class`
   (asserts every entity's class ∈ {World, Personal}) and
   `::test_operational_turn_emits_finding_output_kind` (asserts fail-open to `class=World` even for
   `output_kind=finding` items). This ticket's tests prove the *next* seam — `entity_data["class"]` →
   `Entity` → persisted `e.class` on Neo4j — which is the actual gap AC-1/AC-4 name (`class=None` in
   Core today). Fixtures below use entity dicts shaped exactly like real extraction output (same
   `"class"` key, same value domain already proven above), not simulations of normalization.
   - `tests/personal_agent/memory/test_entity_class_persistence.py` — mocked-driver unit tests
     (pattern: `test_neo4j_origination_properties.py`): SET clause carries the first-write-wins CASE
     for `class` when `knowledge_class` is set (`"Personal"` and `"World"` cases); the `class` clause/
     param is absent entirely when `knowledge_class` is `None` (parity with the `coordinates`/
     `geocoded` conditional-append pattern — proves `store_fact` callers aren't forced into a stray
     write).
   - `tests/test_second_brain/test_consolidator_entity_class_wiring.py` — mocked `memory_service`
     unit test (pattern: `test_consolidator_claims_wiring.py`): extraction result entity dict with
     `"class": "Personal"` (and separately `"World"`) flows into the `Entity` object passed
     positionally to `memory_service.create_entity` — assert
     `create_entity.await_args.args[0].knowledge_class == "Personal"` / `"World"`.
   - `tests/personal_agent/memory/test_entity_class_persistence_live.py` — `pytest.mark.integration`
     (pattern: `test_world_description_correction.py`, runs against the test Neo4j substrate,
     `pytest.skip` if unavailable):
     - **AC-1**: `create_entity(Entity(name="FRE864_Chen", ..., knowledge_class="Personal"))` →
       `MATCH (e:Entity {name:$n}) RETURN e.class` == `"Personal"`; a second entity with
       `knowledge_class="World"` → `e.class == "World"`.
     - **AC-4**: an entity written with `knowledge_class="World"` (simulating the extractor's
       fail-open default for an unclassifiable item) exists in Core — `e.class == "World"`, node
       present, not dropped.
     - First-write-wins regression: create with `knowledge_class="Personal"`, re-create the same
       name with `knowledge_class="World"` → stays `"Personal"` (not overwritten) — same guarantee
       `entity_type`/`properties` already have.
     - `ensure_entity_class_index()` returns `True` against the live test substrate.

## Test commands

```bash
make test-file FILE=tests/personal_agent/memory/test_entity_class_persistence.py
make test-file FILE=tests/test_second_brain/test_consolidator_entity_class_wiring.py
make test-infra-up   # if not already running
PERSONAL_AGENT_INTEGRATION=1 make test-file FILE=tests/personal_agent/memory/test_entity_class_persistence_live.py
make test            # full suite
make mypy
make ruff-check
make ruff-format
```

## Not in scope

- `output_kind` dispatch (Core/ES/sysgraph routing) — FRE-728.
- Backfill of the ~7,992 existing `class=None` entities — separate ticket per the ADR.
- Class-aware ranking in the ADR-0104 structural arm — unowned follow-up (D6).
- ADR-0106/0098/0097 status-line consolidation edits (AC-6) — already handled by FRE-863's merge per
  the ticket history; not touched here.
- Until FRE-728 (dispatch) ships, the consolidator's entity-creation loop is unchanged: it still
  writes every entity from `extraction_result["entities"]` regardless of `output_kind`, including
  `finding`/`ephemeral`-natured items. This ticket only makes those writes carry the correct `class`
  once written — it does not change *whether* they're written. That gap is AC-2/AC-3's job (FRE-728).
