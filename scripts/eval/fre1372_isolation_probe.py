r"""FRE-1372 AC-3's proof — deliberately unremarkable.

Drives two turns via ``IsolatedArmRunner``, the shared, sanctioned way any eval script
reaches the isolated eval gateway. This script contains no wipe call, no substrate-guard
import beyond what proves the outcome afterwards, and no isolation logic of its own —
isolation is a side effect of using ``IsolatedArmRunner`` to reach the gateway at all.

The check itself reads the whole eval graph after arm 2, rather than intercepting
``search_memory``'s literal returned payload, because AC-1 explicitly rules out a
model-choice-dependent check ("Fails if... weakened to arm 2 did not *cite* arm 1's
entity — non-citation is a model choice"). Graph-absence holds regardless of whether the
model chose to call ``search_memory`` this turn, or with what query — the same approach
``fre1337_intent_probe/behavioral.py``'s own ``run_contamination_proof`` already uses.

Usage (after ``make eval-infra-up``, with ``NEO4J_PASSWORD`` set)::

    uv run python -m scripts.eval.fre1372_isolation_probe
"""

from __future__ import annotations

import asyncio

import httpx
import structlog
from scripts.eval.eval_isolation import ArmTurnResult, IsolatedArmRunner, create_eval_driver
from scripts.eval.fre1337_intent_probe.substrate import (
    EVAL_NEO4J_URI,
    fetch_originating_session_ids,
    find_cross_session_sources,
)

log = structlog.get_logger(__name__)

#: Same question both arms — the shape AC-1 asks for.
PROBE_MESSAGE = (
    "My favorite programming language is Rust. What is my favorite programming language?"
)


def _extraction_failure(*, first: ArmTurnResult, second: ArmTurnResult) -> str | None:
    """Name which turn's extraction never settled, or ``None`` if both did.

    Both turns drive the same ``arm="control"`` (AC-1's "same question both arms"
    shape), so ``ArmTurnResult.arm`` is identical for both and cannot tell them
    apart — this names failures by call order and ``session_id`` instead.

    A leaked-node comparison against an eval graph that never received an
    extraction is a comparison of two empty sets: it always reports "no leak"
    whether or not isolation holds. This is the check the 2026-09-07 Verify
    Failed comment names — ``require_nonzero=True`` was computed by ``run_turn``
    and then logged past, so an unsettled run still exited 0.

    Args:
        first: The first turn's result.
        second: The second turn's result.

    Returns:
        A message naming the first unsettled turn, or ``None`` if both settled.
    """
    if not first.extraction_settled:
        return f"first turn (session {first.session_id}) never settled entity extraction"
    if not second.extraction_settled:
        return f"second turn (session {second.session_id}) never settled entity extraction"
    return None


async def amain() -> int:
    """Run the two-arm probe and check AC-1. Returns a process exit code."""
    driver = create_eval_driver()
    runner = IsolatedArmRunner(driver=driver)
    try:
        async with httpx.AsyncClient() as http, httpx.AsyncClient() as es:
            arm1 = await runner.run_turn(http, es, PROBE_MESSAGE, arm="control")
            arm2 = await runner.run_turn(http, es, PROBE_MESSAGE, arm="control")

        failure = _extraction_failure(first=arm1, second=arm2)
        if failure is not None:
            log.error("fre1372_isolation_probe_extraction_not_settled", detail=failure)
            print(f"FRE-1372 AC-1 FAILED: {failure} — a run that mints nothing proves nothing.")
            return 1

        current_sources = await fetch_originating_session_ids(driver, uri=EVAL_NEO4J_URI)
        leaked = find_cross_session_sources(current_sources, arm1.session_id)
        log.info(
            "fre1372_isolation_probe",
            arm1_session=arm1.session_id,
            arm2_session=arm2.session_id,
            leaked_count=len(leaked),
        )
        if leaked:
            print(
                f"FRE-1372 AC-1 FAILED: arm 2's graph carries {len(leaked)} node(s) from arm 1: {leaked}"
            )
            return 1
        print(
            f"FRE-1372 AC-1 held: arm 2 ({arm2.session_id}) carries nothing from arm 1 ({arm1.session_id})"
        )
        return 0
    finally:
        await driver.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(amain()))
