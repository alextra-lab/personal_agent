"""Contract tests for the ADR-0098 D5 extraction-emission redesign (FRE-637).

These are UNIT tests: the LLM call is mocked so we assert the *contract shape*
the extractor returns — a knowledge ``class`` on every entity, structured
``stances`` (owner → World concept, never flattened) and ``claims`` (Personal
situational facts), and a Python-stamped ``provenance`` block on every stance
and claim. No live SLM server is required.

The synthetic fixtures are car-buying- and operational-equivalent shapes (not
real owner data — the FRE-636 spike fixture is private); they carry the same
structural signature AC-3/AC-4 exercise.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from unittest.mock import AsyncMock, patch

import orjson
import pytest

from personal_agent.config import get_settings
from personal_agent.second_brain.entity_extraction import (
    _EXTRACTION_PROMPT_TEMPLATE,
    _EXTRACTION_SYSTEM_PROMPT,
    ExtractionModelOverride,
    _build_extraction_prompt,
    extract_entities_and_relationships,
    prompt_material_for_hash,
)

#: Substring unique to the FRE-759 exemplar block; absent when the flag is off.
_EXEMPLAR_SENTINEL = "DISAMBIGUATION EXEMPLARS"

# ---------------------------------------------------------------------------
# Synthetic fixtures — model-emitted JSON (BEFORE Python provenance stamping).
# The mocked LLM returns exactly this; the extractor adds class defaults +
# provenance. Note the model does NOT emit provenance (Python's job) and here
# it even emits a *bogus* observed_at/trace_id on one item to prove Python
# overrides it.
# ---------------------------------------------------------------------------

_CARBUY_MODEL_JSON: dict[str, Any] = {
    "summary": (
        "User compared the Toyota RAV4 Hybrid and Honda CR-V Hybrid compact SUVs "
        "while deciding on a next car before a lease ends."
    ),
    "entities": [
        {
            "name": "Toyota RAV4 Hybrid",
            "type": "Technology",
            "class": "World",
            "description": "Compact hybrid SUV, ~40 mpg combined, evaluated in a purchase decision",
            "properties": {},
        },
        {
            "name": "Honda CR-V Hybrid",
            "type": "Technology",
            "class": "World",
            "description": "Compact hybrid SUV with a roomier cargo area",
            "properties": {},
        },
        {
            "name": "Hybrid Powertrain",
            "type": "Concept",
            "class": "World",
            "description": "Combined combustion-plus-electric drivetrain",
            "properties": {},
        },
    ],
    "relationships": [
        {
            "source": "Toyota RAV4 Hybrid",
            "target": "Hybrid Powertrain",
            "type": "USES",
            "weight": 0.9,
            "properties": {},
        }
    ],
    "stances": [
        {
            "subject": "owner",
            "target": "Toyota RAV4 Hybrid",
            "affect": "loves the hybrid powertrain",
            "mastery": None,
            "description": "User strongly prefers the RAV4 Hybrid's drivetrain over the CR-V Hybrid.",
            # Bogus provenance the model tried to emit — Python MUST override this.
            "provenance": {"trace_id": "MODEL-BOGUS", "observed_at": "1999-01-01T00:00:00+00:00"},
        }
    ],
    "claims": [
        {
            "subject": "owner",
            "content": "The user's current car lease ends in March.",
            "description": "Situational constraint driving the timing of the next-car decision.",
        }
    ],
}

_OPERATIONAL_MODEL_JSON: dict[str, Any] = {
    "summary": "User reviewed a healthcheck: Postgres healthy, Elasticsearch degraded.",
    "entities": [
        {
            "name": "Postgres",
            "type": "Technology",
            "output_kind": "finding",
            "description": "The agent's own database, referenced in a healthcheck",
            "properties": {},
        },
        {
            "name": "Elasticsearch",
            "type": "Technology",
            "output_kind": "finding",
            "description": "The agent's own log store, reported degraded in a healthcheck",
            "properties": {},
        },
    ],
    "relationships": [],
    "stances": [],
    "claims": [],
}

_CARBUY_USER_MSG = (
    "I'm trying to decide on my next car before my current lease ends in March. "
    "I've been comparing the Toyota RAV4 Hybrid and the Honda CR-V Hybrid. "
    "I really love the RAV4's hybrid powertrain."
)
_OPERATIONAL_USER_MSG = "Run a healthcheck on the stack."

_TRACE_ID = "a1b2c3d4-0000-0000-0000-000000000001"
_SESSION_ID = "sess-synthetic-carbuy-01"
_TURN_TS = datetime(2026, 7, 1, 14, 30, 0, tzinfo=timezone.utc)


def _mock_local_response(model_json: dict[str, Any]) -> dict[str, Any]:
    """Shape a LocalLLMClient.respond() return value wrapping the model JSON."""
    return {
        "content": orjson.dumps(model_json).decode("utf-8"),
        "usage": {"prompt_tokens": 100, "completion_tokens": 200},
    }


async def _run_extractor(
    model_json: dict[str, Any],
    *,
    user_message: str,
    turn_timestamp: datetime | None = _TURN_TS,
) -> dict[str, Any]:
    """Run the extractor with the local SLM path mocked to return model_json."""
    # models.yaml default entity_extraction_role is a cloud provider; force the
    # local path by making provider None so LocalLLMClient is used, then mock it.
    with (
        patch("personal_agent.second_brain.entity_extraction.load_model_config") as mock_cfg,
        patch(
            "personal_agent.second_brain.entity_extraction.resolve_role_model_key",
            return_value="primary",
        ),
        patch("personal_agent.second_brain.entity_extraction.LocalLLMClient") as mock_client_cls,
    ):
        mock_cfg.return_value.models = {}  # model_def is None → provider None → local path
        mock_client = mock_client_cls.return_value
        mock_client.respond = AsyncMock(return_value=_mock_local_response(model_json))

        return await extract_entities_and_relationships(
            user_message,
            "assistant reply",
            trace_id=_TRACE_ID,
            session_id=_SESSION_ID,
            turn_timestamp=turn_timestamp,
        )


@pytest.mark.asyncio
class TestExtractionEmissionContract:
    """AC-3 (extractor proxy) + AC-4-lays + provenance stamping."""

    async def test_stance_survives_as_structured_item(self) -> None:
        """AC-3a: an explicit first-person stance is emitted as a structured relation."""
        result = await _run_extractor(_CARBUY_MODEL_JSON, user_message=_CARBUY_USER_MSG)

        assert len(result["stances"]) >= 1
        stance = result["stances"][0]
        assert stance["subject"] == "owner"
        assert stance["affect"]  # non-empty
        assert stance["mastery"] is None
        # target must point at an emitted World concept so FRE-638 can attach the edge.
        assert stance["target"] in result["entity_names"]

    async def test_personal_fact_survives_as_claim(self) -> None:
        """AC-3b: a first-person situational fact is emitted as a Personal Claim."""
        result = await _run_extractor(_CARBUY_MODEL_JSON, user_message=_CARBUY_USER_MSG)

        assert len(result["claims"]) >= 1
        claim = result["claims"][0]
        assert claim["subject"] == "owner"
        assert claim["class"] == "Personal"
        assert "lease" in claim["content"].lower()

    async def test_stance_and_claim_not_flattened_into_entity_description(self) -> None:
        """AC-3c: the affect/lease text must NOT appear in any entity description.

        This assertion reproduces the current bug (flattening) when it fails.
        """
        result = await _run_extractor(_CARBUY_MODEL_JSON, user_message=_CARBUY_USER_MSG)

        for entity in result["entities"]:
            desc = (entity.get("description") or "").lower()
            assert "love" not in desc, f"stance flattened into {entity['name']} description"
            assert "lease" not in desc, f"claim flattened into {entity['name']} description"

    async def test_every_entity_has_valid_class(self) -> None:
        """ADR-0115 D1: every entity's class is in {World, Personal} — System left the class axis."""
        result = await _run_extractor(_CARBUY_MODEL_JSON, user_message=_CARBUY_USER_MSG)

        for entity in result["entities"]:
            assert entity["class"] in {"World", "Personal"}

    async def test_stance_and_claim_have_class_and_output_kind(self) -> None:
        """D5 'class for every item' + ADR-0115 D1 'output_kind for every item'.

        Stances/claims are always user-authored, so Python stamps output_kind=knowledge
        unconditionally (no LLM ambiguity to fail open on for these two item types).
        """
        result = await _run_extractor(_CARBUY_MODEL_JSON, user_message=_CARBUY_USER_MSG)

        assert all(s["class"] == "Stance" for s in result["stances"])
        assert all(s["output_kind"] == "knowledge" for s in result["stances"])
        assert all(c["class"] == "Personal" for c in result["claims"])
        assert all(c["output_kind"] == "knowledge" for c in result["claims"])

    async def test_operational_turn_emits_finding_output_kind(self) -> None:
        """ADR-0115 D1 (emission proxy): an operational turn's subjects are output_kind=finding.

        System-ness is now expressed via output_kind, never as class=System. Single
        fixture proves determinability; persisting/dispatching on this signal is the
        separate Persistence/Dispatch tickets this ticket blocks.
        """
        result = await _run_extractor(_OPERATIONAL_MODEL_JSON, user_message=_OPERATIONAL_USER_MSG)

        assert result["entities"]
        assert all(e["output_kind"] == "finding" for e in result["entities"])
        # class is not meaningful for finding items but must still fail open to World,
        # never resurrect System.
        assert all(e["class"] == "World" for e in result["entities"])

    async def test_python_stamps_provenance_overriding_llm(self) -> None:
        """Provenance is Python-stamped; a bogus model-emitted block is overridden."""
        result = await _run_extractor(_CARBUY_MODEL_JSON, user_message=_CARBUY_USER_MSG)

        for item in [*result["stances"], *result["claims"]]:
            prov = item["provenance"]
            assert prov["trace_id"] == _TRACE_ID  # not "MODEL-BOGUS"
            assert prov["session_id"] == _SESSION_ID
            assert prov["source_type"] == "conversation"
            assert prov["observed_at"] == _TURN_TS.isoformat()  # not 1999
            assert "extracted_at" in prov

    async def test_observed_at_falls_back_to_extraction_time_when_no_turn_ts(self) -> None:
        """If no turn_timestamp is threaded, observed_at falls back (never crashes)."""
        result = await _run_extractor(
            _CARBUY_MODEL_JSON, user_message=_CARBUY_USER_MSG, turn_timestamp=None
        )
        prov = result["claims"][0]["provenance"]
        assert prov["observed_at"]  # present (equals extracted_at on fallback)

    async def test_supplemented_person_entity_gets_default_class_and_output_kind(self) -> None:
        """The regex-supplemented Person entity carries a class + output_kind (finalize runs after supplement)."""
        model_json = {
            "summary": "Discussion of the project lead.",
            "entities": [],
            "relationships": [],
            "stances": [],
            "claims": [],
        }
        result = await _run_extractor(model_json, user_message="The project lead is Jane Smith.")
        supplemented = [e for e in result["entities"] if e["name"] == "Jane Smith"]
        assert supplemented, "regex should supplement the Person entity"
        assert supplemented[0]["class"] in {"World", "Personal"}
        assert supplemented[0]["output_kind"] == "knowledge"


@pytest.mark.asyncio
class TestFallbackShape:
    """Every fallback path must return the full contract shape (empty new arrays)."""

    async def test_default_result_includes_empty_stances_and_claims(self) -> None:
        """A JSON-parse-failure fallback returns stances:[] and claims:[]."""
        with (
            patch("personal_agent.second_brain.entity_extraction.load_model_config") as mock_cfg,
            patch(
                "personal_agent.second_brain.entity_extraction.resolve_role_model_key",
                return_value="primary",
            ),
            patch(
                "personal_agent.second_brain.entity_extraction.LocalLLMClient"
            ) as mock_client_cls,
        ):
            mock_cfg.return_value.models = {}
            mock_client = mock_client_cls.return_value
            mock_client.respond = AsyncMock(
                return_value={"content": "not valid json {{{", "usage": {}}
            )

            result = await extract_entities_and_relationships(
                "hello", "world", trace_id=_TRACE_ID, session_id=_SESSION_ID
            )

        assert result["stances"] == []
        assert result["claims"] == []
        assert result["entities"] == []


@pytest.mark.asyncio
class TestFacetAndUpdateKind:
    """FRE-712: claims carry a normalized ``facet`` slot key + an ``update_kind`` signal."""

    async def test_claim_facet_defaults_to_empty_when_absent(self) -> None:
        """A model that omits facet gets ``facet == ""`` (falls back to embedding matching)."""
        result = await _run_extractor(_CARBUY_MODEL_JSON, user_message=_CARBUY_USER_MSG)
        assert result["claims"]  # fixture emits at least one claim
        for claim in result["claims"]:
            assert claim["facet"] == ""

    async def test_claim_update_kind_defaults_to_new_when_absent(self) -> None:
        result = await _run_extractor(_CARBUY_MODEL_JSON, user_message=_CARBUY_USER_MSG)
        for claim in result["claims"]:
            assert claim["update_kind"] == "new"

    async def test_model_emitted_facet_is_normalized_and_kept(self) -> None:
        model_json: dict[str, Any] = {
            "summary": "s",
            "entities": [],
            "relationships": [],
            "stances": [],
            "claims": [
                {
                    "subject": "owner",
                    "content": "The user's lease ends in June.",
                    "facet": "Lease End Date",
                    "update_kind": "correction",
                }
            ],
        }
        result = await _run_extractor(model_json, user_message="my lease actually ends in June")
        claim = result["claims"][0]
        assert claim["facet"] == "lease_end_date"  # normalized to lower snake
        assert claim["update_kind"] == "correction"

    async def test_off_vocabulary_update_kind_normalizes_to_new(self) -> None:
        model_json: dict[str, Any] = {
            "summary": "s",
            "entities": [],
            "relationships": [],
            "stances": [],
            "claims": [
                {
                    "subject": "owner",
                    "content": "x",
                    "facet": "employer",
                    "update_kind": "corrected",
                }
            ],
        }
        result = await _run_extractor(model_json, user_message="msg")
        assert result["claims"][0]["update_kind"] == "new"

    async def test_evolution_update_kind_preserved(self) -> None:
        model_json: dict[str, Any] = {
            "summary": "s",
            "entities": [],
            "relationships": [],
            "stances": [],
            "claims": [
                {"subject": "owner", "content": "x", "facet": "city", "update_kind": "evolution"}
            ],
        }
        result = await _run_extractor(model_json, user_message="msg")
        assert result["claims"][0]["update_kind"] == "evolution"


def _entity_model_json(entity: dict[str, Any]) -> dict[str, Any]:
    return {
        "summary": "s",
        "entities": [entity],
        "relationships": [],
        "stances": [],
        "claims": [],
    }


@pytest.mark.asyncio
class TestDescriptionUpdateKind:
    """FRE-725: each entity carries a normalized ``description_update_kind`` signal."""

    async def test_description_update_kind_defaults_to_new_when_absent(self) -> None:
        model_json = _entity_model_json(
            {"name": "Neo4j", "type": "Technology", "class": "World", "description": "A database"}
        )
        result = await _run_extractor(model_json, user_message="what is Neo4j")
        assert result["entities"][0]["description_update_kind"] == "new"

    async def test_model_emitted_enrichment_and_correction_preserved(self) -> None:
        for kind in ("enrichment", "correction"):
            model_json = _entity_model_json(
                {
                    "name": "Neo4j",
                    "type": "Technology",
                    "class": "World",
                    "description": "A graph database management system",
                    "description_update_kind": kind,
                }
            )
            result = await _run_extractor(model_json, user_message="Neo4j is a graph DB")
            assert result["entities"][0]["description_update_kind"] == kind

    async def test_off_vocabulary_description_update_kind_normalizes_to_new(self) -> None:
        for bogus in ("enriched", "updated", "EVOLUTION"):
            model_json = _entity_model_json(
                {
                    "name": "Neo4j",
                    "type": "Technology",
                    "class": "World",
                    "description": "A database",
                    "description_update_kind": bogus,
                }
            )
            result = await _run_extractor(model_json, user_message="msg")
            assert result["entities"][0]["description_update_kind"] == "new"


@pytest.mark.asyncio
class TestOutputKindAxis:
    """ADR-0115 D1/D4: output_kind is a fail-open axis orthogonal to class; System leaves
    the class vocabulary entirely — a model still emitting class=System fails open to World.
    """

    async def test_output_kind_defaults_to_knowledge_when_absent(self) -> None:
        model_json = _entity_model_json(
            {"name": "Neo4j", "type": "Technology", "class": "World", "description": "A database"}
        )
        result = await _run_extractor(model_json, user_message="what is Neo4j")
        assert result["entities"][0]["output_kind"] == "knowledge"

    async def test_off_vocabulary_output_kind_fails_open_to_knowledge(self) -> None:
        model_json = _entity_model_json(
            {
                "name": "Neo4j",
                "type": "Technology",
                "class": "World",
                "output_kind": "background_noise",
                "description": "A database",
            }
        )
        result = await _run_extractor(model_json, user_message="what is Neo4j")
        assert result["entities"][0]["output_kind"] == "knowledge"

    async def test_finding_output_kind_is_preserved(self) -> None:
        model_json = _entity_model_json(
            {
                "name": "cost_gate_reaper",
                "type": "Technology",
                "output_kind": "finding",
                "description": "A harness maintenance job reviewed in a telemetry check.",
            }
        )
        result = await _run_extractor(model_json, user_message="review the reaper telemetry")
        assert result["entities"][0]["output_kind"] == "finding"

    async def test_ephemeral_output_kind_is_preserved(self) -> None:
        model_json = _entity_model_json(
            {
                "name": "connectivity ping",
                "type": "Technology",
                "output_kind": "ephemeral",
                "description": "A bare connectivity check with no lasting content.",
            }
        )
        result = await _run_extractor(model_json, user_message="ping")
        assert result["entities"][0]["output_kind"] == "ephemeral"

    async def test_system_class_fails_open_to_world_not_preserved(self) -> None:
        """A model still emitting the retired 'System' class value fails open to World.

        System is no longer in the class vocabulary (ADR-0115 D1) — an off-vocabulary
        class value must not silently persist.
        """
        model_json = _entity_model_json(
            {
                "name": "Postgres",
                "type": "Technology",
                "class": "System",
                "description": "The agent's own database",
            }
        )
        result = await _run_extractor(model_json, user_message="healthcheck")
        assert result["entities"][0]["class"] == "World"


class TestTwoAxisPromptContract:
    """ADR-0115 D1: the prompt instructs the model to emit output_kind + class as two axes."""

    def test_prompt_advertises_output_kind_field(self) -> None:
        """The JSON schema block advertises the output_kind enum on every entity."""
        prompt = _build_extraction_prompt("u", "a")
        assert '"output_kind": "knowledge|ephemeral|finding"' in prompt

    def test_prompt_class_vocabulary_drops_system(self) -> None:
        """The JSON schema's class enum is World|Personal only — System is gone."""
        prompt = _build_extraction_prompt("u", "a")
        assert '"class": "World|Personal"' in prompt
        assert '"class": "World|Personal|System"' not in prompt

    def test_system_prompt_describes_two_axes(self) -> None:
        """The system prompt names OUTPUT KIND and drops the old three-value class framing."""
        assert "OUTPUT KIND" in _EXTRACTION_SYSTEM_PROMPT
        assert "World / Personal / System" not in _EXTRACTION_SYSTEM_PROMPT


@pytest.mark.asyncio
class TestCloudPathTemperature:
    """FRE-758: the cloud extraction call must forward model_def.temperature.

    Unlike the local-path tests above, this exercises the ``provider is not None``
    branch (entity_extraction.py) by mocking ``get_llm_client_for_key`` — proves the
    configured temperature (near-0, pinned per FRE-758) actually reaches
    ``LiteLLMClient.respond`` instead of silently defaulting to the OpenAI
    provider default (~1.0), which was the FRE-630-observed non-determinism.
    """

    async def test_cloud_path_passes_configured_temperature(self) -> None:
        """The cloud call forwards model_def.temperature as an explicit kwarg."""
        from types import SimpleNamespace

        mock_model_def = SimpleNamespace(
            provider="openai", id="gpt-5.4-mini", temperature=0.0, reasoning_effort=None
        )
        with (
            patch("personal_agent.second_brain.entity_extraction.load_model_config") as mock_cfg,
            patch(
                "personal_agent.second_brain.entity_extraction.resolve_role_model_key",
                return_value="gpt-5.4-mini",
            ),
            patch("personal_agent.llm_client.factory.get_llm_client_for_key") as mock_get_client,
        ):
            mock_cfg.return_value.models = {"gpt-5.4-mini": mock_model_def}
            mock_client = mock_get_client.return_value
            mock_client.respond = AsyncMock(
                return_value={
                    "content": orjson.dumps(_OPERATIONAL_MODEL_JSON).decode("utf-8"),
                }
            )

            await extract_entities_and_relationships(_OPERATIONAL_USER_MSG, "assistant reply")

            assert mock_client.respond.call_args.kwargs["temperature"] == 0.0
            # FRE-869: entity_extraction_role is a resolved model key, not a factory
            # role name — get_llm_client_for_key takes budget_role explicitly so spend
            # lands in the entity_extraction budget lane rather than being silently
            # mis-billed to main_inference.
            mock_get_client.assert_called_once_with("gpt-5.4-mini", budget_role="entity_extraction")


class TestFewshotExemplarFlag:
    """FRE-759: the flag-gated few-shot exemplar splice + flag-aware hash material.

    Proves the *mechanism* (AC-4): the prompt toggles deterministically through the
    one shared seam, the hash material distinguishes the two prompts, and the
    exemplar block's literal JSON braces do not break ``.format()`` (codex P2.2).
    The *outcome* (accuracy lift) is the owner-gated FRE-630 A/B, not a unit test.
    """

    def test_flag_off_excludes_exemplars_and_hash_is_system_plus_template(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Flag off: no exemplar block, and hash material == system + template exactly."""
        monkeypatch.setattr(
            get_settings(), "entity_extraction_fewshot_exemplars_enabled", False, raising=False
        )
        prompt = _build_extraction_prompt("u", "a")
        assert _EXEMPLAR_SENTINEL not in prompt
        assert prompt_material_for_hash() == _EXTRACTION_SYSTEM_PROMPT + _EXTRACTION_PROMPT_TEMPLATE

    def test_flag_on_includes_exemplars_and_hash_differs(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Flag on: exemplar block present, and the hash material diverges from flag-off."""
        monkeypatch.setattr(
            get_settings(), "entity_extraction_fewshot_exemplars_enabled", False, raising=False
        )
        off_material = prompt_material_for_hash()
        monkeypatch.setattr(
            get_settings(), "entity_extraction_fewshot_exemplars_enabled", True, raising=False
        )
        prompt = _build_extraction_prompt("u", "a")
        assert _EXEMPLAR_SENTINEL in prompt
        assert prompt_material_for_hash() != off_material
        # The flag-on material is the flag-off material plus the rendered block.
        assert prompt_material_for_hash().startswith(off_material)

    def test_flag_on_renders_without_brace_errors(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The exemplar block's literal JSON braces must not break .format() (P2.2).

        The block is a pre-rendered ``.format()`` value, so its ``{"subject":...}``
        JSON is substituted literally and never rescanned for format fields.
        """
        monkeypatch.setattr(
            get_settings(), "entity_extraction_fewshot_exemplars_enabled", True, raising=False
        )
        # Must not raise KeyError/ValueError from the JSON braces in the exemplar value.
        prompt = _build_extraction_prompt("my lease ends in March", "assistant reply")
        assert '{"subject":"owner"' in prompt  # exemplar JSON present as literal text
        assert "my lease ends in March" in prompt  # per-case content still interpolates


@pytest.mark.asyncio
class TestModelOverrideAndCallStats:
    """FRE-766: the eval-only model_override DI seam + call_stats capture.

    The benchmark drives the real extractor across a model×reasoning matrix without
    mutating global config (concurrency-safe). These prove the seam forwards the
    override's model/reasoning/budget-lane and surfaces usage/cost/error per call.
    """

    async def test_override_drives_model_reasoning_and_budget_role(self) -> None:
        """model_override builds a client for its model with budget_role=entity_extraction
        and forwards its reasoning_effort + temperature to respond().
        """
        override = ExtractionModelOverride(
            model_id="gpt-5.4",
            provider="openai",
            reasoning_effort="high",
            temperature=None,
            max_tokens=4096,
        )
        with patch("personal_agent.llm_client.litellm_client.LiteLLMClient") as mock_client_cls:
            mock_client = mock_client_cls.return_value
            mock_client.respond = AsyncMock(
                return_value={
                    "content": orjson.dumps(_OPERATIONAL_MODEL_JSON).decode("utf-8"),
                    "usage": {"prompt_tokens": 5, "completion_tokens": 7},
                    "cost_usd": 0.001,
                }
            )
            await extract_entities_and_relationships(
                _OPERATIONAL_USER_MSG, "assistant reply", model_override=override
            )
            # client built for the override model, in the entity_extraction budget lane
            ckw = mock_client_cls.call_args.kwargs
            assert ckw["model_id"] == "gpt-5.4"
            assert ckw["provider"] == "openai"
            assert ckw["budget_role"] == "entity_extraction"
            # reasoning_effort + temperature forwarded from the override
            rkw = mock_client.respond.call_args.kwargs
            assert rkw["reasoning_effort"] == "high"
            assert rkw["temperature"] is None

    async def test_prod_path_forwards_config_reasoning_effort(self) -> None:
        """No override: the cloud call forwards model_def.reasoning_effort from config."""
        from types import SimpleNamespace

        model_def = SimpleNamespace(
            provider="openai", id="gpt-5.4-mini", temperature=0.0, reasoning_effort="high"
        )
        with (
            patch("personal_agent.second_brain.entity_extraction.load_model_config") as mock_cfg,
            patch(
                "personal_agent.second_brain.entity_extraction.resolve_role_model_key",
                return_value="gpt-5.4-mini",
            ),
            patch("personal_agent.llm_client.factory.get_llm_client_for_key") as mock_get_client,
        ):
            mock_cfg.return_value.models = {"gpt-5.4-mini": model_def}
            mock_client = mock_get_client.return_value
            mock_client.respond = AsyncMock(
                return_value={"content": orjson.dumps(_OPERATIONAL_MODEL_JSON).decode("utf-8")}
            )
            await extract_entities_and_relationships(_OPERATIONAL_USER_MSG, "assistant reply")
            assert mock_client.respond.call_args.kwargs["reasoning_effort"] == "high"

    async def test_call_stats_sink_captures_usage_and_cost(self) -> None:
        """A passed call_stats_sink gets usage/reasoning_tokens/cost with error_class None."""
        override = ExtractionModelOverride(model_id="gpt-5.4", provider="openai")
        sink: list[dict[str, Any]] = []
        with patch("personal_agent.llm_client.litellm_client.LiteLLMClient") as mock_client_cls:
            mock_client_cls.return_value.respond = AsyncMock(
                return_value={
                    "content": orjson.dumps(_OPERATIONAL_MODEL_JSON).decode("utf-8"),
                    "usage": {"prompt_tokens": 5, "completion_tokens": 7, "reasoning_tokens": 42},
                    "cost_usd": 0.002,
                }
            )
            await extract_entities_and_relationships(
                _OPERATIONAL_USER_MSG,
                "assistant reply",
                model_override=override,
                call_stats_sink=sink,
            )
        assert len(sink) == 1
        assert sink[0]["reasoning_tokens"] == 42
        assert sink[0]["cost_usd"] == 0.002
        assert sink[0]["error_class"] is None

    async def test_call_stats_sink_records_error_class_on_failure(self) -> None:
        """When the cloud call raises, the sink records the error_class (for the smoke
        classifier) and the extractor still returns the empty fallback shape.
        """
        override = ExtractionModelOverride(model_id="gpt-5.4", provider="openai")
        sink: list[dict[str, Any]] = []
        with patch("personal_agent.llm_client.litellm_client.LiteLLMClient") as mock_client_cls:
            mock_client_cls.return_value.respond = AsyncMock(
                side_effect=RuntimeError("provider rejected reasoning_effort=xhigh")
            )
            result = await extract_entities_and_relationships(
                _OPERATIONAL_USER_MSG,
                "assistant reply",
                model_override=override,
                call_stats_sink=sink,
            )
        assert result["entities"] == []  # fallback shape
        assert len(sink) == 1
        assert sink[0]["error_class"] == "RuntimeError"

    async def test_budget_denied_reraises_and_appends_no_stat(self) -> None:
        """BudgetDenied re-raises (consolidator retry signal) and does NOT append a
        generic error stat — it is caught by its own handler before the sink append.
        """
        from datetime import datetime, timezone
        from decimal import Decimal

        from personal_agent.cost_gate import BudgetDenied

        override = ExtractionModelOverride(model_id="gpt-5.4", provider="openai")
        sink: list[dict[str, Any]] = []
        denied = BudgetDenied(
            role="entity_extraction",
            time_window="daily",
            current_spend=Decimal("5"),
            cap=Decimal("5"),
            window_resets_at=datetime(2026, 7, 4, tzinfo=timezone.utc),
        )
        with patch("personal_agent.llm_client.litellm_client.LiteLLMClient") as mock_client_cls:
            mock_client_cls.return_value.respond = AsyncMock(side_effect=denied)
            with pytest.raises(BudgetDenied):
                await extract_entities_and_relationships(
                    _OPERATIONAL_USER_MSG,
                    "assistant reply",
                    model_override=override,
                    call_stats_sink=sink,
                )
        assert sink == []  # BudgetDenied re-raised before the generic-except stat append
