# ruff: noqa: D103
"""Live behavioural proof for the measurement critic (FRE-833, ADR-0113 AC-6/AC-8).

The `critic_reasoning` half of AC-6/AC-8 — a deterministic test cannot cover it:
does the critic, run for real, actually *catch the confound* in a seeded action?
It runs the real `claude -p` critic against the seeded fixtures (a re-embed at the
wrong dimension, a local-vs-cloud precision mixup, a control-plane ruleset change,
and a NOVEL confound not named in the standing guardrails).

Marked `integration` + `requires_llm_server` — **not run in a build session**.
Master/owner runs it at the acceptance gate, per ADR §5::

    uv run pytest tests/scripts/test_measurement_critic_live.py -m requires_llm_server

The mechanical trigger + gate contract (which fixtures fire the critic, and that a
REJECT halts actuation) is proven deterministically in `test_measurement_critic.py`.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from scripts.specialists.measurement_critic import (
    ProposedAction,
    claude_headless_runner,
    guard_action,
)

_FIXTURES = Path("tests/fixtures/specialists/measurement_critic")

pytestmark = [pytest.mark.integration, pytest.mark.requires_llm_server]


def _load(name: str) -> ProposedAction:
    data = json.loads((_FIXTURES / name).read_text(encoding="utf-8"))
    return ProposedAction(
        kind=data["kind"],
        description=data.get("description", ""),
        paths=tuple(data.get("paths", [])),
        params={k: str(v) for k, v in (data.get("params") or {}).items()},
    )


@pytest.mark.parametrize(
    ("fixture", "expect_terms"),
    [
        ("re_embed_4096.json", ("1024", "dimension", "reversib", "ceiling")),
        ("precision_mixup.json", ("precision", "quant", "q4", "provenance", "local")),
        ("control_plane_ruleset.json", ("required check", "ruleset", "reversib", "control")),
        ("novel_confound.json", ("control", "self-select", "confound", "latency")),
    ],
)
def test_critic_rejects_and_names_the_confound(fixture: str, expect_terms: tuple[str, ...]) -> None:
    outcome = guard_action(_load(fixture), specialist_runner=claude_headless_runner())
    assert outcome.triggered is True, f"{fixture} should trigger the mechanical class"
    assert outcome.verdict is not None
    assert outcome.verdict.decision == "REJECT", (
        f"{fixture}: expected REJECT, got {outcome.verdict.decision}: {outcome.verdict.raw_response}"
    )
    assert outcome.actuation_permitted is False
    blob = " ".join(f"{f.category} {f.summary}".lower() for f in outcome.verdict.findings)
    assert any(term in blob for term in expect_terms), (
        f"{fixture}: no finding named the confound ({expect_terms}): "
        f"{[f.summary for f in outcome.verdict.findings]}"
    )


def test_benign_reversible_deploy_is_not_gated_by_the_critic() -> None:
    outcome = guard_action(
        _load("benign_pwa_deploy.json"), specialist_runner=claude_headless_runner()
    )
    assert outcome.triggered is False
    assert outcome.actuation_permitted is True
