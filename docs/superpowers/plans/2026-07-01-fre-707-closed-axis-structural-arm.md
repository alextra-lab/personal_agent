# FRE-707 — Wire closed-axis predicates into recall (structural arm)

**Ticket:** FRE-707 (Approved → In Progress) · **Backing:** ADR-0104 (AC-4) · ADR-0103 §4 ·
design spec `docs/specs/MULTI_PATH_RETRIEVAL_DESIGN_SPEC.md` §2/§8
**Branch:** `fre-707-closed-axis-predicates` · **Codex plan-review:** done (sound with fixes; folded in below)

---

## Scope (what this ticket is, and is NOT)

**Is:** the **structural / closed-axis retrieval arm** — a new, self-contained retrieval function
that narrows *entity* candidacy by the closed axes **entity type**, **recency-as-predicate**, and
**relationship hops**, and returns a **ranked list of entities**. Per the design spec §2/§8 this arm
is **v2** (Dense+Lexical+Multi-query are v1, FRE-722/723/724) and ships **flag-dark**: feature-gated
OFF, integration-tested against the test substrate, **NOT wired into live recall or Stage-6** (the
`memory/fusion.py` combiner it will plug into does not exist yet — FRE-722, Needs-Approval). This
mirrors FRE-723's flag-dark posture exactly.

**Is NOT:** any change to the live recall paths (`query_memory`, `query_memory_broad`,
`suggest_relevant`), the Stage-6 seam (`request_gateway/context.py`), or RRF fusion. Those stay
byte-for-byte. Blast radius = one new method + one new pure helper + a shared parse helper + three
settings fields + tests.

## Acceptance criteria carried (ADR-0104 AC-4 — the definition of done)

1. **AC-4a** — the type-predicate arm is **feature-gated off** until the type axis is enforced.
2. **AC-4b** — once enabled, a type-scoped recall **does not silently drop** entities whose
   `entity_type` is `""` **or** `"Unknown"` (a fixture with an unenforced-type entity still returns it).
3. **AC-4c (extends ADR-0103 AC-3)** — **no** recall path applies a hard predicate on the **open
   axis** (topic, meaning, free-text `name`/`description`); the open axis stays semantic.

FRE-637 (the type-axis contract, PR #293) is **Done**, so AC-4a's precondition is met; we still ship
the arm gated OFF for rollout discipline (FRE-433 standard, spec §6).

---

## Design

### Item space & identity
The arm returns a **ranked `list[EntityNode]`** (best-first). Entity is the natural item of the
closed-axis structure (type is an entity property; hops traverse entity edges). AC-4b is an
entity-level assertion ("a fixture with an unenforced-type entity still returns it"), so an
entity-returning arm proves it directly. Ranked order is the arm's only contract here.

> **Integration dependency (noted, codex medium):** `EntityNode` carries `entity_id`/`name` but
> **not** the Neo4j `elementId` that the fusion dedup rule (spec §4) uses as the entity identity key.
> This is acceptable for AC-4 (entity-level, order-only), but **FRE-722 must adapt this arm's results
> to `elementId`-based dedup** (or this arm later exposes `elementId`). Called out in the master
> handoff so the integration seam owner (FRE-724) inherits it explicitly — not left implicit.

### Closed axes (each optional, driven by params)

- **Type** (`entity_type`) — the **safe** predicate. When the requested types are given AND the type
  sub-predicate is enabled, narrow to those types **but always keep unenforced-type rows**:
  `e.entity_type IN $types OR e.entity_type IS NULL OR e.entity_type = '' OR e.entity_type = 'Unknown'`.
  This is AC-4b: as the graph is re-extracted under FRE-637's contract the unenforced set shrinks and
  the predicate sharpens; until then no legacy row is silently lost. (The explicit `IS NULL` branch
  makes the disjunction true for null-typed rows — Cypher null semantics are handled.)
- **Recency-as-predicate** (`last_seen`) — a deterministic **time window**
  `toString(e.last_seen) >= $recency_cutoff`. Time is a genuinely *closed*, ordered axis, so a hard
  filter here is correct and does **not** violate AC-4c (that guards the *open* axis only).
  **`last_seen` is heterogeneous in the substrate** — an ISO string on the mention path
  (`service.py:654`) and a Neo4j `datetime()` temporal on the create/access path (`service.py:927`).
  A raw `>=` against a string is a type-comparison bug for temporal rows, so we normalise **both**
  sides to ISO strings with `toString(...)` and compare lexicographically at day granularity (the
  date prefix dominates; sub-second/offset differences are immaterial for a 30/90-day window). The
  same `toString(...)` is used in `ORDER BY` for the identical reason. **NULL `last_seen` rows are
  excluded by the recency window** (`NULL >= x` is null → filtered) — correct for a recency
  predicate, and documented; almost no entity has a null `last_seen` (it is set on every mention and
  every access).
- **Relationship hops** (graph traversal) — 1-hop **co-occurrence** neighbours of anchor entities
  over the bipartite `Turn`-`Entity` graph:
  `(a:Entity)<-[:DISCUSSES]-(t:Turn)-[:DISCUSSES]->(e:Entity)` where `a.name IN $anchors`. One hop is
  a real relationship hop; deeper traversal is a documented future extension (bounded-cost posture
  on the VPS). When anchors are absent, the arm is a plain `MATCH (e:Entity)` scan under the type +
  recency predicates.

### Visibility (FRE-229) — all three matched nodes scoped (codex high)
`_build_visibility_filter` is the single read chokepoint: **every** `:Turn`/`:Entity` match must
append its fragment. So the arm scopes **all** matched nodes:
- non-anchor path: `e` scoped.
- anchor path: `a` (anchor), `t` (intermediate Turn), **and** `e` all scoped — otherwise a visible
  `e` could be reached/ranked *through a private Turn* the user cannot see (an association leak).
`_build_visibility_filter` returns identical param keys (`vis_authenticated`, `vis_user_id`) for
every alias, so the three fragments merge into one param set without collision.

### Ranking
- Without anchors: `ORDER BY toString(e.last_seen) DESC, e.name` (recency as the structural signal).
- With anchors: `ORDER BY cooccur DESC, toString(e.last_seen) DESC, e.name` where `cooccur` = distinct
  shared-turn anchor count.

---

## Files & changes

### 1. `src/personal_agent/config/settings.py` (after `recall_candidate_cap`, ~L585)

```python
    # --- Structural / closed-axis retrieval arm (ADR-0104 AC-4 / FRE-707) ---
    structural_arm_enabled: bool = Field(
        default=False,
        description=(
            "ADR-0104 / FRE-707: master gate for the closed-axis structural recall "
            "arm (entity type + recency-as-predicate + relationship hops). Ships "
            "flag-dark: default off means the arm is never invoked and contributes "
            "no candidates. Enabled only once the multi-path fusion core (FRE-722/724) "
            "wires it in, under the FRE-433 flag->verified->rollout discipline."
        ),
    )
    structural_type_predicate_enabled: bool = Field(
        default=False,
        description=(
            "ADR-0104 AC-4 / ADR-0103 §4: gates the entity-type sub-predicate of the "
            "structural arm. Off until the type axis is closed by contract (FRE-637). "
            "When on, the type predicate is SAFE by construction — it narrows to the "
            "requested types but never drops rows whose entity_type is ''/'Unknown', "
            "so an unenforced-type entity is never silently lost."
        ),
    )
    structural_arm_top_k: int = Field(
        default=50,
        ge=1,
        le=500,
        description=(
            "FRE-707 / design spec §3.3: per-arm retrieval depth for the structural "
            "arm's ranked list, matching the multi-path arm depth default (50). "
            "Config-driven per ADR-0031."
        ),
    )
```

### 2. `src/personal_agent/memory/service.py`

**2a. Pure query builder** (module-level, near `_build_visibility_filter`, ~L128). Unit-testable
without a DB — the AC-4b safe-type-predicate and AC-4c open-axis-absence are proven here:

```python
def _build_structural_arm_query(
    *,
    entity_types: Sequence[str] | None,
    type_predicate_enabled: bool,
    recency_days: int | None,
    anchor_names: Sequence[str] | None,
    top_k: int,
    vis_fragment_e: str,
    vis_fragment_t: str,
    vis_fragment_a: str,
) -> tuple[str, dict[str, Any]]:
    """Build the closed-axis structural arm's Cypher and params (ADR-0104 AC-4).

    Pure function (no substrate) so the safe type predicate and the open-axis
    exclusion are unit-testable. Composes three optional closed-axis predicates —
    entity type (safe), recency-as-predicate, relationship hops — over entities.
    Never filters on the open axis (name/description): AC-4c.

    Args:
        entity_types: Requested entity types for the type predicate, or None.
        type_predicate_enabled: Whether the type sub-predicate is active (AC-4a).
        recency_days: Recency window in days for last_seen, or None for no window.
        anchor_names: Entity names to seed 1-hop co-occurrence traversal, or None.
        top_k: Max entities to return.
        vis_fragment_e: Visibility WHERE fragment for the entity alias ``e`` (FRE-229).
        vis_fragment_t: Visibility fragment for the intermediate Turn alias ``t``.
        vis_fragment_a: Visibility fragment for the anchor entity alias ``a``.

    Returns:
        Tuple of (cypher, params). Params never include a name/description filter.
    """
    params: dict[str, Any] = {"top_k": top_k}
    e_where: list[str] = [vis_fragment_e]

    if type_predicate_enabled and entity_types:
        # SAFE type predicate (AC-4b): narrow to requested types but keep
        # unenforced-type rows so none is silently dropped until FRE-637's
        # contract has back-filled the graph.
        e_where.append(
            "(e.entity_type IN $entity_types "
            "OR e.entity_type IS NULL "
            "OR e.entity_type = '' "
            "OR e.entity_type = 'Unknown')"
        )
        params["entity_types"] = list(entity_types)

    if recency_days is not None:
        # last_seen is heterogeneous (ISO string OR Neo4j datetime); normalise
        # both sides with toString for a valid lexicographic day-granular compare.
        params["recency_cutoff"] = (
            datetime.now(timezone.utc) - timedelta(days=recency_days)
        ).isoformat()
        e_where.append("toString(e.last_seen) >= $recency_cutoff")

    e_where_clause = " AND ".join(e_where)

    if anchor_names:
        params["anchor_names"] = list(anchor_names)
        # Scope a, t AND e (FRE-229): never surface an entity reached through a
        # Turn or anchor the caller cannot see.
        cypher = f"""
        MATCH (a:Entity)<-[:DISCUSSES]-(t:Turn)-[:DISCUSSES]->(e:Entity)
        WHERE a.name IN $anchor_names AND e.name <> a.name
          AND {vis_fragment_a} AND {vis_fragment_t} AND {e_where_clause}
        WITH e, count(DISTINCT a) AS cooccur
        RETURN e AS e
        ORDER BY cooccur DESC, toString(e.last_seen) DESC, e.name
        LIMIT $top_k
        """
    else:
        cypher = f"""
        MATCH (e:Entity)
        WHERE {e_where_clause}
        RETURN e AS e
        ORDER BY toString(e.last_seen) DESC, e.name
        LIMIT $top_k
        """
    return cypher, params
```

**2b. Async arm method** on `MemoryService` (near `query_memory_broad`, ~L2326). Gated by
`structural_arm_enabled` (AC-4a / flag-dark):

```python
async def structural_recall_arm(
    self,
    *,
    entity_types: Sequence[str] | None = None,
    recency_days: int | None = None,
    anchor_names: Sequence[str] | None = None,
    limit: int | None = None,
    access_context: AccessContext = AccessContext.SEARCH,
    trace_id: str | None = None,
    session_id: str | None = None,
    user_id: UUID | None = None,
    authenticated: bool = False,
) -> list[EntityNode]:
    """Closed-axis structural recall arm (ADR-0104 AC-4 / FRE-707).

    Returns a ranked list of entities narrowed by the closed axes — entity type
    (safe predicate, gated by structural_type_predicate_enabled), recency, and
    1-hop relationship co-occurrence. Feature-gated OFF (structural_arm_enabled);
    flag-dark until the multi-path fusion core (FRE-722/724) wires it in. Never
    filters on the open axis (name/description) — that stays semantic (AC-4c).

    Args:
        entity_types: Closed-axis type filter; applied only when the type
            sub-predicate is enabled. Unenforced-type rows are never dropped.
        recency_days: Recency window for last_seen; None = no window.
        anchor_names: Seeds for 1-hop co-occurrence traversal; None = plain scan.
        limit: Max entities; defaults to structural_arm_top_k.
        access_context: Typed access context (ADR-0042).
        trace_id: Request trace id for event correlation.
        session_id: Session id for event correlation.
        user_id: Authenticated user UUID for visibility scoping (FRE-229).
        authenticated: Whether the request carries a verified identity (FRE-229).

    Returns:
        Ranked list of EntityNode (best-first). Empty when the arm is gated off,
        the service is disconnected, or nothing matches.
    """
    current_settings = get_settings()
    if not current_settings.structural_arm_enabled:
        return []
    if not self.connected or not self.driver:
        return []

    top_k = limit if limit is not None else current_settings.structural_arm_top_k
    vis_e, vis_params = _build_visibility_filter("e", user_id, authenticated)
    vis_t, _ = _build_visibility_filter("t", user_id, authenticated)
    vis_a, _ = _build_visibility_filter("a", user_id, authenticated)
    cypher, params = _build_structural_arm_query(
        entity_types=entity_types,
        type_predicate_enabled=current_settings.structural_type_predicate_enabled,
        recency_days=recency_days,
        anchor_names=anchor_names,
        top_k=top_k,
        vis_fragment_e=vis_e,
        vis_fragment_t=vis_t,
        vis_fragment_a=vis_a,
    )
    params.update(vis_params)

    try:
        async with self.driver.session() as db_session:
            result = await db_session.run(cypher, parameters=params)
            records = await result.data()
    except Exception as e:
        log.error(
            "structural_recall_arm_failed",
            error=str(e),
            trace_id=trace_id,
            session_id=session_id,
        )
        return []

    entities = [self._entity_node_from_record(r["e"]) for r in records]
    log.info(
        "structural_recall_arm_completed",
        arm="structural",
        entity_count=len(entities),
        type_predicate_enabled=current_settings.structural_type_predicate_enabled,
        has_recency=recency_days is not None,
        has_anchors=bool(anchor_names),
        trace_id=trace_id,
        session_id=session_id,
    )
    return entities
```

**2c. Extract `_entity_node_from_record`** — the EntityNode-from-node parsing at L2980-3019 is
lifted into a small private helper so the existing broad path and the new arm share it (the existing
site calls the helper — no behaviour change; verified by the existing broad-path tests still passing).

### 3. Tests

**Unit** `tests/test_memory/test_structural_arm_query.py` (pure, no Neo4j):
- `test_type_predicate_keeps_unenforced_rows` — `type_predicate_enabled=True`, `entity_types=["Person"]`:
  built Cypher contains the `IS NULL` / `= ''` / `= 'Unknown'` escape hatch (AC-4b).
- `test_type_predicate_absent_when_disabled` — `type_predicate_enabled=False`: no `entity_type`
  clause and `entity_types` not in params (AC-4a).
- `test_no_open_axis_predicate` — for every param combination, the built Cypher contains **no**
  `e.name IN` / `e.name =` / `e.description` / `CONTAINS` filter and params carry no name/description
  key (AC-4c). (`e.name <> a.name` in the anchor path is a self-dedup guard, not an open-axis filter;
  the test asserts absence of the *filter* forms above, which excludes `<>`.)
- `test_recency_predicate_uses_tostring` — `recency_days=30` adds `toString(e.last_seen) >= $recency_cutoff`.
- `test_anchor_traversal_scopes_turn_and_anchor` — `anchor_names` switches to the co-occurrence MATCH
  and the built Cypher contains visibility fragments for **`t` and `a`** as well as `e` (FRE-229).

**Integration** `tests/test_memory/test_structural_arm.py` (test substrate :7688, skips if absent):
- `test_arm_gated_off_returns_empty` — `structural_arm_enabled=False` → `[]` even with seeded data
  (AC-4a / flag-dark).
- `test_type_scoped_recall_keeps_unenforced_entities` — seed a `Person`, an entity with
  `entity_type=""`, **and** one with `entity_type="Unknown"`; enable both flags; call with
  `entity_types=["Person"]` → **all three** returned; both unenforced-type entities present (AC-4b,
  covering `""` **and** `"Unknown"` per codex).
- `test_open_axis_not_filtered` — seed two same-type, same-recency entities with different names; arm
  returns both irrespective of any query text (AC-4c behavioural).
- `test_recency_window_filters` — an entity outside the window is excluded (closed-axis recency).
- `test_traversal_excludes_private_turn` — anchor co-occurs with entity E **only** through a
  `visibility='private:<other-user>'` Turn; an unauthenticated / different-user call does **not**
  surface E (FRE-229 association-leak guard, codex high).

Settings are overridden per-test via monkeypatch on `get_settings()` (existing
`test_relevance_bounded_recall.py` pattern).

---

## Step sequence (TDD)

1. Add the three settings fields → `make ruff-check` clean.
2. Write unit tests (5) → run, confirm they **fail** (helper absent).
3. Implement `_build_structural_arm_query` → unit tests pass.
4. Write integration tests (5) → confirm fail (arm absent).
5. Extract `_entity_node_from_record`; implement `structural_recall_arm` → integration tests pass
   against `make test-infra-up` (Neo4j :7688).
6. `make test` (module then full) · `make mypy` · `make ruff-check` + `make ruff-format` ·
   `pre-commit run --all-files`.
7. Rebase on `origin/main`, PR, master handoff comment.

## Verification → acceptance mapping
| AC | Proof |
|----|-------|
| AC-4a (gated off until enforced) | unit `test_type_predicate_absent_when_disabled` + integration `test_arm_gated_off_returns_empty` |
| AC-4b (no silent drop of `""`/`"Unknown"`) | unit `test_type_predicate_keeps_unenforced_rows` + integration `test_type_scoped_recall_keeps_unenforced_entities` |
| AC-4c (open axis stays semantic) | unit `test_no_open_axis_predicate` + integration `test_open_axis_not_filtered` |
| FRE-229 (no association leak via private Turn) | unit `test_anchor_traversal_scopes_turn_and_anchor` + integration `test_traversal_excludes_private_turn` |

## Out of scope / follow-ups
- Wiring the arm into `memory/fusion.py` + Stage-6, **and adapting arm results to `elementId` dedup
  identity (spec §4)** — **FRE-724** (seam owner; blocked on FRE-722/723/706). Flagged in the master
  handoff so the seam owner inherits the identity gap explicitly.
- Multi-hop (>1) traversal depth — documented future extension; no lived failure-mode pressure (spec §2).
