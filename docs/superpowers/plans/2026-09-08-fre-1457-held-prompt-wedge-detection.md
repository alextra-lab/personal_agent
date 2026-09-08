# FRE-1457 — detect a seat held at an interactive prompt

## Problem

`seat_wedge_signature` (`scripts/dispatch/launcher.py:1347`) fires only when Remote Control
reports a seat busy AND `session_is_idle` reads the pane as idle (the bare-caret prompt). That
catches one wedge shape: an orphaned background poller (CC #61568).

It misses a second shape, measured live on 2026-09-06: a seat holds a modal confirmation
(`Switch model? … 1. Yes … 2. No`). `session_is_idle` correctly reads this pane as **not** idle —
its own busy-marker list already contains `"1. Yes"`, `"Do you want"`, etc. (`pane_state.py:42`),
by design, so a pending decision prompt is never mistaken for readiness. But that same correct
reading is exactly why `seat_wedge_signature`'s `idle AND busy` test never holds for a held
prompt: not-idle reads as "busy", and "busy" was never distinguished from "genuinely
progressing". Measured: 38 `outcome=seat-busy` ticks, 0 `dispatch_seat_wedged` entries, 3h20m
silent.

## Design (revised after codex plan-review — see `## Codex plan-review` below)

Add a second signature, discriminated from a genuine in-progress turn the same way the pane
markers already are: a live turn's status line (`_BUSY_SPINNER_RE`) advances every capture
(elapsed time, token count); a held decision prompt is static. So:

- **held-prompt** = RC busy AND the pane's active region contains the live TUI's own
  selection-cursor line for a numbered choice — `^\s*❯\s*\d+[.)]` (anchored, mirroring
  `_IDLE_PROMPT_RE`/`_BUSY_SPINNER_RE`'s existing anchored-line style) — AND the live spinner does
  NOT match. This is a *structural* signature (the ❯ glyph is the TUI's own render for the
  currently-selected option; ordinary response prose does not emit a leading ❯), deliberately
  narrower than a bare-substring check over `_BUSY_MARKERS` — codex confirmed the existing
  regression fixture (`tests/scripts/test_gating_watcher.py:223-241`) proves "Do you want"/"1.
  Yes"/"No, and tell" all appear in ordinary completed-turn response prose, so a substring check
  over those words would false-positive on a busy seat whose recent reply happens to discuss a
  yes/no decision. The anchor is the fix.
- **pane-idle** = the existing signature, unchanged (AC-3).

Both are computed from ONE `capture-pane` read per tick (a SEPARATE `seat_is_busy` RC read is
still performed inside `seat_wedge_reason`, same as the pre-existing `seat_wedge_signature` did —
codex noted the plan's original "one RC read" framing undercounted this; it is an accepted,
pre-existing redundancy, not new to this change) via a new `seat_wedge_reason()` that classifies
into a small discriminated result (see below). `seat_wedge_signature` becomes a thin wrapper over
it — same public symbol, unchanged behavior, existing truth-table test
(`test_seat_wedge_signature_truth_table`) is the regression proof for AC-3.

`seat_wedge_reason` returns `SeatWedgeSignal | None` — a frozen dataclass with
`reason: Literal["pane-idle", "held-prompt"]` and `prompt_summary: str | None` (populated only for
`"held-prompt"`, from a new `pane_state.held_prompt_summary()` that returns the header + selector
lines around the ❯ match, capped at 200 chars). Carrying the actual matched text (not just the
`"held-prompt"` label) is what closes codex's AC-2 gap: `reason` alone tells master WHICH kind of
wedge, but not what the prompt says — `prompt_summary` is the "master acts without opening the
pane" content itself.

**Correctness gap codex found and this revision fixes:** the original plan reused the *same*
`WedgeState` counter for both reasons unconditionally. Two pane-idle ticks followed by one
held-prompt tick would then cross `wedge_ticks=2` and notify as `held-prompt` off a single
held-prompt observation — breaking AC-1's persistence guarantee, and symmetrically weakening AC-3
on the reverse transition (a stale `last_notified_count` inherited from the old reason could delay
the new reason's due notification). Fix: `WedgeState` gains a `reason` field; `_note_wedge` treats
a reason CHANGE as an episode boundary — resolve the (now-stale) ledger entry, then start the new
reason's count at 1, exactly mirroring `_reset_wedge`'s existing resolve-before-persist ordering.
The stable per-stream ledger key (`dispatch-notify:dispatch_seat_wedged:{stream}`) is unchanged, so
AC-5's bounded-entry-count property still holds by construction — a reason change still produces
at most one open entry per stream, just correctly re-armed rather than silently carried over.

`_note_wedge` threads `reason` and `prompt_summary` (when present) into both the greppable log
warning's `detail` (two fixed strings, keyed by reason) and the `notifier(...)` call's
`reason`/`prompt_summary`/`outcome` fields.

## Files

1. **`scripts/dispatch/pane_state.py`**
   - Add `_HELD_PROMPT_CHOICE_RE = re.compile(r"^\s*❯\s*\d+[.)]", re.MULTILINE)` and
     `_PROMPT_SUMMARY_MAX_CHARS = 200`.
   - Add `held_prompt_summary(pane_text: str) -> str | None`: `None` if `_BUSY_SPINNER_RE` matches
     the active region; else, if `_HELD_PROMPT_CHOICE_RE` matches the active region, the trailing
     lines up to and including the matched line, joined, capped at `_PROMPT_SUMMARY_MAX_CHARS`;
     else `None`. Full Google-style docstring (Args/Returns).
   - Export `held_prompt_summary` in `__all__`.

2. **`scripts/dispatch/launcher.py`**
   - Import `held_prompt_summary` alongside the existing `session_is_idle` import.
   - Add `@dataclasses.dataclass(frozen=True) class SeatWedgeSignal` with `reason:
     Literal["pane-idle", "held-prompt"]` and `prompt_summary: str | None = None`.
   - Add `seat_wedge_reason(topology, runner) -> SeatWedgeSignal | None`: one `seat_is_busy`
     check, one `_capture_pane`, classify via `session_is_idle` then `held_prompt_summary`. Full
     Google-style docstring (Args/Returns), explicitly noting the ❯-anchor rationale and that this
     performs its own RC read (not reusing the caller's).
   - Redefine `seat_wedge_signature` to delegate: compute `signal = seat_wedge_reason(...)`,
     return `signal is not None and signal.reason == "pane-idle"` (docstring keeps its CC #61568
     framing; add a `See also` pointer to `seat_wedge_reason`).

3. **`scripts/dispatch/orchestrator.py`**
   - Swap the `seat_wedge_signature` import for `seat_wedge_reason`.
   - Replace the `if result.outcome == "seat-busy" and seat_wedge_signature(...)` call site
     (line ~1273) with `wedge_signal = seat_wedge_reason(...) if result.outcome == "seat-busy"
     else None`, branching `_note_wedge(..., reason=wedge_signal.reason,
     prompt_summary=wedge_signal.prompt_summary, ...)` / `_reset_wedge(...)` on
     `wedge_signal is not None`.
   - `WedgeState`: add `reason: str = "pane-idle"` (default keeps old persisted-state files
     loadable — `load_wedge_state`'s `WedgeState(**value)` call fills the missing key). Extend
     `load_wedge_state`'s validation to drop a record whose `reason` is not one of the two known
     values (same fail-safe-drop pattern as the existing count/last_notified_count checks).
   - `_note_wedge`: add required `reason: str` and `prompt_summary: str | None` kwargs, plus
     `notify_ledger_path: Path | None` (for the reason-boundary resolve). At the top: `if prior is
     not None and prior.reason != reason: _resolve_dispatch_notify(notify_ledger_path, logger,
     "dispatch_seat_wedged", stream); prior = None` — then the existing count/notify logic
     proceeds unchanged from a reset `prior`. Add a module-level `_WEDGE_DETAIL: dict[str, str]`
     mapping `"pane-idle"`/`"held-prompt"` → their `detail` strings; pass `reason=reason`,
     `prompt_summary=prompt_summary`, `outcome="seat-busy"` through to both `logger.warning` and
     `notifier(...)`. `WedgeState(count, ..., reason)` on the final assignment.
   - Update the one existing call site to pass `notify_ledger_path=notify_ledger_path`.

## Tests (TDD — failing first)

`tests/scripts/test_launcher.py`:
- `test_seat_wedge_reason_classifies_the_held_prompt_shape` — truth table on `seat_wedge_reason`:
  idle pane → `SeatWedgeSignal("pane-idle")`; spinner pane → `None`; a modal-confirmation pane
  (new fixture `_PROMPT_PANE`, with a `❯ 1. Yes, switch to Opus 5` line) → `SeatWedgeSignal`
  with `reason == "held-prompt"` and a non-empty `prompt_summary`; RC idle → `None`; RC
  unreadable → `None`.
- `test_response_prose_containing_prompt_words_is_never_a_held_prompt` (codex Focus 1, the false-
  positive this revision exists to close): a pane whose active region contains ordinary completed-
  turn prose using "Do you want"/"1. Yes"/"No, and tell" (mirroring
  `tests/scripts/test_gating_watcher.py`'s own such fixture) but NO `❯ <n>.` selector line →
  `seat_wedge_reason` returns `None`.
- Existing `test_seat_wedge_signature_truth_table` is the AC-3 regression proof — must still pass
  unmodified.

`tests/scripts/test_orchestrator.py`:
- New `_WEDGE_PROMPT_PANE` fixture text (mirrors `_WEDGE_IDLE_PANE`/`_WEDGE_BUSY_PANE`), and a
  `_WedgeRunner` extension accepting a `pane_sequence` (list of per-capture pane texts, cycling to
  the last on exhaustion) alongside the existing static `pane`.
- `test_held_prompt_is_surfaced_with_its_own_reason` (AC-1 + AC-2): ticks `1..wedge_ticks` on the
  prompt pane assert ZERO notifications; the crossing tick asserts exactly one
  `dispatch_seat_wedged` event with `reason == "held-prompt"`, a non-empty `prompt_summary`, and
  `outcome == "seat-busy"`.
- `test_held_prompt_reaches_notify_ledger` (AC-1, mirrors `test_wedge_reaches_notify_ledger`):
  assert the entry is retrievable via `trigger_ledger.snapshot_unconsumed`.
- Extend `test_wedge_is_surfaced_past_threshold_and_never_killed`'s assertions to check
  `reason == "pane-idle"` on the existing pane-idle event — proves the two reasons stay distinct.
- `test_advancing_spinner_pane_is_never_a_wedge_across_ten_ticks` (AC-4, the ticket's own
  wording — "at least ten ticks", codex Focus 5: must actually ADVANCE, not repeat one static
  string): a `pane_sequence` of 10 spinner captures with increasing elapsed-time/token text;
  assert zero `dispatch_seat_wedged` events/warnings and `wedge_state` empty.
- `test_wedge_reason_change_resets_the_episode` (codex Focus 3, the correctness gap): two
  pane-idle ticks (count reaches 2, below `wedge_ticks=2`'s threshold), then the pane switches to
  the prompt shape — assert the NEXT tick's `WedgeState.count == 1` and `.reason ==
  "held-prompt"` (reset, not carried over) and no notification fired yet; two more held-prompt
  ticks then cross the threshold and notify with `reason == "held-prompt"`. Mirrors
  `test_wedge_ping_is_once_per_episode_and_re_fires_on_a_new_episode`'s pattern of mutating
  `runner._pane` mid-run.
- `test_held_prompt_renotify_schedule_bounded_over_ten_ticks` (AC-5, codex Focus 5 — the existing
  bounded-ledger test only exercises the pane-idle reason): mirrors
  `test_wedge_renotify_schedule_bounded_over_ten_ticks` with the prompt pane; assert `len(ledger)
  == 1` AND the entry's `preconditions["reason"] == "held-prompt"`.

## Quality gates

`make test` (new tests green, full suite unaffected) · `make mypy` · `make ruff-check` +
`make ruff-format` · `pre-commit run --all-files`.

## Risk tier

Standard — touches `scripts/dispatch/` production dispatch-orchestration logic across 3 files,
gates a live master-notification path. Codex plan-review required before implementation.

## Codex plan-review

Task `task-mtsp70f2-ngzpyk`, session `01a0812d-fabb-7f22-8c43-67040c18a4c2`. 10 findings.
Accepted and folded into the `## Design`/`## Files`/`## Tests` sections above:

- **Focus 1 (accepted, design change):** the original `_PROMPT_MARKERS` substring set
  (`"Do you want"`, `"1. Yes"`, `"No, and tell"`) is exactly the marker set
  `tests/scripts/test_gating_watcher.py:223-241` already proves appears in ordinary completed-turn
  response prose. Replaced with the anchored `❯ <n>.` selector-line regex — a structural signal,
  not a word list.
- **Focus 2 (accepted, doc-only correction):** the plan's "ONE capture-pane + ONE claude agents
  read per tick" claim was inaccurate — `seat_wedge_reason` performs its own `seat_is_busy` read,
  same as the pre-existing `seat_wedge_signature` always did. No code change; corrected in
  `## Design` above. Codex explicitly found no issue with the one-pane-capture design itself.
  Codex's third Focus-2 item (the RC read that already happened in `deliver_to_seat` before this
  path is reached) is a pre-existing structural cost outside this ticket's scope — not folded in,
  per the fold-in-vs-file-a-ticket rule: it is a genuinely separate, sequenceable optimization
  (share structured busy evidence out of `deliver_to_seat`), not something this ticket's function
  needs to work.
- **Focus 3 (accepted, the correctness gap):** shared `WedgeState` counter across a reason change
  would under-count the persistence guarantee. Fixed via a `reason` field on `WedgeState` and an
  episode-boundary reset in `_note_wedge`. New transition test added.
- **Focus 4 (accepted, design change):** `reason` alone doesn't tell master WHAT the prompt says.
  Added `prompt_summary` to `SeatWedgeSignal`/the notify payload.
- **Focus 5 (accepted, test-plan changes):** AC-1's test now asserts zero-then-one across the
  threshold explicitly (not just "eventually fires"); AC-4's fixture now uses a genuinely
  advancing pane sequence, not one static spinner string repeated; AC-5 gets a held-prompt-specific
  bounded-ledger test.
- **Coding-standard gap (accepted):** both new public functions get full Google-style docstrings.

Not folded in (Focus 1's second item, and the `deliver_to_seat`-sharing sub-item of Focus 2): both
are hypotheses about future/adjacent conditions with no concrete repro in this codebase today —
flagged here for a future ticket if a real captured pane ever demonstrates a miss, per this
project's "a true pattern is not licence to file every instance" convention.
