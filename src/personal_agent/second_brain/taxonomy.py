"""Canonical ADR-0109 entity-*type* taxonomy — the single source of truth (FRE-772).

The entity-``type`` vocabulary was never enforced as a code enum (ADR-0109 § Provenance);
it lived only as prose in the extractor prompt and as free-form strings on ``:Entity`` nodes.
This module makes the V1→V2 vocabularies and their remap explicit so the FRE-772 KG migration,
the FRE-793 recall-consumer remap, and any future consumer share one definition and cannot
silently diverge (a drift guard in ``tests/personal_agent/second_brain/test_taxonomy.py``
asserts the live extractor prompt still speaks exactly ``V2_ENTITY_TYPES``).

The knowledge-*class* axis (World/Personal/System) is orthogonal to entity *type* (ADR-0097/0098)
and is deliberately **not** modelled here.
"""

from __future__ import annotations

from typing import Final

# V1 — the inherited 7-type vocabulary (ADR-0109 § Provenance). Live in production until the V2 cutover.
V1_ENTITY_TYPES: Final[frozenset[str]] = frozenset(
    {"Person", "Organization", "Location", "Technology", "Concept", "Event", "Topic"}
)

# V2 — the 10-type vocabulary (ADR-0109 § Decision, 8 types + Amendment 1's KnowledgeArtifact/QuantityMeasure).
V2_ENTITY_TYPES: Final[frozenset[str]] = frozenset(
    {
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
    }
)

# Deterministic V1→V2 remap: exactly the V1 types whose STRING changes under V2. ``Person``,
# ``Organization``, ``Location`` and ``Event`` are valid verbatim in both taxonomies (they are the
# intersection ``V1 ∩ V2``) so they need no write; ``Concept`` is not deterministic — it needs a
# model re-classification into one of :data:`V1_CONCEPT_TARGET_TYPES`.
V1_TO_V2_DETERMINISTIC: Final[dict[str, str]] = {
    "Technology": "TechnicalArtifact",
    "Topic": "DomainOrTopic",
}

# The V1 ``Concept`` type ("an abstract idea, methodology, or domain principle") re-classifies via the
# model into exactly one of these five conceptual V2 types (owner decision 2026-07-05). The set is the
# 3-way ADR step-5 target widened by Amendment 1's two additions, which exist precisely to home entities
# stored today as ``Concept`` that the 3-way set could not (e.g. ``wavelength`` → QuantityMeasure, an
# authored paper → KnowledgeArtifact).
V1_CONCEPT_TARGET_TYPES: Final[frozenset[str]] = frozenset(
    {"MethodOrConcept", "DomainOrTopic", "Phenomenon", "QuantityMeasure", "KnowledgeArtifact"}
)

# The V1-only types retired by V2 — the ones a migration must leave no remnant of (ADR-0109 AC-4).
V1_RETIRED_TYPES: Final[frozenset[str]] = V1_ENTITY_TYPES - V2_ENTITY_TYPES
