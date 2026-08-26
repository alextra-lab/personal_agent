"""FRE-1299: Stance co-authorship, extending FRE-1020's Claim attribution.

``asserted_by`` (ADR-0098 D6) lives on Claim nodes, but Claims are pull-only (ADR-0126 D4)
and never reach push recall, so ``SourceRegistry.register_memory_item`` can never see one.
Stance nodes are extracted in the same pass, from the same turn text, and *do* reach push
recall (``request_gateway/context.py``'s ``_stance_context_items`` /
``_behavioural_stance_context_items``) -- so this extends the same producer-path attribution
FRE-1020 built for Claims to a Stance's ``affect`` text.

A Stance's ``affect`` is often much shorter than a Claim's self-contained sentence ("loves
it" vs a full declarative), so the reused classifier is given a stricter grounding-term floor
(``min_terms=2``) here than the Claim call site uses (``min_terms=1``, a no-op) -- a single
coincidental word match against a two-word phrase must not be enough to launder an
agent-inferred stance as user-asserted.
"""

from __future__ import annotations

from datetime import datetime, timezone

from personal_agent.second_brain.consolidator import _build_stance
from personal_agent.second_brain.entity_extraction import (
    _attribute_authorship,
    _finalize_extraction,
)

_TURN_TS = datetime(2026, 3, 1, 12, 0, tzinfo=timezone.utc)

_USER_MESSAGE = "I really love sourdough baking and my new stand mixer."
_ASSISTANT_RESPONSE = "Noted. The staging deploy needs a manual approval gate."

_USER_GROUNDED_AFFECT = "loves sourdough baking"
_AGENT_GROUNDED_AFFECT = "concerned about staging deploy"


def test_user_grounded_stance_is_attributed_to_the_user() -> None:
    assert (
        _attribute_authorship(
            _USER_GROUNDED_AFFECT,
            _USER_MESSAGE,
            _ASSISTANT_RESPONSE,
            subject_kind="stance",
            min_terms=2,
        )
        == "user"
    )


def test_assistant_grounded_stance_is_attributed_to_the_agent() -> None:
    assert (
        _attribute_authorship(
            _AGENT_GROUNDED_AFFECT,
            _USER_MESSAGE,
            _ASSISTANT_RESPONSE,
            subject_kind="stance",
            min_terms=2,
        )
        == "agent"
    )


def test_short_affect_below_min_terms_falls_back_to_agent() -> None:
    """The FRE-1299 guard: a one-term affect must not earn the user tier by coincidence.

    "loves it" reduces to the single grounding term "loves" -- present verbatim in the
    user message, which would clear both the floor and the margin under the Claim call
    site's ``min_terms=1``. The stance-specific floor blocks it.
    """
    user_message = "Yes, the owner loves it a lot."
    affect = "loves it"

    assert (
        _attribute_authorship(affect, user_message, "Noted.", subject_kind="stance", min_terms=1)
        == "user"
    ), "sanity: without the guard this single-term match would pass"
    assert (
        _attribute_authorship(affect, user_message, "Noted.", subject_kind="stance", min_terms=2)
        == "agent"
    )


def test_empty_affect_is_agent() -> None:
    assert (
        _attribute_authorship(
            "", _USER_MESSAGE, _ASSISTANT_RESPONSE, subject_kind="stance", min_terms=2
        )
        == "agent"
    )


def _finalize(stances: list[dict[str, object]]) -> list[dict[str, object]]:
    """Run the real Python-side stamping over an extractor-shaped payload."""
    result: dict[str, object] = {"entities": [], "stances": stances, "claims": []}
    _finalize_extraction(
        result,
        trace_id="trace-1299",
        session_id="session-1299",
        turn_timestamp=_TURN_TS,
        user_message=_USER_MESSAGE,
        assistant_response=_ASSISTANT_RESPONSE,
    )
    return list(result["stances"])  # type: ignore[arg-type]


def test_finalize_extraction_stamps_user_grounded_stance() -> None:
    stamped = _finalize([{"target": "sourdough baking", "affect": _USER_GROUNDED_AFFECT}])
    assert stamped[0]["asserted_by"] == "user"


def test_finalize_extraction_stamps_agent_grounded_stance() -> None:
    stamped = _finalize([{"target": "staging deploy", "affect": _AGENT_GROUNDED_AFFECT}])
    assert stamped[0]["asserted_by"] == "agent"


def test_model_supplied_stance_authorship_is_overridden() -> None:
    """The extractor cannot self-attribute a stance to the user (ADR-0098 AC-9, extended)."""
    stamped = _finalize(
        [{"target": "staging deploy", "affect": _AGENT_GROUNDED_AFFECT, "asserted_by": "user"}]
    )
    assert stamped[0]["asserted_by"] == "agent"


def test_build_stance_carries_asserted_by_through() -> None:
    stamped = _finalize([{"target": "sourdough baking", "affect": _USER_GROUNDED_AFFECT}])
    stance = _build_stance(stamped[0])
    assert stance is not None
    assert stance.asserted_by == "user"


def test_build_stance_off_vocabulary_authorship_never_reaches_the_write_path() -> None:
    """A payload bypassing _finalize_extraction cannot smuggle an unknown authority tier."""
    stance = _build_stance(
        {
            "target": "sourdough baking",
            "affect": "loves it",
            "asserted_by": "superuser",
            "provenance": {"observed_at": _TURN_TS.isoformat()},
        }
    )
    assert stance is not None
    assert stance.asserted_by == "agent"


def test_build_stance_defaults_to_agent_when_asserted_by_absent() -> None:
    stance = _build_stance(
        {
            "target": "sourdough baking",
            "affect": "loves it",
            "provenance": {"observed_at": _TURN_TS.isoformat()},
        }
    )
    assert stance is not None
    assert stance.asserted_by == "agent"
