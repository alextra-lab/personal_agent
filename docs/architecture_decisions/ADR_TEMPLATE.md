> **Canonical source:** [`alextra-lab/ai_operations` › `docs/development/adrs/template.md`](https://github.com/alextra-lab/ai_operations/blob/main/docs/development/adrs/template.md). This file is the **Seshat mirror** — keep it in sync with canonical. The Verification / Acceptance-Criteria (No-BS) section was upstreamed via `ai_operations` PR #193.
>
> **Two rules history kept drifting from:** the **References** section is a bulleted list (one ref per line) — never a run-on `**Related:**` paragraph; and **keep the Status line current** — never cite another ADR by a stale status.

# ADR-XXX: [Short Title of Decision]

**Status:** Proposed | Accepted | Deprecated | Superseded
**Date:** YYYY-MM-DD
**Deciders:** [Team/Role/Names]
**Tags:** [category, technology, pattern]

---

## Context

**What is the issue we're addressing?**

Describe the context and problem statement:
- What forces are at play? (technical, political, social, project)
- What is the background?
- What needs to be decided?

---

## Decision

**What did we decide?**

State the decision clearly and concisely:
- The approach chosen
- Key implementation details
- Why this over alternatives

---

## Alternatives Considered

### Option 1: [Alternative Name]
**Description:** ...
**Pros:**
- Benefit 1
- Benefit 2

**Cons:**
- Drawback 1
- Drawback 2

**Why Rejected:** ...

### Option 2: [Alternative Name]
**Description:** ...
**Pros/Cons:** ...
**Why Rejected:** ...

---

## Consequences

### Positive Consequences

**Benefits of this decision:**
- Benefit 1
- Benefit 2
- Benefit 3

### Negative Consequences

**Tradeoffs and costs:**
- Complexity increase in area X
- Performance impact on Y
- Maintenance burden for Z

### Risks and Mitigations

| Risk | Severity | Mitigation |
|------|----------|------------|
| Risk 1 | High/Medium/Low | How we'll address it |
| Risk 2 | High/Medium/Low | How we'll address it |

---

## Implementation Notes

**Key implementation details:**
- Files affected
- Migration steps required
- Dependencies
- Testing strategy

---

## Verification / Acceptance Criteria

**How will we know this decision actually delivered — not just merged?**

Each criterion is a **testable, discriminating, outcome-level invariant** — the observable
result that proves the decision worked — plus *how* it is checked (reuse existing
instrumentation where possible: a query, a probe, a test assertion, a CLI call).

**No-BS bar — every criterion must be able to fail.** Before accepting one, ask: *could a
broken or half-finished implementation still satisfy it?* If yes, rewrite it until only a
working outcome passes. Reject existence-checks standing in for behaviour ("the field
exists" vs "the field holds the *right* value"), "tests pass" where no test asserts the
actual invariant, vanity counts decoupled from the outcome, and any line that merely
restates the task.

- **AC-1** — <observable outcome> · **Check:** <query | probe | test | CLI> · *Fails if* <…>
- **AC-2** — <observable outcome> · **Check:** <…> · *Fails if* <…>

**Seam owner (for a decomposed ADR):** name who asserts the *assembled* intent holds once
all child tickets land — so the ADR does not close just because its last child merged.

---

## References

- Link to related ADRs
- Link to implementation plans
- Link to analysis documents
- External resources

---

## Status Updates

### YYYY-MM-DD - [Status Change]
**Changed By:** [Name]
**Reason:** ...

### YYYY-MM-DD - [Status Change]
**Changed By:** [Name]
**Reason:** ...

---

**Template Version:** 1.1
**Based On:** [Michael Nygard's ADR pattern](https://cognitect.com/blog/2011/11/15/documenting-architecture-decisions)
