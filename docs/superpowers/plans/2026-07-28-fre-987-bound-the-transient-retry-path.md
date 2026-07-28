# FRE-987 — bound the transient retry path of the session-digest idle sweep

**Ticket:** FRE-987 (Urgent, Approved, `stream:build1`, `Tier-1:Opus`) ·
**Backing ADR:** ADR-0124 (D1 sweep, AC-2/AC-4, terminal-failure rule) ·
**Related:** FRE-947 (the producer this regressed from), FRE-992 (evidence bound), FRE-993 (why
generation failed at all), FRE-996 (JSON contract).
**Revision:** v2 — revised after codex adversarial plan review (findings folded in below).

## The defect, stated precisely

`BrainstemScheduler._session_summary_sweep_loop` (`scheduler.py:432-456`) wakes every
`session_summary_sweep_interval_seconds` (300 s) on a **wall clock** — no session-end, no turn, no
user activity triggers it — and calls `MemoryService.find_dirty_idle_sessions`
(`service.py:1641-1668`). A session is eligible when it is idle and its projection is stale, and is
excluded only when it carries **both** a terminal-eligible failure reason **and** an attempt count at
or above `session_summary_max_attempts`.

`SummaryFailureReason` deliberately classifies `BUDGET_DENIED`, `MODEL_ERROR`, `TIMEOUT` and
`EMPTY_OUTPUT` as transient — never terminal (ADR-0124's terminal-failure rule; the owner confirmed
on the ticket that this classification is *correct*). The consequence is that a session failing for
any of those reasons is eligible again on the very next tick, forever:

* idleness is a **qualification**, not a cooldown — a quiet failing session satisfies every gate on
  every tick, so zero user activity is the worst case rather than the safe one;
* each attempt is a **wholesale** regeneration (`f(canonical captures)`) at up to two model calls, so
  a transient reason that reaches the model — `EMPTY_OUTPUT` above all — bills a full session
  summarisation per tick and produces nothing;
* `BUDGET_DENIED` costs nothing at the provider but is retried 288×/day against a cap that resets
  once a day, and each denial writes to the graph and burns the *shared* attempt counter.

Measured: 14.19 USD produced 6 digests (~2.37 each); 12.79 of that, across 289 calls, came from 9
stuck sessions. Daily spend on the `captains_log` cap went from ~0.05 to 5.02 (100.3 % of cap).

Two further every-tick paid paths, found by the codex review and confirmed in source: a **generated**
digest whose publish comes back `False` (`write_session_digest` collapses "refused because a turn
landed" and "the graph write raised" into one bool, `service.py:1265-1298`) leaves the session dirty
with nothing recorded; and `record_session_summary_failure` returns `False` on a graph error
(`service.py:1556`), so the pacing stamp is not written exactly when it is needed. Both re-attempt on
the next tick and both cost model calls.

**The defect is not the classification and not the Cypher.** It is that the transient path has no
bound of any kind: no backoff, no pacing, and no awareness of when the failing condition could
plausibly clear. The fix bounds the transient path; it does not flatten the taxonomy, does not
terminalise a budget denial, and does not touch the cap.

## Design

Four parts, all inside the retry policy.

**A. A per-session retry stamp — the durable bound.** A new graph property
`Session.summary_retry_after` (ISO-8601, UTC-normalised at the write boundary — the convention
`ended_at`/`summary_generated_at` already use, made explicit rather than assumed).
`find_dirty_idle_sessions` adds `AND (s.summary_retry_after IS NULL OR s.summary_retry_after <= $now)`
— the explicit `IS NULL` disjunct is required for the same reason AC-2 demands one on freshness. It
**delays, never excludes**: once the instant passes the session is selected again. Every write that
resets the failure state clears it (`write_session_digest`, `mark_session_projection_clean`), and so
does `create_session` — new turns are new input, and a session that just received activity must not
sit out an old cooldown.

**B. The pacing rule.** A pure function `next_retry_after` in `memory/session_digest.py`:

* when the caller knows **when the condition clears** — a `BudgetDenied` carries `window_resets_at`,
  the exact instant its cap's window rolls over — retry then;
* otherwise exponential backoff on the session's attempt index: `min(base × 2^(n−1), max)` with base
  900 s and max 21 600 s (6 h), both configurable.

**C. Stand the sweep down on a *global* condition.** A budget denial and a graph-write outage are
properties of the cap and the store, not of the session: once one session hits either, every other
session in that sweep will hit it too. The sweep therefore stops at the first one and stands itself
down until a stated instant (`window_resets_at` for a denial; one backoff-base for a store outage),
logging once. Without this, the sweep walks the backlog one session per tick, inflating each one's
shared attempt counter — the shared-counter hazard FRE-992 removed for evidence failures.

This breaker is **in-process and lost on restart, and that is acceptable** because it is an
optimisation, not the bound: the durable stamp in A is what survives a restart. A crash-loop
re-scans, denies, stamps, and each stamped session is then held until its window resets, so the
backlog drains into durable stamps rather than repeating.

**D. A failure about a shared resource must not spend the session's attempt budget.**
`summary_attempt_count` is shared across every reason while terminality tests only the *current* one,
so anything that spends it shortens every other reason's budget. Two classes stop spending it: a
**budget denial** (nothing was sent — evidence about the cap) and **unreadable evidence** (already
bounded by its own counter since FRE-992, so the shared one adds no bound). Both left a session that
its **first** genuine deterministic failure retires — a week of denials, or two store outages, put the
counter past the limit before any deterministic failure occurred. Deterministic reasons keep spending
it, and must: that counter is how they reach terminality.

### What this bounds, stated at the right altitude

Per **failing** session: ≤ 8 attempts/day instead of 288, saturating at one per 6 h; a first failure
still retries in 15 min, so a genuine blip is not punished. It does **not** by itself bound aggregate
daily spend — newly-eligible sessions are unrestricted, which is the feature working correctly, and
the aggregate ceiling remains the cost gate. The regression was a *failing* population re-attempted
forever, and that is what is bounded.

### Why not the alternatives

* *Terminalise `EMPTY_OUTPUT`* — flattens the taxonomy the owner explicitly ruled correct, and
  permanently retires sessions over a stochastic fault.
* *Lengthen the sweep interval* — the interval bounds detection lag for **healthy** sessions;
  slowing it penalises the working path to bound the failing one.
* *Raise the cap* — explicitly excluded by the ticket; funds the regression.

## Acceptance criteria

| # | Criterion | Where proved |
|---|---|---|
| AC-a | The cadence and trigger of the `captains_log`-billed scheduled work are stated **from the code**, not inferred from timing | The *defect* section above + ADR-0124 Amendment D, citing `scheduler.py:432-456` and `service.py:1641-1668` |
| AC-b | A session failing for a transient reason is **not** re-attempted on the next tick | `test_a_failed_session_waits_out_its_backoff_before_being_retried` |
| AC-c | A budget denial is retried **when its window resets**, not on the sweep clock, and stays non-terminal (ADR-0124 AC-4: retryable, freshness not advanced, digest/label untouched) | `test_a_budget_denial_defers_to_the_window_reset` |
| AC-d | One budget denial stands the whole sweep down until the reset — no further model calls, no further graph writes | `test_a_budget_denial_stands_the_sweep_down_until_the_window_resets` |
| AC-e | Backoff grows with consecutive failures and saturates at the ceiling | `test_backoff_grows_and_saturates` (pure) |
| AC-f | A success clears the stamp, and so does new session activity | `test_a_successful_write_clears_the_retry_stamp`, `test_new_activity_clears_the_retry_stamp` |
| AC-g | The stamp **delays, never retires**: a session held by a stamp is selected again once it elapses | `test_the_retry_stamp_delays_rather_than_excludes` |
| AC-h | A generated digest whose publish fails, and a failure record that cannot be written, do **not** buy another model call on the next tick | `test_an_unpublishable_digest_does_not_pay_again_next_tick`, `test_an_unrecordable_failure_stands_the_sweep_down` |
| AC-i | A budget denial does not spend the shared attempt budget | `test_a_budget_denial_does_not_spend_the_retry_budget` |
| AC-j | Daily `captains_log` spend is explainable by digest production, with a stated cost per digest | **Post-deploy** (master runbook): `budget_counters` daily spend ÷ `session_summary_generated` count |
| AC-k | The Phase-1 digest population accumulates | **Post-deploy** (master runbook): Cypher count of sessions with `summary_generated_at` rising |

AC-j and AC-k cannot be proved pre-merge — they need the sweep re-enabled in prod
(`AGENT_SESSION_SUMMARY_ENABLED=false` today). They go in the handoff runbook, not the PR checklist.

## Steps

1. **Test first — pacing policy.** `tests/personal_agent/memory/test_session_summary_retry_pacing.py`:
   table test over `next_retry_after` (AC-e), the budget-reset branch (AC-c), and the guard for a
   reset already in the past. → verify: fails with `ImportError`.
2. **Implement `next_retry_after`** in `memory/session_digest.py` (leaf module, pure — the caller
   passes base/max). → verify: step-1 tests pass.
3. **Test first — producer surfaces the reset instant.** `BudgetDenied` → outcome is
   `BUDGET_DENIED` **and** `outcome.retry_after == e.window_resets_at`. → verify: fails.
4. **Implement:** `retry_after: datetime | None` on `SessionSummaryOutcome`; set on the
   `BudgetDenied` branch of `generate_session_digest` (`session_summary.py:720`). → verify: passes.
5. **Test first — the graph layer.** In `tests/personal_agent/memory/test_session_digest_write.py`:
   `record_session_summary_failure` writes a UTC-normalised `summary_retry_after` and does **not**
   increment the shared counter when told not to (AC-i); `find_dirty_idle_sessions` passes `$now`,
   carries the `IS NULL OR <= $now` disjunct, and returns `summary_attempt_count`; the two publish
   paths and `create_session` null the stamp; a driver error returns `UNAVAILABLE` while a refused
   predicate returns `REFUSED` (AC-h). → verify: fails.
6. **Implement the service changes** (`memory/service.py`): a `SessionWriteResult` StrEnum
   (`ACCEPTED` / `REFUSED` / `UNAVAILABLE`) returned by `write_session_digest`,
   `mark_session_projection_clean` and `record_session_summary_failure` — the sweep cannot pace
   correctly while "the graph refused me" and "the graph is broken" are the same `False`;
   `record_session_summary_failure(..., retry_after, spend_attempt=True)`; the reset sites and
   `create_session` also set `s.summary_retry_after = null`; `find_dirty_idle_sessions` gains the
   `$now` predicate and returns `summary_attempt_count`. → verify: step-5 tests pass.
7. **Test first — the sweep.** In `tests/personal_agent/brainstem/test_session_summary_sweep.py`:
   teach `_FakeMemory` about the stamp and the tri-state, then add AC-b, AC-c, AC-d, AC-f, AC-g,
   AC-h, AC-i. → verify: new tests fail, the existing 30+ stay green.
8. **Implement the sweep changes** (`brainstem/scheduler.py`): compute `retry_after` per failure
   (outcome-supplied instant, else backoff from `row["summary_attempt_count"] + 1`); pass it at both
   `record_session_summary_failure` call sites; `self._summary_sweep_paused_until` checked at the top
   of `run_session_summary_sweep` and set + `break` on a budget denial or an `UNAVAILABLE` write.
   → verify: step-7 tests pass.
9. **Settings**: `session_summary_retry_backoff_base_seconds` (900.0, ge 30) and
   `session_summary_retry_backoff_max_seconds` (21600.0, ge 60), with `.env.example` entries if the
   config guard requires them. → verify: config tests + config-guard script green.
10. **Docs**: ADR-0124 **Amendment D** — the retry-pacing rule, stated as bounding the transient path
    without changing terminality; `Status Updates` entry. → verify: read-back.
11. **Quality gates**: `make test` · `make mypy` · `make ruff-check` · `make ruff-format` ·
    `pre-commit run --all-files`; then `code-review` at **high** (memory + cost + src logic) and
    `security-review` (no new input, subprocess, auth, secret or network surface — verdict recorded
    either way).

## Codex findings — disposition

### Self-review round (code-review, high)

| Finding | Disposition |
|---|---|
| `EVIDENCE_UNAVAILABLE` still spent the shared `summary_attempt_count`, so two store outages plus one genuine schema failure retire a session — the same hazard D fixes for denials, through the other door | **Fixed** — `spend_attempt=False` on that path; D7 generalised to "a failure about a shared resource does not spend the session's budget"; `test_a_store_outage_never_spends_the_retry_budget` now asserts what its name promised |
| A helper in `test_session_digest_write.py` was not `ruff format` clean | **Fixed** |

### Codex plan-review round

| Finding | Disposition |
|---|---|
| Critical: generated-but-unpublished retries every tick | **Fixed** — tri-state write result + stand-down (design C, step 6/8) |
| High: bounds cadence, not aggregate spend | **Claim corrected** — bound restated per failing session; aggregate stays the cost gate |
| High: failure-record write errors bypass the stamp | **Fixed** — same tri-state + stand-down |
| High: budget denials burn the shared attempt counter | **Fixed** — design D |
| High: restart reopens backlog-wide denial processing | **Answered, no code** — the durable stamp is the bound; the breaker is an optimisation (design C) |
| Medium: ISO lexicographic ordering rests on an unstated invariant | **Fixed** — UTC-normalise at the write boundary |
| Medium: new activity does not clear the stamp | **Fixed** — `create_session` clears it (design A) |
| Medium: the "no retirement" test is trivially satisfiable | **Fixed** — AC-g asserts re-selection after the stamp elapses |
| Medium: `attempt_count ASC` ordering can starve a recovered session | **Not fixed, surfaced** — pre-existing; the eligible corpus is far below the 25-row window, so it cannot bite today. Reported in the handoff |
| Low: `scripts/eval/session_digest_eval.py` calls the producer directly | **Not fixed, surfaced** — operator-run eval, still gated by `session_summary_enabled`; not the scheduled path |

## Out of scope (stated, not silently dropped)

* Re-enabling `AGENT_SESSION_SUMMARY_ENABLED` in prod — a deploy action, master's.
* Sizing the `captains_log` cap — the ticket requires an unconstrained, understood day first.
* Why generation fails at all (FRE-993) and the non-durable input read (FRE-992) — separately filed.
* Whether the same unbounded-transient-retry shape exists in other sweeps — left open by the ticket;
  surfaced in the handoff as a candidate follow-up, not fixed here.
