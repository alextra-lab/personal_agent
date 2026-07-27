# ruff: noqa: D103
"""Contract test (ADR-0125 D5, AC-5).

Content on a path feeding a durable artifact or assembled context must be
stored whole, or shortened with an explicit marker recording that it was
shortened and by how much. This is what actually gates CI for
``scripts/check_evidence_truncation.py``: the script itself is a
pre-commit-only hook (``.pre-commit-config.yaml``), and pre-commit is not
invoked by ``.github/workflows/ci.yml`` — only ordinary pytest/mypy/ruff jobs
are. This test reuses the AST lint and asserts no violation survives the
committed allowlist, so a new silent truncation on an evidence path fails
``backend-unit`` in CI, not just a local commit.

Mirrors ``tests/personal_agent/events/test_bus_publish_carries_identity.py``,
the established pattern for making an AST-lint guard CI-enforcing in this repo.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

SRC = Path("src/personal_agent")


def test_no_silent_evidence_truncation() -> None:
    result = subprocess.run(
        [
            "uv",
            "run",
            "python",
            "scripts/check_evidence_truncation.py",
            str(SRC),
        ],
        capture_output=True,
        text=True,
        cwd=Path(__file__).resolve().parents[3],
    )
    assert result.returncode == 0, (
        "silent truncation on an evidence path (ADR-0125 D5):\n" + result.stdout
    )
