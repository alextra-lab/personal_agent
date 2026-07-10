"""Tests for the ADR-0114 D3/D4 accretion writer (FRE-839).

Unit-level: mocked Neo4j driver/session, no real infra. Alias-resolution
tests are the direct regression coverage for the codex plan-review finding
(asymmetric kind-gating: exact-match kind-blind + first-write-wins,
embedding-fallback kind-gated) and for the batching finding (one Cypher
round-trip per episode, not per concept/membership).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime, timezone
from typing import Any

import pytest
from scripts.study.writer import (
    AssertionProvenance,
    ProposedMembership,
    ResolvedConceptMemberships,
    _is_allcaps_identifier,
    _normalize,
    recompute_member_of_batch,
    resolve_concept_hub,
    resolve_concept_hubs_batch,
    write_episode,
    write_mentions_and_assertions,
)


class _FakeResult:
    def __init__(self, records: list[dict[str, Any]]) -> None:
        self._records = records

    def __aiter__(self) -> AsyncIterator[dict[str, Any]]:
        return self._aiter()

    async def _aiter(self) -> AsyncIterator[dict[str, Any]]:
        for record in self._records:
            yield record

    async def single(self) -> dict[str, Any] | None:
        return self._records[0] if self._records else None


class _ScriptedSession:
    """Fake session dispatching canned results by a query-substring match,
    and recording every (query, params) call for assertions.
    """

    def __init__(self, responses: list[tuple[str, list[dict[str, Any]]]]) -> None:
        self._responses = responses
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def run(self, query: str, parameters: dict[str, Any] | None = None) -> _FakeResult:
        parameters = parameters or {}
        self.calls.append((query, parameters))
        for marker, records in self._responses:
            if marker in query:
                if marker == "db.index.vector.queryNodes":
                    # Faithfully simulate the query's own `WHERE node.kind = $kind`
                    # clause (real Neo4j filters server-side; this fake must too,
                    # or the kind-gating regression tests would pass vacuously).
                    records = [r for r in records if r["kind"] == parameters.get("kind")]
                return _FakeResult(records)
        return _FakeResult([])

    async def __aenter__(self) -> "_ScriptedSession":
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        return None


# ---------------------------------------------------------------------------
# _normalize / _is_allcaps_identifier
# ---------------------------------------------------------------------------


def test_normalize_case_folds_and_strips() -> None:
    assert _normalize("  Arterial Calcification  ") == "arterial calcification"
    assert _normalize("Arterial calcification") == "arterial calcification"


@pytest.mark.parametrize(
    ("name", "expected"),
    [("PENDING", True), ("FSM_STATE_A", True), ("Pending", False), ("pending", False)],
)
def test_is_allcaps_identifier(name: str, expected: bool) -> None:
    assert _is_allcaps_identifier(name) is expected


# ---------------------------------------------------------------------------
# resolve_concept_hub — exact-match path is kind-BLIND (codex finding)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_exact_match_merges_across_differing_kind_first_write_wins() -> None:
    """The ADR's own named bug: `Arterial calcification` (Phenomenon) and
    `Arterial Calcification` (DomainOrTopic) must collapse to one hub even
    though prod tagged them with different kinds — kind-gating the exact
    match would fail to fix the exact case this ADR exists for.
    """
    session = _ScriptedSession(
        [
            (
                "MATCH (s:Surface {normalized_name: $normalized_name})",
                [{"concept_id": "concept-1", "kind": "Phenomenon"}],
            ),
        ]
    )

    concept_id = await resolve_concept_hub(
        session,
        surface_name="Arterial Calcification",
        kind="DomainOrTopic",
        embedding=None,
    )

    assert concept_id == "concept-1"
    # The existing concept's kind (first-write-wins) must never be overwritten
    # by this call — reading c.kind is fine, mutating it is not.
    assert not any("SET c.kind" in query for query, _ in session.calls)


@pytest.mark.asyncio
async def test_exact_match_short_circuits_before_any_embedding_search() -> None:
    session = _ScriptedSession(
        [
            (
                "MATCH (s:Surface {normalized_name: $normalized_name})",
                [{"concept_id": "concept-1", "kind": "Phenomenon"}],
            ),
        ]
    )

    await resolve_concept_hub(
        session, surface_name="Arterial calcification", kind="Phenomenon", embedding=[0.1, 0.2]
    )

    assert not any("db.index.vector.queryNodes" in query for query, _ in session.calls)


# ---------------------------------------------------------------------------
# resolve_concept_hub — embedding-fallback path is kind-GATED (codex finding)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_embedding_fallback_never_merges_across_different_kind() -> None:
    """Direct regression test for the AC-2 homonym failure mode: a high
    embedding-similarity match of a DIFFERENT kind must never merge, even
    though `dedup.py`-style logic alone (name/embedding similarity) would
    otherwise suggest a merge.
    """
    session = _ScriptedSession(
        [
            ("MATCH (s:Surface {normalized_name: $normalized_name})", []),  # no exact match
            (
                "db.index.vector.queryNodes",
                [
                    {
                        "id": "concept-python-lang",
                        "kind": "TechnicalArtifact",
                        "canonical_name": "Python",
                        "score": 0.99,
                    }
                ],
            ),
        ]
    )

    concept_id = await resolve_concept_hub(
        session,
        surface_name="python",
        kind="Phenomenon",  # different kind than the high-similarity match
        embedding=[0.1] * 8,
    )

    # Must NOT merge into concept-python-lang — a new concept is created instead.
    assert concept_id != "concept-python-lang"


@pytest.mark.asyncio
async def test_embedding_fallback_merges_above_threshold_same_kind() -> None:
    session = _ScriptedSession(
        [
            ("MATCH (s:Surface {normalized_name: $normalized_name})", []),
            (
                "db.index.vector.queryNodes",
                [
                    {
                        "id": "concept-42",
                        "kind": "Phenomenon",
                        "canonical_name": "Liver dysfunction",
                        "score": 0.97,
                    }
                ],
            ),
        ]
    )

    concept_id = await resolve_concept_hub(
        session, surface_name="liver dysfunctions", kind="Phenomenon", embedding=[0.1] * 8
    )

    assert concept_id == "concept-42"


@pytest.mark.asyncio
async def test_allcaps_guard_blocks_merge_even_same_kind_high_similarity() -> None:
    """An ALL_CAPS surface must not merge into a differently-cased candidate
    via the embedding fallback, even at high similarity and matching kind —
    the candidate's normalized name must differ from the query's (otherwise
    the exact-match path would have already fired), so this specifically
    exercises the fallback's allcaps guard.
    """
    session = _ScriptedSession(
        [
            ("MATCH (s:Surface {normalized_name: $normalized_name})", []),
            (
                "db.index.vector.queryNodes",
                [
                    {
                        "id": "concept-42",
                        "kind": "MethodOrConcept",
                        "canonical_name": "pending state",
                        "score": 0.99,
                    }
                ],
            ),
        ]
    )

    concept_id = await resolve_concept_hub(
        session, surface_name="PENDING_STATE", kind="MethodOrConcept", embedding=[0.1] * 8
    )

    assert concept_id != "concept-42"


@pytest.mark.asyncio
async def test_no_match_creates_new_concept() -> None:
    session = _ScriptedSession(
        [
            ("MATCH (s:Surface {normalized_name: $normalized_name})", []),
            ("db.index.vector.queryNodes", []),
        ]
    )

    concept_id = await resolve_concept_hub(
        session, surface_name="Something Novel", kind="DomainOrTopic", embedding=None
    )

    assert concept_id
    create_calls = [q for q, _ in session.calls if "CREATE (c:Concept" in q]
    assert len(create_calls) == 1


# ---------------------------------------------------------------------------
# resolve_concept_hubs_batch — batches the common exact-match case
# (code-review finding: a plain per-concept loop reintroduced the N+1
# pattern the sibling writer functions were rewritten to avoid)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_batch_resolve_all_exact_matches_in_one_round_trip() -> None:
    session = _ScriptedSession(
        [
            (
                "UNWIND $normalized_names AS normalized_name",
                [
                    {"normalized_name": "arterial calcification", "concept_id": "c1"},
                    {"normalized_name": "hypertension", "concept_id": "c2"},
                ],
            ),
        ]
    )

    resolved = await resolve_concept_hubs_batch(
        session,
        surfaces=[
            ("Arterial Calcification", "Phenomenon", None),
            ("Hypertension", "Phenomenon", None),
        ],
    )

    assert resolved == {"Arterial Calcification": "c1", "Hypertension": "c2"}
    # Exactly one round trip for both concepts — the batching regression test.
    assert len(session.calls) == 1


@pytest.mark.asyncio
async def test_batch_resolve_falls_through_per_concept_for_misses() -> None:
    session = _ScriptedSession(
        [
            (
                "UNWIND $normalized_names AS normalized_name",
                [{"normalized_name": "hypertension", "concept_id": "c2"}],  # "novel concept" misses
            ),
            ("db.index.vector.queryNodes", []),
        ]
    )

    resolved = await resolve_concept_hubs_batch(
        session,
        surfaces=[
            ("Hypertension", "Phenomenon", None),
            ("Novel Concept", "DomainOrTopic", None),
        ],
    )

    assert resolved["Hypertension"] == "c2"
    assert resolved["Novel Concept"]  # created fresh
    create_calls = [q for q, _ in session.calls if "CREATE (c:Concept" in q]
    assert len(create_calls) == 1


# ---------------------------------------------------------------------------
# write_episode
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_write_episode_merges_by_id() -> None:
    session = _ScriptedSession([])
    await write_episode(session, episode_id="ep-1", source_session_id="sess-1")
    assert len(session.calls) == 1
    query, params = session.calls[0]
    assert "MERGE (e:Episode {id: $episode_id})" in query
    assert params["episode_id"] == "ep-1"
    assert params["source_session_id"] == "sess-1"


# ---------------------------------------------------------------------------
# write_mentions_and_assertions — batching (codex finding)
# ---------------------------------------------------------------------------


def _membership(category: str, confidence: float = 0.8) -> ProposedMembership:
    return ProposedMembership(
        concept_name="Liver dysfunction",
        kind="Phenomenon",
        category_name=category,
        proposed_confidence=confidence,
    )


def _provenance() -> AssertionProvenance:
    return AssertionProvenance(
        model="test-model",
        prompt_version="fre839-categorizer-v1",
        seed=1,
        when=datetime(2026, 7, 10, tzinfo=timezone.utc),
    )


@pytest.mark.asyncio
async def test_write_mentions_and_assertions_issues_one_round_trip() -> None:
    session = _ScriptedSession(
        [
            (
                "UNWIND $rows AS row",
                [
                    {"concept_id": "c1", "category_normalized_name": "adverse effect"},
                    {"concept_id": "c1", "category_normalized_name": "liver health"},
                ],
            )
        ]
    )
    resolved = [
        ResolvedConceptMemberships(
            concept_id="c1",
            memberships=[_membership("adverse effect"), _membership("liver health")],
        )
    ]

    pairs = await write_mentions_and_assertions(
        session, episode_id="ep-1", resolved=resolved, provenance=_provenance()
    )

    assert (
        len(session.calls) == 1
    )  # exactly one Cypher round-trip regardless of concept/category count
    assert set(pairs) == {("c1", "adverse effect"), ("c1", "liver health")}


@pytest.mark.asyncio
async def test_write_mentions_and_assertions_never_overwrites_prior_episode() -> None:
    """Two different episodes' assertions for the same concept both persist —
    this is asserted at the call-shape level (each call CREATEs a fresh
    MembershipAssertion node, never MERGE/SET on an existing one).
    """
    session = _ScriptedSession([("UNWIND $rows AS row", [])])
    resolved = [
        ResolvedConceptMemberships(concept_id="c1", memberships=[_membership("health issue")])
    ]

    await write_mentions_and_assertions(
        session, episode_id="ep-1", resolved=resolved, provenance=_provenance()
    )
    await write_mentions_and_assertions(
        session, episode_id="ep-2", resolved=resolved, provenance=_provenance()
    )

    assert len(session.calls) == 2
    for query, _ in session.calls:
        assert "CREATE (a:MembershipAssertion" in query
        assert (
            "MATCH (a:MembershipAssertion" not in query
        )  # never re-matches an existing assertion to mutate it


# ---------------------------------------------------------------------------
# recompute_member_of_batch — support_count / mean confidence / batching
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_recompute_member_of_batch_one_round_trip_for_n_pairs() -> None:
    session = _ScriptedSession([("UNWIND $pairs AS pair", [])])

    await recompute_member_of_batch(
        session,
        pairs=[("c1", "adverse effect"), ("c1", "liver health"), ("c2", "health issue")],
    )

    assert len(session.calls) == 1
    _, params = session.calls[0]
    assert len(params["pairs"]) == 3


@pytest.mark.asyncio
async def test_recompute_member_of_batch_query_computes_mean_and_distinct_episode_count() -> None:
    session = _ScriptedSession([("UNWIND $pairs AS pair", [])])

    await recompute_member_of_batch(session, pairs=[("c1", "adverse effect")])

    query = session.calls[0][0]
    assert "avg(a.proposed_confidence)" in query
    assert "count(DISTINCT ep)" in query
    assert "MERGE (c)-[m:MEMBER_OF]->(cat)" in query
