# FRE-992 — Session digest reads the durable capture store, and never writes off evidence it could not read

**Ticket:** FRE-992 (Approved, `stream:build2`, `Tier-1:Opus`, High)
**Backing ADR:** ADR-0124 — session-summary producer and phased consumption (Phase 0)
**Codex plan-review:** run 2026-07-26 (session `019f9e52-648a-7b03-9d06-6b34c7d61205`) — 5 confirmed
defects against the first draft; all 5 adopted, plus 4 of its "plan misses". Revisions marked **[codex]**.
**Pre-PR self-review:** code-review (high) + security-review, run against the implemented branch — 3 and
4 findings respectively, all fixed on-branch. See §7.
**Related, deliberately not folded in:** FRE-987 (retry loop), FRE-989 (cost attribution), FRE-990
(reflection enable flag), FRE-993 (digest output-ceiling calibration)

---

## 1. The defect, as traced in source

Three lines produce the 46 silent write-offs:

1. `captains_log/capture.py:185` — `_get_captures_dir()` resolves the capture source by walking up from
   `__file__` to `telemetry/captains_log/captures`. That path is not durable in the gateway container;
   the durable copy is the `agent-captains-captures-*` ES index, written by the very next statement of
   `write_capture` (`capture.py:233`).
2. `second_brain/session_summary.py:535` — a read of `< MIN_TURNS_FOR_DIGEST` (2) returns
   `SKIPPED_BELOW_FLOOR`, indistinguishable from a genuine single-turn session.
3. `brainstem/scheduler.py:582` — that status calls `mark_session_projection_clean`, setting
   `summary_generated_at`. `find_dirty_idle_sessions` never re-selects a session carrying that field,
   so the write-off is permanent and its graph state is byte-identical to a legitimate floor skip.

## 2. Acceptance criteria this ticket carries (ADR-0124 Phase 0)

| AC | What it requires | Violated today | Proof after |
|----|------------------|----------------|-------------|
| **AC-8** | "the assembled prompt contains **every turn in the session** and the full, untruncated user and assistant text of each" | The prompt for the 46 contained *zero* turns — the reader looked in a store holding none | A session whose captures are in ES yields a prompt with all N turns; and a read that cannot be shown complete never reaches the model at all |
| **AC-7** | every multi-turn session quiet past the threshold has a digest, **except those carrying a recorded terminal failure** | The 46 have no digest *and* no recorded failure — excluded from the check by a clean stamp they never earned | Unreadable evidence becomes a *recorded* failure with a stated reason and a bounded counter, so AC-7's exclusion clause is honest |
| **AC-2** | no session left behind its own activity | A session marked clean without a projection having run is "left behind" wearing a clean stamp | Freshness advances only on a projection that provably read the session's evidence |
| **AC-5** / never-silently-truncate | "*Fails if* … the input is silently truncated" | Nothing detects a short read; the header asserts "Nothing has been truncated" unconditionally | Fail-closed on any unproven read makes that header true **by construction** |

**Out of scope by the ticket's own words** — recovering the 46 by clearing `summary_generated_at` is
"a deliberate choice with a cost attached … made knowingly". Mechanism ships here; the recovery Cypher
goes to master in the handoff, unexecuted.

## 3. Design

### 3.1 Read both stores and union them **[codex #3 — reversed from ES-first]**

The first draft read ES and fell back to disk only when ES was empty. That is wrong: the two stores are
**not replicas**. `write_capture` writes disk synchronously (`capture.py:220`) and then *schedules* the
ES index through `schedule_es_index`, which is fire-and-forget and silently drops the task when there is
no running loop (`es_indexer.py:152-157`). So ES can hold turns 1 and 3 while disk holds 1, 2, 3 — and
a non-empty ES answer would have hidden turn 2 forever.

```python
class CaptureSource(StrEnum):
    ELASTICSEARCH = "elasticsearch"
    DISK = "disk"
    BOTH = "both"
    NONE = "none"

@dataclass(frozen=True)
class SessionCaptureRead:
    captures: tuple[TaskCapture, ...]   # union, deduped by trace_id, sorted (timestamp, trace_id)
    source: CaptureSource
    unreadable: int                     # ES docs that failed validation + disk files that failed to parse
    truncated: bool                     # the ES read hit `size`, so exhaustion is unproven

async def load_session_captures(session_id, *, started_at, ended_at, es_client, limit=1000)
    -> SessionCaptureRead
```

- ES query: `{CAPTURES_INDEX_PREFIX}-*,-{SUBAGENT_CAPTURES_INDEX_PREFIX}-*`. **The exclusion is
  load-bearing** — the sub-agent audit index is a sibling under the same wildcard (`capture.py:27`) and
  its docs carry `session_id` but a `SubAgentCapture` shape. `term` on `session_id`, `range` on
  `timestamp` over `[started_at − 1d, ended_at + 1d]` (the same widening the disk reader uses for its
  UTC date directories), `sort` on `timestamp` **then `trace_id`** for a deterministic tie-break,
  `ignore_unavailable`, `allow_no_indices`.
- `len(hits) >= limit` sets `truncated=True` **[codex #2]** — a fixed `size` is itself a silent
  truncation boundary. Sessions top out near 17 turns so this never fires in practice; it is honest
  rather than load-bearing.
- **A doc that fails `TaskCapture` validation is counted, never swallowed.** Same for the disk side:
  `read_captures` currently logs and drops an unparseable file (`capture.py:299`), so the plan's own
  unreadable count would have been blind to exactly the condition it exists to catch **[codex #5]**. An
  internal `_read_session_captures_from_disk` returns `(captures, unreadable)`; the public sync
  `read_session_captures` keeps its signature for its two existing test callers.
- `es_client=None` → disk only. No client is ever constructed inside this function **[codex, FRE-375]**.

### 3.2 The completeness oracle is the graph's `Turn` nodes, not `s.turn_count` **[codex #4]**

`s.turn_count` is **not** a session total. `consolidator.py:434` computes `len(ordered)` from the
captures in *one* consolidation batch (default `limit=50`, `consolidator.py:177`) and
`create_session` does `SET s.turn_count = $turn_count` (`service.py:1158`) — an overwrite. A six-turn
session consolidated across two windows ends up carrying the second window's count.

`Turn` nodes are the sound signal: `MERGE (t:Turn {turn_id: $turn_id})` (`service.py:1016`) is
idempotent and accumulates, one per capture. `count(Turn {session_id})` is therefore a **lower bound on
the turns that genuinely existed** — it can undercount (consolidation may never have seen a turn) but it
can never overcount, because a `Turn` node is proof its capture existed. A lower bound is exactly the
right shape for a fail-closed check: it can prove evidence is missing, and can never falsely accuse a
complete read.

`find_dirty_idle_sessions` returns it alongside each row via `OPTIONAL MATCH` + `count`.

### 3.3 Fail closed — a read that cannot be shown complete never reaches the model **[codex #2 — replaces the draft's "declare the shortfall in the prompt"]**

The first draft would have generated from 19 of 22 turns with a banner saying so. That is the same class
of error as the ticket itself: a digest published as clean, built from evidence the producer knew it did
not have. The `SOME EVIDENCE IS UNAVAILABLE` banner keeps its original, narrower job (an absent
assistant response *within* a turn it did read).

Read is **complete** iff `unreadable == 0` and `not truncated` and `len(captures) >= graph_turn_count`.

| Condition | Outcome | Graph write |
|-----------|---------|-------------|
| not complete | evidence unavailable | `record_session_summary_failure(EVIDENCE_UNAVAILABLE, evidence_failure=True)` |
| complete, 0 captures | evidence unavailable | same — **zero captures never marks clean**, the ticket's central instruction |
| complete, 1 capture | positively-established single-turn session | `mark_session_projection_clean` (`skipped`) |
| complete, ≥ 2 captures | generate | unchanged path |

No model call is issued on any unavailable path — the decision precedes `generate_session_digest`.
A useful consequence: `build_prompt`'s "Nothing has been truncated" is now true by construction, so
`second_brain/session_summary.py` needs **no change at all**.

### 3.4 A retry bound that is actually reason-specific **[codex #1]**

`summary_attempt_count` is **shared across all failure reasons** while terminality tests only the
*current* reason (`service.py:1508`, `service.py:1589`). So adding `EVIDENCE_UNAVAILABLE` to
`TERMINAL_ELIGIBLE_REASONS` would not have bounded it: two prior `MODEL_ERROR`s then one evidence
failure terminalises a session after a *single* evidence read — permanently writing it off on a
transient ES outage, which is this ticket's own sin.

So `EVIDENCE_UNAVAILABLE` is deliberately **not** added to `TERMINAL_ELIGIBLE_REASONS`. It gets its own
counter:

- `s.summary_evidence_failure_count` — incremented only by an evidence failure.
- `find_dirty_idle_sessions` additionally excludes `coalesce(s.summary_evidence_failure_count, 0) >= $max_attempts`.
- Reset to 0 by `write_session_digest` and `mark_session_projection_clean`, alongside the counters
  they already reset — so a session that becomes readable again is fully rehabilitated.

Bounded by construction (the FRE-987 shape this must not reintroduce), and free: the path issues no
model call, so even pre-terminal retries cost one ES query.

A refused failure-write (the `expected_ended_at` predicate lost a race) counts as `refused`, mirroring
the success path **[codex]**.

### 3.5 Wiring

`BrainstemScheduler.__init__` stores `self._lifecycle_es_client` (today the argument is only forwarded
to `DataLifecycleManager`/`TelemetryQueries`). `service/app.py:838` already passes
`es_handler.es_logger.client`, an `AsyncElasticsearch` — no change there. Counter `no_captures` is
renamed **`evidence_unavailable`**: its meaning inverts from "a clean projection" to "a recorded
failure", and keeping the old name on the new meaning is the very drift this ticket is about. Only
tests and `tests/fixtures/session_digest/REGISTRY.md` read it.

## 4. Steps

| # | Step | Verify |
|---|------|--------|
| 1 | Failing test: ES reader — session filter, sub-agent index exclusion, ascending sort, deterministic tie-break | `make test-file FILE=tests/personal_agent/captains_log/test_session_capture_source.py` |
| 2 | Implement `CaptureSource`, `SessionCaptureRead`, `load_session_captures` (union + dedupe by `trace_id`) | step 1 passes |
| 3 | Failing tests: ES-only / disk-only / **complementary halves union to the whole** / both empty → `NONE`; unparseable ES doc and unreadable disk file each raise `unreadable`; `size` hit sets `truncated` | same file passes |
| 4 | Round-trip test: `TaskCapture.model_dump(mode="json")` → `normalize_capture_doc_for_es` → `TaskCapture.model_validate`, incl. dict / list / null `tool_results.output` and `arguments` **[codex #6]** | same file passes |
| 5 | `EVIDENCE_UNAVAILABLE` reason (**not** terminal-eligible); `summary_evidence_failure_count` write/reset/exclude in `service.py`; `graph_turn_count` on `find_dirty_idle_sessions` | `make test-file FILE=tests/personal_agent/memory/test_session_digest_write.py` |
| 6 | Sweep decision table + counter rename; `_FakeMemory` imports `TERMINAL_ELIGIBLE_REASONS` instead of hard-coding three of them **[codex]**; **invert** the two tests asserting clean-on-no-captures, citing FRE-992 | `make test-file FILE=tests/personal_agent/brainstem/test_session_summary_sweep.py` |
| 7 | Test (the 46-session case, AC-8): a 17-turn session with captures **only in ES** generates, and the prompt carries all 17 turns | same file passes |
| 8 | Tests: evidence-unavailable is never marked clean; retried exactly `max_attempts` times then excluded; **an interleaved `MODEL_ERROR` does not shorten that bound**; a refused failure-write counts `refused` | same file passes |
| 9 | Docstrings; `tests/fixtures/session_digest/REGISTRY.md` counter note | — |
| 10 | Gates | `make test` · `make mypy` · `make ruff-check` · `make ruff-format` · `pre-commit run --all-files` · code-review `high` · security-review (new ES read path) |

## 5. Deliberate non-goals

- **No settings knob** for the capture source — a knob is a second way to be wrong.
- **No disk → ES backfill**, and no claim about *why* the host directory is empty (retention sweep vs.
  unmounted container path). It is a real open question the ticket raises; it stops governing this path
  once both stores are read, and guessing would ship an unverified claim.
- **No recovery of the 46** — mechanism only; the decision and the Cypher go to master.
- **No change to `second_brain/session_summary.py`** — fail-closed made the planned prompt surgery
  unnecessary.

## 6. Risk

| Risk | Mitigation |
|------|------------|
| Reintroducing an unbounded retry (FRE-987's shape) | Dedicated `summary_evidence_failure_count` with its own bound (§3.4) — the shared counter would **not** have delivered this |
| Fail-closed denies a digest to a session genuinely missing turns | Correct and *visible*: it becomes a recorded terminal failure, which is precisely AC-7's exclusion clause used honestly, rather than a clean stamp it did not earn |
| Sub-agent index leaking into the read | Index-pattern exclusion **plus** a validation count that makes any leak loud rather than silently dropped |
| `Turn`-node oracle undercounts (consolidation never saw a turn) | Undercount only ever fails *toward* generating, never toward a false accusation — a `Turn` node is proof its capture existed |
| ES read cost | One `term`+`range` query per dirty session, ≤25 per sweep (`find_dirty_idle_sessions` limit) |
| Tests reaching prod ES | `load_session_captures` never constructs a client; all sweep tests inject a fake (FRE-375) |

## 7. What the pre-PR self-review changed

**code-review (high)** — 3 findings, all fixed:

1. **The disk unreadable-count was window-scoped, not session-scoped.** A date directory holds every
   session's captures for that day and the filename is a `trace_id`, so one corrupt file made the read
   of *every* session in the window non-authoritative. Combined with (2) that was a mass-retirement
   vector. Failures are now attributed from the file's own `session_id` before being charged.
2. **The evidence bound was a permanent, self-reinforcing lockout.** An excluded session is never
   selected again, and the only resets were a successful read or clean-mark — the very things exclusion
   prevents. A ~10-minute ES blip (two sweeps at the 300s interval, bound of 2) retired every session
   swept during it. Fixed twice over: an outage no longer spends the budget at all (§3.4), and
   `create_session` clears the counter when new turns arrive, so activity restores the budget.
3. **`LIMIT` came after the `Turn` aggregation**, so the count ran once per *candidate* rather than per
   returned row, against an unindexed label. `LIMIT` now precedes `OPTIONAL MATCH`, and
   `ensure_turn_session_id_index` adds the missing index.

**security-review** — no vulnerabilities (ES/Cypher injection, path traversal and credential leakage all
clear); 4 correctness/hygiene findings, all fixed:

4. **`es_client is None` was read as "no durable store expected".** The client is resolved once at
   startup, so it is `None` for the whole process when ES was down at boot — which classified a *total
   outage* as a deterministic shortfall and re-armed finding (2) through a different door. The test
   for (2) passed because it injected a client that raised. `stores_unavailable` is now `not
   es_consulted`, and a disk-only read is never `complete`.
5. **A file truncated mid-write is unattributable**, so it was neither charged nor surfaced. Now
   counted in `unattributable` — and discharged when the durable store already holds that `trace_id`,
   which the filename supplies. It is deliberately *not* fatal: it cannot be pinned on any session, and
   condemning the whole window is the larger harm. A non-zero value is an operator signal.
6. **`str(ValidationError)` embeds a repr of the offending input** — for a capture, the user's own
   message — shipping conversation content into the logs index, which has different access than the
   captures index. Replaced with field paths and error types (`_safe_error_summary`).
7. **Unreadable files were read twice**, the second time on every session on every sweep forever. The
   parsed body is now carried into the failure path instead of re-read.
