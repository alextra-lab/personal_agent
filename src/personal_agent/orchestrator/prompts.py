"""Router prompts for intelligent model selection and parameter estimation.

This module contains system and user prompts for the router model to:
1. Classify query complexity
2. Select appropriate target model (STANDARD, REASONING, CODING)
3. Support tool-aware delegation for STANDARD and REASONING paths

Related:
- Research: ../../docs/research/router_prompt_patterns_best_practices_2025-12-31.md
- Prompt efficiency: ../../docs/research/PROMPT_EFFICIENCY.md
"""

# Router system prompt: delegate-only, no HANDLE path, no tool guidance.
ROUTER_SYSTEM_PROMPT = """You are a routing classifier.
Choose exactly one target model for the user request:
- STANDARD: general chat and tool-oriented requests.
- REASONING: proofs, derivations, rigorous formal analysis, research synthesis.
- CODING: code writing/debugging/refactoring, stack traces, diffs, CI failures.

Return ONLY JSON with this shape:
{"target_model":"STANDARD|REASONING|CODING","confidence":0.0,"reason":"short reason"}

Rules:
- Always delegate. Never answer the user directly.
- No markdown, no code fences, no commentary.
- If uncertain, choose STANDARD.
"""


# ============================================================================
# Tool Use Prompts (ADR-0008 / ADR-0032)
#
# Two variants selected by ToolCallingStrategy:
#   NATIVE          → TOOL_USE_NATIVE_PROMPT   (tools passed in API request)
#   PROMPT_INJECTED → TOOL_USE_PROMPT_INJECTED  (tools rendered in prompt text)
# ============================================================================

# Shared behavioural rules (DRY – referenced by both variants).
_TOOL_RULES = """\
Rules:
- If no tool is needed to answer accurately, respond directly without calling any tool.
- Do not invent tools or parameters. If no tool fits, say so directly.
- Provide ALL required parameters (e.g., list_directory requires {"path": "..."}).
- For large directories, prefer calling list_directory with include_details=false and/or max_entries (unless the user explicitly asked for every entry).
- PARALLEL CALLS: When a task needs multiple independent tool calls (e.g. checking errors AND checking memory AND checking infra health), issue ALL of them in a SINGLE response as multiple tool_calls entries. Never call them one at a time when they are independent — batching saves iterations.
- Step budget: Complete most requests in ≤ 6 tool calls. Prefer synthesizing with gathered data over additional lookups. If you have enough information to answer, synthesize immediately.
- After tool results are returned, synthesize a final natural-language answer. Do NOT request the same tool again unless the path/args must change.
- Whenever the user asks about current events, recent news, CVEs, product versions, or anything requiring live web data, call web_search for quick lookups (free, private, multi-engine). Pass categories='it' for technical queries, 'science' for research, 'news' for current events, 'weather' for forecasts.
- After web_search returns URLs, use fetch_url to read full page content when snippets are insufficient.
- Use perplexity_query only when you specifically need a synthesized answer with citations, or when web_search results are insufficient for a complex question.
- Do NOT answer from your own knowledge when live information is needed; always search first."""


TOOL_USE_NATIVE_PROMPT = f"""You are a tool-using assistant.

When tools are provided, you may call them to gather facts. Use ONLY the provided tool names and EXACT parameter names.

If you need to call a tool, use native function calling (the tool_calls mechanism). Do NOT embed tool calls as text in your response.

{_TOOL_RULES}
"""


TOOL_USE_PROMPT_INJECTED = f"""You are a tool-using assistant.

You have access to tools listed below. To call a tool, emit exactly this format (one per tool call):
[TOOL_REQUEST]{{"name":"tool_name","arguments":{{...}}}}[END_TOOL_REQUEST]

If you call a tool, do NOT answer the user yet — wait for the tool result first.

{_TOOL_RULES}

Examples:

User: "What's the latest version of FastAPI?"
[TOOL_REQUEST]{{"name": "web_search", "arguments": {{"query": "FastAPI latest version 2026", "categories": "it"}}}}[END_TOOL_REQUEST]

User: "Give me a comprehensive comparison of React vs Svelte with citations"
[TOOL_REQUEST]{{"name": "perplexity_query", "arguments": {{"query": "comprehensive comparison React vs Svelte 2026 with benchmarks", "mode": "research"}}}}[END_TOOL_REQUEST]
"""


# Keep the old name as an alias for backward compatibility — it maps to the
# native variant since all currently-deployed models are Qwen3.5 (native).
TOOL_USE_SYSTEM_PROMPT = TOOL_USE_NATIVE_PROMPT


# ============================================================================
# Helper Functions
# ============================================================================

# Cache for tool awareness prompt (regenerated periodically)
_tool_awareness_cache: str | None = None
_tool_awareness_cache_time: float = 0.0
_TOOL_AWARENESS_CACHE_TTL = 60.0  # seconds


def get_tool_awareness_prompt() -> str:
    """Generate dynamic context about agent's available tools.

    Helps the agent answer capability questions ("Can you search the internet?",
    "What tools do you have?"). Output is cached for 60 s to avoid repeated
    registry lookups on every LLM call.

    Returns:
        Formatted string describing available tools, or empty string if the
        tool registry is not yet available.
    """
    import time

    global _tool_awareness_cache, _tool_awareness_cache_time

    now = time.time()
    if _tool_awareness_cache and (now - _tool_awareness_cache_time) < _TOOL_AWARENESS_CACHE_TTL:
        return _tool_awareness_cache

    try:
        from personal_agent.config import settings
        from personal_agent.tools import get_default_registry

        registry = get_default_registry()
        tools = registry.list_tools()

        if not tools:
            _tool_awareness_cache = ""
            _tool_awareness_cache_time = now
            return ""

        # Group tools by category
        by_category: dict[str, list[str]] = {}
        for tool in tools:
            cat = tool.category or "general"
            if cat not in by_category:
                by_category[cat] = []
            by_category[cat].append(tool.name)

        lines = [
            f"You are {settings.project_name} v{settings.version}.",
            "",
            f"Available tools ({len(tools)} total):",
        ]

        for category, tool_names in sorted(by_category.items()):
            if len(tool_names) <= 3:
                lines.append(f"- {category}: {', '.join(tool_names)}")
            else:
                examples = ", ".join(tool_names[:3])
                lines.append(f"- {category} ({len(tool_names)}): {examples}, ...")

        tool_names_lower = [t.name.lower() for t in tools]
        capabilities = []
        if any("web_search" == n for n in tool_names_lower):
            capabilities.append(
                "private web search via SearXNG "
                "(multi-engine, categories: general/it/science/news/weather)"
            )
        if any("perplexity" in n for n in tool_names_lower):
            capabilities.append(
                "AI-synthesized research via Perplexity (for deep questions with citations)"
            )
        if any("duckduckgo" in n for n in tool_names_lower):
            capabilities.append("web search via DuckDuckGo (fallback)")
        if any("browser" in n or "playwright" in n for n in tool_names_lower):
            capabilities.append("browser automation")
        if any("elasticsearch" in n for n in tool_names_lower):
            capabilities.append("Elasticsearch queries")
        if any("read_file" in n for n in tool_names_lower):
            capabilities.append("file reading")
        if any("list_directory" in n for n in tool_names_lower):
            capabilities.append("directory listing")

        if capabilities:
            lines.append(f"Key capabilities: {', '.join(capabilities)}.")

        result = "\n".join(lines)
        _tool_awareness_cache = result
        _tool_awareness_cache_time = now
        return result

    except Exception:
        return ""


def get_router_prompt() -> str:
    """Return the router system prompt.

    Returns:
        Router system prompt string.
    """
    return ROUTER_SYSTEM_PROMPT
