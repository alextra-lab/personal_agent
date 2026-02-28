# Hypothesis Log — Personal Local AI Collaborator

> This log applies hypothesis-driven development (HDD) to system and architecture evolution.
> Each hypothesis must be testable, measurable, and reversible.

---

## H-001: Graph-First Orchestration Improves Safety & Observability

We believe that:
Using a deterministic graph/state-machine orchestrator as the backbone, with LLM cognition only inside bounded steps, will improve safety, explainability, and operational observability.

We will measure this by:

- Ability to reconstruct execution paths from logs.
- Incident debugging time (how fast can the user understand “what happened”).
- Number of unexpected tool calls / anomalous sequences detected.

Success Criteria:

- ≥ 95% of meaningful tasks have fully reconstructable traces.
- No “mystery transitions” or uncontrolled execution paths in normal use.
- Supervisor flags anomalies instead of discovering surprises accidentally.

Status: Proposed
Notes:

---

## H-002: Limited Multi-Agent Cognition Is Sufficient for MVP

We believe that:
A single Reasoning agent plus a Planner+Critic pattern on selected tasks will achieve most benefits of multi-agent reasoning without the instability and complexity of free conversational multi-agent systems.

We will measure this by:

- Comparing quality of output with vs without Critic.
- Error rate in coding/system reasoning tasks.
- User qualitative rating of “insightfulness” + correctness.

Success Criteria:

- ≥ 30% improvement in task quality when Critic used vs single-agent.
- No need for larger agent teams in ≥ 80% of tasks.
- No major reproducibility regressions introduced.

Status: Proposed
Notes:

---

## H-003: Safety Gateway Primitives Provide Sufficient Governance

We believe that:
A small deterministic “Safety Gateway” with primitives such as capability checking, sandbox execution, outbound filtering, human checkpoints, quotas, and mandatory logging will be sufficient to safely govern agent behavior without stifling usefulness.

We will measure this by:

- Number of blocked actions that would have been dangerous.
- Number of false-positive safety interruptions.
- System usability impact.

Success Criteria:

- ≥ 90% of risky situations mitigated by gateway checks.
- False positives remain “annoying but acceptable”.
- Agent remains practically usable.

Status: Proposed
Notes:

---

## H-004: Structured Captains Log Improves Trust & System Learning

We believe that:
A structured introspection log with self-analysis, ideas, and recorded reasoning will improve trust, self-awareness, and system evolution quality.

We will measure this by:

- clarity of explanations in task outcomes
- ability to understand agent decisions post-hoc
- number of useful improvements originating from reflections

Success Criteria:

- Self-reflections provide meaningful technical value.
- The user perceives increased trustworthiness.
- ≥ 1 meaningful improvement per month originates from introspection.

Status: Proposed
Notes:

---

## H-005: Metacognitive Monitoring Improves Task Success & Safety

We believe that:
Adding explicit metacognitive monitoring (confidence estimation, uncertainty tracking, error detection) to the orchestrator will improve task success rates, reduce harmful actions, and enable better mode transitions in the Brainstem service.

We will measure this by:

- Task success rate with vs without metacognitive monitoring
- Number of false starts/bad paths avoided via low-confidence detection
- Correlation between confidence estimates and actual task outcomes
- Frequency of appropriate ALERT mode triggers

Success Criteria:

- ≥ 15% improvement in task success rate when metacognition is enabled
- ≥ 80% accuracy in confidence calibration (low confidence → actual failure)
- Zero high-confidence failures that cause system damage
- Metacognitive signals trigger appropriate mode changes ≥ 90% of time

Status: Proposed
Notes: Based on brain systems research showing metacognitive networks as essential for robust cognition. This is Phase 1 of cognitive architecture implementation.

---

## H-006: Event-Driven Captain's Log Service Enables Continuous Learning

We believe that:
Decoupling Captain's Log reflection into a standalone, event-driven service that monitors telemetry streams and reflects continuously (rather than per-task) will enable deeper pattern detection, zero performance impact, and emergent insights across execution history.

We will measure this by:

- **Pattern detection**: Number of multi-task patterns identified (vs. single-task reflections)
- **Performance impact**: Main agent latency with vs. without reflection service
- **Insight quality**: Percentage of reflections proposing actionable improvements
- **Temporal patterns**: Detection of time-based patterns (e.g., "Tool X fails after 6pm")

Success Criteria:

- ≥ 30% increase in pattern-based insights (vs. single-task reflections)
- Zero measurable latency impact on main agent
- ≥ 3 temporal/cross-task patterns detected per week
- Service can run on separate machine/heavier model without affecting agent

Status: Proposed (2026-01-14)

**Architecture Considerations:**
- Event-driven: Subscribe to telemetry stream (WebSocket or file watcher)
- Standalone service: Separate process/container, scales independently
- Enhanced telemetry: Would require richer state snapshots, performance metrics
- Batch analysis: Reflects on N executions, not just single tasks
- Monitoring endpoint: `/v1/monitor` or WebSocket for real-time streaming

**Related:**
- ADR-0004 (Telemetry and Metrics)
- ADR-0010 (Structured LLM Outputs)
- Current implementation: `captains_log/` (task-coupled reflection)

Notes: Aligns with sidecar pattern (Kubernetes), CQRS, event sourcing. Would enable continuous learning without blocking user interactions.

---

## H-007: Meta-Agent Query/Response Critic Improves Interaction Quality

We believe that:
A secondary "critic agent" that analyzes user queries and agent responses, suggesting improvements and alternative formulations, will improve both user prompting skills and agent response quality over time through a feedback loop.

We will measure this by:

- **Query improvement**: Quality delta between original and suggested queries (via A/B testing)
- **Response quality**: User satisfaction ratings with vs. without critic feedback
- **Learning loop**: Number of critic suggestions integrated into routing/tool design
- **User skill growth**: Improvement in user query quality over time (measured via agent success rates)

Success Criteria:

- ≥ 20% improvement in task success when using critic-suggested query reformulations
- ≥ 70% of critic suggestions rated as "helpful" by user
- ≥ 2 critic insights per month integrated into agent improvements
- User query quality improves ≥ 15% over 3 months (measured by first-try success rate)

Status: Proposed (2026-01-14)

**Architecture Considerations:**
- Model role: `ModelRole.CRITIC` (uses reasoning model)
- Post-execution hook: Runs after main agent responds
- Opt-in: User triggers via "expand on my request" or "critique this answer"
- Capabilities:
  1. Query enhancement (suggest 3-5 improved phrasings)
  2. Answer rating (1-10 score + reasoning)
  3. Alternative formulations (better ways to ask)
  4. A/B testing (runs improved queries, compares results)
- Experiment logging: Logs to `experiments/` for analysis

**Use Cases:**
- User types vague question → Critic suggests clarifications before execution
- After answer → Critic rates quality, suggests follow-ups
- User says "expand my request" → Critic provides 5 enhanced versions
- Continuous improvement: Patterns feed back to routing logic

**Related:**
- H-005 (Metacognitive Monitoring)
- ADR-0010 (Structured LLM Outputs)
- E-002 (Planner-Critic Quality evaluation)

Notes: Combines metacognition with continuous improvement. Could be phased: (1) Post-hoc critique, (2) Pre-execution query enhancement, (3) Autonomous A/B testing.

---

## H-008: DSPy Framework Adoption Improves Code Maintainability & LLM Program Quality

We believe that:
Adopting DSPy (Stanford NLP's declarative LLM framework) for complex LLM workflows (reflection generation, routing decisions, multi-stage reasoning) will reduce code complexity, enable systematic prompt optimization, and improve output reliability compared to manual prompt engineering.

We will measure this by:

- **Code reduction**: Lines of code comparison (manual vs. DSPy) for reflection generation
- **Reliability**: Parse failure rates for structured outputs (Captain's Log, routing decisions)
- **Maintainability**: Time to modify/extend LLM workflows with DSPy vs. manual
- **Optimization impact**: Quality improvement after running DSPy optimizers (MIPROv2, BootstrapFewShot)
- **Learning value**: Developer understanding of LLM system design principles

Success Criteria:

- **Phase 1 (Prototype - E-008)**: ≥30% code reduction in at least 1 test case (reflection or routing)
- **Phase 2 (Integration)**: ≤5% parse failures for DSPy-based structured outputs
- **Phase 3 (Optimization)**: ≥15% quality improvement after running DSPy optimizer on Captain's Log reflections
- **Overall**: Code maintainability improved (subjective assessment) AND no debugging/control regressions

Status: Under Investigation (2026-01-15)

**Experiment Plan:**

**E-008: DSPy Prototype Evaluation** (Days 26-27, time-boxed 1-2 days)
- Test Case A: Captain's Log reflection generation (manual vs. DSPy ChainOfThought)
- Test Case B: Router decision logic (manual vs. DSPy signature)
- Test Case C: Tool-using agent (manual orchestrator vs. DSPy ReAct)
- Metrics: Code complexity, parse failures, latency, debuggability, control

**Decision Paths:**
- **Option A (Full Adoption)**: DSPy as core framework for all LLM interactions
  - Benefits: Systematic optimization, composable modules, research-backed patterns
  - Risks: Framework lock-in, learning curve, abstraction layer complexity

- **Option B (Selective Adoption)** ⭐ **RECOMMENDED**:
  - Use DSPy for Captain's Log reflection (complex structured output)
  - Keep `instructor` for simple structured outputs (ADR-0010)
  - Apply DSPy patterns manually where framework overhead not justified
  - Benefits: Best of both worlds, lower risk, flexible

- **Option C (Defer)**: Proceed with `instructor`, revisit DSPy post-MVP
  - Choose if prototype shows poor fit with LM Studio or excessive complexity

**Integration Points:**
1. **Captain's Log reflection** (Week 5, Day 31-32): Replace manual JSON prompt with DSPy ChainOfThought signature
2. **Router decision logic** (Optional): Use DSPy signature for cleaner routing decisions
3. **Cognitive architecture** (Weeks 8-16): Use DSPy modules for planning, metacognition, multi-stage reasoning
4. **Optimization** (Week 6+): Run MIPROv2 optimizer on reflection quality with evaluation harness

**Alignment with Architecture:**
- ✅ Type-safe, declarative interfaces (signatures ≈ Pydantic models)
- ✅ Composable modules (matches homeostasis control loop design)
- ✅ Local LLM support (works with LM Studio via OpenAI-compatible endpoint)
- ✅ Research-oriented (learning DSPy = learning principled LM system design)
- ⚠️ Framework complexity (adds abstraction layer, requires telemetry integration)
- ⚠️ Optimization dependencies (needs evaluation datasets, compute for optimizers)

**Related:**
- ADR-0010 (Structured LLM Outputs via Pydantic/instructor)
- ADR-0003 (Model Stack Architecture)
- H-005 (Metacognitive Monitoring - DSPy could provide composable cognitive modules)
- E-008 (DSPy Prototype Evaluation - in progress)
- Research: `../research/dspy_framework_analysis_2026-01-15.md` (comprehensive assessment)

**Next Steps:**
1. Review analysis document: `../research/dspy_framework_analysis_2026-01-15.md`
2. Approve/modify E-008 experiment plan
3. Execute prototype (Days 26-27)
4. Decide: Adopt (A or B) vs. Defer (C) based on E-008 findings

Notes: DSPy represents a shift from "prompt engineering" to "LLM program compilation". Even if not fully adopted, studying DSPy provides valuable insights into systematic LLM system design, optimization algorithms (MIPROv2, GEPA), and composability patterns. Prototype is low-risk (time-boxed to 1-2 days) with high learning value.

---
