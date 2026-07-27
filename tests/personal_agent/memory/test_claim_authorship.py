"""FRE-1020: co-authorship makes the ADR-0098 D2 supersession guard reachable.

Before this ticket every Claim carried ``confidence=0.8`` (``source_type`` was the
hard-coded literal ``"conversation"``, and confidence derived solely from it), so in
:func:`~personal_agent.memory.supersession.adjudicate` the *confidence* comparison could
never be unequal: the weaker-claim REJECT and the confidence-heuristic ``correction`` label
were both **unreachable**, leaving only the ``observed_at`` staleness check. Supersession
degenerated to newer-wins — the naive last-write-wins model ADR-0098 D2 names and rejects.
(The staleness REJECT and the extractor's explicit ``update_kind`` label were reachable in
principle throughout; live, neither had ever produced a rejection — 94 claims, zero.)

These tests pin the producer path end to end: authorship is derived **in Python** from
the role-partitioned captured text (never self-reported by the model — ADR-0098 AC-9),
uplifts a user-asserted Claim above the agent tier, and the agent tier stays at today's
0.8 so no existing supersession path regresses.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from personal_agent.memory.supersession import (
    ClaimRecord,
    SupersessionAction,
    adjudicate,
)
from personal_agent.memory.weight import KnowledgeWeight
from personal_agent.second_brain.consolidator import _build_claim
from personal_agent.second_brain.entity_extraction import (
    _attribute_claim_authorship,
    _finalize_extraction,
)

_TURN_TS = datetime(2026, 3, 1, 12, 0, tzinfo=timezone.utc)

# Two claims from one real turn: the first is grounded in the owner's own words, the
# second only in the assistant's — the 43 %-of-corpus population FRE-1020 measured.
_USER_MESSAGE = "My lease ends in March and I have an HKoenig glacier ice cream maker."
_ASSISTANT_RESPONSE = (
    "Noted. The sandbox network bridge cloud-sim is not attached in this environment."
)
_USER_CLAIM = "The user has an HKoenig glacier ice cream maker."
_AGENT_CLAIM = "The sandbox network bridge cloud-sim is not attached in this environment."


# --------------------------------------------------------------------------- weight


def test_user_asserted_uplifts_above_the_agent_tier() -> None:
    """A user-asserted conversation claim outranks an agent-derived one."""
    user = KnowledgeWeight.from_claim_provenance("conversation", "user")
    agent = KnowledgeWeight.from_claim_provenance("conversation", "agent")
    assert user.confidence > agent.confidence


def test_agent_tier_equals_todays_constant() -> None:
    """AC-D: the agent tier is exactly today's 0.8 — no existing path regresses."""
    agent = KnowledgeWeight.from_claim_provenance("conversation", "agent")
    assert agent.confidence == KnowledgeWeight.from_source("conversation").confidence
    assert agent.confidence == 0.8


def test_unknown_authorship_defaults_to_the_agent_tier() -> None:
    """Off-vocabulary authorship never earns the uplift."""
    assert KnowledgeWeight.from_claim_provenance("conversation", "").confidence == 0.8
    assert KnowledgeWeight.from_claim_provenance("conversation", "sneaky").confidence == 0.8


def test_uplift_is_clamped_to_one() -> None:
    """A user-asserted manual claim cannot exceed the confidence bound."""
    assert KnowledgeWeight.from_claim_provenance("manual", "user").confidence == 1.0


# ---------------------------------------------------------------------- attribution


def test_user_grounded_claim_is_attributed_to_the_user() -> None:
    assert _attribute_claim_authorship(_USER_CLAIM, _USER_MESSAGE, _ASSISTANT_RESPONSE) == "user"


def test_assistant_grounded_claim_is_attributed_to_the_agent() -> None:
    assert _attribute_claim_authorship(_AGENT_CLAIM, _USER_MESSAGE, _ASSISTANT_RESPONSE) == "agent"


def test_ungrounded_claim_falls_back_to_the_agent_tier() -> None:
    """Neither speaker's words support it — never award elevated authority."""
    assert _attribute_claim_authorship("Something nobody said.", _USER_MESSAGE, "") == "agent"


def test_empty_content_is_agent() -> None:
    assert _attribute_claim_authorship("", _USER_MESSAGE, _ASSISTANT_RESPONSE) == "agent"


def test_accented_words_are_not_shredded_into_fragments() -> None:
    """Attribution must survive a French corpus — ``[a-z0-9]+`` split "café" into "caf".

    The owner's captures routinely contain accented French (place names, cooking terms),
    so an ASCII-only word class would score grounding on truncated fragments and flip
    attributions in both directions.
    """
    user_message = "I bought a crème brûlée torch in Forcalquier for my café setup."
    claim = "The user bought a crème brûlée torch in Forcalquier."
    assert _attribute_claim_authorship(claim, user_message, "Noted.") == "user"


def test_accented_claim_grounded_in_assistant_is_not_awarded_to_user() -> None:
    user_message = "What is the weather?"
    assistant = "Météo France reports the user's Forcalquier crème brûlée festival is Saturday."
    claim = "The Forcalquier crème brûlée festival is on Saturday per Météo France."
    assert _attribute_claim_authorship(claim, user_message, assistant) == "agent"


# ------------------------------------------------------------------- producer path


def _finalize(claims: list[dict[str, object]]) -> list[dict[str, object]]:
    """Run the real Python-side stamping over an extractor-shaped payload."""
    result: dict[str, object] = {"entities": [], "stances": [], "claims": claims}
    _finalize_extraction(
        result,
        trace_id="trace-1020",
        session_id="session-1020",
        turn_timestamp=_TURN_TS,
        user_message=_USER_MESSAGE,
        assistant_response=_ASSISTANT_RESPONSE,
    )
    return list(result["claims"])  # type: ignore[arg-type]


def test_producer_path_yields_two_distinct_confidences() -> None:
    """AC-A: confidence is no longer constant on the production producer path."""
    stamped = _finalize(
        [{"content": _USER_CLAIM, "facet": "kitchen_equipment"}, {"content": _AGENT_CLAIM}]
    )
    built = [_build_claim(c) for c in stamped]
    assert all(c is not None for c in built)
    confidences = {c.confidence for c in built if c is not None}
    assert len(confidences) == 2


def test_model_supplied_authorship_is_overridden(caplog: object) -> None:
    """AC-F: the model cannot mint its own trust credential (ADR-0098 AC-9).

    An assistant-grounded claim that self-attributes ``asserted_by: "user"`` is
    overwritten by the Python-derived value, so the extraction model can never
    manufacture the uplift that would make its own output authoritative.
    """
    stamped = _finalize([{"content": _AGENT_CLAIM, "asserted_by": "user"}])
    assert stamped[0]["asserted_by"] == "agent"
    claim = _build_claim(stamped[0])
    assert claim is not None
    assert claim.confidence == 0.8


# -------------------------------------------------------------------- adjudication


def _record(confidence: float, *, observed_at: datetime = _TURN_TS) -> ClaimRecord:
    return ClaimRecord(
        claim_id="current",
        content="The user has a Cuisinart ice cream maker.",
        confidence=confidence,
        observed_at=observed_at,
        embedding=[1.0, 0.0],
        facet="kitchen_equipment",
    )


def _claim_for(content: str, extractor_payload: dict[str, object] | None = None):
    payload: dict[str, object] = {"content": content, "facet": "kitchen_equipment"}
    payload.update(extractor_payload or {})
    return _build_claim(_finalize([payload])[0])


def test_agent_derived_claim_cannot_clobber_a_user_asserted_one() -> None:
    """AC-B: the weaker-claim guard is live — unreachable on constant confidence."""
    user_current = _record(KnowledgeWeight.from_claim_provenance("conversation", "user").confidence)
    incoming = _claim_for(_AGENT_CLAIM)
    assert incoming is not None
    decision = adjudicate(
        new_confidence=incoming.confidence,
        new_observed_at=_TURN_TS + timedelta(hours=1),
        candidate=user_current,
        new_update_kind=incoming.update_kind,
    )
    assert decision.action is SupersessionAction.REJECT


def test_user_asserted_claim_corrects_an_agent_derived_one() -> None:
    """AC-C: the confidence-heuristic ``correction`` arm is reachable — never fired live."""
    agent_current = _record(0.8)
    incoming = _claim_for(_USER_CLAIM)
    assert incoming is not None
    decision = adjudicate(
        new_confidence=incoming.confidence,
        new_observed_at=_TURN_TS + timedelta(hours=1),
        candidate=agent_current,
        new_update_kind=incoming.update_kind,
    )
    assert decision.action is SupersessionAction.SUPERSEDE
    assert decision.reason == "correction"


def test_off_vocabulary_authorship_never_reaches_the_adjudicator() -> None:
    """A payload bypassing _finalize_extraction cannot smuggle an unknown authority tier."""
    claim = _build_claim(
        {
            "content": _USER_CLAIM,
            "asserted_by": "superuser",
            "provenance": {"source_type": "conversation", "observed_at": _TURN_TS.isoformat()},
        }
    )
    assert claim is not None
    assert claim.asserted_by == "agent"
    assert claim.confidence == 0.8


def test_agent_derived_claim_still_supersedes_a_legacy_row() -> None:
    """AC-D: legacy 0.8 rows stay supersedable — the substrate does not freeze."""
    legacy = _record(0.8)
    incoming = _claim_for(_AGENT_CLAIM)
    assert incoming is not None
    decision = adjudicate(
        new_confidence=incoming.confidence,
        new_observed_at=_TURN_TS + timedelta(hours=1),
        candidate=legacy,
        new_update_kind=incoming.update_kind,
    )
    assert decision.action is SupersessionAction.SUPERSEDE
