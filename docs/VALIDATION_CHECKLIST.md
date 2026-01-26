# Validation Checklist for AI-Generated Architecture Documentation

> **Purpose**: Ensure quality, completeness, and consistency of AI-generated specifications, ADRs, and architectural documents
> **Audience**: AI assistants, project owner during review, future contributors
> **Version**: 1.0
> **Date**: 2025-12-28

---

## Why This Checklist Exists

AI-generated documentation can be:

- **Verbose without substance** (looks good, says little)
- **Inconsistent** (contradicts other docs)
- **Over-confident** (presents guesses as decisions)
- **Under-justified** (missing rationale)
- **Ungrounded** (ignores project constraints)

This checklist ensures every architectural document meets **quality, coherence, and usefulness** standards.

---

## 📋 Universal Quality Checks

Use for **all** architecture documents (specs, ADRs, plans, proposals).

### ✅ Completeness

- [ ] **Title** is clear and specific (not vague like "System Design")
- [ ] **Version** or status is indicated (Proposed/Accepted/Deprecated)
- [ ] **Date** of creation or last update is present
- [ ] **Author/Owner** is specified (project owner, AI assistant, joint)
- [ ] **Purpose** is stated explicitly in introduction
- [ ] **Scope** defines what is and isn't covered

### ✅ Clarity

- [ ] **Technical terms** are defined or linked on first use
- [ ] **Acronyms** are spelled out (e.g., ADR = Architecture Decision Record)
- [ ] **Diagrams** are present where they clarify complex relationships
- [ ] **Examples** are provided for abstract concepts
- [ ] **Code snippets** use realistic, runnable syntax (not pseudocode unless noted)
- [ ] **No ambiguous language** ("might", "could", "probably" require justification)

### ✅ Coherence

- [ ] **Consistent with other docs**: No contradictions with existing specs/ADRs
- [ ] **References linked**: Mentions other docs by filename (e.g., `ADR-0004`)
- [ ] **Terminology aligned**: Uses project vocabulary (e.g., "mode" not "state", "tool" not "plugin")
- [ ] **Philosophy aligned**: Respects core principles (safety-first, determinism, observability)
- [ ] **No inappropriate personal references**: Use "project owner" or "user" in specs/examples; personal names only in authoring metadata

### ✅ Justification

- [ ] **Decisions explained**: Not just "what" but "why"
- [ ] **Alternatives considered**: Lists options explored and rejected
- [ ] **Tradeoffs explicit**: Acknowledges costs of chosen approach
- [ ] **Consequences documented**: Positive and negative impacts stated

### ✅ Actionability

- [ ] **Next steps clear**: Obvious what to implement or decide next
- [ ] **Acceptance criteria** defined: How to know document's intent is satisfied
- [ ] **No orphaned ideas**: Every proposal has an owner or disposition

---

## 📐 Architecture Decision Record (ADR) Checks

ADRs require **especially rigorous validation** since they're binding decisions.

### ✅ ADR-Specific Requirements

- [ ] **Status** is one of: Proposed, Accepted, Deprecated, Superseded
- [ ] **Context** explains the problem or need driving the decision
- [ ] **Decision** states the chosen solution clearly
- [ ] **Alternatives** lists at least 2 other options considered
- [ ] **Consequences** separates positive and negative impacts
- [ ] **References** links to supporting research or prior ADRs
- [ ] **Acceptance criteria** defines when ADR is "done"

### ✅ ADR Red Flags (Reject if Present)

- ❌ **No alternatives**: Only one option presented (suggests bias)
- ❌ **No tradeoffs**: Decision presented as pure win (unrealistic)
- ❌ **Vague decision**: Can't extract a clear "we will do X"
- ❌ **No rationale**: Decision stated without explaining "why"
- ❌ **Contradicts existing ADRs**: Without acknowledging supersession

---

## 🏗️ Component Specification Checks

For detailed specs (e.g., `ORCHESTRATOR_CORE_SPEC_v0.1.md`).

### ✅ Spec-Specific Requirements

- [ ] **Purpose & Responsibilities** clearly defined
- [ ] **External interfaces** documented (APIs, protocols, data formats)
- [ ] **Internal structure** explained (key types, classes, modules)
- [ ] **Data flows** illustrated (sequence diagrams or step-by-step descriptions)
- [ ] **Error handling** strategy specified
- [ ] **Observability** (telemetry, logging) integrated
- [ ] **Governance integration** (mode constraints, permissions) addressed
- [ ] **MVP scope boundary** stated (what's in/out for Phase 1)
- [ ] **Open questions** listed (honest about unknowns)
- [ ] **Implementation plan** sketched (order of work)

### ✅ Spec Red Flags

- ❌ **No interfaces**: Just describes internals without external API
- ❌ **No error handling**: Assumes happy path only
- ❌ **No observability**: Doesn't explain how component is monitored
- ❌ **Framework lock-in**: Hard-codes a library without justification
- ❌ **Scope creep**: Includes Phase 2+ features without marking as future

---

## 📊 Experiment Specification Checks

For hypothesis-driven experiments (`experiments/E-XXX-*.md`).

### ✅ Experiment-Specific Requirements

- [ ] **Hypothesis** stated as testable claim (e.g., "X improves Y by Z%")
- [ ] **Metrics** defined (how success/failure measured)
- [ ] **Baseline** established (current state before experiment)
- [ ] **Procedure** documented (reproducible steps)
- [ ] **Success criteria** quantified (threshold for adoption)
- [ ] **Results** section exists (populated after experiment runs)
- [ ] **Decision** recorded (adopt, reject, refine—with rationale)

### ✅ Experiment Red Flags

- ❌ **Unfalsifiable hypothesis**: Can't prove wrong (not a real hypothesis)
- ❌ **No metrics**: "We'll know it when we see it" (not measurable)
- ❌ **No baseline**: Can't compare before/after
- ❌ **No decision**: Experiment ran but no conclusion documented

---

## 🗺️ Project Plan Checks

For planning documents (`plans/PROJECT_PLAN_v0.1.md`).

### ✅ Plan-Specific Requirements

- [ ] **Goals** clearly stated (what does success look like?)
- [ ] **Milestones** defined (major checkpoints)
- [ ] **Sequencing** explicit (dependency order, not just list)
- [ ] **Velocity metric** defined (how progress measured in AI-assisted context)
- [ ] **Assumptions** documented (e.g., "LM Studio already set up")
- [ ] **Risks** identified (what could block progress?)
- [ ] **Checkpoints** for course correction (when to re-evaluate plan)

### ✅ Plan Red Flags

- ❌ **Waterfall assumption**: No room for iteration or course correction
- ❌ **Time estimates in hours**: Doesn't account for AI-assisted velocity
- ❌ **No dependencies**: Implies all tasks can happen in parallel (unrealistic)
- ❌ **No risk mitigation**: Assumes everything goes smoothly

---

## 🧪 Captain's Log Proposal Checks

For agent-generated improvement proposals (`captains_log/CL-*.md`).

### ✅ Captain's Log Requirements

- [ ] **Entry ID** follows format: `CL-YYYY-MM-DD-NNN`
- [ ] **Type** specified: reflection, config_proposal, hypothesis, observation
- [ ] **Title** clear and actionable
- [ ] **Rationale** explains "why this matters"
- [ ] **Supporting evidence** references telemetry, metrics, or observations
- [ ] **Proposed change** is specific (exact file, section, old/new values)
- [ ] **Status** tracked: awaiting_approval, approved, rejected, implemented
- [ ] **Impact assessment** estimates effect of change

### ✅ Captain's Log Red Flags

- ❌ **Vague proposal**: "Improve performance" (not actionable)
- ❌ **No evidence**: Just intuition, no metrics or observations
- ❌ **Breaking change**: Proposes major refactor without staged rollout
- ❌ **No rollback plan**: Doesn't explain how to undo if it fails

---

## 🔍 Cross-Document Consistency Checks

Validate **relationships between documents**.

### ✅ Consistency Matrix

| Document Type | Must Be Consistent With |
|---------------|-------------------------|
| ADR | Other ADRs, System Architecture, Functional Spec |
| Component Spec | ADRs, System Architecture, Interface definitions |
| Experiment | HYPOTHESIS_LOG, related ADRs |
| Captain's Log | Governance configs, HYPOTHESIS_LOG |
| Project Plan | ROADMAP, Functional Spec milestones |

### ✅ Consistency Validation Steps

1. **Search for conflicts**: Grep for contradictory statements (e.g., "sync" vs "async")
2. **Check version alignment**: Ensure referenced specs are at compatible versions
3. **Validate links**: All cross-references point to existing files
4. **Terminology audit**: Same concepts use same terms across docs

---

## 🎯 Quality Tiers

### Tier 1: Publishable ⭐⭐⭐

- Passes all checks
- No red flags
- External contributor could understand and implement
- No project owner knowledge required

### Tier 2: Internal Use ⭐⭐

- Passes most checks
- Minor gaps acceptable (e.g., missing diagrams for simple concepts)
- Project owner can fill in context

### Tier 3: Draft ⭐

- Fails multiple checks
- Needs revision before use
- Placeholder or work-in-progress

**Aim for Tier 1 for all ADRs and core specs.**

---

## 🛠️ Validation Workflow

### For AI Assistants (Self-Check)

Before presenting a document:

1. **Run this checklist** against your draft
2. **Mark failed items** and fix or acknowledge gaps
3. **Highlight uncertainty** (don't fake confidence)
4. **Request review** for items you can't validate

### For Project Owner (Review)

When reviewing AI-generated docs:

1. **Scan for red flags** (these require immediate fix)
2. **Verify consistency** with existing decisions
3. **Check actionability** (can you implement from this?)
4. **Validate grounding** (does it reflect project reality?)
5. **Approve, revise, or reject** with specific feedback

---

## 📈 Continuous Improvement

This checklist evolves based on:

- **Recurring issues** (add checks for common mistakes)
- **New document types** (extend checklist sections)
- **Project maturity** (raise bar as system stabilizes)

**Update this document when patterns emerge.**

---

## 🎓 Training New AI Assistants

When onboarding a new AI assistant:

1. **Study this checklist** before writing any docs
2. **Review past ADRs** as examples of passing quality
3. **Practice on small docs** first (e.g., session logs)
4. **Request feedback** on checklist application
5. **Internalize principles**, not just items

**Quality documentation is a skill, not a formula.**

---

## ✅ Quick Reference Card

### Before Submitting ANY Document

- [ ] Purpose stated
- [ ] Scope defined
- [ ] Consistent with existing docs
- [ ] Decisions justified
- [ ] Tradeoffs explicit
- [ ] Next steps clear
- [ ] No personal info (use "project owner")
- [ ] Version/date present

### ADRs Must Have

- [ ] Status, Context, Decision, Alternatives, Consequences
- [ ] At least 2 alternatives considered
- [ ] Acceptance criteria

### Specs Must Have

- [ ] Interfaces, Data flows, Error handling, Observability
- [ ] MVP scope boundary

### Experiments Must Have

- [ ] Hypothesis, Metrics, Baseline, Success criteria
- [ ] Results section (populate after run)

### Captain's Log Must Have

- [ ] Entry ID, Type, Rationale, Evidence, Proposed change
- [ ] Impact assessment

---

## 🚫 Common AI-Generated Doc Failures

### 1. "Wall of Text" Syndrome

**Symptom**: Long paragraphs, no structure, hard to scan
**Fix**: Use headings, bullets, tables, diagrams

### 2. "Sounds Smart, Says Little"

**Symptom**: Verbose but vague, no concrete decisions
**Fix**: Force specificity—exact APIs, file paths, commands

### 3. "Fantasy Architecture"

**Symptom**: Ignores project constraints (e.g., suggests tools not in stack)
**Fix**: Ground in reality—reference existing code, ADRs, configs

### 4. "Decision by Omission"

**Symptom**: No alternatives listed, implies "only option"
**Fix**: Demand at least 2 alternatives with tradeoffs

### 5. "Future Handwaving"

**Symptom**: "This will be added later" without tracking
**Fix**: Create explicit future work items or defer with reason

---

## 📝 Document Status Badge

Optionally, add a status badge to document headers:

```markdown
> **Quality Status**: ⭐⭐⭐ Tier 1 (Publishable)
> **Last Validated**: 2025-12-28
> **Checklist Score**: 45/45
```

---

**Use this checklist to maintain documentation quality as the project scales.**

---

## Document History

| Version | Date | Changes |
|---------|------|---------|
| 1.0 | 2025-12-28 | Initial validation checklist created |
