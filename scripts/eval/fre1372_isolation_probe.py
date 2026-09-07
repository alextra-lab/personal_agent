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
from scripts.eval.eval_isolation import IsolatedArmRunner, create_eval_driver
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


async def amain() -> int:
    """Run the two-arm probe and check AC-1. Returns a process exit code."""
    driver = create_eval_driver()
    runner = IsolatedArmRunner(driver=driver)
    try:
        async with httpx.AsyncClient() as http, httpx.AsyncClient() as es:
            arm1 = await runner.run_turn(http, es, PROBE_MESSAGE, arm="control")
            arm2 = await runner.run_turn(http, es, PROBE_MESSAGE, arm="control")

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
