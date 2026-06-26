# Self-Diagnosing, Self-Improving Seshat — Design Brief

> **Status:** Design brief — *seeds a future `/adr` cycle* (codex-reviewed, full architecture). Not itself an ADR.
> **Date:** 2026-06-26
> **Author:** adr session (Opus)
> **Origin:** Triage of the auto-promoted KGQ anomaly batch (FRE-423/424/425/428/429/430 + FRE-446), 2026-06-26. The triage exposed that the anomaly→Linear pipeline is a *category error*; the conversation that followed reframed it into the architecture sketched here.
> **Related:** ADR-0030 (insight→Linear promotion — to be superseded in part), ADR-0060 (KGQ governance stream — superseded in part), ADR-0040 (issue-budget gate / FRE-598), ADR-0065 (cost gate — the budget primitive), ADR-0087 (Memory Recall Quality pillar). Implementation stopgap already filed: **FRE-620** (detector correctness + recalibration) and **FRE-621** (graph hygiene).

---

## 1. The triggering failure

The knowledge-graph quality monitor detects ~8 health conditions daily and auto-promotes each breach into a Linear ticket via the ADR-0030 pipeline. It produced **duplicate, low-value tickets** that recur no matter how they're dispositioned, and a deep-dive found the dedup machinery (fingerprints, tombstones, Linear-archive coupling, ephemeral on-disk ledgers) was an ever-more-fragile band-aid over a model that was wrong at the root.

FRE-620 fixes the *calibration* (a stale `:Conversation`→`:Turn` label bug, missing thresholds, no promotion floor). **This brief is the architecture behind it** — what the pipeline should have been.

## 2. The root cause: a category error

Three conflations, each one level deeper than the last:

1. **Discrete artifact from a standing condition.** "empty-description rate is 22.8%" is a *level* — continuously true, no natural cardinality. Minting a discrete work-item (a ticket) each time it's re-observed makes "duplicates" the *correct* output of the wrong model. You cannot dedup your way out of a type mismatch.
2. **Detection treated as the deliverable.** The cancelled tickets' entire "How" section was a **checklist for a human to go investigate** ("check the GQ jsonl, run the quality monitor, grep ES, inspect Neo4j"). The system noticed a number was off and *punted the valuable part — the diagnosis — to a human.* In an AI harness with those tools in-process, that is backwards.
3. **Observability conflated with cognition.** A *condition* (a metric out of band — fixed set, auto-resolving, has a current value) and a *proposal* (a generated idea for a change — open-ended, human-judged, never auto-resolves) were forced through one promotion mechanism. It is wrong for both, in opposite directions: too much machinery for conditions (hence the duplicates), too crude for proposals.

## 3. The reframe

**Detection is a stateless trigger. The deliverable is an *investigated proposal* — a root-cause diagnosis plus a recommended remediation, staged for human approval.**

The gauge was never the product; it is the *trigger*. The product is the cognition the harness can uniquely supply: investigate *why*, and propose *what to do*. Two trigger sources — a sustained metric breach, or Seshat's own reflection — feed **one shared "investigate & propose" faculty** and produce the same kind of artifact, handled the same way.

The trigger source only sets the *wrapper*:
- **Health-triggered** → fixed, enumerable set, so "is there an open investigation for metric X?" is a bounded, no-history dedup. Clean.
- **Reflection-triggered** → open-ended, so dedup is semantic ("have I proposed this before?") — the genuinely hard residue, but it *softens* when the unit is a deliberate, expensive, human-reviewed investigation rather than an auto-filed ticket.

## 4. The unifying control: steered investigation + deterministic gating

The piece that makes this *safe to build* rather than a runaway risk: **the investigation must be steered and bounded**, not free-form spelunking that can loop and burn tokens.

- **Skills as diagnostic runbooks.** Each known problem class gets a *deterministic procedure* (a skill / runbook) the harness executes with its tools — the SRE "diagnosis tree" pattern. This makes investigations repeatable, auditable, and cheap, and keeps the LLM on rails instead of improvising an unbounded search.
- **Deterministic gating around the cognition.** Hard limits independent of the model's judgment: per-investigation token budget and step cap, loop/no-progress detection, and a circuit breaker on token-velocity. The literature is blunt here — *"you cannot ship autonomous agents without billing limits."* A logic-loop or an unsolvable problem must hit a wall and emit "could not determine root cause within budget," never spiral.

This is the line between "an agent that might run away" and "a bounded diagnostic routine that happens to use an LLM."

## 5. This is a known shape (SOTA grounding)

The architecture is **human-in-the-loop [MAPE-K](https://arxiv.org/html/2401.16382v3)** — IBM's autonomic-computing reference loop (2005): **M**onitor → **A**nalyze → **P**lan → **E**xecute over shared **K**nowledge, the canonical model for *self-managing* systems and their four self-* properties (self-configuration, self-optimization, self-healing, self-protection). Our mapping:

| MAPE-K | Seshat | Note |
|---|---|---|
| **Monitor** | the fixed gauges (KG-health, cost, errors) | stateless, recomputed |
| **Analyze** | the steered, skill-driven investigation | the "diagnose *why*" faculty |
| **Plan** | the structured proposal (diagnosis + remediation) | confidence + evidence |
| **Execute** | **a human** approves → change applied | *we deliberately keep the human in Execute* |
| **Knowledge** | memory of past investigations + outcomes | where "self-improving" lives |

The modern agentic literature fills in each stage:

- **Analyze (diagnose).** LLM-based **root-cause-analysis / AIOps** agents are a live field — automated incident diagnosis with reported ~92% RCA accuracy and large MTTD reductions vs. manual baselines, using exactly the "runbook / diagnosis-tree" steering we propose ([Exploring LLM-based Agents for RCA](https://arxiv.org/html/2403.04123v1); [TN-AutoRCA, self-improving alarm-based RCA](https://arxiv.org/pdf/2507.18190)).
- **The reflection faculty.** [Reflexion](https://arxiv.org/pdf/2303.11366) (verbal reinforcement learning — written self-assessments in episodic memory, no gradient updates) and Self-Refine / CRITIC / Chain-of-Verification are the in-context machinery for the analyze→propose step.
- **Self-improvement / the Knowledge loop.** Surveys of **self-evolving agents** frame the design space as *what / when / how / where* to evolve, and experience-lifecycle methods (ExpeL, EvolveR) consolidate past interactions into reusable, retrievable principles ([survey](https://arxiv.org/abs/2507.21046); [comprehensive survey](https://arxiv.org/pdf/2508.07407)). This is the basis for letting *which proposals worked* improve future investigations.
- **The deterministic gate.** Production guidance on **agent runaway / cost control**: circuit breakers on token-velocity (a runaway loop caught within 60s at 10k tok/min), loop detection via consumption-rate-without-progress, and hard budget ceilings enforced at the infrastructure layer, not just `max_tokens` ([cost-control patterns](https://sanj.dev/post/llm-cost-control); [budget guards](https://www.nexgismo.com/blog/ai-agent-budget-guards-stop-runaway-api-costs)).

What we add to the textbook: MAPE-K assumes autonomous Execute; **we hold Execute as a human-approval gate** (propose-not-apply), because the "managed resource" here is *Seshat itself* — this is self-modification, where the safety boundary has to be hard.

## 6. Proposed architecture (sketch — the ADR hardens this)

```
 ┌─ Monitor ──────────────┐   ┌─ Reflection ───────────┐
 │ fixed gauges (8 KG +   │   │ Seshat notices a        │
 │ cost + error), state-  │   │ pattern / opportunity   │
 │ less, recomputed       │   └───────────┬─────────────┘
 └───────────┬────────────┘               │
             │  Trigger (sustained+severe) │  Trigger (novel)
             ▼                             ▼
 ┌─ Trigger Gate (deterministic) ───────────────────────┐
 │ earns-an-investigation? hysteresis · severity ·      │
 │ nothing-open-for-it · budget available               │
 └───────────────────────┬──────────────────────────────┘
                         ▼
 ┌─ Investigate (steered cognition) ────────────────────┐
 │ skill/runbook per problem class · tools (Neo4j/ES/   │
 │ diagnostics) · HARD GATES: step cap, token budget,   │
 │ loop + circuit-breaker → root-cause diagnosis        │
 └───────────────────────┬──────────────────────────────┘
                         ▼
 ┌─ Propose ────────────────────────────────────────────┐
 │ structured: diagnosis + recommended change +         │
 │ confidence + evidence · status = AWAITING_APPROVAL   │
 │ · NEVER auto-applied                                 │
 └───────────────────────┬──────────────────────────────┘
                         ▼
 ┌─ Human disposition ──────────────────────────────────┐
 │ accept → work (human files the ticket / change)      │
 │ reject/mute → recorded as outcome                    │
 │ Linear is OUTPUT ONLY — never read to decide         │
 └───────────────────────┬──────────────────────────────┘
                         ▼
 ┌─ Knowledge (self-improving loop) ────────────────────┐
 │ which diagnoses were right · which proposals were    │
 │ accepted/worked → feeds future investigations        │
 │ (bounded, retention-governed — no forever-history)   │
 └──────────────────────────────────────────────────────┘
```

## 7. Where it maps to existing code (so the ADR starts grounded)

| Layer | Existing surface | Disposition |
|---|---|---|
| Monitor | `second_brain/quality_monitor.py` (`ConsolidationQualityMonitor`, the 8 gauges) | keep as the trigger source; FRE-620 fixes its correctness |
| Trigger gate | `captains_log/promotion.py` (`PromotionPipeline`, fingerprint dedup, Linear coupling) | **replace** — this is the broken layer |
| Investigate | `orchestrator/sub_agent.py` + the skills mechanism | the steered-investigation engine |
| Reflection trigger | `captains_log/` (DSPy `ChainOfThought`), `insights/`, `brainstem/` consolidation | the second trigger source |
| Propose | `CaptainLogEntry` type `CONFIG_PROPOSAL`, status `AWAITING_APPROVAL` | the *intent was always propose-for-approval* — keep the shape, supply the missing investigation, fix delivery |
| Gate primitive | `cost_gate/` (ADR-0065) | the budget/circuit-breaker substrate for the deterministic gate |
| Knowledge | `memory/` (`MemoryService`), Captain's Log outcomes | the experience-lifecycle loop |

The single most important deletion: **the Linear-coupled promotion + fingerprint/tombstone dedup goes away entirely.** Linear becomes a pure output of a *human* decision; the system never reads it to decide anything.

## 8. Open decisions for the `/adr` cycle

1. **Scope.** KG-health leg only first, or the whole proposal pipeline (cost + reflection) at once? (They converge on one faculty, which argues for designing the faculty once — but rolling out per trigger.)
2. **"What earns an investigation?"** The gating policy — sustained-breach hysteresis, severity floor, per-run and per-day budget. (Replaces the crude `seen_count≥3 / age≥7d`.)
3. **Skills-as-runbooks.** How diagnostic procedures are authored, stored, selected, and versioned; how deterministic vs. LLM-chosen the steps are.
4. **Self-modification governance.** Which classes of proposal are permitted; approval routing; the hard propose-not-apply boundary and its enforcement.
5. **The Knowledge / self-improving loop.** How outcomes (was the diagnosis right? was the proposal accepted? did it work?) feed back — *without* unbounded state.
6. **Semantic dedup for reflection-triggered proposals.** How far to go on "have I proposed this before."
7. **Retention everywhere.** Bounded state, explicit retention windows; no forever-history of anything.

## 9. Non-goals / hard guardrails (carry into the ADR)

- **No autonomous application of changes** — propose-only; a human owns Execute.
- **No reading Linear to make decisions** — Linear is fire-and-forget output.
- **No forever-history / unbounded ledgers** — every persistent structure is bounded + retention-governed.
- **No unbounded investigation** — hard token/step/loop gates; a stuck investigation fails closed with "undetermined within budget."

## 10. References

- MAPE-K / autonomic computing: [Architectural Conformance Checking for MAPE-K Self-Adaptive Systems](https://arxiv.org/html/2401.16382v3); [TinyAC: Autonomic Computing principles](https://arxiv.org/pdf/2509.19350).
- Self-evolving agents: [A Survey of Self-Evolving Agents — What/When/How/Where](https://arxiv.org/abs/2507.21046); [A Comprehensive Survey of Self-Evolving AI Agents](https://arxiv.org/pdf/2508.07407); [Awesome-Self-Evolving-Agents](https://github.com/XMUDeepLIT/Awesome-Self-Evolving-Agents).
- Reflection / self-correction: [Reflexion](https://arxiv.org/pdf/2303.11366); [awesome-llm-self-reflection](https://github.com/rxlqn/awesome-llm-self-reflection).
- LLM RCA / AIOps: [Exploring LLM-based Agents for Root Cause Analysis](https://arxiv.org/html/2403.04123v1); [TN-AutoRCA (self-improving RCA)](https://arxiv.org/pdf/2507.18190); [awesome-LLM-AIOps](https://github.com/Jun-jie-Huang/awesome-LLM-AIOps); [awesome-ai-sre](https://github.com/agamm/awesome-ai-sre).
- Runaway / cost guardrails: [LLM Cost Control](https://sanj.dev/post/llm-cost-control); [AI Agent Budget Guards](https://www.nexgismo.com/blog/ai-agent-budget-guards-stop-runaway-api-costs); [Rate Limiting AI Agents](https://www.truefoundry.com/blog/rate-limiting-ai-agents-preventing-llm-api-exhaustion).
