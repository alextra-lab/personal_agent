# Prompt Efficiency Research

**Status:** Active — recommendations applied to `prompts.py` (FRE-105)  
**Date:** 2026-03-13  
**Scope:** `ROUTER_SYSTEM_PROMPT`, `TOOL_USE_SYSTEM_PROMPT`, `get_tool_awareness_prompt()` in `src/personal_agent/orchestrator/prompts.py`

---

## 1. Purpose

This note establishes the evidence base for prompt-efficiency decisions in the personal agent's
orchestrator. It answers: which parts of the system prompt carry the highest token overhead, which
can safely be shortened, and what the research says about compression trade-offs.

---

## 2. Current Token Overhead Profile (post FRE-105 cleanup)

| Prompt | Chars | Approx tokens (÷4) | Frequency |
|--------|-------|--------------------|-----------|
| `ROUTER_SYSTEM_PROMPT` | 556 | ~139 | Every non-heuristic request |
| `TOOL_USE_SYSTEM_PROMPT` | 1,656 | ~414 | Every STANDARD/REASONING call with tools |
| `get_tool_awareness_prompt()` output | ~200–400 | ~50–100 | Same as above (cached 60 s) |
| **Total per tool-enabled call** | **~2,400–2,600** | **~600–650** | — |

This is a significant reduction from the original FRE-105 estimate (~380–480 tokens **before** the
FRE-95 toolset reduction and FRE-46 context-window tightening). Based on recent telemetry, the
router prompt alone went from ~730 tokens (dead `ROUTER_SYSTEM_PROMPT_BASIC`) to ~139 tokens. The
`TOOL_USE_SYSTEM_PROMPT` was also trimmed in this ticket by removing a redundant static-knowledge
example ("How does the TLS handshake work?") and tightening markdown.

---

## 3. Guidance from Authoritative Sources

### 3.1 Anthropic — System Prompt Best Practices

Source: *Anthropic Docs — Prompt Engineering: System Prompts* (2025)

Key points relevant to this codebase:

- **Instructions before examples.** Anthropic recommends placing rules/constraints at the top of
  the system prompt, then examples. The current `TOOL_USE_SYSTEM_PROMPT` already follows this
  pattern (rules block, then two examples).
- **Fewer, clearer rules beat more rules.** Long rule lists create competing constraints. Claude
  (and similar models) handle 5–10 crisp rules better than 15+ verbose ones. Our rules block is
  at 6 items — within the recommended range.
- **Don't repeat the tool schema in prose.** Telling the model "use ONLY the provided tool names
  and EXACT parameter names" is necessary; restating parameter names again in examples adds
  redundancy. The trimmed examples in this ticket removed the commented `(no tool — this is
  static knowledge)` prefix lines, which the model does not need to see.
- **Few-shot examples: 2–3 is typically sufficient** for format adherence. More examples improve
  calibration marginally but cost tokens linearly. The current 2-example `TOOL_USE_SYSTEM_PROMPT`
  is well-positioned.

### 3.2 OpenAI — Prompt Engineering Guide

Source: *OpenAI Platform Docs — Prompt Engineering* (2025)

Key points:

- **Tactic: Use delimiters clearly.** The `[TOOL_REQUEST]...[END_TOOL_REQUEST]` delimiter pattern
  in `TOOL_USE_SYSTEM_PROMPT` is aligned with OpenAI's advice to use unambiguous separators for
  structured output sections.
- **Tactic: Reduce repetition.** OpenAI notes that repeating the same constraint in multiple
  forms (positive + negative restatement) wastes tokens with diminishing reliability returns.
  Example: "Do not invent tools or parameters. If no tool fits, say so directly." — these are
  two ways of saying the same thing. However, empirical evidence (see §4) suggests model-specific
  calibration matters, so this should only be collapsed after measuring regression in tool-call
  accuracy.
- **Token efficiency via structured formats.** OpenAI recommends dense, structured formats (e.g.,
  bullet-point rules) over narrative prose for system prompts. Both our prompts already use this
  format. No change recommended.

### 3.3 LLMLingua (2023) — Prompt Compression via Perplexity Filtering

Source: Jiang et al., *LLMLingua: Compressing Prompts for Accelerated Inference of Large Language
Models*, EMNLP 2023. [arXiv:2310.05736](https://arxiv.org/abs/2310.05736)

Summary: LLMLingua uses a small reference LM to score each token's perplexity conditional on the
target model. Low-perplexity tokens (predictable given context) are dropped. Achieves 3–20×
compression on long prompts with <5% downstream accuracy drop.

**Applicability to this project:**

- LLMLingua targets long document prompts (RAG contexts, retrieved passages), not short
  instruction prompts. Our `TOOL_USE_SYSTEM_PROMPT` at ~414 tokens is too short for meaningful
  LLMLingua gains — the technique's compression ratio degrades below ~500 tokens.
- **Conclusion: Not applicable at current prompt sizes. Revisit if `TOOL_USE_SYSTEM_PROMPT`
  exceeds ~1,500 tokens.**

### 3.4 PromptBench (2024) — Robustness of Prompts to Perturbation

Source: Zhu et al., *PromptBench: Towards Evaluating the Robustness of Large Language Models on
Adversarial Prompts*, NeurIPS 2024. [arXiv:2306.04528](https://arxiv.org/abs/2306.04528)

Summary: PromptBench shows that small wording changes (synonym substitution, word deletion) in
instructions can cause 15–40% accuracy drops on task-specific prompts. The effect is larger for
smaller models (7B–14B range) than for 70B+.

**Applicability to this project:**

- We run Qwen3.5 (a 4B–14B class model) locally. This means prompt stability is **more
  important** for us than for cloud-scale models.
- **Conclusion: When trimming `TOOL_USE_SYSTEM_PROMPT`, change phrasing conservatively. Remove
  redundant content rather than rephrasing existing rules. Validate each edit with the routing
  delegation test suite.**

---

## 4. Recommendations

### 4.1 Immediate (applied in FRE-105)

| Change | Expected savings | Risk |
|--------|-----------------|------|
| Remove dead `ROUTER_SYSTEM_PROMPT_BASIC` (~730 tokens) | Eliminated entirely | None — was unreachable |
| Remove dead `ROUTER_SYSTEM_PROMPT_WITH_FORMAT` (~2,010 tokens) | Eliminated entirely | None — was unreachable |
| Remove redundant `ROUTER_USER_TEMPLATE` and `FORMAT_TOKEN_MAP` | Eliminated entirely | None — unused |
| Strip static-knowledge TLS example from `TOOL_USE_SYSTEM_PROMPT` | ~120 chars / ~30 tokens | Very low — example was informational only |
| Tighten `get_tool_awareness_prompt()` header and footer prose | ~80 chars / ~20 tokens | Very low — no semantic change |

These changes have been applied. Combined saving from FRE-105 prompt edits: approximately **50
tokens per tool-enabled STANDARD/REASONING call** on top of the structural dead-code removal.

### 4.2 Deferred (do not apply without regression tests)

| Candidate change | Potential saving | Risk / Condition |
|------------------|-----------------|------------------|
| Collapse "do not invent tools" + "if no tool fits" rules into one line | ~15 tokens | Medium — PromptBench sensitivity for small models. Gate on tool-selection accuracy test. |
| Replace tool-awareness free-text with a JSON array of tool names | ~40–80 tokens | Medium — unknown effect on capability-question accuracy for Qwen3.5. Needs A/B. |
| Remove `mcp_perplexity_research` example from `TOOL_USE_SYSTEM_PROMPT` | ~80 tokens | Medium-high — the example is the primary training signal for research-vs-ask disambiguation. Do not remove until routing delegation tests cover both tools explicitly. |
| Reduce `TOOL_AWARENESS_CACHE_TTL` to force fresher but smaller output | 0 tokens saved | N/A — TTL is a freshness control, not a size control. |

### 4.3 Structural (future work, new ticket)

- **Dynamic tool injection:** Only pass tool definitions relevant to the current query intent.
  Already partially done (FRE-95 reduced active toolset from 33+ to 14). Next step: query-time
  filtering by topic category could reduce tool-schema payload by ~30% for narrow queries.
- **Prompt versioning:** Log the system_prompt hash alongside `prompt_tokens` in
  `model_call_completed` events so Kibana can correlate prompt changes with token trends.

---

## 5. Measurement

Prompt-token impact is observable through the `prompt_tokens` field in `MODEL_CALL_COMPLETED`
telemetry events (emitted by `llm_client/client.py` and `orchestrator/executor.py`). The
`llm-tokens-over-time` Kibana visualization on the Token Usage dashboard shows the trend.

Regression guard: `tests/fixtures/routing_token_baselines.json` records character counts of the
two static prompt constants. The delegation tests in
`tests/test_orchestrator/test_routing_delegation.py` assert exact equality against the baseline,
causing CI to fail on any unreviewed prompt change.

---

## 6. References

1. Anthropic. *System Prompts — Prompt Engineering*. Anthropic Docs, 2025.  
   <https://docs.anthropic.com/en/docs/build-with-claude/prompt-engineering/system-prompts>

2. OpenAI. *Prompt Engineering Guide*. OpenAI Platform Docs, 2025.  
   <https://platform.openai.com/docs/guides/prompt-engineering>

3. Jiang, H., Wu, Q., Lin, C.-Y., Yang, Y., & Qiu, X. (2023). *LLMLingua: Compressing Prompts
   for Accelerated Inference of Large Language Models*. EMNLP 2023.  
   <https://arxiv.org/abs/2310.05736>

4. Zhu, K., Wang, J., Zhou, J., Wang, Z., Chen, H., Wang, Y., Yang, L., Ye, W., Gong, N., Zhang,
   Y., & Xie, X. (2024). *PromptBench: Towards Evaluating the Robustness of Large Language Models
   on Adversarial Prompts*. NeurIPS 2024.  
   <https://arxiv.org/abs/2306.04528>
