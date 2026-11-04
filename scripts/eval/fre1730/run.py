"""FRE-1730: replay closed tickets with and without a plugin. See ``README.md``.

uv run python -m scripts.eval.fre1730.run preflight
uv run python -m scripts.eval.fre1730.run verify-tasks
uv run python -m scripts.eval.fre1730.run dry-run
uv run python -m scripts.eval.fre1730.run check-arms --plugin-dir DIR --marker TEXT
uv run python -m scripts.eval.fre1730.run paid --plugin-dir DIR --marker TEXT --hard-stop-usd N --projected-usd N --owner-go TEXT
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

from scripts.eval.fre1730.footprint import plugin_only
from scripts.eval.fre1730.jail import Jail, main_repo_root, run_in_jail
from scripts.eval.fre1730.matrix import (
    PlannedSession,
    RunConfig,
    load_records,
    plan_sessions,
    run_matrix,
)
from scripts.eval.fre1730.report import plugin_hook_events, render_report
from scripts.eval.fre1730.score import copy_pr_tests, run_pr_tests
from scripts.eval.fre1730.session import paid_route, prepare_repo, stub_route
from scripts.eval.fre1730.spend import RunStopped, SpendStop
from scripts.eval.fre1730.stub_model import StubModel, serve
from scripts.eval.fre1730.tasks import Task, base_commit, load_tasks

HERE = Path(__file__).resolve().parent
SCRATCH = Path.home() / ".cache" / "seshat-fre1730"
FIXTURE_PLUGIN = HERE / "fixture_plugin"
FIXTURE_MARKER = "FRE1730-FIXTURE-MARKER"
GLOBAL_CLAUDE_MD = Path.home() / ".claude" / "CLAUDE.md"
#: The build seats run Opus. Both arms use this exact id.
MODEL = "claude-opus-5-5"
REPEATS = 3
SEED = "fre1730"
#: The ticket's pin. A tag can move, so the SHA is the pin.
PONYTAIL_REPO = "https://github.com/DietrichGebert/ponytail"
PONYTAIL_SHA = "7801d061243d7b753bad0e0b3c057cd250b11755"
PONYTAIL_MARKER = "PONYTAIL MODE ACTIVE"
EXIT_STOPPED = 5
EXIT_NOT_USABLE = 6
VERIFIED_FILE = "verified-tasks.json"
#: Opus 5.5 at API prices, read from 39 build-seat session transcripts on 2026-11-04. A seat session holds the
#: ``/build`` steps and the seat's plugins, which a replay does not. So a replay is likely to cost less.
SEAT_SESSION_MEDIAN_USD = 4.94
SEAT_SESSION_RANGE_USD = (1.27, 26.37)
AUTHOR_BENCHMARK_USD = 0.142
#: Master's two sessions with the plugin on 2026-11-04 (ticket comment), on larger work than a small ticket.
MASTER_CHECK_USD = (4.81, 2.40)


def require_pin(plugin: Path) -> None:
    """Refuse a plugin copy that is not the pin. Its hooks run in a session that holds a token.

    Args:
        plugin: The plugin directory.

    Raises:
        SystemExit: If the copy is from another repo, at another commit, or changed.
    """

    def git(*args: str) -> str:
        done = subprocess.run(
            ["git", "-C", str(plugin), *args], capture_output=True, text=True, check=False
        )
        if done.returncode != 0:
            raise SystemExit(f"{plugin} is not a git copy of the plugin: {done.stderr.strip()}")
        return done.stdout.strip()

    if git("remote", "get-url", "origin").removesuffix(".git") != PONYTAIL_REPO:
        raise SystemExit(f"{plugin} is not a clone of {PONYTAIL_REPO}")
    if git("rev-parse", "HEAD") != PONYTAIL_SHA:
        raise SystemExit(f"{plugin} is not at the pin {PONYTAIL_SHA}")
    if git("status", "--porcelain"):
        raise SystemExit(f"{plugin} has local changes")


def trace_sessions(sessions: Sequence[PlannedSession]) -> list[PlannedSession]:
    """Pick the trace: one session per arm, on the first task. A projection needs both arms.

    Args:
        sessions: The plan.

    Returns:
        The two sessions.
    """
    ticket = sessions[0].task.ticket
    picked = [s for s in sessions if s.task.ticket == ticket and s.repeat == 0]
    return sorted(picked, key=lambda s: s.arm)


def ensure_venv(source: Path, base: str, scratch: Path) -> Path:
    """Build a task's environment once, at the path that a session sees. Its own uv cache keeps the seats' cache safe.

    Args:
        source: The source repo.
        base: The task's base commit.
        scratch: The scratch directory.

    Returns:
        The environment.

    Raises:
        RunStopped: If the build fails.
    """
    venv = scratch / "envs" / base[:12]
    marker = scratch / "envs" / f"{base[:12]}.complete"
    if marker.exists():
        return venv
    shutil.rmtree(venv, ignore_errors=True)
    run_root = scratch / "runs" / f"env-{base[:12]}"
    shutil.rmtree(run_root, ignore_errors=True)
    prepare_repo(source, base, run_root / "repo")
    (run_root / "uv-cache").mkdir()
    venv.mkdir(parents=True, exist_ok=True)
    try:
        done = run_in_jail(
            Jail(run_root=run_root, venv=venv, network=True, writable_venv=True),
            ["uv", "sync", "--frozen", "--extra", "dev"],
            env={
                "UV_CACHE_DIR": "/tmp/work/uv-cache",
                "UV_PROJECT_ENVIRONMENT": "/tmp/work/repo/.venv",
            },
            timeout_s=1200,
        )
    except subprocess.TimeoutExpired as error:
        raise RunStopped(f"uv sync passed its time limit for {base[:12]}") from error
    finally:
        shutil.rmtree(run_root, ignore_errors=True)
    if done.returncode != 0:
        raise RunStopped(f"uv sync failed for {base[:12]}: {done.stderr[-500:]}")
    marker.touch()
    return venv


def _venvs(source: Path, tasks: Sequence[Task], scratch: Path) -> dict[str, Path]:
    bases = {base_commit(source, task) for task in tasks}
    return {base: ensure_venv(source, base, scratch) for base in bases}


def verify_tasks(tasks: Sequence[Task], source: Path, scratch: Path) -> list[str]:
    """Check that each task's tests can tell a fix from no fix. This calls no model.

    Args:
        tasks: The tasks.
        source: The source repo.
        scratch: The scratch directory.

    Returns:
        One line per task.
    """
    lines = []
    for task in tasks:
        base = base_commit(source, task)
        venv = ensure_venv(source, base, scratch)
        run_root = scratch / "runs" / f"verify-{task.ticket}"
        shutil.rmtree(run_root, ignore_errors=True)
        prepare_repo(source, base, run_root / "repo")
        jail = Jail(run_root=run_root, venv=venv)
        copy_pr_tests(source, task, jail)
        at_base = run_pr_tests(jail, task.tests)
        for args in (
            ("fetch", "-q", "--depth=1", f"file://{source}", task.merge),
            ("checkout", "-qf", "FETCH_HEAD"),
        ):
            subprocess.run(
                ["git", "-C", str(run_root / "repo"), *args], check=True, capture_output=True
            )
        at_merge = run_pr_tests(jail, task.tests)
        shutil.rmtree(run_root, ignore_errors=True)
        usable = at_base.failed > 0 and at_merge.failed == 0 and at_merge.passed > 0
        lines.append(
            f"{task.ticket}: at base {at_base.passed} passed, {at_base.failed} failed. "
            f"At merge {at_merge.passed} passed, {at_merge.failed} failed. {'usable' if usable else 'NOT USABLE'}"
        )
    return lines


def record_verified(tasks: Sequence[Task], lines: Sequence[str], scratch: Path) -> bool:
    """Record a verification that passed. A paid run needs it.

    Args:
        tasks: The tasks.
        lines: The lines from ``verify_tasks``.
        scratch: The scratch directory.

    Returns:
        Every task is usable.
    """
    path = scratch / VERIFIED_FILE
    path.unlink(missing_ok=True)
    if len(lines) != len(tasks) or not all(line.endswith(" usable") for line in lines):
        return False
    path.write_text(json.dumps(sorted(task.merge for task in tasks)))
    return True


def require_verified(tasks: Sequence[Task], scratch: Path) -> None:
    """Refuse a paid run on tasks that have no passed verification.

    Args:
        tasks: The tasks.
        scratch: The scratch directory.

    Raises:
        SystemExit: If this task set has no passed verification.
    """
    path = scratch / VERIFIED_FILE
    if not path.exists() or json.loads(path.read_text()) != sorted(t.merge for t in tasks):
        raise SystemExit("run verify-tasks first, and every task must be usable")


def run_and_report(
    sessions: Sequence[PlannedSession],
    config: RunConfig,
    stop: SpendStop,
    hook_events: Sequence[str],
    go: str,
) -> list[dict[str, object]]:
    """Run, and write the report from the ledger even if the run stops. A stopped run is the likely end of this run.

    Args:
        sessions: The plan.
        config: The run.
        stop: The stop.
        hook_events: The hook events that the plugin declares.
        go: The owner's recorded go.

    Returns:
        The records.
    """
    try:
        return run_matrix(sessions, config, stop)
    finally:
        report = render_report(load_records(config), hook_events, go)
        (config.results / "report.md").write_text(report)


def _stub_run(
    sessions: Sequence[PlannedSession],
    plugin: Path,
    marker: str,
    results: Path,
    source: Path,
    scratch: Path,
    venvs: dict[str, Path],
) -> list[dict[str, object]]:
    stub = StubModel()
    with serve(stub) as url:
        address = ("127.0.0.1", int(url.rsplit(":", 1)[1]))
        config = RunConfig(
            source=source,
            scratch=scratch,
            results=results,
            model=MODEL,
            route=stub_route(),
            plugin=plugin,
            marker=marker,
            global_claude_md=GLOBAL_CLAUDE_MD if GLOBAL_CLAUDE_MD.exists() else None,
            venvs=venvs,
            stub_address=address,
        )
        return run_matrix(sessions, config, SpendStop(hard_stop_usd=1.0, projected_usd=1.0))


def _print_footprint(records: Sequence[dict[str, object]]) -> None:
    for record in records:
        print(f"--- {record['key']}: ~ of the session, each file that changed")
        for line in record["footprint_lines"]:  # type: ignore[attr-defined]
            print(f"  {line}")
    plugin = [p for r in records if r["arm"] == "plugin" for p in r["footprint"]]  # type: ignore[attr-defined]
    control = [p for r in records if r["arm"] == "control" for p in r["footprint"]]  # type: ignore[attr-defined]
    print("--- files that only the plugin arm wrote")
    for path in sorted(set(plugin_only(plugin, control))):
        print(f"  {path}")


def _print_arm_check(records: Sequence[dict[str, object]]) -> None:
    for record in records:
        print(
            f"{record['key']}: requests read from the transcript, problems {record['problems']}, "
            f"cost ${record['cost_usd']:.6f}, refused {record['refused']}"
        )


def cmd_preflight(tasks: Sequence[Task]) -> int:
    """Print the preflight. This calls no model and reads no credential.

    Args:
        tasks: The tasks.

    Returns:
        The exit code.
    """
    print(
        f"Model: {MODEL}, in both arms. Sessions: {len(tasks)} tasks x 2 arms x {REPEATS} runs = {len(tasks) * 2 * REPEATS}."
    )
    print(
        f"Prices: $4 input, $20 output and $0.20 cache read per MTok. Plugin: {PONYTAIL_REPO} at {PONYTAIL_SHA}."
    )
    print("\nTasks:")
    for task in tasks:
        print(f"- {task.ticket} (PR #{task.pr}, risky: {task.risky}): {task.reason}")
    sessions = len(tasks) * 2 * REPEATS
    print("\nCost basis (notional, at API prices):")
    print(
        f"- Author's benchmark: ${AUTHOR_BENCHMARK_USD} per session, so ${AUTHOR_BENCHMARK_USD * sessions:.2f}. It has no CLAUDE.md and no Bash."
    )
    print(
        f"- Our build seats: median ${SEAT_SESSION_MEDIAN_USD} per session, range ${SEAT_SESSION_RANGE_USD[0]} to "
        f"${SEAT_SESSION_RANGE_USD[1]}, so ${SEAT_SESSION_MEDIAN_USD * sessions:.2f}. A replay has less overhead, so this is high."
    )
    print(
        f"- Master's check with the plugin: an audit at ${MASTER_CHECK_USD[0]} and a review at ${MASTER_CHECK_USD[1]}."
    )
    print(
        "- So the ticket's $6 basis is too low, and the proposed $30 stop is likely to end the run before 30 sessions."
    )
    print(
        "- The first 2 sessions of the paid run are the trace. Project the total from them, then set --projected-usd."
    )
    print("\nBilling route (exactly one):")
    print(
        "- This login is Claude Max with no API key. total_cost_usd is then a figure, not a charge, and the run uses plan usage that all seats share."
    )
    print(
        "- FRE1730_CLAUDE_OAUTH_TOKEN: from `claude setup-token`, run by the owner. Revoke it after the run."
    )
    print(
        "- FRE1730_ANTHROPIC_API_KEY: a key made for this run. It bills real dollars, and it might share an organization limit with production."
    )
    print("- The production key is not read. `~/.claude/.credentials.json` is not copied.")
    print("\nCredit check:")
    print(
        "- CANNOT BE READ by this harness. It has no way to read the remaining Max plan quota or the API credit."
    )
    print(
        "- The owner reads the plan's usage page or the Console balance, and records it with the go. A 429 or 402 stops the run."
    )
    print("\nBefore the run:")
    print(
        "- Hold during a llama.cpp measurement window. Sessions cannot reach llama.cpp, but they load the CPU."
    )
    print("- `verify-tasks`, then `check-arms` with the pinned plugin. Both are free.")
    print("\nCommands:")
    print("  uv run python -m scripts.eval.fre1730.run verify-tasks")
    print(
        f"  git clone {PONYTAIL_REPO} ~/.cache/seshat-fre1730/ponytail && git -C ~/.cache/seshat-fre1730/ponytail checkout {PONYTAIL_SHA}"
    )
    print(
        f"  uv run python -m scripts.eval.fre1730.run check-arms --plugin-dir ~/.cache/seshat-fre1730/ponytail --marker '{PONYTAIL_MARKER}'"
    )
    print(
        f"  uv run python -m scripts.eval.fre1730.run paid --plugin-dir ~/.cache/seshat-fre1730/ponytail --marker '{PONYTAIL_MARKER}' --hard-stop-usd <owner's number> --projected-usd <from the trace> --owner-go '<quoted>' --trace"
    )
    print("  Then the same command without --trace.")
    return 0


def _cmd_stub_check(tasks: Sequence[Task], plugin: Path, marker: str, name: str) -> int:
    source = main_repo_root()
    task = tasks[0]
    results = SCRATCH / "results" / name
    shutil.rmtree(results, ignore_errors=True)
    venvs = {base_commit(source, task): ensure_venv(source, base_commit(source, task), SCRATCH)}
    sessions = [PlannedSession(task, "plugin", 0), PlannedSession(task, "control", 0)]
    records = _stub_run(sessions, plugin, marker, results, source, SCRATCH, venvs)
    _print_arm_check(records)
    _print_footprint(records)
    for record in records:
        print(
            f"{record['key']}: diff {record['lines_code']}+{record['lines_test']} lines, PR tests {record['pr_tests_passed']} passed, {record['pr_tests_failed']} failed"
        )
    report = render_report(records, plugin_hook_events(plugin), "")
    (results / "report.md").write_text(report)
    print(f"Report: {results / 'report.md'}")
    return 0


def cmd_paid(args: argparse.Namespace, tasks: Sequence[Task]) -> int:
    """Run the paid sessions. This needs the owner's recorded go and the owner's number.

    Args:
        args: The parsed arguments.
        tasks: The tasks.

    Returns:
        The exit code.

    Raises:
        SystemExit: If the go or a number is missing.
    """
    if not args.owner_go.strip():
        raise SystemExit("pass --owner-go with the go recorded on FRE-1730, quoted and dated")
    if args.hard_stop_usd <= 0 or args.projected_usd <= 0:
        raise SystemExit("--hard-stop-usd and --projected-usd must be above zero")
    require_pin(args.plugin_dir)
    route = paid_route(os.environ)
    source = main_repo_root()
    require_verified(tasks, SCRATCH)
    results = SCRATCH / "results" / "paid"
    results.mkdir(parents=True, exist_ok=True)
    (results / "owner_go.txt").write_text(args.owner_go + "\n")
    sessions = plan_sessions(tasks, REPEATS, SEED)
    stop = SpendStop(hard_stop_usd=args.hard_stop_usd, projected_usd=args.projected_usd)
    config = RunConfig(
        source=source,
        scratch=SCRATCH,
        results=results,
        model=MODEL,
        route=route,
        plugin=args.plugin_dir,
        marker=args.marker,
        global_claude_md=GLOBAL_CLAUDE_MD,
        venvs=_venvs(source, tasks, SCRATCH),
    )
    planned = trace_sessions(sessions) if args.trace else sessions
    hook_events = plugin_hook_events(args.plugin_dir)
    records = run_and_report(planned, config, stop, hook_events, args.owner_go)
    print(
        json.dumps(
            {
                "sessions": len(records),
                "spent_usd": stop.spent_usd,
                "report": str(results / "report.md"),
            }
        )
    )
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    """Run a command.

    Args:
        argv: The arguments.

    Returns:
        The exit code.
    """
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("preflight")
    commands.add_parser("verify-tasks")
    commands.add_parser("dry-run")
    check = commands.add_parser("check-arms")
    check.add_argument("--plugin-dir", type=Path, required=True)
    check.add_argument("--marker", required=True)
    paid = commands.add_parser("paid")
    paid.add_argument("--plugin-dir", type=Path, required=True)
    paid.add_argument("--marker", required=True)
    paid.add_argument("--hard-stop-usd", type=float, required=True)
    paid.add_argument("--projected-usd", type=float, required=True)
    paid.add_argument("--owner-go", required=True)
    paid.add_argument("--trace", action="store_true")
    args = parser.parse_args(argv)
    tasks = load_tasks()
    try:
        match args.command:
            case "preflight":
                return cmd_preflight(tasks)
            case "verify-tasks":
                lines = verify_tasks(tasks, main_repo_root(), SCRATCH)
                print("\n".join(lines))
                return 0 if record_verified(tasks, lines, SCRATCH) else EXIT_NOT_USABLE
            case "dry-run":
                return _cmd_stub_check(tasks, FIXTURE_PLUGIN, FIXTURE_MARKER, "dry-run")
            case "check-arms":
                require_pin(args.plugin_dir)
                return _cmd_stub_check(tasks, args.plugin_dir, args.marker, "check-arms")
            case _:
                return cmd_paid(args, tasks)
    except RunStopped as error:
        print(f"RUN STOPPED: {error}", file=sys.stderr)
        return EXIT_STOPPED


if __name__ == "__main__":
    raise SystemExit(main())
