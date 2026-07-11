r"""ADR-0114 D7/D8 baseline harness (FRE-840).

Reproduces production multipath recall (ADR-0104) against the frozen study
sandbox. Arm A of the D9 ablation ladder (ADR-0114:158) — "the real baseline", per
D7/D8: reproduce the ACTUAL production multipath recall behaviour (not an
embedding-only strawman) over the frozen corpus (FRE-838), so the study has
an honest head-to-head comparator. AC-4's scoring rig
(``scripts/study/scoring_rig.py``) consumes this harness's per-cue ranked
output.

**What "production" means here.** ADR-0114 names the comparator only as
"current production multipath recall (ADR-0104 behaviour)" — the v1 arm set
is dense + lexical + multi-query (``MemoryService._multipath_fused_recall``);
the structural arm is a separate, still-flag-dark axis the ADR does not name
as live, so it is left at its code default (``False``) here. Likewise,
``relevance_bounded_recall_enabled`` (ADR-0100) is explicitly pinned off —
the ADR names only the multipath/lexical/multi-query flags as "enabled in
the owner's live config", so this reproduction does not assume ADR-0100 is
also live; a later ticket can revisit this if that assumption changes.

**This module does not touch ``os.environ``.** Pointing ``personal_agent``'s
settings singleton at the study sandbox is the CLI entrypoint's job
(``scripts/study/run_baseline.py``, which applies
``scripts.study.config.study_substrate_env()`` before importing this module)
— importing THIS module has no global side effects, so it is safe to import
directly in unit tests that run inside the shared ``make test`` process
without polluting other tests' env. :func:`connect_baseline_service` instead
asserts the already-resolved ``settings.neo4j_uri`` matches the study
sandbox and fails loud if it does not (the env-pin didn't take, or nothing
pinned it at all).
"""

from __future__ import annotations

from typing import Protocol

import structlog

from personal_agent.config import settings
from personal_agent.memory.protocol import MemoryRecallQuery
from personal_agent.memory.protocol_adapter import MemoryServiceAdapter
from personal_agent.memory.service import MemoryService
from scripts.eval.fre435_memory_recall.scoring import flatten_recall
from scripts.study.config import STUDY_NEO4J_BOLT_PORT

log = structlog.get_logger(__name__)

#: FRE-706 owner-confirmed noise-guard floor (reused verbatim from ab_multipath.py
#: — the same value the owner confirmed for the live deploy config).
MULTIPATH_FLOOR = 0.60

#: The study sandbox's expected bolt URI — the preflight target for
#: :func:`connect_baseline_service`, not a credential (no password needed to
#: compare host:port).
STUDY_NEO4J_URI = f"bolt://localhost:{STUDY_NEO4J_BOLT_PORT}"


def _capitalized_entity_hints(text: str) -> list[str]:
    """Cheap entity-hint extractor — capitalised words longer than 3 chars.

    Mirrors ``request_gateway/context.py:_capitalized_entity_hints`` to keep
    this module free of cross-package imports on a module-private symbol
    (the same duplication convention ``captains_log/recall.py`` already
    follows for this exact heuristic, per its own docstring).
    """
    if not text:
        return []
    words = text.split()
    return [w.strip('",.:;!?') for w in words if len(w) > 3 and w[0].isupper()][:10]


class _MultipathSettings(Protocol):
    """The flag surface :func:`set_prod_multipath_config` mutates."""

    multipath_recall_enabled: bool
    lexical_arm_enabled: bool
    multiquery_arm_enabled: bool
    recall_similarity_floor: float
    relevance_bounded_recall_enabled: bool


class StudyTargetMismatchError(RuntimeError):
    """Raised when the connected settings singleton did not resolve to the study sandbox.

    Catches the case where some earlier import already initialised
    ``personal_agent.config.settings`` before the CLI entrypoint's env-pin
    ran (the cached-singleton failure mode ``ab_multipath.py``'s own module
    docstring warns about), or where the entrypoint's env-pin was skipped
    entirely -- fail loud rather than silently querying prod or the wrong
    substrate.
    """


def set_prod_multipath_config(settings_obj: _MultipathSettings) -> None:
    """Set the flags ADR-0114 names as "enabled in the owner's live config".

    Enables the v1 multipath arm set (dense + lexical + multi-query) at the
    owner-confirmed noise-guard floor, and explicitly pins
    ``relevance_bounded_recall_enabled`` off (codex review: pin, don't rely
    on the field's own default, so a later change to that default can't
    silently confound this reproduction). ``structural_arm_enabled`` is left
    untouched -- the ADR's arm A is dense+lexical+multi-query only.

    Args:
        settings_obj: The settings object to mutate (the live singleton at
            runtime; a fake in unit tests).
    """
    settings_obj.multipath_recall_enabled = True
    settings_obj.lexical_arm_enabled = True
    settings_obj.multiquery_arm_enabled = True
    settings_obj.recall_similarity_floor = MULTIPATH_FLOOR
    settings_obj.relevance_bounded_recall_enabled = False


async def connect_baseline_service() -> MemoryService:
    """Connect a ``MemoryService`` to the study sandbox and prepare it for recall.

    Applies :func:`set_prod_multipath_config`, connects, and ensures the
    vector + fulltext indexes exist (the frozen corpus copies node
    properties verbatim but the study schema only builds the ``Concept``
    vector index -- ``entity_embedding``/``turn_entity_fulltext`` must be
    created here before any arm can return results, matching
    ``ab_multipath.run()``'s own sequence).

    Returns:
        A connected, index-ready ``MemoryService`` pointed at the sandbox.

    Raises:
        StudyTargetMismatchError: If ``settings.neo4j_uri`` does not resolve
            to the study sandbox -- the caller's env-pin did not take (or
            was never applied).
        RuntimeError: If the connection or index setup fails.
    """
    if settings.neo4j_uri != STUDY_NEO4J_URI:
        raise StudyTargetMismatchError(
            f"expected study sandbox {STUDY_NEO4J_URI!r}, "
            f"settings.neo4j_uri resolved to {settings.neo4j_uri!r} -- "
            "run via scripts/study/run_baseline.py, which pins the study "
            "env before personal_agent is imported"
        )

    set_prod_multipath_config(settings)
    service = MemoryService()  # fre-375-allow: study sandbox pinned by the CLI entrypoint (:7691)
    if not await service.connect():
        raise RuntimeError(f"could not connect to study sandbox at {STUDY_NEO4J_URI}")
    if not await service.ensure_vector_index():
        await service.disconnect()
        raise RuntimeError("vector index unavailable on study sandbox")
    if not await service.ensure_fulltext_index():
        await service.disconnect()
        raise RuntimeError("fulltext index unavailable on study sandbox")
    return service


async def run_baseline_recall(
    adapter: MemoryServiceAdapter, cue_text: str, k: int, trace_id: str
) -> tuple[str, ...]:
    """Run one abstract-cue query through the production-multipath recall path.

    Args:
        adapter: A ``MemoryServiceAdapter`` wrapping a study-connected
            ``MemoryService`` (or a test double exposing the same ``recall``
            coroutine).
        cue_text: The abstract cue text.
        k: The recall cut-off (AC-4 uses 20).
        trace_id: Request trace identifier.

    Returns:
        Ordered, namespaced retrieved ids (``entity:``/``episode:``),
        flattened via the same convention ``scoring_rig.score_cue`` expects.
    """
    hints = _capitalized_entity_hints(cue_text)
    # authenticated=True: the frozen corpus's entities all carry FRE-229
    # visibility='group' (every real owner conversation is authenticated),
    # which _build_visibility_filter only admits when the request is
    # authenticated. An unauthenticated query silently sees zero entities on
    # this corpus -- a false floor that would make any comparison against
    # this baseline meaningless (discovered running this harness for real
    # against the sandbox, FRE-840).
    query = MemoryRecallQuery(
        entity_names=list(hints[:5]), query_text=cue_text, limit=k, authenticated=True
    )
    result = await adapter.recall(query, trace_id=trace_id)
    return flatten_recall(result.episodes, result.entities, result.relevance_scores)
