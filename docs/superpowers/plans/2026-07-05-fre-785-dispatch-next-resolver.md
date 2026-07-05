# FRE-785 — Dispatch NEXT resolver (ADR-0110 T1)

**Backing ADR:** ADR-0110 (External Dispatch Orchestrator for build/adr Worker Sessions)
**Acceptance criteria carried:** AC-1, AC-6
**Ticket:** FRE-785 (stream:build2, Tier-2:Sonnet, context CLEAR)

## Scope

Build `scripts/dispatch/next_resolver.py`: a standalone, dry-runnable library + CLI that,
given a stream (`build1` / `build2` / `adr`), returns that stream's NEXT ticket (or none),
reusing the Linear-native dispatch contract verbatim (`.claude/skills/lifecycle-rules.md`
§ Dispatch): busy guard on `In Progress`/`In Review`, then the head of `Approved` issues
carrying the stream's label, ordered by priority (Urgent → High → Medium → Low → None)
then oldest-created, skipping any issue whose only blockers are still open (a blocker is
open until it reaches `Awaiting Deploy`/`Done`/`Canceled`/`Duplicate`).

It reads from Linear via GraphQL + `AGENT_LINEAR_API_KEY` (not the Linear MCP — per ADR
rationale), mirroring `scripts/reconcile_board.py`'s existing Linear-API approach (same
`urllib`-only, no new dependency). No `src/` behavior change — this is dev-process tooling.

## Files

1. `scripts/dispatch/__init__.py` — empty package marker (new subpackage under `scripts/`,
   matching the existing `scripts/audit/` precedent).
2. `scripts/dispatch/next_resolver.py` — the resolver:
   - `Blocker(identifier: str, state: str | None)` — a blocked-by relation target. `state`
     is `None` only when Linear's response omits it; treated conservatively as **open**
     (never silently satisfied) — see Edge cases below.
   - `IssueSnapshot(identifier: str, state: str, priority: int, created_at: str, labels:
     frozenset[str], blocked_by: tuple[Blocker, ...] = ())` — the dispatch-relevant fields:
     identifier, current Linear state name, numeric priority, ISO-8601 created timestamp
     (string-sortable), label names, and blockers.
   - `stream_label(stream) -> str` — `f"stream:{stream}"`.
   - `_TERMINAL_BLOCKER_STATES` — its own constant (`{"awaiting deploy", "done", "canceled",
     "cancelled", "duplicate"}`), defined independently of
     `reconcile_board._CLOSED_STATE_NAMES` (that constant omits `awaiting deploy`, which is
     terminal for *dispatch* blockers per lifecycle-rules.md — chains advance at merge, not
     deploy-verify — but not for board-reconciliation "Done" semantics; conflating the two
     would wrongly keep an `Awaiting Deploy` blocker open).
   - `_OCCUPIED_STATES = {"in progress", "in review"}` — both busy-guard states.
   - `_PRIORITY_RANK: dict[int, int]` — an explicit rank map (`{1: 0, 2: 1, 3: 2, 4: 3, 0:
     4}`), never a raw numeric sort (raw ascending would wrongly put `None` (0) before
     `Urgent` (1)).
   - `resolve_next(issues, stream) -> IssueSnapshot | None` — **pure**, the logic under test
     (busy guard on `_OCCUPIED_STATES` → priority-rank/oldest-created sort → skip any issue
     with at least one blocker whose state is `None` or not in `_TERMINAL_BLOCKER_STATES`).
   - `fetch_board(stream, api_key) -> list[IssueSnapshot]` — live GraphQL fetch (issues
     filtered server-side by the stream's label; `inverseRelations(filter: {type: {eq:
     "blocks"}})` for blocked-by, including each blocker's state name). Reuses
     `scripts.reconcile_board.load_linear_key` rather than duplicating key-resolution logic.
   - `main(argv) -> int` — CLI: `--stream` (required), `--json`. Prints the resolved
     ticket identifier (or `none`) and exits 0; exits 1 if no API key is configured.
3. `tests/scripts/test_next_resolver.py` — unit tests against `resolve_next` only (pure
   function, no network), covering AC-1's five required fixture boards + AC-6 + the edge
   cases below.

## AC-1 fixture boards (parity test, ≥5 required)

1. **Higher-priority-but-blocked head, skipped** — a High-priority issue with an open
   blocker, plus a Medium-priority issue with no blocker, same stream/Approved → resolver
   returns the Medium one. Parametrized over blocker state `In Progress` **and** `In
   Review` (both are open per lifecycle-rules.md — two test cases, not one).
2. **Wrong-stream decoy excluded** — an Urgent issue on `stream:build1` plus a Low-priority
   issue on `stream:build2` → resolving for `build2` returns only the Low one.
3. **Occupied stream → no candidate** — a separate `Approved` issue plus one occupying
   issue on the same stream label → resolver returns `None`. Parametrized over the
   occupying issue's state `In Progress` **and** `In Review` (two test cases).
4. **Empty board → no candidate** — `issues=[]` → `None`.
5. **Stale-but-satisfied blocker, NOT skipped** — an `Approved` issue whose only blocker is
   terminal → resolver returns that issue (must not treat it as blocked). Parametrized over
   all four terminal states: `Awaiting Deploy`, `Done`, `Canceled`, `Duplicate` (four test
   cases — `Awaiting Deploy` is the one lifecycle-rules.md calls out as the merge-not-deploy
   boundary, so it must be asserted directly, not inferred from `Done`).

## AC-6

A dedicated test reusing fixture board #3's occupied-stream shape (both `In Progress` and
`In Review` variants) asserting `resolve_next(...) is None` — "dry-run against an
occupied-stream fixture asserting zero candidates."

## Edge cases (not separately ADR-mandated, but required to trust the sort/skip logic and
avoid the specific drift risks codex's plan review flagged)

- Priority ordering across all five values (Urgent, High, Medium, Low, None) in one board,
  asserting full ascending order via `_PRIORITY_RANK` — guards against a naive raw-numeric
  sort that would put `None` before `Urgent`.
- Oldest-created tie-break when two candidates share priority.
- Case-insensitive state matching (`"done"` vs `"Done"`, `"in progress"` vs `"In Progress"`).
- An issue with multiple blockers, one open and one terminal → still skipped (any open
  blocker blocks).
- A blocker with `state=None` (Linear response omitted it) → treated as **open**
  (conservative default — never silently satisfied).
- A blocker that belongs to a different stream (or no stream label at all) but is itself
  open → still blocks. Blocker-openness is state-based, not stream-scoped
  (lifecycle-rules.md defines it purely by the blocker's own state).

## Out of scope (future tickets in the chain)

- `scripts/dispatch/launcher.py` (RC session launch) and `scripts/dispatch/orchestrator.py`
  (the poll loop) — later tickets (AC-2 through AC-7, AC-3/AC-4/AC-5/AC-7).
- Live Linear integration test — tests use fixtures only per the ticket.

## Test commands

```
uv run python -m pytest tests/scripts/test_next_resolver.py -v
make test
make mypy
make ruff-check
make ruff-format
```

## Acceptance-criteria proof plan (for the master-facing PR comment)

- AC-1: `test_next_resolver.py` — the 5 required fixture-board scenarios (parametrized to
  ~12 cases covering both busy-guard states and all 4 terminal blocker states), all passing.
- AC-6: the occupied-stream fixture tests (both `In Progress` and `In Review`), all passing.
