# FRE-658 — Explicit time-window recall semantics under de-gated recall

**Ticket:** FRE-658 (Approved → In Progress) · **Project:** Memory Recall Quality
**Backing ADR:** ADR-0100 (relevance-bounded candidate generation)
**Related:** FRE-653 (relevance-bounded de-gate), FRE-655 (flag-flip rollout),
FRE-724 / ADR-0104 (multi-path recall), FRE-654 (broad de-gate)
**Codex plan-review:** verdict *Ship with changes* — this revision folds in all four findings.

---

## The decision (the deliverable)

**YES — an explicit caller-supplied time window remains a HARD candidacy filter, even when
recall is de-gated (relevance-bounded OR multi-path). Automatic recall stays invariant to its
default `recency_days` (ADR-0100 AC-1a).**

**Why (not "unsupported / post-filter"):**
1. **ADR-0100 already sanctions it** — Impl Notes: `recency_days` "no longer a hard gate on
   the automatic path; **retained for explicit time-scoped queries**."
2. **User intent** — "what did I discuss *last week* about X" carries an explicit temporal
   bound; out-of-window turns are *wrong answers*, not lower-ranked ones.
3. **A post-recall filter alone is quality-wrong on the relevance-bounded path** — it filters
   the already-chosen all-time top-k, so an in-window relevant turn ranked #11 never enters the
   candidate set. Correct time-scoped recall needs the window to bound *candidacy*.

## Mechanism (revised per Codex finding 4)

A single field **`hard_recency_days: int | None`** on `MemoryQuery` (default `None`), *not* a
boolean — this avoids the invalid `time_scoped=True + recency_days=None` state and is
self-documenting: `None` = no hard window (soft/de-gated); `int` = explicit hard window of N
days that survives the de-gate. `recency_days` keeps its existing role (ranking weight under
the flag; legacy hard gate when flag off).

The **`memory_search` tool** sets `hard_recency_days = recency_days` **iff** the caller supplied
an explicit positive `recency_days`; otherwise `None`. Automatic callers
(`request_gateway/context.py`, `orchestrator/executor.py` via `protocol_adapter.py`) never set
it → `None` → invariant → **AC-1a holds by construction.**

## Scope (corrected per Codex findings 1 & 2)

The `memory_search` tool entity query **always passes `query_text`**, so under
`multipath_recall_enabled` it routes to `_multipath_query_memory` **before** the
relevance-bounded branch (`service.py:2294`). Handling only the relevance-bounded branch would
leave the explicit window silently ignored under the multipath flag master flips live — a silent
all-time leak. So **both de-gated entity paths are in scope:**

| Path | Reached when | Enforcement |
|------|--------------|-------------|
| `query_memory` relevance-bounded entity branch | `relevance_bounded_recall_enabled`, entity recall | **candidacy-bound**: inject `AND c.timestamp >= $cutoff_date` into the candidate Cypher (naive-ISO cutoff, matches legacy L2442) |
| `_multipath_query_memory` (ADR-0104 entity path) | `multipath_recall_enabled` + `query_text` | **hard post-recall filter** on resolved turns by a tz-aware cutoff (fused arms take no recency; post-filter is the pragmatic stopgap that closes the leak — documented, not silent) |

**Explicitly OUT of scope — and now shown SAFE, not deferred:** `query_memory_broad`. The
tool's broad call **omits `query_text`** (`memory_search.py:185-195`), so both broad de-gate
branches (`… and bool(query_text)` / `multipath and query_text`) stay False → the broad path
keeps its legacy hard-recency gate for the tool. No change needed. (A future ticket may bring
`query_memory_broad` under `hard_recency_days` if a `query_text`-bearing broad caller appears.)

## Acceptance criteria (proof of done)

| # | Criterion | Proof |
|---|-----------|-------|
| AC-658.1 | The contract is documented | `protocol.py` note + resolved code comment (replaces the "tracked follow-up for the rollout ticket" TODO at `service.py:2385`) + this plan + ticket decision record |
| AC-658.2 | Under the relevance-bounded flag, an explicit window hard-bounds candidacy | `_hard_recency_cutoff_iso` helper unit test (naive ISO) + Cypher injects `c.timestamp >= $cutoff` |
| AC-658.3 | Under the multipath flag, an explicit window drops out-of-window turns (no all-time leak) | `_multipath_query_memory` post-filter unit test: mock resolved turns spanning the cutoff, assert only in-window survive |
| AC-658.4 | AC-1a preserved: automatic path (`hard_recency_days is None`) invariant to `recency_days` under both flags | helper returns `None`; post-filter no-ops; no automatic caller sets the field |
| AC-658.5 | Flag-OFF byte-identity | tool window-value coercion `int(recency_days or 90)` unchanged; `hard_recency_days` consulted only inside the two de-gated branches; naive-ISO cutoff matches L2442 |

## Files & changes

1. **`memory/models.py`** — add `hard_recency_days: int | None = Field(default=None, …)` to
   `MemoryQuery` with a docstring line (explicit hard time window surviving the de-gate; `None`
   = soft/no window).

2. **`tools/memory_search.py`** —
   - executor signature `recency_days: int | None = None` (was `int = 90`).
   - `explicit_window = recency_days is not None and int(recency_days) > 0` **before** the
     unchanged `recency_days = int(recency_days or 90)` coercion (preserves flag-off window
     value: omitted/`0`→90, `N`→N).
   - entity-path `MemoryQuery(… hard_recency_days=recency_days if explicit_window else None)`.
   - broad path unchanged (already safe).

3. **`memory/service.py`** —
   - module-level helper `_hard_recency_cutoff_iso(query) -> str | None` → **naive** ISO
     (`datetime.utcnow() - timedelta(days=query.hard_recency_days)`), matching the legacy cutoff
     format at L2442; `None` when `hard_recency_days` is falsy.
   - relevance-bounded entity branch: build `time_frag = "AND c.timestamp >= $cutoff_date"`
     (else `""`) from the helper; interpolate after the entity-predicate `AND (...)` line; set
     `params["cutoff_date"]` when present.
   - `_multipath_query_memory`: when `query.hard_recency_days`, compute a tz-aware cutoff and
     skip resolved turns older than it in the assembly loop (normalise naive turn timestamps to
     UTC defensively).
   - replace the stale comment at ~L2385 with the resolved contract.

4. **`memory/protocol.py`** — `recency_days` docstring note on `MemoryRecallQuery`: hard window
   preserved for explicit time-scoped queries; automatic recall demotes it to a weight.

5. **Tests** (fast `make test`, no Neo4j):
   - `tests/test_tools/test_memory_search.py` — explicit positive → `MemoryQuery.hard_recency_days == N`;
     omitted → `None`, `recency_days == 90`; explicit `0` → `None`, `recency_days == 90` (documents the
     pre-existing "0 = all history" docstring mismatch → follow-up ticket, not fixed here).
   - `tests/test_memory/test_relevance_bounded_recall.py` — `_hard_recency_cutoff_iso`: naive-ISO
     cutoff when set; `None` when `hard_recency_days` None/0; assert format is naive (no `+00:00`).
   - `tests/test_memory/test_multipath_recall_integration.py` (or a new focused unit) — multipath
     post-filter drops out-of-window resolved turns; no-ops when `hard_recency_days is None` (AC-1a).

## TDD sequence

1. Write failing tests (tool detection, `_hard_recency_cutoff_iso`, multipath post-filter) → confirm fail.
2. Add the field; add the helper; wire tool + both branches; update comments/docstrings.
3. `make test-file` on the three test files → green.
4. Full gates: `make test` · `make mypy` · `make ruff-check` + `make ruff-format` · `pre-commit run --all-files`.

## Follow-ups (Step 5, Needs Approval)

- **`memory_search` "0 = all history" docstring/behaviour mismatch** — `int(recency_days or 90)`
  coerces explicit `0` to 90; the tool docstring claims 0 = all history. Pre-existing; document + fix separately.
- **(optional) candidacy-bound the multipath fused arms on `hard_recency_days`** — replace the
  post-recall filter with a recency predicate threaded into the dense/lexical/structural arms, if a
  time-scoped multipath query ever needs the stronger candidacy guarantee.

## Live verification (master, post-flag-flip — for the ticket comment)

With each flag on in turn, via the FRE-489 harness / a `search_memory` call for an entity whose turns
are all >30 days old: `recency_days=7` → **empty** (window respected); `recency_days` omitted →
returns the old relevant turns (de-gate working). Proves AC-658.2 (relevance-bounded) and AC-658.3
(multipath) live, plus AC-1a.
