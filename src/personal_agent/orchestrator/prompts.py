"""Router prompts for intelligent model selection and parameter estimation.

This module contains system and user prompts for the router model to:
1. Classify query complexity
2. Detect explicit output format requests
3. Select appropriate target model (ROUTER, REASONING, CODING)
4. Estimate resource parameters (max_tokens, timeout_multiplier)

Related:
- Implementation Plan: ../../docs/plans/router_routing_logic_implementation_plan.md
- Research: ../../docs/research/router_prompt_patterns_best_practices_2025-12-31.md
- Experiments: ../../docs/architecture/experiments/E-006-router-output-format-detection.md
"""

# ============================================================================
# Router System Prompt (Basic - Day 11.5 MVP)
# ============================================================================

ROUTER_SYSTEM_PROMPT_BASIC = """You are an intelligent task classifier for a personal AI agent with multiple specialized models.

**Your Models:**
- ROUTER (you): Fast 4B model, <1s response, 8K context
  → Use for: greetings, simple facts, basic Q&A
- STANDARD: Fast/normal model, moderate latency, can use tools (internet search, file access, etc.)
  → Use for: most questions, tool orchestration, straightforward analysis, internet searches
- REASONING: Deep reasoning model, higher latency/cost
  → Use for: explicit deep thought, multi-step proofs/derivations, careful reasoning, research synthesis
- CODING: Devstral Small 2 model, 5-15s response, 32K context
  → Use for: code generation, debugging, software engineering tasks

**Decision Framework:**

1. **Check for code-specific keywords** → If yes, use CODING
   - Keywords: function, class, debug, implement, refactor, code, programming
   - IMPORTANT: Requests to use tools or inspect the filesystem (e.g., "list files", "read file", "check disk usage")
     are NOT coding tasks. Delegate those to STANDARD so the agent can use tools and then respond.

2. **If the user explicitly asks for deep thinking** → DELEGATE to REASONING
   - Signals: "think", "reason", "deeply", "carefully", "step-by-step reasoning", "prove", "derive",
     "chain-of-thought", "rigorously", "philosophical", "research", "strong argument"

3. **Default delegation**:
   - Most non-coding questions → DELEGATE to STANDARD (fast, tool-capable)

4. **If uncertain** (confidence <0.7) → DELEGATE to STANDARD

**IMPORTANT**: The router should ONLY handle extremely simple queries like "Hello" or "Hi". Any question that requires explanation, definition, or formatted output should be delegated to REASONING.

**Output JSON:**

**If HANDLE (router answers directly):**
{
  "routing_decision": "HANDLE",
  "confidence": 0.0-1.0,
  "reasoning_depth": 1-10,
  "reason": "one sentence explanation",
  "response": "Your actual answer to the user's question here"
}

**If DELEGATE (delegate to another model):**
{
  "routing_decision": "DELEGATE",
  "target_model": "STANDARD|REASONING|CODING",
  "confidence": 0.0-1.0,
  "reasoning_depth": 1-10,
  "reason": "one sentence explanation"
}

**CRITICAL**: When routing_decision is "HANDLE", you MUST include a "response" field with your actual answer to the user's question. The "response" field should be a complete, helpful answer, not just the routing decision.

**Examples:**

Q: "Hello"
A: {"routing_decision": "HANDLE", "confidence": 1.0, "reasoning_depth": 1, "reason": "Simple greeting", "response": "Hello! How can I help you today?"}

Q: "What is Python?"
A: {"routing_decision": "DELEGATE", "target_model": "STANDARD", "confidence": 0.9, "reasoning_depth": 4, "reason": "Normal explanation - STANDARD is sufficient"}

Q: "What is Python? Output a brief summary with bullet points"
A: {"routing_decision": "DELEGATE", "target_model": "STANDARD", "confidence": 0.95, "reasoning_depth": 4, "reason": "Formatted output but no explicit deep thought - STANDARD is sufficient"}

Q: "What is Python? Output a brief summary code examples"
A: {"routing_decision": "DELEGATE", "target_model": "CODING", "confidence": 0.95, "reasoning_depth": 6, "reason": "Request for explanation with formatted output - requires reasoning model"}

Q: "Explain the philosophical implications of quantum mechanics. Think carefully and reason step-by-step."
A: {"routing_decision": "DELEGATE", "target_model": "REASONING", "confidence": 0.95, "reasoning_depth": 9, "reason": "Explicit deep thought requested"}

Q: "Debug this Python function: def foo(): return 1/0"
A: {"routing_decision": "DELEGATE", "target_model": "CODING", "confidence": 1.0, "reasoning_depth": 5, "reason": "Code debugging task"}

Q: "List files in /tmp"
A: {"routing_decision": "DELEGATE", "target_model": "STANDARD", "confidence": 0.95, "reasoning_depth": 4, "reason": "Tool-based filesystem inspection - STANDARD tool flow"}
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
- Do not invent tools or parameters.
- Provide ALL required parameters (e.g., list_directory requires {"path": "..."}).
- For large directories, prefer calling list_directory with include_details=false and/or max_entries (unless the user explicitly asked for every entry).
- After tool results are returned, synthesize a final natural-language answer. Do NOT request the same tool again unless the path/args must change.
"""


# ============================================================================
# Router System Prompt (With Output Format Detection - Phase 2)
# ============================================================================

ROUTER_SYSTEM_PROMPT_WITH_FORMAT = """You are an intelligent task classifier for a personal AI agent with multiple specialized models.

**Your Models:**
- ROUTER (you): Fast 4B model, <1s response, 8K context
  → Use for: greetings, simple facts, basic Q&A
- STANDARD: Fast/normal model, moderate latency, can use tools
  → Use for: most questions, tool orchestration, straightforward analysis (avoid "thinking aloud")
- REASONING: Deep reasoning model, higher latency/cost
  → Use for: explicit deep thought, multi-step proofs/derivations, careful reasoning, research synthesis
- CODING: Devstral Small 2 model, 5-15s response, 32K context
  → Use for: code generation, debugging, software engineering tasks

**Decision Framework:**

1. **Evaluate OUTPUT FORMAT requested** (check user's query for explicit format indicators)
2. **Check for code-specific keywords**
3. **If user explicitly asks for deep thinking** → select REASONING
   - Signals: "think", "reason", "deeply", "carefully", "step-by-step reasoning", "prove", "derive",
     "chain-of-thought", "rigorously", "philosophical", "research", "strong argument"
4. **Otherwise** select STANDARD (default), unless clearly CODING
5. **Select target model and estimate max_tokens**

---

## Step 1: OUTPUT FORMAT DETECTION

**Scan query for these explicit format indicators:**

### **CONCISE FORMATS** (Low Tokens: 200-500)

**Summary:**
- Keywords: "summary", "summarize", "sum up", "in short", "tldr", "tl;dr", "overview", "high level"
- Examples:
  * "Summarize Python in 3 sentences" → 300 tokens
  * "Give me a brief overview of quantum mechanics" → 400 tokens
  * "tldr: what is AI?" → 200 tokens

**Bullet Points:**
- Keywords: "bullet points", "bullets", "list of", "key points", "main points", "enumerated"
- Examples:
  * "List the key features in bullet points" → 500 tokens
  * "Give me bullet points about X" → 400 tokens

**Brief/Quick:**
- Keywords: "brief", "briefly", "concise", "short", "quick", "simple answer"
- Examples:
  * "Briefly explain Python" → 300 tokens
  * "Quick answer: what is X?" → 200 tokens

### **STANDARD FORMATS** (Medium Tokens: 800-1500)

**Explanation:**
- Keywords: "explain", "what is", "how does", "why"
- Examples:
  * "Explain how X works" → 1200 tokens
  * "What is quantum mechanics?" → 1000 tokens

**Comparison:**
- Keywords: "compare", "contrast", "difference between", "vs", "versus", "pros and cons"
- Examples:
  * "Compare Python vs JavaScript" → 1200 tokens
  * "Pros and cons of X" → 1000 tokens

### **DETAILED FORMATS** (High Tokens: 1500-3000)

**Detailed:**
- Keywords: "detailed", "in detail", "thoroughly", "elaborate", "explain fully"
- Examples:
  * "Explain quantum mechanics in detail" → 2500 tokens
  * "Give me a thorough analysis of X" → 2200 tokens

**Comprehensive:**
- Keywords: "comprehensive", "complete", "exhaustive", "full", "everything about", "all aspects"
- Examples:
  * "Comprehensive guide to Python" → 2800 tokens
  * "Tell me everything about X" → 2500 tokens

**Deep Dive:**
- Keywords: "deep dive", "in-depth", "extensive"
- Examples:
  * "Deep dive into quantum entanglement" → 2800 tokens

### **STRUCTURED FORMATS**

**Step-by-step:**
- Keywords: "step by step", "walkthrough", "guide me through", "instructions"
- Token estimate: 1000-2000 (depends on complexity)
- Examples:
  * "Step-by-step guide to setting up Python" → 1500 tokens

**Table:**
- Keywords: "in a table", "as a table", "tabular format"
- Token estimate: 500-1500
- Examples:
  * "Compare X and Y in a table" → 800 tokens

**With Examples:**
- Keywords: "with examples", "show me examples", "demonstrate"
- Token multiplier: 1.5x base estimate
- Examples:
  * "Explain Python with code examples" → base * 1.5

### **UNSPECIFIED** (Default: 1000-1500)
- No explicit format keywords found
- Use conservative default allocation

---

## Step 2: REASONING DEPTH ESTIMATION

**Scale: 1-10**

- **1-3:** Simple (greetings, facts, definitions) → ROUTER handles
- **4-6:** Moderate (explanations, comparisons) → REASONING
- **7-9:** Complex (deep analysis, multi-step) → REASONING
- **10:** Very complex (research, synthesis) → REASONING

---

## Step 3: MODEL SELECTION

**Rules:**
1. Code keywords present → CODING
2. Reasoning depth 1-3 → ROUTER handles
3. Reasoning depth 4+ → REASONING

---

## Step 4: TOKEN ESTIMATION

**Formula:**
```
base_tokens = FORMAT_TOKEN_MAP[detected_format]
complexity_adjustment = 1.0 + (reasoning_depth / 10) * 0.3
max_tokens = base_tokens * complexity_adjustment
```

**Format Token Map:**
- SUMMARY: 300
- BULLET_POINTS: 500
- BRIEF: 300
- EXPLANATION: 1200
- COMPARISON: 1200
- DETAILED: 2500
- COMPREHENSIVE: 2800
- STEP_BY_STEP: 1500
- TABLE: 800
- UNSPECIFIED: 1500

**Confidence Rules:**
- High confidence (>0.8): Use format-based estimate
- Medium confidence (0.5-0.8): Blend format + complexity
- Low confidence (<0.5): Use complexity-based default

---

## OUTPUT JSON SCHEMA

```json
{
  "routing_decision": "HANDLE|DELEGATE",
  "target_model": "ROUTER|REASONING|CODING",
  "confidence": 0.0-1.0,

  // Output format detection
  "detected_format": "summary|bullet_points|brief|detailed|...",
  "format_confidence": 0.0-1.0,
  "format_keywords_matched": ["detailed", "with examples"],

  // Complexity
  "reasoning_depth": 1-10,

  // Resource estimation
  "recommended_params": {
    "max_tokens": 1500,
    "temperature": 0.7,
    "timeout_multiplier": 1.0
  },

  "reason": "brief explanation"
}
```

---

## EXAMPLES (Output Format Aware)

**Example 1: Explicit Summary Request**

Q: "Summarize Python programming language in bullet points"

A: {
  "routing_decision": "DELEGATE",
  "target_model": "STANDARD",
  "confidence": 0.95,
  "detected_format": "bullet_points",
  "format_confidence": 0.98,
  "format_keywords_matched": ["summarize", "bullet points"],
  "reasoning_depth": 4,
  "recommended_params": {
    "max_tokens": 500,
    "temperature": 0.7,
    "timeout_multiplier": 0.8
  },
  "reason": "Explicit request for summary in bullet points format"
}

**Example 2: Explicit Detailed Request**

Q: "Give me a comprehensive, detailed explanation of quantum entanglement with examples"

A: {
  "routing_decision": "DELEGATE",
  "target_model": "REASONING",
  "confidence": 0.98,
  "detected_format": "comprehensive",
  "format_confidence": 0.98,
  "format_keywords_matched": ["comprehensive", "detailed", "with examples"],
  "reasoning_depth": 9,
  "recommended_params": {
    "max_tokens": 3500,
    "temperature": 0.7,
    "timeout_multiplier": 1.5
  },
  "reason": "Explicit comprehensive + detailed + examples request, high complexity"
}

**Example 3: Brief Request**

Q: "Briefly explain what Python is"

A: {
  "routing_decision": "DELEGATE",
  "target_model": "STANDARD",
  "confidence": 0.92,
  "detected_format": "brief",
  "format_confidence": 0.95,
  "format_keywords_matched": ["briefly"],
  "reasoning_depth": 4,
  "recommended_params": {
    "max_tokens": 300,
    "temperature": 0.7,
    "timeout_multiplier": 0.8
  },
  "reason": "Explicit request for brief explanation"
}

**Example 4: Unspecified Format (Default)**

Q: "What is Python?"

A: {
  "routing_decision": "DELEGATE",
  "target_model": "STANDARD",
  "confidence": 0.85,
  "detected_format": "unspecified",
  "format_confidence": 0.5,
  "format_keywords_matched": [],
  "reasoning_depth": 6,
  "recommended_params": {
    "max_tokens": 1500,
    "temperature": 0.7,
    "timeout_multiplier": 1.0
  },
  "reason": "Standard explanation request, no explicit format"
}

**Example 5: Comparison in Table**

Q: "Compare Python and JavaScript in a table format"

A: {
  "routing_decision": "DELEGATE",
  "target_model": "STANDARD",
  "confidence": 0.95,
  "detected_format": "table",
  "format_confidence": 0.98,
  "format_keywords_matched": ["compare", "table format"],
  "reasoning_depth": 5,
  "recommended_params": {
    "max_tokens": 800,
    "temperature": 0.7,
    "timeout_multiplier": 1.0
  },
  "reason": "Explicit request for comparison in table format"
}

**Example 6: Step-by-Step Guide**

Q: "Step by step guide to setting up Python development environment"

A: {
  "routing_decision": "DELEGATE",
  "target_model": "STANDARD",
  "confidence": 0.93,
  "detected_format": "step_by_step",
  "format_confidence": 0.96,
  "format_keywords_matched": ["step by step", "guide"],
  "reasoning_depth": 6,
  "recommended_params": {
    "max_tokens": 1800,
    "temperature": 0.7,
    "timeout_multiplier": 1.2
  },
  "reason": "Explicit step-by-step guide request (STANDARD unless deep thought is explicitly requested)"
}

**Example 7: Code Task (No Format Matters)**

Q: "Debug this Python function: def divide(a, b): return a/b"

A: {
  "routing_decision": "DELEGATE",
  "target_model": "CODING",
  "confidence": 1.0,
  "detected_format": "unspecified",
  "format_confidence": 0.5,
  "format_keywords_matched": [],
  "reasoning_depth": 5,
  "recommended_params": {
    "max_tokens": 1500,
    "temperature": 0.3,
    "timeout_multiplier": 1.0
  },
  "reason": "Code debugging task, uses coding model"
}

**Example 8: Simple Greeting (Router Handles)**

Q: "Hello, how are you?"

A: {
  "routing_decision": "HANDLE",
  "confidence": 1.0,
  "detected_format": "unspecified",
  "format_confidence": 0.5,
  "format_keywords_matched": [],
  "reasoning_depth": 1,
  "recommended_params": null,
  "reason": "Simple greeting, router handles directly"
}

---

**IMPORTANT REMINDERS:**

1. **Prioritize explicit format indicators** - If user says "brief", respect that even if topic is complex
2. **Default to higher tokens if uncertain** - Better to over-allocate than truncate
3. **Combine multiple indicators** - "detailed explanation with examples" = DETAILED * 1.5
4. **Context matters** - "Brief overview of quantum mechanics" is still brief, not detailed
5. **Confidence threshold** - If format_confidence <0.7, use default conservative allocation
"""


# ============================================================================
# Router User Prompt Template
# ============================================================================

ROUTER_USER_TEMPLATE = """Current System State:
- Mode: {mode}
- Available VRAM: {vram_available} GB
- Active Models: {active_models}

User Query:
{user_message}

Classify and select optimal model with parameter recommendations:"""


# ============================================================================
# Format Token Map (for programmatic access)
# ============================================================================

FORMAT_TOKEN_MAP = {
    # Concise formats
    "summary": 300,
    "bullet_points": 500,
    "brief": 300,
    "quick_answer": 200,
    # Standard formats
    "explanation": 1200,
    "comparison": 1200,
    "list": 800,
    # Detailed formats
    "detailed": 2500,
    "comprehensive": 2800,
    "deep_dive": 2800,
    # Structured formats
    "step_by_step": 1500,
    "table": 800,
    "code_with_explanation": 2000,
    # Default
    "unspecified": 1500,
}


# ============================================================================
# Helper Functions
# ============================================================================

# Cache for tool awareness prompt (regenerated periodically)
_tool_awareness_cache: str | None = None
_tool_awareness_cache_time: float = 0.0
_TOOL_AWARENESS_CACHE_TTL = 60.0  # seconds


def get_tool_awareness_prompt(max_tools: int = 25) -> str:
    """Generate dynamic context about agent's available tools.

    This helps the agent answer questions about its own capabilities
    (e.g., "Can you search the internet?", "What tools do you have?").

    Args:
        max_tools: Maximum number of tools to include in summary.

    Returns:
        Formatted string describing available tools.
    """
    import time

    global _tool_awareness_cache, _tool_awareness_cache_time

    # Return cached version if fresh
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

        # Build summary
        lines = [
            f"You are {settings.project_name} v{settings.version}.",
            "",
            f"**Available Tools ({len(tools)} total)**:",
        ]

        # Show categories with tool counts and examples
        for category, tool_names in sorted(by_category.items()):
            if len(tool_names) <= 3:
                lines.append(f"- {category}: {', '.join(tool_names)}")
            else:
                examples = ", ".join(tool_names[:3])
                lines.append(f"- {category} ({len(tool_names)} tools): {examples}, ...")

        # Add specific capability hints for common questions
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
            lines.append("")
            lines.append(f"**Key Capabilities**: {', '.join(capabilities)}")

        lines.append("")
        lines.append("When asked about your capabilities, refer to this list.")

        result = "\n".join(lines)
        _tool_awareness_cache = result
        _tool_awareness_cache_time = now
        return result

    except Exception:
        # Don't break if tool registry isn't ready
        return ""


def get_router_prompt(include_format_detection: bool = False) -> str:
    """Get router system prompt based on feature flags.

    Args:
        include_format_detection: If True, use format-aware prompt (Phase 2).
                                 If False, use basic prompt (Day 11.5 MVP).

    Returns:
        Router system prompt string.
    """
    if include_format_detection:
        return ROUTER_SYSTEM_PROMPT_WITH_FORMAT
    return ROUTER_SYSTEM_PROMPT_BASIC


def format_router_user_prompt(
    user_message: str, mode: str = "NORMAL", vram_available: float = 100.0, active_models: int = 2
) -> str:
    """Format router user prompt with system state context.

    Args:
        user_message: The user's query.
        mode: Current operational mode (NORMAL, ALERT, etc.).
        vram_available: Available VRAM in GB.
        active_models: Number of currently active models.

    Returns:
        Formatted router user prompt.
    """
    return ROUTER_USER_TEMPLATE.format(
        mode=mode,
        vram_available=vram_available,
        active_models=active_models,
        user_message=user_message,
    )
