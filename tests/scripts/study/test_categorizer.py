"""Tests for the ADR-0114 D3 ingest categorizer (FRE-839).

Unit-level: mocked LLM client — never a real API call. Covers prompt
construction, response parsing (including fence-stripping and fail-open on
malformed JSON), and that provenance (model/prompt_version/seed) is always
Python-stamped, never trusted from the model's own output.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from scripts.study.categorizer import (
    CATEGORIZER_PROMPT_VERSION,
    _build_categorizer_prompt,
    _strip_json_fences,
    categorize_conversation,
)


def test_build_categorizer_prompt_includes_every_concept_and_the_conversation() -> None:
    prompt = _build_categorizer_prompt(
        conversation_text="We discussed liver dysfunction as a medication side effect.",
        concepts=[("Liver dysfunction", "Phenomenon"), ("Medication", "TechnicalArtifact")],
    )

    assert "Liver dysfunction" in prompt
    assert "Medication" in prompt
    assert "We discussed liver dysfunction as a medication side effect." in prompt
    assert "Phenomenon" in prompt
    assert "TechnicalArtifact" in prompt


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ('```json\n{"a": 1}\n```', '{"a": 1}'),
        ('```\n{"a": 1}\n```', '{"a": 1}'),
        ('{"a": 1}', '{"a": 1}'),
        ('noise before {"a": 1}', '{"a": 1}'),
    ],
)
def test_strip_json_fences(raw: str, expected: str) -> None:
    """Mirrors `entity_extraction.py`'s exact fence-stripping shape — the
    last-resort brace-finder only trims LEADING noise, not trailing (trailing
    garbage after valid JSON fails `json.loads` regardless, so
    `categorize_conversation` catches that via its fail-open
    `JSONDecodeError` handling rather than this helper trying to fix it up).
    """
    assert _strip_json_fences(raw) == expected


@pytest.mark.asyncio
async def test_categorize_conversation_parses_wellformed_response() -> None:
    fake_response = {
        "content": (
            "```json\n"
            '{"memberships": ['
            '{"concept": "Liver dysfunction", '
            '"categories": [{"name": "adverse effect", "confidence": 0.81}, '
            '{"name": "liver health", "confidence": 0.6}]}'
            "]}"
            "\n```"
        ),
        "usage": {},
    }
    with patch("scripts.study.categorizer._call_llm", new=AsyncMock(return_value=fake_response)):
        memberships = await categorize_conversation(
            conversation_text="...",
            concepts=[("Liver dysfunction", "Phenomenon")],
            seed=7,
        )

    assert len(memberships) == 2
    names = {m.category_name for m in memberships}
    assert names == {"adverse effect", "liver health"}
    for m in memberships:
        assert m.concept_name == "Liver dysfunction"
        assert m.kind == "Phenomenon"
    confidences = {m.category_name: m.proposed_confidence for m in memberships}
    assert confidences["adverse effect"] == 0.81
    assert confidences["liver health"] == 0.6


@pytest.mark.asyncio
async def test_categorize_conversation_fails_open_on_malformed_json() -> None:
    fake_response = {"content": "not json at all, sorry", "usage": {}}
    with patch("scripts.study.categorizer._call_llm", new=AsyncMock(return_value=fake_response)):
        memberships = await categorize_conversation(
            conversation_text="...", concepts=[("X", "Phenomenon")], seed=1
        )

    assert memberships == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "content",
    [
        # Code-review finding (FRE-839): each of these is syntactically VALID
        # JSON with an unexpected shape — the old code only guarded against
        # JSONDecodeError, so these raised TypeError/ValueError uncaught,
        # crashing the whole corpus run instead of dropping this one episode.
        '{"memberships": [{"concept": "X", "categories": null}]}',
        '{"memberships": [{"concept": "X", "categories": [{"name": "y", "confidence": "high"}]}]}',
        '{"memberships": "not a list"}',
        '{"memberships": [{"concept": "X", "categories": [123]}]}',
        '["not", "a", "dict", "at", "all"]',
    ],
)
async def test_categorize_conversation_fails_open_on_valid_json_wrong_shape(content: str) -> None:
    fake_response = {"content": content, "usage": {}}
    with patch("scripts.study.categorizer._call_llm", new=AsyncMock(return_value=fake_response)):
        memberships = await categorize_conversation(
            conversation_text="...", concepts=[("X", "Phenomenon")], seed=1
        )

    assert memberships == []


@pytest.mark.asyncio
async def test_categorize_conversation_stamps_provenance_in_python_not_the_model() -> None:
    """Even if the model tried to emit model/prompt_version/seed itself, the
    wrapper's own values must win — provenance is never trusted from the LLM.
    """
    fake_response = {
        "content": (
            '{"memberships": [{"concept": "X", "categories": '
            '[{"name": "y", "confidence": 0.5, '
            '"model": "a-lie", "seed": 999, "prompt_version": "a-lie"}]}]}'
        ),
        "usage": {},
    }
    with patch("scripts.study.categorizer._call_llm", new=AsyncMock(return_value=fake_response)):
        memberships = await categorize_conversation(
            conversation_text="...", concepts=[("X", "Phenomenon")], seed=42
        )

    assert len(memberships) == 1
    # ProposedMembership itself carries no provenance fields (that's a
    # separate AssertionProvenance the caller/writer stamps) — this proves
    # the categorizer doesn't smuggle model-supplied provenance through.
    assert not hasattr(memberships[0], "model")
    assert not hasattr(memberships[0], "seed")


def test_categorizer_prompt_version_is_a_module_constant() -> None:
    assert CATEGORIZER_PROMPT_VERSION == "fre839-categorizer-v1"
