# PR Review Rubric for Architectural Changes

> **Purpose**: Standardized evaluation framework for reviewing architectural changes (ADRs, specs, governance config changes)
> **Audience**: Project owner, future contributors, AI assistants preparing proposals
> **Version**: 1.0
> **Date**: 2025-12-28

---

## Why a PR Review Rubric?

### The Problem

Without a rubric, architectural reviews are:

- **Subjective**: "I don't like this" without specific criteria
- **Inconsistent**: Different standards applied to similar changes
- **Incomplete**: Miss critical aspects (security, observability, etc.)
- **Inefficient**: Repeated back-and-forth on same issues

### The Solution

A **structured rubric** provides:

- ✅ **Clear criteria**: Explicit standards for acceptance
- ✅ **Consistency**: Same lens applied to all changes
- ✅ **Completeness**: Ensures all concerns addressed
- ✅ **Efficiency**: AI assistants can self-check before submission
- ✅ **Learning**: Junior contributors understand expectations

### When to Use

Apply this rubric to:

- **ADRs** (Architecture Decision Records)
- **Component specifications** (new or major revisions)
- **Governance config changes** (mode thresholds, tool permissions)
- **Captain's Log proposals** (agent-generated improvements)
- **Experimental changes** (hypothesis-driven modifications)

**Not for**: Minor doc fixes, session logs, routine bug fixes

---

## 🎯 Rubric Structure

Each review category has:

- **Questions** to guide evaluation
- **Scoring** (Pass/Fail or 0-5 scale)
- **Severity** (Blocking, High Priority, Nice-to-Have)

---

## 1️⃣ Problem Clarity & Motivation

### Questions

- [ ] **Is the problem/need clearly stated?** (e.g., "Orchestrator needs error handling strategy")
- [ ] **Is the impact explained?** (e.g., "Without this, crashes cause data loss")
- [ ] **Are specific examples provided?** (not just abstract descriptions)
- [ ] **Is urgency/priority justified?** (why now vs later?)

### Scoring

- **Pass**: Problem statement is clear, impactful, concrete
- **Fail**: Vague, unclear why this matters, or no real problem identified

**Severity**: 🚨 **Blocking** (can't evaluate solution without clear problem)

---

## 2️⃣ Solution Quality

### Questions

- [ ] **Does the solution address the stated problem?** (not a tangent)
- [ ] **Is it technically sound?** (no obvious flaws)
- [ ] **Is it implementable within project constraints?** (available models, tools, time)
- [ ] **Does it align with architectural principles?** (safety-first, observability, determinism, etc.)
- [ ] **Are interfaces and contracts well-defined?** (clear APIs, data formats)

### Scoring (0-5 scale)

- **5**: Excellent—elegant, simple, comprehensive
- **4**: Good—sound with minor refinements needed
- **3**: Acceptable—works but not optimal
- **2**: Weak—significant concerns
- **1**: Poor—major flaws
- **0**: Unacceptable—fundamentally broken

**Severity**: 🚨 **Blocking** if score < 3

---

## 3️⃣ Alternatives Considered

### Questions

- [ ] **Are at least 2 alternatives listed?** (beyond chosen solution)
- [ ] **Are alternatives realistic?** (not strawmen)
- [ ] **Are tradeoffs explicit?** (pros/cons of each option)
- [ ] **Is the choice justified?** (why this option over others?)

### Scoring

- **Pass**: Multiple real alternatives, clear tradeoffs, justified choice
- **Fail**: No alternatives OR only trivial/strawman options

**Severity**: 🔴 **High Priority** for ADRs (blocking), ⚠️ Medium for specs

---

## 4️⃣ Consequences & Impacts

### Questions

- [ ] **Are positive consequences listed?** (what improves)
- [ ] **Are negative consequences acknowledged?** (costs, risks, complexity)
- [ ] **Are downstream impacts identified?** (what else changes as a result)
- [ ] **Is migration cost assessed?** (if changing existing system)
- [ ] **Are rollback/exit strategies discussed?** (if experiment fails)

### Scoring

- **Pass**: Balanced view of positive/negative, downstream impacts considered
- **Fail**: Only upsides listed OR critical impacts missed

**Severity**: 🔴 **High Priority** (can accept with conditions, but must address)

---

## 5️⃣ Observability & Debuggability

### Questions

- [ ] **How will this be monitored?** (metrics, logs, traces)
- [ ] **How will failures be detected?** (alerts, error signals)
- [ ] **How will issues be debugged?** (diagnostic tools, log queries)
- [ ] **Are telemetry events defined?** (what gets logged when)

### Scoring

- **Pass**: Clear observability story, integrated with existing telemetry
- **Fail**: No mention of monitoring OR tacked-on afterthought

**Severity**: 🚨 **Blocking** for system components, ⚠️ Medium for experiments

---

## 6️⃣ Security & Safety

### Questions

- [ ] **Are security implications assessed?** (data exposure, privilege escalation, etc.)
- [ ] **Are governance constraints integrated?** (mode checks, permissions, approvals)
- [ ] **Are error/failure modes handled safely?** (fail-safe vs fail-open)
- [ ] **Are secrets/PII protected?** (no hard-coded secrets, redaction in logs)

### Scoring

- **Pass**: Security reviewed, governance integrated, safe failure modes
- **Fail**: Security not considered OR unsafe behavior possible

**Severity**: 🚨 **Blocking** if any safety risk identified

---

## 7️⃣ Testing Strategy

### Questions

- [ ] **How will this be tested?** (unit, integration, manual)
- [ ] **Are test cases listed?** (happy path + failure scenarios)
- [ ] **Is validation automated or manual?** (prefer automated)
- [ ] **Are acceptance criteria defined?** (how to know it works)

### Scoring

- **Pass**: Clear test strategy, acceptance criteria defined
- **Fail**: No test plan OR "we'll test it later"

**Severity**: 🔴 **High Priority** for core components, ⚠️ Medium for experiments

---

## 8️⃣ Documentation & Communication

### Questions

- [ ] **Is the change documented?** (ADR, spec update, or Captain's Log)
- [ ] **Are related docs updated?** (no orphaned references)
- [ ] **Is terminology consistent?** (uses project vocabulary)
- [ ] **Are diagrams provided?** (if complex relationships)
- [ ] **Is the writing clear?** (not overly verbose or jargon-heavy)

### Scoring

- **Pass**: Well-documented, clear, consistent
- **Fail**: Poor documentation OR inconsistent with existing docs

**Severity**: ⚠️ **Medium** (can fix before merge, but must address)

---

## 9️⃣ Scope & Complexity

### Questions

- [ ] **Is the scope reasonable?** (not too ambitious for one change)
- [ ] **Can it be broken into smaller pieces?** (prefer incremental)
- [ ] **Is the complexity justified?** (not over-engineered)
- [ ] **Are future extensions considered?** (room to grow)

### Scoring

- **Pass**: Right-sized scope, manageable complexity
- **Fail**: Scope creep OR unnecessarily complex

**Severity**: 🔴 **High Priority** (can request scope reduction)

---

## 🔟 Consistency with Project Principles

### Questions

- [ ] **Aligns with biological metaphor?** (if applicable)
- [ ] **Maintains homeostasis focus?** (control loops, stability)
- [ ] **Preserves determinism where needed?** (orchestrator, governance)
- [ ] **Supports transparency?** (observable behavior)
- [ ] **Respects human-first control?** (no silent autonomy)
- [ ] **Local-first?** (no unnecessary cloud dependencies)

### Scoring

- **Pass**: Consistent with project philosophy
- **Fail**: Violates core principles

**Severity**: 🚨 **Blocking** if principle violation is fundamental

---

## 📊 Overall Rubric Scorecard

### Scoring Summary

| Category | Weight | Score | Weighted Score |
|----------|--------|-------|----------------|
| 1. Problem Clarity | 10% | Pass/Fail | — |
| 2. Solution Quality | 25% | 0-5 | × 0.25 |
| 3. Alternatives | 10% | Pass/Fail | — |
| 4. Consequences | 10% | Pass/Fail | — |
| 5. Observability | 15% | Pass/Fail | — |
| 6. Security & Safety | 15% | Pass/Fail | — |
| 7. Testing Strategy | 10% | Pass/Fail | — |
| 8. Documentation | 5% | Pass/Fail | — |
| 9. Scope & Complexity | — | Qualitative | — |
| 10. Consistency | — | Pass/Fail | Blocking if Fail |

### Decision Thresholds

- **Auto-Approve**: All blocking items Pass, Solution Quality ≥ 4
- **Approve with Conditions**: 1-2 High Priority items need fixes
- **Request Revisions**: Multiple High Priority failures OR Solution Quality < 3
- **Reject**: Any Blocking item fails OR fundamental principle violation

---

## 🛠️ How to Use This Rubric

### For AI Assistants (Pre-Submission)

Before proposing an architectural change:

1. **Self-score using this rubric**
2. **Fix obvious gaps** (missing alternatives, no observability, etc.)
3. **Highlight uncertainties** (can't validate security? Say so)
4. **Include rubric score in proposal** (e.g., "Self-assessed: 4/5 Solution Quality")

### For Project Owner (Review)

When reviewing a proposal:

1. **Skim for blocking issues first** (security, principle violations)
2. **Score each category systematically**
3. **Document decision with rubric scores** (not just "looks good")
4. **Provide specific feedback** (reference rubric categories)
5. **Request revisions or approve** with clear rationale

---

## 📈 Benefits of This Approach

### 1. Faster Reviews

- **AI assistants catch issues early** (self-check before submission)
- **Fewer review cycles** (comprehensive first pass)
- **Clear criteria** reduce debate

### 2. Higher Quality

- **Nothing slips through** (checklist ensures completeness)
- **Consistent standards** across all changes
- **Learning reinforcement** (AI assistants improve over time)

### 3. Better Communication

- **Specific feedback** ("Observability section missing" vs "needs work")
- **Shared vocabulary** (rubric categories)
- **Justified decisions** (scores, not opinions)

### 4. Risk Reduction

- **Security always checked** (not an afterthought)
- **Safety implications explicit** (governance integration verified)
- **Testing non-negotiable** (no "we'll test later")

---

## 🎓 Example: Applying the Rubric

### Scenario: ADR for Async Orchestrator

**Proposal**: Switch orchestrator from sync to async execution

#### Rubric Evaluation

| Category | Score | Notes |
|----------|-------|-------|
| Problem Clarity | ✅ Pass | Clear: "Need concurrent tool execution for performance" |
| Solution Quality | 4/5 | Sound, but migration cost high (see below) |
| Alternatives | ✅ Pass | Sync-first, threadpool, async—compared explicitly |
| Consequences | ⚠️ Needs Work | Positive clear, but migration testing not detailed |
| Observability | ✅ Pass | Async spans defined, no change to telemetry model |
| Security & Safety | ✅ Pass | Governance hooks preserved, no new risks |
| Testing Strategy | ⚠️ Needs Work | Unit tests listed, but integration test plan vague |
| Documentation | ✅ Pass | ADR complete, orchestrator spec updated |
| Scope & Complexity | 🔴 Flag | Large scope—could break into phases? |
| Consistency | ✅ Pass | Aligns with principles |

#### Decision: **Approve with Conditions**

**Conditions**:

1. Add detailed migration testing plan (Category 7)
2. Address consequences—rollback strategy if async causes issues (Category 4)
3. Consider phased rollout—async tools first, full orchestrator later (Category 9)

**Rationale**: Solution is sound (4/5), but execution risk requires more detail on testing and phasing.

---

## 🔄 Rubric Evolution

This rubric will evolve based on:

- **Recurring review issues** (add category if pattern emerges)
- **Project maturity** (raise bar as system stabilizes)
- **New risks** (add security checklist items as threats identified)

**Review rubric quarterly** or after major project milestones.

---

## 📝 Quick Reference: Blocking vs Non-Blocking

### 🚨 Blocking Issues (Must Fix Before Approval)

- Problem unclear or non-existent
- Solution technically unsound (score < 3)
- Security risk identified
- No observability plan (for system components)
- Violates core principles

### 🔴 High Priority (Fix Before Merge, Can Conditionally Approve)

- No alternatives listed (ADRs)
- Consequences incomplete
- Testing strategy vague
- Scope too large

### ⚠️ Medium Priority (Can Fix Post-Approval)

- Documentation needs polish
- Minor consistency issues
- Nice-to-have diagrams missing

---

## ✅ Benefits Summary

| Stakeholder | Benefit |
|-------------|---------|
| **Project Owner** | Faster, higher-quality reviews; clear acceptance criteria |
| **AI Assistants** | Self-improvement loop; learn what "good" looks like |
| **Future Contributors** | Transparent expectations; consistent standards |
| **Project** | Risk reduction; better architecture; maintainable docs |

---

**This rubric ensures every architectural change is evaluated rigorously, consistently, and efficiently.**

---

## Document History

| Version | Date | Changes |
|---------|------|---------|
| 1.0 | 2025-12-28 | Initial PR review rubric created |
