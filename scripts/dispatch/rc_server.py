#!/usr/bin/env python3
"""Remote-Control server-mode launch wrapper (FRE-788, ADR-0110 T4).

The systemd unit ``claude-remote-control@.service`` execs this module per
dispatch stream. It resolves the stream to its worktree (reusing the launcher's
``topology_for`` — the single source of truth for stream→worktree, so the two
never drift), then execs ``claude remote-control`` in **server mode**
(``--spawn session``) against that **existing** worktree — honouring ADR-0110
§1's "existing per-stream worktrees, not ``--spawn worktree`` clones."

This wrapper is deliberately thin: the pure ``rc_server_plan`` builds the
(worktree, argv) pair the systemd ExecStart depends on, and ``main`` either
prints it (``--dry-run``, so a human can verify the unit without launching) or
``chdir`` + ``execvp`` into the long-lived RC server. The systemd unit sets
``WorkingDirectory=/opt/seshat`` and ``Restart=always`` (RC exits after a
prolonged network outage; the unit restarts it).

Preconditions (documented in docs/runbooks/dispatch-orchestrator.md): a
claude.ai subscription (not an API key), ``claude auth login`` completed, the
worktree already trusted, and ``ANTHROPIC_BASE_URL`` unset or api.anthropic.com.

Callable by hand::

    python -m scripts.dispatch.rc_server build1 --dry-run   # print the plan
    python -m scripts.dispatch.rc_server build1             # exec the RC server
"""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Sequence

from scripts.dispatch.launcher import topology_for


def rc_server_plan(stream: str) -> tuple[str, tuple[str, ...]]:
    """Resolve a stream to its worktree and RC server-mode argv (pure).

    Args:
        stream: Dispatch stream key (``build1``/``build2``/``adr``).

    Returns:
        A ``(worktree, argv)`` pair — the repo-relative worktree the server runs
        in, and the ``claude remote-control`` server-mode command.

    Raises:
        ValueError: The stream is not a known dispatch stream.
    """
    topology = topology_for(stream)
    argv = (
        "claude",
        "remote-control",
        "--spawn",
        "session",
        "--name",
        f"seshat-{stream}",
    )
    return topology.worktree, argv


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point. Prints the plan (``--dry-run``) or execs the RC server."""
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("stream", help="Dispatch stream: build1, build2, adr.")
    parser.add_argument("--dry-run", action="store_true", help="Print the resolved plan and exit.")
    args = parser.parse_args(argv)

    try:
        worktree, command = rc_server_plan(args.stream)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if args.dry_run:
        print(f"worktree: {worktree}")
        print(f"command: {' '.join(command)}")
        return 0

    os.chdir(worktree)
    os.execvp(command[0], list(command))  # noqa: S606 - fixed, argv-built claude command


if __name__ == "__main__":
    raise SystemExit(main())
