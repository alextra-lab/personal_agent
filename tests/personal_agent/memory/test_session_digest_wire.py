"""Wire model and tool contract for the session digest (FRE-996, ADR-0124 D3).

The load-bearing tests here are the two that pin properties the pilot's *measurement*
depends on, not merely its behaviour:

* ``test_schema_never_asks_for_as_of`` — the reason a wire model exists at all. Handing
  the storage model's schema to a provider would ask the model to author a
  producer-stamped field, violating ADR-0124 D3.
* ``test_litellm_maps_tool_without_json_mode`` — the mechanism pin. If a litellm upgrade
  starts setting ``json_mode`` on this path, it silently overwrites the provider's
  ``stop_reason`` and truncation becomes indistinguishable from success. That must fail
  loudly here rather than quietly corrupt a future measurement.
"""

# ruff: noqa: D103

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import orjson
import pytest

from personal_agent.memory.session_digest import MAX_LABEL_CHARS
from personal_agent.memory.session_digest_wire import (
    DIGEST_TOOL_NAME,
    DigestEnvelope,
    digest_tool,
    digest_tool_choice,
    to_storage,
)

ENDED_AT = datetime(2026, 7, 20, 12, 0, tzinfo=UTC)


def _walk(node: object) -> list[dict[str, Any]]:
    """Every dict in a nested JSON structure, including ``$defs`` bodies."""
    found: list[dict[str, Any]] = []
    if isinstance(node, dict):
        found.append(node)
        for value in node.values():
            found.extend(_walk(value))
    elif isinstance(node, list):
        for item in node:
            found.extend(_walk(item))
    return found


def _schema() -> dict[str, Any]:
    return digest_tool()["function"]["parameters"]


# ── The contract's shape ──────────────────────────────────────────────────────


def test_schema_never_asks_for_as_of() -> None:
    """ADR-0124 D3: ``as_of`` is producer-stamped and must not appear in the contract.

    Walks ``$defs`` too — a nested definition is exactly where this would hide.
    """
    for obj in _walk(_schema()):
        assert "as_of" not in obj.get("properties", {})
        assert "as_of" not in obj.get("required", [])


def test_schema_never_asks_for_provenance_outside_corrections() -> None:
    """Amendment B retired the only basis that obliged a citation on the other slots."""
    schema = _schema()
    item = schema["$defs"]["WireItem"]
    assert set(item["properties"]) == {"text", "basis"}


def test_every_object_forbids_additional_properties() -> None:
    for obj in _walk(_schema()):
        if obj.get("type") == "object":
            assert obj.get("additionalProperties") is False


def test_enums_are_enums_not_const() -> None:
    """Single-value ``Literal``s must be emitted as ``enum``, which providers honour."""
    defs = _schema()["$defs"]
    assert defs["WireLocator"]["properties"]["field"]["enum"] == ["assistant_text"]
    assert defs["WireCorrection"]["properties"]["tier"]["enum"] == ["self_correction"]
    assert set(defs["WireItem"]["properties"]["basis"]["enum"]) == {
        "user_statement",
        "assistant_reasoning",
        "mixed",
    }
    for obj in _walk(_schema()):
        assert "const" not in obj


def test_label_bound_is_not_in_the_schema() -> None:
    """The schema dialect has no ``maxLength`` — the 90-char bound stays a Python check."""
    for obj in _walk(_schema()):
        assert "maxLength" not in obj
        assert "minLength" not in obj


def test_corrections_require_their_provenance() -> None:
    required = _schema()["$defs"]["WireCorrection"]["required"]
    for field in ("span", "locator", "evidence_span", "evidence_locator"):
        assert field in required


def test_bounded_variant_adds_maxitems_and_nothing_else() -> None:
    plain = digest_tool()["function"]["parameters"]
    bounded = digest_tool(bounded=True)["function"]["parameters"]

    slots = bounded["$defs"]["WireDigest"]["properties"]
    assert all("maxItems" in slot for slot in slots.values())

    # Strip the maxItems back out; what remains must be byte-identical.
    for slot in slots.values():
        del slot["maxItems"]
    assert orjson.dumps(bounded, option=orjson.OPT_SORT_KEYS) == orjson.dumps(
        plain, option=orjson.OPT_SORT_KEYS
    )


# ── Producer-side mapping ─────────────────────────────────────────────────────


def _envelope(**digest: object) -> DigestEnvelope:
    payload = {"established": [], "decisions": [], "unresolved": [], "corrections": []}
    payload.update(digest)
    return DigestEnvelope.model_validate({"label": "a session", "digest": payload})


def test_to_storage_stamps_as_of_from_ended_at() -> None:
    envelope = _envelope(
        unresolved=[
            {"text": "whether to keep the cap", "basis": "mixed"},
            {"text": "who owns the sweep", "basis": "user_statement"},
        ]
    )
    _, digest = to_storage(envelope, ended_at=ENDED_AT)

    assert [item.as_of for item in digest.unresolved] == [ENDED_AT, ENDED_AT]


def test_to_storage_enforces_the_label_bound_in_python() -> None:
    envelope = DigestEnvelope.model_validate(
        {
            "label": "x" * (MAX_LABEL_CHARS + 1),
            "digest": {"established": [], "decisions": [], "unresolved": [], "corrections": []},
        }
    )
    with pytest.raises(ValueError, match="label is"):
        to_storage(envelope, ended_at=ENDED_AT)


def test_to_storage_agrees_with_the_prose_parser() -> None:
    """The contract path and the legacy prose path must produce the same record.

    If these diverge, the pilot is comparing two different digests and its
    before/after is meaningless.
    """
    from personal_agent.second_brain.session_summary import parse_model_output

    payload = {
        "label": "cost audit and the digest ceiling",
        "digest": {
            "established": [{"text": "the cap is 5 USD", "basis": "user_statement"}],
            "decisions": [{"text": "do not raise the cap", "basis": "mixed"}],
            "unresolved": [{"text": "when to run the pilot", "basis": "assistant_reasoning"}],
            "corrections": [
                {
                    "text": "the earlier count was wrong",
                    "basis": "assistant_reasoning",
                    "tier": "self_correction",
                    "span": "I said 2048",
                    "locator": {"capture_id": "t1", "field": "assistant_text"},
                    "evidence_span": "it is actually 2856",
                    "evidence_locator": {"capture_id": "t1", "field": "assistant_text"},
                }
            ],
        },
    }

    via_prose_label, via_prose = parse_model_output(
        orjson.dumps(payload).decode(), ended_at=ENDED_AT
    )
    via_wire_label, via_wire = to_storage(DigestEnvelope.model_validate(payload), ended_at=ENDED_AT)

    assert via_wire_label == via_prose_label
    assert via_wire == via_prose


def test_wire_model_rejects_an_off_vocabulary_locator_field() -> None:
    with pytest.raises(ValueError):
        DigestEnvelope.model_validate(
            {
                "label": "x",
                "digest": {
                    "established": [],
                    "decisions": [],
                    "unresolved": [],
                    "corrections": [
                        {
                            "text": "t",
                            "basis": "mixed",
                            "tier": "self_correction",
                            "span": "s",
                            "locator": {"capture_id": "t1", "field": "user_message"},
                            "evidence_span": "e",
                            "evidence_locator": {"capture_id": "t1", "field": "assistant_text"},
                        }
                    ],
                },
            }
        )


# ── The mechanism pin ─────────────────────────────────────────────────────────


def test_litellm_maps_tool_without_json_mode() -> None:
    """Pin the property the pilot's truncation measurement rests on.

    litellm's ``response_format`` path sets ``json_mode``, which overwrites the
    provider's ``stop_reason`` with ``"stop"`` — truncation would then be
    indistinguishable from a clean stop. The explicit-tool path must not do that.

    Offline and free: this exercises litellm's parameter mapping, never the network.
    """
    from litellm.llms.anthropic.chat.transformation import AnthropicConfig

    mapped = AnthropicConfig().map_openai_params(
        {"tools": [digest_tool()], "tool_choice": digest_tool_choice()},
        {},
        "claude-sonnet-5",
        drop_params=False,
    )

    assert "json_mode" not in mapped
    assert mapped["tool_choice"] == {"type": "tool", "name": DIGEST_TOOL_NAME}
    assert [t["name"] for t in mapped["tools"]] == [DIGEST_TOOL_NAME]
    assert mapped["tools"][0]["input_schema"]["$defs"]["WireItem"]["properties"]["basis"]["enum"]
