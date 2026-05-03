# RSI: SOTA vs. personal_agent/Seshat

> Research note · 2026-05-03  
> Purpose: Position personal_agent/Seshat on the recursive self-improvement maturity ladder and map a concrete path forward.

---

## 1. SOTA RSI in One Page

### The Four-Component Loop

Every RSI-capable system requires all four. Two without the other two is observation, not improvement.

| Component | What It Does |
|-----------|-------------|
| **Executor** | Performs tasks, calls tools, writes code |
| **Evaluator** | Judges output: improved / regressed / violated constraints |
| **Modifier** | Controlled mechanism to change prompts, tools, policies, code |
| **Gatekeeper** | Decides what gets promoted: testing, rollback, approval |

### Five RSI Types (risk-ordered)

| Type | What Changes | Risk |
|------|-------------|------|
| Prompt-level | System prompts, tool instructions, few-shot examples | Low |
| Tool-use | Call ordering, retry policies, search strategies | Low–Medium |
| Memory-level | What to store/forget, retrieval policies, schemas | Medium |
| Code-level | Harness, routing logic, orchestration | High |
| Architecture-level | Multi-agent topology, model selection, planner layouts | Very High |

### Maturity Ladder

| Level | Description | Key Signal |
|-------|-------------|-----------|
| **0** | Manual — humans inspect failures, hand-edit | No agent involvement |
| **1** | Agent-assisted — agent suggests, humans apply | Proposals exist, no automation |
| **2** | Sandboxed self-modification — candidate changes in isolated branch; harness evaluates | First real RSI |
| **3** | Gated auto-promotion — low-risk changes auto-apply if tests + safety gates pass | Feedback loop closes |
| **4** | Multi-agent improvement — specialized agents generate, critique, test, package | Distinct roles per agent |
| **5** | Open-ended — architecture, evaluator, permissions all modifiable | Research territory |

### Core Safety Invariant

The agent cannot modify its own evaluator, permissions, or promotion gates.  
Everything else can be scoped, sandboxed, and gated.

---

## 2. Naming Note: Two RSIs in One Project

"RSI" appears twice in this codebase with different meanings:

- **Relative Staleness Index** — the knowledge graph freshness system: entity tier classification (WARM / COOLING / COLD / DORMANT), exponential decay scoring, frequency boost, tier-based reranking (ADR-0060)
- **Recursive Self-Improvement** — the agent self-modification loop described in this document

These are not in conflict — the KG RSI is a *component* of the larger RSI loop. Staleness-aware memory retrieval makes the Evaluator more accurate; a better Evaluator produces better proposals; better proposals improve the Executor. The nested relationship is intentional.

---

## 3. The Multi-Model RSI Architecture

The RSI loop in personal_agent/Seshat is already heterogeneous — different models are used where they have comparative advantage, applied to the RSI loop itself, not just to user tasks.

| RSI Component | Model | Rationale |
|--------------|-------|-----------|
| **Executor** | Local SLM (Qwen3.6-35B-A3B, MLX, :8000) | Low latency, zero cost, sufficient for task execution |
| **Evaluator** | Claude Sonnet (LiteLLMClient, cloud) | Deep reasoning for post-task reflection; Captain's Log DSPy ChainOfThought |
| **Memory Extractor** | GPT-4-mini | Fast, cheap, accurate structured entity/relationship extraction → Neo4j |
| **HITL Gatekeeper** | Human (you) | Judgment on what gets built; irreversible decisions stay human |
| **Modifier** | External coding agent — Claude Code / Codex / Qwencode | [Planned] Implements approved tickets on dev branch, opens PR |

This is a deliberate routing strategy: each role has its own model budget matched to the cognitive demands of that role.

---

## 4. Current State Map

### System-to-Component Mapping

| System | RSI Role | Model | Status | Notes |
|--------|---------|-------|--------|-------|
| Orchestrator | Executor | Local SLM | ✅ Live | Runs tasks, calls tools, produces telemetry |
| Captain's Log (DSPy reflection) | Evaluator | Sonnet | ✅ Live | Post-task reflection → structured proposals with category/scope |
| Insights Engine | Evaluator | Sonnet + deterministic | ✅ Live | Cross-session analysis: delegation patterns, cost anomalies, KG trends |
| Second Brain quality monitor | Evaluator | Deterministic | ✅ Live | Entity ratio, relationship density, duplicate rate, orphan detection |
| Context quality monitor (ADR-0059) | Evaluator | Deterministic | ✅ Live | Compaction incident tracking, freshness scoring |
| KG quality stream (ADR-0060) | Evaluator | Deterministic | ✅ Live | Staleness decay + tier reranking (the KG RSI) |
| Error pattern monitoring (ADR-0056) | Evaluator | Deterministic | ✅ Live | Failure-path extraction, surgical fix suggestions |
| Entity extraction (Second Brain) | Memory | GPT-4-mini | ✅ Live | Entities + relationships → Neo4j |
| Brainstem mode FSM | Modifier (ops) | Deterministic | ✅ Live | Modifies operational mode and tool availability; hardcoded transition rules |
| Promotion pipeline | Modifier (proposer) | Deterministic | ✅ Live | Creates Linear tickets at seen_count ≥ 3, age ≥ 7 days |
| Linear feedback channel | Gatekeeper | Human (HITL) | ✅ Live | Approve / Reject / Deepen / Defer / Duplicate / Too Vague |
| **Modifier automation** | Modifier (implementer) | Coding agent (TBD) | ❌ Planned | Implements approved tickets → dev branch → PR |
| **Sandbox / branch eval** | Evaluator (candidate) | CI + tests | ❌ Planned | Baseline-vs-candidate comparison |
| **Outcome measurement** | Evaluator (post-apply) | Sonnet / deterministic | ❌ Planned | "Did this change help?" — closes the loop |
| **Rollback** | Gatekeeper | Automated | ❌ Planned | Revert if post-apply metrics degrade |

### Feedback Loops That Exist Today

```
Loop A (main):
  Task execution
    → Sonnet reflection (DSPy) → Captain's Log entry
    → Promotion check (seen ≥ 3, age ≥ 7d)
    → Linear ticket (Needs Approval)
    → Human: Approve / Reject / Deepen
    → [STOPS — no apply path exists yet]

Loop B (quality):
  Consolidation quality check (daily)
    → Anomaly detected → Captain's Log entry
    → [joins Loop A at promotion]

Loop C (insights):
  Cross-data pattern analysis
    → Insight detected → Captain's Log proposals
    → [joins Loop A at promotion]

Loop D (operational mode — closed):
  System metrics sampled every 5s
    → Mode FSM evaluates every 30s (60s window)
    → Mode transition → affects tool availability + inference concurrency
    → [fully closed; hardcoded rules, no learning]
```

Loop D is the only closed loop. Loops A–C terminate at the Linear ticket waiting for a human-triggered apply.

---

## 5. Maturity Assessment: Level 1.75

**Solidly between Level 1 and Level 2.**

### Evidence for Level 1 (complete)

- Agent (Sonnet) analyzes its own execution traces and proposes structured changes — this is agent-assisted diagnosis
- The promotion pipeline creates Linear issues autonomously — this is the agent proposing, humans applying
- Six feedback labels (Approved / Rejected / Deepen / Defer / Duplicate / Too Vague) give the human a nuanced gatekeeper role

### Why the Promotion Pipeline Hasn't Fired Yet

The threshold is `seen_count ≥ 3` AND `age ≥ 7 days`. The bulk of the evaluation infrastructure (ADRs 0054–0060) was built in the last two weeks. No proposal has accumulated enough runtime to cross both gates simultaneously. **It will start firing.**

### What Blocks Level 2

1. No apply path — approved ticket exists in Linear but nothing executes it
2. No isolated environment — changes go directly to prod if applied manually
3. No baseline-vs-candidate comparison — no way to know if a change helped

### What Would Constitute Level 2

A candidate change applied in an isolated branch, evaluated against the existing task corpus, compared to baseline, before any promotion to prod. The GitOps model (prod branch stable, dev branch as sandbox, CI as evaluator) is the right infrastructure for this — and it maps directly to the planned A2A delegation architecture.

---

## 6. What the Last Two Weeks Built

Waves 1–3 (2026-04-22 to 2026-04-30) wired the Evaluator layer almost completely:

| ADR | What It Added | RSI Layer |
|-----|--------------|-----------|
| ADR-0054 | Feedback stream bus convention (dual-write, EventBase) | Infrastructure |
| ADR-0055 | System health mode FSM + homeostasis sensors | Evaluator L1 |
| ADR-0056 | Error pattern monitoring + failure-path reflection | Evaluator L2 |
| ADR-0057 | Insights engine: delegation, cost, KG trend analysis | Evaluator L3 |
| ADR-0058 | Self-improvement pipeline stream (Captain's Log bus event) | Evaluator L4 |
| ADR-0059 | Context quality monitoring (compaction incident tracking) | Evaluator L5 |
| ADR-0060 | KG quality stream (staleness decay + tier reranking) | Evaluator L5 |

Nine observability streams now feed the evaluation layer. The feedback data exists. What doesn't exist is the *response* — the Modifier that acts on evaluation output and the Gatekeeper that validates that response before it reaches prod.

---

## 7. The Modifier Architecture: A2A Delegation

The Modifier is not Seshat modifying itself. It is a delegation to a specialized external coding agent (Claude Code, Codex, Qwencode). This is the stronger design:

**Why decoupled is better than self-modifying:**
- Seshat understands *what* needs changing; the coding agent understands *how* to safely implement it
- The core RSI safety risk (agent modifies its own evaluator or promotion gates) is eliminated by design — Seshat never touches its own harness
- The existing `delegation/` module (`DelegationPackage` / `DelegationOutcome`, Stage B) is the hook — it was built for this
- The GitOps model gives the sandbox, CI gives the evaluator, PR review gives the final gate

### The Full A2A RSI Loop (Target State)

```
Seshat (Executor, local SLM)
  → task execution telemetry

Seshat (Evaluator, Sonnet)
  → 9-stream analysis, quality monitors, insights
  → Captain's Log entry → promotion → Linear ticket (Needs Approval)

You (Gatekeeper-1, HITL)
  → review ticket, Approve / Reject / Deepen

Seshat (Modifier-proposer)
  → packages DelegationPackage: what / why / where / success criteria

External Coding Agent (Modifier-implementer)
  → implements on dev branch
  → opens PR with test results

CI (Gatekeeper-2)
  → runs: make test + make mypy + make ruff-check

You (Gatekeeper-3)
  → reviews PR → merges to main → prod deploys

Seshat (Evaluator, post-deploy)
  → outcome measurement: did metrics improve?
  → result feeds back into Captain's Log
```

This architecture reaches **Level 4 RSI** (specialized multi-agent improvement system) without requiring Level 2/3 gated auto-promotion as a prerequisite — because the external coding agent + GitOps provides the sandbox and automated gate that Level 2/3 normally require you to build yourself.

---

## 8. The Sequenced Path

### Step 1 — FRE-226 Phase 2: Skill Doc Self-Update (prompt-level RSI)

- Seshat edits a markdown skill file autonomously based on an approved Captain's Log proposal
- No coding agent needed — Seshat writes a file, commits to dev branch, opens PR
- Lowest-risk RSI type: markdown, reversible by git revert, no behavioral code changes
- Unblocked: ADR-0058 is complete; FRE-226 phase 2 is approved in the wave plan
- Proves the apply-path concept at small scale

### Step 2 — GitOps Infrastructure (Level 2 sandbox)

- `main` branch = prod (frenchforet.com), protected, manual merge only
- `dev` branch = staging environment, auto-deploys on push
- GitHub Actions: `make test` + `make mypy` + `make ruff-check` on every PR
- Rollback = `git revert` + container restart
- This is the Level 2 sandbox — changes tested before they touch prod

### Step 3 — A2A Code Delegation (Level 4 Modifier)

- Seshat packages an approved Linear ticket as a `DelegationPackage`
- External coding agent (Claude Code / Codex) receives delegation, implements on dev branch, opens PR
- CI validates, human reviews, merges to main
- Stage C delegation (programmatic orchestration) in MASTER_PLAN is the architectural hook

### Step 4 — Outcome Measurement (closing the loop)

- Post-deploy: Seshat compares pre/post metrics from the 9 observability streams
- "Did this proposal improve the thing it claimed to improve?"
- Result creates a new Captain's Log entry — feedback into the loop
- This closes the RSI loop and moves the system toward Level 3 gated auto-promotion for low-risk changes

---

## Summary

| Dimension | Status |
|-----------|--------|
| Executor | ✅ Live (local SLM) |
| Evaluator | ✅ Live (Sonnet + 9 streams) |
| Memory | ✅ Live (GPT-4-mini + Neo4j, KG RSI) |
| Modifier — operational | ✅ Live (brainstem mode FSM) |
| Modifier — proposer | ✅ Live (promotion pipeline → Linear) |
| Gatekeeper — human | ✅ Live (HITL via Linear, 6 labels) |
| Modifier — implementer | ❌ Planned (A2A coding agent) |
| Gatekeeper — automated | ❌ Planned (CI + GitOps) |
| Outcome measurement | ❌ Planned (post-deploy loop closure) |
| **Maturity level** | **1.75 — between L1 and L2, architecture designed for L4** |

The Evaluator layer is the strongest part of the current system — built intentionally over the last two weeks with an observability-first approach. The gap is the apply path and the outcome measurement loop. Both are defined and sequenced; neither requires new architecture, only new wiring.
