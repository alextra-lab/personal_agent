"""Recovery Wave 1.2 — prompt harness driving /chat through the full stack.

For each prompt in ``prompts.yaml`` the harness:

  1. POSTs ``message`` to ``http://localhost:9000/chat`` (multi-turn prompts
     reuse ``session_id`` from the prior turn).
  2. Captures ``trace_id`` from the response body (already surfaced by
     ``service/app.py:1350``).
  3. Waits briefly for Elasticsearch indexing.
  4. Queries ES for any logs tagged with that ``trace_id`` and extracts:
     skill_block injection size, tool calls requested vs executed, loop-gate
     decisions, forced-synthesis events, compression events, memory_context
     size, Captain's Log capture id, entity-extraction outcome.
  5. Queries Neo4j for any ``Turn`` / ``Entity`` / relationship writes whose
     ``trace_id`` field matches.
  6. Renders one ``report.md`` per prompt and a roll-up ``summary.md``.

Usage::

    uv run python scripts/eval/recovery_harness.py \
        --run-id smoke-2026-05-05 \
        --prompts telemetry/evaluation/EVAL-agent-self-diagnosis/prompts.yaml

Single prompt::

    uv run python scripts/eval/recovery_harness.py \
        --run-id one-shot --prompt primitive_tool_with_implied_skill

Notes:
- The ``--profile`` flag is forwarded only as a metadata tag in the report.
  Wave 1 does not introduce a recovery profile yet; that is Wave 4 work.
- This script does *not* fail if ES indexing has not caught up; it logs a
  warning and writes the partial report.
- ``--es-wait-seconds`` is deprecated. The harness now polls ES for terminal
  events instead of sleeping. If passed, the value becomes the polling
  hard-timeout (default: 30s).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx
import structlog
import yaml  # type: ignore[import-untyped]

from personal_agent.config import get_settings
from personal_agent.memory.service import MemoryService
from personal_agent.telemetry import TelemetryQueries

log = structlog.get_logger(__name__)


DEFAULT_CHAT_URL = "http://localhost:9000/chat"
DEFAULT_PROMPTS_PATH = Path("telemetry/evaluation/EVAL-agent-self-diagnosis/prompts.yaml")
DEFAULT_OUT_BASE = Path("telemetry/evaluation/EVAL-agent-self-diagnosis")

_TERMINAL_EVENTS: frozenset[str] = frozenset(
    {"reply_ready", "orchestrator_fatal_error", "task_failed"}
)
_DEFAULT_POLL_INTERVAL: float = 0.5
_DEFAULT_HARD_TIMEOUT: float = 30.0


# ---------------------------------------------------------------------------
# Prompt model
# ---------------------------------------------------------------------------


@dataclass
class PromptTurn:
    """One turn of a prompt: a message + optional 'expect' + new_session flag.

    Set ``new_session=True`` on a turn to force the harness to drop the prior
    ``session_id`` and start a fresh session. Required for memory-recall
    canaries — without it, the agent answers from session history and never
    exercises the memory-graph retrieval path.
    """

    message: str
    expect: dict[str, Any] = field(default_factory=dict)
    new_session: bool = False


@dataclass
class PromptDef:
    """A canary prompt definition loaded from prompts.yaml."""

    id: str
    description: str
    turns: list[PromptTurn]
    tags: list[str] = field(default_factory=list)


def load_prompts(path: Path) -> list[PromptDef]:
    """Load prompts.yaml and return a list of PromptDef."""
    raw = yaml.safe_load(path.read_text())
    out: list[PromptDef] = []
    for entry in raw.get("prompts", []):
        turns = [
            PromptTurn(
                message=t["message"],
                expect=t.get("expect", {}),
                new_session=bool(t.get("new_session", False)),
            )
            for t in entry.get("turns", [])
        ]
        out.append(
            PromptDef(
                id=entry["id"],
                description=entry.get("description", ""),
                turns=turns,
                tags=entry.get("tags", []),
            )
        )
    return out


# ---------------------------------------------------------------------------
# Per-turn execution
# ---------------------------------------------------------------------------


@dataclass
class TurnResult:
    """Outcome of a single /chat call."""

    message: str
    session_id: str
    trace_id: str
    response_text: str
    started_at: datetime
    finished_at: datetime
    es_hits: list[dict[str, Any]] = field(default_factory=list)
    neo4j_turn_count: int = 0
    neo4j_entity_count: int = 0
    neo4j_relationship_count: int = 0


async def call_chat(
    client: httpx.AsyncClient,
    chat_url: str,
    message: str,
    session_id: str | None,
    auth_email: str | None,
    profile: str = "local",
    skill_routing_mode: str | None = None,
) -> tuple[str, str, str]:
    """POST /chat. Return (response_text, session_id, trace_id).

    When ``auth_email`` is set, sends the CF Access header that
    ``service/auth.py`` reads. This impersonates an authenticated user for
    local-loopback diagnostic calls — the same trust model as production
    where CF Access stamps the header in front of the service.
    """
    params = {"message": message, "profile": profile, "channel": "EVAL"}
    if session_id is not None:
        params["session_id"] = session_id
    if skill_routing_mode:
        params["skill_routing_mode"] = skill_routing_mode
    headers: dict[str, str] = {}
    if auth_email:
        headers["Cf-Access-Authenticated-User-Email"] = auth_email
    resp = await client.post(chat_url, params=params, headers=headers, timeout=600.0)
    resp.raise_for_status()
    data = resp.json()
    return (
        str(data.get("response", "")),
        str(data["session_id"]),
        str(data["trace_id"]),
    )


async def fetch_trace_logs(
    queries: TelemetryQueries,
    trace_id: str,
    since: datetime,
    until: datetime,
    *,
    page_size: int = 500,
    hard_cap: int = 10_000,
) -> list[dict[str, Any]]:
    """Pull all ES log documents tagged with this trace_id in the window.

    Uses ``search_after`` pagination so traces with more than 500 events are
    fetched completely. Stops at ``hard_cap`` events and emits
    ``harness_es_pagination_capped`` if hit.

    Args:
        queries: TelemetryQueries instance for ES access.
        trace_id: Trace ID to filter on (keyword sub-field exact match).
        since: Lower bound of the ``@timestamp`` range.
        until: Upper bound of the ``@timestamp`` range.
        page_size: Number of documents per ES page.
        hard_cap: Maximum total documents before aborting pagination.

    Returns:
        List of ``_source`` dicts in ascending timestamp order.
    """
    settings = get_settings()
    client = await queries._get_client()  # noqa: SLF001 — internal reuse
    all_hits: list[dict[str, Any]] = []
    sort_key: list[Any] | None = None

    while True:
        body: dict[str, Any] = {
            "query": {
                "bool": {
                    "filter": [
                        {"term": {"trace_id.keyword": trace_id}},
                        {
                            "range": {
                                "@timestamp": {
                                    "gte": since.isoformat(),
                                    "lte": until.isoformat(),
                                }
                            }
                        },
                    ]
                }
            },
            "size": page_size,
            # _id tiebreaker ensures deterministic search_after across pages.
            "sort": [{"@timestamp": {"order": "asc"}}, {"_id": "asc"}],
        }
        if sort_key is not None:
            body["search_after"] = sort_key

        response = await client.search(
            index=f"{settings.elasticsearch_index_prefix}-*",
            **body,
        )
        hits = response.get("hits", {}).get("hits", [])
        if not hits:
            break

        all_hits.extend(hit.get("_source", {}) for hit in hits)

        if len(all_hits) >= hard_cap:
            log.warning(
                "harness_es_pagination_capped",
                trace_id=trace_id,
                hard_cap=hard_cap,
                fetched=len(all_hits),
            )
            break

        if len(hits) < page_size:
            break

        sort_key = hits[-1]["sort"]

    return all_hits


async def _wait_for_trace_complete(
    queries: TelemetryQueries,
    trace_id: str,
    started_at: datetime,
    *,
    poll_interval: float = _DEFAULT_POLL_INTERVAL,
    hard_timeout: float = _DEFAULT_HARD_TIMEOUT,
) -> None:
    """Poll ES until a terminal event appears, event count stabilises, or hard_timeout elapses.

    Terminal events (``reply_ready``, ``orchestrator_fatal_error``, ``task_failed``)
    mark definitive completion. Stability (count unchanged across two consecutive
    polls) is a fallback for traces that finish without a recognised terminal event.

    Args:
        queries: TelemetryQueries instance for ES access.
        trace_id: The trace ID to poll for.
        started_at: Request start time used as the lower-bound on ``@timestamp``.
        poll_interval: Seconds between polls.
        hard_timeout: Maximum seconds to wait before giving up; logs
            ``harness_es_wait_timeout`` and returns so the caller can proceed
            with whatever partial data is available.
    """
    settings = get_settings()
    client = await queries._get_client()  # noqa: SLF001 — internal reuse
    loop = asyncio.get_event_loop()
    deadline = loop.time() + hard_timeout
    prev_count = -1
    stable_polls = 0

    while loop.time() < deadline:
        response = await client.search(
            index=f"{settings.elasticsearch_index_prefix}-*",
            query={
                "bool": {
                    "filter": [
                        {"term": {"trace_id.keyword": trace_id}},
                        {"range": {"@timestamp": {"gte": started_at.isoformat()}}},
                    ]
                }
            },
            size=500,
            sort=[{"@timestamp": {"order": "asc"}}],
        )
        hits = response.get("hits", {}).get("hits", [])
        count = len(hits)

        for hit in hits:
            src = hit.get("_source", {})
            event = str(src.get("event_type") or src.get("event") or "")
            if event in _TERMINAL_EVENTS:
                return

        if count > 0 and count == prev_count:
            stable_polls += 1
            if stable_polls >= 2:
                return
        else:
            stable_polls = 0
        prev_count = count

        await asyncio.sleep(poll_interval)

    log.warning(
        "harness_es_wait_timeout",
        trace_id=trace_id,
        hard_timeout=hard_timeout,
    )


async def fetch_neo4j_for_trace(
    memory_service: MemoryService, trace_id: str
) -> tuple[int, int, int]:
    """Return (turn_count, entity_count, relationship_count) for a trace_id."""
    if not memory_service.connected or memory_service.driver is None:
        return (0, 0, 0)
    async with memory_service.driver.session() as session:
        turn_result = await session.run(
            "MATCH (t:Turn) WHERE t.trace_id = $trace_id RETURN count(t) AS c",
            trace_id=trace_id,
        )
        turn_record = await turn_result.single()
        turn_count = int(turn_record["c"]) if turn_record else 0

        ent_result = await session.run(
            "MATCH (t:Turn)-[:DISCUSSES]->(e:Entity) "
            "WHERE t.trace_id = $trace_id RETURN count(DISTINCT e) AS c",
            trace_id=trace_id,
        )
        ent_record = await ent_result.single()
        entity_count = int(ent_record["c"]) if ent_record else 0

        rel_result = await session.run(
            "MATCH (t:Turn)-[:DISCUSSES]->(:Entity)-[r]-(:Entity) "
            "WHERE t.trace_id = $trace_id RETURN count(DISTINCT r) AS c",
            trace_id=trace_id,
        )
        rel_record = await rel_result.single()
        rel_count = int(rel_record["c"]) if rel_record else 0
    return (turn_count, entity_count, rel_count)


# ---------------------------------------------------------------------------
# Aggregations from raw ES log hits
# ---------------------------------------------------------------------------


def summarize_logs(es_hits: list[dict[str, Any]]) -> dict[str, Any]:
    """Distill the per-trace ES log slice into a small summary block.

    Event-name notes (verified against a real run on 2026-05-05):
    - Tool calls fire as ``tool_call_started`` / ``tool_call_completed``,
      not ``_requested`` / ``_executed``.
    - Loop gates are ``tool_loop_gate``, with ``decision`` field.
    - Within-session compression fires ``within_session_compression_completed``.
    - Skill-block injection currently logs only at DEBUG (``skill_route_matched``)
      and isn't reliably in ES — surfaced size remains None until Workstream D
      adds an INFO-level event.
    - Entity extraction runs in a separate trace_id (consolidation pipeline);
      the canary inspects it directly, this summarizer stays None.
    """
    by_event: dict[str, int] = {}
    skill_block_size: int | None = None
    memory_context_size: int | None = None
    forced_synthesis_events = 0
    compression_events = 0
    capture_id: str | None = None
    extraction_outcome: str | None = None
    tool_calls_requested = 0
    tool_calls_executed = 0
    bash_calls = 0
    loop_gate_decisions: list[str] = []

    for hit in es_hits:
        event = str(hit.get("event_type") or hit.get("event") or "")
        if not event:
            continue
        by_event[event] = by_event.get(event, 0) + 1

        if event == "skill_block_injected":
            sz = hit.get("skill_block_size_tokens") or hit.get("size_tokens")
            if sz is not None:
                skill_block_size = int(sz)
        elif event == "memory_context_assembled":
            sz = hit.get("memory_context_size_tokens") or hit.get("size_tokens")
            if sz is not None:
                memory_context_size = int(sz)
        elif event == "forced_synthesis":
            forced_synthesis_events += 1
        elif event in {
            "context_compression_completed",
            "within_session_compression_completed",
        }:
            compression_events += 1
        elif event == "captains_log_capture_written":
            cap = hit.get("capture_id")
            if cap is not None:
                capture_id = str(cap)
        elif event == "entity_extraction_completed":
            extraction_outcome = "completed"
        elif event == "entity_extraction_failed":
            extraction_outcome = f"failed:{hit.get('error_type', 'unknown')}"
        elif event == "entity_extraction_timeout":
            extraction_outcome = "timeout"
        elif event in {"tool_call_started", "tool_invocation_started"}:
            tool_calls_requested += 1
        elif event in {"tool_call_completed", "tool_invocation_completed"}:
            tool_calls_executed += 1
        elif event == "bash_completed":
            bash_calls += 1
        elif event in {"tool_loop_gate", "loop_gate_decision", "loop_gate_blocked"}:
            decision = str(hit.get("decision") or hit.get("outcome") or event)
            loop_gate_decisions.append(decision)

    return {
        "log_count": len(es_hits),
        "events_by_type": dict(sorted(by_event.items(), key=lambda kv: -kv[1])),
        "skill_block_size_tokens": skill_block_size,
        "memory_context_size_tokens": memory_context_size,
        "forced_synthesis_events": forced_synthesis_events,
        "compression_events": compression_events,
        "capture_id": capture_id,
        "extraction_outcome": extraction_outcome,
        "tool_calls_requested": tool_calls_requested,
        "tool_calls_executed": tool_calls_executed,
        "bash_calls": bash_calls,
        "loop_gate_decisions": loop_gate_decisions,
    }


# ---------------------------------------------------------------------------
# Markdown rendering
# ---------------------------------------------------------------------------


def render_turn_report(prompt: PromptDef, results: list[TurnResult]) -> str:
    """Render one prompt's turn-by-turn report."""
    lines = [
        f"# Prompt: `{prompt.id}`",
        "",
        f"_{prompt.description}_",
        "",
        f"Tags: {', '.join(prompt.tags) if prompt.tags else '—'}",
        "",
    ]
    for i, r in enumerate(results, start=1):
        lines.append(f"## Turn {i}")
        lines.append("")
        lines.append(f"- session_id: `{r.session_id}`")
        lines.append(f"- trace_id:   `{r.trace_id}`")
        lines.append(f"- duration:   {(r.finished_at - r.started_at).total_seconds():.2f}s")
        lines.append("")
        lines.append("**User**")
        lines.append("")
        lines.append("```")
        lines.append(r.message)
        lines.append("```")
        lines.append("")
        lines.append("**Assistant**")
        lines.append("")
        lines.append("```")
        lines.append(r.response_text[:4000])
        if len(r.response_text) > 4000:
            lines.append(f"... <truncated, {len(r.response_text)} chars total>")
        lines.append("```")
        lines.append("")
        s = summarize_logs(r.es_hits)
        lines.append("**ES log summary**")
        lines.append("")
        lines.append(f"- log_count: {s['log_count']}")
        lines.append(f"- skill_block_size_tokens: {s['skill_block_size_tokens']}")
        lines.append(f"- memory_context_size_tokens: {s['memory_context_size_tokens']}")
        lines.append(f"- forced_synthesis_events: {s['forced_synthesis_events']}")
        lines.append(f"- compression_events: {s['compression_events']}")
        lines.append(f"- capture_id: {s['capture_id']}")
        lines.append(f"- extraction_outcome: {s['extraction_outcome']}")
        lines.append(
            f"- tool_calls: {s['tool_calls_requested']} requested / "
            f"{s['tool_calls_executed']} executed (bash: {s['bash_calls']})"
        )
        if s["loop_gate_decisions"]:
            lines.append(f"- loop_gate_decisions: {s['loop_gate_decisions']}")
        lines.append("")
        lines.append("**Neo4j writes scoped to this trace_id**")
        lines.append("")
        lines.append(f"- Turn nodes:        {r.neo4j_turn_count}")
        lines.append(f"- Entity nodes:      {r.neo4j_entity_count}")
        lines.append(f"- Relationships:     {r.neo4j_relationship_count}")
        lines.append("")
        if s["events_by_type"]:
            lines.append("**Events by type (top 20)**")
            lines.append("")
            lines.append("| event | count |")
            lines.append("|---|---:|")
            for ev, n in list(s["events_by_type"].items())[:20]:
                lines.append(f"| `{ev}` | {n} |")
            lines.append("")
    return "\n".join(lines)


def render_summary(
    *,
    run_id: str,
    profile: str,
    prompt_results: dict[str, list[TurnResult]],
) -> str:
    """Render the run-level roll-up across prompts."""
    lines = [
        f"# Recovery Harness Run — {run_id}",
        "",
        f"- profile tag: `{profile}`",
        f"- generated:   {datetime.now(timezone.utc).isoformat()}",
        f"- prompts run: {len(prompt_results)}",
        "",
        "| prompt | turns | entities | relationships | logs (sum) |",
        "|---|---:|---:|---:|---:|",
    ]
    for pid, results in prompt_results.items():
        ent = sum(r.neo4j_entity_count for r in results)
        rel = sum(r.neo4j_relationship_count for r in results)
        logs = sum(len(r.es_hits) for r in results)
        lines.append(f"| `{pid}` | {len(results)} | {ent} | {rel} | {logs} |")
    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Top-level driver
# ---------------------------------------------------------------------------


async def run_prompt(
    prompt: PromptDef,
    *,
    chat_url: str,
    auth_email: str | None,
    profile: str = "local",
    skill_routing_mode: str | None = None,
    hard_timeout: float = _DEFAULT_HARD_TIMEOUT,
    queries: TelemetryQueries,
    memory_service: MemoryService,
    out_dir: Path,
) -> list[TurnResult]:
    """Execute one prompt's turns and write its per-prompt report."""
    results: list[TurnResult] = []
    session_id: str | None = None
    async with httpx.AsyncClient() as client:
        for turn in prompt.turns:
            if turn.new_session:
                session_id = None
            started_at = datetime.now(timezone.utc)
            response_text, session_id, trace_id = await call_chat(
                client,
                chat_url,
                turn.message,
                session_id,
                auth_email,
                profile=profile,
                skill_routing_mode=skill_routing_mode,
            )
            finished_at = datetime.now(timezone.utc)
            log.info(
                "harness_turn_done",
                prompt=prompt.id,
                session_id=session_id,
                trace_id=trace_id,
            )
            await _wait_for_trace_complete(queries, trace_id, started_at, hard_timeout=hard_timeout)
            polled_at = datetime.now(timezone.utc)
            es_hits = await fetch_trace_logs(
                queries,
                trace_id,
                started_at - timedelta(seconds=2),
                polled_at + timedelta(seconds=5),
            )
            n4_turn, n4_ent, n4_rel = await fetch_neo4j_for_trace(memory_service, trace_id)
            results.append(
                TurnResult(
                    message=turn.message,
                    session_id=session_id,
                    trace_id=trace_id,
                    response_text=response_text,
                    started_at=started_at,
                    finished_at=finished_at,
                    es_hits=es_hits,
                    neo4j_turn_count=n4_turn,
                    neo4j_entity_count=n4_ent,
                    neo4j_relationship_count=n4_rel,
                )
            )
    prompt_dir = out_dir / prompt.id
    prompt_dir.mkdir(parents=True, exist_ok=True)
    (prompt_dir / "report.md").write_text(render_turn_report(prompt, results))
    (prompt_dir / "raw.json").write_text(
        json.dumps(
            [
                {
                    "message": r.message,
                    "session_id": r.session_id,
                    "trace_id": r.trace_id,
                    "response_text": r.response_text,
                    "started_at": r.started_at.isoformat(),
                    "finished_at": r.finished_at.isoformat(),
                    "es_hits": r.es_hits,
                    "neo4j_turn_count": r.neo4j_turn_count,
                    "neo4j_entity_count": r.neo4j_entity_count,
                    "neo4j_relationship_count": r.neo4j_relationship_count,
                }
                for r in results
            ],
            indent=2,
            default=str,
        )
    )
    return results


async def run_harness(
    *,
    run_id: str,
    profile: str,
    skill_routing_mode: str | None = None,
    prompts_path: Path,
    chat_url: str,
    auth_email: str | None,
    hard_timeout: float = _DEFAULT_HARD_TIMEOUT,
    only_prompt: str | None,
    out_dir: Path,
) -> Path:
    """Run all selected prompts and write per-prompt reports + summary."""
    out_dir.mkdir(parents=True, exist_ok=True)
    all_prompts = load_prompts(prompts_path)
    if only_prompt:
        all_prompts = [p for p in all_prompts if p.id == only_prompt]
        if not all_prompts:
            raise ValueError(f"No prompt with id={only_prompt!r} in {prompts_path}")

    queries = TelemetryQueries()
    memory_service = (
        MemoryService()
    )  # fre-375-allow: read-only MemoryService, post-FRE-375 settings-driven
    try:
        await memory_service.connect()
        prompt_results: dict[str, list[TurnResult]] = {}
        prompt_errors: dict[str, str] = {}
        for prompt in all_prompts:
            log.info("harness_prompt_start", prompt=prompt.id)
            try:
                results = await run_prompt(
                    prompt,
                    chat_url=chat_url,
                    auth_email=auth_email,
                    profile=profile,
                    skill_routing_mode=skill_routing_mode,
                    hard_timeout=hard_timeout,
                    queries=queries,
                    memory_service=memory_service,
                    out_dir=out_dir,
                )
                prompt_results[prompt.id] = results
            except Exception as exc:  # noqa: BLE001 — per-prompt isolation
                err = f"{type(exc).__name__}: {exc}"
                log.error("harness_prompt_failed", prompt=prompt.id, error=err)
                prompt_errors[prompt.id] = err
                prompt_results[prompt.id] = []
                # Write a stub report so reviewers see the failure inline.
                prompt_dir = out_dir / prompt.id
                prompt_dir.mkdir(parents=True, exist_ok=True)
                (prompt_dir / "report.md").write_text(
                    f"# Prompt: `{prompt.id}` — FAILED\n\n{err}\n"
                )
        summary_path = out_dir / "summary.md"
        summary_path.write_text(
            render_summary(run_id=run_id, profile=profile, prompt_results=prompt_results)
        )
        log.info("harness_done", summary=str(summary_path))
        return summary_path
    finally:
        await queries.disconnect()
        await memory_service.disconnect()


def parse_args() -> argparse.Namespace:
    """Parse CLI arguments."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--run-id",
        required=True,
        help="Identifier for this run; used as the per-run output directory name.",
    )
    parser.add_argument(
        "--profile",
        default="baseline",
        help="Metadata tag for the run (e.g. 'baseline', 'recovery'). "
        "Wave 1 does not toggle behaviour from this flag.",
    )
    parser.add_argument(
        "--prompts",
        type=Path,
        default=DEFAULT_PROMPTS_PATH,
        help="Path to prompts.yaml.",
    )
    parser.add_argument(
        "--prompt",
        default=None,
        help="If set, run only the prompt with this id.",
    )
    parser.add_argument(
        "--chat-url",
        default=DEFAULT_CHAT_URL,
        help="POST endpoint for /chat (default: http://localhost:9000/chat).",
    )
    parser.add_argument(
        "--auth-email",
        default=None,
        help=(
            "Email to send as Cf-Access-Authenticated-User-Email when calling "
            "/chat. Required when gateway_auth_enabled=true and there is no "
            "CF Access proxy in front of the service. Defaults to "
            "settings.agent_owner_email when omitted."
        ),
    )
    parser.add_argument(
        "--skill-routing-mode",
        default=None,
        help="Override skill_routing_mode per-request (keyword|hybrid|model_decided). "
        "Allows running different eval cells without gateway restarts.",
    )
    parser.add_argument(
        "--es-wait-seconds",
        type=int,
        default=None,
        help=(
            "[DEPRECATED] Previously a fixed sleep before fetching trace logs. "
            "Now sets the polling hard-timeout (default: 30s). "
            "Omit to use the new polling default."
        ),
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help=f"Output dir. Defaults to {DEFAULT_OUT_BASE}/<run-id>.",
    )
    return parser.parse_args()


def main() -> int:
    """CLI entry point. Returns exit code."""
    args = parse_args()
    out_dir = args.out or DEFAULT_OUT_BASE / args.run_id
    auth_email = args.auth_email or get_settings().agent_owner_email

    hard_timeout = _DEFAULT_HARD_TIMEOUT
    if args.es_wait_seconds is not None:
        log.warning(
            "es_wait_seconds_deprecated",
            message=(
                "--es-wait-seconds is deprecated; it now sets the polling hard-timeout. "
                "Remove the flag to use the default of 30s."
            ),
            value=args.es_wait_seconds,
        )
        hard_timeout = float(args.es_wait_seconds)

    try:
        summary_path = asyncio.run(
            run_harness(
                run_id=args.run_id,
                profile=args.profile,
                skill_routing_mode=args.skill_routing_mode,
                prompts_path=args.prompts,
                chat_url=args.chat_url,
                auth_email=auth_email,
                hard_timeout=hard_timeout,
                only_prompt=args.prompt,
                out_dir=out_dir,
            )
        )
    except Exception as exc:  # noqa: BLE001 — CLI top-level
        log.error("harness_failed", error=str(exc), error_type=type(exc).__name__)
        return 1
    log.info("harness_done_cli", summary=str(summary_path))
    return 0


if __name__ == "__main__":
    sys.exit(main())
