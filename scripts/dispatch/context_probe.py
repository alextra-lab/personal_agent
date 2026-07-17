#!/usr/bin/env python3
"""Headless /context: a session's live context usage from its transcript JSONL.

Lets the gating watcher poll context% + idle without scraping the pane.

Signals emitted (key=value, one line — the SAME keys on every path):
  session   tmux session asked about
  jsonl     the transcript file resolved for it (basename; NONE if unresolved)
  model     model of the last main-chain turn
  ctx       context-window tokens = input + cache_read + cache_creation of the
            last MAIN-CHAIN usage (sidechain/subagent usage is skipped).
            The cache-inclusive sum — input_tokens alone is misleading.
  window    the model's context window (per-model map; override with --window)
  pct       ctx / window, 1 decimal
  idle_s    seconds since the transcript was last written (mtime); -1 if unknown.
            KNOWN LIMITATION: mtime only advances when a line is appended, so a
            single long model turn with no interleaved tool/line writes can read
            IDLE while genuinely BUSY. Treat idle as a hint, not a hard
            interlock — a consumer gating delivery on it must stay conservative.
  state     BUSY if idle_s < --idle-threshold, IDLE if >=, UNKNOWN if unresolved

Resolution: the CURRENT transcript is the newest ``*.jsonl`` (by mtime) in the
project dir for the session's claude-process cwd. Newest-mtime is deliberate —
the process ``--session-id`` / ``CLAUDE_CODE_SESSION_ID`` stay at the LAUNCH
session and go stale after ``/clear``. Assumes claude runs in window 0 / pane 0.
RC-proof; needs no open fd.

Usage:
  context_probe.py cc-master
  context_probe.py --window 1000000 --idle-threshold 180 cc-build
  context_probe.py --jsonl /path/to/session.jsonl        # skip resolution
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import re
import subprocess
import time

from scripts.dispatch.tmux_target import exact_pane

PROJECTS = os.path.expanduser("~/.claude/projects")
DEFAULT_WINDOW = 1_000_000
# The transcript has no window field, so map it from the model (confirmed via
# /context: opus-4-8 = 1M, sonnet-5 = 1M, haiku-4.5 = 200k). ``--window``
# overrides. An unmapped model falls back to DEFAULT_WINDOW.
MODEL_WINDOWS = {
    "claude-opus-4-8": 1_000_000,
    "claude-sonnet-5": 1_000_000,
    "claude-haiku-4-5": 200_000,
}
# Read only the transcript tail so a poll is ~O(1) regardless of file size;
# large enough to contain the last main-chain usage across many recent turns.
TAIL_BYTES = 512_000

UNKNOWN_LINE = "jsonl=NONE model=? ctx=0 window=0 pct=0.0 idle_s=-1 state=UNKNOWN"


def _run(cmd: list[str]) -> str:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=5).stdout
    except (subprocess.SubprocessError, OSError):
        return ""


def _mtime(path: str) -> float:
    """os.path.getmtime that returns 0.0 if the file vanished (glob/stat race)."""
    try:
        return os.path.getmtime(path)
    except OSError:
        return 0.0


def _project_dir_for_cwd(cwd: str) -> str:
    """Return the ~/.claude/projects dir for a cwd.

    Claude Code names the project dir by replacing every ``/`` and ``.`` in the
    absolute cwd with ``-`` (e.g. ``/opt/seshat/.claude/worktrees/build`` ->
    ``-opt-seshat--claude-worktrees-build``).
    """
    return os.path.join(PROJECTS, re.sub(r"[/.]", "-", cwd))


def resolve_jsonl(session: str) -> str | None:
    """Resolve a tmux session's CURRENT transcript.

    The current transcript is the newest ``*.jsonl`` (by mtime) in the project
    dir for the session's claude-process cwd. Newest-mtime is deliberate: the
    process ``--session-id`` / ``CLAUDE_CODE_SESSION_ID`` stay at the LAUNCH
    session and go stale after ``/clear``. RC-proof; needs no open fd.
    """
    # Exact-match pane target (FRE-909) — an absent seat must resolve to
    # nothing, not to a name-extension seat whose context% would be reported
    # as this seat's.
    pane_pid = (
        _run(["tmux", "list-panes", "-t", exact_pane(session), "-F", "#{pane_pid}"])
        .strip()
        .split("\n")[0]
    )
    if not pane_pid.isdigit():
        return None
    try:
        cwd = os.readlink(f"/proc/{pane_pid}/cwd")
    except OSError:
        return None
    files = glob.glob(os.path.join(_project_dir_for_cwd(cwd), "*.jsonl"))
    return max(files, key=_mtime) if files else None


def read_context(jsonl: str) -> tuple[int, str]:
    """Return (context_tokens, model) from the last main-chain usage entry.

    Reads only the transcript tail (TAIL_BYTES) so a poll stays cheap regardless
    of transcript size. Skips ``isSidechain`` (subagent) entries so a Task
    subagent's usage is never mistaken for the session's own context.
    """
    try:
        size = os.path.getsize(jsonl)
        with open(jsonl, "rb") as fh:
            if size > TAIL_BYTES:
                fh.seek(size - TAIL_BYTES)
                fh.readline()  # drop the partial first line
            raw = fh.read().decode("utf-8", "replace")
    except OSError:
        return 0, "?"

    last_usage, model = None, "?"
    for line in raw.splitlines():
        try:
            o = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(o, dict) or o.get("isSidechain"):
            continue
        msg = o.get("message") or {}
        if not isinstance(msg, dict):
            continue
        if msg.get("model"):
            model = msg["model"]
        u = msg.get("usage")
        if isinstance(u, dict):
            last_usage = u
    if not last_usage:
        return 0, model
    ctx = (
        int(last_usage.get("input_tokens", 0))
        + int(last_usage.get("cache_read_input_tokens", 0))
        + int(last_usage.get("cache_creation_input_tokens", 0))
    )
    return ctx, model


def main() -> int:
    """Probe one session's context + idle and print a key=value line."""
    ap = argparse.ArgumentParser()
    ap.add_argument("session", nargs="?", help="tmux session (e.g. cc-master)")
    ap.add_argument("--jsonl", help="transcript path (skip session resolution)")
    ap.add_argument("--window", type=int, default=None, help="override the per-model window")
    ap.add_argument("--idle-threshold", type=int, default=180, help="seconds")
    args = ap.parse_args()

    sess = args.session or "-"
    jsonl = args.jsonl or (resolve_jsonl(args.session) if args.session else None)
    if not jsonl or not os.path.exists(jsonl):
        print(f"session={sess} {UNKNOWN_LINE}")
        return 1

    mtime = _mtime(jsonl)
    if not mtime:
        print(f"session={sess} {UNKNOWN_LINE}")
        return 1
    ctx, model = read_context(jsonl)
    window = args.window if args.window is not None else MODEL_WINDOWS.get(model, DEFAULT_WINDOW)
    idle_s = int(time.time() - mtime)
    pct = 100 * ctx / window if window else 0
    state = "BUSY" if idle_s < args.idle_threshold else "IDLE"
    print(
        f"session={sess} jsonl={os.path.basename(jsonl)[:8]} "
        f"model={model} ctx={ctx} window={window} pct={pct:.1f} "
        f"idle_s={idle_s} state={state}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
