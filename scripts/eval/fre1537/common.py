"""FRE-1511: run directory layout and JSON-lines helpers shared by the probe steps.

A run directory holds everything one probe run writes. It lives under
``telemetry/evaluation/fre1537-planner-probe/`` by default, which is git-ignored because the captured
request bodies hold full prompts.

    <run>/stub-out/        the recording stub's calls
    <run>/captured/        one primary request body per fixture
    <run>/prompts.json     the design A planner prompts, rendered inside the gateway
    <run>/rows/<tag>/      decide.jsonl, timing.jsonl, longhist.jsonl, fingerprint.json, report.*
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path

DEFAULT_RUNS_DIR = Path("telemetry") / "evaluation" / "fre1537-planner-probe"


@dataclass(frozen=True)
class RunPaths:
    """Paths inside one run directory.

    Attributes:
        run_dir: The run directory.
        tag: Names one replay configuration, for example the planner mode.
    """

    run_dir: Path
    tag: str = "thinking_off"

    @property
    def stub_out(self) -> Path:
        """Return the directory the stub container writes to."""
        return self.run_dir / "stub-out"

    @property
    def captured(self) -> Path:
        """Return the directory of captured primary request bodies."""
        return self.run_dir / "captured"

    @property
    def prompts(self) -> Path:
        """Return the rendered design A prompts file."""
        return self.run_dir / "prompts.json"

    @property
    def rows(self) -> Path:
        """Return the directory of this tag's rows, fingerprint and report."""
        return self.run_dir / "rows" / self.tag

    @property
    def decide(self) -> Path:
        """Return the decision rows file."""
        return self.rows / "decide.jsonl"

    @property
    def timing(self) -> Path:
        """Return the timing rows file."""
        return self.rows / "timing.jsonl"

    @property
    def longhist(self) -> Path:
        """Return the long-history rows file."""
        return self.rows / "longhist.jsonl"

    @property
    def fingerprint(self) -> Path:
        """Return the configuration fingerprint file."""
        return self.rows / "fingerprint.json"


def utc_now() -> str:
    """Return the current UTC time as an ISO-8601 string with a ``Z`` suffix."""
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def read_jsonl(path: Path) -> list[dict[str, object]]:
    """Read a JSON-lines file.

    Args:
        path: The file. A missing file reads as empty.

    Returns:
        One dict per non-blank line.
    """
    if not path.exists():
        return []
    return [json.loads(x) for x in path.read_text().splitlines() if x.strip()]


def append_jsonl(path: Path, row: Mapping[str, object]) -> None:
    """Append one row to a JSON-lines file, creating its directory."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as fh:
        fh.write(json.dumps(row) + "\n")


def done_keys(path: Path) -> set[tuple[str, int]]:
    """Return the ``(label, trial)`` keys of rows without an error, so a rerun resumes."""
    return {(str(r["label"]), int(str(r["trial"]))) for r in read_jsonl(path) if "error" not in r}


def iter_nonempty(items: Iterable[str]) -> Iterator[str]:
    """Yield the stripped, non-empty strings of ``items``."""
    for item in items:
        if item.strip():
            yield item.strip()
