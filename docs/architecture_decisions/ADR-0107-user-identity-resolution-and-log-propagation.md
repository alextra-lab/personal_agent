> **Canonical source:** [`alextra-lab/ai_operations` › `docs/development/adrs/template.md`](https://github.com/alextra-lab/ai_operations/blob/main/docs/development/adrs/template.md).

# ADR-0107: User Identity Resolution for Claims + Trace/Log Identity Propagation

**Status:** Accepted
**Date:** 2026-07-02
**Deciders:** Owner (lextra); adr session (Opus)
**Amends:** ADR-0098 D2 (Personal-situational-facts clause only — "Claims about the owner's life" → "Claims about the acting authenticated User"; the Stance clause is unchanged)
**Related:** ADR-0052, ADR-0074, ADR-0098 (see References for what each contributes)
**Tags:** [identity, memory, neo4j, observability, logging, regression]

---

## Context

**What happened.** A live conversation on 2026-07-02 produced a `:Claim` node (`claim_id 3d5f385a-9887-4352-bf8d-1bbe6a8dd4ee`, content: *"The user is looking to buy a product to treat hair thinning due to GLP-1 use."*) that got attached via `HAS_FACT` to `:Person {name: "Alex", is_owner: true}`. The session that produced it (`session_id 45d8ba3d-b252-40b4-93b1-81e4cd9a8292`) belongs, per Postgres `sessions.user_id → users.user_id`, to **Susan** (`sceilidh@gmail.com`), a distinct authenticated user — not Alex, the harness owner. Investigation traced this to a **conflict between two accepted ADRs**, not a novel design gap: ADR-0098 D2 explicitly specifies Claims as owner-scoped, while ADR-0052 already established (and generalized elsewhere in the same codebase) that every authenticated user — not just the owner — is a first-class `:Person`. This ADR resolves that conflict.

**ADR-0052 already decided the identity model.** It establishes: every CF Access–authenticated user gets a `:Person {user_id}` node (`get_or_provision_user_person`, `memory/service.py:1636`); the owner is just one such Person additionally flagged `is_owner: true` (`bootstrap_owner_identity`, `memory/service.py:1573`); nodes are anchored **by `user_id`, never by name**, specifically to prevent collision with same-named third-party entities extracted from conversation (ADR-0052 §"anchor by user_id"); and the precedent write pattern is already established — `PARTICIPATED_IN` resolves `MATCH (p:Person {user_id: $user_id})`, not a singleton match.

**ADR-0098 (Living Claims) deliberately specified *both* Claims and Stances as owner-scoped — that specification is the thing in conflict with ADR-0052, not a code deviation from it.** ADR-0098 D2 states plainly: *"Personal-situational facts = Claims about **the owner's** life/relationships/events (`(owner)-[:HAS_FACT]->(:Claim)`...)"* and *"Stance = an **owner**↔World edge"* (`docs/architecture_decisions/ADR-0098-memory-substrate-and-lifecycle-architecture.md:55-56`); `second_brain/consolidator.py:722-725` implements this faithfully ("Both resolve the 'owner' sentinel to the is_owner Person node inside the service methods"). But ADR-0052 had *already* generalized past owner-only scope by the time ADR-0098 was written: every authenticated user gets a `:Person {user_id}`, and `get_owner_stanza()` was updated so "every authenticated user is greeted by their own name — not just the admin owner" (ADR-0052 §"per-request provisioning"). ADR-0098 D2's "Claims about the owner's life" phrasing did not carry that generalization forward for Personal-situational facts. Given the live deployment now has Susan, Erika, and Laurent as real authenticated users with real Personal-situational conversation content, this ADR **amends ADR-0098 D2 for Claims specifically**: `assert_claim` and `assert_stance` (`memory/service.py:1341` and `1250`) both currently hardcode the identical Cypher, matching ADR-0098's letter for both — this ADR keeps that Cypher for Stances (Decision §3) and changes it for Claims (Decision §2):
```cypher
MATCH (o:Person {is_owner: true})
...
CREATE (o)-[:HAS_FACT]->(cl:Claim {...})
```
Neither method accepts a `user_id` parameter. The call site (`second_brain/consolidator.py:739`, `assert_claim(claim, trace_id=capture.trace_id)`) has `capture.user_id` in scope two lines earlier (used for entity/turn creation at `consolidator.py:670`) but never passes it through. The result: **every** Personal claim from **every** authenticated user — Susan, Erika, Laurent, or anyone else in the `users` table — attaches to whichever Person carries `is_owner: true` (currently Alex), regardless of who actually said it. `Living Claims` is new (ADR-0098, 2026-06-27); the live graph holds exactly one `:Claim` node total, so blast radius today is one row — but the write path is wrong for every future claim from a non-owner user.

**The same identity is dropped from structured logs, which is how this took investigation to find instead of a Kibana query.** `TraceContext.user_id` (ADR-0074, `telemetry/trace.py:34-37`) exists precisely to propagate authenticated identity through the system, and `TaskCapture.user_id` (`captains_log/capture.py`) is correctly populated end-to-end (verified: the raw on-disk capture and its ES-indexed twin both hold Susan's UUID for this trace). But no request-scoped propagation mechanism exists — `trace_id`/`session_id` are manually passed as kwargs at every individual `log.info(...)` call site (no `structlog.contextvars` binding found anywhere in the codebase), and `user_id` was simply never added to that manual pattern. Quantified against the live `agent-logs-*` family (all indices, 2026-04-15 → 2026-07-02, ~3.05M documents):

| Segment | Doc count |
|---|---|
| Total log docs | 3,048,041 |
| Have `user_id` | 7,691 (0.25%) |
| Missing `user_id`, **and** no `session_id` (legitimately system-wide: `metrics.sampled`, `sensor_poll`, `budget_counter_snapshot`, scheduler ticks) | 3,001,634 |
| Missing `user_id` **despite having a `session_id`** — genuinely request-scoped, should carry it | **38,918** |

Of that 38,918-doc gap, `executor` (orchestrator) alone accounts for 7,994 (20.5%) — the single largest chokepoint — followed by `telemetry` (4,302), `ws_endpoint` (3,852), `service` (1,857), `cost_tracker` (1,544), `scheduler` (829), `consolidator` (545). A separate hand-rolled path, `ElasticsearchLogger.index_request_trace_from_snapshot` (`telemetry/es_logger.py:280-360`, producing `request_trace`/`request_trace_step` docs — the single largest missing-`user_id` event type at 9,834 docs), bypasses structlog entirely and builds its document dict manually; it already threads `trace_id`/`session_id` through its own signature but never had `user_id` added.

**What needs deciding.** (1) How `assert_claim`/`assert_stance` should resolve their subject Person now that ADR-0052's per-user model is the acknowledged baseline. (2) Whether Claims and Stances should resolve identically. (3) Whether a new discriminator field is needed to mark "authenticated User" Person nodes, given `is_owner` already exists and is arguably overloaded. (4) How `user_id` should propagate into structured logs so future identity bugs are a Kibana query, not a three-substrate archaeology dig.

---

## Decision

**1. Identity model — extend ADR-0052's already-established model to Claims; no new primitive.** A `:Person` is a "User of this harness" (anyone authenticated) iff `person.user_id IS NOT NULL` — this is already true today via `get_or_provision_user_person`/`bootstrap_owner_identity`; nothing new is added. `is_owner: true` remains, unchanged in name, as **an attribute a User can have** (the harness's operator/admin), not a separate primitive or label. Exactly one Person carries it (ADR-0052 invariant, unchanged). This ADR's prose is explicit that "Owner of this harness" (an attribute of a User) is distinct from any notion of per-object/per-fact ownership — ADR-0098 D2's "Claims about the owner's life" phrasing predates that distinction being made explicit.

**2. `assert_claim` resolves to the acting User, not the Owner.** New signature `assert_claim(claim: Claim, *, user_id: UUID, trace_id: str | None = None) -> str`. The Cypher's owner-sentinel match is replaced with `MATCH (u:Person {user_id: $user_id})`, mirroring the existing `PARTICIPATED_IN` precedent (ADR-0052). `second_brain/consolidator.py:739` passes `capture.user_id` through — no new plumbing required upstream; the value is already in scope.

**3. `assert_stance` is unchanged — by design, not oversight.** Stances model the harness owner's worldview toward World knowledge (pedagogical north star: Socratic tutor = World know-how + *the owner's* Stance toward it). `assert_stance` keeps `MATCH (u:Person {is_owner: true})`. This ADR states explicitly that this is a reasoned non-change: Claims are per-User facts about whoever is talking; Stances are Owner-scoped opinions about the World, and conflating the two was never correct for Stances in the first place.

**4. No new `is_user` field.** `person.user_id`'s presence is already the "is a User" predicate (ADR-0052). A redundant boolean invites drift from the anchor it would mirror and adds a field with no query it uniquely enables.

**5. Propagate `user_id` into structured logs via `structlog.contextvars`, bound once per request.** Bind `trace_id`, `session_id`, `user_id` via `structlog.contextvars.bind_contextvars(...)` at the point a live request's `TraceContext` is instantiated (gateway pipeline entry / orchestrator task start), clear via `clear_contextvars()` at request teardown. This replaces the current per-call-site manual kwarg threading for these three fields — fixing the `executor` 20.5%-of-gap chokepoint in one place rather than patching 30+ call sites, and preventing the same omission from recurring for new log call sites. `ElasticsearchLogger.index_request_trace`/`index_request_trace_from_snapshot` (`telemetry/es_logger.py`) bypass structlog and must be patched separately — add an explicit `user_id: UUID | None` parameter threaded into the `trace_doc`/`step_doc` dicts the same way `session_id` already is.

**6. Extend the joinability probe to check `user_id`.** `scripts/monitors/joinability_probe.py`'s walk (`observability/joinability/walk.py`) currently checks `session_id`/`trace_id` consistency across Postgres/ES/Neo4j but has no `user_id` check anywhere in its code (verified: no reference to `user_id` in `walk.py`). Add a check comparing the sampled session's `sessions.user_id` (Postgres) against the `user_id` on its ES log docs and, where a Claim exists for that session, the `user_id` of the `:Person` it's attached to in Neo4j. Without this, the probe cannot detect a regression of Decision §2/§5 going forward.

**7. One-time backfill.** Re-parent the single existing Claim (`3d5f385a-9887-4352-bf8d-1bbe6a8dd4ee`) from Alex's Person node to Susan's (`user_id 634c1446-642c-4d2b-88a9-1e783c9fb2d2`) via a single scoped Cypher statement, executed by master (live-graph write, not a build-session action).

---

## Alternatives Considered

### Option 1: Keep `is_owner`-only resolution for Claims (status quo)
**Description:** Leave `assert_claim` as-is; treat this as accepted single-owner scope.
**Pros:** Zero code change; matches the mental model of "Personal Agent" as one person's tool.
**Cons:** Empirically false against live data — the `users` table has 5+ real named accounts (Alex, Susan, Erika, Laurent, plus service/eval accounts) with real sessions and real conversation content; every non-owner user's Personal facts would continue silently misattributing to the owner indefinitely.
**Why Rejected:** The deployment is not single-user in practice, regardless of the product's original framing; this alternative is provably wrong today, not just theoretically limiting.

### Option 2: Add a redundant `is_user: true` boolean on `:Person`
**Description:** Mirror `is_owner` with a new `is_user` flag set on every authenticated Person node.
**Pros:** Symmetric with `is_owner`; arguably more readable in ad-hoc Cypher (`WHERE is_user` vs. `WHERE user_id IS NOT NULL`).
**Cons:** Purely redundant with `person.user_id IS NOT NULL`, which ADR-0052 already established as the authenticated-identity anchor; a second field that must always agree with the first is a drift risk (e.g. a future write path that sets one and forgets the other) for zero new capability.
**Why Rejected:** No query it uniquely enables; violates the project's "no redundant fields" default.

### Option 3: Rename `is_owner` to something less ambiguous (e.g. `is_harness_owner`, `role: "owner"`)
**Description:** Address the "is_owner is vague — an object could have an owner in an unrelated sense" concern by renaming the property.
**Pros:** Removes the ambiguity at the schema level, not just in prose.
**Cons:** `is_owner` is a live property on the one existing owner node and is referenced by name in three accepted ADRs (0052, 0074, 0098) and multiple call sites (`bootstrap_owner_identity`, `assert_stance`, dedup exclusion logic per ADR-0052). A rename is a real migration (data + code + doc) for a naming concern that a clear ADR-level definition already resolves.
**Why Rejected (for now):** The ambiguity is fully addressed by this ADR's explicit terminology ("Owner of this harness" = an attribute of a User) without touching live data or three ADRs' worth of citations. Revisit if the term causes confusion in practice beyond this one incident.

### Option 4: Patch each of the ~30+ log call sites individually to pass `user_id` explicitly
**Description:** Add `user_id=...` as an explicit kwarg to every `log.info(...)` call currently missing it, mirroring how `trace_id`/`session_id` are handled today.
**Pros:** No new mechanism (`structlog.contextvars`) to learn or maintain; consistent with existing style.
**Cons:** This is the exact failure mode already observed — `trace_id`/`session_id` use manual per-call-site threading today, and `user_id` was simply forgotten across all of them. Patching 30+ sites once doesn't prevent site #31 (a future log call) from repeating the omission.
**Why Rejected:** Fixes the symptom, not the class of bug. `structlog.contextvars` binds once per request and is inherited by every subsequent log call in that async context by construction — it cannot be "forgotten" per call site the way a manual kwarg can.

---

## Consequences

### Positive Consequences

- Every authenticated user's Personal claims attach to their own Person node — the multi-user deployment now behaves like one, instead of silently degrading to single-owner semantics under the hood.
- `agent-logs-*` becomes joinable by `user_id` for request-scoped traffic, so a future incident like this one is a Kibana filter, not a three-substrate (Neo4j + Postgres + raw capture file) reconstruction.
- The Owner/User terminology conflict that caused the original bug is resolved in ADR prose without a data migration, closing the same ambiguity for future ADRs that touch `:Person`.
- Fixing the propagation mechanism (contextvars) at the `executor` chokepoint is a one-time change that structurally prevents recurrence, rather than a one-time cleanup that decays again as new log call sites are added.

### Negative Consequences

- `structlog.contextvars` introduces implicit state: a log call issued from a code path that doesn't inherit the bound context (e.g., a manually-created thread outside `asyncio.to_thread`, or a `asyncio.create_task` under an executor/framework that doesn't copy `contextvars.Context`) silently loses `trace_id`/`session_id`/`user_id` with no error — this must be verified at each task-spawn boundary in the request path, not merely bound once and assumed to propagate everywhere.
- `assert_claim`'s signature change touches its one production call site (`consolidator.py:739`) plus test fixtures/mocks — small in count but must be grepped and updated exhaustively, not just the obvious call.
- `ElasticsearchLogger`'s dict-based path needs its own manual `user_id` threading regardless of the contextvars fix (it bypasses structlog), so the propagation story ends up as two mechanisms (contextvars + one manual parameter), not one — an accepted asymmetry, not a clean unification.

### Risks and Mitigations

| Risk | Severity | Mitigation |
|------|----------|------------|
| `contextvars` don't propagate across a task/thread boundary somewhere in the request path, silently reintroducing the gap for a subset of logs | Medium | Add a regression test that spawns the consolidation background task and asserts its emitted logs still carry the bound `user_id`/`trace_id`; document any boundary found not to propagate |
| A future extraction/dedup path merges a non-owner User's Person node with a same-named third-party entity extracted from conversation | Medium | `assert_claim`'s Cypher matches by `user_id` only, per ADR-0052's anchor invariant — never by name; add a regression test asserting a same-named non-`user_id` Person is never matched |
| `assert_claim`'s new required `user_id` parameter breaks an untested or eval-mode caller | Low | Single production call site (`consolidator.py:739`); `capture.user_id` is a non-optional `UUID` on `TaskCapture` already (including eval-mode traffic, which uses the real `eval-verify@example.com` account, not a null identity) |
| The one-time Claim backfill is a live-graph write under time pressure and is done wrong (wrong row, wrong direction) | Low | Single Cypher statement scoped by exact `claim_id`; executed by master under live-environment custodianship, not the build session; reversible by re-running the inverse MATCH/CREATE |

---

## Implementation Notes

**Files affected:**
- `src/personal_agent/memory/service.py` — `assert_claim` (line 1341: signature + Cypher `MATCH` clause), `assert_stance` (line 1250: unchanged, but add a code comment citing this ADR so the non-change is legible to future readers)
- `src/personal_agent/second_brain/consolidator.py` — line 739, pass `user_id=capture.user_id` into `assert_claim(...)`
- `src/personal_agent/telemetry/es_logger.py` — `index_request_trace` (line 242) / `index_request_trace_from_snapshot` (line 280) — add `user_id: UUID | None` parameter, include in `trace_doc`/`step_doc`
- `src/personal_agent/observability/joinability/walk.py` — add a `user_id` consistency check (Postgres `sessions.user_id` vs. ES log docs vs. Neo4j `:Person` on any Claim for the sampled session); none of `walk.py`'s existing checks reference `user_id` today
- Request-entry binding point for `structlog.contextvars` — wherever a live request's `TraceContext.new_trace(...)` is currently constructed in the gateway pipeline (`request_gateway/pipeline.py`) and/or `orchestrator/executor.py` task start; exact call site to be confirmed by the build session against current `main`
- One-time backfill: scoped Cypher (not a script committed to `scripts/`, given it targets exactly one row) — `MATCH (old:Person {is_owner:true})-[r:HAS_FACT]->(c:Claim {claim_id:'3d5f385a-9887-4352-bf8d-1bbe6a8dd4ee'}), (new:Person {user_id:'634c1446-642c-4d2b-88a9-1e783c9fb2d2'}) DELETE r CREATE (new)-[:HAS_FACT]->(c)` — run once by master

**Migration steps:** None schema-level — the `person_user_id_unique` constraint already exists (`bootstrap_owner_identity`, idempotent). No new labels, no new properties beyond the backfill's relationship re-pointing.

**Testing strategy:**
- Unit test: `assert_claim` with a non-owner `user_id` resolves `HAS_FACT` to that Person's node, not the `is_owner` node (mocked Neo4j session, assert on the Cypher params/target).
- Unit test: `assert_stance` continues to resolve to `is_owner` regardless of any caller context — regression guard for Decision §3.
- Integration test: a synthetic request through the gateway pipeline emits at least one `log.info(...)` call downstream (e.g. in `consolidator`) whose captured record includes the bound `user_id` — proves the contextvars binding actually reaches a real call site, not just the binding call itself.

---

## Verification / Acceptance Criteria

- **AC-1** — A Claim asserted from a non-owner authenticated user's session attaches via `HAS_FACT` to that user's own `:Person` node, not the `is_owner` node. **Check:** send a message from a live test session belonging to a known non-owner user that is designed to produce exactly one Personal claim (e.g. a clear, unambiguous personal-fact statement); after consolidation runs, `MATCH (p:Person)-[:HAS_FACT]->(c:Claim {session_id: $session_id}) RETURN count(c) AS n, collect(p.user_id) AS owners`; cross-check against `sessions.user_id` in Postgres for that session. *Fails if* `n = 0` (claim was never written — a half-finished implementation that silently drops the write must not pass by vacuous absence), if `n > 1` (a duplicate-write bug must not pass just because every duplicate happens to attach to the right user), if any `owners` entry ≠ the session's actual Postgres `user_id`, or if any claim attaches to the `is_owner` Person instead.
- **AC-2** — Stances continue to resolve exclusively to the `is_owner` Person regardless of which user's session produced them (Decision §3's non-change holds under test, not just in prose). **Check:** trigger a stance-producing turn from a non-owner session; `MATCH (p:Person)-[:HAS_STANCE]->(s) WHERE p.is_owner = true` count increases by exactly one; no `:Person {is_owner: NULL or false}` ever gains a `HAS_STANCE` edge. *Fails if* any non-owner Person acquires a `HAS_STANCE` edge.
- **AC-3a** — For a freshly-issued live chat request, structlog-emitted session-scoped log lines in `agent-logs-*` carry a non-null `user_id` equal to the request's actual authenticated `user_id`, with zero tolerance for a *wrong* value and limited tolerance for a *missing* one (pre-auth lines only). **Check, two parts, both required, both scoped by `trace_id` (not `session_id`) so the denominator can't be gamed by omitting `session_id` from problem log lines** — `trace_id` is already universally emitted today (it is the base correlation key throughout the current logging, unaffected by this fix) so it cannot be selectively dropped without breaking unrelated, already-relied-upon correlation: (1) `count(trace_id=T AND exists(user_id) AND user_id != <actual>)` **must equal 0** — any log line with the wrong `user_id` is an outright fail, never counted against a tolerance; (2) `count(trace_id=T AND exists(user_id) AND user_id=<actual>)` ÷ `count(trace_id=T AND event_type NOT IN ["request_trace","request_trace_step"])` must clear 90% — compare against the pre-fix measurement in this ADR's Context (0.25% overall / 0-of-many for a comparable request). *Fails if* part (1) is nonzero, part (2) does not clear 90%, or the denominator in (2) is itself anomalously low versus this trace's total log volume (a sign that `trace_id` binding itself was dropped from would-be-failing lines).
- **AC-3b** — The `ElasticsearchLogger` manual-dict path (`request_trace`/`request_trace_step` docs, which bypass structlog and are not fixed by AC-3a's mechanism) also carries `user_id` for the same request. **Check:** `GET agent-logs-*/_doc/trace_<trace_id>` and its `_step_*` siblings for the same live request; each must have `user_id` equal to the request's actual authenticated `user_id`. *Fails if* any `request_trace`/`request_trace_step` doc for that trace lacks `user_id` — this is deliberately separate from AC-3a so a fix that only adds contextvars binding (and skips the `es_logger.py` dict-construction patch from Decision §5) cannot pass by AC-3a's volume alone.
- **AC-4** — The specific misattributed Claim from this incident is corrected. **Check:** `MATCH (c:Claim {claim_id:'3d5f385a-9887-4352-bf8d-1bbe6a8dd4ee'})<-[:HAS_FACT]-(p) RETURN p.user_id`. *Fails if* the result is anything other than `634c1446-642c-4d2b-88a9-1e783c9fb2d2` (Susan).
- **AC-5** — After Decision §6 extends the joinability probe's walk with an explicit `user_id` check, running it against a live non-owner session reports matching `user_id` across Postgres/ES/Neo4j. **Check:** `python -m scripts.monitors.joinability_probe --session-id <non-owner session>` exits green (0), and the probe's own result document (`agent-monitors-joinability-*`) shows the new `user_id` check as `pass`, not merely absent. *Fails if* the probe reports red (2) for a `user_id` mismatch, or if the result document has no `user_id` check recorded at all (i.e. Decision §6 was skipped and the probe is silently blind to this invariant, which is exactly how AC-5 could otherwise pass vacuously).

**Seam owner:** the observability-propagation ticket (Decision §5–§6 — `structlog.contextvars` binding + the `es_logger.py` and `joinability_probe` patches; filed last in the implementation sequence per this ADR's Step 5 tickets) owns the assembled-ADR close: it must run AC-1, AC-2, AC-3a, AC-3b, AC-4, and AC-5 together against one live non-owner request/session in production, not just pass each ticket's isolated unit test. The ADR does not close on the Person-resolution ticket landing alone.

---

## References

- ADR-0052 — Seshat Owner Identity Primitive (`docs/architecture_decisions/ADR-0052-*.md`) — establishes the per-user `:Person {user_id}` model and the anchor-by-`user_id`-never-by-name invariant this ADR extends to Claims
- ADR-0074 — End-to-End Traceability and Observability Joinability (`docs/architecture_decisions/ADR-0074-*.md`) — `TraceContext.user_id`, the joinability probe
- ADR-0098 — Memory Substrate & Lifecycle Architecture (`docs/architecture_decisions/ADR-0098-*.md`) — introduced `assert_claim`/`assert_stance`; D2's Personal-situational-facts clause is amended here for `assert_claim` (Stance clause is affirmed unchanged)
- `src/personal_agent/memory/service.py` — `assert_claim` (line 1341), `assert_stance` (line 1250), `get_or_provision_user_person` (line 1636), `bootstrap_owner_identity` (line 1573)
- `src/personal_agent/second_brain/consolidator.py` — line 739 call site
- `src/personal_agent/telemetry/trace.py` — `TraceContext`
- `src/personal_agent/telemetry/es_logger.py` — `ElasticsearchLogger.index_request_trace_from_snapshot`
- `scripts/monitors/joinability_probe.py` — reused for AC-5
- Incident evidence (2026-07-02 live-session investigation, this ADR's origin): `claim_id 3d5f385a-9887-4352-bf8d-1bbe6a8dd4ee`, `trace_id e7b68197-6914-4a3a-b867-d0f27e992ccb`, `session_id 45d8ba3d-b252-40b4-93b1-81e4cd9a8292`

---

## Status Updates

### 2026-07-02 - Proposed
**Changed By:** adr session (Opus)
**Reason:** Live-session investigation of a misattributed Claim surfaced a conflict between ADR-0098 D2 (Claims specified as owner-scoped) and ADR-0052's already-generalized multi-user `:Person` model, plus a quantified logging-identity gap; drafted per owner direction after live discussion.

### 2026-07-02 - Accepted
**Changed By:** master session (owner-approved)
**Reason:** Approved at the master integration gate (PR #327 merged). Not gated on the implementation children landing (per the ADR-0106 precedent). Implementation chain FRE-738/739/740 approved for build; FRE-739 owns the assembled acceptance-criteria seam.

---

**Template Version:** 1.1
**Based On:** [Michael Nygard's ADR pattern](https://cognitect.com/blog/2011/11/15/documenting-architecture-decisions)
