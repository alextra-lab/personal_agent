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


def test_freshness_assertion_targets_every_arm_driven() -> None:
    """FRE-1350 supersedes FRE-1341's control-only assertion — but does not weaken it.

    The original asserted `assert_gateway_fresh(http, EVAL_CHAT_BASE_URL` — a literal
    pinning the check to the control gateway. Arm 3 now drives control AND treatment,
    which are separate containers that can be stale independently, so pinning to control
    would leave treatment silently unguarded: exactly the shape of the gap FRE-1341
    itself recorded on `run_contamination_proof`.

    The check is therefore keyed on the arm under test, and must sit inside the arm loop
    so every arm is asserted rather than only the first.
    """
    source = inspect.getsource(behavioral.run_behavioral_arm)
    assert "assert_gateway_fresh(http, EVAL_ARMS[arm]" in source, (
        "freshness must be asserted for the arm actually being driven, not a fixed "
        "control constant — two arms, two containers, two chances to be stale"
    )
    arm_loop = source.index("for arm in arms:")
    freshness_call = source.index("assert_gateway_fresh(")
    assert arm_loop < freshness_call, (
        "the assertion must be INSIDE the arm loop; before it, only one arm would ever "
        "be checked no matter how many are driven"
    )
