"""Executor wiring of the cache-aware compaction scheduler (ADR-0081 §D3, FRE-434).

Covers backend detection, scheduler-input derivation, and the strictly
flag-gated reset bridge (`_maybe_frozen_reset`).
"""

from __future__ import annotations

import pytest

from personal_agent.config import settings
from personal_agent.governance.models import Mode
from personal_agent.orchestrator import Channel
from personal_agent.orchestrator import executor as ex
from personal_agent.orchestrator.types import ExecutionContext
from personal_agent.orchestrator.within_session_compression import FrozenResetResult


def _ctx(messages: list[dict[str, str]]) -> ExecutionContext:
    ctx = ExecutionContext(
        session_id="s1",
        trace_id="t1",
        user_message="hi",
        mode=Mode.NORMAL,
        channel=Channel.CHAT,
    )
    ctx.messages = messages
    return ctx


def test_derive_reset_inputs_counts_user_turns() -> None:
    msgs = [
        {"role": "user", "content": "a"},
        {"role": "assistant", "content": "b"},
        {"role": "user", "content": "c"},
    ]
    out = ex._derive_reset_inputs(msgs, "local")
    assert out["turns_since_reset"] == 2
    assert out["min_run_turns"] == settings.cache_reset_min_run_turns_local
    assert out["accum_max_tokens"] == int(
        settings.cache_frozen_accum_max_ratio * settings.context_window_max_tokens
    )


def test_derive_reset_inputs_backend_asymmetry() -> None:
    msgs = [{"role": "user", "content": "a"}, {"role": "assistant", "content": "b"}]
    local = ex._derive_reset_inputs(msgs, "local")
    cloud = ex._derive_reset_inputs(msgs, "cloud")
    assert local["min_run_turns"] == settings.cache_reset_min_run_turns_local
    assert cloud["min_run_turns"] == settings.cache_reset_min_run_turns_cloud
    # Local pays a full re-prefill; cloud only the rewritten span → cheaper reset.
    assert local["reset_cost_tokens"] > cloud["reset_cost_tokens"]


def test_frozen_backend_defaults_local_without_profile() -> None:
    # No profile set in this context → conservative default.
    assert ex._frozen_backend() in {"local", "cloud"}


# The flag-off no-op test was removed with cache_frozen_layout_enabled (FRE-941):
# the frozen reset is now unconditional apart from the ctx.session_id guard.


@pytest.mark.asyncio
async def test_maybe_frozen_reset_holds_below_threshold(monkeypatch: pytest.MonkeyPatch) -> None:
    # Short history → below the min-run floor → hold.
    msgs = [{"role": "user", "content": "a"}, {"role": "assistant", "content": "b"}]
    ctx = _ctx(list(msgs))
    await ex._maybe_frozen_reset(ctx)
    assert ctx.messages == msgs


@pytest.mark.asyncio
async def test_maybe_frozen_reset_fires_and_stashes_highlights(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _fake_build_frozen_reset(messages, *, trace_id, session_id, **kw):  # type: ignore[no-untyped-def]
        return FrozenResetResult(
            messages=[{"role": "user", "content": "compacted"}],
            salient_highlights="HL",
            narrative="N",
        )

    # Force the scheduler to fire regardless of derived inputs.
    monkeypatch.setattr(
        ex,
        "_derive_reset_inputs",
        lambda messages, backend: {
            "turns_since_reset": 99,
            "accumulated_tokens": 1,
            "accum_max_tokens": 0,
            "min_run_turns": 1,
            "reset_cost_tokens": 1.0,
            "delta_turn_tokens": 1.0,
            "quality_token_weight": 4000.0,
            "quality_slope": 0.0,
        },
    )
    monkeypatch.setattr(
        "personal_agent.orchestrator.within_session_compression.build_frozen_reset",
        _fake_build_frozen_reset,
    )

    ctx = _ctx([{"role": "user", "content": f"q{i}"} for i in range(20)])
    await ex._maybe_frozen_reset(ctx)
    assert ctx.messages == [{"role": "user", "content": "compacted"}]
    assert ctx.salient_highlights == "HL"
