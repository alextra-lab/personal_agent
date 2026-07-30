# FRE-867 — surface a seat parked on a permission prompt to master

**Ticket:** FRE-867 (Approved, Tier-2:Sonnet, stream:build2)
**Backing context:** MASTER_PLAN §0/§8 (not a formal ADR — this composes with the existing
event-driven gating watcher, FRE-823/845/939, no new architectural surface).

**Revision note:** this plan went through one round of codex plan-review before implementation.
The review found six real issues in the first draft; every fix below responds to a specific
finding (marked **[codex]**).

**Post-implementation security-review finding (fixed on-branch):** the `security-review` skill
flagged that `_PERMISSION_STALL_NUDGE` was the first payload in this module to embed multi-line
text (`{snippet}`) into a `tmux send-keys -l` call — every other trigger here is single-line.
`send-keys -l` sends embedded newlines as literal bytes with no bracketed-paste framing, so an
embedded `\n` risks the target's input box treating it as a premature submit, splitting the
safety preamble from the payload it must travel with. Fixed by flattening the snippet
(`snippet.replace("\n", " | ")`) and removing the template's own `\n\n` separator, so the entire
nudge — preamble and pane content together — is guaranteed to arrive as the single atomic message
every other trigger in this file already relies on. Locked in by
`test_run_once_permission_stall_nudge_never_embeds_a_raw_newline`.

## Scope recap

The allowlist half of FRE-867 already shipped (PRs #492/#749/#751/#752). This ticket owns the
**robust half**: today, if a worker seat (`cc-1build`/`cc-2build`/`cc-adrs`) hits a Claude Code
permission-confirmation dialog for a command the settings allowlist doesn't cover, the seat just
hangs — nothing notices until a human happens to look (FRE-860 sat ~18h; 2026-07-30 both build
seats sat ~16.5h, one on exactly this class). `scripts/dispatch/gating_watcher.py` already polls
every worker pane every tick (60s) for the idle/busy heuristic in `pane_state.py`, and that
heuristic already treats `"Do you want"` / `"❯ 1"` / `"1. Yes"` / `"No, and tell"` as one
generic "busy" bucket (`_BUSY_MARKERS`) — indistinguishable from an ordinary in-progress spinner.
This ticket adds a **distinct** classification for the permission-prompt case specifically, and
surfaces it to master unconditionally (not gated on master's own idle state — the module's own
FRE-939 lesson: idle-gating a master notification is exactly what let master go uninformed
before).

**Non-goal (ticket-explicit):** nothing is auto-approved beyond the existing settings allowlist.
Master's job on receiving the nudge is to relay the named command to the **owner** as a decision
— never to approve/deny it unilaterally, and never to guess which numbered option means what.

## Acceptance criteria (from the ticket body)

1. A worker seat blocked on a permission prompt is surfaced to master within one watcher cycle
   (≤60s poll interval), naming the seat and the exact command awaiting confirmation.
2. Nothing is auto-approved beyond the existing settings allowlist.
3. The silent-indefinite-hang failure mode is gone — the nudge fires even if master's own pane is
   mid-turn, GitHub is unreachable, or a *different* stall on the same seat was already notified
   and is still within its dedup window.

## Files touched

1. `scripts/dispatch/pane_state.py` — new pure function.
2. `scripts/dispatch/gating_watcher.py` — new watcher check wired into `run_once`, a production
   IO-seam reader, CLI plumbing.
3. `tests/scripts/test_gating_watcher.py` — new tests (this file already covers pane_state via
   gating_watcher's re-export; no new test file).
4. `.claude/skills/master/SKILL.md` — one short paragraph: how master reacts to the new nudge
   (relay to owner, don't decide; how to answer once told).

## Step 1 — `pane_state.py`: `permission_prompt_snippet`

**[codex #3]** A bare `"Do you want"` substring match over the trailing 30-line active region is
not safe enough to wake master *unconditionally*: `pane_state.py`'s own comments note a completed
turn's response prose routinely contains phrasing that overlaps a marker word, and a recent,
short reply ending in a rhetorical "...do you want me to also update the tests?" would sit inside
the same trailing window `session_is_idle` already uses — that's a live false-positive risk for a
generic busy check (low cost: a missed interrupt opportunity) but becomes an active false alarm
once it unconditionally wakes master. Fix: require the marker **and** a numbered-option line in
the same region — a genuine permission dialog always renders one, rhetorical prose essentially
never does immediately below it. This is proportionate to this codebase's existing idiom (an
anchored regex, like `_BUSY_SPINNER_RE`), not a full box-structure parser, which would be
over-engineering for a Tier-2 ticket and fragile against a Claude Code UI change.

Add, after `session_is_idle`:

```python
# The one substring a permission-confirmation dialog always renders that no
# other busy state does (spinner/"esc to interrupt"/"Running…"/"Compacting"
# never say this) -- LAST_SESSION.md 2026-07-30 confirms every real captured
# stall ends "Do you want to proceed?". Checked standalone rather than folded
# into _BUSY_MARKERS, which answers a different question (should this pane be
# interrupted right now) and must not gain a distinct-alerting responsibility.
_PERMISSION_PROMPT_MARKER = "Do you want"
# A genuine dialog always renders a numbered menu (`❯ 1. Yes`, `  2. No...`);
# ordinary response prose asking a rhetorical "do you want...?" essentially
# never does. Requiring both closes the false-positive gap a bare substring
# match leaves open for THIS use case (an unconditional master wake, not
# merely "should this pane be interrupted") -- FRE-867 codex review.
_PERMISSION_OPTION_RE: re.Pattern[str] = re.compile(r"^\s*(?:❯\s*)?\d\.\s", re.MULTILINE)


def permission_prompt_snippet(pane_text: str) -> str | None:
    """Return the pending-command context when a pane is parked on a permission prompt.

    Distinct from ``session_is_idle``'s generic busy-ness (FRE-867): only a
    permission-confirmation dialog means a human decision is required, not
    that a turn is merely in flight. Requires both the "Do you want" marker
    AND a numbered-option line within the same trailing active region (FRE-845
    scoping) -- a bare marker match alone is too permissive to unconditionally
    wake master on (see the FRE-867 codex review: a recent, short completed
    reply ending in a rhetorical question would otherwise false-positive).

    Args:
        pane_text: The ``tmux capture-pane -p`` output.

    Returns:
        The pane's trailing active-region text (verbatim, for a human to
        read before deciding) if a permission prompt is present, else
        ``None``.
    """
    region = _active_region(pane_text)
    if _PERMISSION_PROMPT_MARKER not in region:
        return None
    if not _PERMISSION_OPTION_RE.search(region):
        return None
    return region
```

Add `"permission_prompt_snippet"` to `__all__`.

**Tests** (`tests/scripts/test_gating_watcher.py`, new section `# --- permission_prompt_snippet
(FRE-867) ---` after the `session_is_idle` section, importing `permission_prompt_snippet` from
`scripts.dispatch.gating_watcher`, which will re-export it same as `session_is_idle`):
- `test_permission_prompt_snippet_none_when_idle` — `_REAL_IDLE_PANE` → `None`.
- `test_permission_prompt_snippet_none_on_busy_spinner` — `_REAL_BUSY_SPINNER_PANE` → `None`.
- `test_permission_prompt_snippet_none_on_generic_busy` — `_BUSY_PANE` (`"esc to interrupt"`) →
  `None`.
- `test_permission_prompt_snippet_present_for_prompt` — synthetic pane with a `Bash(...)` line +
  `"Do you want to proceed?"` + numbered options → snippet is not `None` and contains both.
- `test_permission_prompt_snippet_present_on_tall_prompt_near_bottom` — mirrors
  `test_session_idle_false_on_tall_permission_prompt_near_bottom`'s fixture.
- `test_permission_prompt_snippet_none_when_marker_only_in_scrollback_prose` — mirrors
  `test_session_idle_true_when_marker_words_appear_only_in_scrollback_prose`'s construction
  (prose + 40 filler lines + `_REAL_IDLE_PANE`) → `None`.
- `test_permission_prompt_snippet_none_on_recent_rhetorical_question_without_options` **[codex
  #3, new]** — a short completed-turn-style reply ending `"...do you want me to also update the
  tests?"` with NO numbered-option line, sitting well within the trailing 30 lines → `None`. Locks
  in the option-line requirement against exactly the false-positive codex flagged.

## Step 2 — `gating_watcher.py`: the watcher check

**Why `require_idle=False`, not the context-pressure idiom.** `context_pressure`'s own nudge to
master calls `send_to_session(session, command, runner)` with the **default** `require_idle=True`
— a busy master pane skips the send outright (`test_run_once_context_pressure_busy_session_skips`
proves this). That is fine for a slow-changing, self-healing, low-urgency signal. It is the wrong
choice here: the module's own docstring records that idle-gating a **master** notification is
exactly the bug already fixed once ("idle detection over capture-pane is not reliable enough to
*gate* on — it kept the watcher from ever informing a busy master"). Codex's review confirmed this
call is defensible (it doesn't introduce a mechanically new injection class beyond what the
existing master-ready trigger already uses) provided the dedup/staleness gaps below are closed.

**[codex #2] Dedup key must be content-addressed, not session-only.** A `permstall:<session>` key
(no content component) conflates every stall on a seat into one 6h suppression window: if a first
prompt is answered and a **different** prompt appears 5 minutes later, the second would be
silently suppressed until the first's TTL expires — a direct violation of "within one watcher
cycle". Fix: append a short hash of the snippet, so a materially different prompt mints a fresh
key while the *same* still-pending prompt continues to dedup normally tick-to-tick. **Must NOT**
use a third `:`-separated segment — `prune_state`'s `_pr_of_key` treats any 3-plus-colon key as
`<kind>:<pr>:<sha>` and would extract the session as a bogus "PR number", find it absent from
`open_prs`, and evict the entry on the very next `prune_state` call (defeating dedup entirely,
spamming master every tick). The hash is therefore appended with a hyphen, inside the same second
segment, mirroring why `context_pressure` already uses a bare 2-part `ctxpressure:<session>` key.

Add imports: `hashlib` (stdlib), `permission_prompt_snippet` (from `pane_state`), `known_streams`
(from `launcher`, alongside the existing `topology_for` import).

Add constants (near `DEFAULT_CONTEXT_PRESSURE_TTL_S`):

```python
# One nudge per stall EPISODE (dedup key includes a content hash, not just the
# session), self-heals after 6h if the same episode is still unresolved --
# same reasoning as DEFAULT_CONTEXT_PRESSURE_TTL_S, reused rather than
# inventing a second magic number.
DEFAULT_PERMISSION_STALL_TTL_S: float = DEFAULT_MASTER_TTL_S
```

Add the nudge template (near `_CONTEXT_PRESSURE_NUDGE`). **[codex #5]** The first draft told master
to type a hardcoded `"1"` for yes / `"2"` for no — confirmed wrong: the tall-prompt fixture already
in this test file has option 2 as "Yes, and don't ask again" and option 3 as "No", so a fixed
recipe could make master *broaden* a permission instead of denying it. There is no safe universal
digit mapping — the menu's option count and order vary per prompt. Fixed by deferring entirely to
the options actually shown. **[codex, "stale replay"]** also added an explicit re-verify caveat: if
delivery was queued (master busy) and only re-offered later, the snippet embedded in the ledger
entry could be stale by the time master reads it — master must re-capture the seat's live pane
before acting, not trust the embedded text as current truth (this is also already the standing
doctrine per `docs/plans/LAST_SESSION.md`: "Read the dialog before answering — never approve
blind").

```python
_PERMISSION_STALL_NUDGE = (
    "Seat {session} is BLOCKED on a permission-confirmation prompt (FRE-867 -- the "
    "silent-hang class) -- it is waiting on a human decision, not doing autonomous "
    "work. SURFACE this to the owner as a decision: name the seat and show them the "
    "pending command below. Do NOT approve or deny it yourself -- nothing is "
    "auto-approved beyond the existing settings allowlist, and the menu's option "
    "count/order varies per prompt, so there is no safe universal digit to suggest. "
    "This snippet may be stale if delivery was delayed -- re-capture the seat's live "
    "pane (tmux capture-pane -t {pane} -p) before deciding. Once the owner decides, "
    "read the options shown live and answer in the seat's own pane: tmux send-keys -t "
    "{pane} -l \"<digit>\" then tmux send-keys -t {pane} Enter for a specific option, "
    "or bare Enter alone to accept the highlighted default. Never answer blind.\n\n"
    "{snippet}"
)
```

Add the production IO-seam reader (near `_master_context_reader`):

```python
def _capture_permission_stalls(runner: CommandRunner) -> list[tuple[str, str]]:
    """Capture every worker seat's pane and report those parked on a permission prompt (FRE-867).

    Enumerates every known dispatch stream's tmux session -- not gated on any
    open PR, since a seat can stall on a permission prompt before it has ever
    opened one (the FRE-860 failure mode). A seat that is not currently
    launched is silently skipped.

    Args:
        runner: The command runner seam (shells ``tmux``).

    Returns:
        ``(session, snippet)`` pairs for every stalled seat found this tick.
    """
    stalls: list[tuple[str, str]] = []
    for stream in known_streams():
        session = topology_for(stream).tmux_session
        if runner(["tmux", "has-session", "-t", exact_session(session)]).returncode != 0:
            continue
        pane = runner(["tmux", "capture-pane", "-t", exact_pane(session), "-p"])
        snippet = permission_prompt_snippet(pane.stdout)
        if snippet is not None:
            stalls.append((session, snippet))
    return stalls
```

In `run_once`, add parameters (after `context_pressure_ttl_s`):

```python
permission_stall_reader: Callable[[], Sequence[tuple[str, str]]] = lambda: (),
permission_stall_ttl_s: float = DEFAULT_PERMISSION_STALL_TTL_S,
```

...documented the same way `context_reader`/`context_pressure_threshold` are.

**[codex #6] Placement — must not depend on `board_fetcher()` succeeding.** The first draft placed
this loop after `prs = board_fetcher()`; a `fetch_open_prs` failure (`RuntimeError`, e.g. GitHub
unreachable) propagates out of `run_once` before that point is ever reached, so a GitHub outage
would have silently disabled permission-stall alerting too — exactly the kind of coupling this
signal must not have (it is independent of any PR/GitHub state by design). Fix: place the loop
**before** `prs = board_fetcher()`, right after the top-of-tick ledger `reconcile()` call (which
itself has no dependency on `prs` either).

```python
    if execute:
        tick_ledger = trigger_ledger.reconcile(
            tick_ledger, now=now, execute_pending=_retry_pending, persist=ledger_persist,
            logger=logger,
        )

    # --- permission-stall nudge (FRE-867) --------------------------------
    # Placed BEFORE board_fetcher(): this signal has no dependency on PR/gh
    # state, and must keep firing even when GitHub is unreachable (codex
    # review: the first draft's placement after board_fetcher() meant a
    # fetch_open_prs() failure would silently suppress this too).
    for session, snippet in permission_stall_reader():
        logger.info("permission_stall", trace_id=trace_id, session=session)
        if not execute:
            continue
        content_hash = hashlib.sha256(snippet.encode()).hexdigest()[:12]
        # Hyphen inside ONE colon-segment, not a third `:` segment -- prune_state's
        # _pr_of_key treats a 3-plus-colon key as <kind>:<pr>:<sha> and would
        # evict this every tick (session is never in open_prs), spamming master
        # (see this step's docstring note above; mirrors ctxpressure's 2-part key).
        key = f"permstall:{session}-{content_hash}"
        if _suppressed(state, key, now, permission_stall_ttl_s):
            continue
        pane_target = exact_pane(session)
        command = _PERMISSION_STALL_NUDGE.format(session=session, pane=pane_target, snippet=snippet)
        tick_ledger, record_outcome = trigger_ledger.record_pending(
            tick_ledger,
            event_id=key,
            source="permission-stall",
            target_pane=MASTER_SESSION,
            ticket=session,  # non-numeric -- ages out by TTL, not open-PR closure (mirrors ctxpressure)
            command=command,
            preconditions={},
            now=now,
            ttl_s=permission_stall_ttl_s,
        )
        ledger_persist(tick_ledger)
        if record_outcome == "duplicate":
            logger.warning(
                "permission_stall_skip", trace_id=trace_id, session=session,
                reason="tick_ledger-duplicate",
            )
            continue
        tick_ledger = trigger_ledger.mark_send_started(tick_ledger, key, now)
        ledger_persist(tick_ledger)

        def _record_queued_permstall(event_id: str = key) -> None:
            nonlocal tick_ledger
            tick_ledger = trigger_ledger.mark_queued(tick_ledger, event_id, now)
            ledger_persist(tick_ledger)

        outcome = send_to_session(
            MASTER_SESSION, command, runner, require_idle=False, on_queued=_record_queued_permstall
        )
        if outcome == "queued":
            logger.warning(
                "permission_stall_unconfirmed", trace_id=trace_id, session=session,
                reason="master-busy",
            )
        elif outcome == "sent":
            logger.info("permission_stall_send", trace_id=trace_id, session=session)
            tick_ledger = trigger_ledger.mark_sent(tick_ledger, key, now)
            ledger_persist(tick_ledger)
            state[key] = now
            persist(state)
            tick_ledger = trigger_ledger.mark_consumed(tick_ledger, key, now)
            ledger_persist(tick_ledger)
        else:
            logger.warning(
                "permission_stall_skip", trace_id=trace_id, session=session, reason=outcome
            )
            tick_ledger = trigger_ledger.mark_consumed(tick_ledger, key, now)
            ledger_persist(tick_ledger)

    prs = board_fetcher()
    ... # unchanged from here
```

Update the final `prune_state` call's `max_ttl_s` to include `permission_stall_ttl_s`:

```python
max_ttl_s=max(master_ttl_s, worker_ttl_s, context_pressure_ttl_s, permission_stall_ttl_s),
```

**CLI wiring** in `main()`: add `--permission-stall-ttl` (float, default
`DEFAULT_PERMISSION_STALL_TTL_S`), and in `tick()`'s `run_once(...)` call add
`permission_stall_reader=lambda: _capture_permission_stalls(subprocess_runner)` and
`permission_stall_ttl_s=args.permission_stall_ttl`.

**Tests** (new section `# --- run_once: permission-stall nudge (FRE-867) ---`, mirroring the
context-pressure `run_once` tests but proving the properties codex's review specifically pushed
on):
- `test_run_once_permission_stall_logs_regardless_of_execute`
- `test_run_once_permission_stall_dry_run_sends_nothing`
- `test_run_once_permission_stall_sends_nudge_naming_seat_and_command` — asserts the send-keys
  payload contains the session name and the snippet text, targets `exact_pane(MASTER_SESSION)`,
  and contains no hardcoded "1"/"2" recipe (regression guard against codex finding #5).
- `test_run_once_permission_stall_sent_even_when_master_pane_busy` — runner returns a busy
  `capture-pane` for `cc-master`; assert a `send-keys` call still happens (unlike
  `test_run_once_context_pressure_busy_session_skips`) and the ledger entry ends up `queued` (via
  `snapshot_unconsumed`), not silently dropped.
- `test_run_once_permission_stall_dedup_suppresses_same_snippet_second_tick_within_ttl`
- `test_run_once_permission_stall_re_arms_after_ttl`
- `test_run_once_permission_stall_distinct_snippet_notifies_again_within_ttl` **[codex #2, new]**
  — same seat, first snippet sent; second tick with a *different* snippet (a new, distinct stall
  episode) within the TTL window → still sends (proves episodes aren't conflated).
- `test_run_once_permission_stall_still_sent_when_board_fetcher_fails` **[codex #6, new]** —
  `board_fetcher` raises `RuntimeError`; assert the permission-stall send-keys call happened before
  the exception propagates (proves the decoupling from GitHub reachability).
- `test_run_once_permission_stall_ledger_records_and_consumes_on_successful_send`
- `test_run_once_permission_stall_defaults_do_not_affect_pr_only_ticks` — no
  `permission_stall_reader` passed → zero extra send-keys calls (regression guard, mirrors the
  existing context-pressure equivalent).

Add a small section `# --- _capture_permission_stalls (FRE-867) ---`:
- `test_capture_permission_stalls_finds_stalled_seat_skips_absent_and_idle` — three known streams
  (`adr`→`cc-adrs` absent, `build1`→`cc-1build` stalled, `build2`→`cc-2build` idle) via
  `_RecordingRunner`; assert result is exactly `[("cc-1build", <snippet containing "Do you
  want">)]` and no `capture-pane` call was made for the absent session.

**Accepted limitation (not fixed here):** no *real* captured permission-dialog fixture exists (the
FRE-825 real-pane fixtures only cover idle/busy-spinner). This matches existing precedent —
`test_session_idle_false_on_permission_prompt` already ships with a synthetic fixture only, not a
deviation this ticket introduces. A future ticket can promote a real capture once one exists from
an actual incident.

## Step 3 — `.claude/skills/master/SKILL.md`

Add one short paragraph after Step 1's existing "Gating PR #X" paragraph:

> **A permission-stall nudge** (`Seat <session> is BLOCKED on a permission-confirmation prompt...`,
> FRE-867) is not a PR gate — relay the named seat and pending command **to the owner** as a
> decision; never approve or deny it yourself, and never guess which numbered option means what
> (the menu varies per prompt). Re-capture the seat's live pane before acting if the nudge arrived
> late. Once the owner decides, answer in the seat's own pane per the recipe the nudge carries.

## Test commands

```bash
make test-file FILE=tests/scripts/test_gating_watcher.py
make test-k K=permission
make mypy
make ruff-check
make ruff-format
pre-commit run --all-files
```

## Acceptance-criteria proof mapping (for the master handoff)

| AC | Evidence |
|----|----------|
| Surfaced within one cycle, naming seat + command | `test_run_once_permission_stall_sends_nudge_naming_seat_and_command` |
| Nothing auto-approved beyond the allowlist | No new tool/permission logic touched; nudge text explicitly defers to the owner and the live-shown options (code review of the nudge template + master skill doc) |
| Silent-indefinite-hang mode gone: unconditional even if master busy, GitHub down, or a distinct prior stall is within its dedup window | `test_run_once_permission_stall_sent_even_when_master_pane_busy`, `test_run_once_permission_stall_still_sent_when_board_fetcher_fails`, `test_run_once_permission_stall_distinct_snippet_notifies_again_within_ttl` |
