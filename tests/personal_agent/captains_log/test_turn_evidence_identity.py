"""FRE-1150 fold-in B — the capture must make identity claims auditable.

The capture stored ``system_prompt_chars`` and nothing else about the prompt, so a
corpus-wide search for the stanza's rendered text returned zero **by construction**,
whether or not the stanza was ever there. That is how "the operator stanza has never
worked" became a reachable conclusion from a correct reading of a correct record.

These tests pin the three fields that close it: the components actually spliced, the
identity the operator component asserted, and the stanza text itself.
"""

from __future__ import annotations

import pytest

from personal_agent.captains_log.turn_evidence import (
    AssembledContextRecord,
    CandidatePopulation,
    EvidenceState,
    InlineOutcome,
    build_turn_evidence,
)

STANZA = (
    "## Operator\nYou are assisting Alex.\n"
    "This identity is established by authentication and is fixed for this conversation."
)


def _build(**overrides: object) -> object:
    kwargs: dict[str, object] = {
        "candidates": (),
        "memory_context_present": False,
        "rendered_identities": (),
        "inline_outcome": InlineOutcome.EMPTY_BLOCK,
        "session_facts_injected": False,
        "wire_messages": [{"role": "system", "content": "x"}],
        "system_prompt": "x",
        "user_message": "Good evening",
        "skill_bodies": (),
        "call_index": 0,
        "candidate_population": CandidatePopulation.POST_SELECTION,
    }
    kwargs.update(overrides)
    return build_turn_evidence(**kwargs)  # type: ignore[arg-type]


class TestAssembledContextRecordsIdentity:
    def test_records_components_identity_and_stanza(self) -> None:
        evidence = _build(
            prompt_component_ids=("tool_awareness", "operator_stanza", "memory_section"),
            operator_identity="Alex",
            operator_stanza=STANZA,
        )
        record = evidence.assembled_context  # type: ignore[attr-defined]

        assert record.prompt_component_ids == [
            "tool_awareness",
            "operator_stanza",
            "memory_section",
        ]
        assert record.operator_identity == "Alex"
        # The authority rule is readable verbatim — this is what AC-2 reads.
        assert "established by authentication" in record.operator_stanza

    def test_defaults_keep_legacy_captures_readable(self) -> None:
        """Every field defaults: a capture written before this change still validates,
        and reads as 'not recorded' rather than as 'was absent'.
        """
        record = AssembledContextRecord(
            state=EvidenceState.PRESENT,
            message_count=2,
            system_prompt_chars=100,
        )

        assert record.prompt_component_ids == []
        assert record.operator_identity is None
        assert record.operator_stanza is None

    def test_unidentified_turn_records_no_identity(self) -> None:
        evidence = _build(prompt_component_ids=("tool_awareness",))
        record = evidence.assembled_context  # type: ignore[attr-defined]

        assert record.operator_identity is None
        assert record.operator_stanza is None
        assert "operator_stanza" not in record.prompt_component_ids

    @pytest.mark.parametrize("length", [2500, 10_000])
    def test_stanza_is_bounded(self, length: int) -> None:
        """Bounded so a pathologically enriched :Person node cannot bloat the capture."""
        evidence = _build(operator_identity="Alex", operator_stanza="x" * length)
        record = evidence.assembled_context  # type: ignore[attr-defined]

        assert record.operator_stanza is not None
        assert len(record.operator_stanza) <= 2000
