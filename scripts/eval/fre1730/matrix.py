"""FRE-1730: run the sessions, one at a time, and stop on anything that makes the run wasteful or unsafe."""

from __future__ import annotations

import fcntl
import hashlib
import json
import shutil
import subprocess
import uuid
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from scripts.eval.fre1730.footprint import Footprint, compare, snapshot
from scripts.eval.fre1730.jail import Jail
from scripts.eval.fre1730.score import (
    DiffScore,
    PrTestScore,
    copy_pr_tests,
    ignored_paths,
    run_pr_tests,
    score_diff,
)
from scripts.eval.fre1730.session import (
    Route,
    find_transcript,
    prepare_home,
    prepare_repo,
    run_session,
)
from scripts.eval.fre1730.spend import SESSION_CAP_USD, RunLocked, RunStopped, SpendStop
from scripts.eval.fre1730.tasks import Task, base_commit
from scripts.eval.fre1730.transcript import ARMS, Arm, arm_problems, read_transcript

SESSION_TIMEOUT_S = 1800
BUDGET_SUBTYPE = "error_max_budget_usd"
PENDING_SUBTYPE = "pending"


@dataclass(frozen=True)
class PlannedSession:
    """One session in the run.

    Attributes:
        task: The task.
        arm: The arm.
        repeat: The repeat's number, from 0.
    """

    task: Task
    arm: Arm
    repeat: int

    @property
    def key(self) -> str:
        """Return the session's name.

        Returns:
            The name.
        """
        return f"{self.task.ticket}-{self.arm}-{self.repeat}"


def plan_sessions(tasks: Sequence[Task], repeats: int, seed: str) -> list[PlannedSession]:
    """Mix the arms in a fixed order. Time of day and the model's load then fall on both arms.

    Args:
        tasks: The tasks.
        repeats: The runs per task and arm.
        seed: The order's seed.

    Returns:
        The sessions in order.
    """
    sessions = [
        PlannedSession(task, arm, repeat)
        for task in tasks
        for arm in ARMS
        for repeat in range(repeats)
    ]
    return sorted(sessions, key=lambda s: hashlib.sha256(f"{seed}:{s.key}".encode()).hexdigest())


@dataclass(frozen=True)
class RunConfig:
    """One run.

    Attributes:
        source: The source repo.
        scratch: The scratch directory for run directories.
        results: The results directory. It survives a stop.
        model: The exact model id.
        route: The route to a model.
        plugin: The plugin for the plugin arm.
        marker: The text that only the plugin adds.
        global_claude_md: The owner's global rules.
        venvs: The environment for each task's base commit. Empty on a run that has no tests.
        stub_address: The stub's address on a stub run.
        extra_env: More session variables.
    """

    source: Path
    scratch: Path
    results: Path
    model: str
    route: Route
    plugin: Path
    marker: str
    global_claude_md: Path | None
    venvs: Mapping[str, Path]
    stub_address: tuple[str, int] | None = None
    extra_env: Mapping[str, str] | None = None

    @property
    def ledger(self) -> Path:
        """Return the file that holds one line per finished session.

        Returns:
            The path.
        """
        return self.results / "results.jsonl"


def load_records(config: RunConfig) -> list[dict[str, object]]:
    """Read the sessions. The last line of an attempt wins, so an attempt with only its first line stays charged.

    Args:
        config: The run.

    Returns:
        The records.

    Raises:
        RunStopped: If a line cannot be read, because the spend is then unknown.
    """
    if not config.ledger.exists():
        return []
    latest: dict[str, dict[str, object]] = {}
    for number, line in enumerate(config.ledger.read_text().splitlines(), start=1):
        try:
            record = json.loads(line)
        except ValueError as error:
            raise RunStopped(f"line {number} of {config.ledger} is damaged: {error}") from error
        latest[str(record["attempt"])] = record
    return list(latest.values())


def _append(config: RunConfig, record: Mapping[str, object]) -> None:
    config.results.mkdir(parents=True, exist_ok=True)
    with config.ledger.open("a") as ledger:
        ledger.write(json.dumps(record) + "\n")


@contextmanager
def one_run_at_a_time(scratch: Path) -> Iterator[None]:
    """Hold the lock that keeps a second run from starting.

    Args:
        scratch: The scratch directory.

    Yields:
        Nothing.

    Raises:
        RunStopped: If another run holds the lock.
    """
    scratch.mkdir(parents=True, exist_ok=True)
    with open(scratch / "run.lock", "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            raise RunLocked("another FRE-1730 run holds the lock") from error
        yield


def _run_one(planned: PlannedSession, config: RunConfig, stop: SpendStop) -> dict[str, object]:
    task, arm = planned.task, planned.arm
    run_root = config.scratch / "runs" / hashlib.sha256(planned.key.encode()).hexdigest()[:12]
    shutil.rmtree(run_root, ignore_errors=True)
    base = base_commit(config.source, task)
    prepare_repo(config.source, base, run_root / "repo")
    prepare_home(run_root / "home", config.global_claude_md)
    venv = config.venvs.get(base)
    plugin = config.plugin if arm == "plugin" else None
    before = snapshot(run_root / "home")
    budget_usd = stop.session_budget_usd
    attempt = uuid.uuid4().hex
    identity = {
        "attempt": attempt,
        "key": planned.key,
        "ticket": task.ticket,
        "arm": arm,
        "repeat": planned.repeat,
        "risky": task.risky,
        "budget_usd": budget_usd,
    }
    # A kill of the harness must not lose this session's spend, so the charge is written before the session starts.
    _append(
        config,
        {
            **identity,
            "cost_usd": budget_usd,
            "reported_cost_usd": None,
            "is_error": True,
            "exit_code": -1,
            "subtype": PENDING_SUBTYPE,
            "problems": ["the harness ended during this session, so the ledger charges its budget"],
        },
    )
    result = run_session(
        run_root=run_root,
        prompt=task.prompt(),
        model=config.model,
        route=config.route,
        budget_usd=budget_usd,
        timeout_s=SESSION_TIMEOUT_S,
        plugin=plugin,
        venv=venv,
        stub_address=config.stub_address,
        extra_env=config.extra_env,
    )
    problems: list[str] = []
    cost_usd = result.cost_usd
    if cost_usd is None or (result.exit_code != 0 and not result.is_error):
        # The ledger is the stop's memory across restarts, so an unknown spend counts as the most it can be.
        cost_usd = budget_usd
        problems.append(
            f"the spend is unknown, so the ledger charges the session's budget: {result.subtype}"
        )
    stop.add(cost_usd)

    diffs = config.results / "diffs"
    transcripts = config.results / "transcripts"
    footprint = Footprint((), (), ())
    try:
        diffs.mkdir(parents=True, exist_ok=True)
        transcripts.mkdir(parents=True, exist_ok=True)
        footprint = compare(before, snapshot(run_root / "home"))
        transcript = find_transcript(run_root / "home", result.session_id)
        if transcript is None:
            problems.append("no transcript")
        else:
            problems += arm_problems(read_transcript(transcript, config.marker), arm)
            shutil.copyfile(transcript, transcripts / f"{planned.key}.jsonl")
    except (ValueError, KeyError, AttributeError, TypeError, OSError) as error:
        problems.append(f"the session's files cannot be read: {error!r}")

    # A failure here must not lose the session's spend, so the caller always receives a record.
    diff = DiffScore(0, 0, 0, "")
    tests = PrTestScore(0, 0, 0, "not run")
    ignored: list[str] = []
    diff_path = diffs / f"{planned.key}.diff"
    score_jail = Jail(run_root=run_root, venv=venv)
    try:
        diff = score_diff(score_jail, base)
        diff_path.write_text(diff.diff)
        ignored = ignored_paths(score_jail)
        if task.tests and venv is not None:
            copy_pr_tests(config.source, task, score_jail)
            tests = run_pr_tests(score_jail, task.tests)
    except (subprocess.SubprocessError, RunStopped, ValueError, OSError) as error:
        problems.append(f"scoring failed: {error!r}")
    shutil.rmtree(run_root, ignore_errors=True)

    return {
        **identity,
        "model": config.model,
        "route": config.route.kind,
        "session_id": result.session_id,
        "cost_usd": cost_usd,
        "reported_cost_usd": result.cost_usd,
        "is_error": result.is_error,
        "exit_code": result.exit_code,
        "subtype": result.subtype,
        "api_error_status": result.api_error_status,
        "turns": result.turns,
        "duration_s": result.duration_s,
        "refused": list(result.refused),
        "stderr_tail": result.stderr_tail,
        "lines_code": diff.lines_code,
        "lines_test": diff.lines_test,
        "tests_added": diff.tests_added,
        "ignored_paths": ignored,
        "pr_tests_passed": tests.passed,
        "pr_tests_failed": tests.failed,
        "pr_tests_fraction": tests.fraction,
        "footprint": list(footprint.paths()),
        "footprint_lines": footprint.lines(),
        "footprint_digests": dict(footprint.digests),
        "problems": problems,
        "diff_path": str(diff_path),
    }


def _forged_success(record: Mapping[str, object]) -> bool:
    """Find a success result from a process that did not exit cleanly.

    The real result comes last from a live ``claude``, so such a result was written by something else.

    Args:
        record: The session's record.

    Returns:
        The result cannot be trusted.
    """
    return record["exit_code"] != 0 and not record["is_error"]


def is_valid(record: Mapping[str, object]) -> bool:
    """Decide if a session counts. Only such a session is done, and only such a session is in the report.

    A session that reached the fixed budget counts. It is what that arm did, and leaving it out would hide the costly
    runs of one arm. The report counts such sessions per arm. A session that the nearby cap gave a smaller budget does
    not count, because its peers had more.

    Args:
        record: The session's record.

    Returns:
        The session ended normally or on its budget, and its arm check passed.
    """
    if record["problems"] or _forged_success(record):
        return False
    if not record["is_error"]:
        return True
    return (
        record["subtype"] == BUDGET_SUBTYPE and float(str(record["budget_usd"])) >= SESSION_CAP_USD
    )


def _stop_reason(record: Mapping[str, object]) -> str | None:
    """Decide if a finished session must stop the run.

    A session that reached its budget is valid, and the next ``SpendStop.check`` ends the run if the cap is reached.

    Args:
        record: The session's record.

    Returns:
        The reason, or ``None``.
    """
    if _forged_success(record):
        return f"{record['key']}: a success result, but exit code {record['exit_code']}. The spend is unknown."
    if record["problems"]:
        return f"{record['key']}: invalid session: {record['problems']}"
    if record["is_error"] and record["subtype"] != BUDGET_SUBTYPE:
        return (
            f"{record['key']}: the session ended on an error: subtype {record['subtype']}, "
            f"API status {record['api_error_status']}. A quota or credit error must go to master."
        )
    return None


def run_matrix(
    sessions: Sequence[PlannedSession], config: RunConfig, stop: SpendStop
) -> list[dict[str, object]]:
    """Run the sessions that have no record yet, one at a time.

    The ledger's spend counts, so a restart cannot reset the stop. A session that is not valid runs again after a
    restart, and its spend still counts.

    Args:
        sessions: The plan.
        config: The run.
        stop: The stop.

    Returns:
        All records of this run.

    Raises:
        RunStopped: If the run must stop. Finished sessions stay in the ledger.
    """
    config.results.mkdir(parents=True, exist_ok=True)
    records = load_records(config)
    done = {str(record["key"]) for record in records if is_valid(record)}
    stop.spent_usd = sum(float(str(record["cost_usd"])) for record in records)
    with one_run_at_a_time(config.scratch):
        for planned in sessions:
            if planned.key in done:
                continue
            stop.check()
            record = _run_one(planned, config, stop)
            _append(config, record)
            records.append(record)
            reason = _stop_reason(record)
            if reason:
                raise RunStopped(reason)
    return records
