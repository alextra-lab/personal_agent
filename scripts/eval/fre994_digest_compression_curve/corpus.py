"""Sampling frame for the compression curve (FRE-994 §3).

Captures are read from **Elasticsearch**, never from the local telemetry directory.
That directory is nearly empty on the live host — the defect FRE-992 covers — so a
curve sourced from it would be measured over almost no data while looking perfectly
healthy. The per-session read goes through FRE-992's union reader rather than a
second bespoke query, so this study and the producer agree about what a session's
captures are.
"""

from __future__ import annotations

import random
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from personal_agent.captains_log.capture import (
    CAPTURES_INDEX_PREFIX,
    SUBAGENT_CAPTURES_INDEX_PREFIX,
    SessionCaptureRead,
    load_session_captures,
)

#: Real sessions carry a UUID. Everything else in the index is eval residue —
#: `test-session`, `test-reasoning`, and ten siblings, 637 documents between them.
#: They are not conversations and would calibrate the bound against synthetic text.
_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)

#: Mirrors ``session_summary.MIN_TURNS_FOR_DIGEST``. Imported as a literal rather than
#: from the producer module so this file cannot pull the producer's call path into the
#: harness (AC-5); the test suite pins the two together.
MIN_TURNS = 2


@dataclass(frozen=True)
class SessionRef:
    """One eligible session, before its captures are read.

    Attributes:
        session_id: The session's UUID.
        turn_count: Captures in the durable index — the sampling statistic, not a
            claim about the session's true length (the graph owns that count).
        conversation_chars: Total user + assistant characters, the size axis the
            sample is stratified over and the denominator of the relative bound.
        started_at: First capture timestamp.
        ended_at: Last capture timestamp.
        quartile: 1-4 over ``conversation_chars`` across the eligible frame.
    """

    session_id: str
    turn_count: int
    conversation_chars: float
    started_at: datetime
    ended_at: datetime
    quartile: int = 0


@dataclass(frozen=True)
class Sample:
    """The two disjoint draws, made once and recorded in the manifest."""

    fit: tuple[SessionRef, ...]
    holdout: tuple[SessionRef, ...]
    seed: int


CAPTURES_INDEX = f"{CAPTURES_INDEX_PREFIX}-*,-{SUBAGENT_CAPTURES_INDEX_PREFIX}-*"


def frame_query() -> dict[str, Any]:
    """The aggregation that enumerates candidate sessions with their size.

    Returns:
        An Elasticsearch search body: one bucket per session carrying its capture
        count, its conversation size in characters, and its span.
    """
    return {
        "size": 0,
        # `user_id` is required by TaskCapture, and 1,169 of 2,787 documents — every
        # one of them in an April 2026 `-v2` index — do not carry it. Those documents
        # cannot be parsed, so a session drawn from that era arrives with part of its
        # transcript missing while still looking like a complete read. Excluding them
        # at the frame is what stops the curve attributing the reader's loss to the
        # bound: a digest cannot omit a conclusion its input never contained.
        "query": {"bool": {"filter": [{"exists": {"field": "user_id"}}]}},
        "aggs": {
            "by_session": {
                "terms": {"field": "session_id", "size": 5000},
                "aggs": {
                    "chars": {
                        "sum": {
                            "script": {
                                "source": (
                                    "(params._source.user_message==null?0:"
                                    "params._source.user_message.length()) + "
                                    "(params._source.assistant_response==null?0:"
                                    "params._source.assistant_response.length())"
                                )
                            }
                        }
                    },
                    "first": {"min": {"field": "timestamp"}},
                    "last": {"max": {"field": "timestamp"}},
                },
            }
        },
    }


def _parse_ts(agg: object) -> datetime:
    if isinstance(agg, dict):
        raw = agg.get("value_as_string")
        if isinstance(raw, str):
            return datetime.fromisoformat(raw.replace("Z", "+00:00"))
        millis = agg.get("value")
        if isinstance(millis, (int, float)):
            return datetime.fromtimestamp(millis / 1000.0, tz=timezone.utc)
    raise ValueError(f"unparsable timestamp aggregation: {agg!r}")


def eligible_sessions(buckets: list[dict[str, Any]]) -> list[SessionRef]:
    """Reduce the frame aggregation to the sessions this study may draw from.

    Two exclusions, both load-bearing: synthetic ``test-*`` ids are eval residue
    rather than conversations, and a single-turn session is below the producer's own
    floor — it would never be digested in production, so calibrating against it
    would size the bound on material the bound never sees.

    Args:
        buckets: ``aggregations.by_session.buckets`` from :func:`frame_query`.

    Returns:
        Eligible sessions, each stamped with its size quartile, ordered by size.
    """
    refs = [
        SessionRef(
            session_id=str(b["key"]),
            turn_count=int(b["doc_count"]),
            conversation_chars=float(b["chars"]["value"]),
            started_at=_parse_ts(b.get("first")),
            ended_at=_parse_ts(b.get("last")),
        )
        for b in buckets
        if _UUID.match(str(b["key"])) and int(b["doc_count"]) >= MIN_TURNS
    ]
    refs.sort(key=lambda r: r.conversation_chars)

    n = len(refs)
    return [
        SessionRef(
            session_id=r.session_id,
            turn_count=r.turn_count,
            conversation_chars=r.conversation_chars,
            started_at=r.started_at,
            ended_at=r.ended_at,
            quartile=min(4, (i * 4) // n + 1) if n else 0,
        )
        for i, r in enumerate(refs)
    ]


def draw_sample(eligible: list[SessionRef], *, fit_n: int, holdout_n: int, seed: int) -> Sample:
    """Draw disjoint fit and held-out samples, stratified by size quartile.

    Stratification is what makes the absolute-versus-relative question answerable:
    a sample bunched at the median cannot separate a constant bound from one that
    scales with the material. The held-out draw is disjoint because a rule confirmed
    on the data it was fitted to has not been confirmed at all.

    Args:
        eligible: Output of :func:`eligible_sessions`.
        fit_n: Sessions in the fit sample.
        holdout_n: Sessions in the held-out sample.
        seed: Recorded in the manifest; the run must be reproducible from it.

    Returns:
        The two draws.

    Raises:
        ValueError: If the frame cannot supply the requested sizes. Refused rather
            than quietly shrunk — a run that silently returns fewer sessions reports
            a sample size it never measured.
    """
    total = fit_n + holdout_n
    if total > len(eligible):
        raise ValueError(
            f"requested {total} sessions but only {len(eligible)} eligible in the frame"
        )

    rng = random.Random(seed)
    by_quartile: dict[int, list[SessionRef]] = {q: [] for q in (1, 2, 3, 4)}
    for ref in eligible:
        by_quartile[ref.quartile].append(ref)
    for refs in by_quartile.values():
        rng.shuffle(refs)

    drawn: list[SessionRef] = []
    cursor = {q: 0 for q in (1, 2, 3, 4)}
    # Round-robin across quartiles so any shortfall in one stratum is spread rather
    # than silently emptying the largest-session stratum, which is the one the
    # relative-bound fit depends on most.
    while len(drawn) < total:
        progressed = False
        for q in (1, 2, 3, 4):
            if len(drawn) == total:
                break
            if cursor[q] < len(by_quartile[q]):
                drawn.append(by_quartile[q][cursor[q]])
                cursor[q] += 1
                progressed = True
        if not progressed:  # pragma: no cover — guarded by the size check above
            break

    return Sample(fit=tuple(drawn[:fit_n]), holdout=tuple(drawn[fit_n:total]), seed=seed)


async def read_captures(ref: SessionRef, *, es_client: Any, trace_id: str) -> SessionCaptureRead:
    """Read one session's captures through FRE-992's union reader.

    Args:
        ref: The session to read.
        es_client: An open ``AsyncElasticsearch``. Never constructed here — a
            background read must not open its own connection to whatever URL happens
            to be configured (FRE-375).
        trace_id: Trace identifier for log correlation (ADR-0074 §I3).

    Returns:
        The captures plus the provenance and trustworthiness of the read.
    """
    return await load_session_captures(
        ref.session_id,
        started_at=ref.started_at,
        ended_at=ref.ended_at,
        es_client=es_client,
        trace_id=trace_id,
    )
