# FRE-1015 — ADR-0126 T1: Topic-scoped stance enrichment (push on entity selection)

**Ticket:** FRE-1015 · **ADR:** ADR-0126 (D1, D2 topic-scoped half, D5 push half, D6) · **Tier:** Tier-2:Sonnet
**Blocks:** FRE-1017 (T2, always-present behavioural layer) · FRE-1019 (T5 seam)
**Blocked by:** FRE-1021 (satisfied — PR #734 merged, `Awaiting Deploy`)

**Revision 2** — after a codex plan-review pass (`task-ms56w8jl-ogl2ha`) found 2 BLOCKER + 8
MAJOR/MINOR issues in revision 1. Each finding is addressed below with a citation to where it's
fixed; two findings' proposed fixes were adjusted after further verification (see §Design decision
5 and §Precondition semantics) because the literal suggested fix would have broken more than it
solved, or didn't survive a second read of the actual admission mechanics.

## Scope (from the ticket)

Build a read path: for each entity the existing recall path has already selected, retrieve the
owner's **current** `HAS_STANCE` edge toward that entity and push its `affect` into assembled
context. Enrichment only — no new relevance decision, no claim classification.

Three binding constraints (ADR D1/D5/D6):
1. Current-only: `valid_to IS NULL AND invalid_at IS NULL`.
2. Empty/whitespace-only `affect` filtered **before render**, never rendered blank.
3. `affect` alone is sufficient; `mastery` is not a prerequisite (correctly null on all live rows).

Entity-selection precondition (binding on AC-1 positive half, AC-5 push half, AC-6 populated
control): each of those criteria must first assert the target entity is in the turn's recall set,
read from the turn-evidence admission record. Precondition failure → INCONCLUSIVE (`pytest.skip`
with a clear message), never a silent pass or a false stance-defect.

Observation point (fixed by the ADR for the whole suite): every "reaches the model" assertion is
against the **serialized provider request** (the `build_wire_messages` output), never against
`prompt_manifest` (a disjoint component — `captains_log/prompt_manifest.py`, DSPy-reflection input
only, zero code path overlap with the executor's render/inline/dispatch chain).

## Codebase map (verified by direct read)

- **`request_gateway/context.py`** — `assemble_context()` (L316-420) calls
  `_query_memory_for_intent()` (L178-313) to populate `memory_context`. All three producer branches
  emit `{"type": "entity", "name": ..., ...}` dicts into the same list shape.
- **`orchestrator/executor.py`** — **a second, independent producer exists outside the gateway
  path.** `step_init` (L3150+) branches on `ctx.gateway_output is not None` (L3330). When the
  gateway pipeline throws — wrapped in `try/except Exception` in `service/app.py:348-374`,
  `gateway_output` stays `None` on any failure — the function falls through past L3564 to a
  pre-gateway fallback (L3614+) that calls `MemoryService` directly, bypassing `context.py`
  entirely. Its broad-recall sub-branch (`is_memory_recall_query`, L3637-3675) calls
  `_format_broad_recall()` (L925-950, a *different* function from `context.py`'s
  `_format_broad_recall_context`) which **also** emits `{"type": "entity", ...}` items
  (L931-940) into `ctx.memory_context` directly — a second entity producer a single hook in
  `context.py` cannot reach. The entity-name-match sub-branch (L3676-3719) builds only
  conversation/episode dicts (`conversation_id`/`user_message`/`summary` keys, no `type` field at
  all) — it never produces entity items, so it needs no stance hook.
  `_render_memory_section_with_ids()` (L2285-2360) is the actual renderer either way — `context.py`
  never renders/serializes anything, so both producers converge on the same render/inline/wire
  pipeline downstream.
- **`memory/protocol.py`** — `MemoryProtocol` (L151-270): `recall`, `recall_broad`, `store_episode`,
  `promote`, `is_connected`, `suggest_relevant`. No stance method exists — add one. **Also:**
  `tests/personal_agent/memory/test_protocol.py` defines a runtime-checkable "complete fake"
  (`FakeMemory`, L207) used to assert `MemoryServiceAdapter`/fakes satisfy the protocol (L254) — a
  new required protocol method must be added there too or that conformance test breaks.
- **`memory/service.py`** — `assert_stance()` (L2339, write side, bitemporal supersession — sets
  `valid_to` and `invalid_at` **together** on supersession, L2379, never independently).
  `query_stance_history()` (L2740-2811, pull-only full chain, no `user_id`) and `query_claims()`
  (L2641-2738, current-only filter pattern) are the two patterns to combine, batched via `UNWIND`.
  **`query_memory()`'s default/legacy branch never populates `MemoryQueryResult.entities` at all**
  (constructed at L4066-4068 with `conversations`/`relevance_scores` only) — only the multipath
  fused-recall arm (`_multipath_query_memory`, L4927-5050) resolves fused `"entity"` items into
  `EntityNode`s (L5046-5050). Confirmed by direct read, not inferred from the FRE-1021 commit
  message.
- **`captains_log/turn_evidence.py`** — `MemoryItemKind` (L73-80: no STANCE). `memory_item_identity()`
  (L160-204) dispatches on `item["type"]`. `build_recall_candidates()` (L374-404) calls
  `memory_item_identity` on every item. **`_resolve_admission()` (L442-492) keys its
  `rendered_budget: Counter[str]` (built at L529, consumed at L477-484) by bare identity string
  alone — kind is not part of the key.** This is the real mechanism behind the admission-safety
  concern (see Design decision 5 below): it is not that an unhandled `"stance"` type corrupts
  anything today (unrecognised items resolve to `(UNKNOWN, "")`, contribute no line, and never enter
  `rendered_budget`, so there's no existing corruption) — it's that *after* adding STANCE with the
  target entity's own name as its identity, an entity candidate and its same-named stance candidate
  would share one Counter slot, letting one item's render satisfy the other's admission check.

## Design decisions this plan makes (numbered; findings from the codex review are cross-referenced)

1. **Two hook points, not one** — `request_gateway/context.py`'s `assemble_context()` (the primary,
   gateway-driven path) **and** `orchestrator/executor.py`'s pre-gateway fallback broad-recall
   branch (L3641-3662, only reachable when the gateway pipeline itself throws). *(Codex finding 3,
   MAJOR.)* Both call a **shared, pure, import-cycle-free pair of helper functions** defined once in
   `request_gateway/context.py` and imported by `executor.py`:
   - `_entity_names_from_memory_context(memory_context) -> list[str]` — order-preserving dedup
     (`dict.fromkeys`), not `sorted(set(...))`. *(Codex finding 5, MAJOR — see decision 6.)*
   - `_stance_context_items(stances) -> list[dict[str, Any]]` — pure dict-shape conversion.
   The legacy entity-match sub-branch (L3676-3719) is unchanged: it never emits entity items, so
   there is nothing for the hook to enrich there.

2. **No behavioural/topic-scoped split in T1 — kept as a deliberate scope call, not silently
   absorbed.** *(Codex finding 6, MAJOR — disagreed with after re-reading the ADR's own seam
   table.)* ADR-0126's AC-8 SEAM table (lines 630-636) assigns **disjoint** criteria per consumer:
   mutation 1 ("Topic-scoped stance enrichment") owns AC-1/AC-5/AC-6 — exactly this ticket's carried
   criteria — while mutation 2 ("Behavioural-profile injection") owns AC-2/AC-7, assigned to T2
   (FRE-1017) by the ticket text itself ("Implements ADR-0126 D1, D2 (**topic-scoped half**), D5
   (push half) and D6"). If T1 were required to exclude curated-behavioural targets, AC-3 ("a
   topic-scoped stance does *not* become always-present") would need to be checkable against T1
   alone — it isn't; AC-3 is checked on the entity-free probe turn that only makes sense once T2's
   always-present layer exists. T1 fetching a stance for *any* recalled entity — including one that
   later joins T2's curated set — is D2's actual entity-gated mechanism, applied uniformly; D3's
   curation is a **read-time facet T2 adds on top**, not a filter T1 must pre-apply. Recorded here
   explicitly so master's review sees the call was made, not missed.
3. **`get_current_stances` takes no `user_id`, gated on `authenticated` only** — mirrors
   `query_stance_history`'s existing precedent (Stance is a single `is_owner` sentinel edge, not
   per-user like Claim).
4. **No empty-affect filtering in the query layer** — filtering happens only in
   `_render_memory_section_with_ids` (D6: "filtering happens before render"; mirrors the FRE-1010
   precedent the ADR names). Keeps the empty-affect case observable end-to-end for the AC-6 test.
5. **Stance identity is namespaced (`f"stance:{target}"`), not the bare target name.** *(Codex
   finding 2, BLOCKER — fixed differently than codex's suggested remedy.)* Codex's proposed fix was
   to make `rendered_budget` (and the whole admission pipeline) key on `(MemoryItemKind, identity)`
   tuples instead of bare strings. Verified that would require changing
   `_render_memory_section_with_ids`'s return type (`tuple[str, ...]` → `tuple[tuple[MemoryItemKind,
   str], ...]`), `build_turn_evidence`'s `rendered_identities` param type, and `Counter` construction
   in `_resolve_admission` — and breaks 3 existing test files that assert on the returned tuple as
   bare strings (`tests/personal_agent/orchestrator/test_memory_section_render.py:265,271,283,286`,
   `tests/personal_agent/orchestrator/test_memory_render_filter.py`,
   `tests/personal_agent/captains_log/test_turn_evidence.py:58`). A namespaced identity string
   achieves the identical collision-safety property (no shared Counter key between an entity and its
   same-named stance) with **zero** changes to the shared evidence-contract type signatures other
   consumers (episode, entity, session_fact) depend on. `memory_item_identity()`'s stance branch
   returns `(MemoryItemKind.STANCE, f"stance:{target}")`; `_stance_line` still renders the bare
   `target` text (the namespacing is an internal bookkeeping key, never user-visible).
6. **Order-preserving stance placement, not `sorted(set(...))`.** *(Codex finding 5, MAJOR.)*
   `_entity_names_from_memory_context` preserves `memory_context` insertion order. The batch stance
   query's return order is not guaranteed by Neo4j (`UNWIND` fans out, rows return in undefined
   order), so before appending, the hook re-orders the *returned* stances to match
   `entity_names`'s order (`stance_by_target = {s["target"]: s for s in stances}`, then iterate
   `entity_names` in order). `_MAX_RENDERED_STANCES` at render time then takes an order-preserving
   prefix of that *same* order recall already established — not an independent re-selection, which
   is what would have made T1 a second, unstated relevance decision (the one thing the ADR forbids).
7. **The enrichment hook is wrapped in `try/except Exception`, log-and-continue.** *(Codex finding
   7, MAJOR.)* `query_current_stances` fails closed internally, but `MemoryProtocol` is an
   interface — a different implementation is not guaranteed to. `_query_memory_for_intent`'s own
   try/except does not cover code that runs after it returns, so the hook needs its own guard;
   without it, a stance-layer fault would fail the entire turn rather than just omit stance
   enrichment.
8. **Precondition semantics: recall *candidacy*, not render *admission*.** *(Resolves codex findings
   1 and 8, both BLOCKER/MAJOR, together — see the dedicated section below; this is not a minor
   wording change, it changes what the AC-6 fixtures need to look like.)*
9. **New render section, cap constant `_MAX_RENDERED_STANCES = 15`** (same value as
   `_MAX_RENDERED_ENTITIES`, for the reason in decision 6 — the stance prefix must never exceed what
   the entity prefix already bounds).
10. **Protocol conformance and adapter-delegation coverage added to the test list** (`FakeMemory` in
    `test_protocol.py`; an adapter test asserting `targets`/`authenticated`/`trace_id` forwarded
    exactly). *(Codex finding 10, MINOR.)*

## Precondition semantics — recall candidacy vs. render admission

The ticket text says the precondition must be "read from the admitted-recall record the evidence
contract writes," naming `RecallAdmissionRecord` specifically. Codex's findings 1 and 8 (AC-6
"impossible as written," AC-1 negative half "doesn't prove absence") both trace back to one
question this plan initially answered wrong: **does the precondition require
`RecalledMemoryRecord.admitted == True` (the item survived render/cap/inline all the way to the
wire), or merely that recall *selected* it as a candidate (present in
`RecallAdmissionRecord.items`, admitted or not)?**

Revision 1 used `admitted`. That is wrong for what this mechanism actually gates: the stance hook
fires off `memory_context` membership (`type == "entity"`), which happens **before** any
render-time filtering — an entity with an empty description still triggers a stance fetch (its
description-emptiness is irrelevant to whether recall "selected" it), but it will never be
`admitted=True` in the entity-kind sense, because FRE-1010's existing filter
(`described = [m for m in entities if (m.get("description") or "").strip()]`,
`executor.py:2329-2331`) drops it from the entity section before it ever reaches
`rendered_budget`. Requiring `admitted=True` as the precondition therefore forces a contradiction
exactly where AC-6 needs an empty/near-empty fixture: to make the entity-kind precondition hold,
the entity would need a populated description (so it renders) — but then its name legitimately
appears in the wire via the ordinary entity section, defeating the "no orphaned target name in any
form" assertion codex flagged as impossible.

**Fix:** the precondition checks **presence in `RecallAdmissionRecord.items`** (any
`RecalledMemoryRecord` with `kind == MemoryItemKind.ENTITY and identity == target`), independent of
its `.admitted` value. This is both the semantically correct gate for *this* mechanism (recall
selected it — my hook doesn't care if it went on to render) and still literally "read from the
admitted-recall record the evidence contract writes" (the record's `items` field is documented as
"every candidate, admitted or dropped — never filtered," `turn_evidence.py:252`). It resolves both
findings:

- **AC-6 (finding 1):** the target entity can now carry an **empty description too** — it is still
  a legitimate recall candidate (precondition holds) even though neither its entity line nor its
  stance line renders, so "no entry for it in any form" becomes literally achievable, not
  self-contradicting. The populated control uses a **separate turn** (per the ADR's own "Then run a
  turn recalling a populated topic-scoped stance" — three sequential checks, not one combined turn),
  removing any need to distinguish an entity-section mention from a stance-section mention in mixed
  content.
- **AC-1 negative half (finding 8):** the negative turn now explicitly asserts the *target*
  identity has **no** `MemoryItemKind.ENTITY` record in `items` at all (not merely that some
  *other*, unrelated entity was recalled instead) — proving the target's absence from the recall
  set is the reason its affect is missing, not a masked stance-layer defect.

## Implementation steps

### 1. `memory/service.py` — new method `query_current_stances`

Add near `query_stance_history` (after it, before `query_claims_history`):

```python
async def query_current_stances(
    self,
    targets: list[str],
    *,
    authenticated: bool,
    trace_id: str | None = None,
) -> list[dict[str, Any]]:
    """Current-only, batched Stance retrieval for topic-scoped push enrichment (ADR-0126 D1/D2/D5/D6).

    Push surface: called once per turn for every entity the recall path already selected
    (``request_gateway/context.py``, and the pre-gateway fallback in ``orchestrator/executor.py``).
    Current stances only (``valid_to IS NULL AND invalid_at IS NULL``, D5) — a superseded original
    is never returned here; the chain is :meth:`query_stance_history`'s job (pull-only, ADR-0126
    T4). No emptiness filtering: D6 requires filtering to happen at render, not fetch, so an
    empty-affect row (e.g. ``Barrage républicain``) is returned like any other and the renderer is
    what proves it never reaches the model.

    Stance is the harness owner's worldview toward World knowledge — a single ``is_owner``
    sentinel edge, not per-user (mirrors :meth:`query_stance_history`; no ``user_id``
    parameter, unlike Claim's ``HAS_FACT`` scoping).

    Args:
        targets: Entity names already selected by the recall path this turn. Empty
            returns [] without a query.
        authenticated: Whether the request carries a verified identity. False returns [].
        trace_id: Request trace id for log correlation.

    Returns:
        One dict per target with a current stance, each with ``target``, ``affect``,
        ``mastery``. Targets with no current stance are simply absent — never a
        placeholder row. Order is not guaranteed to match ``targets``' order (callers
        that need order-preservation must re-key by ``target`` themselves).
    """
    if not self.connected or not self.driver:
        return []
    if not authenticated:
        return []
    if not targets:
        return []

    try:
        async with self.driver.session() as db_session:
            result = await db_session.run(
                "UNWIND $targets AS target\n"
                "MATCH (:Person {is_owner: true})-[s:HAS_STANCE]->(:Entity {name: target})\n"
                "WHERE s.valid_to IS NULL AND s.invalid_at IS NULL\n"
                "RETURN target, s.affect AS affect, s.mastery AS mastery",
                targets=targets,
            )
            stances: list[dict[str, Any]] = []
            async for row in result:
                stances.append(
                    {
                        "target": row["target"],
                        "affect": row["affect"] or "",
                        "mastery": row["mastery"],
                    }
                )
    except Exception as e:
        log.warning("query_current_stances_failed", error=str(e), trace_id=trace_id)
        return []

    log.info(
        "current_stances_queried",
        trace_id=trace_id,
        target_count=len(targets),
        result_count=len(stances),
    )
    return stances
```

**Verify:** `make test-file FILE=tests/personal_agent/memory/test_query_current_stances.py` (new,
step 7) — fake-driver unit test mirroring `test_query_stance_history.py`'s pattern, **plus** a
live-Neo4j case (see step 7) proving `valid_to` and `invalid_at` are each independently enforced —
`assert_stance` always sets both together (L2379), so a fake-driver test alone cannot prove the
query's `WHERE` clause requires both predicates rather than either one.

### 2. `memory/protocol.py` — add to `MemoryProtocol`

After `suggest_relevant` (L244-270):

```python
async def get_current_stances(
    self,
    targets: list[str],
    trace_id: str,
    authenticated: bool = False,
) -> list[dict[str, Any]]:
    """Current-only Stance retrieval for topic-scoped push enrichment (ADR-0126 T1).

    Args:
        targets: Entity names already selected by this turn's recall.
        trace_id: Trace identifier for observability.
        authenticated: Whether the caller is authenticated.

    Returns:
        One dict per target carrying a current stance (``target``, ``affect``, ``mastery``).
    """
    ...
```

Also add the same method to `FakeMemory` in `tests/personal_agent/memory/test_protocol.py` (L207) —
a required protocol method with no fake implementation breaks that file's runtime-checkable
conformance assertion (L254).

### 3. `memory/protocol_adapter.py` — implement on `MemoryServiceAdapter`

```python
async def get_current_stances(
    self,
    targets: list[str],
    trace_id: str,
    authenticated: bool = False,
) -> list[dict[str, Any]]:
    """Delegate to MemoryService's current-only batched stance query."""
    return await self._service.query_current_stances(
        targets, authenticated=authenticated, trace_id=trace_id
    )
```

**Verify:** new unit test asserting `targets`, `authenticated`, and `trace_id` are forwarded exactly
(codex finding 10) — a `MagicMock`/`AsyncMock` service, assert the mock's call args.

### 4. `captains_log/turn_evidence.py` — STANCE kind, namespaced identity

- Add `STANCE = "stance"` to `MemoryItemKind` (after `SESSION_FACT`, before `UNKNOWN`).
- In `memory_item_identity()`, add a branch:
  ```python
  if declared == "stance":
      target = _text(item.get("target"))
      return (MemoryItemKind.STANCE, f"stance:{target}" if target else "")
  ```
  (Empty target still yields an empty identity — consistent with every other branch's "identity is
  never guessed" contract.)

**Verify:** existing test suite still passes unchanged (no shared-type-signature changes — see
Design decision 5); add cases for the new branch (step 7).

### 5. `request_gateway/context.py` — shared helpers + the enrichment hook

Add two module-level helpers (placed near `_format_broad_recall_context`, exported for
`executor.py` to import):

```python
def _entity_names_from_memory_context(memory_context: list[dict[str, Any]]) -> list[str]:
    """Order-preserving, deduplicated entity names from a memory-context list.

    Order matters: stance rendering must follow the same relevance order recall already
    established (ADR-0126 — enrichment must not become a second, unstated ranking decision).
    """
    seen: dict[str, None] = {}
    for item in memory_context:
        if item.get("type") == "entity":
            name = item.get("name")
            if name:
                seen.setdefault(name, None)
    return list(seen)


def _stance_context_items(
    entity_names: list[str], stances: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Build ``{"type": "stance", ...}`` items in ``entity_names`` order.

    ``stances`` (from ``get_current_stances``) has no guaranteed order — re-keyed by target
    and walked in ``entity_names``'s order so a stance never renders in an order recall did
    not establish.
    """
    by_target = {s.get("target", ""): s for s in stances}
    return [
        {"type": "stance", "target": name, "affect": by_target[name].get("affect", "")}
        for name in entity_names
        if name in by_target
    ]


async def _enrich_with_stances(
    memory_context: list[dict[str, Any]],
    memory_adapter: MemoryProtocol,
    trace_id: str,
    authenticated: bool,
) -> None:
    """Push each recalled entity's current stance into ``memory_context`` (ADR-0126 T1).

    Mutates ``memory_context`` in place. Fails closed: a stance-layer fault omits enrichment
    for this turn rather than failing it — ``MemoryProtocol`` is an interface, so a swapped
    implementation is not guaranteed to fail closed internally the way ``MemoryService`` does.
    """
    if not memory_context or not authenticated:
        return
    entity_names = _entity_names_from_memory_context(memory_context)
    if not entity_names:
        return
    try:
        stances = await memory_adapter.get_current_stances(
            entity_names, trace_id=trace_id, authenticated=authenticated
        )
    except Exception:
        logger.exception("stance_enrichment_failed", trace_id=trace_id)
        return
    memory_context.extend(_stance_context_items(entity_names, stances))
```

In `assemble_context()`, immediately after the existing block (L360-370):

```python
    if memory_adapter is not None:
        memory_context, memory_scores = await _query_memory_for_intent(
            intent=intent,
            user_message=user_message,
            memory_adapter=memory_adapter,
            trace_id=trace_id,
            session_id=session_id,
            session_messages=session_messages,
            user_id=user_id,
            authenticated=authenticated,
        )
        if memory_context is not None:
            await _enrich_with_stances(memory_context, memory_adapter, trace_id, authenticated)
```

**Verify:** `make test-file FILE=tests/personal_agent/request_gateway/test_context.py`.

### 6. `orchestrator/executor.py` — second hook (pre-gateway fallback) + render + D6 filter

**6a. Second hook**, in the pre-gateway broad-recall branch, immediately after
`ctx.memory_context = _format_broad_recall(broad)` (L3652) and **before**
`ctx.recall_candidates = build_recall_candidates(ctx.memory_context, {})` (L3655) — the candidates
record must reflect the *enriched* list, not the pre-enrichment one:

```python
                        ctx.memory_context = _format_broad_recall(broad)
                        if ctx.memory_context:
                            from personal_agent.request_gateway.context import (
                                _entity_names_from_memory_context,
                                _stance_context_items,
                            )

                            entity_names = _entity_names_from_memory_context(ctx.memory_context)
                            if entity_names and ctx.authenticated:
                                try:
                                    stances = await memory_service.query_current_stances(
                                        entity_names,
                                        authenticated=ctx.authenticated,
                                        trace_id=ctx.trace_id,
                                    )
                                    ctx.memory_context.extend(
                                        _stance_context_items(entity_names, stances)
                                    )
                                except Exception as stance_err:
                                    log.warning(
                                        "stance_enrichment_failed",
                                        trace_id=ctx.trace_id,
                                        error=str(stance_err),
                                    )
                        # FRE-1004: legacy path — no gateway candidates to inherit,
                        # so the recalled set is its own candidate set.
                        ctx.recall_candidates = build_recall_candidates(ctx.memory_context, {})
```

This calls `MemoryService.query_current_stances` directly (not through `MemoryProtocol`/
`MemoryServiceAdapter`) — consistent with this fallback's existing style, which already calls
`memory_service.query_memory_broad(...)` directly rather than through the protocol.

**6b. Render + D6 filter.** Add a cap constant after `_MAX_RENDERED_EPISODES` (L2223-2231):

```python
_MAX_RENDERED_STANCES = _MAX_RENDERED_ENTITIES
"""Rank cap on rendered stances (ADR-0126 T1).

Deliberately the *same* constant as _MAX_RENDERED_ENTITIES, not an independent value: a stance
is only ever fetched for an entity the recall path already selected, so its rendered prefix
must never exceed what the entity prefix already bounds — an independent cap value could select
a stance subset misaligned with which entities actually render, which would make this an
unstated second relevance decision (the one thing ADR-0126 forbids).
"""
```

Add a line-renderer near `_entity_line`/`_episode_text`:

```python
def _stance_line(item: dict[str, Any]) -> str:
    """Render one current stance. Mastery is not rendered (ADR-0126 D1: affect alone is
    sufficient; mastery is correctly null on every live topic-scoped stance)."""
    target = item.get("target", "")
    affect = mark_truncated((item.get("affect") or "").strip(), _MAX_ITEM_CHARS)
    return f"- {target}: {affect}"
```

In `_render_memory_section_with_ids`, extend the classification loop and add a third section,
following the described/recalled pattern exactly (L2314-2360). **Order is preserved throughout —
no `sorted()`/set anywhere in this path**, so the stance prefix reflects the same order
`_entity_names_from_memory_context` established:

```python
    entities: list[dict[str, Any]] = []
    episodes: list[dict[str, Any]] = []
    stance_items: list[dict[str, Any]] = []
    for item in items:
        kind, _ = memory_item_identity(item)
        if kind is MemoryItemKind.ENTITY:
            entities.append(item)
        elif kind is MemoryItemKind.EPISODE:
            episodes.append(item)
        elif kind is MemoryItemKind.STANCE:
            stance_items.append(item)

    ...
    stances = [m for m in stance_items if (m.get("affect") or "").strip()][
        :_MAX_RENDERED_STANCES
    ]
    ...
    if stances:
        rendered_ids.extend(memory_item_identity(m)[1] for m in stances)
        section = "\n\n## What The User Thinks About Related Topics\n"
        section += "\n".join(_stance_line(m) for m in stances)
        sections.append(section)
```

(Insert the `stances` filter line alongside `described`/`recalled` at L2329-2332, and the `if
stances:` block after the `if recalled:` block at L2347-2358 — same shape, third bucket.)

**Verify:** `make test-file FILE=tests/personal_agent/orchestrator/test_memory_section_render.py`
(extend with stance cases) and the full render-filter suite.

### 7. Tests

**Unit — `tests/personal_agent/memory/test_query_current_stances.py`** (new, mirrors
`test_query_stance_history.py`'s fake-driver fixture pattern):
- Current stance returned; superseded (`valid_to` set) excluded.
- Multiple targets batched in one call (assert the Cypher `targets` param, not N separate calls).
- `authenticated=False` → `[]`, no query run.
- Empty `targets` → `[]`, no query run.
- Empty-affect row is still returned (not filtered here — proves design decision 4).
- **Cypher text assertion**: the query string literally contains both `valid_to IS NULL` and
  `invalid_at IS NULL` (fake-driver tests can't prove filtering behaviourally — see the live case
  below for that).

**Live-Neo4j addition to the same file** (`pytestmark = pytest.mark.integration` on these specific
cases, or a small dedicated `test_query_current_stances_live.py` if mixing markers in one file is
inconvenient): seed one stance via direct Cypher with **only** `valid_to` set (not `invalid_at`),
and a second with **only** `invalid_at` set (not `valid_to`) — `assert_stance` cannot produce either
row (it always sets both together on supersession), so this must bypass it and `SET` the properties
directly. Assert `query_current_stances` excludes **both** independently — proving the `WHERE`
clause requires both predicates, not just one. *(Codex finding 9, MAJOR.)*

**Unit — `tests/personal_agent/request_gateway/test_context.py`** (extend):
- `memory_adapter.recall` returns an entity; `get_current_stances` mock returns a matching
  stance → `result.memory_context` contains a `{"type": "stance", ...}` item.
- Two entities recalled, only one has a stance, order preserved: stance item appears after
  its own entity, and entity order in `memory_context` is unaffected.
- No entities recalled → `get_current_stances` never called (assert not awaited).
- `authenticated=False` → `get_current_stances` never called even with entities present.
- `get_current_stances` raising → `assemble_context()` still returns normally, with the
  pre-enrichment `memory_context` unchanged (proves decision 7's fail-closed wrap).

**Unit — `tests/personal_agent/orchestrator/test_executor_recall_fallback.py`** (or extend the
nearest existing test file covering the pre-gateway path, if a better-fitting one exists — locate
via `grep -rl "_format_broad_recall\b" tests/`): the pre-gateway broad-recall branch enriches with
stances too, mirroring the gateway-path test above (mock `memory_service.query_current_stances`,
assert the stance item lands in `ctx.memory_context` and `ctx.recall_candidates`).

**Unit — `tests/personal_agent/captains_log/test_turn_evidence.py`** (extend):
- `memory_item_identity({"type": "stance", "target": "Python"})` →
  `(MemoryItemKind.STANCE, "stance:Python")`.
- An entity item and a stance item sharing the same target name resolve to **different**
  identities (`"Python"` vs `"stance:Python"`) — the direct regression test for the BLOCKER this
  namespacing exists to prevent.

**Unit — `tests/personal_agent/orchestrator/test_memory_section_render.py`** (extend):
- Non-empty-affect stance renders, target and affect substrings present.
- Empty/whitespace-only affect filtered — no line, no id in `rendered_ids` (D6).
- An entity and its same-named-target stance in the same render call: both render (entity line +
  stance line), each with its own correctly namespaced id in `rendered_ids` — proving no
  cross-consumption between the two.

**Integration — `tests/personal_agent/memory/test_adr_0126_topic_scoped_stance_push.py`** (new,
`pytestmark = pytest.mark.integration`, mirrors `test_adr_0126_claims_pull.py`'s live-Neo4j fixture
and render/inline/wire reproduction pattern):

Fixture: connect to test Neo4j (`:7688`, skip if unavailable), create a synthetic `is_owner` Person
if none exists (mirrors `test_adr_0126_supersession_chain.py`'s pattern — Stance has no user
scoping), clean up a `FRE1015_*`-prefixed Entity/Stance set before and after.

**Deterministic entity recall — real Stance retrieval, stubbed entity ranking** *(codex finding 4,
MAJOR — revision 1's multipath+patched-embedding approach replaced)*: a patched embedding makes an
entity *strong* in one fused-recall arm; it does not guarantee it survives fusion against competing
turn candidates — that's the exact FRE-1021 mechanism this ADR names as a known, accepted risk, not
something a test should depend on for determinism. Instead: monkeypatch
`MemoryService.query_memory` (the method `MemoryServiceAdapter.recall()` calls) to return a directly
constructed `MemoryQueryResult(entities=[EntityNode(entity_id=..., name=target, entity_type=...,
first_seen=..., last_seen=...)], conversations=[], relevance_scores={})` for the probe. This makes
entity recall itself deterministic (no ranking/fusion dependency) while keeping **stance retrieval
fully real** against live Neo4j via the real `MemoryServiceAdapter.get_current_stances()` →
`MemoryService.query_current_stances()` → real Cypher. `assemble_context()`, the renderer, the
inliner, and `build_wire_messages()` all run for real and unstubbed — only the entity-ranking step
(genuinely orthogonal to what T1 does) is deterministic by construction instead of by hoping fusion
cooperates. A **separate**, explicitly-labelled note in the test file's docstring records that real
end-to-end recall ranking (including FRE-1021's displacement mechanism) is out of scope for this
suite — that is what production observation and FRE-1021's own measurement ticket own, not a unit
proving stance enrichment is wired correctly.

**Precondition check** (shared helper, per the "recall candidacy" semantics above — see that
section for why `.admitted` is not the check): build `TurnEvidence` via the real
`captains_log.turn_evidence.build_turn_evidence` using `result.recall_candidates` (from
`AssembledContext`), `rendered_identities` from `_render_memory_section_with_ids`, `inline_outcome`
from `_inline_volatile_with_outcome`, and `wire_messages` from `build_wire_messages` — reusing
exactly the pieces `test_adr_0126_claims_pull.py` already constructs. Assert `any(i.kind ==
MemoryItemKind.ENTITY and i.identity == target for i in evidence.recall.items)` (candidacy, not
`i.admitted`); if false, `pytest.skip(f"precondition failed: {target} not a recall candidate this
turn — re-fixture, not a stance defect")`.

- **AC-1 positive half** (`FRE1015_Python`, affect `"prefers over Java"`, populated description so
  it also renders normally): stub entity recall to return `FRE1015_Python`; assert precondition;
  assert affect string present in serialized wire.
- **AC-1 negative half**: stub entity recall to return an unrelated `FRE1015_Unrelated` entity (no
  stance) instead. Assert `FRE1015_Python` (the stance-bearing target) has **no**
  `MemoryItemKind.ENTITY` record in `evidence.recall.items` at all — proving its absence, not merely
  that something else was recalled. Then assert the affect string is absent from the wire.
- **AC-5 push half** (`FRE1015_Sorbet`, seed two stances via `assert_stance` — vague then specific,
  mirroring `test_adr_0126_supersession_chain.py`'s exact fixture text): stub entity recall to
  return `FRE1015_Sorbet`; assert precondition; assert current affect present, superseded affect
  absent, in the wire.
- **AC-6 empty-affect half** (`FRE1015_BarrageRepublicain`, `affect=""`, **empty description too** —
  see the precondition-semantics section for why this no longer contradicts the precondition):
  stub entity recall to return it; assert precondition (candidacy, not admission — it will **not**
  be admitted, and that's expected and fine); assert the entity name is absent from the wire in any
  form (no entity line — filtered by FRE-1010's existing empty-description guard — and no stance
  line — filtered by D6's affect guard).
- **AC-6 whitespace-only half**: same fixture, affect `"   "` instead of `""`. Same assertions.
- **AC-6 populated control** (separate turn, `FRE1015_Comte`-style, non-empty affect and
  description): stub entity recall to return it; assert precondition; assert its affect **is**
  present in the wire.

**Verify:** `make test-infra-up` if not running; `make test-file
FILE=tests/personal_agent/memory/test_adr_0126_topic_scoped_stance_push.py`.

### 8. Quality gates

`make test` (module: `tests/personal_agent/memory/`, `tests/personal_agent/request_gateway/`,
`tests/personal_agent/orchestrator/`, `tests/personal_agent/captains_log/` — then full) ·
`make mypy` · `make ruff-check` · `make ruff-format` · `pre-commit run --all-files`. Self-review:
`code-review` skill at `high` effort (src/ memory read-path + new ADR-implementation, two hook
points, a shared-evidence-contract change), plus `security-review` (new Cypher — parameterised via
`$targets`, not string-built, but worth the pass).

## Explicitly out of scope (per ADR D8 / D2, and ticket text)

- Behavioural/always-present stance layer — FRE-1017 (T2). See Design decision 2 for why T1
  deliberately does not pre-filter for it.
- Supersession chain retrieval — already shipped (FRE-1018, T4).
- Claims (any surface) — already shipped (FRE-1016, T3) / D4 (pull-only, out of scope for Stance).
- Real end-to-end recall-ranking determinism (FRE-1021's displacement mechanism) — this ticket's
  integration tests stub entity *selection* deterministically and test stance *enrichment* for
  real; they do not exercise or claim anything about production ranking behaviour.
- AC-7 (behavioural layer's 12-stance/1500-byte cap) — T2's criterion, not T1's.
- AC-8 (SEAM) — FRE-1019, master's to hold once all consumers exist.
