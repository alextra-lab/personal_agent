# FRE-1016 — ADR-0126 T3: Claims pull path

**Ticket:** FRE-1016 (Approved, Tier-2:Sonnet, stream:build2)
**ADR:** ADR-0126 D4/D5 — Claims are pull-only, current-only, reachable solely via `search_memory`.
**Acceptance criterion carried:** AC-4 (both halves — see ADR §Verification).
**Blocks:** FRE-1018 (T4, supersession chain pull), FRE-1019 (T5 SEAM — owns AC-8, not this ticket).

## Scope (from ADR-0126)

- D4: Claims reachable **only** through `search_memory`; never injected into assembled context.
- D5 (claims half): pull returns **current** claims only (`valid_to IS NULL AND invalid_at IS NULL`).
  The supersession chain on pull is T4 (FRE-1018), out of scope here.
- Implementation notes from the ADR: no vector index needed at 91-claim scale (decide via
  measurement, default to Python cosine scan mirroring `memory/supersession.py`'s existing pattern);
  Claims carry no `visibility` property, so `_build_visibility_filter` does not apply — scoping is
  `:Person.user_id` on `HAS_FACT` (ADR-0107).

## Files touched

1. `src/personal_agent/memory/service.py` — new `MemoryService.query_claims()` method.
2. `src/personal_agent/tools/memory_search.py` — wire claims into `search_memory_executor`; extend
   tool description.
3. Tests (new/extended, listed in Step 4).

No changes to `request_gateway/context.py` or `memory/protocol.py` — confirmed zero existing
references to `Claim`/`HAS_FACT` in `request_gateway/`, `orchestrator/`, `gateway/` (re-verified this
session), and the claims-pull path is wired tool-side only, bypassing `MemoryProtocol` exactly as
`query_memory_broad` already does. AC-4(a) is a **proof that this stays true**, not new code.

## Step 1 — `MemoryService.query_claims` (new method, after `assert_claim`, before `ensure_vector_index`)

```python
async def query_claims(
    self,
    query_text: str,
    *,
    user_id: UUID | None,
    authenticated: bool,
    limit: int = 10,
    trace_id: str | None = None,
    session_id: str | None = None,
) -> list[dict[str, Any]]:
    """Pull-only Claim retrieval for ``search_memory`` (ADR-0126 D4).

    Claims are never injected into assembled context (D4); this is their only read
    surface. Current claims only (``valid_to IS NULL AND invalid_at IS NULL``, D5) —
    the supersession chain is a separate pull (ADR-0126 T4), not this method's job.
    Ranked by content-embedding cosine similarity to ``query_text``, computed in
    Python: 91 claims at today's scale does not justify a dedicated vector index
    (ADR-0126 implementation note; mirrors the existing scan in
    ``memory/supersession.py``).

    Claims carry no ``visibility`` property, so the entity-path visibility filter
    does not apply (ADR-0126 implementation note). Scoping is the owning
    ``:Person.user_id`` on ``HAS_FACT`` (ADR-0107) — an unauthenticated or
    identity-less call returns nothing rather than guessing a person.

    Args:
        query_text: Free-text query to match against claim content.
        user_id: The acting authenticated user's UUID. None returns [].
        authenticated: Whether the request carries a verified identity. False
            returns [] — Claims are personal facts with no visibility gate of
            their own, so scoping requires a verified identity, not just a UUID.
        limit: Maximum claims to return.
        trace_id: Request trace id, for log correlation and embedding cost
            attribution.
        session_id: Session id, for embedding cost attribution.

    Returns:
        Current claims ranked by descending similarity, each a dict with
        ``claim_id``, ``content``, ``confidence``, ``knowledge_class``, and
        ``observed_at`` — keys distinct from the entity/turn rows the tool
        returns today (ADR-0126 AC-4).
    """
    if not self.connected or not self.driver:
        return []
    if user_id is None or not authenticated:
        return []
    if not query_text or not query_text.strip():
        return []

    query_embedding = await generate_embedding(
        query_text, mode="query", trace_id=trace_id, session_id=session_id
    )
    if not any(x != 0.0 for x in query_embedding):
        # Embedder unavailable/degraded: a zero vector scores every current
        # claim identically, which would return an arbitrary slice of the
        # user's personal facts rather than nothing (mirrors the query_memory
        # zero-vector guard at service.py:3399-area).
        return []

    try:
        async with self.driver.session() as db_session:
            result = await db_session.run(
                "MATCH (:Person {user_id: $user_id})-[:HAS_FACT]->(cl:Claim)\n"
                "WHERE cl.valid_to IS NULL AND cl.invalid_at IS NULL\n"
                "RETURN cl.claim_id AS claim_id, cl.content AS content,\n"
                "       cl.confidence AS confidence, cl.class AS knowledge_class,\n"
                "       cl.observed_at AS observed_at, cl.embedding AS embedding",
                user_id=str(user_id),
            )
            scored: list[tuple[float, dict[str, Any]]] = []
            async for row in result:
                embedding = row["embedding"]
                if embedding is None or row["claim_id"] is None:
                    continue
                score = cosine_similarity(query_embedding, list(embedding))
                scored.append(
                    (
                        score,
                        {
                            "claim_id": row["claim_id"],
                            "content": row["content"] or "",
                            "confidence": float(row["confidence"] or 0.0),
                            "knowledge_class": row["knowledge_class"] or "Personal",
                            "observed_at": row["observed_at"],
                        },
                    )
                )
    except Exception as e:
        log.warning("query_claims_failed", error=str(e), trace_id=trace_id)
        return []

    scored.sort(key=lambda pair: pair[0], reverse=True)
    log.info(
        "claims_queried",
        trace_id=trace_id,
        candidate_count=len(scored),
        result_count=min(limit, len(scored)),
    )
    return [item for _, item in scored[:limit]]
```

Import addition: `from personal_agent.memory.embeddings import generate_embedding, generate_embeddings_batch, cosine_similarity` (currently only the first two are imported at `service.py:28`).

## Step 2 — Wire into `search_memory_executor` (`tools/memory_search.py`)

- Extend `search_memory_tool.description` to mention durable facts, e.g. append: `"Also returns
  durable facts (Claims) the user has previously asserted, distinct from entities and turns."`
- In `search_memory_executor`, after building `output` in **both** branches (entity-match and
  broad), add a claims lookup unconditionally (independent of the branch) — `query_claims` itself
  fail-closes on blank `query_text`, missing identity, or a zero embedding (Step 1), so the executor
  needs no duplicate gating:

```python
claims = await memory_service.query_claims(
    query_text,
    user_id=getattr(ctx, "user_id", None),
    authenticated=getattr(ctx, "authenticated", False),
    limit=limit,
    trace_id=trace_id,
    session_id=getattr(ctx, "session_id", None),
)
output["claims"] = claims
```

Place this right before the final `log.info("search_memory_tool_completed", ...)` call so it applies
to both `output` shapes uniformly.

## Step 3 — No context.py / protocol.py changes

Confirmed by design: `query_claims` is called only from the tool executor (mirrors how
`query_memory_broad` is called directly on `MemoryService`, bypassing `MemoryProtocol`). Push paths
(`request_gateway/context.py`, `_query_memory_for_intent`) are untouched — this is what AC-4(a) proves
stays true.

## Step 4 — Tests

### 4a. Unit tests — `tests/personal_agent/memory/test_query_claims.py` (new, no Neo4j required)

Mock the driver session (pattern from `tests/personal_agent/memory/test_visibility.py`
`_make_service_with_mock`). Cover:
- `user_id=None` → `[]`, no Cypher run.
- `authenticated=False` → `[]`, no Cypher run.
- Blank/whitespace-only `query_text` → `[]`, no Cypher run, no embedding call.
- Embedder degraded to a zero vector (patch `generate_embedding` to return `[0.0, 0.0]`) → `[]`, no
  Cypher run — proves a relevance-mechanism outage cannot dump an arbitrary slice of the user's
  claims (codex plan-review finding).
- Rows ranked by descending cosine similarity (patch `generate_embedding` deterministically, feed
  fixed embeddings per row, assert order).
- Rows missing `embedding` or `claim_id` are skipped, not raised.
- Result respects `limit`.
- Cypher query text contains `valid_to IS NULL AND invalid_at IS NULL` and scopes by `user_id`
  (string form of the UUID passed as a parameter).
- Not connected (`service.connected = False`) → `[]`.

### 4b. Unit tests — extend `tests/test_tools/test_memory_search.py`

- Both entity-match and broad-recall paths: `output["claims"]` present, sourced from a mocked
  `memory_service.query_claims` AsyncMock; assert the mock was awaited with `query_text`, `user_id`,
  `authenticated`, `trace_id`, `session_id`, `limit` threaded from `ctx`/args (mirrors the existing
  `test_search_memory_*_threads_identity` pattern).
  - **Distinguishability check** (part of AC-4(b) at the unit level): assert the `claims` entries'
    keys (`claim_id`, `content`, `confidence`, `knowledge_class`, `observed_at`) are disjoint from a
    `matched_turns`/`entities` row's keys.

### 4c. Live behavioural proof — `tests/personal_agent/memory/test_adr_0126_claims_pull.py` (new,
`@pytest.mark.integration`, live test Neo4j :7688 — mirrors `test_claims_stance_storage.py`'s
`owner_service` fixture)

This is the AC-4 proof cited in the Linear handoff comment. Both halves in one test module:

**Codex plan-review caught two vacuity gaps here (2026-07-28) — both folded in below:**
(1) `AssembledContext.messages` is not the wire form — recalled memory rides a separate
`memory_context` field that the *orchestrator* (not `assemble_context`) renders
(`_render_memory_section_with_ids`) and inlines into messages
(`_inline_volatile_with_outcome`) before `build_wire_messages` ever sees it; checking
`result.messages` alone skips the exact rendering surface ADR-0126's fixed observation point exists
to cover. (2) "reachable via `search_memory`" means the tool executor, not the service method
directly — calling `query_claims()` proves the query works, not that the tool composes it correctly.

- **Half (a)** — `test_ac4a_claim_never_reaches_assembled_context`:
  - Seed one claim via `assert_claim` (patched deterministic embedder so it is maximally similar to
    the probe message's embedding — same `_fake_embed`-style helper as the existing suite).
  - Build a real `MemoryServiceAdapter(owner_service)` (`memory.protocol_adapter`).
  - `monkeypatch` every recall toggle to its most permissive setting:
    `relevance_bounded_recall_enabled=True`, `multipath_recall_enabled=True`,
    `proactive_memory_enabled=True`, `recall_similarity_floor=0.0`.
  - Call `assemble_context(user_message=<probe>, session_messages=[], intent=..., memory_adapter=adapter,
    trace_id=..., user_id=_OWNER_UID, authenticated=True)` → `result` (an `AssembledContext`).
  - Reproduce the executor's real rendering pipeline (not just `result.messages`), importing directly
    from `personal_agent.orchestrator.executor`:
    ```python
    from personal_agent.orchestrator.executor import (
        _inline_volatile_with_outcome,
        _render_memory_section_with_ids,
        build_wire_messages,
    )
    memory_section, _ = _render_memory_section_with_ids(result.memory_context or [])
    final_messages, _ = _inline_volatile_with_outcome(result.messages, memory_section)
    wire = build_wire_messages(final_messages, "", trace_id)
    ```
  - Assert the claim's `content` string does not appear anywhere in `wire` (serialize each message's
    `content` — including list-shaped content blocks — and search the joined text).

- **Half (b)** — `test_ac4b_claim_reachable_via_search_memory_tool`:
  - Same seeded claim (or a fresh one, deterministic embedder).
  - Call the **real tool executor**, `search_memory_executor`, with `owner_service` installed as the
    app's memory service (mirrors `tests/test_tools/test_memory_search.py`'s `_fake_app_module` +
    `patch.dict(sys.modules, ...)` pattern) and a real `TraceContext(trace_id=..., user_id=_OWNER_UID,
    authenticated=True)`.
  - Call with `query_text` matching the claim's content.
  - Assert the claim's `content` **is** present in `output["claims"]`, and that the shape is
    claim-specific (`claim_id` key present) — i.e. not an entity/turn row.

- One more regression guard, `test_ac4_fails_if_claims_query_returns_nothing` — a smoke test that
  documents what AC-4(b) rejects (vacuity): assert `query_claims` on a graph with **no** claims for
  the user returns `[]`, so the positive-path test above is not trivially satisfied by an always-empty
  implementation (this is the "positive companion" ADR-0126 D7/AC-4 asks for, made explicit rather
  than only implicit in half (b)'s assertion).

## Step 5 — Quality gates

```bash
make test-file FILE=tests/personal_agent/memory/test_query_claims.py
make test-file FILE=tests/test_tools/test_memory_search.py
make test-infra-up   # only if not already running, for the integration test
PERSONAL_AGENT_INTEGRATION=1 make test-file FILE=tests/personal_agent/memory/test_adr_0126_claims_pull.py
make test            # full unit suite
make mypy
make ruff-check
make ruff-format
pre-commit run --all-files
```

## Step 6 — Self-review

`src/` logic change (new Cypher, new method, tool wiring) touching memory substrate → **high** effort
code-review, plus security-review (new Cypher query with a parameterized `user_id` — injection surface,
though parameterized so low actual risk; still in scope since it touches auth/scoping).

## Open questions resolved during scoping (no ask needed)

- **No vector index**: per ADR's own guidance, decided on measurement — 91 rows, in-Python scan
  mirrors the existing supersession-matching pattern exactly. Revisit only if claim count grows by an
  order of magnitude.
- **`authenticated` gate**: chosen to require `authenticated=True` in addition to `user_id` being
  present, even though Claims carry no visibility property. Rationale: Claims are personal facts
  scoped by a specific Person's `user_id`; without a verified identity there is no basis to trust the
  caller-supplied `user_id`, so requiring both fail-closed is the safer default and costs nothing (no
  legitimate caller is authenticated=False with a real user_id it's entitled to use).
- **No `MemoryAccessedEvent` publish**: existing publishers are gated on `settings.freshness_enabled`
  and drive Entity `last_accessed_at` staleness tracking — a mechanism Claims don't participate in.
  Adding it here would be scope creep with no consumer; left out.
