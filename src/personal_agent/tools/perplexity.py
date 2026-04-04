"""Native Perplexity AI tool — Phase 2 of CLI-First Tool Migration (ADR-0028).

Replaces three MCP tools (mcp_perplexity_ask, mcp_perplexity_reason,
mcp_perplexity_research) with a single in-process Python tool using the
Perplexity REST API (OpenAI-compatible chat completions endpoint).
"""

from __future__ import annotations

from typing import Any

import httpx

from personal_agent.config import settings
from personal_agent.telemetry import TraceContext, get_logger
from personal_agent.tools.executor import ToolExecutionError
from personal_agent.tools.types import ToolDefinition, ToolParameter

log = get_logger(__name__)

# Perplexity model IDs per mode (see https://docs.perplexity.ai/guides/model-cards)
_MODE_TO_MODEL: dict[str, str] = {
    "ask": "sonar",
    "reason": "sonar-reasoning",
    "research": "sonar-deep-research",
}

perplexity_query_tool = ToolDefinition(
    name="perplexity_query",
    description=(
        "Query Perplexity AI for synthesized answers with citations sourced from the web. "
        "Three modes:\n"
        "- 'ask' (default): Fast, synthesized answer with inline citations. "
        "Use for technical questions, explanations, or when web_search snippets are insufficient.\n"
        "- 'reason': Combines live web research with logical analysis. "
        "Use when the question requires both information retrieval AND multi-step reasoning.\n"
        "- 'research': Deep investigation from many sources. "
        "Significantly slower. Use only for comprehensive surveys or multi-vendor comparisons.\n"
        "For routine factual lookups, prefer web_search (faster, private). "
        "Use Perplexity when you need synthesized summaries with traceable citations."
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
    rate_limit_per_hour=50,
)


async def perplexity_query_executor(
    query: str = "",
    mode: str | None = None,
    ctx: TraceContext | None = None,
) -> dict[str, Any]:
    """Execute a Perplexity AI query via the REST API.

    Args:
        query: Question or research prompt.
        mode: One of 'ask' (default), 'reason', 'research'.
        ctx: Optional trace context for structured logging.

    Returns:
        Dict with ``answer``, ``citations``, ``model``, and ``mode`` keys.

    Raises:
        ToolExecutionError: When the API key is missing, the query is empty,
            the mode is invalid, or the API call fails.
    """
    query = (query or "").strip()
    if not query:
        raise ToolExecutionError("'query' parameter is required and cannot be empty.")

    mode = (mode or "ask").strip().lower()
    if mode not in _MODE_TO_MODEL:
        raise ToolExecutionError(
            f"Invalid mode '{mode}'. Must be one of: {', '.join(sorted(_MODE_TO_MODEL))}."
        )

    api_key = settings.perplexity_api_key
    if not api_key:
        raise ToolExecutionError(
            "Perplexity API key not configured. Set AGENT_PERPLEXITY_API_KEY in your .env file."
        )

    model = _MODE_TO_MODEL[mode]
    trace_id = getattr(ctx, "trace_id", "unknown") if ctx else "unknown"

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
        async with httpx.AsyncClient(
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
                log.error("perplexity_query_http_error", trace_id=trace_id, status=resp.status_code)
                raise ToolExecutionError(msg)

            data = resp.json()

    except ToolExecutionError:
        raise
    except httpx.ConnectError as exc:
        msg = f"Cannot connect to Perplexity API at {settings.perplexity_base_url}."
        log.error("perplexity_query_connect_failed", trace_id=trace_id, error=msg)
        raise ToolExecutionError(msg) from exc
    except httpx.TimeoutException as exc:
        msg = f"Perplexity API request timed out after {settings.perplexity_timeout_seconds}s."
        log.error("perplexity_query_timeout", trace_id=trace_id)
        raise ToolExecutionError(msg) from exc
    except Exception as exc:
        log.error(
            "perplexity_query_failed",
            trace_id=trace_id,
            mode=mode,
            error=str(exc),
            exc_info=True,
        )
        raise ToolExecutionError(str(exc)) from exc

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
