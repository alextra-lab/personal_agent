"""Context compressor — summarize evicted conversation turns.

Replaces the static ``[Earlier messages truncated]`` marker with a structured
summary of evicted messages, preserving key decisions, entities, and facts.

Uses a lightweight compressor model (ADR-0038) to generate concise summaries
that fit within a bounded token budget.

ADR-0061 layers a deterministic ``_pre_pass_tool_outputs`` helper *before*
the LLM call: large ``role="tool"`` payloads are replaced with 1-line JSON
descriptors (preserving ``tool_call_id`` for assistant↔tool pair sanity).
``summarize_middle`` returns the post-pre-pass summary together with the
stats the within-session caller needs.
"""

from __future__ import annotations

import json
import time
from typing import Any

from personal_agent.config import load_model_config
from personal_agent.llm_client.factory import get_llm_client
from personal_agent.llm_client.types import LLMClientError, ModelRole
from personal_agent.orchestrator.context_window import estimate_message_tokens
from personal_agent.telemetry import get_logger

log = get_logger(__name__)

FALLBACK_MARKER = "[Earlier messages truncated]"

_compressor_role_missing_logged: bool = False

_COMPRESSOR_SYSTEM_PROMPT = """\
You are a context compressor. Given a sequence of conversation messages that \
are being evicted from the context window, produce a concise structured summary \
that preserves the most important information for continuing the conversation.

Output format (use exactly these headings):
## Conversation Summary
- **Decisions:** Bullet list of decisions made (empty if none)
- **Entities:** Key names, tools, technologies, people mentioned
- **Facts:** Important facts established during the conversation
- **Open Items:** Unresolved questions or pending actions

Rules:
- Maximum 200 words total
- Only include information actually present in the messages
- Prefer specifics over generalities (names, versions, choices)
- Skip pleasantries and meta-conversation
- Each bullet should stand on its own — a downstream reader will not see the
  original messages
- Preserve identifiers (file paths, ticket IDs, function names, model IDs,
  hostnames, ports) verbatim — they are how the next turn re-grounds itself
- Tool outputs may be truncated upstream; if you encounter an "[Earlier \
messages truncated]" marker or an existing "## Conversation Summary"
  block, fold that prior summary into the new one rather than re-summarising
  raw text that no longer exists

Worked example 1 — coding session:
  Input (paraphrased):
    [user]: We're deciding between SQLite and Postgres for the metrics store.
    [assistant]: How many writes/sec do you expect, and is concurrent
                 read/write a hard requirement?
    [user]: ~50/sec peak, multiple concurrent readers, single writer.
    [assistant]: Postgres is the right call — SQLite WAL handles concurrent
                 readers fine but multiple-writer concurrency requires
                 server-grade locking, and you'll want pg_stat_statements
                 from day one.
    [tool: ran ALTER TABLE …]: ok
  Output:
    ## Conversation Summary
    - **Decisions:** Use Postgres (not SQLite) for the metrics store; enable
      pg_stat_statements from day one.
    - **Entities:** Postgres, SQLite, WAL, pg_stat_statements, metrics store.
    - **Facts:** Expected load ~50 writes/sec peak, multiple concurrent
      readers, single writer. ALTER TABLE applied successfully.
    - **Open Items:** None.

Worked example 2 — debugging session:
  Input (paraphrased):
    [user]: The /chat endpoint is returning 504 after 30s when the prompt
            exceeds ~10k tokens.
    [assistant]: That looks like the gateway timeout, not the LLM. Check
                 the proxy.
    [tool: curl /chat]: HTTP 504 after 30.1s
    [assistant]: Confirmed — Caddy's default upstream timeout is 30s.
                 Raise it on the /chat path.
  Output:
    ## Conversation Summary
    - **Decisions:** Raise Caddy upstream timeout on the /chat path; root
      cause is gateway, not LLM.
    - **Entities:** /chat endpoint, Caddy reverse proxy, 504 status.
    - **Facts:** 504 reproduces at ~30s when prompt exceeds ~10k tokens;
      Caddy default upstream_timeout=30s.
    - **Open Items:** Apply the timeout bump and re-test.

Worked example 3 — tool-heavy session with prior summary:
  Input (paraphrased):
    [system]: ## Conversation Summary
              - **Decisions:** Use Neo4j 5.26 LTS for the knowledge graph.
              - **Entities:** Neo4j 5.26 LTS, pgvector.
              - **Facts:** pgvector deferred to phase 2.
              - **Open Items:** Choose embedding dimension.
    [user]: Let's go with 768d embeddings — matches Qwen3-Embedding-0.6B.
    [tool: edit docker-compose.yml]: ok
    [assistant]: 768d set. Embedding service rebuilt.
  Output:
    ## Conversation Summary
    - **Decisions:** Use Neo4j 5.26 LTS for the knowledge graph; embedding
      dimension is 768 to match Qwen3-Embedding-0.6B.
    - **Entities:** Neo4j 5.26 LTS, pgvector, Qwen3-Embedding-0.6B,
      docker-compose.yml.
    - **Facts:** pgvector deferred to phase 2; embedding service rebuilt.
    - **Open Items:** None pending from this slice.

Anti-patterns to avoid:
- Do not invent details that are not in the messages — if a decision was
  discussed but not concluded, list it under Open Items, not Decisions.
- Do not summarise meta-conversation ("I will now check…", "let me think
  about this…") — only the substantive turn output matters.
- Do not collapse distinct entities into a generic term ("the database",
  "the model") — name them.
- Do not write narrative prose under the headings — every line is a bullet.
- Do not include this instruction text or any system metadata in the
  output. Output only the four-section summary."""


async def compress_turns(
    evicted_messages: list[dict[str, Any]],
    trace_id: str = "",
    session_id: str | None = None,
) -> str:
    """Compress evicted conversation turns into a structured summary.

    Calls the compressor LLM to extract key information from messages being
    dropped from the context window. Falls back to the static truncation
    marker on any failure.

    Args:
        evicted_messages: Messages being evicted from the context window.
        trace_id: Request trace identifier for telemetry.
        session_id: Originating session id for cost attribution (ADR-0074).

    Returns:
        Structured summary string, or the fallback marker on failure.
    """
    if not evicted_messages:
        return FALLBACK_MARKER

    config = load_model_config()
    if "compressor" not in config.models:
        global _compressor_role_missing_logged
        if not _compressor_role_missing_logged:
            log.warning(
                "context_compressor_role_missing",
                fallback="static_marker",
                trace_id=trace_id,
                remedy="Add 'compressor' role to active models.yaml to enable summarisation",
            )
            _compressor_role_missing_logged = True
        return FALLBACK_MARKER

    start_ms = time.monotonic() * 1000
    formatted = _format_messages_for_compression(evicted_messages)

    try:
        from personal_agent.telemetry.trace import SystemTraceContext, TraceContext

        compress_ctx: TraceContext = (
            TraceContext(trace_id=trace_id, session_id=session_id)
            if trace_id
            else SystemTraceContext.new("context_compressor", session_id=session_id)
        )
        client = get_llm_client(role_name="compressor")
        response = await client.respond(
            role=ModelRole.COMPRESSOR,
            messages=[
                {"role": "system", "content": _COMPRESSOR_SYSTEM_PROMPT},
                {"role": "user", "content": formatted},
            ],
            max_tokens=320,  # ≤200 words ≈ 260-300 tokens; 320 enforces the prompt cap (FRE-576 F5)
            temperature=0.2,
            timeout_s=25.0,
            trace_ctx=compress_ctx,
        )

        summary = str(response.get("content", "")).strip()
        if not summary:
            log.warning(
                "context_compression_empty_response",
                evicted_count=len(evicted_messages),
                trace_id=trace_id,
            )
            return FALLBACK_MARKER

        duration_ms = time.monotonic() * 1000 - start_ms
        summary_tokens = max(1, len(summary) // 4)

        log.info(
            "context_compression_completed",
            evicted_count=len(evicted_messages),
            summary_tokens=summary_tokens,
            duration_ms=round(duration_ms),
            trace_id=trace_id,
        )
        return summary

    except LLMClientError as exc:
        duration_ms = time.monotonic() * 1000 - start_ms
        log.warning(
            "context_compression_failed",
            error=str(exc),
            error_type=type(exc).__name__,
            evicted_count=len(evicted_messages),
            duration_ms=round(duration_ms),
            trace_id=trace_id,
        )
        return FALLBACK_MARKER
    except Exception as exc:
        duration_ms = time.monotonic() * 1000 - start_ms
        log.warning(
            "context_compression_failed",
            error=str(exc),
            error_type=type(exc).__name__,
            evicted_count=len(evicted_messages),
            duration_ms=round(duration_ms),
            trace_id=trace_id,
        )
        return FALLBACK_MARKER


def _format_messages_for_compression(
    messages: list[dict[str, Any]],
) -> str:
    """Format messages into a text block for the compressor prompt.

    Args:
        messages: OpenAI-style message dicts to format.

    Returns:
        Formatted text block with role labels.
    """
    parts: list[str] = []
    for msg in messages:
        role = msg.get("role", "unknown")
        content = msg.get("content", "")
        if not content:
            continue
        if isinstance(content, str):
            parts.append(f"[{role}]: {content}")
        else:
            parts.append(f"[{role}]: {content!s}")
    return "\n\n".join(parts)


# ---------------------------------------------------------------------------
# ADR-0061 — Pre-pass + summariser used by within-session compression
# ---------------------------------------------------------------------------


_ERROR_MARKERS = ('"error"', '"status": "error"', '"status":"error"')


def _content_is_error_payload(content: str) -> bool:
    """Return True when *content* looks like a tool error JSON.

    Tool errors are kept verbatim through the pre-pass so the compressor LLM
    can incorporate the failure into Decisions/Open Items.  Heuristic only —
    matches the JSON-shaped error markers used elsewhere in the codebase
    (see ``orchestrator/context_window.py:_is_tool_error_message``).
    """
    lowered = content.lower()
    return any(marker in lowered for marker in _ERROR_MARKERS)


def _shape_descriptor(content: str) -> str:
    """Return a short shape descriptor for a tool ``content`` payload.

    Tries JSON parse; on success returns sorted top-level keys (dict) or
    ``list[N]`` (list).  On failure, returns the first 120 chars of
    ``repr(content)`` with newlines collapsed.
    """
    try:
        parsed = json.loads(content)
    except (TypeError, ValueError):
        compact = " ".join(content.split())
        return compact[:120]
    if isinstance(parsed, dict):
        keys = sorted(str(k) for k in parsed.keys())
        return "keys=" + ",".join(keys[:10]) + ("…" if len(keys) > 10 else "")
    if isinstance(parsed, list):
        return f"list[{len(parsed)}]"
    return repr(parsed)[:120]


def _pre_pass_tool_outputs(
    middle: list[dict[str, Any]],
    *,
    threshold_tokens: int,
) -> tuple[list[dict[str, Any]], int]:
    """Replace large tool messages in *middle* with 1-line descriptors.

    Per ADR-0061 §D4 — runs before the LLM summariser so the compressor
    pays for conversational content, not raw tool bodies.  Assistant
    messages, system markers, user messages, and small / error tool
    messages pass through unchanged.

    The replacement preserves ``tool_call_id`` so
    ``orchestrator.context_window._sanitize_tool_pairs`` does not later
    drop the matching assistant ``tool_calls`` block.

    Args:
        middle: Middle-band messages between head and tail.  Not mutated;
            a new list is returned.
        threshold_tokens: Per-message size threshold.  Tool messages with
            an estimated token count below this are kept verbatim.

    Returns:
        ``(rewritten_middle, replacement_count)``.
    """
    rewritten: list[dict[str, Any]] = []
    replacements = 0
    for msg in middle:
        if msg.get("role") != "tool":
            rewritten.append(msg)
            continue
        content = msg.get("content", "")
        if not isinstance(content, str) or not content:
            rewritten.append(msg)
            continue
        if estimate_message_tokens(msg) < threshold_tokens:
            rewritten.append(msg)
            continue
        if _content_is_error_payload(content):
            rewritten.append(msg)
            continue

        descriptor_payload = {
            "_replaced": True,
            "tool_call_id": msg.get("tool_call_id", ""),
            "size_chars": len(content),
            "shape": _shape_descriptor(content),
        }
        replaced = dict(msg)
        replaced["content"] = json.dumps(descriptor_payload, sort_keys=True)
        rewritten.append(replaced)
        replacements += 1

    return rewritten, replacements


async def summarize_middle(
    middle: list[dict[str, Any]],
    *,
    trace_id: str = "",
    session_id: str | None = None,
) -> tuple[str, int]:
    """Run the LLM compressor on *middle* and return ``(summary, duration_ms)``.

    Thin wrapper around :func:`compress_turns` that exposes the wall time
    used by the compressor call so the within-session record can include
    ``summariser_duration_ms``.  On compressor failure the wrapper returns
    the same fallback marker ``compress_turns`` would and a duration of 0.

    Args:
        middle: Middle-band messages (already pre-passed) to summarise.
        trace_id: Request trace identifier for telemetry.
        session_id: Originating session id for cost attribution (ADR-0074).

    Returns:
        Tuple of ``(summary, duration_ms)``.  ``duration_ms`` is 0 when the
        summariser was skipped (empty input or compressor role missing) or
        when the call failed before producing a summary.
    """
    if not middle:
        return FALLBACK_MARKER, 0
    started = time.monotonic()
    summary = await compress_turns(middle, trace_id=trace_id, session_id=session_id)
    duration_ms = int(round((time.monotonic() - started) * 1000))
    if summary == FALLBACK_MARKER:
        return summary, 0
    return summary, duration_ms
