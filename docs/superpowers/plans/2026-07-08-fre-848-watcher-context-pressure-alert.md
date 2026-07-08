# FRE-848 — Watcher: context-pressure alert (poke master via context_probe)

Backing: FRE-847 (context_probe, merged), FRE-822 (long-session cost/degradation risk). No ADR
(dispatch tooling).

## Scope

Wire the merged `context_probe` module into `gating_watcher.run_once` so master gets a one-shot
checkpoint nudge before its context bloats. Read-only signal, no shell-out to a `context_probe.py`
subprocess — import `resolve_jsonl`/`read_context` in-process. Reuses `session_is_idle` and the
existing dedup store unmodified in spirit, but requires one surgical fix to a shared helper (see
"Design decision" below).

## Design decision: dedup key shape (revised after codex review)

`gating_watcher.state` (the dedup store) is a single flat `dict[str, float]` shared by every trigger
kind. `run_once` prunes it every tick via `prune_state(state, ..., open_prs=[pr.number for pr in prs])`,
which calls `_pr_of_key(key)` to decide whether a key is tied to a specific PR that is no longer open.
`_pr_of_key` returns `parts[1]` for any key with **3 or more** colon-separated parts, with no kind
check — it assumes every key is `<kind>:<pr>:<sha>`.

Originally planned a bucketed key `ctxpressure:cc-master:<decile>` (3 parts, per the ticket's *"like
`ctxpressure:cc-master:<10%-bucket>`"* suggestion). Codex review (2026-07-08) caught two problems with
that:

1. **Prune collision** — a 3-part key hits `_pr_of_key`'s blind `parts[1]` extraction, yielding
   `"cc-master"` as "the PR", which is never in `open_prs` → `prune_state` deletes the entry on the
   very next prune call inside the *same* `run_once` tick that just wrote it, silently defeating the
   TTL and spamming the nudge every tick.
2. **Oscillation spam** — even with the prune bug fixed, bucketing by decile means a session hovering
   near a boundary (79.9% → 80.0% → 79.5% → 80.1% ...) mints a fresh key on every crossing and
   re-triggers the nudge repeatedly inside what should be one suppressed episode.

**Revised design: drop the bucket, key by session alone — `f"ctxpressure:{session}"` (2 parts), TTL-only
dedup.** This fixes both problems as a side effect of the key shape, with zero changes to shared pruning
code: `_pr_of_key("ctxpressure:cc-master".split(":"))` has `len(parts) == 2 < 3`, so `_pr_of_key` already
returns `None` for it (unmodified), and `prune_state`'s `pr is not None and pr not in open_set` check is
then `False` — the entry ages out by TTL alone, which is exactly the semantics a session-keyed (not
PR-keyed) signal needs. `_pr_of_key`/`prune_state` are **not touched**.

## Implementation

**File: `scripts/dispatch/context_probe.py`** — no changes (only consumed).

**File: `scripts/dispatch/gating_watcher.py`**

1. `from scripts.dispatch import context_probe` (module import, not the CLI-script subprocess route —
   satisfies "do not shell out").
2. New frozen dataclass `ContextReading(session: str, ctx: int, model: str)` — the raw
   `context_probe.read_context()` output, session-tagged.
3. New pure helper:
   ```
   def context_pressure(readings: Sequence[ContextReading], threshold: float) -> list[tuple[str, float]]
   ```
   Computes `pct = 100 * ctx / MODEL_WINDOWS.get(model, DEFAULT_WINDOW)` per reading (delegates the
   window table to `context_probe`, no duplication) and returns `(session, pct)` for every reading
   `pct >= threshold`. AC-1.
4. `DEFAULT_CONTEXT_PRESSURE_THRESHOLD: float = 70.0` module constant.
5. `_CONTEXT_PRESSURE_NUDGE` template — the exact ticket wording:
   `"Context at {pct}% — checkpoint MASTER_PLAN + run the prime-master pre-reset gate; consider /clear at the next clean boundary."`
6. `run_once` gains three new keyword params, all defaulted so every existing call site/test is
   unaffected:
   - `context_reader: Callable[[], Sequence[ContextReading]] = lambda: ()`
   - `context_pressure_threshold: float = DEFAULT_CONTEXT_PRESSURE_THRESHOLD`
   - `context_pressure_ttl_s: float = DEFAULT_MASTER_TTL_S`
8. AFTER the existing PR-trigger `for trigger in triggers:` loop, before the trailing `prune_state`
   call:
   ```
   for session, pct in context_pressure(context_reader(), context_pressure_threshold):
       logger.info("context_pressure", trace_id=trace_id, session=session, pct=round(pct, 1))
       if not execute:
           continue
       key = f"ctxpressure:{session}"
       if _suppressed(state, key, now, context_pressure_ttl_s):
           continue
       outcome = send_to_session(session, _CONTEXT_PRESSURE_NUDGE.format(pct=round(pct)), runner)
       if outcome == "sent":
           logger.info("context_pressure_send", trace_id=trace_id, session=session, pct=round(pct, 1))
           state[key] = now
           persist(state)
       else:
           logger.warning("context_pressure_skip", trace_id=trace_id, session=session, reason=outcome)
   ```
   Reuses `send_to_session` verbatim (idle check + tmux injection), never touches
   `session_is_idle`/the delivery guard.
9. Update the trailing `prune_state` call's `max_ttl_s` to
   `max(master_ttl_s, worker_ttl_s, context_pressure_ttl_s)` — otherwise a customized (larger)
   context-pressure TTL could be pruned early by the coarser bound, silently shortening the dedup
   window.
10. Production context reader (used only from `tick()`, not unit-tested against real tmux — same
    posture as `fetch_open_prs`/`_resolver`):
    ```
    def _master_context_reader() -> list[ContextReading]:
        jsonl = context_probe.resolve_jsonl(MASTER_SESSION)
        if not jsonl or not os.path.exists(jsonl):
            return []
        ctx, model = context_probe.read_context(jsonl)
        return [ContextReading(MASTER_SESSION, ctx, model)]
    ```
11. `main()`: new CLI arg
    ```
    parser.add_argument(
        "--context-pressure-threshold",
        type=float,
        default=float(os.environ.get(
            "AGENT_CONTEXT_PRESSURE_THRESHOLD", DEFAULT_CONTEXT_PRESSURE_THRESHOLD
        )),
        help="Context-pressure percent threshold for the master nudge "
             "(env AGENT_CONTEXT_PRESSURE_THRESHOLD, default 70).",
    )
    ```
    (matches the `os.environ.get` convention already used in this package —
    `scripts/reconcile_board.py::load_linear_key`, `orchestrator.check_preconditions` — scripts/ is
    outside `personal_agent.config`'s "never `os.getenv()`" rule, which governs `src/`.)
12. `tick()`: pass `context_reader=_master_context_reader`,
    `context_pressure_threshold=args.context_pressure_threshold` into `run_once`.

## Tests (`tests/scripts/test_gating_watcher.py`)

- `context_pressure`: below / at / above threshold; opus/sonnet (1M) vs haiku (200k) window mapping;
  unmapped-model falls back to `DEFAULT_WINDOW`. (AC-1)
- `prune_state`: a `ctxpressure:<session>` key (2 parts, no PR association) survives an in-TTL tick and
  is dropped only once past its TTL — locks in that the 2-part key shape rides `_pr_of_key`'s existing
  `len(parts) < 3` branch safely, with no change to shared pruning code (load-bearing for AC-2/AC-3, not
  independently required by the ticket text, but the mechanism silently breaks without it — see the
  design decision above).
- `run_once` + context reader (fake, no real tmux):
  - logs `context_pressure` every tick regardless of `execute` (dry-run observability). (AC-3)
  - `execute=False` sends nothing. (AC-3)
  - `execute=True` + idle + over-threshold + not-deduped → exactly one nudge send-keys call with the
    ticket's wording; dedup key recorded. (AC-2)
  - second tick, still over threshold, within TTL → suppressed (no second send). (AC-2)
  - below-threshold reading → no send.
  - busy session → no send (reuses `session_is_idle` via `send_to_session`, unmodified).
- Existing test suite (804 lines) passes unmodified — no existing call site passes the new params, so
  defaults (`context_reader=lambda: ()`) mean zero behavior change for every pre-existing scenario.
  (AC-4)

## Test command

`make test-file FILE=tests/scripts/test_gating_watcher.py`

## Explicitly out of scope (per ticket)

- No change to `session_is_idle` / the delivery guard.
- No owner-facing alert (Linear/Slack/etc.) — agent-to-agent only, via tmux send-keys to `cc-master`.
- No worker-context monitoring (`cc-build*`/`cc-adrs` are out of scope; dispatch `/clear` already
  manages worker context).

## Review

Per ticket: `/code-review` at **high** effort + `/security-review` after implementation (touches
subprocess/file reads), findings fixed on-branch before PR.

## Addendum — findings from the high-effort code-review workflow (2026-07-08)

Two CONFIRMED findings, both fixed on-branch:

1. **Unguarded `float()` on `AGENT_CONTEXT_PRESSURE_THRESHOLD`** — a malformed env value crashed
   the whole watcher process at argparse-build time (before `main()` even reaches `parse_args()`),
   killing all three trigger kinds, not just context-pressure. Fixed with
   `_context_pressure_threshold_default()`: a safe parse that falls back to
   `DEFAULT_CONTEXT_PRESSURE_THRESHOLD` and warns on stderr instead of raising.
2. **Context-pressure bypassed `trigger_ledger`** — unlike the two PR triggers, the context-pressure
   send had no crash-safety backup for the narrow window between `send_to_session` returning
   `"sent"` and `persist(state)` completing. Initially scoped out (see reasoning that was here and is
   now superseded below), then folded in per owner instruction after they asked what a deferred
   follow-up would actually involve. Required generalizing `trigger_ledger.prune_ledger`'s open-PR
   eviction to skip non-numeric (`ticket.isdigit()`) tickets — a session-keyed entry like
   `ticket="cc-master"` can never be a PR number, so the old unconditional check pruned it within
   the same tick it was written, before a crash could ever benefit from the protection. This is the
   `_pr_of_key`/`prune_state` design decision (§ above) recurring one layer down, in
   `scripts/dispatch/trigger_ledger.py`. Both the fix and the context-pressure block's ledger wiring
   (mirroring the PR-trigger loop's `record_pending`/`mark_send_started`/`mark_sent`/`mark_consumed`
   sequence) are in this PR, along with regression tests in both `test_gating_watcher.py` and
   `test_trigger_ledger.py`.

A stray follow-up ticket (FRE-849) was briefly filed for item 2 before the owner asked for it to be
folded into this PR instead; it was canceled rather than left dangling.
