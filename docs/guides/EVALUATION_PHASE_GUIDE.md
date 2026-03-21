# Evaluation Phase Guide — HITL Researcher Instructions

**Date:** 2026-03-21
**Phase:** Slices 1 & 2 Implemented → Evaluation → Slice 3 Planning
**Audience:** You, the human-in-the-loop researcher and project owner
**Purpose:** What to do, study, and decide during the evaluation phase before Slice 3 begins

---

## What This Phase Is

You built the machine. Now you run it and watch. Slices 1 & 2 gave you:

- A Pre-LLM Gateway that classifies every request before the LLM sees it
- A single-brain agent that can expand (sub-agents) and delegate (external agents)
- A memory system with episodic→semantic promotion
- Telemetry that records everything to Elasticsearch

Slice 3 ("Intelligence") requires **data from real usage** to make good design decisions. You can't design proactive memory without knowing what the agent remembers poorly. You can't tune decomposition thresholds without seeing where HYBRID helps and where it adds overhead. You can't close the self-improvement loop without knowing which proposals the agent makes that are actually useful.

**This phase is the experiment. You are the instrument.**

---

## Action List

### Week 1: Get the System Running and Generating Data

- [ ] **Start infrastructure and agent service** — Get everything running end-to-end
  ```bash
  ./scripts/init-services.sh
  cd ../slm_server && ./start.sh  # separate terminal
  uv run uvicorn personal_agent.service.app:app --reload --port 9000  # separate terminal
  ```

- [ ] **Verify telemetry is flowing** — Open Kibana (`localhost:5601`) and confirm events appear in `agent-logs-*` index. Look for `intent_classification` and `gateway_output` events specifically. These are new from Slice 1 & 2.

- [ ] **Run 10-20 varied conversations** — Cover the full intent spectrum. Try:
  - Simple questions (CONVERSATIONAL)
  - "What do you remember about X?" (MEMORY_RECALL)
  - "Analyze this code/concept" (ANALYSIS)
  - "Plan a feature for the agent" (PLANNING)
  - "Delegate this to Claude Code" (DELEGATION)
  - "How could you improve yourself?" (SELF_IMPROVE)
  - "Check system health" (TOOL_USE)

- [ ] **Trigger at least one HYBRID expansion** — Ask something complex enough that the decomposition assessment selects HYBRID. Something like: "Analyze three different approaches to implementing procedural memory, comparing their trade-offs." Watch for sub-agent spawn events in ES.

- [ ] **Trigger at least one memory promotion** — Have enough conversations that the consolidation scheduler fires and `promote()` executes. Check Neo4j for entities with `memory_type=semantic`.

- [ ] **Compose at least one delegation package** — Ask the agent to prepare a task for Claude Code. Examine the DelegationPackage output — is the context sufficient? What's missing?

### Week 2: Observe Patterns and Run the Graphiti Experiment

- [ ] **Import Slice 2 Kibana dashboards** — Follow `docs/guides/KIBANA_EXPANSION_DASHBOARDS.md` and `docs/guides/KIBANA_INTENT_DASHBOARD.md`. These give you visual insight into what the gateway is doing.

- [ ] **Review intent classification accuracy** — In Kibana, look at intent distribution. Are requests classified correctly? Log any misclassifications (intent says CONVERSATIONAL but it was really ANALYSIS). This data directly informs Slice 3's "decomposition learning" feature.

- [ ] **Review decomposition decisions** — How often does the gateway select SINGLE vs. HYBRID vs. DELEGATE? Does it feel right? When HYBRID triggers, do the sub-agent results actually improve the final answer?

- [ ] **Review context budget behavior** — Are contexts getting trimmed? What's being cut? Is anything important being lost? This data informs Slice 3's "memory-informed context assembly."

- [ ] **Run the Graphiti experiment** — Follow the template at `docs/research/GRAPHITI_EXPERIMENT_REPORT.md`. This is a Slice 2 prerequisite for the Seshat backend decision in Slice 3. The 4 test scenarios (entity retrieval, temporal queries, dedup, scaling) must be completed with real data.

- [ ] **Log delegation outcomes** — Each time you use a delegation package with Claude Code (or don't), note what happened:
  - Did the context package contain enough information?
  - How many rounds did the external agent need?
  - What was missing from the delegation?
  - Rate satisfaction 1-5

### Week 3: Synthesize and Decide

- [ ] **Write evaluation summary** — Capture your findings in `docs/research/EVALUATION_PHASE_FINDINGS.md`:
  - Intent classification accuracy (estimate %)
  - Decomposition effectiveness (when does HYBRID help?)
  - Context budget adequacy (too aggressive? too loose?)
  - Memory promotion quality (are promoted facts useful?)
  - Delegation gaps (what context is always missing?)
  - Graphiti experiment results (better/worse/different than Neo4j?)

- [ ] **Make the Seshat backend decision** — Based on Graphiti experiment + current Neo4j experience, decide: stick with current Neo4j schema, adopt Graphiti, or hybrid approach? Document in an ADR.

- [ ] **Draft Slice 3 priorities** — Based on what you learned, rank the Slice 3 deliverables by value. Not everything in Slice 3 may be worth building. The evaluation data tells you what matters.

---

## Study List

### Your Code (Read in This Order)

These are the files that define what you built. Understanding them deeply will make the evaluation phase productive and Slice 3 planning precise.

| Priority | File | Why Read It | Time |
|----------|------|-------------|------|
| 1 | `docs/specs/COGNITIVE_ARCHITECTURE_REDESIGN_v2.md` | The entire vision. Sections 1-4 for architecture, Section 5 for Seshat, Section 6 for delegation, Section 7 for self-improvement, Section 8.3 for Slice 3 specifics. You wrote this — re-read it with fresh eyes after implementation. | 45 min |
| 2 | `src/personal_agent/request_gateway/pipeline.py` | How the 7-stage gateway actually flows. Trace a request from entry to GatewayOutput. | 15 min |
| 3 | `src/personal_agent/request_gateway/intent.py` | How intent classification works. Understand the heuristics — you'll be evaluating their accuracy. | 10 min |
| 4 | `src/personal_agent/request_gateway/decomposition.py` | The decision matrix. Understand when HYBRID triggers vs. SINGLE. The thresholds here are what Slice 3's "decomposition learning" will tune. | 10 min |
| 5 | `src/personal_agent/orchestrator/expansion.py` | HYBRID execution path. How sub-agents are spawned, how results are synthesized. | 15 min |
| 6 | `src/personal_agent/memory/protocol.py` | The MemoryProtocol interface. This is what Seshat's intelligence layer will sit on top of in Slice 3. | 10 min |
| 7 | `src/personal_agent/memory/promote.py` | The episodic→semantic promotion pipeline. Understand what triggers it and what quality looks like. | 10 min |
| 8 | `src/personal_agent/request_gateway/delegation.py` | How delegation packages are composed. What context gets included? What's missing? | 10 min |
| 9 | `src/personal_agent/insights/engine.py` | The insights engine that will power Slice 3's self-improvement loop. Understand what patterns it detects today. | 15 min |
| 10 | `src/personal_agent/orchestrator/prompts.py` | The system prompts. These shape the agent's behavior — understand what's being instructed. | 10 min |

### External Reading (Slice 3 Preparation)

These inform the design decisions you'll make for Slice 3.

| Priority | Resource | Why | Time |
|----------|----------|-----|------|
| 1 | **[Graphiti documentation](https://github.com/getzep/graphiti)** | You must run the experiment. Understand its data model, temporal queries, and entity dedup before comparing against your Neo4j approach. | 2 hrs |
| 2 | **[Anthropic: Building effective agents](https://www.anthropic.com/engineering/building-effective-agents)** | The design philosophy behind Claude Code's skill model — the same "one brain, many hands" approach your agent uses. Pay attention to how they handle context and tool selection. | 30 min |
| 3 | **[Anthropic: Claude Code best practices (Boris Cherny)](https://www.anthropic.com/engineering/claude-code-best-practices)** | How Claude Code uses skills, subagents, and verification loops. Directly relevant to Slice 3's dynamic skill loading and self-improvement loop. | 20 min |
| 4 | **[MemGPT / Letta](https://github.com/letta-ai/letta)** | The leading implementation of agentic memory management with tiered storage. Their approach to "memory editing" and "inner thoughts" is directly relevant to Seshat's lifecycle management (demote/forget) in Slice 3. | 1 hr |
| 5 | **[Zep Memory Framework](https://www.getzep.com/)** | Built by the Graphiti team. Understand their approach to long-term agent memory, fact extraction, and temporal knowledge. This contextualizes the Graphiti experiment. | 30 min |
| 6 | **[Reflexion: Language Agents with Verbal Reinforcement Learning](https://arxiv.org/abs/2303.11366)** | The foundational paper on self-reflective agents. Directly relevant to Slice 3's closed-loop self-improvement. How does verbal self-critique improve agent performance? | 45 min |
| 7 | **[Cognitive Architectures for Language Agents (CoALA)](https://arxiv.org/abs/2309.02427)** | Survey paper that taxonomizes cognitive architectures. Useful for positioning your agent's design against the academic landscape and identifying gaps. | 1 hr |
| 8 | **[Generative Agents: Interactive Simulacra](https://arxiv.org/abs/2304.03442)** | The Stanford paper on agents with memory, reflection, and planning. Their memory retrieval model (recency + importance + relevance) is what your multi-factor scoring evolves into. The "reflection" mechanism is relevant to Seshat's promotion pipeline. | 45 min |

### Optional Deep Dives (If Curious)

| Resource | Why |
|----------|-----|
| **[RAISE: Retrieval-Augmented Impersonation and Self-Evolving Agents](https://arxiv.org/abs/2401.02777)** | Self-evolving agent architecture — relevant to the "agent as architect" concept |
| **[AgentDB](https://github.com/agentdb/agentdb)** | Alternative to Neo4j for agent memory — worth evaluating alongside Graphiti |
| **[Voyager: LLM-Powered Agent with Skill Library](https://arxiv.org/abs/2305.16291)** | Procedural memory implemented as a skill library — directly relevant to Slice 3's procedural memory |

---

## Key Decisions Before Slice 3

These are the decisions the evaluation data should inform. Don't decide them upfront — let the data speak.

| Decision | What Informs It | Where to Document |
|----------|----------------|-------------------|
| **Seshat backend**: Keep Neo4j, adopt Graphiti, or hybrid? | Graphiti experiment results | New ADR |
| **Decomposition thresholds**: Are the current SINGLE/HYBRID/DELEGATE boundaries right? | Intent + decomposition telemetry | Update `decomposition.py` or new ADR |
| **Proactive memory**: Should Seshat inject context unprompted, or only when asked? | Memory recall quality observations | Slice 3 spec section |
| **Procedural memory scope**: What's worth remembering procedurally? Tool patterns? Delegation templates? Prompt strategies? | Delegation outcome data + insights engine patterns | Slice 3 spec section |
| **Self-improvement loop**: Is the agent's proposal quality high enough to close the loop? | Captain's Log entries + your satisfaction with proposals | Slice 3 spec section |
| **Sub-agent model routing**: Should sub-agents use the same 35B model or smaller 9B? | Expansion telemetry (quality vs. latency at different tiers) | Experiment design |

---

## What to Watch For (Researcher's Journal)

Keep a running log of observations. These are the qualitative insights that complement the quantitative telemetry. A simple markdown file (`docs/research/EVALUATION_JOURNAL.md`) works.

**Things worth noting:**

- Moments where the agent's response was notably better or worse because of the gateway
- Intent misclassifications — what did the agent think vs. what you meant?
- Times HYBRID expansion produced a better answer than SINGLE would have
- Times HYBRID expansion was pure overhead (slower, no quality gain)
- Memory queries that returned irrelevant results
- Memory queries that surfaced exactly the right context
- Delegation packages that were sufficient vs. insufficient
- Moments where you wished the agent remembered something it forgot
- Moments where you wished the agent forgot something stale
- Ideas that emerge for new tools, skills, or memory types
- Surprises — anything the system does that you didn't expect

---

## Timeline

| Week | Focus | Output |
|------|-------|--------|
| 1 | Run the system, generate diverse data, verify telemetry | 20+ conversations across all intent types, HYBRID triggered, promotion triggered, 1+ delegation |
| 2 | Observe patterns, run Graphiti experiment, import dashboards | Kibana dashboards populated, Graphiti experiment report filled, delegation outcomes logged |
| 3 | Synthesize, make decisions, draft Slice 3 scope | Evaluation findings doc, Seshat backend ADR, Slice 3 priority ranking |

---

## One More Thing

This evaluation phase is not busywork before the "real" Slice 3 work. **This IS the research.** The Redesign v2 spec (Section 1.1) says the system is simultaneously a conversational partner, a research platform, and a self-improving system. During this phase, you're using all three: having real conversations, studying how the architecture performs, and gathering the evidence that shapes what comes next.

The agent gets smarter in Slice 3. But *you* get smarter in this phase.

---

*This guide should be revisited and updated as the evaluation progresses. Save findings in `docs/research/`.*
