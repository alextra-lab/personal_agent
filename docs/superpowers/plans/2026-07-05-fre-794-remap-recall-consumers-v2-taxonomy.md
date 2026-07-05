# FRE-794 — Remap recall consumers to the ADR-0109 V2 entity taxonomy

**Backing:** ADR-0109 (Entity & Relationship Taxonomy Redesign), the FRE-769 downstream-impact
check (`docs/research/2026-07-04-fre-769-recall-type-downstream-impact.md`), reviving scope
FRE-771 shipped the prompt for but did not touch.

## Scope confirmed by source read

Grep across `src/personal_agent/` for the retired V1 type-string literals (`"Technology"`,
`"Topic"`, `"Concept"`, excluding V2 names that contain those as substrings, e.g.
`TechnicalArtifact`/`DomainOrTopic`/`MethodOrConcept`) turns up exactly:

1. `orchestrator/executor.py:697-720` — `_ENTITY_TYPE_KEYWORDS`, the only place that *writes*
   literal V1 strings into the live recall-intent filter.
2. `tools/memory_search.py:39-48` — the `search_memory` tool's `entity_types` parameter
   description, which documents the V1 7-type vocabulary to the LLM.
3. `memory/dedup.py:62` — a docstring example only (`entity_type: Entity type (e.g.,
   "Technology")`). `check_entity_duplicate`/`_find_similar_entities` take `entity_type` as a
   parameter and filter `WHERE node.entity_type = $entity_type` — fully generic, no hardcoded
   type value. It already operates correctly on whatever string its caller (`create_entity`,
   passed `entity.entity_type` from the extractor) supplies. No functional change needed; only
   the misleading docstring example and a proof-test.
4. `memory/models.py:35` — a stale inline comment on `Entity.entity_type` (`# "Person", "Place",
   "Topic", "Concept", etc.`). Not a live consumer (not named by the FRE-769 doc, not in this
   ticket's explicit scope) — leaving untouched per surgical-change discipline.

The ADR-0100/ADR-0104 flag-gated arms (`memory/service.py`) were also checked: no hardcoded V1
strings — confirmed type-agnostic, `entity_types IN $entity_types`-style filters fed by whatever
the caller passes. They inherit correctness for free once (1) and (2) above are fixed. No code
change required there, consistent with the FRE-769 doc's classification ("type-agnostic
mechanism, value-dependent callers").

V2 10-type vocabulary (confirmed from the live extraction prompt,
`second_brain/entity_extraction.py:246`): `Person, Organization, Location, TechnicalArtifact,
KnowledgeArtifact, MethodOrConcept, DomainOrTopic, Phenomenon, QuantityMeasure, Event`.

## Steps

1. **`src/personal_agent/orchestrator/executor.py`** — `_ENTITY_TYPE_KEYWORDS`:
   - `"tool"/"tools"/"technology"` → `"TechnicalArtifact"`
   - `"topic"/"topics"` → `"DomainOrTopic"`
   - `"concept"/"concepts"` → `"MethodOrConcept"`
   - Leave `location*/place*/city*/country*` → `Location`, `person/people/someone` → `Person`,
     `organization/org/company/companies` → `Organization` unchanged (stable across V1→V2).
   - Add light keyword coverage for the new types per the ticket's suggestion (single best-fit
     word each, no ambiguity with existing keys): `"phenomenon"/"phenomena"` →
     `"Phenomenon"`; `"quantity"/"quantities"/"measurement"/"measurements"` →
     `"QuantityMeasure"`.
   - Fix the stale docstring example on `_extract_entity_type_hints`
     (`"What tools have I used" -> ["Technology"]` → `-> ["TechnicalArtifact"]`).

2. **`src/personal_agent/tools/memory_search.py`** — rewrite the `entity_types` parameter
   description to list the V2 10-type vocabulary instead of the V1 7-type one.

3. **`src/personal_agent/memory/dedup.py`** — fix the docstring example
   (`e.g., "Technology"` → `e.g., "TechnicalArtifact"`). No functional change.

4. **Tests:**
   - `tests/test_orchestrator/test_executor.py` — update
     `test_extract_entity_type_hints_technology` → asserts `["TechnicalArtifact"]`;
     `test_extract_entity_type_hints_topic` → asserts `["DomainOrTopic"]`;
     `test_extract_entity_type_hints_concept` → asserts `["MethodOrConcept"]`.
     Leave the location/person/organization tests as-is (already correct — proves stable types
     unchanged). Add two small tests for the new phenomenon/quantity keyword coverage.
   - `tests/test_tools/test_memory_search.py` — add a test asserting the `entity_types`
     parameter description contains the V2 vocabulary and does not contain the retired
     `"Technology"`/`"Topic"`/`"Concept"` strings (word-boundary check, since `"Technology"`
     is not a substring of any V2 name but `"Concept"` and `"Topic"` are — assert absence of the
     exact retired words, not mere substring, to avoid false positives against
     `MethodOrConcept`/`DomainOrTopic`).
   - `tests/personal_agent/memory/test_dedup.py` — add a test that
     `check_entity_duplicate`/`_find_similar_entities` correctly filters using a V2 type value
     (e.g. `"TechnicalArtifact"`), proving the dedup path is generic and V2-clean (AC #4).
   - Add one small guard test (new, colocated in `tests/test_orchestrator/test_executor.py` or a
     new `tests/test_taxonomy_guard.py`) that asserts `_ENTITY_TYPE_KEYWORDS.values()` contains
     none of the retired strings — the "provable by a grep or guard" AC #1, made durable as a
     test rather than a one-off grep.

## Acceptance criteria mapping (from the Linear ticket)

| AC | Proof |
|---|---|
| No live recall consumer references the retired type strings | New guard test on `_ENTITY_TYPE_KEYWORDS.values()` + `memory_search` schema-content test |
| Recall-intent hint extractor resolves tools→TechnicalArtifact, topics→DomainOrTopic, concepts→MethodOrConcept; stable types unchanged | Updated `test_executor.py` assertions |
| `search_memory` tool schema advertises exactly the V2 10-type vocabulary | New schema-content test |
| Dedup fuzzy/alias path scopes on V2 type values | New `test_dedup.py` test with a V2 type value |
| Unit tests, mypy, ruff all pass clean | `make test`, `make mypy`, `make ruff-check` |

## Out of scope (confirmed)

- Neo4j historical-node migration (FRE-772, in flight on build1 — different files, no conflict).
- `/memory/query` REST surface docs (lower priority per FRE-769, external-caller-dependent).
- `memory/models.py:35` stale comment (not a live consumer, not named in ticket scope).

## Test commands

```
make test-file FILE=tests/test_orchestrator/test_executor.py
make test-file FILE=tests/test_tools/test_memory_search.py
make test-file FILE=tests/personal_agent/memory/test_dedup.py
make mypy
make ruff-check
make ruff-format
```
