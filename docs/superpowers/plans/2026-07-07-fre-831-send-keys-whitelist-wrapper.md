# FRE-831 — send-keys whitelist wrapper + pane attestation (ADR-0113)

Backing ADR: ADR-0113 §2 (actuation autonomy — `send-keys` whitelist). Blocked by FRE-829 (trigger
ledger, merged — PR #427); blocks the future master/watcher rewiring ticket that routes real sends
through this wrapper. **Revision 2** — folds in a codex plan-review pass (findings below).

## Scope decision (flagged for master)

The ticket's closed command grammar is exactly `/build <valid-id>` and `/prime-worker` (ADR §2 +
FRE-831 body — it does **not** include `/master <PR#>`, which the live `gating_watcher.py` also
sends to `cc-master`). **Why that's intentional, not a gap (codex finding #6, resolved by
clarification):** this wrapper exists specifically to gate *LLM-driven* actuation — the ADR's own
rationale is "an LLM master can rationalize intent into a whitelisted command." The watcher's
`/master` trigger is emitted by a *dumb, contextless sensor* (ADR §1) that is not an LLM and cannot
rationalize anything; it already gets its crash-safety from the FRE-829 ledger. So `/master` has no
role in *this* mechanism and is correctly excluded — this wrapper's job is master's own future sends
*to workers*, the one actuation path an LLM actually drives. The module docstring states this
explicitly so a reader doesn't need to rediscover the reasoning.

This ticket builds the **wrapper module only** — a pure grammar/pane validator plus a
ledger-integrated send function — and does **not** rewire `gating_watcher.py` or any master skill to
call through it. `gating_watcher.py` keeps sending directly via its own `send_to_session` (unchanged,
per FRE-829's documented scope-out) until a follow-up ticket consolidates actuation through this
wrapper. This keeps the PR to one phase and matches the ticket's literal "what to build."

## What to build

### New module `scripts/dispatch/send_keys_whitelist.py`

A non-LLM grammar parser + target-pane attestation gate in front of `tmux send-keys`, reusing the
existing `send_to_session` tmux primitive (`gating_watcher.py`) and the existing trigger ledger
(`trigger_ledger.py`) so an approved send is tied to a durable ledger event (FRE-829).

**Closed grammar** — mirrors the real `/build` skill contract (a stream selector `1`/`2`, or an
explicit `FRE-<digits>` id) and the exact `/prime-worker` literal, nothing else:

```python
@dataclasses.dataclass(frozen=True)
class BuildCommand:
    arg: str  # "1", "2", or "FRE-<n>" (ASCII digits only)

@dataclasses.dataclass(frozen=True)
class PrimeWorkerCommand:
    pass

ParsedCommand = BuildCommand | PrimeWorkerCommand

_BUILD_RE = re.compile(r"/build (1|2|FRE-[0-9]+)")   # matched with .fullmatch(), see below
_PRIME_WORKER_RE = re.compile(r"/prime-worker")       # matched with .fullmatch()

def parse_command(text: str) -> ParsedCommand | None: ...
```

**Codex finding #3 (blocking) — parser hardening.** Match with `pattern.fullmatch(text)` (no
`^`/`$` anchors at all — `fullmatch` already requires the whole string to match, and unlike `$`,
carries no "matches just before a trailing `\n`" quirk). Character classes are explicit ASCII
(`[0-9]`), never `\d`/`\s`/`.` — Python's `\d` matches Unicode digits (fullwidth, Arabic-indic,
etc.) by default, which would let a lookalike ticket id slip through the grammar and then reach
`tmux send-keys` as literal (and misleading) text. No whitespace tolerance, no case normalization
on `FRE-`. This combination also closes the newline/control-character/multi-line-payload risk for
free: any embedded `\n`, `\r`, `\t`, or non-ASCII byte fails `fullmatch` outright, since the pattern
has no character class that admits it.

**Pane attestation** — derived from the existing launcher topology (`launcher.topology_for`), not
re-declared, so it can never drift from the real worker sessions:

```python
_ATTESTED_STREAMS: tuple[str, ...] = ("build1", "build2", "adr")

def attested_panes() -> frozenset[str]:
    return frozenset(topology_for(s).tmux_session for s in _ATTESTED_STREAMS)
```

**Codex finding #2 (blocking) — cross-field validation, not just membership.** `attested_panes()`
alone is not enough: `cc-adrs` is a real, attested pane, but its skill contract is `/adr`, not
`/build` (`launcher._TOPOLOGY["adr"].skill_command == "/adr"`) — a naive pane-membership check would
wrongly approve `/build FRE-471` sent at `cc-adrs`. Validation is command-role-aware:

```python
_BUILD_PANES = frozenset({"cc-build", "cc-build2"})       # topology_for("build1"/"build2").tmux_session
_PRIME_WORKER_PANES = frozenset({"cc-build", "cc-build2", "cc-adrs"})  # any worker stream

def _panes_for(command: ParsedCommand) -> frozenset[str]:
    return _BUILD_PANES if isinstance(command, BuildCommand) else _PRIME_WORKER_PANES
```

(`_BUILD_PANES`/`_PRIME_WORKER_PANES` are still derived from `topology_for(...)`, never hand-typed
literals duplicating `launcher._TOPOLOGY` — the literals above are illustrative only.)

**Pure validation** (the AC-3/AC-10 proof surface — no IO):

```python
RefusalReason = Literal["ungrammatical", "unattested-pane"]

@dataclasses.dataclass(frozen=True)
class Refusal:
    reason: RefusalReason
    pane: str
    text: str

@dataclasses.dataclass(frozen=True)
class Approved:
    command: ParsedCommand
    pane: str
    text: str

def validate(pane: str, text: str) -> Approved | Refusal:
    """Grammar first (the largest attack surface), then command-role-aware pane attestation."""
```

`validate()` refuses `"unattested-pane"` both for a pane outside `attested_panes()` entirely *and*
for an attested pane that is real but wrong for the parsed command's role (e.g. `/build` at
`cc-adrs`) — same reason code, since both are "this pane may not receive this command."

**Ledger-integrated send** — refuses *before* any ledger write or keystroke; an approved send
follows the same ledger-before-send/consumed-after pattern `gating_watcher.run_once` already uses,
so "a send is tied to a ledger event" (ticket body) holds for every call through this wrapper:

```python
SendResult = Literal["sent", "busy", "absent", "refused", "ledger-duplicate", "kill-switch"]

@dataclasses.dataclass(frozen=True)
class SendOutcome:
    result: SendResult
    pane: str
    text: str
    reason: str | None = None

def send(
    pane: str,
    text: str,
    *,
    event_id: str,
    source: str,
    ticket: str,
    preconditions: Mapping[str, str],
    now: float,
    ttl_s: float,
    ledger: trigger_ledger.Ledger,
    ledger_persist: Callable[[trigger_ledger.Ledger], None],
    runner: CommandRunner,
    logger: Logger,
    trace_id: str | None = None,
    kill_switch_engaged: Callable[[], bool] = lambda: False,
) -> tuple[trigger_ledger.Ledger, SendOutcome]:
```

**Codex finding #4 (blocking, resolved via explicit contract, not a redesign) — `event_id` is a
caller-supplied idempotency key, documented as such.** The wrapper cannot derive a correct dedup key
itself — it does not know the caller's `(kind, PR#, head-SHA)` triple (or equivalent) that makes a
key collision-safe, only `pane`/`text`/`ticket`. `send()`'s docstring states the contract explicitly:
"`event_id` must be a key that is stable for retries of the *same* logical trigger and distinct
across logically different triggers — mirror `gating_watcher`'s `<kind>:<pr>:<sha>` pattern.
`send()` trusts this key; it does not attempt to derive or validate it." A test proves the mechanism
(same `event_id` twice while unconsumed → second call is `"ledger-duplicate"`, no second `tmux`
call) without overclaiming semantic correctness the wrapper cannot see.

**Codex finding #5 (non-blocking) — exception propagation after `mark_send_started`, made
explicit.** `send()` does **not** wrap the `send_to_session(...)` call in `try/except` — this
mirrors `gating_watcher.run_once`'s existing behavior exactly (it does not catch there either; only
`trigger_ledger.reconcile`'s retry path does). If the runner raises, the exception propagates to
`send()`'s caller *after* `mark_send_started` has already been persisted, so the ledger entry is
left in the "started, never confirmed sent" state — exactly the state a later `reconcile()` call
(owned by the caller's own tick loop, not by `send()`) surfaces as `surfaced_at` for owner
intervention. `send()` deliberately does **not** call `reconcile()` itself — reconciliation is a
per-tick, caller-owned step (as it is today in `gating_watcher.tick()`), not a per-send one. The
module docstring states this pairing requirement explicitly.

**Codex finding #7 (non-blocking) — kill-switch predicate, defense in depth.** `send()` accepts an
optional `kill_switch_engaged` predicate (default always-`False`, matching `run_once`'s shape). When
engaged, `send()` refuses immediately — before grammar/pane validation, before any ledger read —
logs `send_keys_whitelist_blocked reason=kill-switch`, and returns `SendOutcome("kill-switch", ...)`.
This does not replace a caller's own tick-level kill-switch check (`gating_watcher.run_once` already
has one); it means a future caller that forgets to check it independently still fails closed.

**Codex finding #8 (blocking) — bounded, non-leaking refusal logging.** Every log call carries
`trace_id` (accepted as a parameter, defaulting to a fresh `uuid.uuid4()` if the caller does not
supply one — mirrors `run_once`'s per-tick `trace_id`). A refused command's `text` is logged
*truncated* (first 200 chars) with an explicit `text_truncated: bool` flag, never the unbounded raw
string — CLAUDE.md "never log secrets/PII"; an adversarial free-form send is exactly the
attacker-controlled-content case that rule exists for, and this is the one place in the module where
arbitrary caller-supplied text reaches the logger.

Flow: check kill switch (finding #7) → `validate()` → if `Refusal`: log
`send_keys_whitelist_refused` (`trace_id`, `reason`, `pane`, truncated `text`) and return
**unchanged ledger** + `SendOutcome("refused", ...)` — no ledger write, no `tmux` call, no side
effect of any kind (AC-10: refused before any side effect). If `Approved`: `record_pending` → (on
`"duplicate"`: log + return, no send) → `mark_send_started` + persist → `send_to_session` (exception
propagates uncaught, per finding #5) → `mark_sent`/`mark_consumed` on `"sent"`, or `mark_consumed`
alone (abandoned) on `"busy"`/`"absent"` — identical bookkeeping shape to
`gating_watcher.run_once`'s existing actuation block, factored out here so a future caller gets it
for free instead of re-deriving it.

### Documentation

`docs/runbooks/dispatch-orchestrator.md` — new subsection under "Gating watcher (FRE-823)" /
"Trigger ledger (FRE-829)": what the wrapper enforces, the closed grammar (including the
command-role-aware pane check), why `/master` is out of grammar by design (not a gap), and that it
is not yet wired into any live sender (explicit "not yet integrated" note so the runbook doesn't
overstate current behavior).

## Acceptance-criteria proof map (from the ticket, sliced from ADR-0113 AC-3/AC-10)

| AC | Test |
|----|------|
| AC-3(a) valid `/build FRE-<id>` at a build pane → approved/sent | `test_validate_approves_valid_build_at_build_pane`, `test_send_valid_build_sends` |
| AC-3(a) valid `/prime-worker` at any worker pane → approved/sent | `test_validate_approves_prime_worker_at_each_worker_pane`, `test_send_prime_worker_sends` |
| AC-3(b) free-form instruction → refused pre-send, logged | `test_validate_refuses_free_form`, `test_send_free_form_never_calls_runner` |
| AC-3(c) valid command at wrong/unattested pane → refused pre-send, logged | `test_validate_refuses_unattested_pane`, `test_send_unattested_pane_never_calls_runner` |
| Codex #2: `/build` refused at `cc-adrs` (attested pane, wrong role) | `test_validate_refuses_build_at_adr_pane` |
| Codex #3: unicode-digit / newline / control-char / multi-line payload refused | `test_parse_command_rejects_unicode_digits`, `test_parse_command_rejects_embedded_newline`, `test_parse_command_rejects_trailing_newline` |
| AC-10 refusal never reaches `tmux` (no keystroke) | `test_send_refused_makes_no_runner_call` |
| AC-10 refusal never writes the ledger | `test_send_refused_ledger_untouched` |
| "wrapper consults the trigger ledger so a send is tied to a ledger event" | `test_send_approved_writes_ledger_entry`, `test_send_duplicate_event_id_suppressed` |
| Codex #5: runner exception leaves ledger entry ambiguous (started, not sent), propagates | `test_send_runner_exception_propagates_and_leaves_entry_ambiguous` |
| Codex #7: kill switch refuses before validation/ledger | `test_send_kill_switch_blocks_before_validation` |
| Codex #8: refusal log truncates text, carries trace_id | `test_send_refusal_log_truncates_text_and_carries_trace_id` |
| Grammar closed-set: unknown command, malformed `/build` arg, extra args | `test_parse_command_rejects_*` (table-driven) |
| Pane attestation matches real launcher topology (never hand-duplicated) | `test_attested_panes_matches_launcher_topology` |

## Files

- `scripts/dispatch/send_keys_whitelist.py` (new)
- `tests/scripts/test_send_keys_whitelist.py` (new)
- `docs/runbooks/dispatch-orchestrator.md` (edit — new subsection)

## TDD steps

1. `parse_command` table-driven: valid `/build 1`, `/build 2`, `/build FRE-471`, `/prime-worker`;
   invalid — free-form text, `/build` with no arg, `/build` with a malformed arg (`FRE-abc`, bare
   `471`, lowercase `fre-471`, fullwidth/Arabic-indic digit lookalikes), `/build FRE-471 extra`,
   `/prime-worker extra`, unknown slash command, empty string, embedded `\n`/`\r`/`\t`, and text with
   a trailing `\n` (the `fullmatch` regression test for codex finding #3).
2. `attested_panes()` / `_panes_for()` — asserts the sets equal `{cc-build, cc-build2, cc-adrs}` and
   `{cc-build, cc-build2}` respectively by deriving from `launcher.topology_for`, not a hand-written
   literal (so they can never silently drift); a `/build` at `cc-adrs` is refused.
3. `validate()` — the AC-3/AC-10 table above plus the codex-finding tests, pure (no runner/logger).
4. `send()` — fake `CommandRunner` + `Logger` (records fields, including `trace_id` presence) + a
   fake clock-free in-memory ledger dict (mirrors `test_gating_watcher.py`'s
   `_RecordingRunner`/`_NullLogger` fakes): refused-before-ledger-write, refused-before-runner-call,
   kill-switch-blocks-before-validation, approved-sends-and-closes-ledger,
   approved-busy/absent-abandons, duplicate-event-id-suppressed-without-resend, runner-exception
   propagates with the ledger entry left ambiguous, refusal log text truncated at 200 chars.
5. Docs subsection.

## Quality gates

`make test-file FILE=tests/scripts/test_send_keys_whitelist.py` → `make test` → `make mypy` →
`make ruff-check` + `make ruff-format` → `pre-commit run --all-files`.

## Out of scope (follow-ups, do not build here)

- Rewiring `gating_watcher.py` or any master skill to call `send()` instead of `send_to_session`
  directly — a separate ticket (the FRE-828 umbrella names this as "master... actuates via
  send-keys (workers)", not yet built).
- A `/master <PR#>` grammar entry — deliberately excluded (see Scope decision above): this wrapper
  gates LLM-driven actuation only, and the watcher's `/master` trigger is already non-LLM-sourced.
- Deeper pane identity attestation beyond session-name membership (e.g. verifying the live pane's
  cwd matches the stream's worktree via `tmux display-message`) — codex finding #1, judged
  non-blocking for this ticket: `send_to_session`'s existing `tmux has-session` check has the same
  name-only trust model today, spoofing a same-named tmux session already requires local shell
  access on the VPS (a much larger compromise than this mechanism defends against), and the ADR's
  own threat model is "an LLM rationalizing free-form intent," not "an on-box attacker renaming tmux
  sessions." Worth a follow-up hardening ticket if the threat model ever includes local compromise.
- A CLI/manual-invocation entry point — no `main()`/argparse; this module is a library import
  target for the future integration ticket, not an operator-run script (unlike `gating_watcher.py`
  and `launcher.py`, which are systemd/cron entry points today).

## Codex findings (2026-07-07 plan review) — resolution status

1. Pane attestation is name-only (no live cwd/identity check) → **judged non-blocking**, documented
   rationale in Out of scope above; matches the existing `send_to_session` trust model.
2. `/build` validated at any attested pane, including `cc-adrs` (which only runs `/adr`) →
   **fixed**: command-role-aware pane sets (`_BUILD_PANES` vs `_PRIME_WORKER_PANES`), both still
   derived from `topology_for`.
3. Parser doesn't tightly reject newline/control-char/Unicode-digit bypasses → **fixed**:
   `fullmatch()` (no `$`-trailing-newline quirk) + explicit ASCII `[0-9]` character classes (never
   `\d`).
4. Duplicate-send protection depends entirely on a caller-supplied `event_id` with no derivation or
   enforcement → **resolved via explicit documented contract + test**, not a redesign (the wrapper
   cannot see the caller's dedup context, e.g. head SHA, so deriving it internally is not viable).
5. Exception semantics after `mark_send_started` were unstated → **fixed**: documented as
   uncaught-propagation (mirrors `run_once`'s existing behavior), `reconcile()` explicitly stays a
   caller-owned per-tick step, not a `send()`-internal one.
6. `/master` exclusion makes the "sits in front of `gating_watcher`'s actuation" framing look false →
   **resolved via clarification**: the exclusion is intentional (LLM-actuation-only threat model,
   ADR §2's own rationale); the module docstring and this plan now state it explicitly instead of
   leaving it implicit.
7. No kill-switch hook, so a future caller could bypass the existing halt semantics → **fixed**:
   optional `kill_switch_engaged` predicate on `send()`, checked first, defense-in-depth (does not
   replace a caller's own tick-level check).
8. Refusal logging could record arbitrary (adversarial, potentially sensitive) rejected text
   unbounded, with no `trace_id` → **fixed**: `trace_id` parameter (generated if absent), refusal log
   truncates `text` to 200 chars with a `text_truncated` flag.
9. Types could lean harder into `Literal`/frozen-dataclass discipline → **already satisfied by the
   design** (frozen dataclasses throughout, `ParsedCommand`/`SendResult` are discriminated by type
   and `Literal` respectively); no further change needed beyond what's already specified above.
