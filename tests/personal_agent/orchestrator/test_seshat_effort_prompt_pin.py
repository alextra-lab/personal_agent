"""FRE-1622 (ADR-0157 D7 rule 2, AC-5): Standard's D2 planner prompt is the prompt D7 qualified.

Standard is the default level, so its D2 prompt (a ``planner_asked`` turn, ``admit_single``
true) is a routing configuration under ADR-0154 D7. The hash below is the ``prompt_hash`` of the
probe run posted on FRE-1622. A change to Standard's values in ``config/seshat_effort.yaml``, to
the planner text, or to the sub-agent tools that governance grants changes the prompt, and this
test fails until a new passing D7 run is posted and the hash is updated with it.
"""

from __future__ import annotations

import hashlib

import pytest

from personal_agent.config.seshat_effort import (
    effort_spec,
    get_seshat_effort,
    reset_current_effort,
    set_current_effort,
)
from personal_agent.orchestrator.expansion_controller import (
    _build_planner_system_prompt,
    _current_sub_agent_tool_surface,
)

#: The sub-agent tools governance grants in NORMAL mode, as production and the probe rendered
#: them for the qualifying run (FRE-1622, 2026-10-20).
QUALIFIED_SURFACE = [
    "fetch_url",
    "get_library_docs",
    "measure_model_latency",
    "notes_search",
    "read",
    "read_skill",
    "recall_personal_history",
    "run_python",
    "search_logs",
    "search_memory",
    "web_search",
]

#: ``prompt_hash`` of the passing D7 run for Standard, posted on FRE-1622.
STANDARD_D2_PROMPT_SHA256 = "74d78c3a3581dce553ddcbd1d181fdc535af313a13f8c50cfd9796b5b256d63f"

#: FRE-1678 gave the ``general`` worker ``artifact_read``. Governance now grants a tool that
#: ``QUALIFIED_SURFACE`` lacks, so production renders a different planner prompt. The surface pin
#: waits for a new passing D7 run (owner decision, 2026-10-25: ship first, run D7 in a window the
#: owner orders). When the run passes, add ``artifact_read`` to ``QUALIFIED_SURFACE``, check the
#: new hash, and remove this marker. It is strict, so a pin that passes again fails the suite
#: until the marker is gone. The hash pin below renders the qualified surface, so it still holds.
_WAITS_FOR_D7 = pytest.mark.xfail(
    strict=True, reason="FRE-1678 changed the worker tool surface; waits for a passing D7 run"
)


def test_standard_is_the_default_level() -> None:
    assert get_seshat_effort().default_level == "standard"


@_WAITS_FOR_D7
def test_the_granted_surface_is_the_qualified_surface() -> None:
    assert sorted(_current_sub_agent_tool_surface("pin")) == QUALIFIED_SURFACE


def test_standards_d2_prompt_is_the_qualified_prompt() -> None:
    token = set_current_effort(effort_spec("standard"))
    try:
        prompt = _build_planner_system_prompt(QUALIFIED_SURFACE, admit_single=True)
    finally:
        reset_current_effort(token)
    assert hashlib.sha256(prompt.encode()).hexdigest() == STANDARD_D2_PROMPT_SHA256
