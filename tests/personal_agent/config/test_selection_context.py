"""Per-turn selection context isolation (ADR-0121 §4 invariant 7 / FRE-917, AC-7b).

The selection is set once per background task; because each ``asyncio.Task`` gets
its own copy of the context, a selection set (or changed) in one task never leaks
into another already running — the mechanism that makes an in-flight turn keep
the selection it launched with even as another turn changes the store.
"""

from __future__ import annotations

import asyncio

import pytest

from personal_agent.config.selection import (
    get_current_selection,
    set_current_selection,
)


@pytest.mark.asyncio
async def test_selection_is_isolated_per_task() -> None:
    """Two concurrent tasks each see only their own launch selection."""
    started = asyncio.Event()

    async def _turn(model: str, hold: bool) -> str | None:
        set_current_selection({"primary": model})
        if hold:
            started.set()
            # Yield so the sibling task runs (and sets its own selection) mid-flight.
            await asyncio.sleep(0.02)
        else:
            await started.wait()
            # Change our own selection while the sibling is holding — must not leak.
            set_current_selection({"primary": "claude_haiku"})
        return get_current_selection("primary")

    held, other = await asyncio.gather(
        _turn("claude_sonnet", hold=True),
        _turn("qwen3.6-35b-thinking", hold=False),
    )

    # The holding turn keeps the selection it launched with, unaffected by the
    # sibling reassigning its own context.
    assert held == "claude_sonnet"
    assert other == "claude_haiku"


@pytest.mark.asyncio
async def test_default_selection_is_empty() -> None:
    """With nothing set, a role resolves to None (the factory then uses its default)."""
    assert get_current_selection("primary") is None
