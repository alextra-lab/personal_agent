"""Read the facts and the searches from production ES, read only (ADR-0158 D5.1, D5.2).

**Read only.** ``ReadOnlyES`` can send one request shape: ``POST <es>/<index>/_search``. It holds
no other method, so the harness cannot index, update or delete (FRE-375).

**Helper attribution.** ``web_search_started`` carries ``trace_id`` and ``span_id`` but no task
id. A helper capture holds its end ``timestamp`` and ``duration_ms``. Measured 2026-10-21 over the
124 web-using helpers with ``rounds``: each helper's window held exactly as many ``web_search``
calls as its rounds, and no call fell in two windows. So a call belongs to the helper whose window
holds it. A helper whose count differs from its rounds has its searches excluded and counted.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta

import httpx
from scripts.eval.fre1641_anchor_retention.anchors import extract_anchors
from scripts.eval.fre1641_anchor_retention.record import as_list

CAPTURES_INDEX = "agent-captains-captures-subagents-*"
LOGS_INDEX = "agent-logs-*"
WEB_TOOLS = frozenset({"web_search", "fetch_url"})
#: ``web_search_started`` logs ``query[:120]`` (``tools/web.py``), so a 120-character query may be
#: cut and is excluded (D5.2).
LOGGED_QUERY_CUT = 120
_PAGE = 10_000


class ReadOnlyES:
    """A client that can only search production ES."""

    def __init__(self, base_url: str, client: httpx.Client) -> None:
        """Create the client.

        Args:
            base_url: The ES base URL.
            client: The HTTP client.
        """
        self._base = base_url.rstrip("/")
        self._client = client

    def search(self, index: str, body: Mapping[str, object]) -> list[dict[str, object]]:
        """Run one ``_search`` and return every hit's ``_source``.

        Args:
            index: The index pattern.
            body: The search body. Its ``size`` must exceed the match count.

        Returns:
            The hit sources.

        Raises:
            ValueError: If the index name holds a path, or the hits fill ``size`` (the result
                may be truncated).
        """
        if "/" in index or index.startswith("_"):
            raise ValueError(f"not an index pattern: {index!r}")
        size = int(str(body.get("size", 10)))
        r = self._client.post(f"{self._base}/{index}/_search", json=dict(body), timeout=60.0)
        r.raise_for_status()
        hits = r.json()["hits"]
        total = int(hits["total"]["value"])
        if total >= size or hits["total"].get("relation") != "eq":
            raise ValueError(f"{index}: {total} hits fill size {size}; raise size")
        return [h["_source"] for h in hits["hits"]]


@dataclass(frozen=True)
class Helper:
    """One web-using helper with findings."""

    task_id: str
    trace_id: str
    thoroughness: str | None
    klass: str
    spec_task: str
    window_start: str
    window_end: str
    search_rounds: int
    searches_attributed: int


@dataclass(frozen=True)
class Finding:
    """One stored finding with a ``source_url``."""

    fid: str
    task_id: str
    trace_id: str
    source_url: str
    claim: str
    anchors: list[list[str]]


@dataclass(frozen=True)
class Replay:
    """One distinct search to replay."""

    replay_id: str
    query: str
    categories: str
    engines: str | None
    time_range: str | None


def task_class(thoroughness: str | None) -> str:
    """Return the D6.3 class of a helper: ``quick``, ``standard_thorough`` or ``unknown``."""
    if thoroughness == "quick":
        return "quick"
    if thoroughness in ("standard", "thorough"):
        return "standard_thorough"
    return "unknown"


def _ts(value: object) -> datetime:
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def _findings(full_output: object) -> list[dict[str, object]]:
    try:
        report = json.loads(str(full_output))
    except ValueError:
        return []
    found = report.get("findings") if isinstance(report, dict) else None
    return [f for f in found or [] if isinstance(f, dict) and f.get("source_url")]


def read_facts(es: ReadOnlyES) -> dict[str, object]:
    """Read helpers, findings and attributed searches.

    Args:
        es: The read-only ES client.

    Returns:
        The facts document: ``helpers``, ``findings``, ``replays``, ``searches`` and ``counts``.
    """
    captures = es.search(
        CAPTURES_INDEX,
        {
            "size": _PAGE,
            "query": {"exists": {"field": "full_output"}},
            "_source": [
                "trace_id",
                "task_id",
                "timestamp",
                "duration_ms",
                "spec_task",
                "thoroughness",
                "tools_used",
                "rounds",
                "full_output",
            ],
        },
    )
    raw_helpers = [
        c
        for c in captures
        if WEB_TOOLS & {str(t) for t in as_list(c.get("tools_used"))}
        and _findings(c.get("full_output"))
    ]
    traces = sorted({str(c["trace_id"]) for c in raw_helpers})

    def events(event_type: str, extra: Sequence[Mapping[str, object]]) -> list[dict[str, object]]:
        return es.search(
            LOGS_INDEX,
            {
                "size": _PAGE,
                "query": {
                    "bool": {
                        "filter": [
                            {"terms": {"trace_id": traces}},
                            {"term": {"event_type": event_type}},
                            *extra,
                        ]
                    }
                },
            },
        )

    calls = events("tool_call_started", [{"term": {"tool_name": "web_search"}}])
    started = {str(e["span_id"]): e for e in events("web_search_started", [])}

    helpers: list[Helper] = []
    findings: list[Finding] = []
    replays: dict[tuple[str, str, str | None, str | None], Replay] = {}
    searches: list[dict[str, str]] = []
    counts = {"helpers_attribution_mismatch": 0, "searches_cut_at_120": 0, "searches_no_start": 0}
    for c in raw_helpers:
        task_id, trace_id = str(c["task_id"]), str(c["trace_id"])
        end = _ts(c["timestamp"])
        start = end - timedelta(milliseconds=float(str(c.get("duration_ms") or 0)))
        n_rounds = sum(
            1
            for r in as_list(c.get("rounds"))
            if isinstance(r, dict) and r.get("tool") == "web_search"
        )
        mine = [
            e for e in calls if e["trace_id"] == trace_id and start <= _ts(e["@timestamp"]) <= end
        ]
        thoroughness = c.get("thoroughness")
        helpers.append(
            Helper(
                task_id=task_id,
                trace_id=trace_id,
                thoroughness=str(thoroughness) if thoroughness else None,
                klass=task_class(str(thoroughness) if thoroughness else None),
                spec_task=str(c.get("spec_task") or ""),
                window_start=start.isoformat(),
                window_end=end.isoformat(),
                search_rounds=n_rounds,
                searches_attributed=len(mine),
            )
        )
        for i, f in enumerate(_findings(c.get("full_output"))):
            findings.append(
                Finding(
                    fid=f"{task_id}#{i}",
                    task_id=task_id,
                    trace_id=trace_id,
                    source_url=str(f["source_url"]),
                    claim=str(f.get("claim") or ""),
                    anchors=[[a.kind, a.value] for a in extract_anchors(str(f.get("claim") or ""))],
                )
            )
        if len(mine) != n_rounds:
            counts["helpers_attribution_mismatch"] += 1
            continue
        for e in mine:
            s = started.get(str(e["span_id"]))
            if s is None:
                counts["searches_no_start"] += 1
                continue
            query = str(s.get("query") or "")
            if len(query) >= LOGGED_QUERY_CUT:
                counts["searches_cut_at_120"] += 1
                continue
            key = (
                query,
                str(s.get("categories") or "general"),
                str(s["engines"]) if s.get("engines") else None,
                str(s["time_range"]) if s.get("time_range") else None,
            )
            if key not in replays:
                replays[key] = Replay(f"q{len(replays):04d}", *key)
            searches.append(
                {
                    "span_id": str(e["span_id"]),
                    "task_id": task_id,
                    "replay_id": replays[key].replay_id,
                }
            )

    counts.update(
        helpers=len(helpers),
        findings=len(findings),
        findings_with_anchors=sum(1 for f in findings if f.anchors),
        distinct_urls=len({f.source_url for f in findings if f.anchors}),
        traces=len(traces),
        searches_attributed=len(searches),
        replays=len(replays),
    )
    return {
        "helpers": [asdict(h) for h in helpers],
        "findings": [asdict(f) for f in findings],
        "replays": [asdict(r) for r in replays.values()],
        "searches": searches,
        "counts": counts,
    }
