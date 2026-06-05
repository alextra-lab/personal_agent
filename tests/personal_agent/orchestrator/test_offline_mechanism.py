"""Deterministic offline mechanism test for intra-turn digestion (ADR-0085, FRE-475).

Guards the *mechanical ceiling* claim reproducibly in CI: for the fixed tape, with no
model in the loop, digestion shrinks the tokens re-sent each round. Per the owner's
instructive note (Linear ``c8ac1fee``) the test must **not pass by construction** — so
it asserts the real per-entry gate outcomes first (``should_digest``, the D4 read pin,
``digest_saves_enough``, the fix-a expand-exemption), and the aggregate reduction only
as a consequence. The live ≥30%% acceptance gate stays a scale-rung item; this test
asserts a loose floor that only fails if digestion silently breaks.
"""

from __future__ import annotations

import pytest
from scripts.eval.fre475_compression_ab.offline_mechanism import (
    DIGESTED,
    VERBATIM_BELOW_THRESHOLD,
    VERBATIM_EXPAND_EXEMPT,
    VERBATIM_MIN_SAVINGS,
    VERBATIM_PINNED,
    breakeven_analysis,
    build_tape,
    simulate,
)


def _expected_binary(expected: str) -> str:
    """Map a tape annotation to the binary transcript outcome."""
    return "digested" if expected == DIGESTED else "verbatim"


@pytest.mark.asyncio
async def test_every_entry_matches_its_declared_gate_outcome() -> None:
    """The real defense (codex Q4): each entry's actual outcome equals its annotation.

    This proves the harness exercises every gate rather than assuming size ⇒ digestion.
    """
    tape = build_tape()
    result = await simulate(tape)

    assert len(result.on.rows) == len(tape)
    for entry, row in zip(tape, result.on.rows, strict=True):
        assert row.outcome == _expected_binary(entry.expected), (
            f"{entry.tool_call_id} ({entry.tool_name}) expected {entry.expected} "
            f"→ {_expected_binary(entry.expected)}, got {row.outcome}"
        )


@pytest.mark.asyncio
async def test_adverse_and_exempt_entries_are_oversized_yet_verbatim() -> None:
    """The cases that would falsely 'digest' under a size-only harness stay verbatim.

    - the min-savings entry clears the threshold but its digest does not save enough,
    - the expand_tool_result entry clears the threshold but is structurally exempt (fix-a).
    Both prove their gate is genuinely in the loop.
    """
    tape = build_tape()
    result = await simulate(tape)
    by_id = {e.tool_call_id: (e, r) for e, r in zip(tape, result.on.rows, strict=True)}

    for expected_kind in (VERBATIM_MIN_SAVINGS, VERBATIM_EXPAND_EXEMPT):
        entry, row = next(v for v in by_id.values() if v[0].expected == expected_kind)
        assert row.verbatim_tokens >= 1500, f"{expected_kind} fixture must clear the threshold"
        assert row.outcome == "verbatim"


@pytest.mark.asyncio
async def test_pinned_reads_and_below_threshold_stay_verbatim() -> None:
    """Reads are pinned-verbatim at birth (D4); small results never digest."""
    tape = build_tape()
    result = await simulate(tape)
    rows_by_kind: dict[str, list[str]] = {}
    for entry, row in zip(tape, result.on.rows, strict=True):
        rows_by_kind.setdefault(entry.expected, []).append(row.outcome)

    assert rows_by_kind.get(VERBATIM_PINNED), "tape must contain pinned reads"
    assert all(o == "verbatim" for o in rows_by_kind[VERBATIM_PINNED])
    assert all(o == "verbatim" for o in rows_by_kind[VERBATIM_BELOW_THRESHOLD])
    # And at least some entries genuinely digest, or the harness measures nothing.
    assert any(r.outcome == "digested" for r in result.on.rows)


@pytest.mark.asyncio
async def test_on_arm_reduces_both_curves() -> None:
    """ON shrinks both the cumulative re-bill total and the new-tail lower bound."""
    result = await simulate()
    assert result.on.cumulative_fresh_total < result.off.cumulative_fresh_total
    assert result.on.new_tail_total < result.off.new_tail_total
    # Loose floor — fails only if digestion silently stops working (not the live gate).
    assert result.on.cumulative_fresh_total <= result.off.cumulative_fresh_total * 0.9
    assert result.on.new_tail_total <= result.off.new_tail_total * 0.9


@pytest.mark.asyncio
async def test_on_arm_cumulative_tail_flattens() -> None:
    """The ON cumulative-tail slope over the last K rounds is below OFF (curve flattens)."""
    result = await simulate()
    k = 5

    def slope(rows: list) -> float:
        return (rows[-1].cumulative_fresh - rows[-1 - k].cumulative_fresh) / k

    assert slope(result.on.rows) < slope(result.off.rows)


@pytest.mark.asyncio
async def test_simulation_is_byte_stable_and_deterministic() -> None:
    """Re-running yields byte-identical per-round transcript content (D3 fixed point)."""
    first = await simulate()
    second = await simulate()
    first_contents = [r.final_content for r in first.on.rows]
    second_contents = [r.final_content for r in second.on.rows]
    assert first_contents == second_contents
    assert first.on.cumulative_fresh_total == second.on.cumulative_fresh_total


@pytest.mark.asyncio
async def test_breakeven_analysis_is_well_formed() -> None:
    """Break-even rates are sane: a positive new-tail ratio in (0,1), worst ≤ best."""
    result = await simulate()
    be = breakeven_analysis(result)
    assert be.digested_count == sum(1 for r in result.on.rows if r.outcome == "digested")
    assert 0.0 < be.new_tail_breakeven < 1.0
    assert be.cumulative_breakeven_worst <= be.cumulative_breakeven_best
    assert be.cumulative_breakeven_worst > 0.0


@pytest.mark.asyncio
async def test_simulate_does_not_leak_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    """simulate() restores settings it pins, leaving the global config untouched."""
    from personal_agent.config import settings

    sentinel = 4242
    monkeypatch.setattr(settings, "tool_result_digest_threshold_tokens", sentinel)
    await simulate()
    assert settings.tool_result_digest_threshold_tokens == sentinel
