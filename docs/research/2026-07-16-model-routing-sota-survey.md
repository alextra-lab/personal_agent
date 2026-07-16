# Model routing in agentic LLM harnesses — SOTA survey (2025–2026)

> **Date:** 2026-07-16
> **Purpose:** Settle whether Seshat should add orchestrator-driven model routing, given two owner concerns — (a) determinism (routing must be predictable) and (b) latency (a routing decision that delegates to a full LLM is unacceptable). Input to the config-management-interface epic (the "layer 2" routing question).
> **Method:** `deep-research` fan-out harness — 6 search angles → 21 sources fetched → 97 claims extracted → 25 adversarially verified with 3 independent refutation votes each (23 confirmed, 2 killed). All sub-agents ran Opus 4.8. Raw run: workflow `wf_694b24db-de9`.
> **Companion doc:** `2026-07-03-fre-432-ph0-thinking-token-measurement.md` (ADR-0082) — see §7 for how they interlock.

---

## Verdict

The owner's fear is **correct — and it applies to exactly one pattern, which even OpenAI's peers avoid.** Model routing splits into two camps:

- **The LLM-in-the-loop router (GPT-5 in ChatGPT) is the outlier.** It is genuinely non-deterministic from an integrator's seat.
- **Everyone else routes deterministically, off the hot path** — a fixed config lookup, a fixed architectural role→model assignment, or a cheap pre-trained classifier thresholded against a constant. None invokes a full model to decide the route.

For a latency-sensitive, single-user, pedagogic harness that **already has a deterministic intent-classification gateway and context-isolated sub-agents**, the SOTA-quality-without-non-determinism answer is to **extend that existing deterministic layer** (map intent/task-type and sub-agent role to a fixed model via config precedence), **keep an explicit user/prompt override lever**, and **never put a routing LLM in the hot path.** A learned classifier is an optional later refinement, not a starting requirement.

Jargon, translated once: *router* = the thing that picks the model; *deterministic* = same input → same choice, reproducible; *hot path* = the live request the user waits on; *classifier* = a small fast model that labels a request (doesn't generate text); *cascade* = try cheap first, escalate only if unsure.

---

## 1. How the frontier actually routes

| System | Routing decision | Deterministic? | User control |
|---|---|---|---|
| **GPT-5 (ChatGPT)** | dedicated **real-time router**, continuously retrained on live usage | **No** — boundary drifts across versions, not exposed | hybrid: auto by default + model picker / "think hard" cue |
| **Claude Code sub-agents** | fixed config precedence: env var → per-call param → frontmatter → inherit-parent | **Yes** — no LLM in loop | frontmatter / param; default `inherit` |
| **Anthropic multi-agent** | fixed architectural role→model (Opus lead + Sonnet workers) | **Yes** — set by architecture | n/a (design-time) |
| **OpenAI Agents SDK** | per-agent model assignment in code (small/fast for triage, large for complex) | **Yes** — code config | design-time |
| **RouteLLM / vLLM Semantic Router / CARGO** | learned score compared to a **fixed threshold** | **Yes, per snapshot** (retraining shifts the boundary) | threshold α is a tunable constant |

- **GPT-5** is a "unified system... a smart efficient model, a deeper reasoning model, and a real-time router that quickly decides which to use based on conversation type, complexity, tool needs, and your explicit intent." The router "is continuously trained on real signals, including when users switch models, preference rates... and measured correctness." That continuous retraining + unexposed boundary is precisely what makes it unpredictable to build on. ([introducing-gpt-5](https://openai.com/index/introducing-gpt-5/), [system card](https://openai.com/index/gpt-5-system-card/))
- **Claude Code** picks a sub-agent's model by a documented precedence order, no LLM in the loop; the frontmatter default is `inherit`, so built-in sub-agents run the parent's model unless told otherwise. ([Claude Code docs](https://code.claude.com/docs/en/sub-agents)) — *This is exactly what the Seshat deep-research run itself did: every sub-agent inherited Opus 4.8 because the workflow script set no `model:` — verified by reading the script, not by asking a router.*
- **Anthropic's** Opus-lead / Sonnet-worker config beat single-agent Opus by 90.2% on their internal research eval — but **they attribute ~80% of the variance to the multi-agent architecture (token budget, parallel exploration), not the model heterogeneity.** So "mix strong+weak models" is *not* cleanly isolated as the causal win. ([Anthropic engineering](https://www.anthropic.com/engineering/multi-agent-research-system))
- **RouteLLM** routes between exactly two models via `weak if P(strong wins | q) < α else strong` — a threshold comparison on a *pre-trained* score. The decision itself never invokes an LLM. Cuts cost >2× with no quality loss *in their benchmarks*. ([ICLR 2025](https://arxiv.org/abs/2406.18665), [LMSYS](https://www.lmsys.org/blog/2024-07-01-routellm/))

## 2. Latency — the reassuring part

| Approach | Routing-decision latency | Notes |
|---|--:|---|
| Rule-based (regex/keyword) | **<1 ms** | deterministic, auditable |
| Embedding similarity | ~5–100 ms | ~65× cheaper than LLM classification |
| Small classifier (BERT-class) | ~50–100 ms (GPU); more CPU-only | "never your latency bottleneck" vs 500–2000 ms responses |
| Full LLM in the loop (GPT-5-style) | 100s–1000s ms | + drifting non-determinism |

RouteLLM's heaviest router adds ≤0.4% of the cost of the call it gates; the vLLM Semantic Router does 16K-token routing in ~108 ms sharing a GPU with serving. **None of the deterministic options requires a full LLM call to route.** ([digitalapplied](https://www.digitalapplied.com/blog/llm-model-routing-2026-cost-quality-optimization-engineering-guide), [ICLR 2025](https://proceedings.iclr.cc/paper_files/paper/2025/file/5503a7c69d48a2f86fc00b3dc09de686-Paper-Conference.pdf), [vLLM](https://arxiv.org/pdf/2603.12646))

## 3. SOTA techniques, ranked by fit for this system

1. **Rule/role → model mapping (deterministic config lookup).** Sub-1ms, reproducible, auditable, zero added model calls. What Claude Code and the OpenAI Agents SDK do. **Best starting point.**
2. **Explicit user/prompt override.** Even GPT-5 preserves it ("think hard", model picker). Cheap, pedagogically valuable, keeps the owner in control.
3. **Learned classifier / embedding-regressor thresholded against a constant** (RouteLLM, vLLM Semantic Router, CARGO). Deterministic per snapshot, sub-1% latency *on a GPU*. Optional later refinement — adds a training/maintenance surface a single user rarely needs.
4. **Confidence-gap cascade** (CARGO): score cheaply, escalate only when the top-two are within threshold τ. Deterministic given τ; the escalation branch pays 2× *answer* cost (not routing cost).
5. **Full LLM router (GPT-5 style).** Avoid for a latency-sensitive harness — the only non-deterministic option, and the highest latency.

## 4. Recommendation for Seshat

Extend the **deterministic intent gateway that already exists** (the 7-stage pre-LLM pipeline, intent → TaskType):

- Map **task-type and sub-agent role → a fixed model** via config precedence (the config surface / "layer 1" — this is also the SOTA routing substrate, not a throwaway gadget).
- Keep an **explicit override lever** (a picker and/or a prompt cue) so the owner can force a model when the mapping is wrong — this is the pedagogic "compare by using" surface.
- **Do not** add a full-LLM router in the hot path. If learned routing is ever wanted, add a cheap classifier thresholded against a constant — but for a single user with a narrow query mix, deterministic mapping + override captures most of the benefit at zero added model calls.

## 5. Caveats (what not to over-read)

- **Two claims were killed in verification:** that GPT-5's four signals give a *clean deterministic override* (refuted 0-3), and a specific "95% of GPT-4 quality at 50% of the calls / MT-Bench 8.8 vs 9.3" number (refuted 1-2). Neither is relied on here.
- **Vendor bias:** GPT-5 and Anthropic figures rest on first-party announcements/evals, not independent audits. Anthropic's 90.2% is a single-vendor internal eval on breadth-first tasks, and the paper itself attributes most of the gain to multi-agent architecture, **not** model heterogeneity.
- **Preprint caveat:** the vLLM "98×" is vs an unoptimized baseline (arXiv preprint). RouteLLM's ">2×" is benchmark- and threshold-dependent ("in certain cases").
- **Determinism nuance:** "deterministic" for learned routers means *deterministic per snapshot* (fixed weights + fixed threshold → reproducible); retraining shifts the boundary. Only GPT-5's is *continuously* retrained in production.

## 6. Open questions (carry into the ADR)

1. **CPU-only latency on the VPS (8-vCPU AVX2, no GPU):** the sub-100ms classifier figures assume a GPU. A BERT/embedding classifier CPU-only could add real per-turn latency — a reason to start with rule/role mapping (no model at all) and only add a learned router if the simple mapping proves insufficient.
2. **Marginal value of a learned router for one user:** RouteLLM's gains came from broad public benchmarks with abundant preference data. A single user with a narrow query mix has little to train on — deterministic mapping may capture nearly all the benefit.
3. **Heterogeneity in *isolated* sub-agents:** Anthropic's Opus-lead/Sonnet-worker result was in *their* architecture. Does it transfer to Seshat's context-isolated sub-agents where a worker lacks parent context?
4. **How to surface the override lever pedagogically:** explicit picker vs prompt cue vs per-task-type config.

## 7. How this composes with FRE-432 / ADR-0082 (complementary, not contradicting)

The prior Seshat routing research — `2026-07-03-fre-432-ph0-thinking-token-measurement.md` (ADR-0082 tier-aware selection) — and this survey **interlock into one complete picture**. They answer different halves of the same question and do not conflict.

| | FRE-432 / ADR-0082 (internal) | This survey (external SOTA) |
|---|---|---|
| Question | *Is there waste to recover?* | *How do we route safely?* |
| Answer | **Yes** — trivial SINGLE turns burn ~816 median completion tokens, ~75% thinking (bracketed to ≥~300 thinking tokens even conservatively). There is real mass to save. | **Deterministically, off the hot path** — extend the intent gateway; avoid an LLM router. |
| Contribution | Establishes the **need** + measured cost. | Establishes the **method** without the determinism/latency cost. |

Two interlocks worth carrying forward:

- **FRE-432 adds a Seshat-specific guardrail the generic SOTA research does not.** Its ADR-0084 counterweight: confirming *"there is thinking to reduce"* is **not** license to route trivial turns *off* the primary — the primary is the Socratic continuity layer, and any thinking/routing policy must be **A/B'd for quality-neutrality against the primary baseline before a default flip.** So the SOTA method (deterministic mapping) must still respect the pedagogical constraint — route for quality/fit, not merely for cost. A naive reading of the SOTA advice ("route cheap when you can") would push the opposite way; the pedagogical input refines it, and the two only *appear* to diverge here.
- **FRE-432's instrument fix is a prerequisite for this survey's optional "learned router later" tier.** FRE-432 found the cost ledger undercounts 45% of turns and nothing records a think/visible split — so you cannot A/B a routing policy on cost until that is fixed. Deterministic rule/role mapping needs no such measurement (it's free and quality-driven), but a *learned* router — or any claim of routing cost savings — inherits FRE-432's "fix the instrument first."

Net: **there is waste to recover (FRE-432), there is a determinism-safe method to recover it (this survey), and there is a pedagogical guardrail and an instrument prerequisite that bound how far to push it (FRE-432 again).** No contradiction; the two docs are the need, the method, and the guardrails of a single decision.

---

## Sources

- [OpenAI — Introducing GPT-5](https://openai.com/index/introducing-gpt-5/) (primary)
- [OpenAI — GPT-5 System Card](https://openai.com/index/gpt-5-system-card/) (primary)
- [Claude Code — Sub-agents docs](https://code.claude.com/docs/en/sub-agents) (primary)
- [Anthropic — Multi-agent research system](https://www.anthropic.com/engineering/multi-agent-research-system) (primary)
- [OpenAI Agents SDK — Models](https://openai.github.io/openai-agents-python/models/) (primary)
- [RouteLLM (arXiv 2406.18665 / ICLR 2025)](https://arxiv.org/abs/2406.18665) · [LMSYS blog](https://www.lmsys.org/blog/2024-07-01-routellm/) (primary)
- [vLLM Semantic Router (arXiv 2603.12646)](https://arxiv.org/pdf/2603.12646) · [vLLM blog](https://blog.vllm.ai/2025/09/11/semantic-router.html) (preprint/blog)
- [CARGO embedding router (arXiv 2509.14899)](https://arxiv.org/html/2509.14899v1) (primary)
- [Microsoft Foundry Model Router](https://techcommunity.microsoft.com/blog/azure-ai-foundry-blog/architecting-cost-aware-llm-workloads-with-model-router-in-microsoft-foundry/4514440) (blog)
- Practitioner latency data: [digitalapplied](https://www.digitalapplied.com/blog/llm-model-routing-2026-cost-quality-optimization-engineering-guide), [tianpan.co](https://tianpan.co/blog/2025-11-03-llm-routing-model-cascades), [augmentcode](https://www.augmentcode.com/guides/ai-model-routing-guide) (blog)
- Companion internal doc: `docs/research/2026-07-03-fre-432-ph0-thinking-token-measurement.md` (ADR-0082 / ADR-0084)
