# ruff: noqa: D103
"""Contract test (FRE-376 Phase 3, ADR-0074 §I3).

Every ``bus.publish`` site in ``src/personal_agent/`` must carry both
``trace_id`` and ``session_id`` in the published payload — either as inline
dict-literal keys or as explicit kwargs. Background-task and scheduled
publishes must mint a :class:`TraceContext` and propagate it; allowlist
entries are reserved for legitimately context-free sites (sensors, FastAPI
lifespan), none of which call ``bus.publish``.

This test reuses the AST lint (``scripts/check_identity_threaded.py``) and
asserts no ``bus_publish_missing_identity`` violations remain.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

SRC = Path("src/personal_agent")


def test_no_bus_publish_missing_identity() -> None:
    result = subprocess.run(
        [
            "uv",
            "run",
            "python",
            "scripts/check_identity_threaded.py",
            "--strict",
            str(SRC),
        ],
        capture_output=True,
        text=True,
        cwd=Path(__file__).resolve().parents[3],
    )
    bus_violations = [
        line for line in result.stdout.splitlines() if "bus_publish_missing_identity" in line
    ]
    assert not bus_violations, (
        "bus.publish sites missing trace_id/session_id identity:\n" + "\n".join(bus_violations)
    )
