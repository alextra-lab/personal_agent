"""FRE-488 — probe-set schema + loaders for the memory-recall harness.

A probe set is a list of :class:`ProbeCase`. Each case pairs a *history setup*
(how the memory substrate is seeded — either pre-extracted entities for offline
``replay`` or raw turns for real ``extract``) with a *query* and an
*expected recall* label, per ADR-0087 §D2. The harness (``harness.py``) loads a
set, drives it against the test substrate, and scores the D1 metrics.

Two loaders are exposed:

* :func:`load_probe_set` — the bespoke YAML format (the FRE-489 gate set and the
  FRE-488 seed both use this).
* :func:`load_longmemeval` — a stub; the LongMemEval adapter is FRE-490.

The pure metric core (``metrics.py``) is namespace-agnostic; the *labels* here
carry the ``entity:`` namespace so retrieved entity ids and expected ids share a
space and never collide with episode (``turn_id``) ids (codex review, FRE-488).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml  # type: ignore[import-untyped]

#: Namespace prefix for entity-keyed recall labels and retrieved entity ids.
ENTITY_NS = "entity:"
#: Namespace prefix for episode-keyed (``turn_id``) retrieved ids.
EPISODE_NS = "episode:"


class ProbeSetError(ValueError):
    """Raised when a probe set is malformed or degenerate.

    A *degenerate* set is one where no case carries an expected recall label
    (every ``relevant`` set is empty). Such a set can make every retrieval
    metric pass vacuously, so loading it is rejected (codex review, FRE-488).
    """


@dataclass(frozen=True)
class ProbeTurn:
    """One turn of setup history (used by ``--write-mode extract``).

    Attributes:
        user: The user message of the turn.
        assistant: The assistant response of the turn (the text extraction runs on).
    """

    user: str
    assistant: str


@dataclass(frozen=True)
class SeedEntity:
    """A pre-extracted entity to seed directly (used by ``--write-mode replay``).

    Attributes:
        name: Entity name (the recall label is ``entity:<name lowercased>``).
        entity_type: Entity type (defaults to ``concept``).
        description: Description text (first-write-wins on the substrate).
    """

    name: str
    entity_type: str = "concept"
    description: str = ""


@dataclass(frozen=True)
class SeedRelationship:
    """A pre-extracted relationship to seed directly (replay mode).

    Attributes:
        source: Source entity name.
        rel_type: Relationship type.
        target: Target entity name.
    """

    source: str
    rel_type: str
    target: str


@dataclass(frozen=True)
class ExpectedRecall:
    """The labelled expectation for a query.

    Attributes:
        entity_names: Entity names that *should* surface for the query.
        must_not_deny: Whether the system must NOT claim "no prior discussions"
            (the false-negative check — ADR-0087 §D1 headline).
    """

    entity_names: tuple[str, ...] = ()
    must_not_deny: bool = True


@dataclass(frozen=True)
class ProbeCase:
    """One recall probe: ``(history setup, query, expected recall)``.

    Attributes:
        case_id: Stable identifier (tag in the report).
        query: The recall query driven against the retrieval path.
        note: Why this case exposes the behaviour.
        history: Setup turns for ``extract`` mode.
        seed_entities: Pre-extracted entities for ``replay`` mode.
        seed_relationships: Pre-extracted relationships for ``replay`` mode.
        expected: The labelled expected recall.
        tags: Free-form tags (e.g. ``false-negative``, ``pedagogical``).
    """

    case_id: str
    query: str
    note: str = ""
    history: tuple[ProbeTurn, ...] = ()
    seed_entities: tuple[SeedEntity, ...] = ()
    seed_relationships: tuple[SeedRelationship, ...] = ()
    expected: ExpectedRecall = field(default_factory=ExpectedRecall)
    tags: tuple[str, ...] = ()

    @property
    def relevant_ids(self) -> frozenset[str]:
        """Namespaced, normalised set of expected-recall ids for scoring."""
        return frozenset(
            f"{ENTITY_NS}{name.strip().lower()}"
            for name in self.expected.entity_names
            if name.strip()
        )


def _parse_one(raw: dict[str, Any]) -> ProbeCase:
    """Build a :class:`ProbeCase` from a raw YAML mapping.

    Args:
        raw: A single case mapping.

    Returns:
        The parsed case.

    Raises:
        ProbeSetError: If a required field is missing or mistyped.
    """
    if not isinstance(raw, dict):
        raise ProbeSetError(f"Each case must be a mapping, got {type(raw).__name__}")
    try:
        case_id = str(raw["case_id"])
        query = str(raw["query"])
    except KeyError as exc:
        raise ProbeSetError(f"Case missing required field {exc}") from exc

    history = tuple(
        ProbeTurn(user=str(t["user"]), assistant=str(t["assistant"]))
        for t in raw.get("history", [])
    )
    seed_entities = tuple(
        SeedEntity(
            name=str(e["name"]),
            entity_type=str(e.get("entity_type", "concept")),
            description=str(e.get("description", "")),
        )
        for e in raw.get("seed_entities", [])
    )
    seed_relationships = tuple(
        SeedRelationship(
            source=str(r["source"]),
            rel_type=str(r["rel_type"]),
            target=str(r["target"]),
        )
        for r in raw.get("seed_relationships", [])
    )
    raw_expected = raw.get("expected", {}) or {}
    expected = ExpectedRecall(
        entity_names=tuple(str(n) for n in raw_expected.get("entity_names", [])),
        must_not_deny=bool(raw_expected.get("must_not_deny", True)),
    )
    return ProbeCase(
        case_id=case_id,
        query=query,
        note=str(raw.get("note", "")),
        history=history,
        seed_entities=seed_entities,
        seed_relationships=seed_relationships,
        expected=expected,
        tags=tuple(str(t) for t in raw.get("tags", [])),
    )


def parse_probe_cases(raw_cases: Sequence[dict[str, Any]]) -> list[ProbeCase]:
    """Parse and validate a sequence of raw case mappings.

    Args:
        raw_cases: Raw mappings (e.g. from a YAML ``cases`` list).

    Returns:
        Parsed, validated cases.

    Raises:
        ProbeSetError: If the set is empty, a case is malformed, or the set is
            degenerate (no case carries an expected-recall label).
    """
    if not raw_cases:
        raise ProbeSetError("Probe set is empty")
    cases = [_parse_one(c) for c in raw_cases]
    if not any(c.relevant_ids for c in cases):
        raise ProbeSetError(
            "Probe set is degenerate: no case has a non-empty expected recall set. "
            "Retrieval metrics would pass vacuously."
        )
    return cases


def load_probe_set(path: Path) -> list[ProbeCase]:
    """Load a bespoke YAML probe set from disk.

    Args:
        path: Path to a YAML file with a top-level ``cases`` list.

    Returns:
        Parsed, validated cases.

    Raises:
        ProbeSetError: If the file has no ``cases`` list or is degenerate.
    """
    raw = yaml.safe_load(path.read_text())
    cases = raw.get("cases") if isinstance(raw, dict) else None
    if not cases:
        raise ProbeSetError(f"No 'cases' found in {path}")
    return parse_probe_cases(cases)


def load_longmemeval(path: Path) -> list[ProbeCase]:
    """Adapter for the LongMemEval benchmark — NOT yet implemented.

    Args:
        path: Path to a LongMemEval question file.

    Raises:
        NotImplementedError: Always; the adapter is FRE-490.
    """
    raise NotImplementedError(
        "LongMemEval adapter is FRE-490; FRE-488 ships the bespoke YAML loader only."
    )
