"""FRE-771 — the live extraction prompt speaks the ADR-0109 10-type V2 taxonomy.

Confirms the entity-type prompt swap (ADR-0109 Implementation Notes step 3): the
default-rendered extraction prompt carries all 10 V2 GoLLIE definitions and none of the
retired V1-only type keys, and the flag-gated FRE-759 few-shot exemplar block (which
names entity types in its disambiguation examples) never regresses to V1 vocabulary.
"""

from __future__ import annotations

import re

from personal_agent.second_brain.entity_extraction import (
    _EXTRACTION_FEWSHOT_EXEMPLARS,
    _build_extraction_prompt,
)


def _contains_word(text: str, word: str) -> bool:
    """Whole-word substring check (avoids e.g. "Concept" matching inside "MethodOrConcept")."""
    return re.search(rf"\b{re.escape(word)}\b", text) is not None


#: The ADR-0109 10-type V2 entity vocabulary (mirrors gold.py's ALLOWED_ENTITY_TYPES_V2).
V2_TYPES = (
    "Person",
    "Organization",
    "Location",
    "TechnicalArtifact",
    "KnowledgeArtifact",
    "MethodOrConcept",
    "DomainOrTopic",
    "Phenomenon",
    "QuantityMeasure",
    "Event",
)

#: The retired V1-only keys (7-type vocabulary minus what V2 kept as-is).
V1_ONLY_TYPES = ("Technology", "Concept", "Topic")

_JSON_TYPE_ENUM_LINE = '"type": "{}"'.format("|".join(V2_TYPES))


def test_default_prompt_carries_all_ten_v2_types() -> None:
    """The rendered prompt's JSON-footer type enum lists exactly the 10 V2 keys."""
    prompt = _build_extraction_prompt("hello", "hi there")
    assert _JSON_TYPE_ENUM_LINE in prompt


def test_default_prompt_has_no_v1_only_type_keys() -> None:
    """None of the retired V1-only type keys appear as a controlled-vocabulary token."""
    prompt = _build_extraction_prompt("hello", "hi there")
    for v1_type in V1_ONLY_TYPES:
        assert f'"{v1_type}"' not in prompt, f"stale V1-only type {v1_type!r} still in prompt"
        assert f"|{v1_type}|" not in prompt
        assert not prompt.endswith(f"|{v1_type}")


def test_default_prompt_states_all_ten_gollie_definitions() -> None:
    """Every V2 type's distinguishing GoLLIE clause renders, not just its bare key."""
    prompt = _build_extraction_prompt("hello", "hi there")
    # A representative inclusion/exclusion phrase per type (ADR-0109 § Decision table) —
    # proves the full definitions render, not just the enum line (codex test-gap finding).
    distinguishing_phrases = (
        "real, named individual human",  # Person
        "named company, institution, agency",  # Organization
        "named geographic or physical place",  # Location
        "engineered/built thing",  # TechnicalArtifact
        "human-authored work",  # KnowledgeArtifact
        "human-invented abstract idea",  # MethodOrConcept
        "broad field, domain, discipline",  # DomainOrTopic
        "naturally-occurring physical/natural phenomenon",  # Phenomenon
        "physical quantity, property, dimension",  # QuantityMeasure
        "specific named occurrence, milestone",  # Event
    )
    for phrase in distinguishing_phrases:
        assert phrase in prompt, f"missing GoLLIE definition text: {phrase!r}"


def test_fewshot_block_has_no_v1_only_types() -> None:
    """The flag-gated few-shot exemplar block never names a retired V1-only type (D2-b).

    The block is off by default, but if ever re-enabled it must not contradict the V2
    header with stale Concept/Topic/Technology disambiguation guidance.
    """
    for v1_type in V1_ONLY_TYPES:
        assert not _contains_word(_EXTRACTION_FEWSHOT_EXEMPLARS, v1_type), (
            f"fewshot block still names retired V1-only type {v1_type!r}"
        )
