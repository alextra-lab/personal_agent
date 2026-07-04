# FRE-769 — Downstream-Impact Check: Does Recall or the Pedagogy Layer Key on Entity Type?

**Date:** 2026-07-04
**Author posture:** read-only codebase audit (no production change). This is ADR-0109 Implementation
Notes step 1 — the do-first gate for the entire V2 taxonomy migration chain (FRE-769→770→771→772→773).
**Backing:** [ADR-0109 Entity & Relationship Taxonomy Redesign](../architecture_decisions/ADR-0109-entity-taxonomy-redesign.md), AC-3.

---

## Question

ADR-0109 proposes coarsening the 7-type inherited entity taxonomy (`Person, Organization, Location,
Technology, Concept, Event, Topic`) to an 8-type V2 (`Person, Organization, Location,
TechnicalArtifact, MethodOrConcept, DomainOrTopic, Phenomenon, Event`). Alternative #4 in the ADR
("do nothing") is only viable if entity-type accuracy doesn't matter downstream. This check asks: does
anything downstream of extraction/storage actually **read, filter, rank, or branch on the entity type
*value*** — or does everything downstream operate on the entity node itself, making the type value
effectively decorative?

**Verdict: the premise does not hold.** There are real, live, type-string-keyed dependencies. Coarsening
is not "near-free" as stated in the ADR's Consequences section — it requires deliberate remapping in at
least three places, or it will silently regress production behavior. This does **not** block V2 (the ADR's
own Implementation Notes step 5 already plans a KG migration pass); it changes the scope of step 5 from
"nice to keep clean" to "load-bearing, must ship in lockstep with the extraction-prompt rewrite."

---

## Findings

### Real dependencies — keyed on the type *value*, will break silently if types are renamed without remapping

| # | Consumer | Location | Status | Risk if types renamed without remapping |
|---|---|---|---|---|
| 1 | **Dedup vector search (fuzzy/alias matching)** | `src/personal_agent/memory/dedup.py:153` (`check_entity_duplicate`), called unconditionally from `create_entity` at `src/personal_agent/memory/service.py:1309` | **Live, unconditional, runs on every entity write today** | `WHERE node.entity_type = $entity_type` scopes the fuzzy/embedding-similarity dedup search to nodes of the *exact same type string*. Note: exact same-*name* entities are unaffected — final storage is `MERGE (e:Entity {name: $name})` (`memory/service.py:1418`), which merges on name alone, not type. The real risk is narrower but still live: an entity re-extracted under a *different name or phrasing* whose type changes across the taxonomy boundary (e.g. `Concept` → `MethodOrConcept`) can never fuzzy-match its own prior history at any similarity score → duplicate-node risk for aliased/renamed-surface-form entities of a renamed type. **Highest-risk finding**, scope corrected from an earlier draft that overstated it as affecting same-name entities too. |
| 2 | **Orchestrator recall-intent keyword map** | `src/personal_agent/orchestrator/executor.py:697-720` (`_ENTITY_TYPE_KEYWORDS`), consumed by `_extract_entity_type_hints` (:723-736), fed unconditionally into `query_memory_broad(entity_types=...)` at :2531-2543 | **Live, unconditional** — runs whenever `is_memory_recall_query()` matches | Hardcodes the **old** taxonomy strings (`"tool"→"Technology"`, `"topic"→"Topic"`, `"concept"→"Concept"`, etc.) as literal values passed into the live Cypher filter below. If the extraction prompt starts emitting V2 strings while this map still emits V1 strings, natural-language recall queries like "what tools have I used" silently return **zero** results (`entity_types` filters to a value nothing in the graph has anymore) — a silent-empty failure, not an error. |
| 3 | **`query_memory` / `query_memory_broad` Cypher filters** | `src/personal_agent/memory/service.py:2615`, `:3972` | **Live, default recall path today** (both ADR-0100 relevance-bounded recall and ADR-0104 multi-path recall default `False` in `config/settings.py:553,639`, so these legacy queries are what production actually serves) | `e.entity_type IN $entity_types` — a hard WHERE predicate. This is the mechanism consumers #1(caller path)/#2/#4 write into; not a separate risk, but confirms the filter is real and live, not vestigial. |
| 4 | **`search_memory` tool schema (LLM-facing)** | `src/personal_agent/tools/memory_search.py:39-48` (`entity_types` parameter description enumerates `Location, Person, Organization, Technology, Topic, Concept, Event`), flows unchanged into `query_memory` (:155-166) and `query_memory_broad` (:193-204) → the same live filters as #3 | **Live, internal — the model calls this tool directly** | This is not merely an external-caller edge case: the tool's own parameter description hardcodes the V1 strings as the documented valid values for the LLM. Must be rewritten to the V2 8-type list in the same change as the extraction-prompt swap, or the model will keep requesting old-taxonomy filters that match nothing. |
| 5 | **`/memory/query` REST surface** | caller-supplied `entity_types` parameter flows into the same filters as #3 | Live, external-caller-controlled | Any external REST client that names an old-taxonomy string will also silently zero out once the graph re-types, until API docs are updated. Lower priority than #1/#2/#4 since it requires an external caller to be using the old strings, not an internal code path. |

### Flag-gated dependencies — not live today, but designed to be type-aware; will inherit the same risk when enabled

| # | Consumer | Location | Flag (default `False`) |
|---|---|---|---|
| 6 | ADR-0100 relevance-bounded recall arm — type match scored as `escore = 1.0` inside a `CASE WHEN` (not just a filter) | `memory/service.py:2583-2593`, `:3944-3956` | `relevance_bounded_recall_enabled` (`config/settings.py:553`) |
| 7 | ADR-0104 multi-path broad-entity arm — Python-side `if ent.get("type") not in entity_types: continue` | `memory/service.py:3517` (`_multipath_broad_entities`) | `multipath_recall_enabled` (`config/settings.py:639`) |
| 8 | ADR-0104 structural/closed-axis arm — `(e.entity_type IN $entity_types OR e.entity_type IS NULL OR e.entity_type = '' OR e.entity_type = 'Unknown')` | `memory/service.py:230-330` (`_build_structural_arm_query`), `:2897-2981` | `structural_arm_enabled`, `structural_type_predicate_enabled` (`config/settings.py:607,617`) |

These three arms are dormant today, so they carry no *current* production risk, but they are exactly the
"closed axis" recall paths ADR-0104 designed around an entity-type contract — they need the same
remap/backfill before their flags are ever flipped on, not a separate future audit.

### Confirmed coarsen-safe — display, logging, telemetry, or grouping only

- `memory/proactive.py:120,142` — `entity_type` carried into a suggestion payload dict; none of the scoring functions (`_overlap_subscore`, `_topic_subscore`, `_combine_scores`) read it.
- `memory/service.py:4130-4135` (`_classify_query_type`) — telemetry label keyed on *whether* `entity_types` is set, not on which string.
- `memory/service.py:4587-4671` (`promote_entity`, `get_promotion_candidates`) — logged/returned; ordering is `ORDER BY mention_count DESC` only.
- `memory/protocol_adapter.py` (`recall_broad`) — `entities_by_type.setdefault(entity_type, [])` is a display-grouping dict, not a filter.
- `orchestrator/executor.py:1733` (`_render_memory_section`) — interpolates `entity_type` only as a display bracket (`- [Technology] Neo4j: ...`) in LLM-facing prompt text; confirmed by `tests/personal_agent/orchestrator/test_memory_render_filter.py`, which never varies behavior by type.
- `request_gateway/context.py:101,119,228` — grouping key / display field only; ranking is driven by `freshness_modifier` from `last_accessed_at`.
- `second_brain/consolidator.py:686` — passes `entity_type` straight through into `create_entity()`; no branching in the consolidator itself. The risk is entirely inherited one hop downstream (finding #1).
- `second_brain/quality_monitor.py` — the only "type" hits are an unrelated `anomaly_type` field (six quality-monitor categories); no entity-type-keyed logic.
- `insights/` (`engine.py`, `fingerprints.py`, `skill_routing_threshold_monitor.py`) — **zero references** to entity type. Not a factor for this migration.
- `gateway/knowledge_api.py`, `gateway/client.py` — `entity_type` appears only in write-path request/response models and a `log.debug` call; the `/knowledge/search` and `/knowledge/entities/{id}` GET endpoints have no type filter.

### Pedagogy layer

**No live code exists.** Grep across `src/` for "pedagog", "tutor", "spaced_repetition", "socratic" found:
- `second_brain/entity_extraction.py:94,482-483` — the extraction prompt describes gating what's
  "visible to the tutor" by **knowledge class** (World/Personal/System, ADR-0097/0098/0106), which is
  explicitly orthogonal to entity type — not a type dependency.
- `observability/route_trace/types.py:79,135` — a `pedagogical_outcomes: Sequence[str] | None = None`
  field, always populated as `None` (`assembler.py:323`) — reserved, unpopulated, not live.
- Real content is design-doc only: `ADR-0084-pedagogical-architecture-socratic-tutor-layer.md`,
  `docs/specs/PEDAGOGICAL_NORTH_STAR.md`, `docs/research/2026-06-03-pedagogical-architecture-origins.md`.

**Verdict: zero risk to this migration.** The pedagogy layer is a North-Star concept with no built
consumer to protect.

---

## Verdict summary (per ADR-0109 AC-3)

| Consumer | Verdict |
|---|---|
| Dedup fuzzy/alias vector search (`memory/dedup.py`) | **Keep grain — must remap.** Highest-risk item; needs an explicit old→new type map (or a backfill pass) applied atomically with the extraction-prompt swap (ADR-0109 step 3), not deferred to the later KG migration (step 5). Scope: affects aliased/renamed-surface-form re-extractions, not exact-name entities (those merge on name alone). |
| Orchestrator recall-intent keywords (`_ENTITY_TYPE_KEYWORDS`) | **Keep grain — must remap.** Update the literal value strings in lockstep with the prompt rewrite, and update `tests/test_orchestrator/test_executor.py:36-65` (currently asserts old-taxonomy output) in the same change. |
| `search_memory` tool schema | **Keep grain — must remap.** The tool's `entity_types` parameter description hardcodes the V1 strings as the LLM's documented valid values (`tools/memory_search.py:39-48`); rewrite to the V2 list in the same change as the prompt swap or the model keeps requesting filters that match nothing. |
| Legacy `query_memory` / `query_memory_broad` Cypher filters | **Type-agnostic mechanism, value-dependent callers.** The Cypher itself needs no change; it only breaks if a caller (the two items above, or an external client) still supplies old-taxonomy strings after the graph re-types. |
| `/memory/query` REST surface | **Type-agnostic mechanism, external-caller-dependent.** Lower priority — requires an external client using old strings; update API docs alongside the rollout. |
| ADR-0100/0104 flag-gated arms | **Coarsen when enabled, not before.** No current risk (flags off); remap before each flag flips on, sequenced with the KG migration (step 5). |
| Recall ranking/scoring, promotion ordering, proactive suggestions, telemetry classification, prompt display formatting, `insights/` | **Coarsen for free.** No production behavior depends on the specific type string. |
| Pedagogy/tutor layer | **N/A — no live consumer exists.** |

**Net conclusion:** ADR-0109's own Implementation Notes step 5 (KG migration, idempotent re-classification
of existing nodes) already anticipates this. This check sharpens step 5's scope: the migration is not
"clean up decorative labels," it is "atomically replace three live functional dependencies" (dedup grain,
recall-intent keyword map, `search_memory` tool schema) at the same time the extraction prompt changes,
plus a remap for the flag-gated arms before they're ever enabled. Recommend the Step 3 (prompt update) PR
also update `_ENTITY_TYPE_KEYWORDS` and the `search_memory` tool schema, and add an explicit dedup-grain
remap/backfill note to Step 5's plan — do not let any of the three drift to "later."

---

## Method

Read-only grep + `Read` verification across `src/personal_agent/{memory,second_brain,orchestrator,
insights,request_gateway,gateway,tools,observability}/` and `tests/` for `entity_type`, `node_type`,
`EntityType`, the V1 type-name literals, and the Cypher `entity_type` property (entities are labeled
uniformly `:Entity` with an `entity_type` property — no per-type Neo4j labels exist, so no
`MATCH (e:Concept)`-style label matching was found or possible). Every file:line cited above was read
directly; none are inferred from the grep hit alone.

An independent adversarial review (Codex) of this finding surfaced one missed consumer (the
`search_memory` tool schema, now folded in above) and one overclaim (dedup risk narrowed from "all
same-type entities" to "fuzzy/alias-matched entities specifically" — exact-name entities merge on name
alone via `MERGE (e:Entity {name: $name})`, `memory/service.py:1418`). Both corrections are incorporated.
