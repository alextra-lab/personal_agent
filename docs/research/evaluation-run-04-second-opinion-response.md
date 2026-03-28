I think the core problem is not “the agent is weak.” It is that the system currently mixes **deterministic routing decisions** with **non-deterministic execution decisions** in a way that leaves a control gap. Your gateway deterministically classifies CP-16 as `analysis/moderate/hybrid` and CP-17 as `analysis/complex/decompose`, but the actual expansion still depends on the primary model choosing to comply. The brief itself shows that this gap is producing silent divergence: same prompt class, same gateway output, different downstream behavior across runs. That pattern is exactly what modern agent guidance warns about: use deterministic workflows when you know the desired control flow, and reserve free-form agent autonomy for the parts that actually benefit from it. LangGraph, Haystack’s agentic pipelines, and Google ADK all distinguish between dynamic agents and predefined workflow orchestration for this reason; Anthropic’s production guidance similarly emphasizes simple, composable patterns over over-abstracted autonomous behavior.  Brief[^1]  LangChain Docs[^2]

So my top-line recommendation is: **move “whether expansion must happen” out of the model and into code**. Let the gateway keep deciding `SINGLE/HYBRID/DECOMPOSE`, but once it says HYBRID or DECOMPOSE, the executor should deterministically enter an expansion workflow. The LLM should generate the *plan content* or *subtask specs*, but not choose whether to expand at all. That aligns with how several frameworks operationalize subagents: the supervisor coordinates workers as tools or explicit workflow nodes, rather than hoping the model decides to branch. It also aligns with current tool-calling guidance from OpenAI and Anthropic: if the model must bridge into application behavior, use tool/function calling with schema constraints rather than relying on natural-language compliance alone.  Brief[^1]  LangChain Docs[^3]

## What I think is really failing

### 1) CP-16 and CP-17 are orchestration contract failures

In both paths, the gateway is correct and the execution contract is not enforced. CP-16 proves this cleanly: classification is right, strategy is right, the model answers well, but no `hybrid_expansion_start` event appears. That means the model is treating the expansion flag as advice, not as a contract. CP-17 is the same class of failure, except the fallback path is more expensive and hits timeout. This is not primarily a retrieval problem, memory problem, or context-budget problem. It is a **workflow determinism problem**.  Brief[^1]

That behavior is unsurprising in light of the broader literature. ReAct-style systems and decomposition papers show that models can reason and plan effectively, but they do not guarantee consistent branching when branching is left to free-form generation. SELF-DISCOVER and related work suggest that letting the model invent its own reasoning structure can improve difficult reasoning tasks, but that is an argument for the model generating task structure, not for the model owning workflow guarantees. Production agent frameworks increasingly separate those concerns.  arXiv[^4]

### 2) CP-17 is likely a “failed decomposition + no guarded fallback” path

The fact pattern is telling: baseline succeeded at about 272s with expansion, run-04 times out at about 187s with no expansion event, and CP-16 under similar architecture returns a decent single-pass answer in about 29s. That strongly suggests CP-17 is not simply “harder,” but that the DECOMPOSE prompt shape is pushing the model into a long pre-planning or full-answer attempt without transitioning into the sub-agent phase. In other words: the system likely has a **serial monologue failure mode** when decomposition is recommended but not enforced.  Brief[^1]

This is also consistent with current production advice around tools and long-running agents: tool calls should be explicit and observable; deterministic harnesses should own sequencing, retries, and timeouts; and long-running flows should degrade gracefully instead of surfacing raw timeout failures. Anthropic’s material on multi-agent research and harnesses emphasizes orchestration, progress visibility, and parallel workstreams, not “ask the model nicely and hope.” LangChain’s async subagent patterns similarly frame long-running parallelism as an explicit orchestration pattern.  Anthropic[^5]

### 3) CP-19 is partly a classifier bug and partly a recall-path design bug

The brief is right that the classifier misses implicit recall phrasing like “Going back to the beginning — what was our primary database again?” That should not be `conversational`. But I would push one step further: the bad answer also suggests the system lacks a robust **fact recall rescue path** even when explicit history is available. The model said it did not know, despite the fact being in-session and apparently still present. So yes, the first defect is classification. But the second defect is that the answering stack appears too willing to continue in ordinary chat mode when a response should have triggered a targeted session lookup or quote-back mechanism.  Brief[^1]

That second point matters because long-context research repeatedly shows that models do not use available context uniformly well. “Lost in the Middle” found that models often miss relevant information depending on its position in context, and benchmarks like RULER were created precisely because naive “the text is in context” assumptions are not reliable. Memory-system work like MemGPT and newer long-term memory tooling exists because having information in the transcript is not the same as retrieving it robustly at the right time. So I agree it is not a trimming artifact, but I do not think “fix the classifier and we’re done” is fully safe.  arXiv[^6]

## Are other projects seeing the same behavior?

Yes. The broad pattern is common:

- **Non-deterministic branching/tool use** is a known issue, which is why frameworks offer supervisor-as-tools, graph workflows, and strict tool-use modes rather than relying on pure prompt instructions.  LangChain Docs[^3]
- **Long-context recall failures despite information being present** are well documented in “Lost in the Middle” and RULER.  arXiv[^6]
- **Need for explicit memory tiers or extracted semantic memory** is exactly what systems like MemGPT and LangMem address.  arXiv[^7]
- **Production advice is trending toward hybrid systems**: deterministic code for workflow and control, LLM for synthesis and localized reasoning. Anthropic explicitly says the most successful teams generally use simple, composable patterns rather than highly autonomous spaghetti agents.  Anthropic[^8]

So the behavior in your brief is not weird. It is almost textbook for a system where the LLM still owns one control decision too many.

## My recommended target architecture

### A. Split strategy selection from strategy execution

Keep:
- Stage 4 intent classification
- Stage 5 decomposition assessment

Change:
- If Stage 5 returns `SINGLE`, call the primary agent normally.
- If Stage 5 returns `HYBRID` or `DECOMPOSE`, do **not** let the primary agent decide whether expansion happens. Instead enter a deterministic expansion controller.

That controller should:
1. Ask the LLM for a structured subtask plan.
2. Validate the plan against a schema.
3. If valid, spawn subagents deterministically.
4. If invalid or empty, use a deterministic fallback planner.
5. Synthesize results with the primary agent.

This matches the architecture bias in LangGraph supervisors, Google ADK workflow agents, and tool-based subagent supervision.  Brief[^1]  LangChain Docs[^3]

### B. Replace “natural language expansion instruction” with a tool contract

Instead of putting “you have been asked to expand this task” in the system prompt, expose something like:

```text
plan_subtasks(query, strategy, max_tasks, required_dimensions) -> {tasks:[...]}
```

or directly:

```text
spawn_sub_agents(tasks:[{name, goal, constraints, expected_output}])
```

When strategy is HYBRID/DECOMPOSE, set tool choice to required where your model stack supports it, or emulate the same behavior in the executor by refusing any first response that is not a valid plan/tool call. OpenAI’s function-calling/structured-output guidance and Anthropic’s tool-use docs both support this style of explicit application bridging.  OpenAI Developers[^9]

### C. Add a deterministic fallback planner

If the planner output is empty, malformed, or too shallow, do not continue to final answer mode. Fall back to a cheap deterministic decomposition rule:

- Extract enumerated entities or dimensions from the prompt.
- For HYBRID, cap at 2–3 subtasks.
- For DECOMPOSE, split by explicit evaluation dimensions plus recommendation.
- Then launch subagents anyway.

For CP-17, the fallback planner could trivially produce:
1. Compare Redis/Memcached/Hazelcast performance.
2. Compare memory management approaches.
3. Compare operational complexity and produce recommendation for 10k rps.

That alone would likely prevent the monologue timeout path. This is exactly the sort of “deterministic code wraps model reasoning” move that practical frameworks favor.  Brief[^1]  Anthropic[^8]

### D. Separate planning latency budget from synthesis latency budget

CP-17 strongly suggests you need per-phase time budgets, not just a single 180s envelope. I would set:

- Planner budget: 5–15s
- Subagent budget: per-worker + global wall-clock cap
- Synthesizer budget: 20–40s
- If planner misses budget, deterministic fallback planner
- If some subagents fail, synthesize partials
- If synthesis misses budget, return compact recommendation + “detailed comparison available on retry”

That is how you prevent raw timeout surfacing to users: enforce phase boundaries and degrade at boundaries, not at the very end. Anthropic’s harness guidance and async-subagent framing point in this direction.  Anthropic[^10]

### E. Add a recall controller for “session fact lookup”

For CP-19, I would not rely only on regex classification. Add a lightweight recall controller:

- Detect cues like `again`, `earlier`, `back to`, `at the beginning`, `what was our`, `what did we decide`.
- If detected, run a deterministic session fact lookup over recent turns or a compact session summary before asking the model to answer.
- Inject the retrieved evidence in a tiny, explicit structure:
  - `session_fact_candidates: [{"turn":2,"fact":"Primary database is PostgreSQL"}]`

That gives you a robust path even if the model under-attends older turns. This mirrors the general rationale behind memory-tier systems and semantic memory extraction.  Brief[^1]  arXiv[^7]

## Review of the conversation paths and assertions

### CP-16 assertion review

The current assertion that `hybrid_expansion_start` must exist is valid **if expansion is a contractual behavior**. Right now it is not; it is only an intended behavior. So the failing assertion is revealing a real design bug, not a bad test. The issue is that the implementation does not yet honor the architectural contract implied by the assertion.  Brief[^1]

However, once you fix the architecture, I would keep two assertions:
1. `strategy == hybrid`
2. `expansion trace exists with >=1 worker`
3. response covers all requested dimensions

That gives you contract-level plus outcome-level coverage. Testing only response quality would hide the very orchestration bug you are trying to solve.  Brief[^1]

### CP-17 assertion review

The assertions are also valid, but I would add one more that is more diagnostic:
- `planner_phase_completed within X seconds`

Right now you only discover absence of expansion after the fact. A planner-phase assertion would tell you whether the system failed before planning, during planning, or after planning. That matters because CP-17’s timeout is probably a planner/executor gap, not simply a subagent problem.  Brief[^1]

I would also log:
- plan token count
- number of numbered items parsed
- parse success/failure reason
- whether fallback planner engaged
- first token latency and total output tokens for the planner step

Without that, CP-17 is still a little too “black box.”  Brief[^1]

### CP-19 assertion review

The existing assertion on `intent_classified.task_type == memory_recall` is good and should stay. But I would add a semantic answer assertion too:
- answer contains or cites `PostgreSQL`

Why? Because even if you fix the classifier, you still want to know whether the downstream recall path works. The brief says the content error is not due to trimming because the fact is present in context; that is exactly why you should test both classification and answer correctness separately.  Brief[^1]

## Answers to the open questions

### Q1 — where should expansion enforcement live?

My answer: **B first, C second, A only as support, not D.**

- **B: tool-call enforcement** is the cleanest design. It makes expansion explicit, observable, and testable. It matches supervisor/subagent patterns in LangChain and broader function/tool-calling guidance.  LangChain Docs[^3]
- **C: executor gate** is the safety net. If the required plan/tool call does not occur promptly, retry once with a stricter scaffold or deterministic fallback planner.  Google GitHub[^11]
- **A: stronger system prompt** can help, but it is not enough by itself. Prompt-only compliance is exactly what is failing now.  Brief[^1]
- **D: accept non-determinism** is wrong for this part of the stack. It weakens the meaning of your gateway and makes your telemetry unverifiable.  Brief[^1]

### Q2 — why does DECOMPOSE time out?

Most likely because DECOMPOSE currently encourages a longer internal planning monologue or a broader first-pass answer attempt, and because there is no enforced transition into explicit expansion. The prompt also names three systems and three evaluation dimensions plus recommendation, which is exactly the kind of structure that invites a big serial answer if the model is not forced into planning-then-execute mode. The evidence from baseline versus run-04 supports that interpretation.  Brief[^1]

The fix is architectural:
- bounded planner phase
- deterministic fallback planner
- parallel subagents with independent time budgets
- partial synthesis if one worker fails
- no raw timeout returned to user unless the entire envelope is exhausted

That is how you turn “expensive monologue” into “bounded orchestration.”  Anthropic[^5]

### Q3 — should the classifier use context?

My answer: **B plus a tiny amount of C.**

I would not make Stage 4 a general history-aware free-form classifier. That muddies a nice deterministic stage. But I would allow a very limited contextual peek for ambiguous retrospective questions. Concretely:

- first pass: regex/heuristics on raw text
- if uncertain and cues like `again`, `earlier`, `back`, `beginning`, `what was our X` appear:
  - inspect a tiny recent-history metadata layer or extracted session facts
  - then classify as `memory_recall` if a prior matching noun/fact exists

So the right architecture is a **hybrid deterministic classifier**, not pure regex and not full LLM classification. Option D post-hoc retry is useful as a rescue, but it should be backup, not primary design. The literature on long-context behavior is exactly why I would not trust “the fact is in context, the model will find it.”  Brief[^1]  arXiv[^6]

### Q4 — is telemetry the right proxy for quality?

Telemetry is the right proxy for **workflow compliance**, not for final answer quality. Those are different test layers. In CP-16, a good answer without expansion still violates the orchestration contract implied by the gateway. So I would not replace telemetry assertions with response-length heuristics. I would **add** quality assertions beside telemetry, not instead of telemetry.  Brief[^1]

A good evaluation stack here is:

- **Layer 1: gateway correctness**  
  intent, complexity, strategy
- **Layer 2: workflow correctness**  
  planner called, expansion started, workers completed, fallback engaged if needed
- **Layer 3: answer correctness**  
  coverage, recommendation quality, mentions expected entities
- **Layer 4: efficiency**  
  latency, token usage, retries, timeout incidence

That layered design is much closer to how production agent systems are debugged and improved.  Anthropic[^8]

## Concrete changes I would make next

1. **Make expansion mandatory in code** when Stage 5 returns HYBRID/DECOMPOSE. Do not leave it to the model.  Brief[^1]  
2. **Introduce a plan schema** and require structured plan output or tool call.  OpenAI Developers[^12]  
3. **Add a deterministic fallback planner** if the LLM planner fails or stalls.  
4. **Split timeout budgets by phase** and never let a planner monopolize the full request budget.  
5. **Add planner telemetry**: parse count, plan depth, fallback used, per-phase timing.  
6. **Add a session fact lookup path** for retrospective questions.  
7. **Upgrade CP-19 tests** to assert both classification and answer correctness.  
8. **Add ambiguity cues** for implicit recall instead of relying only on explicit “do you remember” regexes.  Brief[^1]  
9. **Create adversarial eval variants**:
   - same CP-16 prompt phrased 10 ways
   - CP-17 with 2, 3, and 4 explicit dimensions
   - CP-19 with `again`, `earlier`, `at the start`, `remind me`, `what did we say`
   This will tell you whether the problem is lexical brittleness, plan parsing brittleness, or runtime orchestration.  
10. **Track “strategy mismatch rate”** as a first-class metric: `% of HYBRID/DECOMPOSE turns with no expansion trace`. That is your real canary.  Brief[^1]

## Final judgment

My strongest conclusion is this: **the system is one design shift away from becoming much more reliable**. The brief already shows the gateway is often right. The main flaw is that the system still asks the LLM to make one decision that should belong to the runtime: whether a required branch actually executes. CP-16 and CP-17 are symptoms of that. CP-19 is a smaller but related issue: the system also asks the raw chat model to perform recall behavior that should be partly scaffolded by a dedicated retrieval/controller path.  Brief[^1]

So I would not treat these as three separate bugs. I would treat them as one architectural principle violation: **control decisions that matter to correctness are still too latent in model behavior**. Move those into deterministic orchestration, keep the model for localized planning/synthesis, and your evals should become both more stable and more meaningful.  Anthropic[^8]

If you want, I can turn this into a concrete remediation plan with:
- proposed state machine,
- telemetry schema additions,
- CP-16/17/19 revised assertions,
- and pseudocode for the planner/executor split.

## References

[^1]: evaluation-run-04-second-opinion-brief.md
[^2]: https://docs.langchain.com/oss/python/langgraph/workflows-agents
[^3]: https://docs.langchain.com/oss/python/langchain/multi-agent/subagents
[^4]: https://arxiv.org/pdf/2210.03629
[^5]: https://www.anthropic.com/engineering/multi-agent-research-system
[^6]: https://arxiv.org/abs/2307.03172
[^7]: https://arxiv.org/abs/2310.08560
[^8]: https://www.anthropic.com/research/building-effective-agents
[^9]: https://developers.openai.com/api/docs/guides/function-calling/
[^10]: https://www.anthropic.com/engineering/effective-harnesses-for-long-running-agents
[^11]: https://google.github.io/adk-docs/agents/workflow-agents/
[^12]: https://developers.openai.com/api/docs/guides/structured-outputs/
