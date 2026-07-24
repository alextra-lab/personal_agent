# FRE-948 — ADR-0124 Phase 1: session-browser surface for label + digest (AC-15)

**Backing:** ADR-0124 §D4 Phase 1, Amendment B. Blocked-by FRE-947 (Phase 0, **Done** — verified via
`get_issue`, merged PR #636, deployed). Blocks FRE-949 (Phase 2a).

**Branch:** `fre-948-adr-0124-phase1-session-browser-digest`

**Revision note:** this plan was adversarially reviewed by codex plan-review before implementation
(build skill Step 3 — Standard tier). Confirmed findings are folded in below; each is marked
`[codex]`. One codex claim was checked against the repo and found wrong (no existing PWA test
harness for `SessionList`) — corrected, noted where relevant.

## Scope (from the ticket + ADR §D4)

Surface `session_label` and `session_digest` — written to Neo4j by the Phase-0 producer
(`second_brain/session_summary.py`, `MemoryService.write_session_digest`) — into the
Postgres-backed session endpoints, and render them in the PWA's session browser. Not in the
recall path; no injection surface (D4).

**Known constraint (ticket + ADR §"Two claims that do not hold"):** this is a genuine
cross-substrate read. `gateway/session_api.py:list_sessions` reads Postgres only
(`SessionRepository.list_recent`); the digest lives on the Neo4j `:Session` node. Must degrade
explicitly — never break the list — when the graph is disabled, unreachable, slow, or a session
has no digest yet (single-turn floor, or a generation that hasn't run/failed).

## Acceptance criterion (verbatim, ADR-0124 line 1066)

> **AC-15** — The surface works end to end. **Check:** the session list renders label and digest
> for a session whose digest lives in Neo4j while its row lives in Postgres; a session with no
> digest (single-turn, or failed) renders without error and without a stale or placeholder digest;
> the generated label replaces the first-60-characters title hack. **Fails if** the cross-substrate
> read fails, a missing digest breaks or fabricates the row, or the old title hack still shows.

**How the two fail-conditions coexist** `[codex]`: "fails if the cross-substrate read fails" cannot
mean a graph outage must break the endpoint — the ADR's own D4/§"Known constraint" requires
graceful degradation. Read as two distinct things to prove: (a) the **normal** cross-substrate path
demonstrably works end to end (a real digest, written by the real producer shape, read back and
rendered correctly) — proven by the live test in Step 9, not by mocks alone; (b) an **operational**
graph failure (unavailable, errors, or hangs) must not break the list — proven by the degradation
tests in Step 8.

## What already exists (verified by reading source, not assumed)

- `memory/session_digest.py` already has everything needed to go from the stored JSON blob to
  displayable prose: `SessionDigest` (Pydantic, frozen), and **`render_digest(digest) -> str`**
  (`session_digest.py:340-371`) — "dense labelled prose... derived, never stored." No new rendering
  logic needed; call this function.
- `memory/service.py:write_session_digest` (1180-1288) stores `session_label` (plain string) and
  `session_digest` (an `orjson`-serialized JSON string of `SessionDigest.model_dump(mode="json")`)
  as `:Session` node properties. A session with no digest yet simply lacks these properties
  (never set by `create_session` — deliberately, to avoid the clobber bug); reading them back
  yields `None`.
- **No read method exists yet** for pulling `session_label`/`session_digest` back out of Neo4j —
  confirmed by grep, zero callers of `orjson.loads`/`SessionDigest.model_validate` from a graph
  read anywhere in the repo. This ticket adds the first one.
- The house pattern for a Neo4j read in `memory/service.py` (`get_person_location`,
  `service.py:2778-2835`): `if not self.connected or not self.driver: log.warning(...); return
  <falsy>` → `try/except Exception: log.error(...); return <falsy>` → success logs `info`. Never
  raises out of the method.
- The house pattern for gateway-side graceful degradation on a secondary-store read
  (`gateway/session_api.py:_fetch_selections`, 449-489, and its test
  `test_get_session_config_selection_store_failure_logs_trace_id`,
  `tests/personal_agent/gateway/test_session_api.py:875-905`): catch broadly, `log.warning(...,
  trace_id=ctx.trace_id)`, return the empty/default value, **response stays 200**.
- `gateway/app.py:_KnowledgeGraphAdapter` (202-337) is the existing seam between gateway code and
  `MemoryService` — each gateway-visible capability gets one thin delegating method here.
  `app.state.knowledge_graph` is `None` when Neo4j is disabled (`app.py:160`) —
  `session_api.py` currently never touches it at all.
- **No Neo4j index/constraint exists anywhere in the repo on `:Session(session_id)`** `[codex]` —
  verified by grep across `src/` and `docker/` (only Postgres migrations have `CREATE INDEX`; the
  house Neo4j idempotent-index pattern, `ensure_entity_class_index()`/`ensure_fulltext_index()` at
  `service.py:2401-2420` and `2370-2399`, has no `Session` counterpart). Every existing
  `session_id`-keyed Cypher query (`write_session_digest`, `create_session`,
  `mark_session_projection_clean`) already relies on a label scan today — a pre-existing,
  currently-small gap (121 sessions, per the ADR's measurements) affecting the whole `:Session`
  label, not something this ticket introduces. It is being made **user-facing and unconditional**
  by this ticket (a browser opening the session drawer now touches Neo4j on every page load, where
  before it never did), so this plan closes it rather than deferring it — see Step 2.
- No driver-level or per-call timeout exists on any Neo4j read in the codebase today (`driver =
  Neo4jAsyncGraphDatabase.driver(uri, auth=(user, password))`, `service.py:648`, no timeout kwargs;
  no `asyncio.wait_for`/`asyncio.timeout` usage anywhere in `memory/service.py` or `gateway/`).
  `[codex]` This is acceptable for the existing write/read paths (called from a background sweep or
  from a turn already paying for an LLM round trip), but `GET /api/v1/sessions` was previously
  **Neo4j-independent** — this ticket is what couples it to Neo4j's availability for the first
  time, and a hung (not merely erroring) connection attempt would stall a previously
  Postgres-only endpoint. Bounding this new call site specifically (not retrofitting every existing
  Neo4j read) is the surgical fix — see Step 4.
- PWA: `SessionList.tsx` renders only `s.title` today (no digest anywhere in `seshat-pwa/src`).
  `SessionSummary` (`agui-client.ts:559-578`) has no label/digest fields.
- `seshat-pwa/src/__tests__/SessionList.test.tsx` **already exists** (turn-count/EVAL-badge/loading
  tests) — an earlier draft of this plan wrongly claimed no test harness targets this component;
  corrected. New tests extend this file, matching its existing `vi.mock`/`SESSION_BASE` pattern.

## Plan

### Backend — memory layer

**1. `src/personal_agent/memory/session_digest.py`** — add a small frozen view model next to the
existing schema (co-located, since it's this module's read-time projection):

```python
class SessionDigestView(BaseModel):
    """Display-ready projection of one session's label + rendered digest (ADR-0124 Phase 1).

    Read-time only — never stored. ``digest_text`` is already run through
    :func:`render_digest`; a consumer needs no further parsing. ``label`` and
    ``digest_text`` are independent: a malformed stored digest must not suppress
    a perfectly valid label (they are written independently by
    ``write_session_digest``, so they must be read back independently too).
    """

    model_config = ConfigDict(frozen=True)

    label: str | None = None
    digest_text: str | None = None
```

**2. `src/personal_agent/memory/service.py`** — two additions:

**2a. `ensure_session_id_index()`** `[codex]`, mirroring `ensure_entity_class_index()` exactly
(same idempotent `IF NOT EXISTS` shape, same `if not self.connected or not self.driver: return
False` / `try/except: log.error(...); return False` structure):

```python
async def ensure_session_id_index(self) -> bool:
    """Create the Session.session_id index (ADR-0124 Phase 1 read path).

    Idempotent (IF NOT EXISTS). Mirrors ensure_entity_class_index()'s pattern. Every
    session_id-keyed Cypher query (write_session_digest, create_session, and this
    phase's batched digest read) already relies on this lookup; it is made
    user-facing and unconditional by the session-browser read, which is why it is
    added now rather than left as a background label scan.

    Returns:
        True if the index exists or was created successfully.
    """
```
Cypher: `CREATE INDEX session_id_index IF NOT EXISTS FOR (s:Session) ON (s.session_id)`.

**2b. `get_session_digest_views`** — batched read, modeled on `get_person_location`'s three-tier
shape (batched — one query for a whole page, not N+1 round trips) but with **label and digest
parsed independently** `[codex — confirmed]`:

```python
async def get_session_digest_views(
    self, session_ids: Sequence[str], *, trace_id: str | None = None
) -> dict[str, SessionDigestView]:
    """Batch-read display-ready label + rendered digest for a page of sessions.

    One query for the whole page (ADR-0124 Phase 1) rather than a round trip per
    session. A session absent from the graph, never digested, or whose stored
    digest fails to parse is simply missing from the returned mapping — the
    caller treats an absent id exactly like "no digest yet." A malformed stored
    digest never suppresses a valid label: they were written independently
    (write_session_digest sets both in one statement, but as two separate
    properties) and are parsed independently here. Never raises.

    Args:
        session_ids: Postgres session ids for the current page.
        trace_id: Trace identifier for log correlation (ADR-0074 §I3).

    Returns:
        ``{session_id: SessionDigestView}`` for sessions with a usable label
        and/or a well-formed digest. Empty when Neo4j is unavailable, no id
        matches, the query fails, or ``session_ids`` is empty.
    """
```
Implementation:
- Empty `session_ids` → `{}` immediately (no round trip). Dedupe the input list before querying
  (harmless if the caller never sends duplicates, cheap to guarantee).
- Not connected → `log.warning("session_digest_view_read_unavailable", ...)`, `{}`.
- Query: `MATCH (s:Session) WHERE s.session_id IN $session_ids RETURN s.session_id AS
  session_id, s.session_label AS session_label, s.session_digest AS session_digest`, via
  `result.data()` (matches `find_dirty_idle_sessions`'s style, not `.single()`). `$session_ids` is
  parameter-bound — no injection concern; confirmed no different by codex review.
- Query raises (includes a row-fetch failure inside the `async with` block, not only
  `driver.session()` itself — test both) → `log.error("session_digest_view_read_failed",
  exc_info=True, ...)`, `{}`.
- Per row:
  - `session_id`: if missing or not a non-empty string, skip the row entirely (log at `warning`,
    include whatever raw value was present) — a malformed key can't be used to key the returned
    dict.
  - `label`: the raw `session_label` value, treated as absent unless it is a non-empty,
    non-whitespace-only string (a stray empty-string property must not display as a label).
  - `digest_text`: **only when `session_digest` is not `None`**, parse in its own nested
    try/except — `orjson.loads` then `SessionDigest.model_validate(...)` then `render_digest(...)`;
    an empty rendered string (all-slots-empty digest) is `None`. On any exception here
    (`orjson` decode error, Pydantic `ValidationError`, or anything else `render_digest` could
    raise), log `session_digest_view_parse_failed` (session_id, error, trace_id) and set
    `digest_text = None` for **this row only** — **the label computed above is unaffected and is
    still included** `[codex — this is the fix for the confirmed high-severity finding]`.
  - Add an entry to the returned dict only when at least one of `label`/`digest_text` ends up
    non-`None` — an entry that's `SessionDigestView(None, None)` is indistinguishable from absence.
- Import `render_digest` and `SessionDigestView` alongside the existing `SessionDigest` /
  `TERMINAL_ELIGIBLE_REASONS` import block (`service.py:50-53`).

**2c.** `src/personal_agent/service/app.py` (~line 697, right after the existing
`ensure_entity_class_index()` startup call) and `src/personal_agent/gateway/app.py` (in
`_gateway_lifespan`, right after `memory_service.connect()` succeeds, ~app.py:166-168) both gain:
```python
try:
    await memory_service.ensure_session_id_index()
    log.info("neo4j_session_id_index_ensured")
except Exception as idx_e:
    log.warning("neo4j_session_id_index_setup_failed", error=str(idx_e))
```
— covers both the combined local-mode startup (`service/app.py`, where the gateway router is
mounted onto the same app) and the standalone-gateway cloud startup (`gateway/app.py`), so the
index exists regardless of deployment mode.

### Backend — gateway layer

**3. `src/personal_agent/gateway/app.py`** — add one delegating method to
`_KnowledgeGraphAdapter` (202-337), matching its existing per-capability-method convention:

```python
async def get_session_digest_views(
    self, session_ids: Sequence[str], *, trace_id: str | None = None
) -> dict[str, SessionDigestView]:
    """Delegate to MemoryService's batch session-digest read (ADR-0124 Phase 1)."""
    return await self._service.get_session_digest_views(session_ids, trace_id=trace_id)
```
(Import `Sequence` from `collections.abc` and `SessionDigestView` from
`personal_agent.memory.session_digest` at module scope.)

**4. `src/personal_agent/gateway/session_api.py`**:
- Import `SessionDigestView` from `personal_agent.memory.session_digest`, and `asyncio`.
- Module constant: `_DIGEST_HYDRATION_TIMEOUT_SECONDS = 3.0` — `[codex]` bounds how long the
  previously-Neo4j-independent list/get endpoints will wait on this new optional enrichment before
  degrading; a plain module constant (matching the existing `_USER_EXCERPT_CHARS`-style
  local-constant convention noted in the ADR) rather than a new settings knob, since nothing else
  in this file is externally configurable at this granularity and one bespoke config key for a
  single internal timeout would be exactly the unrequested configurability CLAUDE.md warns
  against.
- Add `_fetch_session_digest_views(request, session_ids, *, ctx) -> dict[str, SessionDigestView]`
  right next to `_fetch_selections`, same contract: `knowledge_graph is None` or empty
  `session_ids` → `{}` immediately (mirrors the `es_client is None` skip in
  `_attach_turn_ratings`); otherwise:
  ```python
  try:
      return await asyncio.wait_for(
          kg.get_session_digest_views(session_ids, trace_id=ctx.trace_id),
          timeout=_DIGEST_HYDRATION_TIMEOUT_SECONDS,
      )
  except Exception as exc:  # noqa: BLE001 — availability guard; also catches TimeoutError
      log.warning(
          "session_digest_view_hydration_failed",
          session_ids=session_ids,
          error=str(exc),
          trace_id=ctx.trace_id,
      )
      return {}
  ```
  (`asyncio.TimeoutError`/`TimeoutError` is caught by the same broad `except Exception` — no
  separate branch needed.)
- `_session_to_dict(session: Any, digest_view: SessionDigestView | None = None) -> dict[str, Any]`
  — add two keys to the returned dict: `"session_label": digest_view.label if digest_view else
  None`, `"session_digest": digest_view.digest_text if digest_view else None`. Default arg keeps
  the set-selection endpoint's call site (line 791) unchanged.
- `list_sessions` (99-132): after `sessions = await repo.list_recent(...)`, fetch
  `digest_views = await _fetch_session_digest_views(request, [str(s.session_id) for s in
  sessions], ctx=ctx)` (reuse the `ctx` already constructed for the `gateway_sessions_list` log
  line), then `return [_session_to_dict(s, digest_views.get(str(s.session_id))) for s in
  sessions]`.
- `get_session` (135-230): **also enrich** `[codex — fold-in, addresses the confirmed response
  inconsistency]`. Right after `result = _session_to_dict(session)` (line 189), fetch
  `digest_views = await _fetch_session_digest_views(request, [str(uuid)], ctx=ctx)` and rebuild
  `result` via `_session_to_dict(session, digest_views.get(str(uuid)))` before the rest of the
  function bolts on `context_tokens`/etc. Rationale for folding this in rather than leaving it
  list-only: the batch method already handles a single id at no extra cost, and leaving `get_session`
  emitting a hardcoded `None` while the same session's row in the list carries a real label is an
  internally inconsistent resource representation the codex review flagged directly — same helper,
  same code path, no new logic. The set-selection endpoint (line 791) is left unenriched: its
  purpose is confirming a selection mutation, not display, and enriching it would be reaching for
  symmetry the ticket doesn't need.

### Frontend — PWA

**5. `seshat-pwa/src/lib/agui-client.ts`** (`SessionSummary`, 559-578) — add:
```ts
/** Model-generated label, replacing the first-60-chars title hack when present (ADR-0124 Phase 1). */
session_label: string | null;
/** Rendered digest prose ("Established: …\n\nDecisions: …"), or null with no digest yet. */
session_digest: string | null;
```

**6. `seshat-pwa/src/components/SessionList.tsx`** (80-108):
- Header line 93-100: display `s.session_label ?? s.title ?? '(empty session)'` (label replaces
  the title hack per AC-15; falls back exactly as today when absent).
- Below the existing relative-time/turn-count line, conditionally render the digest when present:
  a `<p>` with `whitespace-pre-line` (the rendered text uses `\n`/`\n\n` as structure) and a
  `line-clamp-3` Tailwind class to keep rows scannable — a presentation choice, not a change to
  the "renders... digest" requirement; full text stays in the DOM. Render nothing when
  `session_digest` is `null` (or the field is entirely absent, for forward/backward compatibility)
  — the "no stale or placeholder digest" half of AC-15.

**7. `seshat-pwa/src/__tests__/SessionList.test.tsx`** `[codex — confirmed gap; file exists,
extend it]` — new `describe('SessionList — label and digest')` block, matching the file's existing
`SESSION_BASE`/`mockListSessions` pattern:
- `renders session_label instead of the title when both are present` — assert the label text is
  in the document and the title text is **not** (`screen.queryByText(...)).not.toBeInTheDocument()`)
  — this is the test that actually proves "label replaces the title hack," which the earlier draft
  of this plan only proved at the API-transport level.
- `falls back to title when session_label is null` — existing behavior, regression guard.
- `renders digest text when present, including multiline content`.
- `renders no digest element when session_digest is null` — asserts absence, not just that no error
  is thrown.
- `does not crash when session_label/session_digest are undefined` (simulates an old cached
  service-worker response shape, or a backend that hasn't deployed yet — the two are independent
  deploys per the ADR's deploy note) — passes `SESSION_BASE` unmodified (no new keys) through
  unchanged and asserts the existing title/turn-count rendering still works.

**8. `seshat-pwa/public/sw.js`** — bump `CACHE_NAME` (currently
`'seshat-v35-session-continuity-fallback'` → next `v36-...`) per the project's PWA-deploy
convention, since the shell's rendered output changes.

### Tests (TDD — write failing first)

**Backend, new file `tests/personal_agent/memory/test_session_digest_read.py`** (sibling to
`test_session_digest_write.py`, which is explicitly scoped to the write path per its own
docstring — a new file matching that scoping is cleaner than growing it). Reuses
`_make_service_with_mock`'s shape but needs `result.data()` mocked, not `.single()`
`[codex — the write-path helper can't be copied unchanged; confirmed]`:
- `test_get_session_digest_views_empty_ids_returns_empty_without_a_query` — `session_ids=[]` never
  calls `driver.session()`.
- `test_get_session_digest_views_not_connected_returns_empty` — `service.connected = False`.
- `test_get_session_digest_views_parses_label_and_renders_digest` — `result.data()` returns one row
  with a real `orjson`-serialized `SessionDigest`; assert the returned view's `digest_text` equals
  `render_digest(that_digest)` and `label` matches.
- `test_get_session_digest_views_label_only_row` / `test_get_session_digest_views_digest_only_row`
  `[codex]` — each independently absent field is handled, not just the "both present" case.
- `test_get_session_digest_views_no_digest_yet_is_absent_from_the_mapping` — row with
  `session_digest=None, session_label=None` → session id not a key in the returned dict.
- `test_get_session_digest_views_malformed_digest_preserves_the_valid_label` `[codex — the
  confirmed high-severity fix, tested directly]` — a row with a real `session_label` string and an
  unparseable `session_digest` string → the returned view has the correct `label` and
  `digest_text=None`; no exception escapes.
- `test_get_session_digest_views_whitespace_only_label_is_treated_as_absent` `[codex]`.
- `test_get_session_digest_views_malformed_session_id_row_is_skipped` `[codex]` — a row with a
  `None`/non-string `session_id` doesn't crash the batch and doesn't appear in the result.
- `test_get_session_digest_views_query_failure_returns_empty` — failure inside `driver.session()`.
- `test_get_session_digest_views_row_fetch_failure_returns_empty` `[codex]` — failure raised by
  `result.data()` itself (not just `driver.session()`), matching the more realistic failure point
  inside the `async with` block.
- `test_ensure_session_id_index_creates_idempotent_index` — asserts the emitted Cypher string
  (mirrors how `test_dirty_scan_includes_the_is_null_disjunct` asserts on `captured[0][0]`).

**Backend, `tests/personal_agent/gateway/test_session_api.py`**:
- `test_list_sessions_includes_label_and_digest_when_graph_has_them` — mock
  `app.state.knowledge_graph` with an async `get_session_digest_views` returning a populated view
  for one of two sessions; assert the response has `session_label`/`session_digest` populated for
  that one and `None` for the other.
- `test_list_sessions_no_digest_yet_renders_without_error` — `knowledge_graph` present but returns
  `{}` (nothing generated yet) → 200, both fields `None`, unchanged from pre-Phase-1 shape.
- `test_list_sessions_knowledge_graph_none_skips_enrichment` — `app.state.knowledge_graph = None`
  (today's default in every existing test) → unchanged 200 response, both new fields `None`. This
  is the regression guard that every pre-existing test in the file keeps passing unmodified.
- `test_list_sessions_digest_hydration_failure_logs_trace_id_and_degrades` — mock
  `get_session_digest_views` with `side_effect=RuntimeError("boom")`; assert 200 (never 500) and
  `structlog.testing.capture_logs()` shows `session_digest_view_hydration_failed` carrying
  `trace_id` — mirrors `test_get_session_config_selection_store_failure_logs_trace_id`.
- `test_list_sessions_digest_hydration_times_out_and_degrades` `[codex — confirmed gap: a
  completed-exception test doesn't cover a hang]` — mock `get_session_digest_views` with a coroutine
  that `await asyncio.sleep(10)` (longer than `_DIGEST_HYDRATION_TIMEOUT_SECONDS`); assert 200 and
  the same degrade-log event within the test's timeout budget.
- `test_get_session_includes_label_and_digest_when_graph_has_them` — the single-session GET path
  now also enriches (Step 4's fold-in).
- `test_get_session_adapter_delegation_is_real` `[codex — closes the "layered mocks" gap]` — build
  the app with a real `_KnowledgeGraphAdapter(fake_memory_service)` (a small stub/`Mock` standing in
  for just `get_session_digest_views`) instead of mocking `app.state.knowledge_graph` directly, so
  the adapter's own delegation method is exercised, not assumed.

**Live, new file `tests/personal_agent/memory/test_session_digest_read_live.py`** `[codex —
addresses "the AC is only covered by layered mocks"; matches the project's established
mock-unit + live-integration split, e.g. `test_entity_class_persistence_live.py`]`. Marked
`pytest.mark.integration`, skips when the test Neo4j (`:7688`) isn't up
(`make test-infra-up`):
- Write a real digest via `write_session_digest` (the actual Phase-0 write path, not a hand-crafted
  node) for a fixture session, then read it back via `get_session_digest_views` and assert the
  returned `digest_text` matches `render_digest` of the original `SessionDigest` — this is the one
  test that proves the full real shape round-trips, not just that each layer's mock agrees with
  itself.
- A session with no digest written is absent from the returned mapping.
- `ensure_session_id_index()` succeeds against the live substrate (mirrors
  `test_ensure_entity_class_index_succeeds`).

**Run:**
```bash
make test-file FILE=tests/personal_agent/memory/test_session_digest_read.py
make test-file FILE=tests/personal_agent/gateway/test_session_api.py
PERSONAL_AGENT_INTEGRATION=1 pytest tests/personal_agent/memory/test_session_digest_read_live.py -m integration  # after make test-infra-up
```
then the full module dirs, then `make test` for the full suite (the live file is `integration`-marked,
out of the default `make test` run, matching `test_entity_class_persistence_live.py`'s convention).

**Frontend:**
```bash
cd seshat-pwa && npx vitest run src/__tests__/SessionList.test.tsx
cd seshat-pwa && npm run lint
```

### Quality gates (Step 8 of the build skill)

`make test` (module then full) · `make mypy` · `make ruff-check` + `make ruff-format` ·
`cd seshat-pwa && npm run lint` · `pre-commit run --all-files`. Self-review (code-review skill,
effort **medium** — the codex plan review raised real correctness/robustness findings, so the
diff is no longer "straightforward read-path addition following house patterns exactly" without
qualification; it now includes a timeout, an index bootstrap touching two startup paths, and a
fold-in into a second endpoint) before opening the PR.

### Deploy (per ticket)

Gateway rebuild (ask-first) plus a PWA rebuild with the `CACHE_NAME` bump (standing-approval class
per lifecycle-rules — PWA-only rebuild is one of the three deploy classes master does without
asking; the gateway leg is ask-first).

### Explicitly out of scope (do not fold in)

- The set-selection endpoint (`session_api.py:791`) stays unenriched — see Step 4's rationale.
- No new Postgres columns, no schema change — digest data stays in Neo4j only, read at request
  time (matches D3: "storage is structured; rendering is derived... no second staleness surface").
- No changes to the Phase-0 producer, `write_session_digest`, or the sweep — this is a pure read
  path addition.
- Not retrofitting a timeout onto every other pre-existing Neo4j read in `memory/service.py` —
  only the new call site that couples a previously Neo4j-independent endpoint to Neo4j gets one;
  the others are called from contexts (a background sweep, a turn already paying for an LLM round
  trip) where the same regression doesn't apply, and bounding all of them is a separate, larger
  piece of work this ticket doesn't need.
