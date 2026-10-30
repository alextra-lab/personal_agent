"""Native Perplexity AI tool — Phase 2 of CLI-First Tool Migration (ADR-0028).

Replaces three MCP tools (mcp_perplexity_ask, mcp_perplexity_reason,
mcp_perplexity_research) with a single in-process Python tool using the
Perplexity REST API (OpenAI-compatible chat completions endpoint).
"""

from __future__ import annotations

from typing import Any, Literal

import httpx

from personal_agent.config import settings
from personal_agent.security import EgressBlockedError, create_guarded_http_client
from personal_agent.telemetry import TraceContext, get_logger
from personal_agent.tools.contract import ToolError, error_codes
from personal_agent.tools.types import ToolDefinition, ToolParameter

log = get_logger(__name__)

PerplexityQueryCode = Literal[
    "invalid_argument",
    "invalid_mode",
    "not_configured",
    "egress_blocked",
    "upstream_unavailable",
    "upstream_timeout",
    "upstream_failed",
    "upstream_rejected",
    "bad_reply",
]
"""``perplexity_query`` failure codes (ADR-0156 D6.1)."""


class PerplexityQueryError(ToolError[PerplexityQueryCode]):
    """A ``perplexity_query`` failure."""


_RETRY_NEXT = "Call perplexity_query once more; if it fails again, use web_search instead."

# Perplexity model IDs per mode (see https://docs.perplexity.ai/guides/model-cards)
_MODE_TO_MODEL: dict[str, str] = {
    "ask": "sonar",
    "reason": "sonar-reasoning-pro",
    "research": "sonar-deep-research",
}

perplexity_query_tool = ToolDefinition(
    name="perplexity_query",
    description=(
        "Ask Perplexity AI a question and get a web-sourced synthesized answer with citation "
        "URLs. Use it when web_search snippets are not enough and you need a summary with "
        "traceable sources. Do not use it for routine factual lookups (use web_search: faster "
        "and private) or for library API docs (use get_library_docs). "
        "Modes: 'ask' (default, fast), 'reason' (retrieval plus multi-step analysis), "
        "'research' (deep multi-source survey, much slower and costlier — only for broad "
        "comparisons). The query is sent to an external service, and the answer can be wrong; "
        "check the citations. "
        'Examples: perplexity_query(query="What changed in Python 3.13\'s GIL handling?"), '
        'perplexity_query(query="Compare pgvector and Qdrant for 1M vectors", mode="reason").'
    ),
    category="network",
    parameters=[
        ToolParameter(
            name="query",
            type="string",
            description="Question or research prompt to send to Perplexity.",
            required=True,
            default=None,
            json_schema=None,
        ),
        ToolParameter(
            name="mode",
            type="string",
            description=(
                "Query mode: 'ask' (default, fast), 'reason' (analytical), "
                "or 'research' (deep, slow)."
            ),
            required=False,
            default=None,
            json_schema=None,
        ),
    ],
    risk_level="medium",
    allowed_modes=["NORMAL", "DEGRADED"],
    requires_approval=False,
    requires_sandbox=False,
    timeout_seconds=120,
    error_codes=error_codes(PerplexityQueryError),
    truncation_next_action="Ask a narrower question, or use mode='ask' instead of 'research'.",
)


async def perplexity_query_executor(
    query: str = "",
    mode: str | None = None,
    *,
    ctx: TraceContext,
) -> dict[str, Any]:
    """Execute a Perplexity AI query via the REST API.

    Args:
        query: Question or research prompt.
        mode: One of 'ask' (default), 'reason', 'research'.
        ctx: Trace context for structured logging.

    Returns:
        Dict with ``answer``, ``citations``, ``model``, and ``mode`` keys.

    Raises:
        PerplexityQueryError: When the query is empty, the mode is invalid, the API key is
            missing, the egress guard refuses the host, or the API call fails.
    """
    query = (query or "").strip()
    if not query:
        raise PerplexityQueryError(
            "invalid_argument",
            "'query' parameter is required and cannot be empty.",
            retryable=False,
            next_action="Call perplexity_query with a non-empty query.",
        )

    mode = (mode or "ask").strip().lower()
    if mode not in _MODE_TO_MODEL:
        raise PerplexityQueryError(
            "invalid_mode",
            f"Invalid mode '{mode}'. Must be one of: {', '.join(sorted(_MODE_TO_MODEL))}.",
            retryable=False,
            next_action="Call perplexity_query again with mode 'ask', 'reason' or 'research'.",
        )

    api_key = settings.perplexity_api_key
    if not api_key:
        raise PerplexityQueryError(
            "not_configured",
            "Perplexity API key not configured. Set AGENT_PERPLEXITY_API_KEY in your .env file.",
            retryable=False,
            next_action="Perplexity is unavailable on this deployment. Use web_search instead.",
        )

    model = _MODE_TO_MODEL[mode]
    trace_id = ctx.trace_id
    session_id = ctx.session_id

    log.info(
        "perplexity_query_started",
        trace_id=trace_id,
        mode=mode,
        model=model,
        query_preview=query[:120],
    )

    payload: dict[str, Any] = {
        "model": model,
        "messages": [{"role": "user", "content": query}],
    }

    try:
        async with create_guarded_http_client(
            timeout=settings.perplexity_timeout_seconds,
        ) as client:
            resp = await client.post(
                f"{settings.perplexity_base_url}/chat/completions",
                json=payload,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
            )
            if resp.is_error:
                try:
                    body = resp.json()
                    reason = body.get("error", {}).get("message") or str(body)
                except Exception:
                    reason = resp.text[:500]
                msg = f"Perplexity API returned HTTP {resp.status_code}: {reason}"
                log.error(
                    "perplexity_query_http_error",
                    trace_id=trace_id,
                    session_id=session_id,
                    status=resp.status_code,
                )
                transient = resp.status_code == 429 or resp.status_code >= 500
                raise PerplexityQueryError(
                    "upstream_rejected",
                    msg,
                    retryable=transient,
                    next_action=(
                        _RETRY_NEXT
                        if transient
                        else "Do not repeat this call. Use web_search, or tell the user."
                    ),
                )

            data = resp.json()

    except EgressBlockedError as exc:
        log.error(
            "perplexity_query_egress_blocked",
            trace_id=trace_id,
            session_id=session_id,
            reason=exc.reason,
        )
        raise PerplexityQueryError(
            "egress_blocked",
            f"Perplexity API call refused by egress policy: {exc.reason}",
            retryable=False,
            next_action="Perplexity cannot be reached from here. Use web_search instead.",
        ) from exc
    except httpx.ConnectError as exc:
        msg = f"Cannot connect to Perplexity API at {settings.perplexity_base_url}."
        log.error(
            "perplexity_query_connect_failed", trace_id=trace_id, session_id=session_id, error=msg
        )
        raise PerplexityQueryError(
            "upstream_unavailable", msg, retryable=True, next_action=_RETRY_NEXT
        ) from exc
    except httpx.TimeoutException as exc:
        msg = f"Perplexity API request timed out after {settings.perplexity_timeout_seconds}s."
        log.error("perplexity_query_timeout", trace_id=trace_id, session_id=session_id)
        raise PerplexityQueryError(
            "upstream_timeout", msg, retryable=True, next_action=_RETRY_NEXT
        ) from exc
    except httpx.HTTPError as exc:
        log.error(
            "perplexity_query_failed",
            trace_id=trace_id,
            session_id=session_id,
            mode=mode,
            error=str(exc),
            exc_info=True,
        )
        raise PerplexityQueryError(
            "upstream_failed",
            f"Perplexity API request failed: {exc}",
            retryable=True,
            next_action=_RETRY_NEXT,
        ) from exc
    except ValueError as exc:
        log.error(
            "perplexity_query_bad_reply",
            trace_id=trace_id,
            session_id=session_id,
            mode=mode,
            error=str(exc),
        )
        raise PerplexityQueryError(
            "bad_reply",
            "Perplexity API returned a reply that is not JSON.",
            retryable=True,
            next_action=_RETRY_NEXT,
        ) from exc

    if not isinstance(data, dict):
        raise PerplexityQueryError(
            "bad_reply",
            "Perplexity API returned a reply that is not a JSON object.",
            retryable=True,
            next_action=_RETRY_NEXT,
        )

    choices = data.get("choices") or []
    answer = choices[0].get("message", {}).get("content", "") if choices else ""
    citations: list[str] = data.get("citations") or []

    log.info(
        "perplexity_query_completed",
        trace_id=trace_id,
        mode=mode,
        model=model,
        citation_count=len(citations),
    )

    return {
        "answer": answer,
        "citations": citations,
        "model": model,
        "mode": mode,
    }
