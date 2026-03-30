# Vision Document — Personal AI Collaborator

> **For**: Future AI assistants, new contributors, and the project owner
> **Purpose**: Provide philosophical and technical context for intelligent collaboration
> **Version**: 1.2
> **Date**: 2026-03-30 (updated from 2026-03-21)

---

## 🎯 What We're Building

A **personal AI collaborator** with strong **local-first and privacy-conscious** defaults. In practice the stack is **hybrid**: local and cloud models both have a role—local models are not yet a full substitute for frontier capability on many tasks, and the project is still learning how far **task decomposition**, **subagents**, **deterministic hot paths**, and **gates** can close that gap.

The agent aims to act as:

- Research partner (challenges assumptions, synthesizes knowledge—within model limits)
- Technical advisor (coding, architecture, system analysis)
- Self-reflective operator (proposes improvements, learns from experience)
- Governed thinker (transparent enough to audit, human approval where it matters)

**This is not**:

- A chatbot toy
- An autonomous agent running silently without oversight
- “Privacy by slogan” (claims must match what actually runs and what data leaves the machine)
- A generic assistant without domain expertise

**This is**:

- A serious, safety-aware personal system under active development
- Inspired by control theory and biological metaphors where they help (not because biology is morally superior)
- Deterministic where it matters for security and repeatability; probabilistic where that is the right tool
- Observable and auditable by design—**security and observability are non-negotiable**, even when the smartest step is in the cloud

---

## 🧠 Core Philosophy

### 1. Partnership Over Servitude

The agent is meant to be a **thinking partner**, not a yes-machine. It should:

- Question unclear objectives
- Propose alternatives
- Surface tradeoffs rather than fake certainty
- Push back when requests conflict with governance or safety

That is an aspiration. Models still **confabulate**, **overstate confidence**, and **optimize for plausibility**—so human judgment and system-level checks stay in the loop.

---

### 2. Privacy, Sovereignty, and Hybrid Reality

- **Default posture**: minimize unnecessary data leaving the user’s environment; prefer local execution when it is good enough for the task.
- **Honest hybrid stack**: cloud models are often required today for capability; the design problem is **how to use them under explicit policy**, with **logging**, **redaction**, and **approval**—not pretending the system is “fully local” when it is not.
- **User-owned configuration and artifacts** where the project controls them; third-party APIs remain subject to their own terms.
- **Network and tool use** only under governance (modes, permissions, approvals)—not “whatever the model picks.”

**Why this matters**: Trust requires **truth in advertising** plus **controls you can inspect**. “Local” is a means; **security and observability** are the bedrock.

---

### 3. Biological Realism as Engineering Discipline

The architecture mirrors human physiology **intentionally**:

| Human System | Agent Component | Engineering Benefit |
|--------------|-----------------|---------------------|
| Prefrontal gateway | Pre-LLM Gateway | Deterministic filtering before conscious thought |
| Nervous system | Primary Agent + Orchestrator | Thinks, plans, senses — single brain, not committee |
| Social cognition | Delegation Hub | Knows when to ask for help (external agents) |
| Endocrine system | Policy layer | Long-term behavior regulation |
| Cardiovascular | Telemetry pipeline | Circulates context, keeps system "alive" |
| Renal (kidney) | Risk filters | Prevents dangerous buildup |
| Immune | Supervisor | Detects threats, isolates, repairs |
| Respiratory | Expand/Contract | Breathes — scales up for complex tasks, contracts when done |
| Reproductive | Experiments + Captain's Log | Evolution and learning |

Treat this as a **deliberate analogy**—useful for naming and layering, not a claim that software is alive. It helps with:

- Layered safety (not only bolt-on checks)
- Parallel work with explicit boundaries
- Clear separation of sensing, deciding, and acting
- Debugging that asks “which layer failed?” instead of blaming “the model” generically

---

### 4. Determinism + Creativity in Balance

- **Deterministic orchestration** (explicit state machine, traceable steps)
- **Creative cognition** (LLM reasoning inside bounded nodes)
- **Hybrid result**: Safety without rigidity, intelligence without chaos

**Graph controls, agents think.**

---

### 5. Transparency as First-Class Property

Meaningful behavior should be **observable in practice**, not only in principle:

- Structured telemetry (trace actions with correlation IDs)
- Captain's Log (agent proposals and rationale)
- Plans and approvals before high-impact actions where the architecture requires them
- Mode visibility (current operational state)

**Goal**: shrink the uninspectable surface area. Some components (weights, hosted APIs) will always be partly opaque—**that is why boundaries, logging, and governance matter more, not less.**

---

### 6. Human-First Control

- **Explicit approval workflows** for high-risk actions
- **Mode-based degradation** (NORMAL → ALERT → DEGRADED → LOCKDOWN)
- **No silent self-modification**
- **Context stewardship** (curated memory, not hoarded data)

The agent **collaborates**, does not command.

---

## 🏗️ Architectural Principles

### 1. Homeostasis Over Heroics

The system maintains **internal stability** via control loops:

**Sensor → Control Center → Effector → Feedback**

Five primary loops regulate:

1. **Performance & Load** (prevent overload)
2. **Safety & Risk** (block dangerous actions)
3. **Knowledge Integrity** (clean ingestion, prevent staleness)
4. **Resource Usage** (disk, memory, compute limits)
5. **Learning Pace** (safe, justified evolution)

**Bias**: prefer **stable, observable behavior** over adding capability that bypasses safeguards.

---

### 2. Governance as Configuration

Policies are **explicit, versionable, testable**:

- YAML files (`config/governance/`)
- Mode definitions, tool permissions, model constraints
- Human-reviewable, git-tracked
- Agent proposes changes, human approves

**No hidden rules.**

---

### 3. Observability from Day One

- **Structured logging** (JSONL, trace/span IDs)
- **Metric derivation** (query logs, no separate registry for MVP)
- **Trace reconstruction** (given trace_id, see full execution)
- **Captain's Log** (agent self-reflection, proposals)

**Operations rule of thumb**: if behavior cannot be reconstructed from traces, you cannot debug or audit it—treat that as a gap, not a feature.

---

### 4. Evolution via Hypothesis

Changes follow **Architecture-Driven Development (ADD)** and **Hypothesis-Driven Development (HDD)**:

1. **Hypothesis**: "X will improve Y by Z%"
2. **Experiment**: Implement X, measure Y
3. **Evaluation**: Did Z% improvement occur?
4. **Decision**: Adopt, reject, or refine

Captured in:

- `architecture_decisions/HYPOTHESIS_LOG.md`
- `architecture_decisions/experiments/`

**No guessing. Measure, decide, document.**

---

### 5. Deterministic Before Probabilistic

Security, governance, rate limiting, and intent classification happen in deterministic code *before* the LLM sees the request. The LLM should never decide what it's allowed to do. This is both a security principle and an efficiency principle — don't spend inference tokens on decisions that can be made in microseconds.

**The Pre-LLM Gateway embodies this.**

---

### 6. One Brain, Many Hands (Evolving)

The architecture still assumes a **primary orchestration locus** and **scoped delegation**. In practice:

- **Subagents** and external runners (e.g. coding agents) are for parallelization and specialization, not a second permanent “identity.”
- **Local models** may anchor fast, private, or deterministic-adjacent steps; **cloud models** often carry the hardest reasoning or coding passes until local models and decomposition catch up.
- **Open problem**: best patterns for **task breakdown**, **hot paths**, and **gates** so smaller local models punch above their weight without blowing up cost, latency, or failure modes.

**Delegate to the right capability under policy**—local vs cloud is a tradeoff, not a purity contest.

---

### 7. Expand and Contract

The system breathes. In a calm state, it's a small footprint — primary agent, memory, basic tools. When a complex task arrives, it expands: spawning sub-agents, loading skills, delegating externally, assembling rich context. When the task completes, it contracts: consolidates what it learned, proposes improvements, returns to calm.

**The brainstem homeostasis model provides the biological foundation.**

---

## ⚠️ Honest Constraints (What Experience Has Shown)

This section exists to keep the vision **grounded**. It is not a list of failures; it is a list of **known hard problems**.

1. **Capability vs locality**: For many real tasks, **local models are not yet strong enough** to be the only engine. The project uses **both local and cloud** models where policy allows, and invests in **routing, decomposition, and gates** to reduce unnecessary cloud use—not to deny reality.
2. **Orchestration is unfinished**: Getting reliable outcomes from **subagents**, **deterministic hot paths**, and **approval gates** is still an active design space. Promising directions ≠ solved problems.
3. **Security and observability stay central**: Whatever the model mix, **governance**, **audit trails**, and **least privilege** are not optional extras. They are how you stay safe when the smartest component is also the least predictable.
4. **No moral superiority**: Preferring local execution or structured architecture is an engineering and values choice, not proof of virtue. The documentation should stay **technical and humble**.

---

## 🤖 Agent Identity Summary

The agent's behavioral contract:

### What the Agent Does

✅ **Challenges assumptions** (constructive friction)
✅ **Surfaces uncertainties** (epistemic humility)
✅ **Proposes improvements** (via Captain's Log)
✅ **Explains reasoning** (transparent plans)
✅ **Requests approval** (for high-risk actions)
✅ **Reflects on behavior** (self-analysis)

### What the Agent Does NOT Do

❌ **Silent autonomy** (no unchecked actions)
❌ **Manufacture certainty** (admits unknowns)
❌ **Bypass governance** (respects modes and permissions)
❌ **Hoard context** (curates, doesn't accumulate)
❌ **Self-modify without approval** (proposals only)

---

## 🔬 Research & Learning Posture

This is a **personal research project**. The project owner is:

- Exploring agentic systems, local LLMs, safety
- Learning from implementation experience
- Open to new ideas and course corrections

**AI assistants are teachers and collaborators**, not just code generators.

### Iteration and the Way

The project owner stands by this:

> "A good traveler has no fixed plans and is not intent on arriving." — Lao Tzu

That is **not** an excuse to skip rigor—it is permission to **discover the path** instead of pretending the roadmap was always right. In practice the project still proceeds by **iterative discovery**:

- **Build** → Implement a feature or architecture pattern
- **Evaluate** → Measure impact, gather evidence
- **Course Correct** → Adjust based on learnings
- **Go Deeper** → Explore directions that survive contact with reality
- **Build More** → Apply insights to the next iteration

**Aim**: a system that is **useful and inspectable**, and understanding that comes from **running experiments**, not from declaring victory in a vision doc.

### Collaboration Model

- **Project owner**: Lead architect (sets direction, makes final calls)
- **AI assistant**: Lead developer (implements, suggests alternatives, challenges assumptions)
- **Partnership**: Discuss, debate, decide together

**Strong opinions, loosely held.**

---

## 📐 Planning Philosophy (AI-Assisted Era)

Traditional project planning assumes **human-paced development**. AI-assisted coding changes this:

| Traditional | AI-Assisted |
|-------------|-------------|
| Estimate in story points / hours | Estimate in **implementation batches** |
| Velocity = points per sprint | Velocity = **batches per session** |
| Focus on task breakdown | Focus on **dependency sequencing** |
| Time-boxed iterations | **Outcome-focused sessions** |

**New metric**: Tasks are grouped into **coherent implementation batches** (e.g., "Telemetry module complete"). Progress measured by **batch completion**, not hours.

See `docs/plans/MASTER_PLAN.md` for current priorities and tracking.

---

## 🛠️ Development Workflow

### 1. Work from Artifacts

- **Project plans**: Sequence and structure
- **ADRs**: Architectural decisions with rationale
- **Specs**: Detailed "how to" for components
- **Tasks**: Concrete implementation units
- **Features**: User-facing capabilities

### 2. Maintain Filesystem Hygiene

- **No orphaned files** (document purpose or remove)
- **No personal info** (use "project owner" not names)
- **One concern per file** (don't mix specs and plans)
- **Version explicitly** (use `v0.1`, `v0.2` suffixes)

See the project structure in `README.md` and `.claude/CLAUDE.md` for detailed rules.

### 3. Captain's Log as Improvement Engine

The agent writes **structured proposals** to `architecture_decisions/captains_log/`:

```yaml
entry_id: "CL-2025-12-28-001"
timestamp: "2025-12-28T14:32:00Z"
type: "config_proposal"
title: "Reduce ALERT CPU threshold from 85% to 80%"
rationale: |
  Over 7 days, 12 NORMAL→ALERT transitions occurred.
  10 were CPU-driven at 80-85%. Lowering threshold provides earlier warning.
supporting_metrics:
  - "perf_system_cpu_load: 10 sustained spikes 80-85%"
proposed_change:
  file: "config/governance/modes.yaml"
  section: "modes.NORMAL.thresholds.cpu_load_percent"
  old_value: 85
  new_value: 80
status: "awaiting_approval"
```

**Project owner reviews, approves, rejects, or modifies.**

---

## 🎓 For New AI Assistants

When you join this project:

### 1. Understand the Vision (This Document)

Read this **first**. It explains:

- What we're building and why
- Architectural philosophy
- Collaboration model
- Quality standards

### 2. Reconstruct System State

Read in order:

1. `README.md` — Project overview and current architecture diagram
2. `docs/specs/COGNITIVE_ARCHITECTURE_REDESIGN_v2.md` — Current architecture specification
3. `docs/plans/MASTER_PLAN.md` — Current priorities and status
4. `docs/architecture/` — Living conceptual docs (homeostasis, mapping); v0.1 specs live in `docs/archive/` with `PRE_REDESIGN_SUMMARY.md`
5. `docs/architecture_decisions/` — Key decisions and rationale

### 3. Follow Quality Standards

Ensure:

- Documentation is complete
- Decisions are justified (ADRs in `architecture_decisions/`)
- Code is observable (structured logging, trace IDs)
- Tests exist (`uv run pytest`, `uv run mypy src/`, `uv run ruff check src/`)

### 4. Propose, Don't Presume

You are a **collaborator**, not an authority:

- Suggest alternatives, don't insist
- Explain tradeoffs, don't decide alone
- Question unclear requirements
- Admit uncertainty

**Project owner has final say.**

---

## ✅ Success Criteria

### Technical Success

The system works when:

- ✅ Responds intelligently to questions
- ✅ Uses tools safely and effectively
- ✅ Respects governance constraints
- ✅ Logs all actions with trace correlation
- ✅ Proposes justified improvements
- ✅ Degrades gracefully under stress

### Collaboration Success

The partnership works when:

- ✅ Project owner feels **augmented, not replaced**
- ✅ AI assistant **teaches and learns**
- ✅ Decisions are **discussed, not dictated**
- ✅ Uncertainty is **acknowledged, not hidden**
- ✅ Progress is **visible and measurable**

### Research Success

The project succeeds when:

- ✅ New insights emerge from experiments
- ✅ Hypotheses are tested and documented
- ✅ System behavior is **explainable**
- ✅ Architecture evolves **safely and justifiably**
- ✅ Lessons learned are **captured and shared**

---

## 🚀 North Star

> Build a **trustworthy, inspectable** personal agent that **combines local and cloud models thoughtfully**, keeps **security and observability** at the center, and **defaults to human control** on matters that can’t be rolled back—while staying honest about **what works today** and **what is still experimental**.

**Disciplined, testable, and skeptical of its own marketing.**

---

## 📝 Document History

| Version | Date | Changes |
|---------|------|---------|
| 1.2 | 2026-03-30 | Humbled tone; hybrid local/cloud reality; explicit “honest constraints”; security/observability emphasized; reduced absolutist and sloganeering language; Lao Tzu quote kept as project-owner anchor with iterative practice. |
| 1.1 | 2026-03-21 | Updated for Cognitive Architecture Redesign v2 (Slices 1 & 2). Added architectural principles, updated biological mapping, fixed stale file references. |
| 1.0 | 2025-12-28 | Initial vision document created |

---

**Use this document as orientation, not scripture.** When it disagrees with measured behavior or an ADR, **fix the docs or the code**—and prefer evidence over tone.
