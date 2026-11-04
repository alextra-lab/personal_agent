"""FRE-1730: score one result. Everything that touches the session's repo runs in a jail.

A session can write ``.git/config`` and the code under test. So neither git nor pytest runs on the host against them.
"""

from __future__ import annotations

import re
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from scripts.eval.fre1730.jail import Jail, run_in_jail
from scripts.eval.fre1730.spend import RunStopped
from scripts.eval.fre1730.tasks import Task

_PYTEST_TIMEOUT_S = 600
_ENV = {
    "UV_NO_SYNC": "1",
    "UV_OFFLINE": "1",
    "UV_CACHE_DIR": "/tmp/uv-cache",
    "PYTHONDONTWRITEBYTECODE": "1",
}
_USUAL_IGNORED = (".venv", "__pycache__", ".pytest_cache", ".ruff_cache", ".mypy_cache")
_ADDED_TEST = re.compile(r"^\+[ \t]*(?:async[ \t]+)?def[ \t]+(test_\w+)", re.MULTILINE)
_REMOVED_TEST = re.compile(r"^-[ \t]*(?:async[ \t]+)?def[ \t]+(test_\w+)", re.MULTILINE)
_COUNT = re.compile(r"(\d+) (passed|failed|errors?)")
#: A library can log after pytest's summary, so the summary is the last line of this form.
_SUMMARY = re.compile(
    r"^=*\s*\d+ (?:passed|failed|errors?|skipped|deselected|xfailed|xpassed|warnings?)\b.* in \d"
)


@dataclass(frozen=True)
class DiffScore:
    """The size of a result.

    Attributes:
        lines_code: Lines added or removed outside the test files.
        lines_test: Lines added or removed in the test files.
        tests_added: The count of new test functions.
        diff: The diff.
    """

    lines_code: int
    lines_test: int
    tests_added: int
    diff: str


@dataclass(frozen=True)
class PrTestScore:
    """The PR's own tests against a result.

    Attributes:
        passed: Tests that passed.
        failed: Tests that failed or could not run.
        exit_code: pytest's exit code.
        tail: The end of pytest's output.
    """

    passed: int
    failed: int
    exit_code: int
    tail: str

    @property
    def fraction(self) -> float:
        """Return the share that passed.

        Returns:
            The share. A run that collected nothing scores 0.
        """
        total = self.passed + self.failed
        return self.passed / total if total else 0.0


def _is_test_path(path: str) -> bool:
    return path.startswith("tests/") or Path(path).name.startswith("test_")


def parse_numstat(numstat: str) -> tuple[int, int]:
    """Add up ``git diff --numstat``.

    Args:
        numstat: The output.

    Returns:
        Lines outside the test files, and lines in them. A binary file counts as 0.
    """
    code, test = 0, 0
    for line in numstat.splitlines():
        added, removed, path = line.split("\t", 2)
        count = int(added) + int(removed) if added != "-" else 0
        if _is_test_path(path):
            test += count
        else:
            code += count
    return code, test


def count_new_tests(test_hunks: str) -> int:
    """Count the new test functions. A test that was edited or moved has the same name on a removed line.

    Args:
        test_hunks: The diff of the test files.

    Returns:
        The count.
    """
    removed = set(_REMOVED_TEST.findall(test_hunks))
    return sum(1 for name in _ADDED_TEST.findall(test_hunks) if name not in removed)


def git(jail: Jail, *args: str) -> str:
    """Run git in the jail. A failure must not read as a small diff.

    Args:
        jail: The session's jail, without the plugin.
        *args: The arguments.

    Returns:
        The output.

    Raises:
        RunStopped: If git fails.
    """
    done = run_in_jail(jail, ["git", *args])
    if done.returncode != 0:
        raise RunStopped(f"git {args[0]} failed: {done.stderr[-300:]}")
    return done.stdout


def score_diff(jail: Jail, base: str) -> DiffScore:
    """Measure the result against the base, committed or not.

    Args:
        jail: The session's jail, without the plugin.
        base: The base commit.

    Returns:
        The score.
    """
    git(jail, "add", "-A")
    numstat = git(jail, "diff", "--cached", "--numstat", base)
    diff = git(jail, "diff", "--cached", base)
    code, test = parse_numstat(numstat)
    test_hunks = "\n".join(
        part for part in diff.split("diff --git ") if _is_test_path(part.split(" ", 1)[0][2:])
    )
    return DiffScore(code, test, count_new_tests(test_hunks), diff)


def ignored_paths(jail: Jail) -> list[str]:
    """List ignored files other than the usual caches. A session's own ignore rule can hide a change from the diff.

    Args:
        jail: The session's jail, without the plugin.

    Returns:
        The paths, for the owner to read.
    """
    listing = git(
        jail, "ls-files", "--others", "--ignored", "--exclude-standard", "--directory"
    ).splitlines()
    return [path for path in listing if not any(cache in path for cache in _USUAL_IGNORED)]


def copy_pr_tests(source: Path, task: Task, jail: Jail) -> None:
    """Replace the PR's test files with the merged ones, inside the jail.

    A session can leave a test path as a link to a host file. So the write runs where a link cannot reach the host.

    Args:
        source: The trusted source repo.
        task: The task.
        jail: The session's jail, without the plugin.

    Raises:
        RunStopped: If a file cannot be written.
    """
    for test in task.tests:
        content = subprocess.run(
            ["git", "-C", str(source), "show", f"{task.merge}:{test}"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        done = run_in_jail(
            jail,
            ["sh", "-c", 'mkdir -p "$(dirname "$1")" && rm -f "$1" && cat > "$1"', "sh", test],
            stdin_text=content,
        )
        if done.returncode != 0:
            raise RunStopped(f"{task.ticket}: could not write {test}: {done.stderr[-300:]}")


def parse_pytest(output: str, exit_code: int) -> PrTestScore:
    """Read pytest's last summary line.

    Args:
        output: pytest's output.
        exit_code: pytest's exit code.

    Returns:
        The score. A run with no count scores one failure, because no test is known to have run.
    """
    lines = output.strip().splitlines()
    summary = next((line for line in reversed(lines) if _SUMMARY.match(line)), "")
    counts = {kind: int(number) for number, kind in _COUNT.findall(summary)}
    passed = counts.get("passed", 0)
    failed = counts.get("failed", 0) + counts.get("error", 0) + counts.get("errors", 0)
    if passed + failed == 0:
        failed = 1
    return PrTestScore(passed, failed, exit_code, "\n".join(lines[-5:]))


def run_pr_tests(jail: Jail, tests: Sequence[str]) -> PrTestScore:
    """Run the PR's test files with no network.

    Args:
        jail: The session's jail, without the plugin.
        tests: The test files.

    Returns:
        The score. A session's code that hangs scores one failure.
    """
    if not tests:
        return PrTestScore(0, 0, 0, "no tests")
    try:
        done = run_in_jail(
            jail,
            [
                ".venv/bin/python",
                "-m",
                "pytest",
                "-q",
                "-p",
                "no:cacheprovider",
                "-m",
                "not integration",
                *tests,
            ],
            env=_ENV,
            timeout_s=_PYTEST_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired:
        return PrTestScore(0, 1, -1, "pytest passed its time limit")
    return parse_pytest(done.stdout + done.stderr, done.returncode)
