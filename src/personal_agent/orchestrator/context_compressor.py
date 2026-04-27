"""Context compressor — summarize evicted conversation turns.

Replaces the static ``[Earlier messages truncated]`` marker with a structured
summary of evicted messages, preserving key decisions, entities, and facts.

Uses a lightweight compressor model (ADR-0038) to generate concise summaries
that fit within a bounded token budget.
"""

from __future__ import annotations

import time
from typing import Any

from personal_agent.config import load_model_config
from personal_agent.llm_client.factory import get_llm_client
from personal_agent.llm_client.types import LLMClientError, ModelRole
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
- Skip pleasantries and meta-conversation"""


async def compress_turns(
    evicted_messages: list[dict[str, Any]],
    trace_id: str = "",
) -> str:
    """Compress evicted conversation turns into a structured summary.

    Calls the compressor LLM to extract key information from messages being
    dropped from the context window. Falls back to the static truncation
    marker on any failure.

    Args:
        evicted_messages: Messages being evicted from the context window.
        trace_id: Request trace identifier for telemetry.

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
        client = get_llm_client(role_name="compressor")
        response = await client.respond(
            role=ModelRole.COMPRESSOR,
            messages=[
                {"role": "system", "content": _COMPRESSOR_SYSTEM_PROMPT},
                {"role": "user", "content": formatted},
            ],
            max_tokens=512,
            temperature=0.2,
            timeout_s=25.0,
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
