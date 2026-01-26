# Risk & Tradeoff Register

Tracks architectural, safety, usability, performance, and philosophical risks.

---

## R-001: Over-Constraint → Loss of Usefulness

Graph + safety gates might make the system too rigid.

Impact: Medium
Likelihood: Medium
Mitigation:

- Allow bounded agentic cognition inside graph nodes
- Tune safety primitives pragmatically
- Review usability feedback regularly

Status: Open

---

## R-002: Under-Constraint → Unsafe Behavior

Too much freedom could allow unexpected or harmful behavior.

Impact: High
Likelihood: Medium
Mitigation:

- Safety Gateway primitives mandatory
- Supervisor monitoring
- Human checkpoints for risk class thresholds

Status: Managed

---

## R-003: Complexity Explosion with Multi-Agent Systems

Conversational agents may introduce chaos and debugging difficulty.

Impact: High
Likelihood: Low–Medium
Mitigation:

- Start with Planner+Critic only
- Restrict turns / budget
- Apply only where needed

Status: Open

---

## R-004: Performance / Latency Degeneration

Multiple models + heavy orchestration could slow experience.

Impact: Medium
Likelihood: Medium
Mitigation:

- Prefer small efficient models where possible
- Telemetry-driven evaluation of latency
- Optimize after measurement

Status: Open

---

## R-005: Trust Gap

If introspection or explainability breaks down, trust collapses.

Impact: High
Likelihood: Medium
Mitigation:

- Captains Log mandatory
- Require explainable plans
- Evaluation observes clarity

Status: Managed

---

## R-006: Architecture Drift Over Time

System may evolve chaotically without discipline.

Impact: Medium
Likelihood: Medium
Mitigation:

- Hypothesis-driven changes required
- ADRs for major changes
- Periodic architecture review

Status: Open

---
