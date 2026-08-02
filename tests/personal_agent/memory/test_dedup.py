# tests/personal_agent/memory/test_dedup.py
"""Tests for fuzzy entity deduplication.

The dedup pipeline checks vector similarity before MERGE to prevent
near-duplicate explosion (40 mentions → 500 nodes → should be ~10).

Fixture entity_type values are arbitrary strings — dedup.py filters generically on
whatever type value its caller supplies (no hardcoded taxonomy), so these fixtures use
the ADR-0109 V2 taxonomy (FRE-794) purely for consistency with the live extractor, not
because dedup.py depends on the specific strings.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from personal_agent import memory as _pa_memory  # noqa: F401
from personal_agent.memory import dedup as dedup_module
from personal_agent.memory.dedup import (
    DedupDecision,
    DedupResult,
    _find_similar_entities,
    _is_allcaps_identifier,
    _names_are_equivalent,
    check_entity_duplicate,
)


@pytest.fixture(autouse=True)
def _pin_dedup_settings() -> None:
    """Pin dedup threshold so tests are independent of config."""
    mock_settings = MagicMock()
    mock_settings.dedup_similarity_threshold = 0.92
    with patch("personal_agent.memory.dedup.get_settings", return_value=mock_settings):
        yield  # type: ignore[misc]


class TestCheckEntityDuplicate:
    """Tests for the top-level dedup decision function."""

    @pytest.mark.asyncio
    async def test_no_existing_entities_no_dedup(self) -> None:
        """No existing entities → create new (no duplicate)."""
        with patch(
            "personal_agent.memory.dedup._find_similar_entities",
            new_callable=AsyncMock,
            return_value=[],
        ):
            result = await check_entity_duplicate(
                name="PostgreSQL",
                entity_type="TechnicalArtifact",
                embedding=[0.1] * 1536,
                neo4j_session=AsyncMock(),
            )
        assert result.decision == DedupDecision.CREATE_NEW
        assert result.canonical_name is None

    @pytest.mark.asyncio
    async def test_exact_match_merges(self) -> None:
        """Exact name match → merge with existing."""
        with patch(
            "personal_agent.memory.dedup._find_similar_entities",
            new_callable=AsyncMock,
            return_value=[
                {"name": "PostgreSQL", "similarity": 1.0, "entity_type": "TechnicalArtifact"}
            ],
        ):
            result = await check_entity_duplicate(
                name="PostgreSQL",
                entity_type="TechnicalArtifact",
                embedding=[0.1] * 1536,
                neo4j_session=AsyncMock(),
            )
        assert result.decision == DedupDecision.MERGE_EXISTING
        assert result.canonical_name == "PostgreSQL"

    @pytest.mark.asyncio
    async def test_high_similarity_merges(self) -> None:
        """Above threshold similarity → merge when the names are the same name.

        FRE-1115 changed this case. It previously asserted that "PostgreSQL Database"
        merges into "Postgres" on similarity alone; a merge destroys the losing
        entity's identity, and that pair is a *substantive* judgement rather than two
        spellings of one name. Whether such pairs should merge is the dedup-semantics
        ADR's question, so the above-threshold merge is now exercised with a genuine
        name variant.
        """
        with patch(
            "personal_agent.memory.dedup._find_similar_entities",
            new_callable=AsyncMock,
            return_value=[
                {"name": "Postgres", "similarity": 0.92, "entity_type": "TechnicalArtifact"}
            ],
        ):
            result = await check_entity_duplicate(
                name="postgres",
                entity_type="TechnicalArtifact",
                embedding=[0.1] * 1536,
                neo4j_session=AsyncMock(),
            )
        assert result.decision == DedupDecision.MERGE_EXISTING
        assert result.canonical_name == "Postgres"

    @pytest.mark.asyncio
    async def test_high_similarity_alone_does_not_merge_distinct_names(self) -> None:
        """Similarity above threshold is not sufficient to rename (FRE-1115)."""
        with patch(
            "personal_agent.memory.dedup._find_similar_entities",
            new_callable=AsyncMock,
            return_value=[
                {"name": "Postgres", "similarity": 0.92, "entity_type": "TechnicalArtifact"}
            ],
        ):
            result = await check_entity_duplicate(
                name="PostgreSQL Database",
                entity_type="TechnicalArtifact",
                embedding=[0.1] * 1536,
                neo4j_session=AsyncMock(),
            )
        assert result.decision == DedupDecision.CREATE_NEW

    @pytest.mark.asyncio
    async def test_low_similarity_creates_new(self) -> None:
        """Below threshold similarity → create new entity."""
        with patch(
            "personal_agent.memory.dedup._find_similar_entities",
            new_callable=AsyncMock,
            return_value=[{"name": "Redis", "similarity": 0.3, "entity_type": "TechnicalArtifact"}],
        ):
            result = await check_entity_duplicate(
                name="PostgreSQL",
                entity_type="TechnicalArtifact",
                embedding=[0.1] * 1536,
                neo4j_session=AsyncMock(),
            )
        assert result.decision == DedupDecision.CREATE_NEW


class TestFindSimilarEntities:
    """Tests for the Cypher candidate query itself."""

    @pytest.mark.asyncio
    async def test_query_excludes_user_id_bound_nodes(self) -> None:
        """Cypher must filter out user_id-bound :Person nodes (FRE-342).

        Owner/user-anchored :Person nodes (FRE-213 schema) must never appear
        as merge candidates for extracted entities, otherwise an extracted
        "Alex" would collide into the harness owner Person and destroy the
        user_id anchor invariant.
        """
        session = AsyncMock()
        result_obj = AsyncMock()
        result_obj.data = AsyncMock(return_value=[])
        session.run = AsyncMock(return_value=result_obj)

        await _find_similar_entities(
            embedding=[0.1] * 1536,
            entity_type="Person",
            neo4j_session=session,
            top_k=5,
        )

        session.run.assert_awaited_once()
        cypher = session.run.await_args.args[0]
        assert "node.user_id IS NULL" in cypher

    @pytest.mark.asyncio
    async def test_scopes_by_v2_entity_type_value(self) -> None:
        """Dedup grain is generic — it binds whatever V2 type string the caller supplies.

        ADR-0109 (FRE-794): dedup.py has no hardcoded taxonomy; the Cypher WHERE
        clause filters on the caller-supplied entity_type parameter, so it operates
        correctly on the retired V1 strings, the V2 strings, or anything else.
        """
        session = AsyncMock()
        result_obj = AsyncMock()
        result_obj.data = AsyncMock(return_value=[])
        session.run = AsyncMock(return_value=result_obj)

        await _find_similar_entities(
            embedding=[0.1] * 1536,
            entity_type="TechnicalArtifact",
            neo4j_session=session,
            top_k=5,
        )

        session.run.assert_awaited_once()
        assert session.run.await_args.kwargs["entity_type"] == "TechnicalArtifact"


class TestAllcapsGuard:
    """Tests for the ALL_CAPS name-pattern guard (FRE-412)."""

    @pytest.mark.asyncio
    async def test_allcaps_does_not_merge_with_snakecase(self) -> None:
        """ALL_CAPS FSM state must not merge with snake_case entity (FRE-412).

        LLM_CALL (similarity 0.935) should not merge into model_call_error.
        """
        with patch(
            "personal_agent.memory.dedup._find_similar_entities",
            new_callable=AsyncMock,
            return_value=[
                {"name": "model_call_error", "similarity": 0.935, "entity_type": "MethodOrConcept"}
            ],
        ):
            result = await check_entity_duplicate(
                name="LLM_CALL",
                entity_type="MethodOrConcept",
                embedding=[0.1] * 1536,
                neo4j_session=AsyncMock(),
            )
        assert result.decision == DedupDecision.CREATE_NEW

    @pytest.mark.asyncio
    async def test_allcaps_merges_with_allcaps(self) -> None:
        """Two ALL_CAPS names pass the FRE-412 guard and merge when name-equivalent.

        FRE-1115 changed this case. It previously used HTTPS/HTTP at 0.95 — two
        different protocols, so asserting they merge encoded the conflation defect
        this ticket fixes. The FRE-412 behaviour under test is that a matched *shape*
        does not itself block the merge, which an underscore variant exercises without
        asserting that two distinct protocols are one entity.
        """
        with patch(
            "personal_agent.memory.dedup._find_similar_entities",
            new_callable=AsyncMock,
            return_value=[
                {"name": "LLM_CALL", "similarity": 0.95, "entity_type": "MethodOrConcept"}
            ],
        ):
            result = await check_entity_duplicate(
                name="LLMCALL",
                entity_type="MethodOrConcept",
                embedding=[0.1] * 1536,
                neo4j_session=AsyncMock(),
            )
        assert result.decision == DedupDecision.MERGE_EXISTING
        assert result.canonical_name == "LLM_CALL"

    @pytest.mark.asyncio
    async def test_allcaps_distinct_protocols_do_not_merge(self) -> None:
        """HTTPS must not be absorbed into HTTP however close the embeddings."""
        with patch(
            "personal_agent.memory.dedup._find_similar_entities",
            new_callable=AsyncMock,
            return_value=[{"name": "HTTP", "similarity": 0.95, "entity_type": "MethodOrConcept"}],
        ):
            result = await check_entity_duplicate(
                name="HTTPS",
                entity_type="MethodOrConcept",
                embedding=[0.1] * 1536,
                neo4j_session=AsyncMock(),
            )
        assert result.decision == DedupDecision.CREATE_NEW

    @pytest.mark.asyncio
    async def test_snakecase_does_not_merge_with_allcaps(self) -> None:
        """snake_case entity must not merge into an ALL_CAPS canonical."""
        with patch(
            "personal_agent.memory.dedup._find_similar_entities",
            new_callable=AsyncMock,
            return_value=[
                {"name": "LLM_CALL", "similarity": 0.94, "entity_type": "MethodOrConcept"}
            ],
        ):
            result = await check_entity_duplicate(
                name="llm_call_wrapper",
                entity_type="MethodOrConcept",
                embedding=[0.1] * 1536,
                neo4j_session=AsyncMock(),
            )
        assert result.decision == DedupDecision.CREATE_NEW

    def test_is_allcaps_identifier_true(self) -> None:
        """Recognises ALL_CAPS_WITH_UNDERSCORES names."""
        assert _is_allcaps_identifier("LLM_CALL")
        assert _is_allcaps_identifier("TOOL_EXECUTION")
        assert _is_allcaps_identifier("SYNTHESIS")
        assert _is_allcaps_identifier("HTTP")

    def test_is_allcaps_identifier_false(self) -> None:
        """Rejects mixed-case and snake_case names."""
        assert not _is_allcaps_identifier("model_call_error")
        assert not _is_allcaps_identifier("PostgreSQL")
        assert not _is_allcaps_identifier("Redis")
        assert not _is_allcaps_identifier("llm_call")
        assert not _is_allcaps_identifier("A")  # single char — too short

    @pytest.mark.asyncio
    async def test_below_raised_threshold_creates_new(self) -> None:
        """Similarity 0.88 is below the new 0.92 threshold — creates new."""
        with patch(
            "personal_agent.memory.dedup._find_similar_entities",
            new_callable=AsyncMock,
            return_value=[
                {"name": "Redis", "similarity": 0.88, "entity_type": "TechnicalArtifact"}
            ],
        ):
            result = await check_entity_duplicate(
                name="RedisQueue",
                entity_type="TechnicalArtifact",
                embedding=[0.1] * 1536,
                neo4j_session=AsyncMock(),
            )
        assert result.decision == DedupDecision.CREATE_NEW


class TestDedupResult:
    """Tests for the DedupResult dataclass."""

    def test_create_new(self) -> None:
        """CREATE_NEW result has no canonical name."""
        result = DedupResult(decision=DedupDecision.CREATE_NEW)
        assert result.canonical_name is None

    def test_merge_existing(self) -> None:
        """MERGE_EXISTING result carries canonical name and score."""
        result = DedupResult(
            decision=DedupDecision.MERGE_EXISTING,
            canonical_name="PostgreSQL",
            similarity_score=0.95,
        )
        assert result.canonical_name == "PostgreSQL"


class TestNameCompatibilityGuard:
    """FRE-1115 Step 0 — a different-name merge requires name equivalence.

    The 0.92 cosine threshold conflates distinct entities under the OVH 8B embedder
    (measured on the live graph 2026-08-02: ``mathematics`` -> ``computer science`` at
    0.960, ``Blueberries`` -> ``Apricots`` at 0.957). Merging destroys the losing
    entity's identity, so an above-threshold match may only rename when the two names
    are equivalent under casefold + accent-fold + punctuation normalization. Rejecting
    is fail-open: it creates a distinct entity rather than destroying one.

    Pairs below are the real ``entity_deduplicated`` events that motivated the guard.
    """

    # (proposed, canonical, observed similarity) — all above the 0.92 threshold.
    CONFLATING_PAIRS = [
        ("mathematics", "computer science", 0.960),
        ("neuroscience", "computer science", 0.923),
        ("Blueberries", "Apricots", 0.957),
        ("Arts faculty", "Cornell University", 0.935),
        ("Walkaway", "Little Brother", 0.936),
        ("Azure", "Bedrock", 0.952),
        ("Vertex", "Bedrock", 0.939),
        ("WebAuthn", "Passkey authentication", 0.978),
        ("Cantaloupe", "Melon", 0.966),
        ("Cumin", "Ground cumin", 0.952),
    ]

    # Name-equivalent pairs that must keep merging — these are the legitimate value
    # dedup provides (diacritic and punctuation variants of one name).
    EQUIVALENT_PAIRS = [
        ("Pâté à bombe", "Pâte à bombe", 0.928),
        ("Météo-France", "Météo France", 0.945),
        ("LM  Studio", "LM Studio", 0.999),
        ("neo4j", "Neo4j", 0.999),
    ]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("proposed,canonical,similarity", CONFLATING_PAIRS)
    async def test_distinct_names_do_not_merge(
        self, proposed: str, canonical: str, similarity: float
    ) -> None:
        """An above-threshold match between non-equivalent names must not merge."""
        with patch(
            "personal_agent.memory.dedup._find_similar_entities",
            new_callable=AsyncMock,
            return_value=[
                {"name": canonical, "similarity": similarity, "entity_type": "DomainOrTopic"}
            ],
        ):
            result = await check_entity_duplicate(
                name=proposed,
                entity_type="DomainOrTopic",
                embedding=[0.1] * 1536,
                neo4j_session=AsyncMock(),
            )
        assert result.decision == DedupDecision.CREATE_NEW, (
            f"{proposed!r} must stay distinct from {canonical!r} (sim={similarity})"
        )
        assert result.canonical_name is None

    @pytest.mark.asyncio
    @pytest.mark.parametrize("proposed,canonical,similarity", EQUIVALENT_PAIRS)
    async def test_name_equivalent_variants_still_merge(
        self, proposed: str, canonical: str, similarity: float
    ) -> None:
        """Diacritic / punctuation / case variants of one name still merge."""
        with patch(
            "personal_agent.memory.dedup._find_similar_entities",
            new_callable=AsyncMock,
            return_value=[
                {"name": canonical, "similarity": similarity, "entity_type": "MethodOrConcept"}
            ],
        ):
            result = await check_entity_duplicate(
                name=proposed,
                entity_type="MethodOrConcept",
                embedding=[0.1] * 1536,
                neo4j_session=AsyncMock(),
            )
        assert result.decision == DedupDecision.MERGE_EXISTING, (
            f"{proposed!r} and {canonical!r} name the same thing and must merge"
        )
        assert result.canonical_name == canonical

    @pytest.mark.asyncio
    async def test_rejected_merge_is_logged_for_audit(self) -> None:
        """A rejected merge emits the audit event the FRE-1115 Step 7 ticket needs."""
        with (
            patch(
                "personal_agent.memory.dedup._find_similar_entities",
                new_callable=AsyncMock,
                return_value=[
                    {
                        "name": "computer science",
                        "similarity": 0.96,
                        "entity_type": "DomainOrTopic",
                    }
                ],
            ),
            patch.object(dedup_module.logger, "info") as mock_log,
        ):
            await check_entity_duplicate(
                name="mathematics",
                entity_type="DomainOrTopic",
                embedding=[0.1] * 1536,
                neo4j_session=AsyncMock(),
            )
        events = [c.args[0] for c in mock_log.call_args_list if c.args]
        assert "entity_dedup_rejected_name_incompatible" in events
        kwargs = next(
            c.kwargs
            for c in mock_log.call_args_list
            if c.args and c.args[0] == "entity_dedup_rejected_name_incompatible"
        )
        assert kwargs["proposed_name"] == "mathematics"
        assert kwargs["candidate_name"] == "computer science"
        assert kwargs["similarity"] == pytest.approx(0.96)


class TestNamesAreEquivalent:
    """Unit tests for the normalization predicate itself."""

    @pytest.mark.parametrize(
        "a,b",
        [
            ("Predictive Processing", "predictive processing"),
            ("Pâté à bombe", "Pate a bombe"),
            ("Météo-France", "Météo France"),
            ("LM  Studio", "lm studio"),
            ("claude-code", "Claude Code"),
        ],
    )
    def test_equivalent(self, a: str, b: str) -> None:
        """Case, diacritic, punctuation and spacing variants are equivalent."""
        assert _names_are_equivalent(a, b)

    @pytest.mark.parametrize(
        "a,b",
        [
            ("mathematics", "computer science"),
            ("Blueberries", "Apricots"),
            ("Cumin", "Ground cumin"),
            ("Azure", "Bedrock"),
            ("Walkaway", "Little Brother"),
        ],
    )
    def test_not_equivalent(self, a: str, b: str) -> None:
        """Distinct names are not equivalent however similar their embeddings."""
        assert not _names_are_equivalent(a, b)
