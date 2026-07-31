# FRE-1017 — ADR-0126 T2: The standing behavioural profile (always-present stances)

**Ticket:** FRE-1017 · **ADR:** ADR-0126 (D2 behavioural half, D3, AC-7 bound) · **Tier:** Tier-2:Sonnet
**Blocked by:** FRE-1015 (T1, merged — PR #738) · **Blocks:** FRE-1019 (T5 SEAM)

**Revision 2** — after a codex plan-review pass (`task-ms8yuoqt-nkze70`) found 2 BLOCKER + 1 MAJOR +
1 MINOR issue in revision 1. Each is fixed below, at the point it applies:
1. **MAJOR** (executor.py L3760-3860, step 3c below): the entity-match sub-branch has no local
   `try/except` (unlike the broad-recall sub-branch, which already catches locally) — an exception
   there jumps straight to the outer handler and skips the post-branch injection point entirely,
   which would make the "always-present, not gated on entity recall" guarantee fail exactly when
   recall itself is unhealthy. Fixed by giving the entity-match sub-branch its own local
   `try/except`, mirroring the broad-recall sub-branch's existing shape.
2. **MINOR** (step 2 below): `get_current_stances`/`query_current_stances`'s docstrings state
   targets are "entity names already selected by this turn's recall," which becomes inaccurate once
   a second, fixed-list caller exists. Fixed with a one-line docstring addition, no behaviour change.
3. **BLOCKER** (integration test design, step 5 below): the original design called
   `owner_service.assert_stance()` on synthetic target names with no backing `:Entity` node —
   `assert_stance`'s Cypher is `MATCH (c:Entity {name: $target})`, which returns `False` (skipped,
   logged) when the target does not exist, so no stance would ever be written and the positive
   assertions could never pass. Fixed by seeding a bare `:Entity` node via raw Cypher first (`CREATE
   (:Entity {name: ..., class: 'World'})`, no `Turn`/`DISCUSSES` edge — mirrors
   `test_adr_0126_supersession_chain.py:56`'s exact precedent), so the target exists for
   `assert_stance` but is never a recall candidate.
4. **BLOCKER** (integration test design, step 5 below): a suite that only ever monkeypatches
   `CURATED_BEHAVIOURAL_STANCE_TARGETS` to synthetic names never proves anything about the actual
   shipped constant — it could be empty, misspelled, or point at nonexistent entities and the suite
   would stay green. This is the same class of vacuity T1's own suite was corrected for (a stub
   whose own installed value is what gets asserted back). Fixed by adding a dedicated test that
   seeds real Stance edges for the **actual, unmonkeypatched** `CURATED_BEHAVIOURAL_STANCE_TARGETS`
   names and asserts every one of them reaches the wire — plus asserting the real constant's
   cardinality is both non-zero and ≤ 12 (a `len() <= 12` check alone passes on an empty set).
5. **MAJOR-adjacent** (AC-7 measurement, step 5 below): measuring `len(_serialized(wire).encode())`
   on the raw joined message-content string is not "the serialized provider request" — that string
   is pre-JSON-serialization, so it excludes JSON escaping overhead and cannot be compared against a
   real per-request byte budget. Fixed by measuring `len(json.dumps(wire,
   ensure_ascii=False).encode("utf-8"))` — the JSON encoding of the exact message list
   `build_wire_messages` hands to the HTTP client layer — and taking it **differentially** (with the
   curated set present vs. an ablated empty-tuple baseline on the identical turn), which is what ADR-
   0126's own wording asks for ("the byte length the behavioural layer **contributes**") rather than
   an absolute measurement of one hand-picked substring.

## Scope (from the ticket)

Build an always-present layer: inject a small owner-curated set of standing behavioural Stance
affects into assembled context on **every** turn, independent of what the recall path selected —
never gated on entity recall (unlike T1's topic-scoped enrichment, which this ticket does not
change).

Carried acceptance criteria (ADR-0126):
- **AC-2** — a curated behavioural affect is present on a turn whose recall set contains **neither**
  curated target.
- **AC-3** — a topic-scoped (non-curated) affect stays **absent** on that same probe turn. Checked
  as a pair with AC-2 on the same turn — either alone is satisfiable by a degenerate implementation.
- **AC-7** — the curated set holds **at most 12** stances and contributes **at most 1,500 bytes** to
  the serialized provider request; the contribution is non-zero and rises when a stance is added.

Not this ticket: AC-1/AC-5/AC-6 (T1, already shipped), AC-4 (Claims, T3), AC-8 (SEAM, FRE-1019).

## Verified against the live substrate

Queried `cloud-sim-neo4j` directly (current `HAS_STANCE` edges, `valid_to`/`invalid_at` both null):
the ADR's four named curated targets exist verbatim as `:Entity.name` values — `Artifact`
("prefers explicit request before creation"), `Plain text responses` ("prefers by default for
follow-up data"), `production transactions` ("wants guidance instead of another artifact"),
`Health Issues` ("wants condition-level recall"). Confirms exact capitalisation/spelling for the
Cypher exact-match the existing `query_current_stances` query performs — no new query needed (see
below).

## Codebase state after T1 (verified by direct read, 2026-07-31)

- `request_gateway/context.py` — `_enrich_with_stances()` (L220-255) is T1's topic-scoped hook,
  called from `assemble_context()` (L531-543) **only when `memory_context is not None`**. T2 must
  run independent of that condition.
- `orchestrator/executor.py` — T1's second hook lives inline in the pre-gateway broad-recall
  sub-branch (L3708-3736), gated on `entity_names` being non-empty. T2's fallback hook must run
  regardless of which sub-branch (broad-recall / entity-match) fired, or neither.
- `memory/protocol.py` / `protocol_adapter.py` / `memory/service.py` — `get_current_stances` /
  `query_current_stances` (T1) already do exactly what T2 needs: batched, current-only
  (`valid_to IS NULL AND invalid_at IS NULL`), gated on `authenticated`, targets passed in by the
  caller. **No changes needed here** — T2 is a second caller passing the curated list instead of
  recalled entity names.
- `captains_log/turn_evidence.py` — `MemoryItemKind.STANCE` and its `"stance:{target}"` namespaced
  identity (T1) exist. T2 needs its **own** kind (`BEHAVIOURAL_STANCE`) so the two producers stay
  independently identifiable/removable (AC-8 SEAM's mutation table treats them as separate
  consumers) and so a curated target that is *also* topic-scoped-recalled the same turn (e.g. the
  owner asks about `Artifact` directly) does not collide in `_resolve_admission`'s identity-keyed
  Counter — three possible items per target name (entity, `stance:`, `behavioural_stance:`), three
  distinct identities.
- `orchestrator/executor.py:3480-3499` — the DELEGATE decomposition's `memory_excerpt` builder
  already special-cases `item.get("type") == "stance"` to avoid an orphaned preference string (no
  `summary`/`description`/`name` key on a stance dict). A `behavioural_stance` item has the same
  shape and needs the same branch, or it silently renders as an empty string in that one excerpt
  path.

## Design decisions

1. **No new Cypher, no new protocol method.** `get_current_stances(targets, ...)` is a pure batched
   lookup keyed by `target` name; passing the curated tuple instead of recall-selected entity names
   is the entire mechanism difference. Reusing it also means T2 inherits T1's fail-closed behaviour
   (`authenticated=False` → `[]`, empty targets → `[]`) for free.
2. **The curated set is a module-level constant in `request_gateway/context.py`**, not a settings
   field — ADR-0125 D7 / ADR-0126 D3: a read-time facet, revisable by editing a tuple, no migration,
   no write-path change. `executor.py`'s fallback hook imports it from there (mirrors T1's own
   cross-module import of `_entity_names_from_memory_context`/`_stance_context_items`).
3. **New `MemoryItemKind.BEHAVIOURAL_STANCE`, item shape `{"type": "behavioural_stance", "target":
   ..., "affect": ...}`.** Identity is namespaced `f"behavioural_stance:{target}"` — same rationale
   as T1's `stance:` namespacing (finding 2 of the T1 codex review), extended one kind further.
4. **Ordering is the curated tuple's own declared order — not recall order, not query-return
   order.** Unlike T1 (which must preserve recall's order because recall is the only ranking that
   exists), this layer has no recall selection to inherit an order from; the curated list *is* the
   ranking, D3's whole point being that it is a deliberate, revisable, owner-authored set.
5. **D6 empty/whitespace-affect filtering happens at render, not fetch** — identical to T1,
   identical reason (keeps the empty-affect case observable end-to-end, matches the one filter-then-
   cap shape the renderer already uses for every kind).
6. **Render cap `_MAX_RENDERED_BEHAVIOURAL_STANCES = 12`, fixed independently — not derived from
   `_MAX_RENDERED_ENTITIES`.** T1's stance cap deliberately *equals* the entity cap because topic-
   scoped stances ride the entity selection order/bound (an independent value there would be an
   unstated second relevance decision). This layer has no recall selection to ride on at all — AC-7
   fixes its own ceiling directly, so the render cap restates AC-7's number, not another producer's.
7. **Two hook points, mirroring T1's shape exactly:**
   - `request_gateway/context.py::assemble_context()` — call the new injector **unconditionally**
     after the existing `if memory_context is not None: await _enrich_with_stances(...)` block, not
     inside it. It must run even when `memory_context` is `None` (nothing else was recalled this
     turn) — that is precisely AC-2's scenario. The injector accepts `memory_context: list | None`
     and returns a list (freshly created if it was `None` and at least one curated stance was
     fetched, otherwise the original object unchanged).
   - `orchestrator/executor.py`'s pre-gateway fallback — placed **after** the broad-recall/entity-
     match `if/else` (both sub-branches, at the same indentation as the "Populate operator stanza"
     comment), not nested inside either sub-branch, and not gated on `entity_names`/
     `potential_entities` — it must fire whether or not either recall sub-branch produced anything.
8. **Fail-closed**, same shape as T1: `try/except Exception: log.warning(...); return
   memory_context unchanged` (context.py) / continue past this turn's fetch (executor.py). A
   stance-layer fault omits the layer for this turn; it never fails the turn.
9. **Reuse `_stance_line` for rendering** — the behavioural item shape (`target`, `affect`) is
   identical to T1's stance shape; no new renderer function needed, only a new classification bucket
   and a distinctly-headed section (`## Standing Behavioural Preferences`) in
   `_render_memory_section_with_ids`.
10. **Duplication across the two stance sections is accepted, not deduped** — already decided by T1
    (Design decision 2 in its own plan): if a curated target is *also* topic-scoped-recalled the
    same turn, its affect can legitimately appear in both sections. Not this ticket's problem to
    solve; D2's split is entity-gated-vs-always-present, not mutual exclusion.

## Implementation steps

### 1. `captains_log/turn_evidence.py`

- Add `BEHAVIOURAL_STANCE = "behavioural_stance"` to `MemoryItemKind` (after `STANCE`).
- In `memory_item_identity()`, add (after the `stance` branch):
  ```python
  if declared == "behavioural_stance":
      target = _text(item.get("target"))
      return (
          MemoryItemKind.BEHAVIOURAL_STANCE,
          f"behavioural_stance:{target}" if target else "",
      )
  ```

**Verify:** extend `tests/personal_agent/captains_log/test_turn_evidence.py` — identity namespaced;
blank target → empty identity; an entity, a topic-scoped stance and a behavioural stance sharing one
target name resolve to three distinct identities.

### 2. `request_gateway/context.py`

Add near `_entity_names_from_memory_context`/`_stance_context_items`:

```python
CURATED_BEHAVIOURAL_STANCE_TARGETS: tuple[str, ...] = (
    "Artifact",
    "Plain text responses",
    "production transactions",
    "Health Issues",
)
"""Owner-curated standing-behavioural Stance targets (ADR-0126 D2/D3, T2).

Read-time facet, not a stored field or classifier (ADR-0125 D7): each name is an ordinary
:Entity node, no different from any topic-scoped stance target. Revising this set takes
effect on the next turn -- no migration, no write-path change. Bounded to at most 12
entries by ADR-0126 AC-7; raising that bound requires amending the ADR. Order is the
injection order -- this list IS the ranking, there is no recall selection to preserve.
"""


def _behavioural_stance_context_items(stances: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Build ``{"type": "behavioural_stance", ...}`` items in curated-set order (ADR-0126 T2).

    Unlike ``_stance_context_items`` (T1), order comes from
    ``CURATED_BEHAVIOURAL_STANCE_TARGETS`` itself, not from any recall order -- this layer
    has no recall selection to preserve.
    """
    by_target = {s.get("target", ""): s for s in stances}
    return [
        {
            "type": "behavioural_stance",
            "target": target,
            "affect": by_target[target].get("affect", ""),
        }
        for target in CURATED_BEHAVIOURAL_STANCE_TARGETS
        if target in by_target
    ]


async def _inject_behavioural_stances(
    memory_context: list[dict[str, Any]] | None,
    memory_adapter: MemoryProtocol,
    trace_id: str,
    authenticated: bool,
) -> list[dict[str, Any]] | None:
    """Push the curated standing-behavioural Stance set into context, every turn (ADR-0126 T2, D2).

    Independent of what the recall path selected -- unlike ``_enrich_with_stances``'s
    topic-scoped enrichment, this never reads ``memory_context`` for its targets and runs
    even when ``memory_context`` is ``None`` (nothing else was recalled this turn), because
    a standing behavioural preference must be present before the behaviour it governs
    occurs, not only when its own topic happens to come up (ADR-0126 D2's motivating case).

    Fails closed: a stance-layer fault omits the layer for this turn rather than failing it.

    Args:
        memory_context: The turn's memory-context list so far, or None if nothing was
            recalled. Not mutated in place when None -- a new list is returned instead, so
            the no-op paths below can return the original object unchanged.
        memory_adapter: Seshat protocol adapter.
        trace_id: Request trace identifier.
        authenticated: Whether the request carries a verified identity. Unauthenticated
            requests never fetch stance (mirrors ``_enrich_with_stances``).

    Returns:
        ``memory_context`` with the curated behavioural items appended (a new list if it
        was ``None`` and at least one was fetched), or unchanged otherwise.
    """
    if not authenticated:
        return memory_context
    try:
        stances = await memory_adapter.get_current_stances(
            list(CURATED_BEHAVIOURAL_STANCE_TARGETS),
            trace_id=trace_id,
            authenticated=authenticated,
        )
    except Exception:
        logger.exception("behavioural_stance_injection_failed", trace_id=trace_id)
        return memory_context
    items = _behavioural_stance_context_items(stances)
    if not items:
        return memory_context
    result = memory_context if memory_context is not None else []
    result.extend(items)
    return result
```

In `assemble_context()`, change:
```python
        if memory_context is not None:
            await _enrich_with_stances(memory_context, memory_adapter, trace_id, authenticated)
```
to:
```python
        if memory_context is not None:
            await _enrich_with_stances(memory_context, memory_adapter, trace_id, authenticated)
        memory_context = await _inject_behavioural_stances(
            memory_context, memory_adapter, trace_id, authenticated
        )
```
(still inside the `if memory_adapter is not None:` block — no adapter means no substrate access,
same fail-closed boundary as everything else here).

**Verify:** extend `tests/personal_agent/request_gateway/test_context.py`.

### 3. `orchestrator/executor.py`

**3a. Render cap + section**, after `_MAX_RENDERED_STANCES` (L2243-2252):
```python
_MAX_RENDERED_BEHAVIOURAL_STANCES = 12
"""Cap fixed by ADR-0126 AC-7 (T2): the curated behavioural set holds at most 12 stances.

Unlike _MAX_RENDERED_STANCES (T1), this is not inherited from another producer's bound --
there is no recall selection this layer rides on (D2: always-present, not gated on entity
recall). It restates AC-7's own ceiling directly, so a curated-set edit that quietly grew
past 12 stays bounded by construction here even before a test assertion catches it. Raising
it requires amending ADR-0126.
"""
```

Extend the classification loop (L2347-2357) with a third bucket, and the filter/cap block
(L2365-2372) and section-building block (L2374-2405) with a fourth section — placed **first**
(before entities/episodes/topic-scoped stances), reusing `_stance_line`:
```python
    behavioural_items: list[dict[str, Any]] = []
    ...
        elif kind is MemoryItemKind.BEHAVIOURAL_STANCE:
            behavioural_items.append(item)
    ...
    behavioural = [m for m in behavioural_items if (m.get("affect") or "").strip()][
        :_MAX_RENDERED_BEHAVIOURAL_STANCES
    ]
    ...
    if behavioural:
        rendered_ids.extend(memory_item_identity(m)[1] for m in behavioural)
        section = "\n\n## Standing Behavioural Preferences\n"
        section += "\n".join(_stance_line(m) for m in behavioural)
        sections.append(section)
```
(placed before the `if described:` block, so `sections` assembles behavioural → entities →
episodes → topic-scoped stances)

**3b. Delegation excerpt fix** (L3490-3494): extend the existing `stance`-only special-case to
also cover `behavioural_stance`:
```python
                        or (
                            f"{item['target']}: {item['affect']}"
                            if item.get("type") in ("stance", "behavioural_stance")
                            else None
                        )
```

**3c-pre. Fix the entity-match sub-branch's missing local `try/except` (codex MAJOR finding).**
Today the broad-recall sub-branch (L3696-3752) catches its own exceptions locally
(`except Exception as broad_err:`), so an unrelated recall failure there still reaches whatever
runs after the `if/else`. The entity-match sub-branch (L3760-3803) has no such guard — its
`memory_service.query_memory(...)` call (L3772) can raise straight through to the *outer*
`except Exception as e:` at L3852, skipping everything between (including "Populate operator
stanza" today, and the new behavioural hook below). Since D2's guarantee is "present on every
turn," it must not silently disappear whenever entity-match recall throws for an unrelated
reason. Wrap the sub-branch's body in its own `try/except`, mirroring the broad-recall shape
exactly:
```python
                else:
                    # Entity-name match path (existing)
                    words = ctx.user_message.split()
                    potential_entities = [
                        w.strip('",.:;!?') for w in words if len(w) > 3 and w[0].isupper()
                    ]
                    if potential_entities:
                        try:
                            query = MemoryQuery(
                                entity_names=potential_entities[:5],
                                limit=5,
                                recency_days=30,
                            )
                            result = await memory_service.query_memory(
                                query,
                                feedback_key=ctx.session_id,
                                query_text=ctx.user_message,
                                trace_id=ctx.trace_id,
                                session_id=ctx.session_id,
                                user_id=ctx.user_id,
                                authenticated=ctx.authenticated,
                            )
                            ctx.memory_context = [
                                {
                                    "conversation_id": conv.turn_id,
                                    "timestamp": conv.timestamp.isoformat(),
                                    "user_message": conv.user_message,
                                    "summary": conv.summary
                                    or mark_truncated(conv.user_message, 400),
                                    "key_entities": conv.key_entities,
                                }
                                for conv in result.conversations
                            ]
                            ctx.recall_candidates = build_recall_candidates(
                                ctx.memory_context, {}
                            )
                            conversations_found = len(ctx.memory_context)
                            log.info(
                                "memory_enrichment_completed",
                                trace_id=ctx.trace_id,
                                conversations_found=conversations_found,
                            )
                        except Exception as entity_match_err:
                            log.warning(
                                "memory_entity_match_query_failed",
                                trace_id=ctx.trace_id,
                                error=str(entity_match_err),
                            )
```
This is a pre-existing gap (operator-stanza population already silently skips on this same
failure today), but it becomes load-bearing for *this* ticket's own D2 claim, so it is folded in
here rather than filed separately (build skill Step 5 — a supporting fix this ticket's own change
depends on).

**3c. Second hook**, in the pre-gateway fallback (`step_init`, `gateway_output is None` path),
after the broad-recall/entity-match `if/else` block ends (after L3802), before the "Populate
operator stanza" comment (L3804), at that same 16-space indentation:
```python
                # ADR-0126 T2 (FRE-1017): standing behavioural stances, independent of
                # what either recall branch above selected -- present whenever
                # memory_service is connected, not gated on entity recall (D2).
                if ctx.authenticated:
                    from personal_agent.request_gateway.context import (
                        CURATED_BEHAVIOURAL_STANCE_TARGETS,
                        _behavioural_stance_context_items,
                    )

                    try:
                        behavioural_stances = await memory_service.query_current_stances(
                            list(CURATED_BEHAVIOURAL_STANCE_TARGETS),
                            authenticated=ctx.authenticated,
                            trace_id=ctx.trace_id,
                        )
                        behavioural_items = _behavioural_stance_context_items(
                            behavioural_stances
                        )
                        if behavioural_items:
                            if ctx.memory_context is None:
                                ctx.memory_context = []
                            ctx.memory_context.extend(behavioural_items)
                            ctx.recall_candidates = build_recall_candidates(
                                ctx.memory_context, {}
                            )
                    except Exception as behavioural_err:
                        log.warning(
                            "behavioural_stance_injection_failed",
                            trace_id=ctx.trace_id,
                            error=str(behavioural_err),
                        )
```

**Verify:** extend `tests/personal_agent/orchestrator/test_memory_section_render.py` and
`tests/test_orchestrator/test_executor.py`.

### 4. Fix pre-existing tests broken by the new second call to `get_current_stances`/`query_current_stances`

Both hooks now call the **same mock** in existing T1 tests twice per turn (once for T1's recalled
entity names, once for T2's curated list) wherever that test authenticates the request and the
mock is shared. Cases found by direct read (fixed here, folded into this ticket per build skill
Step 5 — direct fallout of this ticket's own change, not scope creep):

- `tests/personal_agent/request_gateway/test_context.py::TestStanceEnrichment::
  test_stance_item_appended_for_recalled_entity` — `assert_awaited_once()` / `await_args` (singular)
  break because the mock is now awaited twice. Since the mock's `return_value` (`[{"target":
  "Python", ...}]`) never matches any curated target name, T2 injects nothing — only the assertion
  style needs to change, not the expected `memory_context` contents. Replace with a search over
  `await_args_list` for the call whose first positional arg is `["Python"]`.
- `tests/personal_agent/request_gateway/test_context.py::TestStanceEnrichment::
  test_no_entities_recalled_skips_stance_fetch` (found by codex review, missed in revision 1) —
  passes `authenticated=True` with no entities recalled, and asserts
  `get_current_stances.assert_not_called()`. T2's injector *does* call it now (for the curated set,
  unconditionally on authentication) even though T1's entity-gated hook still correctly has nothing
  to call for. `result.memory_context is None` still holds (the mock returns `[]`, so nothing is
  appended), but the call-count assertion must become
  `assert_awaited_once_with(list(CURATED_BEHAVIOURAL_STANCE_TARGETS), trace_id="t-none",
  authenticated=True)` — proving both halves: T1's hook made no call, T2's hook made exactly one,
  for the curated set.
- `tests/test_orchestrator/test_executor.py::TestExecutorFallbackStanceEnrichment::
  test_broad_recall_entity_gets_stance_enriched` — same `assert_awaited_once()` fix, same reasoning.
- `tests/test_orchestrator/test_executor.py::TestExecutorFallbackStanceEnrichment::
  test_no_entities_in_broad_recall_skips_stance_fetch` — currently asserts
  `query_current_stances.assert_not_called()`. With T2, the call now **does** happen (for the
  curated set) even though T1's entity-gated hook still correctly skips (no entity names). Replace
  with `assert_awaited_once_with(list(CURATED_BEHAVIOURAL_STANCE_TARGETS), authenticated=True,
  trace_id="trace-1015b")` — this proves *both* halves at once: T1's hook was not called with an
  (empty) entity list, and T2's hook fired with exactly the curated set.

No other existing test in these files asserts call counts against this mock (verified by grep for
`assert_awaited_once\|assert_not_called\|await_args` across both files' stance-related tests), so
no further fixes are needed there.

### 5. New tests

**`tests/personal_agent/request_gateway/test_context.py`** — new `TestBehaviouralStanceInjection`
class mirroring `TestStanceEnrichment`'s shape:
- curated stances present → `behavioural_stance` items appended, in curated-tuple order (not mock's
  return order).
- no memory_context at all (`memory_context is None` going in) + authenticated + adapter present →
  behavioural items still appended (proves the "runs even when nothing else was recalled" property
  — the AC-2 scenario, at the unit level).
- unauthenticated → `get_current_stances` not called for the curated set, memory_context unchanged.
- `get_current_stances` raising → fails closed, memory_context unchanged (including staying `None`
  when it started `None`).
- a stance whose target is not in the curated set is dropped (mirrors T1's equivalent test).

**`tests/test_orchestrator/test_executor.py`** — new
`TestExecutorFallbackBehaviouralStanceInjection` class:
- entity-match sub-branch with zero `potential_entities` (so `ctx.memory_context` stays `None`
  going in) + authenticated → behavioural items still land in `ctx.memory_context` and
  `ctx.recall_candidates`.
- unauthenticated → no call.
- raising → fails closed, turn completes.

**`tests/personal_agent/captains_log/test_turn_evidence.py`** — extend the stance-identity test
class:
- `memory_item_identity({"type": "behavioural_stance", "target": "Artifact", "affect": "..."})` →
  `(MemoryItemKind.BEHAVIOURAL_STANCE, "behavioural_stance:Artifact")`.
- blank target → empty identity.
- an entity, a topic-scoped stance, and a behavioural stance sharing target `"Artifact"` resolve to
  three pairwise-distinct identities.

**`tests/personal_agent/orchestrator/test_memory_section_render.py`** — extend
`TestRendererDispatchesPerItemKind` and `TestRendererIsBoundedByStatedConstants`:
- a behavioural stance with a non-empty affect renders; header
  `"## Standing Behavioural Preferences"` present.
- empty/whitespace-only affect filtered (D6) — no line, no id, no header.
- a behavioural stance and a topic-scoped stance sharing the same target both render, each with its
  own correctly namespaced id (`stance:X` vs `behavioural_stance:X`).
- cap: `_MAX_RENDERED_BEHAVIOURAL_STANCES` bounds cardinality on an oversized input, order-preserving
  prefix of **input order** (the curated tuple's own order, already established by the producer —
  the renderer must not re-sort).

**New integration file — `tests/personal_agent/memory/test_adr_0126_behavioural_stance_profile.py`**
(`pytestmark = pytest.mark.integration`, mirrors `test_adr_0126_topic_scoped_stance_push.py`'s
`_run_turn`/`_serialized`/`_memory_section_of` helpers). The `owner_service` fixture mirrors
`test_adr_0126_supersession_chain.py`'s pattern (explicit named-entity cleanup, not a session-id
scan, since these targets carry no `Turn`): it cleans up, both before and after, every synthetic
`FRE1017_*` name the suite uses **and** the real, unmonkeypatched
`CURATED_BEHAVIOURAL_STANCE_TARGETS` names (the latter only ever touches the isolated `:7688` test
substrate, never `cloud-sim-neo4j`/production).

```python
async def _seed_bare_entity(service: MemoryService, name: str) -> None:
    """A bare :Entity, no Turn/DISCUSSES edge -- exists for assert_stance's
    ``MATCH (c:Entity {name: $target})`` precondition (which CREATEs nothing itself and
    returns False when the target is absent) without ever being a recall candidate.
    Mirrors test_adr_0126_supersession_chain.py:56's exact precedent.
    """
    assert service.driver is not None
    async with service.driver.session() as s:
        await s.run("MERGE (:Entity {name: $name, class: 'World'})", name=name)


def _wire_json_bytes(wire: list[dict[str, Any]]) -> int:
    """A defensible proxy for 'the serialized provider request' (ADR-0126's fixed
    observation point): the JSON encoding of the exact message list build_wire_messages
    hands to the HTTP client layer, which is what actually gets serialized to bytes over
    the wire -- unlike a raw joined-string measurement, this reflects JSON escaping
    overhead. Used differentially (with vs. without the curated set on the identical
    turn), matching AC-7's own wording: "the byte length the behavioural layer
    *contributes*" -- a marginal measurement, not an absolute one.
    """
    return len(json.dumps(wire, ensure_ascii=False).encode("utf-8"))
```

- **AC-2 + AC-3 (paired, one test)**: monkeypatch `CURATED_BEHAVIOURAL_STANCE_TARGETS` to two
  synthetic, token-disjoint names (test-owned, not lifted from the ADR's illustrative text — same
  convention T1's own suite established). Seed a bare `:Entity` for each via
  `_seed_bare_entity`, then `owner_service.assert_stance` for each (assert both return `True` — this
  is what revision 1 got wrong: `assert_stance` alone against a nonexistent target silently returns
  `False`). Seed a third bare entity + stance for a distinct topic-scoped-only target (the AC-3
  analogue). Run a probe turn whose message shares no token with any of the three. Assert
  (precondition) none of the three has an `ENTITY`-kind record in `evidence.recall.items` — proving
  the recall set genuinely excludes them. Then assert both curated affects **are** present in the
  wire (AC-2) and the topic-scoped-only affect is **absent** (AC-3).
- **AC-2 real-production-set test (closes the codex-flagged vacuity gap)**: a *separate* test that
  does **not** monkeypatch the constant. Import the real, shipped `CURATED_BEHAVIOURAL_STANCE_TARGETS`,
  assert `0 < len(...) <= 12` (non-empty *and* bounded — a bare `<= 12` passes on an empty set, which
  is exactly the degenerate case this test exists to rule out), seed a bare entity + a real,
  distinguishable stance affect for **every** name in the real constant, run one probe turn, and
  assert every seeded affect string is present in the wire. This is the test that actually proves the
  shipped set works end to end — the monkeypatched AC-2/AC-3 test above only proves the *mechanism*
  does.
- **AC-7 cardinality**: covered by the same `0 < len(...) <= 12` assertion in the real-production-set
  test above — no separate test needed.
- **AC-7 byte bound + responsiveness**: monkeypatch the curated tuple to `()` on the identical probe
  turn used above to get a baseline `_wire_json_bytes` reading with the layer fully ablated. Then set
  it to one seeded synthetic name and re-run, computing `contribution = bytes_with_one -
  bytes_baseline`; assert `0 < contribution <= 1500`. Then set it to that name plus a second seeded
  synthetic name and re-run; assert the new contribution is strictly greater than the first — proving
  the measurement responds to the curated set changing, differentially, against the actual
  JSON-serialized request.

**Verify:** `make test-infra-up` if not already running;
`make test-file FILE=tests/personal_agent/memory/test_adr_0126_behavioural_stance_profile.py`.

### 6. Quality gates

`make test` (module: `tests/personal_agent/request_gateway/`, `tests/personal_agent/orchestrator/`,
`tests/personal_agent/captains_log/`, `tests/personal_agent/memory/`, `tests/test_orchestrator/` —
then full) · `make mypy` · `make ruff-check` · `make ruff-format` · `pre-commit run --all-files`.
Self-review: `code-review` skill at `high` effort (src/ memory read-path, new ADR-implementation
producer, two hook points, a shared-evidence-contract addition — same tier as T1's own review), plus
`security-review` (reuses T1's already-parameterised Cypher; no new query, but worth the pass since
this is a second, differently-sourced caller of it).

## Explicitly out of scope

- AC-1/AC-5/AC-6 (T1, already shipped) — unchanged by this ticket.
- AC-4 (Claims pull) — T3, already shipped, unrelated surface.
- AC-8 (SEAM) — FRE-1019, master's to hold once every consumer exists; this ticket only needs to
  leave the behavioural-injection call independently identifiable/removable (satisfied by design
  decision 7's two dedicated hook functions, mirroring T1's own shape).
- Any classifier deciding curated-set membership — D3 explicitly rejects this for T2; the set is a
  hand-maintained constant.
- Changing T1's topic-scoped mechanism or its render section — untouched except for the necessary
  delegation-excerpt branch widening (step 3b) and the shared render-dispatch function gaining a
  fourth bucket.
