#!/usr/bin/env python3
"""Rank the uncitable population by refused tool origin (FRE-1359, ADR-0140 AC-3).

ADR-0139 D8 provisions a typed wrapper for evidence the agent needs to cite, **in the
order the uncitable population ranks them**. ADR-0140 AC-3 makes that ordering an
obligation and fails when a wrapper ships with no population behind it. This script is the
query that criterion is checked against, and it is re-run once per window because AC-3
adjudicates over two consecutive windows.

Everything it applies was preregistered on FRE-1359 before the first window was read.
Re-state, do not re-decide:

* **Window** — fourteen consecutive UTC days, half-open ``[since, until)``. Any other
  span is refused rather than measured, so a window cannot be chosen to suit a result.
* **Unit of analysis — the delivered turn, never the document.** ``_record_grounding``
  runs on every generation attempt and one source registry serves the whole turn, so a
  retried trace writes a second document carrying the first attempt's refused origins.
  Documents are collapsed by ``trace_id``, keeping the highest ``attempts``.
* **Qualifying threshold** — an origin qualifies at **>= 25 %** of the window's
  wrapper-candidate uncitable turns **and** **>= 5** such turns in absolute terms.
* **Wrapper candidate** — a refusal whose admissibility is ``model_authored_invocation``
  or ``unclassified_tool``. The other two shapes are reachable only after a tool passed
  the policy table, so their origin already has a typed tool and is never demand. They are
  reported and never qualify.

Both denominators are printed — all uncitable turns, and wrapper-candidate uncitable turns
— so the choice is visible rather than assumed.

**The residual this cannot resolve.** ADR-0140 states that AC-3 cannot detect the split
inside ``bash`` between transitional retrieval and permanently uncitable command
execution, and ``ARBITRARY_CODE_TOOLS`` also holds generative tools no wrapper replaces.
The reason narrows the noise. It does not close that gap, and the output says so.

Usage::

    python scripts/audit/fre1359_uncitable_population.py --since 2026-09-15 --until 2026-09-29
    python scripts/audit/fre1359_uncitable_population.py --since ... --until ... --json

Exit codes:
    0   the window was measured
    1   the window is not the preregistered fourteen days, or the result cap was reached
    70  Elasticsearch unreachable, so nothing was measured
"""

from __future__ import annotations

import argparse
import collections
import json
import os
import sys
import urllib.error
import urllib.request
from datetime import UTC, datetime
from typing import Any

ES_URL = os.environ.get("AUDIT_ES_URL", "http://localhost:9200")
INDEX_PATTERN = "agent-logs-*"
EVENT = "grounding_verification_completed"

#: Preregistered on FRE-1359 before the first window read. Do not edit to fit a result:
#: ADR-0140 AC-3 fails if the threshold is revised after any window is read.
WINDOW_DAYS = 14
QUALIFYING_SHARE = 0.25
QUALIFYING_ABSOLUTE = 5
WRAPPER_CANDIDATE_REASONS = frozenset({"model_authored_invocation", "unclassified_tool"})

#: A filled result set reads exactly like a complete one, so the cap is an error and never
#: a silent truncation.
MAX_DOCUMENTS = 10000

EXIT_OK = 0
EXIT_INVALID = 1
EXIT_UNREACHABLE = 70


def _parse_day(value: str) -> datetime:
    """Parse one UTC calendar day.

    Args:
        value: A date as ``YYYY-MM-DD``.

    Returns:
        Midnight UTC on that day.

    Raises:
        argparse.ArgumentTypeError: The value is not a valid date.
    """
    try:
        return datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=UTC)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"{value!r} is not a YYYY-MM-DD date") from exc


def fetch_documents(since: datetime, until: datetime) -> list[dict[str, Any]]:
    """Return every verification document in the half-open window.

    Args:
        since: Window start, inclusive.
        until: Window end, exclusive.

    Returns:
        The ``_source`` of each matching document.

    Raises:
        ConnectionError: Elasticsearch did not answer.
        RuntimeError: The result cap was reached, so the window is not fully measured.
    """
    query = {
        "size": MAX_DOCUMENTS,
        "track_total_hits": True,
        "query": {
            "bool": {
                "filter": [
                    {"term": {"event_type": EVENT}},
                    {
                        "range": {
                            "@timestamp": {
                                "gte": since.isoformat(),
                                "lt": until.isoformat(),
                            }
                        }
                    },
                ]
            }
        },
        "_source": [
            "trace_id",
            "attempts",
            "turn_evidence_class",
            "refused_tool_origins",
            "refused_origin_admissibility",
        ],
    }
    request = urllib.request.Request(  # noqa: S310 - fixed local scheme, operator-run audit
        f"{ES_URL}/{INDEX_PATTERN}/_search",
        data=json.dumps(query).encode(),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310
            payload = json.load(response)
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise ConnectionError(f"Elasticsearch unreachable at {ES_URL}: {exc}") from exc

    hits = payload.get("hits", {})
    total = hits.get("total", {}).get("value", 0)
    documents = [hit.get("_source", {}) for hit in hits.get("hits", [])]
    if total > MAX_DOCUMENTS:
        raise RuntimeError(
            f"{total} documents match but the cap is {MAX_DOCUMENTS}; the window would be "
            "measured short. Narrow the window or raise the cap deliberately."
        )
    return documents


def collapse_to_turns(documents: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int]:
    """Collapse per-attempt documents into delivered turns.

    The preregistered unit of analysis: group by ``trace_id`` and keep the document with
    the highest ``attempts``. A document carrying no ``trace_id`` is kept on its own,
    because dropping it would shrink the denominator invisibly.

    Args:
        documents: The raw documents.

    Returns:
        The collapsed turns, and how many traces carried more than one attempt.
    """
    by_trace: dict[str, dict[str, Any]] = {}
    orphans: list[dict[str, Any]] = []
    attempt_counts: collections.Counter[str] = collections.Counter()
    for document in documents:
        trace_id = document.get("trace_id")
        if not isinstance(trace_id, str) or not trace_id:
            orphans.append(document)
            continue
        attempt_counts[trace_id] += 1
        best = by_trace.get(trace_id)
        if best is None or int(document.get("attempts") or 0) >= int(best.get("attempts") or 0):
            by_trace[trace_id] = document
    retried = sum(1 for count in attempt_counts.values() if count > 1)
    return [*by_trace.values(), *orphans], retried


def _reasons_by_origin(turn: dict[str, Any]) -> dict[str, set[str]]:
    """Map each refused origin on one turn to the rules that refused it.

    Args:
        turn: One collapsed turn document.

    Returns:
        Origin to the set of admissibility values recorded for it. An origin present in
        ``refused_tool_origins`` with no pair recorded maps to an empty set, so a
        pre-FRE-1359 document degrades to "unknown reason" instead of vanishing.
    """
    origins = turn.get("refused_tool_origins") or []
    pairs = turn.get("refused_origin_admissibility") or []
    reasons: dict[str, set[str]] = {origin: set() for origin in origins if isinstance(origin, str)}
    for pair in pairs:
        if not isinstance(pair, str) or ":" not in pair:
            continue
        # Split on the LAST colon: an admissibility value never contains one, while a
        # tool name conceivably could. Splitting on the first would silently truncate
        # such an origin and file its turns under a name no tool has.
        origin, _, reason = pair.rpartition(":")
        reasons.setdefault(origin, set()).add(reason)
    return reasons


def measure(turns: list[dict[str, Any]]) -> dict[str, Any]:
    """Produce the AC-3 table for one window.

    Args:
        turns: The collapsed turns.

    Returns:
        The class split, both denominators, and the per-origin ranking.
    """
    classes: collections.Counter[str] = collections.Counter(
        str(turn.get("turn_evidence_class")) for turn in turns
    )
    uncitable = [turn for turn in turns if turn.get("turn_evidence_class") == "uncitable"]
    asserting = classes["uncitable"] + classes["citable"]

    origin_turns: collections.Counter[str] = collections.Counter()
    origin_reasons: dict[str, set[str]] = {}
    candidate_turns = 0
    for turn in uncitable:
        reasons = _reasons_by_origin(turn)
        if any(reason & WRAPPER_CANDIDATE_REASONS for reason in reasons.values()):
            candidate_turns += 1
        for origin, origin_reason in reasons.items():
            origin_turns[origin] += 1
            origin_reasons.setdefault(origin, set()).update(origin_reason)

    ranking = []
    for origin, count in origin_turns.most_common():
        reasons = origin_reasons.get(origin, set())
        is_candidate = bool(reasons & WRAPPER_CANDIDATE_REASONS)
        share = count / candidate_turns if candidate_turns else 0.0
        ranking.append(
            {
                "origin": origin,
                "uncitable_turns": count,
                "reasons": sorted(reasons) or ["unknown"],
                "wrapper_candidate": is_candidate,
                "share_of_candidate_turns": round(share, 4),
                "share_of_uncitable_turns": round(count / len(uncitable), 4) if uncitable else 0.0,
                "qualifies": bool(
                    is_candidate and share >= QUALIFYING_SHARE and count >= QUALIFYING_ABSOLUTE
                ),
            }
        )

    return {
        "turns": len(turns),
        "turn_evidence_class": dict(classes),
        "asserting_turns": asserting,
        "uncitable_turns": len(uncitable),
        "uncitable_share_of_asserting": round(len(uncitable) / asserting, 4) if asserting else 0.0,
        "wrapper_candidate_uncitable_turns": candidate_turns,
        "ranking": ranking,
        "adjudicable": candidate_turns > 0,
    }


def render(result: dict[str, Any], retried: int) -> str:
    """Render the window as markdown for the ticket comment.

    Args:
        result: What :func:`measure` returned.
        retried: How many traces carried more than one attempt.

    Returns:
        The report.
    """
    lines = [
        f"Turns: {result['turns']} (traces with more than one attempt, collapsed: {retried})",
        f"Class split: {result['turn_evidence_class']}",
        f"Asserting turns: {result['asserting_turns']}",
        f"Uncitable share of asserting turns: {result['uncitable_share_of_asserting']:.1%}",
        f"Wrapper-candidate uncitable turns: {result['wrapper_candidate_uncitable_turns']}",
        "",
        "| origin | uncitable turns | reasons | candidate | share of candidate | qualifies |",
        "|---|---:|---|---|---:|---|",
    ]
    for row in result["ranking"]:
        lines.append(
            f"| {row['origin']} | {row['uncitable_turns']} | {', '.join(row['reasons'])} "
            f"| {'yes' if row['wrapper_candidate'] else 'no'} "
            f"| {row['share_of_candidate_turns']:.1%} "
            f"| {'YES' if row['qualifies'] else 'no'} |"
        )
    if not result["adjudicable"]:
        lines += [
            "",
            "No wrapper-candidate uncitable turn in this window. ADR-0140 AC-3 is "
            "NOT YET ADJUDICABLE for it — that is not the same as met.",
        ]
    lines += [
        "",
        "Residual ADR-0140 already declares: this ranking cannot split bash between "
        "transitional retrieval and permanently uncitable command execution, and the "
        "arbitrary-code set also holds generative tools no wrapper replaces.",
    ]
    return "\n".join(lines)


def main() -> int:
    """Measure one preregistered window.

    Returns:
        The process exit code.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--since", required=True, type=_parse_day, help="window start (UTC day)")
    parser.add_argument("--until", required=True, type=_parse_day, help="window end, exclusive")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    args = parser.parse_args()

    span = (args.until - args.since).days
    if span != WINDOW_DAYS:
        sys.stderr.write(
            f"window is {span} days; FRE-1359 preregistered exactly {WINDOW_DAYS}. Refusing "
            "to measure a window the preregistration does not describe.\n"
        )
        return EXIT_INVALID

    try:
        documents = fetch_documents(args.since, args.until)
    except ConnectionError as exc:
        sys.stderr.write(f"{exc}\n")
        return EXIT_UNREACHABLE
    except RuntimeError as exc:
        sys.stderr.write(f"{exc}\n")
        return EXIT_INVALID

    turns, retried = collapse_to_turns(documents)
    result = measure(turns)
    result["window"] = {"since": args.since.isoformat(), "until": args.until.isoformat()}
    result["documents"] = len(documents)
    sys.stdout.write(
        json.dumps(result, indent=2) if args.json else render(result, retried),
    )
    sys.stdout.write("\n")
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
