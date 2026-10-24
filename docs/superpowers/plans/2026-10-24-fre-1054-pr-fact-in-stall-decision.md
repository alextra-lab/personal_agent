# FRE-1054 — The stall decision must carry the facts it read

> Ticket: FRE-1054 (Approved, `Tier-2:Sonnet`, `stream:build2`). Related: FRE-976 (terminal release), FRE-1095 (decision-provenance ADR request).
> Tier: Standard (changes daemon decision logic). Codex plan-review required.

## Problem

`scripts/dispatch/orchestrator.py` calls `_open_pr_exists` and passes the result to `decide`. `_decide_launched` reads
it in one branch only (`normalized == "in review"`). A ticket in `Approved` with an open PR falls through to
`reason="no-pr-past-timeout"`. The reason names the branch taken. It does not describe the state. No log line carries the PR fact.

Two adjacent defects in `_open_pr_exists`, both still present on main `0ff3dd12`:

1. `... or bool(raw)` returns true for any open PR in the search result, so the `headRefName` match is dead code.
2. A non-zero exit, bad JSON or a non-list returns `False`. "Could not ask" looks the same as "no PR".

## Decisions

| Decision | Choice | Why |
|---|---|---|
| Release the slot when a ticket leaves the stream without a terminal state? | **No. Hold and surface.** | FRE-976 sets the rule that only a confirmed terminal state releases the slot. A release on `Approved` or off-board risks double dispatch to a busy seat. Every tick logs `on_stream_board` in `dispatch_decision`. The stall notice carries it on the first pre-pickup stall of a record. The notice is latched once per record (`stall_notified`), so a change after that notice is logged only. A test records this. |
| How does a PR belong to a ticket? | The ticket id appears as a whole token in the PR `headRefName`, `title` or `body`. | Codex plan-review found that the `adr` stream is dispatched too, and its branches are `adr-NNNN-...`, not `fre-...`. A branch-only rule would stop `run_complete` for ADR tickets. A token match in title or body is stricter than the `gh --search` text match, and it ends the catch-all `or bool(raw)`. |
| How do facts reach the log? | One frozen dataclass `TrackedFacts` on `StreamDecision.facts`. | One carrier for all launched-record branches. No `Any`, no dict. |
| Tri-state PR fact | `bool \| None`. `None` = could not determine. | Separates "unknown" from "false". |
| Stall reason | A function of the PR fact. | See the table below. |

Reason table for the pre-pickup stall branch (ticket not In Progress, run not confirmed, past `stall_timeout_s`):

| `pr_open` | reason |
|---|---|
| `False` | `no-pr-past-timeout` (a PR was checked and none exists) |
| `True` | `pr-open-ticket-not-in-review` |
| `None` | `pr-check-undetermined-past-timeout` |

The `in-progress-past-timeout` reason is unchanged. `_apply` keys its latch on that exact string. The facts are attached to it.

## Files

- Modify `scripts/dispatch/orchestrator.py`
- Modify `tests/scripts/test_orchestrator.py`
- Create this plan.

## Steps

### 1. Failing tests first (`tests/scripts/test_orchestrator.py`)

Add after `test_decide_never_completes_on_silence`:

- `test_decide_stall_names_the_open_pr_when_the_ticket_left_review` — launched record, `now=40_000`, `tracked_state="Approved"`, `tracked_pr_open=True`, `issues=[]`. Assert `kind == "stall"`, `reason == "pr-open-ticket-not-in-review"`, `facts.pr_open is True`, `facts.ticket_state == "Approved"`, `facts.on_stream_board is False`, `facts.age_s == 40_000`.
- `test_decide_stall_keeps_no_pr_reason_only_when_no_pr_was_found` — same, `tracked_pr_open=False` → `no-pr-past-timeout`, `facts.pr_open is False`.
- `test_decide_stall_says_the_pr_check_was_undetermined` — `tracked_pr_open=None` → `pr-check-undetermined-past-timeout`, `facts.pr_open is None`.
- `test_decide_carries_facts_on_every_launched_branch` — parametrised over Done, In Review (PR true, false and `None`), In Progress, Approved-within-grace. Assert `facts` carries `ticket_state`, `pr_open` and `run_confirmed`. The In Review with `None` case asserts `kind == "await"`.
- `test_decide_does_not_release_a_ticket_that_left_the_stream_board` — `issues=[]`, `tracked_state="Approved"`, past timeout. Assert `kind == "stall"` and `facts.on_stream_board is False`. This is the "surfaced, not released" proof.

Add `_open_pr_exists` unit tests (import it):

- non-zero exit → `None`
- invalid JSON → `None`
- JSON object (not list) → `None`
- `[]` → `False`
- `[{"number":1,"headRefName":"fre-999-other"}]` for `FRE-786` → `False` (the fallback is gone)
- `[{"number":1,"headRefName":"fre-786-slug"}]` → `True`
- `[{"number":1,"headRefName":"fre-7860-slug"}]` for `FRE-786` → `False` (no prefix match)
- ADR shape: branch `adr-0144-x`, title without a ticket id, body `Implements FRE-786` → `True`
- A body that mentions only `FRE-7860` → `False`

Add the end-to-end test through `run_once`, with a `_CapturingLogger` that records `(event, kwargs)`:

- `test_run_once_logs_the_pr_fact_with_the_stall_decision` — the exact FRE-1015 state: record `FRE-1015` launched at 0, `now=40_000`, board empty (ticket parked off the stream), `reconcile=lambda t: "Approved"`, runner answers `gh pr list` with `[{"number":738,"headRefName":"fre-1015-stance"}]`. Assert the `dispatch_decision` log has `pr_open=True`, `ticket_state="Approved"`, `on_stream_board=False`, and a reason other than `no-pr-past-timeout`. Assert the `dispatch_stall` notifier event carries `pr_open=True`, `on_stream_board=False`.

- `test_run_once_facts_after_the_stall_notice_are_logged_not_renotified` — tick 1 stalls on-board and notifies. Tick 2 has the ticket off-board with the PR open. Assert tick 2 logs `on_stream_board=False` and `pr_open=True`, and the notifier has one event only. This records the once-per-record latch.
- The end-to-end test asserts all five fields on both the log line and the notifier call.

Run: `make test-file FILE=tests/scripts/test_orchestrator.py`. Expect the new tests to fail on current main (confirm before step 2).

### 2. Implement (`scripts/dispatch/orchestrator.py`)

1. Add frozen dataclass `TrackedFacts` (`ticket_state`, `pr_open`, `on_stream_board`, `age_s`, `run_confirmed`) with `as_log_fields() -> dict[str, object]`. Place before `StreamDecision`.
2. Add `facts: TrackedFacts | None = None` as the last field of `StreamDecision`. Update its docstring.
3. `_open_pr_exists` → `bool | None`. Ask `gh` for `number,headRefName,title,body`. Return `None` for non-zero exit, `JSONDecodeError`, non-list. Match the compiled pattern `(?<![a-z0-9])<ticket>(?![0-9])` (case-insensitive) against `headRefName`, `title` and `body`. `fre-786` must not match `fre-7860`. Remove `or bool(raw)`. Add `import re`. Update the docstring.
4. `decide` and `_decide_launched`: type `tracked_pr_open: bool | None`. Pass `issues` to `_decide_launched`. Build `TrackedFacts` once at the top. Attach it to every returned `StreamDecision`. Pick the pre-pickup stall reason from the table. Keep `tracked_pr_open is True` in the `in review` branch (an undetermined check keeps the old behaviour: no `run_complete`).
5. `run_once`: annotate `tracked_pr_open: bool | None = False`. Add `**decision.facts.as_log_fields()` to the `dispatch_decision` log when facts exist. Add the same fields to the `dispatch_stall` notifier call.
6. Update the `_decide_launched` docstring: state that a non-terminal departure from the stream is held and surfaced, not released (FRE-1054).

### 3. Fix the three fixtures that relied on the fallback

`stdout='[{"number": 385}]'` at the three existing sites gains `"headRefName": "fre-<ticket>-x"` for the ticket under test. These fixtures passed only because of the defect.

### 4. Gates

`make test` · `make mypy` · `make ruff-check` · `make ruff-format` · `pre-commit run --all-files`. Commit. Then `feature-dev:code-reviewer` on `git diff origin/main...HEAD`.

## Acceptance criteria (from the ticket)

| # | Criterion | Proof |
|---|---|---|
| 1 | A tracked ticket in `Approved` with an open PR emits a decision that names the PR fact. It fails on current main. | `test_decide_stall_names_the_open_pr_when_the_ticket_left_review`, `test_run_once_logs_the_pr_fact_with_the_stall_decision` |
| 2 | Renaming the reason without carrying the facts fails. | The tests assert `facts.*` and the logged `pr_open`, not only the reason. |
| 3 | An undetermined fact differs from a false fact. | `_open_pr_exists` returns `None`; `test_decide_stall_says_the_pr_check_was_undetermined` |
| 4 | The branch-name match is live. | `_open_pr_exists` unrelated-branch and prefix tests |
| 5 | A ticket moved out of the stream is surfaced (decision recorded above). | `test_decide_does_not_release_a_ticket_that_left_the_stream_board` and the end-to-end test |

## Out of scope

- A release path for non-terminal departure (decision above).
- Repeat cadence of the once-per-record stall notice (a design question in the ticket comments, not a defect).
- The `gh` search itself (`--search <ticket>`).
