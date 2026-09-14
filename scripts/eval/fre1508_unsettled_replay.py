r"""Replay captured verification inputs before and after FRE-1508 (AC-2, AC-3).

FRE-1508 routes an entity-free span that containment matched only in part to the inline
entailment judge. This script measures what that changes on real turns, and nothing else.

Run as a module so ``scripts`` resolves as a package::

    make test-infra-up      # FRE-375: cost substrate on :5433, never production

    # 1. export the sample captures (read-only). The file holds user text and fetched
    #    pages, so it stays outside git.
    uv run python -m scripts.eval.fre1508_unsettled_replay export \
        --es-url <captures Elasticsearch URL> --out captures.json

    # 2. "before": run this file against a checkout of origin/main
    PYTHONPATH=<main checkout>/src uv run python -m scripts.eval.fre1508_unsettled_replay \
        replay --in captures.json --verdicts verdicts.json --out before.json

    # 3. "after": the same command on the branch
    uv run python -m scripts.eval.fre1508_unsettled_replay replay \
        --in captures.json --verdicts verdicts.json --out after.json

    uv run python -m scripts.eval.fre1508_unsettled_replay compare before.json after.json

**What the replay rebuilds, and what it cannot.** A capture records each tool's output,
but not the exact string the registry received, so a rebuilt identifier's digest does not
match the captured one. Sources are therefore matched by **ordinal**: the user message,
one placeholder per admitted memory identity, then each tool result in order. Memory
content is not captured, so a span citing a memory source is left out and counted. The
"before" run is compared with the captured outcomes, and that agreement is the replay's
fidelity: a low figure means the rebuilt inputs are not the turn's inputs.

**The judge is real and its verdicts are cached.** Both runs share one verdict file keyed
on the claim and a digest of the source content, so a span the judge saw in the "before"
run gets the same verdict in the "after" run. Without the cache, judge variance alone
could move a settled outcome and fail AC-3 for a reason unrelated to the fix.
"""

from __future__ import annotations

import os

# FRE-375: point the cost substrate at the TEST stack BEFORE importing any personal_agent
# code — ``settings`` is a cached import-time singleton. The judge calls go to the real
# provider; the cost ledger they reserve against must never be production's.
_TEST_SUBSTRATE_ENV = {
    "APP_ENV": "test",
    "AGENT_NEO4J_URI": "bolt://localhost:7688",
    "AGENT_ELASTICSEARCH_URL": "http://localhost:9201",
    "AGENT_DATABASE_URL": (
        "postgresql+asyncpg://agent:agent_dev_password@localhost:5433/personal_agent"
    ),
    "AGENT_DATABASE_ADMIN_URL": (
        "postgresql+asyncpg://agent:agent_dev_password@localhost:5433/personal_agent"
    ),
    "AGENT_SYSGRAPH_DATABASE_URL": (
        "postgresql+asyncpg://agent:agent_dev_password@localhost:5433/personal_agent"
    ),
    "AGENT_ELASTICSEARCH_INDEX_PREFIX": "agent-logs-test",
    "AGENT_CAPTAINS_LOG_INDEX_PREFIX": "agent-captains-test",
}
for _key, _value in _TEST_SUBSTRATE_ENV.items():
    os.environ.setdefault(_key, _value)

import argparse  # noqa: E402
import asyncio  # noqa: E402
import hashlib  # noqa: E402
import inspect  # noqa: E402
import json  # noqa: E402
import urllib.request  # noqa: E402
from collections.abc import Mapping, Sequence  # noqa: E402
from dataclasses import dataclass  # noqa: E402
from pathlib import Path  # noqa: E402
from typing import Any  # noqa: E402

UNSETTLED = frozenset(
    {"unverifiable_by_containment", "entailment_required", "entailment_unavailable"}
)
"""ADR-0151 D2's unsettled outcomes, as the turn record spells them."""

SAMPLE_START = "2026-08-31T00:00:00Z"
SAMPLE_END = "2026-09-14T00:00:00Z"
"""The FRE-1508 sample window: captures 2026-08-31 to 2026-09-13 inclusive."""

_FILLER = "replayed "
"""Word text placed before each cited span, outside its offsets.

A marker binds the text since the previous marker, and a region with no word characters
binds nothing. The captured fragments (``". "``) would otherwise read as uncited.
"""


# ── Pure core: comparison ────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Comparison:
    """What changed between the two replays.

    Attributes:
        valid: Whether the run can serve as AC-2 and AC-3 evidence at all.
        invalid_reason: Why not, when it cannot.
        turns: Turns compared after the fidelity filter.
        turns_dropped: Turns dropped because a "before" outcome differed from the capture.
        spans: Spans compared after the fidelity filter.
        unsettled_before: Unsettled spans in the "before" run.
        unsettled_after: Unsettled spans in the "after" run.
        changes: Every ``(key, before, after, verdict)`` whose outcome differs.
        settled_changed: Changes whose "before" outcome was settled (AC-3 violations).
        passed_without_verdict: Unsettled-to-passed changes with no ``supported`` verdict
            (AC-2 violations).
        fidelity_matched: "Before" outcomes equal to the captured outcome, over every
            included span before the filter.
        fidelity_total: Included spans before the filter.
    """

    valid: bool
    invalid_reason: str
    turns: int
    turns_dropped: int
    spans: int
    unsettled_before: int
    unsettled_after: int
    changes: tuple[tuple[str, str, str, str | None], ...]
    settled_changed: tuple[tuple[str, str, str, str | None], ...]
    passed_without_verdict: tuple[tuple[str, str, str, str | None], ...]
    fidelity_matched: int
    fidelity_total: int


def compare(
    before: Sequence[Mapping[str, Any]],
    after: Sequence[Mapping[str, Any]],
    *,
    min_turns: int = 10,
) -> Comparison:
    """Compare two replay outputs span by span.

    Args:
        before: Span rows from the run on the unfixed code.
        after: Span rows from the run on the fixed code.
        min_turns: Fewest turns that may remain after the fidelity filter. The AC-2 bar
            recorded on FRE-1508 says 10.

    Returns:
        The comparison. It is invalid when the two runs did not include the same spans, or
        when fewer than ``min_turns`` turns remain after dropping every turn with a
        "before" outcome that differs from its capture. A replay that cannot reproduce a
        turn is not evidence about that turn.

    Raises:
        ValueError: When a span key repeats within one run.
    """
    before_rows = _index(before)
    after_rows = _index(after)
    included_before = {key for key, row in before_rows.items() if row.get("excluded") is None}
    included_after = {key for key, row in after_rows.items() if row.get("excluded") is None}
    unfaithful = {
        key
        for key in included_before
        if before_rows[key]["replay_outcome"] != before_rows[key]["captured_outcome"]
    }
    dropped = {before_rows[key]["trace_id"] for key in unfaithful}
    keys = sorted(
        key
        for key in included_before & included_after
        if before_rows[key]["trace_id"] not in dropped
    )
    turns = {before_rows[key]["trace_id"] for key in keys}

    changes = []
    unsettled_before = unsettled_after = 0
    for key in keys:
        b = before_rows[key]
        a = after_rows[key]
        unsettled_before += b["replay_outcome"] in UNSETTLED
        unsettled_after += a["replay_outcome"] in UNSETTLED
        if b["replay_outcome"] != a["replay_outcome"]:
            changes.append((key, b["replay_outcome"], a["replay_outcome"], a.get("verdict")))

    invalid_reason = ""
    if included_before != included_after:
        invalid_reason = "the two runs did not include the same spans"
    elif len(turns) < min_turns:
        invalid_reason = (
            f"{len(turns)} turns remain after the fidelity filter; {min_turns} are required"
        )

    return Comparison(
        valid=not invalid_reason,
        invalid_reason=invalid_reason,
        turns=len(turns),
        turns_dropped=len(dropped),
        spans=len(keys),
        unsettled_before=unsettled_before,
        unsettled_after=unsettled_after,
        changes=tuple(changes),
        settled_changed=tuple(c for c in changes if c[1] not in UNSETTLED),
        passed_without_verdict=tuple(
            c for c in changes if c[1] in UNSETTLED and c[2] == "passed" and c[3] != "supported"
        ),
        fidelity_matched=len(included_before) - len(unfaithful),
        fidelity_total=len(included_before),
    )


def _index(rows: Sequence[Mapping[str, Any]]) -> dict[str, Mapping[str, Any]]:
    """Key span rows by ``trace_id#index``.

    Args:
        rows: Span rows from one run.

    Returns:
        The rows by key.

    Raises:
        ValueError: When a key repeats.
    """
    indexed: dict[str, Mapping[str, Any]] = {}
    for row in rows:
        key = f"{row['trace_id']}#{row['index']}"
        if key in indexed:
            raise ValueError(f"duplicate span row {key}")
        indexed[key] = row
    return indexed


# ── I/O: export ─────────────────────────────────────────────────────────────────────


def export(es_url: str, out: Path) -> int:
    """Write every sample turn that carries an unverifiable span to ``out``.

    Args:
        es_url: The Elasticsearch holding ``agent-captains-captures-*``. Read only.
        out: Destination JSON file.

    Returns:
        Process exit code.
    """
    query = {
        "size": 1000,
        "_source": [
            "trace_id",
            "user_message",
            "assembled_context.memory_identities",
            "tool_results",
            "grounding.spans",
        ],
        "query": {
            "bool": {
                "filter": [
                    {"range": {"timestamp": {"gte": SAMPLE_START, "lt": SAMPLE_END}}},
                    {"term": {"grounding.available": True}},
                    {"term": {"grounding.spans.outcome": "unverifiable_by_containment"}},
                ]
            }
        },
    }
    request = urllib.request.Request(
        f"{es_url.rstrip('/')}/agent-captains-captures-*/_search",
        data=json.dumps(query).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request) as response:  # noqa: S310 — operator-supplied URL
        hits = json.load(response)["hits"]["hits"]
    turns = [hit["_source"] for hit in hits]
    out.write_text(json.dumps(turns))
    print(f"exported {len(turns)} turns to {out}")  # noqa: T201
    return 0


# ── I/O: replay ─────────────────────────────────────────────────────────────────────


class _CachingJudge:
    """A judge that answers from the shared verdict file before it calls the model.

    Keyed on the turn, a digest of the passage and the claim, so one claim citing two
    passages, or the same claim in two turns, never shares a verdict.
    """

    def __init__(self, inner: object, cache: dict[str, dict[str, str]]) -> None:
        self._inner = inner
        self.cache = cache
        self.calls = 0
        self.trace_id = ""

    def key(self, claim: str, source_content: str) -> str:
        """Return the cache key for one judgement in the current turn."""
        digest = hashlib.sha256(source_content.encode("utf-8")).hexdigest()
        return f"{self.trace_id}:{digest}:{claim}"

    async def judge(self, claim: str, source_content: str, *, trace_ctx: object = None) -> Any:
        """Return the cached verdict, or ask the model and cache its answer."""
        from personal_agent.grounding.entailment import (  # noqa: PLC0415
            EntailmentJudgement,
            EntailmentVerdict,
        )

        key = self.key(claim, source_content)
        cached = self.cache.get(key)
        if cached is not None:
            return EntailmentJudgement(
                verdict=EntailmentVerdict(cached["verdict"]), reason=cached["reason"]
            )
        self.calls += 1
        judgement = await self._inner.judge(claim, source_content, trace_ctx=trace_ctx)  # type: ignore[attr-defined]
        self.cache[key] = {"verdict": judgement.verdict.value, "reason": judgement.reason}
        return judgement


def _rebuild(turn: Mapping[str, Any]) -> tuple[Any, dict[int, Any]]:
    """Rebuild one turn's registry and index its sources by ordinal.

    Args:
        turn: One exported capture.

    Returns:
        The registry and its sources by ordinal.
    """
    from personal_agent.grounding.source_registry import SourceRegistry  # noqa: PLC0415

    registry = SourceRegistry(turn_id=turn["trace_id"])
    registry.register_user_message(turn.get("user_message") or "")
    memory = (turn.get("assembled_context") or {}).get("memory_identities") or []
    for identity in memory:
        # Distinct content per placeholder: the registry dedupes on (kind, origin, content),
        # and identical empty placeholders would collapse into one ordinal and shift every
        # tool source after them.
        registry.register_memory_item(
            {"type": "episode", "id": identity, "description": f"placeholder {identity}"}
        )
    for result in turn.get("tool_results") or []:
        arguments = result.get("arguments") or {}
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments)
            except json.JSONDecodeError:
                arguments = {}
        output = result.get("output")
        registry.register_tool_result(
            tool_name=result["tool_name"],
            arguments=arguments if isinstance(arguments, dict) else {},
            content=output if isinstance(output, str) else json.dumps(output),
            success=bool(result.get("success")),
        )
    by_ordinal = {int(src.identifier.split("@")[0][1:]): src for src in registry.sources()}
    return registry, by_ordinal


async def _replay_turn(turn: Mapping[str, Any], judge: _CachingJudge) -> list[dict[str, Any]]:
    """Verify one rebuilt turn and return one row per captured span.

    Args:
        turn: One exported capture.
        judge: The caching judge.

    Returns:
        Span rows.
    """
    from personal_agent.config import settings  # noqa: PLC0415
    from personal_agent.grounding.citations import parse_citations  # noqa: PLC0415
    from personal_agent.grounding.source_registry import SourceKind  # noqa: PLC0415
    from personal_agent.grounding.spans import (  # noqa: PLC0415
        NonExemptReason,
        Span,
        SpanExtraction,
        SpanLabel,
    )
    from personal_agent.grounding.verification import (  # noqa: PLC0415
        apply_entailment,
        verify_turn,
    )

    registry, by_ordinal = _rebuild(turn)
    captured = (turn.get("grounding") or {}).get("spans") or []
    rows: list[dict[str, Any]] = []
    cited: list[tuple[int, str, str]] = []
    uncited: list[tuple[int, str]] = []

    for index, span in enumerate(captured):
        row = {
            "trace_id": turn["trace_id"],
            "index": index,
            "captured_outcome": span["outcome"],
            "replay_outcome": None,
            "verdict": None,
            "excluded": None,
        }
        rows.append(row)
        identifier = span.get("identifier")
        if identifier is None:
            uncited.append((index, span["text"]))
            continue
        if span["outcome"] == "unresolved":
            cited.append((index, span["text"], identifier))
            continue
        source = by_ordinal.get(int(identifier.split("@")[0][1:]))
        if source is None:
            row["excluded"] = "no rebuilt source at this ordinal"
        elif source.kind is SourceKind.MEMORY:
            row["excluded"] = "cites a memory source, whose content is not captured"
        else:
            cited.append((index, span["text"], source.identifier))

    output = ""
    spans: list[Span] = []
    order: list[int] = []
    for index, text, identifier in cited:
        output += _FILLER
        spans.append(_span(len(output), text, Span, SpanLabel, NonExemptReason))
        output += f"{text} [{identifier}]\n"
        order.append(index)
    for index, text in uncited:
        spans.append(_span(len(output), text, Span, SpanLabel, NonExemptReason))
        output += f"{text}\n"
        order.append(index)

    if not spans:
        return rows

    verification = verify_turn(
        SpanExtraction(output=output, spans=tuple(spans)), parse_citations(output), registry
    )
    # The "before" run imports origin/main, whose apply_entailment has no partial-miss arm.
    partial_cap: dict[str, int] = (
        {"max_partial_miss_checks": settings.grounding_entailment_max_partial_miss_checks}
        if "max_partial_miss_checks" in inspect.signature(apply_entailment).parameters
        else {}
    )
    entail: Any = apply_entailment
    judge.trace_id = turn["trace_id"]
    settled = await entail(
        verification,
        registry,
        judge,
        max_checks=settings.grounding_entailment_max_inline_checks,
        budget_ms=settings.grounding_entailment_latency_budget_ms,
        **partial_cap,
    )
    for index, contained, span in zip(order, verification.spans, settled.spans, strict=True):
        row = rows[index]
        row["replay_outcome"] = span.outcome.value
        row["containment_outcome"] = contained.outcome.value
        row["missing"] = list(contained.missing)
        row["entity_free_predicate"] = contained.entity_free_predicate
        source = registry.resolve(span.identifier) if span.identifier else None
        cached = judge.cache.get(judge.key(span.text, source.content)) if source else None
        if cached is not None:
            row["verdict"] = cached["verdict"]
            row["reason"] = cached["reason"]
    return rows


def _span(start: int, text: str, span_type: Any, label: Any, reason: Any) -> Any:
    """Return one non-exempt span at ``start``."""
    return span_type(
        start=start,
        end=start + len(text),
        text=text,
        label=label.CLAIM_NON_EXEMPT,
        reason=reason.CLASSIFIED,
    )


async def _replay(captures: Path, verdicts: Path, out: Path) -> int:
    """Replay every exported turn on the code this process imported.

    Args:
        captures: The export.
        verdicts: The shared verdict cache, created when absent.
        out: Destination for the span rows.

    Returns:
        Process exit code.
    """
    import personal_agent  # noqa: PLC0415
    from personal_agent.config import settings  # noqa: PLC0415
    from personal_agent.cost_gate import (  # noqa: PLC0415
        CostGate,
        load_budget_config,
        set_default_gate,
    )
    from personal_agent.grounding.entailment import ModelEntailmentJudge  # noqa: PLC0415
    from personal_agent.llm_client.factory import get_llm_client  # noqa: PLC0415
    from personal_agent.llm_client.types import ModelRole  # noqa: PLC0415

    print(f"personal_agent imported from {Path(personal_agent.__file__).parent}")  # noqa: T201
    gate = CostGate(config=load_budget_config(), db_url=settings.database_url)
    await gate.connect()
    set_default_gate(gate)

    cache: dict[str, dict[str, str]] = json.loads(verdicts.read_text()) if verdicts.exists() else {}
    judge = _CachingJudge(
        ModelEntailmentJudge(
            get_llm_client(role_name=ModelRole.ENTAILMENT.value),
            timeout_s=settings.grounding_entailment_latency_budget_ms / 1000,
            max_excerpt_chars=settings.grounding_entailment_max_excerpt_chars,
        ),
        cache,
    )
    rows: list[dict[str, Any]] = []
    try:
        for turn in json.loads(captures.read_text()):
            rows.extend(await _replay_turn(turn, judge))
    finally:
        verdicts.write_text(json.dumps(cache, indent=1))
    out.write_text(json.dumps(rows, indent=1))
    print(f"replayed {len(rows)} spans, {judge.calls} new judge calls → {out}")  # noqa: T201
    return 0


def _render(result: Comparison) -> str:
    """Render a comparison for the ticket."""
    lines = [
        f"valid: {result.valid}" + (f" ({result.invalid_reason})" if result.invalid_reason else ""),
        f"fidelity (before == captured): {result.fidelity_matched}/{result.fidelity_total}",
        f"turns compared: {result.turns} (dropped by the fidelity filter: {result.turns_dropped})",
        f"spans compared: {result.spans}",
        f"unsettled before: {result.unsettled_before}",
        f"unsettled after: {result.unsettled_after}",
        f"fall: {result.unsettled_before - result.unsettled_after}",
        f"AC-3 settled outcomes changed: {len(result.settled_changed)}",
        f"AC-2 unsettled -> passed without a supported verdict: "
        f"{len(result.passed_without_verdict)}",
        "changes:",
    ]
    lines += [f"  {key}: {b} -> {a} (verdict {v})" for key, b, a, v in result.changes]
    return "\n".join(lines)


def main() -> int:
    """Parse arguments and run.

    Returns:
        Process exit code.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    exporting = commands.add_parser("export")
    exporting.add_argument("--es-url", required=True)
    exporting.add_argument("--out", type=Path, required=True)
    replaying = commands.add_parser("replay")
    replaying.add_argument("--in", dest="captures", type=Path, required=True)
    replaying.add_argument("--verdicts", type=Path, required=True)
    replaying.add_argument("--out", type=Path, required=True)
    comparing = commands.add_parser("compare")
    comparing.add_argument("before", type=Path)
    comparing.add_argument("after", type=Path)
    args = parser.parse_args()

    match args.command:
        case "export":
            return export(args.es_url, args.out)
        case "replay":
            return asyncio.run(_replay(args.captures, args.verdicts, args.out))
        case "compare":
            result = compare(
                json.loads(args.before.read_text()), json.loads(args.after.read_text())
            )
            print(_render(result))  # noqa: T201
            clean = not result.settled_changed and not result.passed_without_verdict
            return 0 if result.valid and clean else 1
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
