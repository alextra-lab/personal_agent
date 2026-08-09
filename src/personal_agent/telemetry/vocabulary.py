"""Governed telemetry vocabulary for the ``agent-logs`` write path (ADR-0133).

This module is data plus one pure validating function. It generates nothing
and has no side effects — it is read only by
:func:`personal_agent.telemetry.es_logger.ElasticsearchLogger.log_event`,
which calls :func:`validate_document` on the assembled document immediately
before the write (ADR-0133 D2).

**What is, and is not, seeded here.** ADR-0133's Context measured 59 field
names that cross more than one log-record family, via an ``ast-grep`` census
run during ADR authoring with no committed data file. Reproducing that exact
census is ADR-0133 AC-7's job — a post-implementation, corpus-derived check
assigned to the seam ticket (FRE-1176, parked, due 2026-10-15), not this
module's. What *is* seeded here, per ADR-0133 D3, is unambiguous: the five
recorded naming divergences Rule 1 exists to catch, and the spine fields
already load-bearing in this codebase's own write path (``@timestamp``,
``event_type``, ``trace_id``, ``span_id``, ``session_id``, ``component_id``,
``user_id``, ``input_tokens``, ``output_tokens``). Growing this vocabulary
toward full corpus coverage is expected and does not require touching
:func:`validate_document`.

Three rules, checked in priority order:

1. **Retired spelling** (exact match) — a document key in
   :data:`RETIRED_SPELLINGS` fails, naming the canonical replacement when one
   exists.
2. **Near miss** — a document key that is not itself a governed name, is not
   an exact retired spelling, is not on the closed :data:`NEAR_MISS_EXCEPTIONS`
   list, and scores at or above :data:`NEAR_MISS_THRESHOLD`
   (``difflib.SequenceMatcher``) against some governed name, fails.
3. **Declared type** — a document key that is a governed name in
   :data:`DECLARED_TYPES` whose value is not an instance of the declared type
   fails.

An unrecognised key that trips none of the three passes. There is no
presence obligation and no exclusivity (ADR-0133 D3).

**Production never drops (ADR-0133 D4, FRE-1178).** :func:`validate_document`
raises outside production — under test and in CI, which is where the
development-time guarantee lives. In production it never raises, drops or
mutates the record: the violation is only counted, and the caller always
proceeds to index the document unchanged. Both counters —
:data:`VocabularyCounts.validated` and :data:`VocabularyCounts.violations` —
are incremented here, inside the function that ran the rules, immediately
after they ran, never by the caller. A record on which rule evaluation
itself fails (an exception other than :class:`VocabularyViolationError`)
increments neither counter and propagates unchanged: the rules never ran on
it, so it must not present as coverage.

**Scope: this module never sanitises, but it does not disable sanitising
either.** "Never drop, reject or mutate" describes what *this validator*
does to the document. ``ElasticsearchLogger._index_agent_log`` still runs
every document — governed or not, violating or not — through
``redact_mapping`` (FRE-1068) before the write, a separate, pre-existing
security control that can rewrite a secret-*shaped* value. That is
orthogonal to and unaffected by this module: a governed field carrying an
ordinary value is stored byte-identical; the redaction guarantee for
secret-shaped values is unchanged either way.
"""

from __future__ import annotations

import threading
from collections.abc import Mapping
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Any

from personal_agent.config import Environment, settings
from personal_agent.exceptions import VocabularyViolationError

#: Retired spelling -> canonical replacement, or ``None`` when the spelling is
#: retired outright with no canonical log field (intrinsic span duration —
#: ``duration_ms`` / ``latency_ms`` moved to spans under ADR-0129 and were
#: never a log field to begin with).
RETIRED_SPELLINGS: dict[str, str | None] = {
    "duration_ms": None,
    "latency_ms": None,
    "prompt_tokens": "input_tokens",
    "completion_tokens": "output_tokens",
    "ts": "@timestamp",
    "timestamp": "@timestamp",
    "started_at": "@timestamp",
    "probed_at": "@timestamp",
    "rated_at": "@timestamp",
    "event": "event_type",
    "event.name": "event_type",
}

#: Governed name -> its declared type. Checked by Rule 3 only for governed
#: names that carry an entry here; a governed name may exist purely as a
#: Rule 1 canonical target with no declared type.
DECLARED_TYPES: dict[str, type[object]] = {
    "@timestamp": str,
    "event_type": str,
    "trace_id": str,
    "span_id": str,
    "parent_span_id": str,
    "session_id": str,
    "component_id": str,
    "user_id": str,
    "input_tokens": int,
    "output_tokens": int,
    "total_tokens": int,
    "role": str,
    "provider": str,
    "model": str,
    "endpoint": str,
    "prompt_callsite": str,
    "prompt_component_ids": list,
    "prompt_static_prefix_hash": str,
    "prompt_dynamic_hash": str,
}

#: Governed names — the canonical sides of :data:`RETIRED_SPELLINGS` plus
#: everything in :data:`DECLARED_TYPES`. This is Rule 2's comparison set: a
#: document key equal to one of these is never itself a near-miss.
GOVERNED_NAMES: frozenset[str] = frozenset(
    {name for name in RETIRED_SPELLINGS.values() if name is not None} | set(DECLARED_TYPES)
)

#: Similarity threshold for Rule 2, per ADR-0133 D3. Not a free parameter —
#: the ADR's Context table measures the cost curve and fixes this value.
NEAR_MISS_THRESHOLD = 0.85


@dataclass(frozen=True)
class FieldExclusion:
    """One entry in the field exclusion list (ADR-0133 D7).

    Attributes:
        reason: Why this field is not governed in the log path. Must be
            evidence-backed; "moved to spans" without verifying the field
            no longer appears on logs is not a valid reason (FRE-1179).
    """

    reason: str


#: Fields that are deliberately NOT governed in the log path. ADR-0133 D7:
#: "An exclusion without a stated reason is a defect, not a configuration."
#: Empty after FRE-1179: all eight model-call fields initially listed here
#: were verified still present in recent log records and moved to DECLARED_TYPES.
FIELD_EXCLUSIONS: dict[str, FieldExclusion] = {}


@dataclass(frozen=True)
class NearMissException:
    """One entry on Rule 2's closed exception list.

    ADR-0133 D3: "An exception without a stated reason is a defect, not a
    configuration."

    Attributes:
        matched_governed_name: The governed name this key scores a near-miss
            against.
        similarity: The measured ``difflib.SequenceMatcher`` ratio.
        reason: Why this key is a legitimate distinct field, not a typo.
    """

    matched_governed_name: str
    similarity: float
    reason: str


#: Closed exception list for Rule 2, keyed by the excepted document key.
NEAR_MISS_EXCEPTIONS: dict[str, NearMissException] = {
    "component": NearMissException(
        matched_governed_name="component_id",
        similarity=0.857,
        reason=(
            "component and component_id are a real name-vs-id pair, not a "
            "typo of one another — component is written on every es_handler "
            "record (es_handler.py _build_item) and must keep passing"
        ),
    ),
    "mode": NearMissException(
        matched_governed_name="model",
        similarity=0.889,
        reason=(
            "mode and model are distinct fields: mode is a feature flag or "
            "execution mode, model is the LLM identifier. Both live in logs; "
            "80 docs/day carry mode (FRE-1179 Master review 2026-08-09)"
        ),
    ),
    "roles": NearMissException(
        matched_governed_name="role",
        similarity=0.889,
        reason=(
            "roles and role are distinct fields: roles is a list or plural "
            "collection, role is the singular LLM call role. Both live in logs; "
            "44 docs/day carry roles (FRE-1179 Master review 2026-08-09)"
        ),
    ),
}


@dataclass(frozen=True)
class VocabularyCounts:
    """Point-in-time snapshot of the production-mode counters (ADR-0133 D4).

    Read by the joinability monitor (``observability/joinability/``) when it
    builds its published health document — this is the sole source of both
    numbers (FRE-1178).

    Attributes:
        validated: Records the rules actually ran against, since process
            start or the last :func:`reset_counts`. The invocation witness:
            compared against documents actually indexed, it separates
            "nothing was wrong" from "nothing was checked."
        violations: Of ``validated``, how many carried a violation.

    In-memory and process-lifetime, not durable across a restart — a
    deliberate simplification per this ticket's own scope ("Both numbers
    ride the existing joinability monitor... No new monitor, no new
    schedule"). Reconciling exactly across restarts (ADR-0133 AC-2/AC-5) is
    the seam ticket's question, not this one's.
    """

    validated: int = 0
    violations: int = 0


_counts_lock = threading.Lock()
_counts: dict[str, int] = {"validated": 0, "violations": 0}


def snapshot_counts() -> VocabularyCounts:
    """Return the current validated/violation counts.

    Returns:
        A thread-safe, point-in-time snapshot.
    """
    with _counts_lock:
        return VocabularyCounts(validated=_counts["validated"], violations=_counts["violations"])


def reset_counts() -> None:
    """Reset both counters to zero.

    Test-only. Production never calls this: the counters are process-lifetime
    cumulative by design, and a live reset would corrupt the joinability
    monitor's denominator.
    """
    with _counts_lock:
        _counts["validated"] = 0
        _counts["violations"] = 0


def validate_document(doc: Mapping[str, Any]) -> None:
    """Check an assembled ``agent-logs`` document against the governed vocabulary.

    Applies :func:`_check_rules`, then increments :func:`snapshot_counts`'s
    counters, then — outside production only — re-raises. Call this on the
    fully assembled document, after every field a write path merges in and
    before the document is indexed.

    Args:
        doc: The assembled document, as it will be indexed.

    Raises:
        VocabularyViolationError: Outside production, when the document
            carries a declared retired spelling, a near-miss of a governed
            name outside the exception list, or a governed name whose value
            does not match its declared type. In production the same
            violation is counted instead (ADR-0133 D4, FRE-1178).
    """
    try:
        _check_rules(doc)
    except VocabularyViolationError:
        with _counts_lock:
            _counts["validated"] += 1
            _counts["violations"] += 1
        if settings.environment == Environment.PRODUCTION:
            return
        raise
    with _counts_lock:
        _counts["validated"] += 1


def _check_rules(doc: Mapping[str, Any]) -> None:
    """Apply Rule 1, then Rule 2, then Rule 3, raising on the first violation found.

    Args:
        doc: The assembled document, as it will be indexed.

    Raises:
        VocabularyViolationError: The document carries a declared retired
            spelling, a near-miss of a governed name outside the exception
            list, or a governed name whose value does not match its declared
            type.
    """
    for key in doc:
        if key in RETIRED_SPELLINGS:
            canonical = RETIRED_SPELLINGS[key]
            detail = f"; use '{canonical}' instead" if canonical is not None else ""
            raise VocabularyViolationError(
                f"'{key}' is a retired spelling{detail}",
                field=key,
                rule="retired_spelling",
            )

    for key in doc:
        if key in GOVERNED_NAMES:
            continue
        exception = NEAR_MISS_EXCEPTIONS.get(key)
        for governed in GOVERNED_NAMES:
            if exception is not None and governed == exception.matched_governed_name:
                continue
            if SequenceMatcher(None, key, governed).ratio() >= NEAR_MISS_THRESHOLD:
                raise VocabularyViolationError(
                    f"'{key}' is a near-miss of governed name '{governed}'",
                    field=key,
                    rule="near_miss",
                )

    for key, declared_type in DECLARED_TYPES.items():
        if key not in doc:
            continue
        value = doc[key]
        if value is None:
            # trace_id/span_id are legitimately absent when no trace context
            # exists (es_logger.log_event passes None, not a sentinel) — a
            # governed field with no value is not a type violation.
            continue
        # bool is a subclass of int: isinstance(True, int) is True, so an
        # int-declared field would otherwise silently accept a bool.
        wrong_type = not isinstance(value, declared_type) or (
            declared_type is int and isinstance(value, bool)
        )
        if wrong_type:
            raise VocabularyViolationError(
                f"'{key}' must be {declared_type.__name__}, got {type(value).__name__}",
                field=key,
                rule="declared_type",
            )
