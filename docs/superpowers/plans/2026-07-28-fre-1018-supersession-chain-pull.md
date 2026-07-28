# FRE-1018 — ADR-0126 T4: Supersession chain on pull

**Ticket:** FRE-1018 (Approved, Tier-2:Sonnet, stream:build2, context:keep — direct follow-on to
FRE-1016/T3, same files/substrate).
**ADR:** ADR-0126 D5 (pull half) — supersession chain reachable on demand through `search_memory`,
never pushed. Current-only remains the default (T3 already ships this for Claims; Stance push is
T1, not yet built).
**Acceptance criterion carried:** AC-5, **chain-on-pull half only** (the push half — current
present, superseded absent on a sorbet-topic turn — belongs to T1 and is proven there; T1 hasn't
shipped in this codebase yet, and this ticket does not depend on it).
**Blocks:** FRE-1019 (T5 SEAM — owns AC-8, the fourth mutation row: "Stance supersession-chain
retrieval from search_memory").
**Blocked by:** T3 (FRE-1016) — merged (PR #727).

## Scope (from ticket + ADR-0126 D5)

- A retrieval path, reachable through `search_memory`, returning a supersession chain **on
  demand**: the current item, every superseded original retained for history, and the link
  connecting them. Covers **both** Stance and Claim chains — the ticket is explicit that AC-5 only
  exercises the Stance case (the live fixture, Sorbet), but "covers both" is the stated objective.
- Retention/storage unchanged — surfacing only. No changes to `assert_claim`/`assert_stance`.
- AC-5 (this ticket's half): using the live superseded pair on **Sorbet** (`"prefers"` →
  `"prefers a sorbet-leaning texture"`), request the chain through the pull path and assert it
  returns both entries plus the supersession link. Fails if the pull path cannot return the
  superseded original.
- Separate consumer from T3 for the SEAM (AC-8 row 4): removing T3's claim-retrieval path must NOT
  make this ticket's assertion fail, and vice versa — they must be independently removable.

## Design decisions (made explicit here, since the ADR doesn't specify tool-level mechanics)

1. **New tool parameter, not a new tool.** `search_memory` gains `include_history: bool = False`.
   Default `False` reproduces T3's behaviour exactly (current-only) — matches D5's "on demand"
   framing; the caller must explicitly ask for history, mirroring why Claims are pull rather than
   push at all (D4's same asymmetry, one level down, per the ticket text).

2. **Stance chain has no stored "reason."** `assert_stance` (service.py:2309) sets `valid_to`/
   `invalid_at` on the prior edge but writes no `superseded_by`/`supersession_reason` — unlike
   Claims, the `Stance` Pydantic model carries no reason concept at all. AC-5's Check text asks
   only for "both entries plus the supersession link," not a reason string, so the link is
   structural: both entries share the same `(owner)-[:HAS_STANCE]->(target)` pair, ordered by
   `valid_from`, with an `is_current` flag — no write-path change, no schema addition. (Claims DO
   carry `superseded_by`/`supersession_reason` already, written by `assert_claim` — the claim-chain
   method below returns them since they already exist on the node.)

3. **Stance chain is not user-scoped.** `assert_stance` resolves the owner via the `is_owner: true`
   sentinel, not `user_id` (ADR-0107 §3 — "a Stance is the harness owner's worldview... not a
   per-User fact"). `query_stance_history` mirrors this: no `user_id` parameter. It still requires
   `authenticated: bool = True` as a fail-closed gate (a deliberate choice, not inherited from
   `assert_stance` which takes no such parameter) — Stance is still personal-preference data about
   the owner, and T3 set the precedent of requiring verified identity for any new personal-data
   pull surface this ADR adds. Flagged for codex review as a judgment call, not a given.

4. **Claim chain reuses T3's ranking, then walks `superseded_by` backward in Python.** Fetches ALL
   of the user's claims (current + superseded — a superset of T3's current-only fetch, still
   bounded at ~91-claim scale), ranks the CURRENT ones by cosine similarity to `query_text` (same
   method as `query_claims`), picks the best-matching current claim as the chain's head, then walks
   backward via `superseded_by` pointers (built as a Python reverse-lookup: `predecessor_of[new_id]
   = old_id`) until no predecessor remains. No recursive Cypher — matches the ADR's own guidance
   that a vector index/complex query is unneeded at this scale; the supersession scan is already an
   established in-Python pattern (`memory/supersession.py`).

## Files touched

1. `src/personal_agent/memory/service.py` — two new methods: `query_stance_history`,
   `query_claims_history` (placed after T3's `query_claims`, before `ensure_vector_index`).
2. `src/personal_agent/tools/memory_search.py` — new `include_history` tool parameter; wiring in
   `search_memory_executor` (additive — `output["claims"]` from T3 is untouched; new
   `output["stance_history"]` / `output["claims_history"]` keys appear only when
   `include_history=True`).
3. Tests (new/extended, listed in Step 4).

## Step 1 — `MemoryService.query_stance_history`

```python
async def query_stance_history(
    self,
    target: str,
    *,
    authenticated: bool = True,
    trace_id: str | None = None,
) -> list[dict[str, Any]]:
    """Full Stance supersession chain for one target, current + superseded (ADR-0126 D5).

    Pull-only: the chain is reachable on demand through search_memory, never pushed
    (D5's pull half). A Stance is the harness owner's worldview toward World
    knowledge (ADR-0098 D2/D3, ADR-0107 §3) — a single is_owner sentinel, not
    per-user, so no user_id scoping applies (mirrors assert_stance). Still gated on
    ``authenticated`` as a fail-closed default for personal-preference data (a
    deliberate choice, not inherited from assert_stance, which takes no such
    parameter).

    Stance carries no stored "reason" for a supersession (unlike Claims) — the
    link between entries is structural: both share the same (owner)-[:HAS_STANCE]->
    (target) pair, ordered by valid_from, with is_current distinguishing them.

    Args:
        target: The World Entity name the stance chain is about (e.g. "Sorbet").
        authenticated: Whether the request carries a verified identity. False
            returns [].
        trace_id: Request trace id for log correlation.

    Returns:
        Every HAS_STANCE edge from the owner to ``target``, oldest first, each a
        dict with ``target``, ``affect``, ``mastery``, ``observed_at``,
        ``valid_from``, ``valid_to``, ``invalid_at``, ``is_current``. Empty list
        if the owner has no stance toward ``target``, or on any guard failure.
    """
    if not self.connected or not self.driver:
        return []
    if not authenticated:
        return []
    if not target or not target.strip():
        return []

    try:
        async with self.driver.session() as db_session:
            result = await db_session.run(
                "MATCH (:Person {is_owner: true})-[s:HAS_STANCE]->(:Entity {name: $target})\n"
                "RETURN s.affect AS affect, s.mastery AS mastery, s.observed_at AS observed_at,\n"
                "       s.valid_from AS valid_from, s.valid_to AS valid_to,\n"
                "       s.invalid_at AS invalid_at\n"
                "ORDER BY s.valid_from ASC",
                target=target,
            )
            chain: list[dict[str, Any]] = []
            async for row in result:
                chain.append(
                    {
                        "target": target,
                        "affect": row["affect"] or "",
                        "mastery": row["mastery"],
                        "observed_at": row["observed_at"],
                        "valid_from": row["valid_from"],
                        "valid_to": row["valid_to"],
                        "invalid_at": row["invalid_at"],
                        "is_current": row["valid_to"] is None,
                    }
                )
    except Exception as e:
        log.warning("query_stance_history_failed", target=target, error=str(e), trace_id=trace_id)
        return []

    log.info(
        "stance_history_queried", target=target, chain_length=len(chain), trace_id=trace_id
    )
    return chain
```

## Step 2 — `MemoryService.query_claims_history`

```python
async def query_claims_history(
    self,
    query_text: str,
    *,
    user_id: UUID | None,
    authenticated: bool,
    trace_id: str | None = None,
    session_id: str | None = None,
) -> list[dict[str, Any]]:
    """Full Claim supersession chain for the best-matching fact-slot (ADR-0126 D5).

    Pull-only, on demand: finds the current claim best matching query_text (same
    ranking as query_claims), then walks its history backward through
    superseded_by to return the whole chain — current plus every superseded
    original, each carrying the supersession link/reason already written by
    assert_claim. Never pushed. Same identity gating as query_claims — Claims are
    per-user personal facts (ADR-0107 §2), unlike Stance.

    Args:
        query_text: Free-text query to match against the CURRENT claim's content.
        user_id: The acting authenticated user's UUID. None returns [].
        authenticated: Whether the request carries a verified identity. False
            returns [].
        trace_id: Request trace id for log correlation and embedding cost
            attribution.
        session_id: Session id for embedding cost attribution.

    Returns:
        The chain for the best-matching fact-slot, oldest first, each a dict with
        ``claim_id``, ``content``, ``confidence``, ``observed_at``, ``valid_to``,
        ``invalid_at``, ``superseded_by``, ``supersession_reason``, ``is_current``.
        Empty list if no current claim matches, or on any guard failure.
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
        return []

    try:
        async with self.driver.session() as db_session:
            result = await db_session.run(
                "MATCH (:Person {user_id: $user_id})-[:HAS_FACT]->(cl:Claim)\n"
                "RETURN cl.claim_id AS claim_id, cl.content AS content,\n"
                "       cl.confidence AS confidence, cl.observed_at AS observed_at,\n"
                "       cl.valid_to AS valid_to, cl.invalid_at AS invalid_at,\n"
                "       cl.superseded_by AS superseded_by,\n"
                "       cl.supersession_reason AS supersession_reason,\n"
                "       cl.embedding AS embedding",
                user_id=str(user_id),
            )
            rows: dict[str, dict[str, Any]] = {}
            # Codex finding #1: rank ALL claims (current + superseded), not just current —
            # the query may best-match a superseded ancestor whose current descendant has
            # drifted semantically; scoring only current rows would miss that chain entirely.
            candidates: list[tuple[float, str]] = []
            async for row in result:
                claim_id = row["claim_id"]
                if claim_id is None or row["embedding"] is None:
                    continue
                rows[claim_id] = dict(row)
                score = cosine_similarity(query_embedding, list(row["embedding"]))
                candidates.append((score, claim_id))
    except Exception as e:
        log.warning("query_claims_history_failed", error=str(e), trace_id=trace_id)
        return []

    if not candidates:
        return []
    candidates.sort(key=lambda pair: pair[0], reverse=True)
    _, best_match_id = candidates[0]

    # Walk FORWARD to the chain's current head, whichever generation matched best. Guards
    # against a dangling superseded_by pointer (caught by TDD, not the original design: an
    # unguarded rows[head_id] dereference on a missing next-id raised KeyError uncaught).
    head_id = best_match_id
    seen_forward: set[str] = set()
    while head_id not in seen_forward:
        seen_forward.add(head_id)
        next_id = rows[head_id].get("superseded_by")
        if not next_id or next_id not in rows:
            break
        head_id = next_id

    # Codex finding #2 (bug fix): assert_claim can supersede MULTIPLE current claims in one
    # write (facet-aware matching invalidates every matching current claim, all stamped with
    # the SAME new superseded_by). A single-valued predecessor map silently drops all but one
    # ancestor on key collision — predecessors_of must be list-valued (fan-in aware), and the
    # walk must be a BFS/DFS collecting every ancestor transitively, not a linear walk assuming
    # exactly one predecessor per step.
    predecessors_of: dict[str, list[str]] = {}
    for cid, r in rows.items():
        sup_by = r.get("superseded_by")
        if sup_by:
            predecessors_of.setdefault(sup_by, []).append(cid)

    chain_ids: list[str] = []
    seen: set[str] = set()
    frontier = [head_id]
    while frontier:
        next_frontier: list[str] = []
        for cid in frontier:
            if cid in seen:
                continue
            seen.add(cid)
            chain_ids.append(cid)
            next_frontier.extend(predecessors_of.get(cid, []))
        frontier = next_frontier

    chain = [
        {
            "claim_id": cid,
            "content": rows[cid]["content"] or "",
            "confidence": float(rows[cid]["confidence"] or 0.0),
            "observed_at": rows[cid]["observed_at"],
            "valid_to": rows[cid]["valid_to"],
            "invalid_at": rows[cid]["invalid_at"],
            "superseded_by": rows[cid]["superseded_by"],
            "supersession_reason": rows[cid]["supersession_reason"],
            "is_current": rows[cid]["valid_to"] is None,
        }
        for cid in chain_ids
    ]
    chain.sort(key=lambda c: c["observed_at"])  # oldest first — a DAG has no single linear order
    log.info(
        "claims_history_queried",
        head_claim_id=head_id,
        chain_length=len(chain),
        trace_id=trace_id,
    )
    return chain
```

**Trace confirming the backward walk is correct (codex-verified):** `assert_claim` sets
`old.superseded_by = $claim_id` (the NEW claim's id) on the row it invalidates. For a 3-claim chain
A→B→C (C current): `predecessors_of = {B_id: [A_id], C_id: [B_id]}`. BFS from `head_id=C_id`:
frontier `[C]` → append C, next frontier `[B]` → append B, next frontier `[A]` → append A, next
frontier `[]`. `chain_ids = [C, B, A]`, sorted by `observed_at` ascending → `[A, B, C]` oldest-first.
Matches the claimed behaviour.

## Step 3 — Wire into `search_memory_executor`

- Add `include_history` to `search_memory_tool.parameters` (boolean, default `False`, description
  along the lines of: "When true, also return the full supersession history — every superseded
  prior version plus the current one — for matched stances and claims, instead of only the current
  value. Use when the user asks how something changed over time.").
- In `search_memory_executor`, after the existing `output["claims"] = await
  memory_service.query_claims(...)` line (from T3), add:

```python
if include_history:
    # Codex finding #3: reuse the same effective-name resolution the entity-match branch
    # already applies (entity_names, or capitalised words extracted from query_text) — an
    # explicit entity_names=["Sorbet"] must not be the only way to reach Sorbet's history
    # when the caller just asked query_text="Sorbet".
    effective_entity_names = entity_names or _extract_keywords(query_text)
    stance_history: dict[str, list[dict[str, Any]]] = {}
    for name in effective_entity_names:
        chain = await memory_service.query_stance_history(
            name,
            authenticated=getattr(ctx, "authenticated", False),
            trace_id=trace_id,
        )
        if chain:
            stance_history[name] = chain
    output["stance_history"] = stance_history

    output["claims_history"] = await memory_service.query_claims_history(
        query_text,
        user_id=getattr(ctx, "user_id", None),
        authenticated=getattr(ctx, "authenticated", False),
        trace_id=trace_id,
        session_id=getattr(ctx, "session_id", None),
    )
```

- New `search_memory_executor` parameter: `include_history: bool = False`.

## Step 4 — Tests

### 4a. Unit tests — `tests/personal_agent/memory/test_query_stance_history.py` (new, fake driver)
- `authenticated=False` → `[]`, no Cypher run.
- Blank/whitespace target → `[]`, no Cypher run.
- Not connected → `[]`.
- Two rows (superseded + current) → returned oldest-first, `is_current` correct on each.
- DB error caught → `[]`.

### 4b. Unit tests — `tests/personal_agent/memory/test_query_claims_history.py` (new, fake driver)
- `user_id=None` / `authenticated=False` / blank query / zero-vector embedder → `[]`, mirroring T3's
  guards.
- Three-claim chain (`A` superseded by `B` superseded by `C`, `C` current) with the query embedding
  closest to `C` → chain returned as `[A, B, C]` (oldest-first), each carrying `superseded_by`
  pointing to the next, `is_current` True only on `C`.
- **Same 3-claim chain, but the query embedding is closest to `A` (the oldest, superseded row)**
  → still returns the FULL `[A, B, C]` chain (codex finding #1 regression guard: candidate ranking
  must not be restricted to current-only, and must walk forward to the chain's current head before
  collecting ancestors).
- **Fan-in chain: `X` and `Y` are both superseded by the same `Z`** (mirrors `assert_claim`'s
  facet-aware matching invalidating multiple current claims in one write) → the chain includes
  BOTH `X` and `Y` as ancestors of `Z`, not just one (codex finding #2 regression guard — the exact
  bug the original single-valued `predecessor_of` dict would have silently dropped one of).
- No claim matches at all (empty graph) → `[]`.
- Claim with `superseded_by` pointing to a row not present in the fetched set (defensive: a
  concurrent write mid-query) → walk stops there rather than raising.

### 4c. Unit tests — extend `tests/test_tools/test_memory_search.py`
- `include_history=False` (default): `stance_history`/`claims_history` keys absent — T3's behaviour
  is unchanged.
- `include_history=True` with `entity_names=["Sorbet"]`: `query_stance_history` called with
  `"Sorbet"`; `output["stance_history"]["Sorbet"]` present.
- **`include_history=True` with `entity_names` empty but `query_text="Sorbet"`**:
  `query_stance_history` still called with `"Sorbet"` (codex finding #3 regression guard — the
  capitalised-word extraction path must feed the history lookup, not just the explicit-names path).
- `include_history=True`: `query_claims_history` called with the same
  identity/trace/session threading as `query_claims`.

### 4d. Live behavioural proof — `tests/personal_agent/memory/test_adr_0126_supersession_chain.py`
(new, `@pytest.mark.integration`, live test Neo4j — mirrors T3's `test_adr_0126_claims_pull.py`)

This is the AC-5 (chain-on-pull half) proof cited in the Linear handoff comment:

- `test_ac5_sorbet_stance_chain_reachable_via_search_memory_tool`:
  - Seed the live fixture named in the ADR/ticket: `assert_stance(target="Sorbet", affect="prefers
    it", ...)` then `assert_stance(target="Sorbet", affect="prefers a sorbet-leaning texture",
    ...)` (second call supersedes the first, per `assert_stance`'s unconditional-supersede
    semantics — no embedder patch needed here, `assert_stance` doesn't compute embeddings).
  - Call the real `search_memory_executor` with `entity_names=["Sorbet"]`, `include_history=True`,
    and a real authenticated `TraceContext`.
  - Assert `output["stance_history"]["Sorbet"]` has length 2, contains both `"prefers it"` and
    `"prefers a sorbet-leaning texture"`, and exactly one entry has `is_current=True` (the specific
    one) — i.e. both entries plus the link (structural: same target, ordered, one current one not).
- `test_ac5_positive_and_negative_together`: on the SAME fixture, assert the tool call WITHOUT
  `include_history` (T3/default behaviour) does not expose `stance_history` at all — guards against
  a degenerate implementation that always returns history regardless of the flag (D5's "on demand"
  requirement, not "always").

## Step 5 — Quality gates

```bash
make test-file FILE=tests/personal_agent/memory/test_query_stance_history.py
make test-file FILE=tests/personal_agent/memory/test_query_claims_history.py
make test-file FILE=tests/test_tools/test_memory_search.py
PERSONAL_AGENT_INTEGRATION=1 make test-file FILE=tests/personal_agent/memory/test_adr_0126_supersession_chain.py
make test
make mypy
make ruff-check
make ruff-format
pre-commit run --all-files
```

## Step 6 — Self-review

Standard-tier: new `src/` logic touching the memory substrate → codex plan-review (this document),
then high-effort code-review + security-review before PR (mirrors T3's process).

## Codex plan-review findings (2026-07-28) — folded in below

Two review runs: the first hung for 90+ minutes (killed; see
`feedback_codex_rescue_hangs_check_and_kill` memory) and was retried fresh. The retry confirmed
`authenticated=True` gating and the FRE-1019 seam split are sound, and confirmed the
`predecessor_of` direction/trace is correct as designed — but surfaced three real gaps, all folded
into Step 2 below:

1. **Current-only candidate ranking is a real gap.** If `query_text` best matches a SUPERSEDED
   claim whose current successor has semantically drifted, the original design never selects that
   chain at all (only current rows were scored). **Fix:** rank ALL claims (current + superseded) by
   similarity; whichever row wins, walk FORWARD via `superseded_by` to find its chain's current head
   before doing the backward ancestor collection — so the chain is found regardless of which
   generation the query happens to match best.

2. **Critical bug: `assert_claim` can supersede MULTIPLE current claims in one write** (facet-aware
   matching returns every matching current claim as `supersede_ids`, all stamped with the SAME new
   `superseded_by = $claim_id` — service.py `matching_candidates`/`supersede_ids` in `assert_claim`).
   The original design's `predecessor_of: dict[str, str]` (single value per successor) silently
   drops all but one ancestor on key collision when two old claims share one `superseded_by`
   target. **Fix:** `predecessors_of: dict[str, list[str]]` (fan-in aware), and a BFS/DFS backward
   walk collecting the FULL ancestor set transitively, not a linear walk assuming exactly one
   predecessor per step. Chain is sorted by `observed_at` ascending after collection (a DAG has no
   single "the" linear order — ascending `observed_at` is the deterministic display order).

3. **Stance history only fired off explicit `entity_names`, not `query_text`-derived names.**
   `search_memory(query_text="Sorbet", include_history=True)` without an explicit
   `entity_names=["Sorbet"]` would silently return no stance history, even though the ordinary
   entity-match path already resolves capitalised words out of `query_text` via
   `_extract_keywords` when `entity_names` is empty. The original AC-5 test hard-coded
   `entity_names=["Sorbet"]` and would not have caught this. **Fix:** the stance-history loop uses
   `entity_names or _extract_keywords(query_text)` — the same effective-name resolution the
   entity-match branch already uses — and a new test proves `query_text`-only reachability.

## Post-implementation fixes (TDD + security-review, 2026-07-28)

- **TDD caught a bug the plan missed**: the forward-walk-to-current-head loop dereferenced
  `rows[head_id]` unconditionally after following a `superseded_by` pointer, so a dangling pointer
  (naming a claim_id absent from the fetched set) raised an uncaught `KeyError`. Fixed by checking
  `next_id in rows` before following it; regression test
  `test_dangling_superseded_by_pointer_does_not_raise` added.
- **security-review sub-agent found no exploitable vulnerabilities** (Cypher fully parameterized;
  `query_claims_history`'s `user_id` scoping verified to prevent cross-user reads; no
  claim/affect content or PII logged). One hardening recommendation adopted: dropped
  `query_stance_history`'s `authenticated: bool = True` default in favour of a required keyword
  (matching `query_claims`/`query_claims_history`'s no-default pattern) — no live call site relied
  on the default, so this is a zero-risk tightening against a future caller silently inheriting a
  permissive default.
