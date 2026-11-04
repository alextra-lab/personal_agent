"""FRE-1730: the five closed tickets to replay."""

from __future__ import annotations

import json
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

HERE = Path(__file__).resolve().parent

PROMPT_HEADER = """\
Implement the ticket below in this repository. The ticket is approved. Linear, GitHub and the network \
are not available, so do not look for them. Do not open a pull request. Leave your change in the working \
tree. Run only the test files that concern the change, one file at a time, with `uv run pytest <file>`. \
Do not run the whole test suite. Do not start a service or a container.

"""


@dataclass(frozen=True)
class Task:
    """One closed ticket.

    Attributes:
        ticket: The ticket's id.
        pr: The merged PR's number.
        merge: The merge commit. Its first parent is the commit before the PR.
        tests: The PR's own test files that score a result.
        risky: The change is in logic that needs a test: money, a budget or a gate.
        reason: Why this ticket is in the set.
    """

    ticket: str
    pr: int
    merge: str
    tests: tuple[str, ...]
    risky: bool
    reason: str

    def prompt(self) -> str:
        """Build the session's prompt. It is the same in both arms.

        Returns:
            The prompt.
        """
        return PROMPT_HEADER + (HERE / "tasks" / f"{self.ticket}.md").read_text()


def load_tasks(path: Path = HERE / "tasks.json") -> Sequence[Task]:
    """Load the task set.

    Args:
        path: The file.

    Returns:
        The tasks.
    """
    return [Task(**{**row, "tests": tuple(row["tests"])}) for row in json.loads(path.read_text())]


def base_commit(source: Path, task: Task) -> str:
    """Find the commit before the PR.

    Args:
        source: The source repo.
        task: The task.

    Returns:
        The commit.
    """
    done = subprocess.run(
        ["git", "-C", str(source), "rev-parse", f"{task.merge}^1"],
        capture_output=True,
        text=True,
        check=True,
    )
    return done.stdout.strip()
