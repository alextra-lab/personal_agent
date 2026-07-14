#!/usr/bin/env python3
"""PR-gate signal collector (ADR-0117, FRE-877).

The deterministic *signal collector* for master's PR gate — the gating-side twin
of ``reconcile_board.py``. It surfaces **only unambiguous external signals** for a
PR, each as a raw fact mapped one-to-one to its GitHub source field:

- **required CI checks** — each *required* context's raw state (GitHub decides
  which are required; this never hardcodes a list or synthesizes an overall
  "CI passed");
- **mergeability** — the raw ``mergeable`` / ``mergeStateStatus`` / ``isDraft``
  fields, never collapsed to one yes/no;
- **author identity** — ``is_dependabot_author`` as a raw boolean, carrying no
  merge implication.

It renders **no** judgment, makes **no** inference, aggregates **nothing**, and
**never blocks**. Every evaluation — codex, handoff completeness, AC evidence,
doc-drift, seam ownership, the merge decision — stays master's, uncaged
(ADR-0117 § Decision). ``UNKNOWN`` is first-class: an undeterminable signal is
reported as ``UNKNOWN``, never silently PASS. The process **exits 0 for every
signal value** (green, red, unknown); a nonzero exit is reserved only for a CLI
usage error or an unhandled crash, so wiring it as master SKILL Step 4's first
action can never become a gate by the back door.

Callable by hand::

    python -m scripts.pr_gate 520            # human summary
    python -m scripts.pr_gate 520 --json     # raw signal dict
"""

from __future__ import annotations

import argparse
import json
import subprocess  # noqa: S404 - runs trusted, argv-built `gh` commands, no shell
from collections.abc import Sequence
from typing import Literal, Protocol

# The GitHub App login a dependabot-authored PR carries (identity only — no merge
# implication; whether a bump is safe is master's judgment, ADR-0117 Decision 1).
_DEPENDABOT_LOGIN = "dependabot[bot]"

UNKNOWN = "UNKNOWN"


class RunResult(Protocol):
    """The subset of ``subprocess.CompletedProcess`` the collector reads."""

    returncode: int
    stdout: str


class CommandRunner(Protocol):
    """A callable that runs an argv and returns its result (injectable seam)."""

    def __call__(self, argv: Sequence[str]) -> RunResult:
        """Run ``argv`` and return its result."""
        ...


def subprocess_runner(argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
    """Default runner: run an argv, capturing output, never via a shell.

    Args:
        argv: The command and its arguments (never shell-interpreted).

    Returns:
        The completed process, with ``returncode`` and captured ``stdout``.
    """
    return subprocess.run(  # noqa: S603 - argv-built from a validated PR number, no shell
        list(argv), capture_output=True, text=True, check=False
    )


# --- pure parsers (no IO, no judgment — raw field extraction only) ----------


def parse_required_checks(stdout: str) -> list[dict[str, str]] | str:
    """Extract each required check's raw state, or ``UNKNOWN`` if undeterminable.

    Parses ``gh pr checks --required`` tab-delimited text (``name<TAB>state<TAB>
    duration<TAB>url``). GitHub's ``--required`` decides which contexts are
    required, so this never hardcodes or filters a list. Returns one
    ``{"name", "state"}`` per required context, verbatim.

    **The command's exit code is deliberately ignored.** ``gh pr checks`` encodes
    the *aggregate* outcome in its exit status (0 = all pass, non-zero =
    fail/pending) — but aggregation is exactly what the collector must not do, and
    gating on it would report a red or pending PR (the most useful case) as
    ``UNKNOWN``. Each check's own state is read from stdout instead. Returns
    ``UNKNOWN`` only when stdout has no parseable rows — never an empty "all clear".

    Args:
        stdout: The command's stdout (tab-delimited rows).

    Returns:
        A list of ``{"name": str, "state": str}`` dicts (raw), or ``UNKNOWN``.
    """
    lines = [line for line in stdout.splitlines() if line.strip()]
    if not lines:
        return UNKNOWN
    checks: list[dict[str, str]] = []
    for line in lines:
        parts = line.split("\t")
        if len(parts) < 2 or not parts[0]:
            return UNKNOWN
        checks.append({"name": parts[0], "state": parts[1]})
    return checks


def parse_mergeability(stdout: str, returncode: int) -> dict[str, object]:
    """Extract the raw mergeability fields, ``UNKNOWN`` for any that is absent.

    Reads ``mergeable`` / ``mergeStateStatus`` / ``isDraft`` verbatim from a
    ``gh pr view --json ...`` payload — never collapsed into a single yes/no
    (the distinct GitHub states — MERGEABLE / CONFLICTING / computing / BLOCKED —
    are not the same fact, ADR-0117 Decision 1).

    Args:
        stdout: The command's stdout.
        returncode: The command's exit code.

    Returns:
        ``{"mergeable", "merge_state_status", "is_draft"}`` — each raw or ``UNKNOWN``.
    """
    data = _load_obj(stdout, returncode)
    if data is None:
        return {"mergeable": UNKNOWN, "merge_state_status": UNKNOWN, "is_draft": UNKNOWN}
    return {
        "mergeable": _raw(data, "mergeable"),
        "merge_state_status": _raw(data, "mergeStateStatus"),
        "is_draft": _raw(data, "isDraft"),
    }


def parse_author_identity(stdout: str, returncode: int) -> dict[str, object]:
    """Extract ``is_dependabot_author`` as a raw boolean, or ``UNKNOWN``.

    Identity only — it carries no merge implication (ADR-0117 Decision 1).

    Args:
        stdout: The command's stdout.
        returncode: The command's exit code.

    Returns:
        ``{"is_dependabot_author": bool | "UNKNOWN"}``.
    """
    data = _load_obj(stdout, returncode)
    author = data.get("author") if data is not None else None
    if not isinstance(author, dict):
        return {"is_dependabot_author": UNKNOWN}
    login = str(author.get("login", ""))
    return {"is_dependabot_author": login == _DEPENDABOT_LOGIN}


def _load_obj(stdout: str, returncode: int) -> dict[str, object] | None:
    """Parse a JSON object from stdout, or ``None`` on failure / non-object."""
    if returncode != 0:
        return None
    try:
        data: object = json.loads(stdout or "null")
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def _raw(data: dict[str, object], key: str) -> object:
    """Return ``data[key]`` verbatim, or ``UNKNOWN`` when the key is absent/null."""
    value = data.get(key)
    return UNKNOWN if value is None else value


# --- IO seam ----------------------------------------------------------------


def collect_signals(pr: int, runner: CommandRunner = subprocess_runner) -> dict[str, object]:
    """Collect the raw external signals for ``pr`` (two ``gh`` reads).

    Returns a flat dict of raw facts only — no top-level verdict, no aggregation.
    Each value maps one-to-one to a GitHub source field (or ``UNKNOWN``).

    Args:
        pr: The PR number.
        runner: The command runner seam (shells ``gh``).

    Returns:
        ``{"pr", "required_checks", "mergeability", "author"}`` — raw signals.
    """
    checks_result = runner(["gh", "pr", "checks", str(pr), "--required"])
    view_result = runner(
        ["gh", "pr", "view", str(pr), "--json", "mergeable,mergeStateStatus,isDraft,author"]
    )
    return {
        "pr": pr,
        "required_checks": parse_required_checks(checks_result.stdout),
        "mergeability": parse_mergeability(view_result.stdout, view_result.returncode),
        "author": parse_author_identity(view_result.stdout, view_result.returncode),
    }


def _human_summary(signals: dict[str, object]) -> str:
    """Render the raw signals as a flat facts list — no verdict, no readiness line.

    Deliberately contains none of the words pass/fail/ready/blocked/hold/merge as
    a *derived* status: it echoes each raw field so master reads facts, not a
    recommendation (ADR-0117 AC-2).
    """
    lines = [f"PR #{signals['pr']} — external signals (facts only; evaluation is master's):"]
    checks = signals["required_checks"]
    if checks == UNKNOWN:
        lines.append("  required_checks: UNKNOWN (could not determine the required set)")
    elif isinstance(checks, list):
        lines.append("  required_checks:")
        for check in checks:
            lines.append(f"    - {check['name']}: {check['state']}")
    merge = signals["mergeability"]
    if isinstance(merge, dict):
        lines.append(
            f"  mergeable={merge['mergeable']} "
            f"merge_state_status={merge['merge_state_status']} is_draft={merge['is_draft']}"
        )
    author = signals["author"]
    if isinstance(author, dict):
        lines.append(f"  is_dependabot_author={author['is_dependabot_author']}")
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> Literal[0]:
    """Collect and print a PR's external signals. Always returns 0.

    The collector reports; it never halts the gate (ADR-0117 Decision 5), so it
    returns 0 for every signal value — red CI, a conflict, dependabot identity, or
    an ``UNKNOWN`` signal all still exit 0. A CLI usage error raises argparse's own
    ``SystemExit(2)`` before this returns; that is the only nonzero exit.

    Args:
        argv: The CLI arguments (defaults to ``sys.argv``).

    Returns:
        ``0`` — always.
    """
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("pr", type=int, help="The PR number to collect signals for.")
    parser.add_argument("--json", action="store_true", help="Emit the raw signal dict as JSON.")
    args = parser.parse_args(argv)

    signals = collect_signals(args.pr)
    if args.json:
        print(json.dumps(signals, indent=2))
    else:
        print(_human_summary(signals))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
