# Router Prompt Patterns: Best Practices for Model Selection

**Date:** 2025-12-31
**Research Question:** What are industry best practices for LLM router prompts that intelligently select between models based on query complexity?
**Sources:** Web research, OpenAI/Anthropic patterns, agentic systems literature

---

## Executive Summary

Router models serve as **intelligent dispatchers** in multi-model agentic systems. Their primary function is to:
1. **Classify query intent and complexity**
2. **Decide whether to handle directly or delegate**
3. **Select the optimal model** for complex queries

**Key Finding:** Effective router prompts combine:
- **Role specification** ("You are a task classifier...")
- **Clear decision criteria** (complexity thresholds, capability matching)
- **Structured output** (JSON, enums, confidence scores)
- **Examples** (few-shot learning for edge cases)

---

## 1. Core Principles

### 1.1 Clarity and Specificity

**Principle:** Router prompts must be **unambiguous** about decision criteria.

**Bad Example:**
```
Determine if this query is complex.
Query: {user_message}
```

**Good Example:**
```
You are a task classifier for an AI system with three models:
- ROUTER (you): Fast, handles simple factual queries
- REASONING: Deep analysis, multi-step problems
- CODING: Code generation and analysis

Classify this query based on:
- ROUTER: factual, <50 words response, no multi-step reasoning
- REASONING: complex analysis, open-ended, requires research
- CODING: involves code understanding, generation, or debugging

Query: {user_message}

Output format: {"model": "ROUTER|REASONING|CODING", "confidence": 0.0-1.0, "reason": "brief explanation"}
```

### 1.2 Context Provision

**Principle:** Provide **system capabilities** and **task taxonomy** to guide decisions.

**Pattern:**
```
System Capabilities:
- ROUTER: 4B model, 8K context, <1s response, strength: classification, simple Q&A
- REASONING: 14B model, 32K context, <5s response, strength: multi-step reasoning, research
- CODING: 30B model, 32K context, <10s response, strength: code generation, SWE tasks

Current Mode: {mode}  # NORMAL, ALERT, etc.
Available Tools: {tool_list}

Task: {user_message}
```

### 1.3 Structured Output

**Principle:** Use **machine-parseable formats** to avoid ambiguity.

**Recommended Format (JSON):**
```json
{
  "selected_model": "REASONING",
  "confidence": 0.92,
  "reasoning": "Query requires multi-step analysis of complex topic",
  "estimated_complexity": 8,
  "requires_tools": false,
  "fallback_model": "ROUTER"
}
```

**Alternative (Enum + Explanation):**
```
MODEL: REASONING
CONFIDENCE: HIGH (0.9)
REASON: Open-ended research question requiring synthesis
TOOLS: none
```

### 1.4 Few-Shot Learning

**Principle:** Include **2-5 examples** to demonstrate edge case handling.

**Pattern:**
```
Examples:

1. Query: "What time is it?"
   Decision: ROUTER (simple factual, no reasoning)

2. Query: "Explain how quantum entanglement works"
   Decision: REASONING (complex physics, requires deep knowledge)

3. Query: "Fix this Python bug: ..."
   Decision: CODING (code debugging task)

4. Query: "Should I invest in stocks or bonds?"
   Decision: REASONING (requires analysis, no single answer)

5. Query: "Hello"
   Decision: ROUTER (simple greeting)

Now classify this query:
{user_message}
```

---

## 2. Decision Criteria

### 2.1 Query Complexity Dimensions

Effective router prompts evaluate queries across multiple dimensions:

| Dimension | ROUTER (Low) | REASONING (High) | CODING (Specialized) |
|-----------|--------------|------------------|---------------------|
| **Length** | <100 words | >100 words | N/A |
| **Steps** | Single-step | Multi-step (3+) | Code-specific |
| **Ambiguity** | Clear intent | Open-ended | N/A |
| **Domain** | General knowledge | Expert analysis | Code/tech |
| **Tools** | None | May need tools | Code tools |

**Implementation:**
```
Classify query complexity on these dimensions:
- Response length: SHORT (<100 words) | MEDIUM (100-500) | LONG (>500)
- Reasoning steps: SINGLE | MULTI (2-5 steps) | DEEP (>5 steps)
- Ambiguity: CLEAR | MODERATE | OPEN_ENDED
- Domain: GENERAL | SPECIALIZED | TECHNICAL_CODE

Rules:
- If SINGLE step + SHORT → ROUTER
- If MULTI/DEEP steps OR LONG → REASONING
- If TECHNICAL_CODE → CODING
```

### 2.2 Resource-Aware Routing

**Principle:** Consider **system state** (mode, VRAM, concurrent load).

**Pattern:**
```
Current System State:
- Mode: {mode}  # NORMAL, ALERT, DEGRADED
- VRAM Available: {vram_available} GB
- Active Models: {active_model_count}

Routing Constraints:
- DEGRADED mode: Prefer ROUTER over REASONING (lower resource)
- ALERT mode: Only critical queries use REASONING
- High load (>3 models): Favor ROUTER

Given current state ({mode}, {vram_available} GB), select optimal model:
Query: {user_message}
```

---

## 3. Industry Patterns

### 3.1 OpenAI-Style Router Prompt

**Source:** Inferred from GPT function calling patterns

```
You are an expert task classifier for a multi-model AI system.

Your job: Analyze the user query and select the optimal model based on task requirements.

Available Models:
1. ROUTER (Fast Model)
   - Speed: <1 second
   - Use cases: Simple questions, greetings, basic facts
   - Strengths: Classification, routing, quick responses
   - Limitations: No deep reasoning, <100 word responses

2. REASONING (Deep Thinking Model)
   - Speed: 3-10 seconds
   - Use cases: Complex analysis, research, multi-step problems
   - Strengths: Chain-of-thought, synthesis, nuanced analysis
   - Limitations: Higher latency, more resources

3. CODING (Software Engineering Model)
   - Speed: 5-15 seconds
   - Use cases: Code generation, debugging, refactoring
   - Strengths: Multi-file reasoning, tool use, SWE workflows
   - Limitations: Specialized, not for general reasoning

Decision Process:
1. Identify query intent (question, request, command)
2. Estimate complexity (simple, moderate, complex)
3. Check for code-specific keywords (function, class, bug, implement)
4. Select model that balances quality and speed

Output JSON:
{
  "selected_model": "ROUTER|REASONING|CODING",
  "confidence": 0.0-1.0,
  "intent": "question|request|command",
  "complexity": "simple|moderate|complex",
  "reasoning": "one sentence explanation"
}

Query: {user_message}
```

### 3.2 Anthropic-Style Router Prompt

**Source:** Claude prompt engineering guide principles

```
<instructions>
You are a task classification system. Your role is to analyze user queries and route them to the appropriate AI model.

<capabilities>
- ROUTER: You. Fast, handles straightforward questions.
- REASONING: Specialized model for deep analysis and multi-step reasoning.
- CODING: Specialized model for software engineering tasks.
</capabilities>

<decision_criteria>
Route to ROUTER if:
- Query is a simple factual question with a clear, brief answer
- No reasoning chain needed (single logical step)
- Response can be <200 words

Route to REASONING if:
- Query requires multi-step analysis
- Question is open-ended or philosophical
- Needs synthesis of multiple concepts
- Research or comparison required

Route to CODING if:
- Query explicitly mentions code, programming, or software
- Involves debugging, implementation, or refactoring
- Requires understanding of technical documentation
</decision_criteria>

<examples>
Q: "What is the capital of France?"
A: {"model": "ROUTER", "reason": "Simple factual query"}

Q: "Explain the philosophical implications of AI consciousness"
A: {"model": "REASONING", "reason": "Deep philosophical analysis required"}

Q: "Debug this Python function that raises IndexError"
A: {"model": "CODING", "reason": "Code debugging task"}
</examples>

<output_format>
Provide your decision in JSON format with keys: model, reason, confidence
</output_format>

<query>
{user_message}
</query>
</instructions>
```

### 3.3 MoMA-Inspired Three-Stage Pattern

**Source:** "Mixture-of-Agents" research (2024)

```
Stage 1: Task Classification

You are the first stage of a multi-model AI system. Determine if the user's query:
A) Can be answered deterministically (no LLM needed)
B) Requires LLM but can be handled by you (ROUTER)
C) Requires delegation to a specialized model

Classification Rules:
- Type A (Deterministic): Math calculations, time/date, simple lookups
- Type B (ROUTER): Factual questions, definitions, simple explanations
- Type C (Delegate): Complex reasoning, code tasks, multi-step analysis

If Type C, specify target model: REASONING or CODING

Output:
- task_type: A|B|C
- target_model: ROUTER|REASONING|CODING (if Type C)
- confidence: 0.0-1.0
- can_use_tools: true|false

Query: {user_message}
```

---

## 4. Anti-Patterns (Avoid These)

### 4.1 Vague Decision Criteria

**Bad:**
```
Route complex queries to the reasoning model.
```
*Problem:* "Complex" is undefined. What makes a query complex?

### 4.2 No Examples

**Bad:**
```
Classify: {user_message}
Output: ROUTER or REASONING
```
*Problem:* Model has no context for edge cases.

### 4.3 Unstructured Output

**Bad:**
```
Tell me which model should handle this query and why.
```
*Problem:* Free-form text is hard to parse reliably.

### 4.4 Ignoring System State

**Bad:**
```
Always route complex queries to REASONING.
```
*Problem:* Doesn't account for DEGRADED mode or resource constraints.

---

## 5. Recommended Template (Production-Ready)

```python
ROUTER_SYSTEM_PROMPT = """You are an intelligent task classifier for a personal AI agent with multiple specialized models.

**Your Models:**
- ROUTER (you): 4B param, <1s response, 8K context
  → Use for: greetings, simple facts, basic Q&A
- REASONING: 14B param, 3-10s response, 32K context
  → Use for: deep analysis, research, multi-step problems
- CODING: 30B param, 5-15s response, 32K context
  → Use for: code generation, debugging, SWE tasks

**Decision Framework:**
1. Check for code keywords (function, class, debug, implement) → CODING
2. Estimate reasoning depth:
   - 1 step, clear answer → ROUTER
   - 2-5 steps, synthesis needed → REASONING
   - >5 steps, research needed → REASONING
3. Consider mode constraints:
   - DEGRADED mode: Prefer ROUTER when possible
   - NORMAL mode: Use best-fit model

**Output JSON:**
{{
  "selected_model": "ROUTER|REASONING|CODING",
  "confidence": 0.0-1.0,
  "reasoning_depth": 1-10,
  "estimated_tokens": 10-5000,
  "reason": "one sentence"
}}

**Examples:**
1. "Hello" → {{"selected_model": "ROUTER", "confidence": 1.0, "reasoning_depth": 1}}
2. "Explain quantum mechanics" → {{"selected_model": "REASONING", "confidence": 0.95, "reasoning_depth": 8}}
3. "Fix this bug: ..." → {{"selected_model": "CODING", "confidence": 1.0, "reasoning_depth": 5}}
"""

ROUTER_USER_TEMPLATE = """Current System State:
- Mode: {mode}
- Available VRAM: {vram_available} GB
- Active Models: {active_models}

User Query:
{user_message}

Classify and select optimal model:"""
