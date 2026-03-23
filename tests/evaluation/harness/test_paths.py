"""Parameterized pytest tests for all 25 evaluation conversation paths.

Each path runs as a separate test case. Tests are marked with
@pytest.mark.evaluation and require the live agent service.

Run:
    uv run pytest tests/evaluation/harness/test_paths.py -v -m evaluation

Run a single path:
    uv run pytest tests/evaluation/harness/test_paths.py -v -k "CP_01"

Run a category:
    uv run pytest tests/evaluation/harness/test_paths.py -v -k "Intent"
"""

from __future__ import annotations

import pytest

from tests.evaluation.harness.dataset import ALL_PATHS
from tests.evaluation.harness.models import ConversationPath
from tests.evaluation.harness.runner import EvaluationRunner


@pytest.mark.evaluation
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path",
    ALL_PATHS,
    ids=[p.path_id for p in ALL_PATHS],
)
async def test_conversation_path(
    path: ConversationPath,
    evaluation_runner: EvaluationRunner,
    agent_healthy: None,
) -> None:
    """Execute a conversation path and verify telemetry assertions.

    Quality criteria are NOT checked here — they require human judgment.
    This test only verifies machine-checkable telemetry assertions.

    Paths with setup_notes (e.g., CP-18) may need manual setup before
    running. If the path has no telemetry assertions, it passes
    automatically (quality-only paths).
    """
    if path.setup_notes:
        pytest.skip(f"Requires manual setup: {path.setup_notes[:80]}...")

    result = await evaluation_runner.run_path(path)

    # Build detailed failure message
    if not result.all_assertions_passed:
        failures = []
        for turn in result.turns:
            for a in turn.assertion_results:
                if not a.passed:
                    failures.append(
                        f"  Turn {turn.turn_index + 1} "
                        f"(trace={turn.trace_id}): {a.message}"
                    )
        failure_msg = (
            f"{path.path_id} ({path.name}): "
            f"{result.failed_assertions}/{result.total_assertions} "
            f"assertions failed:\n" + "\n".join(failures)
        )
        pytest.fail(failure_msg)
