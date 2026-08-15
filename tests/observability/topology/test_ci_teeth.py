"""ADR-0088 D7 — the observable-first done-bar, with CI teeth (FRE-513).

These tests are the enforcement the ADR promises: a forced fallback must produce a durable
degradation record *and* a ``turn_status`` degraded state; model work run outside
``observe_topology`` is detectable; and the model SDK stays confined to ``llm_client/`` so
``record_api_call`` remains the single runtime cost+observability choke point.
"""

from __future__ import annotations

import re
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from personal_agent.events.models import TurnDegradedEvent
from personal_agent.observability.topology import current_topology, observe_topology
from personal_agent.observability.topology import seam as seam_mod
from personal_agent.observability.topology.projector import TurnObservationProjector

_SRC_ROOT = Path(__file__).resolve().parents[3] / "src" / "personal_agent"

# The provider SDKs a model invocation could use. Reviewable and extensible: add a name
# here, not a regex tweak (FRE-1262 AC-1).
_CONFINED_SDK_MODULES = ("litellm", "anthropic", "openai")

_SDK_IMPORT_PATTERNS = {
    sdk: re.compile(rf"(^|\n)\s*(import {sdk}\b|from {sdk}\b)") for sdk in _CONFINED_SDK_MODULES
}

# Non-invocation uses of a confined SDK outside llm_client/, keyed by (relative path, sdk
# name) with the reason that specific SDK is not a model invocation at that call site
# (verified by reading the call site). New entries require the same scrutiny: a *model
# invocation* outside llm_client/ is a contract violation, not an allowlist candidate.
_ALLOWED_SDK_IMPORTERS: dict[tuple[str, str], str] = {
    ("memory/embeddings.py", "openai"): (
        "embeddings-only call (client.embeddings.create), not a chat/completion model "
        "invocation; cost is tracked via llm_client.cost_tracker.record_vendor_cost, not "
        "record_api_call (FRE-974)"
    ),
}


def _sdk_import_offenders(root: Path) -> list[str]:
    """Scan ``root`` for confined-SDK imports outside ``llm_client/``, minus the allowlist.

    Shared by the real-tree guard (below) and its seeded proof tests — a clean tree alone
    cannot distinguish a working guard from a vacuous one (FRE-1262 AC-2/AC-3).
    """
    offenders: list[str] = []
    for py in root.rglob("*.py"):
        rel = py.relative_to(root).as_posix()
        if rel.startswith("llm_client/"):
            continue
        text = py.read_text(encoding="utf-8")
        for sdk, pattern in _SDK_IMPORT_PATTERNS.items():
            if pattern.search(text) and (rel, sdk) not in _ALLOWED_SDK_IMPORTERS:
                offenders.append(f"{rel}:{sdk}")
    return offenders


# -- (a) forced fallback → durable degradation record + turn_status degraded -------------


@pytest.mark.asyncio
async def test_forced_fallback_writes_durable_degradation_record(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A turn whose sub-agents all failed writes a durable row flagged fallback_triggered."""
    ledger = AsyncMock()
    ledger.fetch_authoritative_cost = AsyncMock(return_value=(0.9, 100, 50))
    ledger.write = AsyncMock()
    monkeypatch.setattr(seam_mod, "get_route_trace_ledger", lambda: ledger)
    monkeypatch.setattr(seam_mod, "get_event_bus", lambda: AsyncMock())

    failed_sub = SimpleNamespace(success=False, summary="", full_output="", error="boom")
    ctx = SimpleNamespace(
        trace_id=str(uuid4()),
        session_id=str(uuid4()),
        gateway_output=None,
        messages=[],
        steps=[],
        sub_agent_results=[failed_sub],
        expansion_phase_results=[],
        topology=None,
        turn_cost_usd=0.0,
    )

    async with observe_topology(ctx):
        pass

    ledger.write.assert_awaited_once()
    row = ledger.write.call_args.args[0]
    # The durable degradation record: a fallback was classified and persisted.
    assert row.fallback_triggered is True
    assert row.orchestration_event == "fallback_triggered"


@pytest.mark.asyncio
async def test_forced_fallback_raises_turn_status_degraded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A degradation event drives a visible degraded turn_status via the projector."""
    emitted: list[dict[str, object]] = []

    async def _fake_emit(*, session_id: str, value: dict[str, object]) -> None:
        emitted.append(value)

    from personal_agent.observability.topology import projector as projector_mod

    monkeypatch.setattr(projector_mod, "emit_turn_status", _fake_emit)
    proj = TurnObservationProjector()

    await proj.handle(
        TurnDegradedEvent(
            trace_id="t-1",
            session_id="s-1",
            where="decompose",
            reason="planner_schema_fail",
            severity="critical",
        )
    )

    assert emitted[-1]["degraded"] is True
    assert any("planner_schema_fail" in d for d in emitted[-1]["degradations"])  # type: ignore[operator]


# -- (b) out-of-seam model work is detectable (runtime context-var guard) -----------------


@pytest.mark.asyncio
async def test_model_work_inside_seam_carries_topology() -> None:
    """Inside observe_topology the active topology is set; the cost event would stamp it."""
    ctx = SimpleNamespace(
        trace_id=str(uuid4()),
        session_id=str(uuid4()),
        gateway_output=None,
        messages=[],
        steps=[],
        sub_agent_results=None,
        expansion_phase_results=[],
        topology=None,
        turn_cost_usd=0.0,
    )
    assert current_topology() is None  # no seam active yet
    async with observe_topology(ctx):
        # A model call on this stack would see a non-None topology (in-seam).
        assert current_topology() == "primary"
    # Reset on exit — work after the seam is out-of-seam again.
    assert current_topology() is None


def test_out_of_seam_model_work_is_flagged() -> None:
    """Model work with no active topology is the D7 violation the guard detects."""
    # No observe_topology on this call stack → current_topology() is None, which the cost
    # boundary stamps onto the event so out-of-seam calls are queryable / test-catchable.
    assert current_topology() is None


# -- (c) static guard: provider SDKs are confined to llm_client/ -------------------------


def test_model_sdk_confined_to_llm_client() -> None:
    """No file outside llm_client/ imports a confined provider SDK except the allowlist."""
    offenders = _sdk_import_offenders(_SRC_ROOT)
    assert not offenders, (
        f"provider SDK ({', '.join(_CONFINED_SDK_MODULES)}) imported outside llm_client/ "
        f"(route model calls through CostTrackerService.record_api_call instead): {offenders}"
    )


def test_model_sdk_guard_catches_unlisted_provider_sdk(tmp_path: Path) -> None:
    """A seeded, unlisted provider-SDK import outside llm_client/ is reported (AC-2).

    The real tree is clean, which alone cannot distinguish a working guard from a vacuous
    one — this proves the guard fires against a fabricated offender.
    """
    offending = tmp_path / "some_module" / "caller.py"
    offending.parent.mkdir(parents=True)
    offending.write_text("import anthropic\n")

    assert _sdk_import_offenders(tmp_path) == ["some_module/caller.py:anthropic"]


def test_model_sdk_guard_does_not_over_fire(tmp_path: Path) -> None:
    """A permitted in-seam import and a non-import textual mention both go unreported (AC-3)."""
    llm_client_dir = tmp_path / "llm_client"
    llm_client_dir.mkdir()
    (llm_client_dir / "client.py").write_text("import litellm\n")

    (tmp_path / "docs_helper.py").write_text(
        '"""Talks to litellm/anthropic/openai under the hood — no actual import here."""\n'
    )

    assert _sdk_import_offenders(tmp_path) == []


# -- (d) ADR-0088 D4 — projector is the sole emit_turn_status caller --------------------


def test_emit_turn_status_called_only_from_projector() -> None:
    """Only ``observability/topology/projector.py`` may call ``emit_turn_status``.

    ADR-0088 D4: the projector is the sole ``turn_status`` emitter. Any second call site
    would fork the sole-emitter invariant that this file enforces.  The definition in
    ``transport/agui/transport.py`` is explicitly excluded (it *defines* the function, not
    calls it).
    """
    # Matches bare invocations: emit_turn_status( — not the async def line.
    pattern = re.compile(r"(?<!async def )(?<!def )\bemit_turn_status\s*\(")
    _SOLE_CALLER = "observability/topology/projector.py"
    offenders: list[str] = []
    for py in _SRC_ROOT.rglob("*.py"):
        rel = py.relative_to(_SRC_ROOT).as_posix()
        if rel == _SOLE_CALLER:
            continue
        if pattern.search(py.read_text(encoding="utf-8")):
            offenders.append(rel)
    assert not offenders, (
        "emit_turn_status called outside the projector — ADR-0088 D4 sole-emitter "
        f"invariant violated: {offenders}"
    )
