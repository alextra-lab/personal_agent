# FRE-998 — Thread user identity into the graph write path

**Ticket:** FRE-998 (Approved, `stream:build2`, `Tier-1:Opus`, High)
**Backing ADR:** ADR-0107 (User Identity Resolution + Trace/Log Identity Propagation)
**Related:** FRE-738 (T1, Claims), FRE-739 (T2, logs), FRE-740 (T3, joinability probe)
**Date:** 2026-07-28

---

## 1. Live-graph baseline (measured, not assumed)

Queried `cloud-sim-neo4j` read-only before planning. The ticket's premise is **partly stale** and one
central claim is **wrong**; the plan is built on the measurement, not the description.

| Fact | Value |
|---|---|
| Session nodes | 122 (118 carry `user_id`, 4 do not) |
| Turn nodes | 2288 (**0** carry a `user_id` property) |
| `(:Person)-[:PARTICIPATED_IN]->(:Turn)` edges | **370** |
| `:Person` nodes with `user_id` | 5 of 5 |
| Orphan turns (no `CONTAINS` from any Session) | 1828, across 1039 distinct `session_id`s |

**Turn identity is NOT absent — it is edge-shaped, and it already works.** Coverage by month:

| Month | Turns | With `PARTICIPATED_IN` |
|---|---|---|
| 2026-04 | 626 | 0 |
| 2026-05 | 1356 | 64 |
| 2026-06 | 169 | **169 (100%)** |
| 2026-07 | 137 | **137 (100%)** |

Attribution is correct and multi-user: owner 336, plus three non-owner users (21 / 10 / 3) — matching
the four real humans. The ticket's finding of "no identity by any mechanism" for turns came from
enumerating turn **properties** only; relationships were enumerated for *sessions* only (ticket's own
check #2), so the edge was never in the sample. This is the mirror of the trap master already
self-corrected for Claims in the comment thread: checking one shape and generalising.

**What is genuinely broken, confirmed:** `create_session` (`memory/service.py:1177`) writes no identity
at all — no property, no edge. The 118 attributed sessions are entirely master's 2026-07-26 backfill
footprint; the 4 sessions created after it (2026-07-26 19:49, 2026-07-27 18:28/18:32/18:39) have nothing.
The ticket's prediction — a backfill without a write-path fix reopens the gap on the next session — is
proven true in the data.

**Orphan `CONTAINS` question — root cause found, and it is the same cause as the missing turn identity.**
The ticket offered two candidate answers ("a write-path defect or the expected result of session
pruning"). It is neither. Both defects were produced by one one-off run of
`scripts/replay_sessions_to_neo4j.py` (FRE-374 D3) around 2026-05-30:

1. It calls `consolidator._process_capture(capture)` **directly** (`replay_sessions_to_neo4j.py:225`),
   bypassing `consolidate()`. Session nodes are created only by `_consolidate_sessions()`, which
   `consolidate()` invokes at `consolidator.py:307` from a `sessions_with_new_turns` set the script
   never populates. Every Turn it writes is therefore born without a Session node, and
   `link_session_turns` `MATCH`es a Session node — so `CONTAINS` is impossible for them, permanently
   (`turn_exists` short-circuits any later re-processing).
2. It resolves identity as `metadata.get("user_id") or metadata.get("owner_id")`, falling back to
   **`uuid4()`**. Measured: **0 of 1034** of those sessions carry either key in `metadata`, while **all
   1034** have the authoritative `sessions.user_id` column populated. So it stamped a fresh *random*
   UUID per session, which then hit `MATCH (p:Person {user_id: …})`, matched nothing, and silently
   wrote no edge.

Evidence chain, every element measured:

| Evidence | Fits |
|---|---|
| Script docstring estimates "1,025 sessions" | 1039 distinct orphan `session_id`s |
| Script reads from the Postgres sessions table | 1034 of 1039 orphan sids are real Postgres sessions |
| Script last touched 2026-05-30 | Orphan turns stop dead at 2026-05-29; zero after |
| Same `create_conversation` writer | Orphan and linked cohorts have **identical property key sets** |
| Ran before FRE-523 added `eval_mode` | 0 of 1828 orphans carry the `eval_mode` key; linked turns do |
| Replayed old/local history | Orphans are 100% `execution_profile=local`; linked are 72 local / 50 cloud |

The live write path is healthy on both counts since 2026-06 (100% edge coverage, zero new orphans).
Repairing the existing 1828 is backfill = master's domain; **preventing a rerun from recreating them
is folded into this PR** (§3 Step 4), since the script is still in the tree with both defects intact.

---

## 2. The design decision the ticket demands (property vs. node)

**Decision: the `user_id` property on `Session` and `Turn` is the authoritative identity record. The
existing `(:Person)-[:PARTICIPATED_IN]->(:Turn)` edge is retained unchanged as a traversal affordance.
No new `Session`→`Person` edge is added.**

Rationale, argued from the measurement above:

1. **The edge fails open; the property cannot.** `create_conversation` resolves identity with
   `MATCH (p:Person {user_id: $user_id})` — if that Person does not exist, the MERGE writes **no edge**
   and the Turn is still created with identity silently dropped. The method's own docstring already
   flags this as "a logic bug worth investigating". A property written straight from `capture.user_id`
   (non-optional on `TaskCapture`) has no dependency on a second node existing.
2. **The property survives the absent-session case.** 1828 turns have no Session node to inherit from,
   so identity derived from the session is unrecoverable for them while identity carried on the turn is
   not. This answers the ticket's "should turns carry identity independently" — yes. *Correction to an
   earlier draft of this plan:* the orphan population is **not** proof that the live path needs this —
   the orphans are a script artifact (§1), not a property of the live writer. It is a design argument,
   not a measured live defect.

   **The counter-argument, stated rather than buried.** Had the property existed at replay time, those
   turns would now carry 1039 *fabricated* UUIDs that look valid and resolve to nobody — arguably worse
   than honest absence. Property-primary still wins: a wrong property is **detectable** (join it against
   `users`), whereas the missing edge was **silent** — 1828 turns lost identity with no error and no
   trace, which is precisely the failure this ticket exists to close. The fabrication is the script's
   defect, fixed in §3 Step 4, not the property's.
3. **A new node/edge for sessions adds nothing the property does not.** Per-user scoping and deletion
   are `MATCH (s:Session {user_id: $uid}) DETACH DELETE s` either way, and `:Person` is already
   reachable from any turn. ADR-0107 §4 rejected a redundant identity field on exactly this test —
   "no query it uniquely enables".
4. **It matches how the capture record already models identity** (the ticket's own words) and keeps the
   ADR-0052 anchor invariant: identity is keyed by `user_id`, never by name.

Non-goal, stated explicitly: this ticket does **not** change `assert_claim`/`assert_stance` (FRE-738,
shipped) and does **not** extend the joinability probe (FRE-740, its own ticket, ADR-0107 §6).

---

## 3. Implementation

### Step 1 — failing tests first (TDD)

**New file** `tests/test_memory/test_graph_user_identity.py` — unit, mocked driver:
- `create_session` passes `user_id` into the Cypher params and the query sets `s.user_id`.
- `create_session` with `user_id=None` uses `COALESCE` so an existing value is never erased.
- `create_conversation` sets `t.user_id` from its existing `user_id` parameter.
- `create_conversation` with `user_id=None` does not clobber (`COALESCE`).

**New file** `tests/test_second_brain/test_consolidator_session_identity.py` — unit, mocked service:
- `_consolidate_sessions` passes `capture.user_id` through to `create_session`.
- Captures with **mixed** `user_id` for one session **fail closed**: `user_id=None` is passed (so
  `COALESCE` preserves whatever is already stored) and an error is logged. Never pick a winner.
- The **stub-Turn path** (`_process_capture` extraction-capped branch, `consolidator.py:646`) also
  carries identity — it calls `create_conversation` separately and must not be a silent identity hole.
- A capture with **no `session_id`** still produces an identity-bearing Turn (the orphan case): no
  Session node is created, and the Turn's own `user_id` is the only identity carrier.
- `create_conversation` logs `participated_in_person_missing` (not success) when the `:Person` is absent.

**New file** `tests/test_memory/test_graph_user_identity_integration.py` — real Neo4j (test substrate
:7688, `pytest.skip` when unavailable, matching `test_graph_structure.py`'s established pattern):
Drive the **real consolidator** (only `extract_entities_and_relationships` is mocked — no LLM) against
the real graph, then **read the properties back with Cypher**. This is the ticket's "verified by
querying the graph rather than by asserting the code path runs", at component altitude:
- normal capture → both its `Session` and its `Turn` carry the capture's `user_id`;
- stub-Turn path (extraction capped) → the Turn still carries `user_id`;
- capture with no `session_id` → an identity-bearing orphan Turn, no Session node;
- re-running consolidation over an already-attributed session does **not** null its `user_id`.

Verify: each test fails before the change, for the right reason.

### Step 2 — `src/personal_agent/memory/service.py`

`create_conversation` (~line 1034) — add to the existing Turn MERGE's SET clause:
```cypher
t.user_id = COALESCE($user_id_str, t.user_id),
```
passing `user_id_str=str(user_id) if user_id is not None else None`. Docstring: state that the property
is the authoritative record and the `PARTICIPATED_IN` edge is a best-effort traversal affordance that
writes nothing when the `:Person` is missing (cite FRE-998 / ADR-0107).

`create_session` (~line 1137) — new keyword-only parameter `user_id: UUID | None = None`; add to the
Session MERGE's SET clause:
```cypher
s.user_id = COALESCE($user_id, s.user_id),
```

**`COALESCE` is load-bearing on the Session path specifically.** A Session node is re-`MERGE`d every
time its session receives new turns, so a bare `SET s.user_id = $user_id` with a null argument would
**erase master's 118-session backfill** on the next consolidation of an old session. (On the Turn path
it is cheaper insurance than necessity: `turn_exists` short-circuits re-processing of an existing turn,
so the Turn MERGE rarely re-runs — but `protocol_adapter.store_episode` calls `create_conversation`
with no identity at all, so the guard is still the correct shape.) Argument order is
`COALESCE($user_id, existing)` — a genuine new value always wins, so a wrong identity remains
correctable through the normal write path. The tests above pin this.

**Fix the fail-open edge log while we are here** (folded in, not a separate ticket — it is a false
identity signal inside this ticket's exact subject). `create_conversation` today logs
`participated_in_edge_written` unconditionally, even when `MATCH (p:Person {user_id: ...})` matched
nothing and no edge was written. Have the statement `RETURN 1 AS ok` and branch on whether any record
came back: zero rows means the `:Person` is missing (edge genuinely not written) — log
`participated_in_person_missing` at warning. A returned row covers both "created" and "already
existed", which is the correct success condition for a `MERGE`.

### Step 3 — `src/personal_agent/second_brain/consolidator.py`

In `_consolidate_sessions` (~line 431), derive the session's identity from its captures and pass it:
```python
session_user_ids = {c.user_id for c in ordered if c.user_id is not None}
if len(session_user_ids) > 1:
    # Invariant violation, not routine ambiguity: TaskCapture.user_id is non-optional
    # and a session belongs to exactly one user. Fail closed — writing either candidate
    # could overwrite correct identity with the wrong user's.
    log.error(
        "session_captures_mixed_user_id",
        session_id=session_id,
        user_id_count=len(session_user_ids),
        trace_id=trace_id,
    )
    session_user_id = None
else:
    session_user_id = next(iter(session_user_ids), None)
created = await self.memory_service.create_session(
    session_node, user_id=session_user_id, trace_id=trace_id
)
```
**Fails closed on disagreement.** Picking the earliest capture would be deterministic but arbitrary —
there is no reason the first capture is the correct one, and under `COALESCE` a non-null value always
wins, so a wrong pick would silently overwrite correct identity. Passing `None` preserves whatever is
stored and leaves a loud error for a condition that should be impossible.

### Step 4 — `scripts/replay_sessions_to_neo4j.py` (folded in, owner-approved 2026-07-28)

The script that caused §1's damage is still in the tree and would reproduce it exactly on a rerun. Two
minimal fixes, no redesign:

1. **Read the authoritative identity.** Replace the `metadata.get("user_id") or metadata.get("owner_id")`
   lookup with the `sessions.user_id` column already selected from Postgres, and **fail the session
   loudly** (log + skip, counted as an error) rather than falling back to `uuid4()`. Fabricating an
   identity that resolves to no user is never better than refusing to write one.
2. **Create the Session nodes.** After the capture loop, call `_consolidate_sessions` for the sessions
   whose turns were created, mirroring what `consolidate()` does at `consolidator.py:307` — so a replay
   produces linked, attributed turns instead of orphans.

Tests: `tests/scripts/test_replay_sessions_identity.py` — the session `user_id` comes from the column
not the metadata; a session with no resolvable user is skipped rather than assigned a random UUID.

### Step 5 — documentation

Update the `create_session` / `create_conversation` docstrings (done in Step 2) and add the identity
property to any graph-schema reference that enumerates Session/Turn properties. No ADR change: this
implements ADR-0107's propagation principle on the graph surface; it does not amend a decision.

---

## 4. Quality gates

Four new test files:
`tests/test_memory/test_graph_user_identity.py` (8 unit) ·
`tests/test_second_brain/test_consolidator_session_identity.py` (4 unit) ·
`tests/scripts/test_replay_sessions_identity.py` (8 unit) ·
`tests/test_memory/test_graph_user_identity_integration.py` (4, real Neo4j :7688).

Then full `make test` · `make mypy` · `make ruff-check` · `make ruff-format` ·
`pre-commit run --all-files`.

**Non-vacuity check:** the integration suite was re-run with only the `src/` changes stashed —
4 failed, 4 passed with them restored. The proof does not pass by accident of the graph's prior state.

Self-review: `code-review` at **high** (src + memory + a substrate write path). `security-review` is
**not** indicated — no new input, subprocess, file, auth, secret or network surface; identity values
already flow through this path today.

---

## 5. Acceptance criteria and how each is proven

| # | Criterion (from the ticket) | Proof |
|---|---|---|
| AC-1 | A new session and its turns carry the user identifier in the graph immediately after a real turn, verified by querying the graph | Integration test drives the **real consolidator** against the real test-substrate graph and reads the properties back via Cypher — covering the normal turn, the stub-Turn path and the sessionless/orphan turn (component altitude, pre-merge) **+** post-deploy live query on a fresh session (system altitude, master's runbook) |
| AC-2 | Existing sessions carry it after the backfill | Split honestly in two: (a) **no regression** of the 118 backfilled sessions — the `COALESCE` no-clobber test is the guard; (b) the **4 sessions created after the backfill are still unattributed** and this ticket does not retro-fix them — they gain identity only if they receive another turn. Backfilling those 4 is master's, flagged in the handoff. AC-2 does **not** pass on "118/122 already carry it" |
| AC-3 | A query answers whose session a given session is, from the graph alone, for a non-owner user | Post-deploy: `MATCH (s:Session {session_id: $sid}) RETURN s.user_id` for a non-owner session, cross-checked against Postgres `sessions.user_id` |
| AC-4 | Explicit property-vs-node decision recorded, not defaulted | §2 above, argued from the live measurement |

AC-1's live half and AC-3 are post-deploy — they need a real turn through the deployed gateway, which
is master's, not the build session's. Runbook goes in the Linear handoff comment.

---

## 6. Out of scope (stated, not silently dropped)

- **Backfilling `user_id` onto the 2288 existing turns.** Backfill is master's per the ticket. 370 can
  be derived from the existing `PARTICIPATED_IN` edge; the rest need the Postgres `sessions` join.
- **Repairing the 1828 orphan `CONTAINS` relationships.** Historical, pre-June, no live recurrence.
- **The joinability probe `user_id` check** — FRE-740, ADR-0107 §6.
- **Claims** — master's comment resolved these as out of scope (identity is modelled structurally and works).
- **Backfilling the 4 post-backfill sessions** (2026-07-26/27) — master's, same class as the original backfill.

## 7. Known limitations (stated, not hidden)

- Turn creation, the participation edge, Session creation and linking are **separate autocommit
  statements**, not one transaction. Concurrent conflicting non-null identity writes are last-writer-wins.
  Not addressed here: a session belongs to one user, so conflicting writers imply the invariant
  violation §3 already fails closed on, and fixing it properly means transaction restructuring well
  beyond this ticket.
- The `PARTICIPATED_IN` edge stays best-effort. This ticket makes its failure **audible** (the log fix)
  rather than making it fail closed, because the property is now the authoritative record and the edge
  is a convenience.
