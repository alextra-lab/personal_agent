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
# Tool Use Prompt (Hybrid Tool Calling Strategy - ADR-0008)
# ============================================================================

TOOL_USE_SYSTEM_PROMPT = """You are a tool-using assistant.

When tools are provided, you may call them to gather facts. Use ONLY the provided tool names and EXACT parameter names.

- If you call a tool, do NOT answer the user yet. Instead, emit a tool call:
  - Preferred: native function calling (tool_calls)
  - Fallback (text): [TOOL_REQUEST]{"name":"tool_name","arguments":{...}}[END_TOOL_REQUEST]

Rules:
- If no tool is needed to answer accurately, respond directly without calling any tool.
- Do not invent tools or parameters. If no tool fits, say so directly.
- Provide ALL required parameters (e.g., list_directory requires {"path": "..."}).
- For large directories, prefer calling list_directory with include_details=false and/or max_entries (unless the user explicitly asked for every entry).
- After tool results are returned, synthesize a final natural-language answer. Do NOT request the same tool again unless the path/args must change.
- Whenever the user asks about current events, recent news, CVEs, product versions, or anything requiring live web data, always call mcp_perplexity_ask instead of answering from your own knowledge.

Examples:

User: "What CVEs affect OpenSSH this month?"
[TOOL_REQUEST]{"name": "mcp_perplexity_ask", "arguments": {"messages": [{"role": "user", "content": "CVEs affecting OpenSSH this month"}]}}[END_TOOL_REQUEST]

User: "Give me a comprehensive survey of all zero-trust network access vendors and their market positions."
[TOOL_REQUEST]{"name": "mcp_perplexity_research", "arguments": {"messages": [{"role": "user", "content": "comprehensive survey of zero-trust network access vendors and market positions"}]}}[END_TOOL_REQUEST]
"""


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
        if any("perplexity" in n for n in tool_names_lower):
            capabilities.append("internet search via Perplexity")
        if any("duckduckgo" in n for n in tool_names_lower):
            capabilities.append("web search via DuckDuckGo")
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
