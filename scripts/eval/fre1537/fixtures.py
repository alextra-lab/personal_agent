"""FRE-1511 / ADR-0154 D7: the probe's fixtures and their expected decisions.

The 19 single-turn fixtures are those of ``scripts/eval/fre1498/fixtures.yaml``. The 8 two-turn
follow-ups are in ``followups.yaml`` beside this file. Expected decisions follow ADR-0154
Appendix A1: decline for the four Susan requests, ``greeting`` and ``tool_logs``; expand for the
other twelve single-turn fixtures; ``c5_noverb`` is excluded from scoring.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import yaml

Expected = Literal["decline", "expand", "excluded"]

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[2]
SINGLE_TURN_FIXTURES = REPO_ROOT / "scripts" / "eval" / "fre1498" / "fixtures.yaml"
FOLLOWUPS = HERE / "followups.yaml"

_SINGLE_TURN_DECLINE = frozenset(
    {
        "susan_1_run_tool",
        "susan_2_able_to_run",
        "susan_3_thought_i_asked",
        "susan_4_build_and_run",
        "greeting",
        "tool_logs",
    }
)
_SINGLE_TURN_EXCLUDED = frozenset({"c5_noverb"})


@dataclass(frozen=True)
class Fixture:
    """One probe fixture.

    Attributes:
        label: Unique fixture label.
        message: The user message the planner decides on.
        expected: ``decline``, ``expand`` or ``excluded`` (never scored).
        history: Name of the scripted history for a two-turn fixture, else ``None``.
        history_user: Turn-1 user text of the scripted history, else ``None``.
        history_assistant: Turn-1 scripted assistant reply, else ``None``.
    """

    label: str
    message: str
    expected: Expected
    history: str | None = None
    history_user: str | None = None
    history_assistant: str | None = None

    @property
    def kind(self) -> Literal["single", "followup"]:
        """Return ``followup`` for a two-turn fixture and ``single`` otherwise."""
        return "single" if self.history is None else "followup"

    @property
    def history_messages(self) -> list[dict[str, str]]:
        """Return the scripted turn-1 messages, oldest first (empty for a single-turn fixture)."""
        if self.history_user is None or self.history_assistant is None:
            return []
        return [
            {"role": "user", "content": self.history_user},
            {"role": "assistant", "content": self.history_assistant},
        ]


def _single_turn_expected(label: str) -> Expected:
    if label in _SINGLE_TURN_EXCLUDED:
        return "excluded"
    return "decline" if label in _SINGLE_TURN_DECLINE else "expand"


def load_histories() -> dict[str, tuple[str, str]]:
    """Load the four scripted histories.

    Returns:
        Mapping of history name to ``(user text, assistant text)`` of turn 1.
    """
    data = yaml.safe_load(FOLLOWUPS.read_text())
    return {name: (h["user"], h["assistant"]) for name, h in data["histories"].items()}


def load_fixtures() -> list[Fixture]:
    """Load the 19 single-turn fixtures, then the 8 two-turn follow-ups.

    Returns:
        All 27 fixtures with their expected decisions.
    """
    single = yaml.safe_load(SINGLE_TURN_FIXTURES.read_text())["fixtures"]
    follow = yaml.safe_load(FOLLOWUPS.read_text())
    out = [
        Fixture(
            label=fx["label"], message=fx["message"], expected=_single_turn_expected(fx["label"])
        )
        for fx in single
    ]
    histories = load_histories()
    for fx in follow["fixtures"]:
        user, assistant = histories[fx["history"]]
        out.append(
            Fixture(
                label=fx["label"],
                message=fx["message"],
                expected=fx["expected"],
                history=fx["history"],
                history_user=user,
                history_assistant=assistant,
            )
        )
    return out
