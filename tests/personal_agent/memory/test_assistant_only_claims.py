"""FRE-1739: a claim that only the assistant stated is not stored as a fact about the user.

Defect A of the FRE-1728 pilot. The assistant wrote "since you're on Europe/Paris I'll assume
a French agence under IDCC 2120 — say so if it's another country". The extractor turned that
text into the owner claim "The user wants the analysis tailored to a French branch ...".
The user's later correction ("I'm in the United States") was stored under another facet, so
it never matched the old claim, and both stayed current.

The fix: the extractor labels each claim with the speaker that stated it (``stated_by``).
Python drops a claim that only the assistant stated. The label can only remove a claim. It
never raises trust, so ADR-0098 AC-9 holds. A missing or unknown label keeps the claim.

A word-overlap rule was tried and rejected in plan review. It dropped a user paraphrase
("meetings" in the user text, "meeting" in the claim). See the paraphrase test below.

The strings are the real pilot strings (simulated user data).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from personal_agent.memory.supersession import (
    ClaimRecord,
    SupersessionAction,
    adjudicate,
    matching_candidates,
    strongest_blocker,
)
from personal_agent.second_brain.consolidator import _build_claim
from personal_agent.second_brain.entity_extraction import (
    _build_extraction_prompt,
    _finalize_extraction,
)

_T0 = datetime(2026, 11, 5, 9, 0, tzinfo=timezone.utc)

# Episode 2, turn 2 of the pilot: the user said "banking" and "retail branch" and never a country.
_EP2_USER = (
    "I'm in banking, so I need this framed around a branch, not a generic business schedule. "
    "I want the same kind of pressure test, but specifically: what has to be covered every day "
    "in a retail bank branch, what can slide a week, and what usually blows up if I get it wrong?"
)
_EP2_ASSISTANT = (
    "Banking framing, and since you're on Europe/Paris I'll assume a French agence under "
    "IDCC 2120 — say so if it's another country and I'll re-frame."
)
_ASSUMPTION = (
    "The user wants the analysis tailored to a French branch under IDCC 2120 "
    "unless they specify another country."
)

# Episode 3: the user corrects the assistant.
_EP3_USER = (
    "That answer misses my setup. I'm in the United States, not a French branch, so I need the "
    "checklist framed for my branch records and not tied to French-specific rules."
)
_EP3_ASSISTANT = (
    "You're right — the last one was built on the France-framed context I had on file. "
    "Here's the same idea rebuilt for a US branch packet."
)
_US_CLAIM = "The user is in the United States."


def _finalize(
    claims: list[dict[str, object]], user_message: str, assistant_response: str
) -> list[dict[str, object]]:
    """Run the real Python-side stamping over an extractor-shaped payload."""
    result: dict[str, object] = {"entities": [], "stances": [], "claims": claims}
    _finalize_extraction(
        result,
        trace_id="trace-1739",
        session_id="session-1739",
        turn_timestamp=_T0,
        user_message=user_message,
        assistant_response=assistant_response,
    )
    return list(result["claims"])  # type: ignore[arg-type]


# ------------------------------------------------------------------------------- AC-2


def test_assistant_only_assumption_is_not_stored_as_a_user_claim() -> None:
    """AC-2: the pilot's ``jurisdiction_assumption`` does not survive extraction."""
    stamped = _finalize(
        [
            {
                "content": _ASSUMPTION,
                "facet": "jurisdiction_assumption",
                "update_kind": "new",
                "stated_by": "assistant",
            }
        ],
        _EP2_USER,
        _EP2_ASSISTANT,
    )
    assert stamped == []


def test_assistant_only_status_claim_is_dropped() -> None:
    """The later pilot claims came from assistant text too (episode 8)."""
    stamped = _finalize(
        [
            {
                "content": "The user has not yet said whether the branch context is US or France.",
                "facet": "jurisdiction_status",
                "stated_by": "assistant",
            }
        ],
        "I'm not asking for another template. I need the concrete priority call.",
        "One word, US or France, and the roster output becomes trustworthy.",
    )
    assert stamped == []


def test_label_is_read_case_and_space_insensitively() -> None:
    """A label such as " Assistant " still drops the claim."""
    stamped = _finalize(
        [{"content": _ASSUMPTION, "facet": "jurisdiction_assumption", "stated_by": " Assistant "}],
        _EP2_USER,
        _EP2_ASSISTANT,
    )
    assert stamped == []


def test_extraction_prompt_asks_for_the_speaker_label() -> None:
    """The label is only useful if the extractor is asked for it, in the schema and the rules."""
    prompt = _build_extraction_prompt("hello", "hi")
    assert prompt.count("stated_by") >= 3
    assert "I'll assume" in prompt  # the BAD example of the pilot defect
    assert "accepts or confirms" in prompt  # a user confirmation of a proposal is "user"


# ------------------------------------------------------------------------------- AC-4


def test_user_stated_fact_is_still_stored() -> None:
    """AC-4: the guard does not silence extraction of what the user says."""
    stamped = _finalize(
        [
            {
                "content": _US_CLAIM,
                "facet": "current_country",
                "update_kind": "correction",
                "stated_by": "user",
            }
        ],
        _EP3_USER,
        _EP3_ASSISTANT,
    )
    assert len(stamped) == 1
    assert stamped[0]["asserted_by"] == "user"


def test_mixed_payload_keeps_only_the_claim_the_user_stated() -> None:
    stamped = _finalize(
        [
            {"content": _US_CLAIM, "facet": "current_country", "stated_by": "user"},
            {"content": _ASSUMPTION, "facet": "jurisdiction_assumption", "stated_by": "assistant"},
        ],
        _EP3_USER,
        _EP3_ASSISTANT,
    )
    assert [c["facet"] for c in stamped] == ["current_country"]


def test_user_paraphrase_with_little_word_overlap_is_kept() -> None:
    """A word-overlap rule dropped this claim ("meetings" vs "meeting"). The label keeps it."""
    stamped = _finalize(
        [
            {
                "content": "The user has a heavy meeting load this week.",
                "facet": "workload",
                "stated_by": "user",
            }
        ],
        "I'm drowning in meetings and back to back calls this week.",
        "That sounds like a heavy week. Block time between meetings and protect a load-free hour.",
    )
    assert len(stamped) == 1


def test_missing_or_unknown_label_keeps_the_claim() -> None:
    """Fail open: a model that omits the label, or writes another value, loses no claim."""
    stamped = _finalize(
        [
            {"content": "The user lives in Lyon."},
            {"content": "The user works nights.", "stated_by": "both"},
            {"content": "The user rides a bike.", "stated_by": None},
        ],
        "I live in Lyon, work nights and ride a bike.",
        "Noted.",
    )
    assert len(stamped) == 3


# ------------------------------------------------------------------------------- AC-3


def _record(
    claim_id: str, content: str, facet: str, confidence: float, embedding: list[float]
) -> ClaimRecord:
    return ClaimRecord(
        claim_id=claim_id,
        content=content,
        confidence=confidence,
        observed_at=_T0,
        embedding=embedding,
        facet=facet,
    )


def test_user_correction_leaves_no_active_claim_with_the_old_value() -> None:
    """AC-3: a user claim "France", then the user's correction "United States".

    Runs the producer path (``_finalize_extraction`` then ``_build_claim``) and the writer's
    selection (``matching_candidates``, ``strongest_blocker``, ``adjudicate``). The active set
    after the write has no claim with the old value. The real writer is covered by the
    integration test in ``test_claims_stance_storage.py``.
    """
    stored_user = _finalize(
        [{"content": "The user is in France.", "facet": "current_country", "stated_by": "user"}],
        "I'm in France and I manage a branch.",
        "Noted.",
    )
    old = _build_claim(stored_user[0])
    assert old is not None and old.asserted_by == "user"
    active = [_record("old", old.content, old.facet, old.confidence, [1.0, 0.2])]

    corrected = _finalize(
        [
            {
                "content": _US_CLAIM,
                "facet": "current_country",
                "update_kind": "correction",
                "stated_by": "user",
            }
        ],
        _EP3_USER,
        _EP3_ASSISTANT,
    )
    payload = dict(corrected[0])
    payload["provenance"] = {
        **payload["provenance"],  # type: ignore[dict-item]
        "observed_at": (_T0 + timedelta(days=1)).isoformat(),
    }
    new = _build_claim(payload)
    assert new is not None

    matches = matching_candidates(new.facet, [0.9, 0.4], active)
    decision = adjudicate(
        new_confidence=new.confidence,
        new_observed_at=new.observed_at,
        candidate=strongest_blocker(matches),
        new_update_kind=new.update_kind,
    )
    assert decision.action is SupersessionAction.SUPERSEDE
    superseded = {m.claim_id for m in matches}
    still_active = [r.content for r in active if r.claim_id not in superseded] + [new.content]
    assert still_active == [_US_CLAIM]
