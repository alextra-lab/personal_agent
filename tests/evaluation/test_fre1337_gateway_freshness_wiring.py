"""FRE-1341 — arm 3 must refuse against a stale eval gateway, not silently measure it.

`run_behavioral_arm` needs a live Neo4j driver and gateway to actually execute (it's
"exercised live per the harness README" by design — see
`test_fre1337_behavioral_completeness.py`'s module docstring for the same precedent on
this file). This checks the wiring structurally: the freshness assertion is present, and
runs before the fixture loop starts driving the gateway. The live behavior (refuses on a
stale image, passes after rebuild) is proven in the PR's AC-1 evidence, not here.
"""

from __future__ import annotations

import inspect

from scripts.eval.fre1337_intent_probe import behavioral


def test_run_behavioral_arm_asserts_gateway_freshness_before_the_fixture_loop() -> None:
    source = inspect.getsource(behavioral.run_behavioral_arm)
    assert "assert_gateway_fresh(" in source, (
        "run_behavioral_arm must assert the eval gateway is fresh before driving it "
        "(FRE-1341) — a stale cached image used to serve silently, with /health "
        "reporting healthy throughout."
    )
    freshness_call = source.index("assert_gateway_fresh(")
    fixture_loop = source.index("for fixture in fixtures:")
    assert freshness_call < fixture_loop, (
        "the freshness assertion must run before any fixture is driven through the "
        "gateway, not after"
    )


def test_freshness_assertion_targets_the_control_gateway() -> None:
    source = inspect.getsource(behavioral.run_behavioral_arm)
    assert "assert_gateway_fresh(http, EVAL_CHAT_BASE_URL" in source
