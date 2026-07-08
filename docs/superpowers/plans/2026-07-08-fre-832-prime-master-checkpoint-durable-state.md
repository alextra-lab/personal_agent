# FRE-832 — prime-master revision: checkpoint-to-durable-state + coordinator role (ADR-0113)

Backing ADR: ADR-0113 §1 (role model — sensor → brain → hands) + §4 (context discipline).
Blocked by FRE-829 (trigger ledger, Done — PR #427). Codex plan-review completed 2026-07-08 (see
"Codex findings" below) — this revision incorporates its fixes.

**What this ticket closes vs. what it hands off:** ADR-0113 AC-1's check is "seed one PR at the
gate + one unconsumed trigger, `/clear`, run `prime-master`, diff the rebuilt snapshot" — a
behavioral check of the *assembled* loop. Per the ADR's own "Seam owner" clause, master owns final
seam verification on the FRE-828 umbrella; this ticket cannot itself run `/clear` + `prime-master`
as an automated test. What it delivers and proves automatically: (1) the missing durable-read
mechanism (a CLI on the trigger ledger, since FRE-829 shipped the data layer but no way to read it
from outside a Python process), (2) `prime-master`'s markdown wired to call it and surface the
result, with a content-contract test pinning that wiring so it can't silently regress, and (3) the
PR-at-the-gate half already covered by the pre-existing `gh pr list` step (unchanged). Master's
later seam demonstration is the remaining, non-automatable half of AC-1.

## Scope decision (flagged for master)

FRE-829 shipped the ledger's data layer (`load_ledger`, `snapshot_unconsumed`,
`record_pending`/`reconcile`/etc.) but no way to *read* it from outside a Python test — no CLI.
`prime-master` is a markdown skill executed by an LLM shelling out to bash; it cannot `import` the
module. This ticket adds the missing read surface (a `main()` CLI on `trigger_ledger.py`, mirroring
`next_resolver.py`'s existing `--json` pattern) and wires `prime-master`'s rebuild-snapshot step to
call it, so an unconsumed trigger is now part of the printed guardian snapshot.

**Not in scope** (flagging so master doesn't expect it and bounce for its absence):
- The best-effort `X% context used` pane-parse alert. ADR-0113 §4 calls this "optionally add ...
  explicitly a nicety, never the safety mechanism (it is the fragile terminal-parse class that
  already produced a live bug [FRE-825])." Building a second instance of the exact fragile-parse
  class the ADR calls out, with no AC coverage and no owner ask beyond "optionally," fails
  Simplicity First. Documented as a deliberate skip in the skill file itself, not silently dropped.
- Changing `/master`'s Step 8 "Advance dispatch" or `/build`'s Step-0 stream-selector logic to shell
  out to `next_resolver.py` instead of inline Linear-MCP calls. That resolver already exists
  (FRE-785) but consuming it from `/master`/`/build` is a separate, more invasive change than
  "prime-master reads durable state on rebuild" — no AC here requires it. `prime-master` instead
  gains a documentation note: the coordinator does not hold NEXT-ticket resolution logic in
  context; it is available as an external process.
- The "PR at the gate" half of AC-1's seed scenario is already satisfied by the existing `gh pr
  list` step (prime-master Step 3, unchanged) — no code change needed there.
- End-to-end seam demonstration (seed both a PR-at-gate and an unconsumed trigger, `/clear`, run
  `prime-master`, diff). Per ADR-0113 "Seam owner": **master owns final seam verification on the
  FRE-828 umbrella** — this ticket delivers the mechanism + its own unit proof that the ledger half
  reconstructs from disk only; master demonstrates the assembled loop once, later, per the ADR.

## Codex findings (2026-07-08 plan review) and how each is closed

1. **AC-1 proof under-scoped vs. the ticket's stated AC** — closed by the "What this ticket closes
   vs. what it hands off" paragraph above: the ticket no longer claims to close AC-1 outright, only
   the ledger-read mechanism + skill wiring half of it.
2. **Markdown skill behavior isn't protected by any test** — closed: add a content-contract test in
   `tests/scripts/test_dispatch_skill_contracts.py` (mirroring its existing prime-worker/build/adr
   contract tests) asserting `prime-master/SKILL.md` contains the exact CLI invocation and the
   "unconsumed actuation trigger" output field — so a later edit that drops the wiring fails CI, not
   just a human re-read.
3. **CLI tests missing `surfaced` and mixed-ledger cases** — closed: add a surfaced-entry test and a
   mixed (pending + surfaced + consumed) ledger test asserting only the two non-terminal entries
   appear in output and `surfaced_at` serializes correctly.
4. **Malformed ledger is indistinguishable from "no triggers"** — closed: the CLI wraps `load_ledger`
   with a small logger that detects the `trigger_ledger_corrupt` warning event; on corruption it
   prints an explicit stderr error and exits 1, rather than emitting `[]`/`none` as if the ledger were
   healthy-empty. A CLI test asserts this (corrupt file → exit 1, stderr mentions "corrupt").
5. **Minor CLI consistency nit** — closed: use `argparse.RawDescriptionHelpFormatter` to match
   `next_resolver.py`'s existing convention.
6. **Avoid overclaiming the resolver note** — closed: the `prime-master` doc note is phrased as
   "available as an external process," not as fulfilling the ADR's actuation-boundary requirement —
   the follow-up ticket (implementation step 5) is the actual behavior change.

## What to build

### 1. `scripts/dispatch/trigger_ledger.py` — add a CLI entry point

Add, following the exact shape of `next_resolver.main()` (argparse, `--json` flag, prints to
stdout, `if __name__ == "__main__": raise SystemExit(main())`):

```python
def _default_ledger_path() -> Path:
    """Return the default trigger-ledger path under the repo's telemetry dir."""
    return Path("telemetry") / "trigger_ledger.json"


def _entry_to_json(entry: LedgerEntry) -> dict[str, object]:
    """Serialize a `LedgerEntry` to a JSON-safe dict."""
    return {
        "event_id": entry.event_id,
        "source": entry.source,
        "target_pane": entry.target_pane,
        "ticket": entry.ticket,
        "command": entry.command,
        "preconditions": dict(entry.preconditions),
        "created_at": entry.created_at,
        "send_started_at": entry.send_started_at,
        "sent_at": entry.sent_at,
        "surfaced_at": entry.surfaced_at,
    }


class _CLILogger:
    """Wraps the CLI's own logger to detect a corrupt-ledger warning.

    `load_ledger` already logs+swallows a corrupt file as `{}` (the right
    behavior for a long-running caller like the watcher, which must not
    crash on a bad file). A one-shot CLI read needs to tell "genuinely no
    triggers" apart from "the file exists but failed to parse" — the two
    must not both print as an empty/`none` result.
    """

    def __init__(self) -> None:
        self.corrupted = False

    def info(self, event: str, **fields: object) -> None:
        pass

    def warning(self, event: str, **fields: object) -> None:
        if event == "trigger_ledger_corrupt":
            self.corrupted = True
        print(f"warning: {event} {fields}", file=sys.stderr)


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point. Prints unconsumed ledger entries (the `/clear`-safe read, FRE-832)."""
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--ledger-file", default=str(_default_ledger_path()), help="Path to the trigger ledger."
    )
    parser.add_argument(
        "--unconsumed",
        action="store_true",
        help="Print entries not yet fully closed out (pending or surfaced).",
    )
    parser.add_argument("--json", action="store_true", help="Emit the result as JSON.")
    args = parser.parse_args(argv)

    if not args.unconsumed:
        parser.error("--unconsumed is required (the only supported read today)")

    logger = _CLILogger()
    ledger = load_ledger(Path(args.ledger_file), logger)
    if logger.corrupted:
        print("error: trigger ledger file is corrupt — cannot determine in-flight state", file=sys.stderr)
        return 1
    entries = snapshot_unconsumed(ledger)

    if args.json:
        print(json.dumps([_entry_to_json(e) for e in entries], indent=2))
    elif not entries:
        print("none")
    else:
        for e in entries:
            state = "surfaced" if e.surfaced_at is not None else "pending"
            print(f"{e.event_id} [{state}] ticket={e.ticket} target={e.target_pane}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

Needs new imports at the top of `trigger_ledger.py`: `argparse`, `sys` (for the corrupt-ledger and
warning stderr writes), and `from collections.abc import Sequence` (extend the existing
`collections.abc` import rather than adding a second one). No `structlog` dependency — the CLI's
own tiny `_CLILogger` satisfies the module's existing `Logger` Protocol without pulling in the full
structured-logging stack for a one-shot script (`gating_watcher.py` uses `structlog` because it's a
long-running daemon whose logs need to be queryable in ES; this is a synchronous CLI read whose
only consumer is a human/LLM shelling out and reading stdout/stderr directly). `--unconsumed` is
required-but-a-flag (not a plain positional) so the CLI's shape has room for a future
`--all`/`--surfaced-only` mode without a breaking change; today's callable is exactly the one read
`prime-master` needs.

Callable by hand (mirrors the module docstring's existing "Callable by hand" convention — add a
line there too):
```
python -m scripts.dispatch.trigger_ledger --unconsumed --json
```

### 2. `.claude/skills/prime-master/SKILL.md` — wire in the read + bake in the coordinator role

- **Rebuild snapshot**, insert a new step between the current step 3 (`git status` / `gh pr list`)
  and step 4 (Linear states): read unconsumed actuation triggers via
  `python -m scripts.dispatch.trigger_ledger --unconsumed --json` and fold any pending/surfaced
  entries into the snapshot (event id, target pane, ticket, pending-vs-surfaced). A `surfaced`
  entry is a Verify-Failed-class exception — call it out explicitly, it demands owner attention the
  same way a `Verify Failed` Linear ticket does.
- **Output** section: add "unconsumed actuation triggers (from the trigger ledger — none, or
  list)" to the printed guardian-snapshot fields.
- **New short section**, "Coordinator role (ADR-0113 §1)," ahead of or folded into the existing
  guardian-role restatement: state the sensor → brain → hands shape in master's own terms — the
  watcher is a dumb contextless sensor that talks only to master; master is the single brain +
  hands, reasoning from durable state (Linear / `MASTER_PLAN` / git / the trigger ledger) and
  actuating via `send-keys` / `gh` / Linear; **the NEXT-ticket dispatch resolver
  (`scripts/dispatch/next_resolver.py`) is a separate process master can shell out to — dispatch
  mechanics are not logic held in master's context.** This is a documentation-only addition (no
  behavior change to `/master` or `/build`'s existing dispatch flow — see Scope decision above).
- **New short note** on the context-% alert: state plainly that it is out of scope for this
  revision per ADR-0113 §4's own caution (fragile terminal-parse class, FRE-825), and that the
  checkpoint-to-durable-state mechanism above is the actual safety net — so a future session
  doesn't file this as a silently-dropped requirement.

## Acceptance-criteria slice and how proven

- **AC-1 (ledger half)** — an unconsumed trigger, written to disk by one process, is readable by a
  wholly separate process invocation with no shared memory (simulating "a context clear happened
  in between"), and the read correctly distinguishes pending / surfaced / consumed / corrupt.
  **Proof:** new tests in `tests/scripts/test_trigger_ledger.py`:
  - seeded-unconsumed-visible: `record_pending` + `save_ledger` to a `tmp_path` file (simulating
    the pre-clear state), then call `trigger_ledger.main(["--ledger-file", str(path),
    "--unconsumed", "--json"])` (a fresh call, no shared ledger object) — assert the emitted JSON
    contains the seeded `event_id`/`ticket`.
  - empty/absent ledger → `[]`.
  - consumed-only ledger → `[]` (a closed-out trigger must not resurface as "in-flight" forever).
  - surfaced-only entry → JSON includes it with `surfaced_at` set (the Verify-Failed-class
    exception must not be dropped).
  - mixed ledger (one pending + one surfaced + one consumed) → JSON contains exactly the pending
    and surfaced entries, not the consumed one.
  - corrupt ledger file (malformed JSON) → exit code 1, stderr contains "corrupt", stdout is empty
    (never a silent `[]`/`none` masquerading as healthy-empty).
- **AC-1 (skill-wiring half)** — `prime-master/SKILL.md` actually calls the new CLI and surfaces
  its result, and a later edit that drops this can't silently regress. **Proof:** new test(s) in
  `tests/scripts/test_dispatch_skill_contracts.py` (mirroring its existing prime-worker/build/adr
  contract-test pattern) asserting the skill text contains the exact invocation
  `python -m scripts.dispatch.trigger_ledger --unconsumed --json` and the phrase "unconsumed
  actuation trigger".
- **AC-1 (PR-at-gate half)** — no code change; already covered by the existing `gh pr list` step,
  unit-untestable at the skill-markdown layer (it is a live `gh` call), no new proof needed here.
- **AC-1 (assembled seam)** — explicitly out of scope, owned by master per the ADR's "Seam owner"
  clause (see Scope decision above).

## Implementation steps

1. `tests/scripts/test_trigger_ledger.py`: add the six new CLI tests listed above. Run `make
   test-file FILE=tests/scripts/test_trigger_ledger.py` — confirm they fail (no `main` yet).
2. `scripts/dispatch/trigger_ledger.py`: add `_default_ledger_path`, `_entry_to_json`,
   `_CLILogger`, `main`, the `__main__` guard, and the new imports (`argparse`, `sys`,
   `collections.abc.Sequence`). Re-run the file's tests — confirm green (all, not just the six
   new ones).
3. `.claude/skills/prime-master/SKILL.md`: the four edits (rebuild-snapshot step, Output field,
   coordinator-role section, context-% scope note).
4. `tests/scripts/test_dispatch_skill_contracts.py`: add the skill-wiring content-contract test(s).
   Run `make test-file FILE=tests/scripts/test_dispatch_skill_contracts.py` — confirm it fails
   before step 3's edit lands, passes after.
5. `make mypy` / `make ruff-check` / `make ruff-format` on `scripts/dispatch/trigger_ledger.py`.
6. Follow-up ticket (Needs Approval, Build/ADR Dispatch Automation project): wire `/master` Step 8
   and `/build` Step 0 to shell out to `next_resolver.py` instead of inline Linear-MCP dispatch
   logic — the ADR's "not logic held in context" intent, applied to the resolver's actual callers,
   not just documented in `prime-master`.

## Test commands

- `make test-file FILE=tests/scripts/test_trigger_ledger.py`
- `make test-file FILE=tests/scripts/test_dispatch_skill_contracts.py`
- `make mypy`
- `make ruff-check`
