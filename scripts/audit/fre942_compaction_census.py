#!/usr/bin/env python3
"""FRE-942 — compaction-outcome and tool-result size census.

Read-only audit tool backing the FRE-942 decision (see
``docs/superpowers/plans/2026-07-23-fre-942-compaction-tail-ceiling.md`` §1). It
reproduces the two measurements the decision rests on, so the numbers quoted in the
ticket, the ADR-0061 amendment and the FRE-908 research addendum are re-derivable
rather than asserted:

* **compaction outcomes** — every ``within_session_compression_completed`` record in
  ``agent-logs-*``. Reports how many passes achieved zero-or-negative net reduction,
  how many produced a tail above its own floor, and the worst post-compaction working
  set. This is the evidence that ADR-0061's head-middle-tail pass fails on real
  production histories, not only on the FRE-908 synthetic fixture.
* **tool-result sizes** — every captured tool result in ``agent-captains-captures-*``,
  token-counted with the same ``cl100k_base`` encoding the compaction gates use.
  Reports which tools have ever emitted a single result above the tail floor, and the
  current-regime maximum. This is the evidence that the ADR-0085 intra-turn digest
  targets a scenario no current tool can produce.

Both corners are plain read-only ``_search`` calls — nothing is written, no LLM is
invoked, and no live gateway turn is fired.

Usage::

    python scripts/audit/fre942_compaction_census.py
    python scripts/audit/fre942_compaction_census.py --since 2026-06-01 --json
"""

from __future__ import annotations

import argparse
import json
import urllib.error
import urllib.request
from collections import Counter
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from typing import Any

import tiktoken

from personal_agent.config import settings

# The two bands the decision turns on, derived from live settings so this census
# self-invalidates if the thresholds drift (same discipline as the FRE-908 tests).
MAX_TOKENS = settings.context_window_max_tokens
TAIL_FLOOR = int(settings.within_session_min_tail_ratio * MAX_TOKENS)
HARD_THRESHOLD = int(settings.within_session_hard_threshold_ratio * MAX_TOKENS)

# Below this, exact token counting costs more than it informs — a 4k-char result is
# nowhere near the 24k-token floor under any encoding. Approximate those and spend the
# encode budget on the tail that actually matters.
EXACT_COUNT_ABOVE_CHARS = 4_000
SCROLL_PAGE = 500
SCROLL_TTL = "2m"


@dataclass(frozen=True)
class CompactionRecord:
    """One ``within_session_compression_completed`` emit.

    Attributes:
        timestamp: ISO-8601 emit time.
        trigger: ``"soft"`` or ``"hard"``.
        head_tokens: Preserved head band size.
        middle_tokens_in: Middle band size before compression.
        middle_tokens_out: Middle band size after compression.
        tail_tokens: Preserved tail band size.
        input_messages: Message count before the pass.
        output_messages: Message count after the pass.
    """

    timestamp: str
    trigger: str
    head_tokens: int
    middle_tokens_in: int
    middle_tokens_out: int
    tail_tokens: int
    input_messages: int
    output_messages: int

    @property
    def tokens_saved(self) -> int:
        """Net middle-band reduction; negative when the pass grew the band."""
        return self.middle_tokens_in - self.middle_tokens_out

    @property
    def post_total(self) -> int:
        """Working-set size after the pass, summed across all three bands."""
        return self.head_tokens + self.middle_tokens_out + self.tail_tokens


class ElasticsearchReader:
    """Minimal read-only scrolling reader.

    Deliberately not the project's ES client: this is an offline audit tool that must
    run against any reachable cluster without importing the service's async stack.
    """

    def __init__(self, base_url: str) -> None:
        """Store the cluster base URL.

        Args:
            base_url: Cluster root, e.g. ``http://localhost:9200``.
        """
        self._base_url = base_url.rstrip("/")

    def _post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        request = urllib.request.Request(
            f"{self._base_url}{path}",
            data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request) as response:
            parsed: dict[str, Any] = json.load(response)
        return parsed

    def scroll(
        self, index: str, query: dict[str, Any], source: Sequence[str]
    ) -> Iterator[dict[str, Any]]:
        """Yield every ``_source`` matching *query*, paging via the scroll API.

        Args:
            index: Index pattern, e.g. ``agent-logs-*``.
            query: Elasticsearch query DSL fragment.
            source: Fields to return.

        Yields:
            One ``_source`` mapping per matching document.
        """
        page = self._post(
            f"/{index}/_search?scroll={SCROLL_TTL}",
            {"size": SCROLL_PAGE, "_source": source, "query": query, "sort": [{"_doc": "asc"}]},
        )
        scroll_id = page.get("_scroll_id")
        hits = page["hits"]["hits"]
        while hits:
            for hit in hits:
                yield hit["_source"]
            page = self._post("/_search/scroll", {"scroll": SCROLL_TTL, "scroll_id": scroll_id})
            scroll_id = page.get("_scroll_id")
            hits = page["hits"]["hits"]


def collect_compactions(reader: ElasticsearchReader) -> list[CompactionRecord]:
    """Return every within-session compaction record in ``agent-logs-*``.

    Args:
        reader: Cluster reader.

    Returns:
        Records in index order.
    """
    fields = [
        "@timestamp",
        "trigger",
        "head_tokens",
        "middle_tokens_in",
        "middle_tokens_out",
        "tail_tokens",
        "input_messages",
        "output_messages",
    ]
    records: list[CompactionRecord] = []
    for src in reader.scroll(
        "agent-logs-*",
        {"term": {"event_type": "within_session_compression_completed"}},
        fields,
    ):
        records.append(
            CompactionRecord(
                timestamp=str(src.get("@timestamp", "")),
                trigger=str(src.get("trigger", "")),
                head_tokens=int(src.get("head_tokens") or 0),
                middle_tokens_in=int(src.get("middle_tokens_in") or 0),
                middle_tokens_out=int(src.get("middle_tokens_out") or 0),
                tail_tokens=int(src.get("tail_tokens") or 0),
                input_messages=int(src.get("input_messages") or 0),
                output_messages=int(src.get("output_messages") or 0),
            )
        )
    return records


def collect_tool_result_sizes(
    reader: ElasticsearchReader, since: str | None
) -> list[tuple[int, str, str]]:
    """Return ``(tokens, tool_name, timestamp)`` for every captured tool result.

    Args:
        reader: Cluster reader.
        since: Optional inclusive ISO date lower bound on the capture timestamp.

    Returns:
        One tuple per tool result. Results under
        :data:`EXACT_COUNT_ABOVE_CHARS` are approximated at 4 chars/token; larger
        ones are encoded exactly with ``cl100k_base``.
    """
    encoding = tiktoken.get_encoding("cl100k_base")
    query: dict[str, Any] = {"range": {"timestamp": {"gte": since}}} if since else {"match_all": {}}
    sizes: list[tuple[int, str, str]] = []
    for src in reader.scroll("agent-captains-captures-*", query, ["timestamp", "tool_results"]):
        timestamp = str(src.get("timestamp") or "")
        for result in src.get("tool_results") or []:
            output = result.get("output")
            text = output if isinstance(output, str) else json.dumps(output, default=str)
            tokens = (
                len(encoding.encode(text))
                if len(text) > EXACT_COUNT_ABOVE_CHARS
                else len(text) // 4
            )
            sizes.append((tokens, str(result.get("tool_name") or "?"), timestamp))
    return sizes


def report_compactions(records: Sequence[CompactionRecord]) -> dict[str, object]:
    """Summarise compaction outcomes and print the decision table.

    Args:
        records: Every collected compaction record.

    Returns:
        The same figures as a mapping, for ``--json`` consumers.
    """
    print(f"\n=== compaction outcomes ({len(records)} records) ===")
    if not records:
        print("no within_session_compression_completed records found")
        return {"records": 0}

    zero = [r for r in records if r.tokens_saved <= 0]
    tail_over = [r for r in records if r.tail_tokens > TAIL_FLOOR]
    still_over = [r for r in records if r.post_total >= HARD_THRESHOLD]
    worst = max(records, key=lambda r: r.post_total)
    saved = sorted(r.tokens_saved for r in records)

    pct = 100.0 / len(records)
    print(f"window={MAX_TOKENS:,}  tail_floor={TAIL_FLOOR:,}  hard_threshold={HARD_THRESHOLD:,}")
    print(f"triggers: {dict(Counter(r.trigger for r in records))}")
    print(f"zero-or-negative net reduction : {len(zero):>4} ({len(zero) * pct:.0f}%)")
    print(f"tail above its own floor       : {len(tail_over):>4} ({len(tail_over) * pct:.0f}%)")
    print(f"still >= hard threshold after  : {len(still_over):>4} ({len(still_over) * pct:.0f}%)")
    print(
        f"saved per record min/median/max: {saved[0]:,} / {saved[len(saved) // 2]:,} / {saved[-1]:,}"
    )
    print(
        f"worst post-compaction total    : {worst.post_total:,} tokens "
        f"({worst.post_total / MAX_TOKENS:.2f}x the window) at {worst.timestamp[:19]} "
        f"[{worst.trigger}] msgs {worst.input_messages}->{worst.output_messages} "
        f"mid {worst.middle_tokens_in:,}->{worst.middle_tokens_out:,} tail {worst.tail_tokens:,}"
    )
    return {
        "records": len(records),
        "zero_or_negative": len(zero),
        "tail_above_floor": len(tail_over),
        "still_above_hard_threshold": len(still_over),
        "worst_post_total": worst.post_total,
    }


def report_tool_sizes(
    sizes: Sequence[tuple[int, str, str]], since: str | None
) -> dict[str, object]:
    """Summarise tool-result sizes against the tail floor and print the table.

    Args:
        sizes: Collected ``(tokens, tool_name, timestamp)`` tuples.
        since: The lower bound applied, for the header line.

    Returns:
        The same figures as a mapping, for ``--json`` consumers.
    """
    scope = f"since {since}" if since else "all time"
    print(f"\n=== tool-result sizes, {scope} ({len(sizes)} results) ===")
    if not sizes:
        print("no captured tool results found")
        return {"results": 0}

    over = [s for s in sizes if s[0] > TAIL_FLOOR]
    ordered = sorted(sizes, reverse=True)
    print(f"tail_floor={TAIL_FLOOR:,} tokens")
    print(f"results exceeding the tail floor: {len(over)} ({100.0 * len(over) / len(sizes):.2f}%)")
    if over:
        print(f"tools that produced them        : {dict(Counter(s[1] for s in over))}")
        print(f"months they occurred in         : {dict(Counter(s[2][:7] for s in over))}")
    print(
        f"largest single result           : {ordered[0][0]:,} tokens ({ordered[0][1]}, {ordered[0][2][:10]})"
    )
    print("\ntop 10 by tokens:")
    for tokens, name, timestamp in ordered[:10]:
        print(
            f"  {tokens:>8,} tok  {name:<24} {timestamp[:19]}  = {tokens / TAIL_FLOOR:.2f}x floor"
        )
    return {
        "results": len(sizes),
        "exceeding_tail_floor": len(over),
        "largest": ordered[0][0],
        "tools_exceeding": dict(Counter(s[1] for s in over)),
    }


def main() -> int:
    """Run both censuses and print the decision tables.

    Returns:
        Process exit code — ``0`` on success, ``1`` when the cluster is unreachable.
    """
    parser = argparse.ArgumentParser(description="FRE-942 compaction / tool-result census")
    parser.add_argument(
        "--es-url",
        default=settings.elasticsearch_url,
        help="Elasticsearch base URL (defaults to the configured cluster)",
    )
    parser.add_argument(
        "--since",
        default=None,
        help="ISO date lower bound for the tool-result census, e.g. 2026-06-01",
    )
    parser.add_argument("--json", action="store_true", help="also emit the figures as JSON")
    args = parser.parse_args()

    reader = ElasticsearchReader(args.es_url)
    try:
        compactions = report_compactions(collect_compactions(reader))
        tools = report_tool_sizes(collect_tool_result_sizes(reader, args.since), args.since)
    except (urllib.error.URLError, OSError) as exc:
        print(f"elasticsearch unreachable at {args.es_url}: {exc}")
        return 1

    if args.json:
        print("\n" + json.dumps({"compactions": compactions, "tool_results": tools}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
