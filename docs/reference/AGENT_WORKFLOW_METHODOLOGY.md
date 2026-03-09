# Agent Workflow Methodology

**Version**: 1.0
**Date**: 2026-03-09
**Scope**: Project management, documentation, and autonomous agent workflow for Cursor IDE projects

---

## What This Is

A structured methodology for managing software projects where AI coding agents (Cursor)
do the implementation work, with human-in-the-loop (HITL) approval gates at every
decision point. The system combines Linear for task management, Cursor rules for agent
behavior, hooks for verification, and a layered documentation structure that keeps
agents oriented and humans informed.

This document explains what was implemented, why each piece exists, and how to
replicate it in any Cursor project.

---

## 1. Documentation Architecture

### Problem

Documentation grows organically and becomes a flat dump of files with no clear
taxonomy. Agents can't find what they need. Humans can't tell what's current.
Plans, specs, guides, and reference docs all mixed together.

### Solution: Five-category taxonomy

| Category | Directory | Contains | Audience |
|----------|-----------|----------|----------|
| **Reference** | `docs/reference/` | Standards, policies, checklists, conventions | Agents + humans |
| **Guides** | `docs/guides/` | How-to, setup, and integration guides | Humans (agents read when needed) |
| **Specs** | `docs/specs/` | Technical specifications for features | Agents (implementation source) |
| **Plans** | `docs/plans/` | Active plans, tracking, session logs | Agents + humans |
| **Architecture** | `docs/architecture/`, `docs/architecture_decisions/` | Design docs and ADRs | Both |

Additionally: `docs/research/` for exploratory analysis that hasn't become a decision yet.

### Why this structure

- **Agents need fast orientation.** When an agent starts a session, it reads
  `MASTER_PLAN.md` -> Linear issues -> linked spec. Three hops to context. No searching
  through 40+ flat files.
- **Specs are not plans.** A spec says *how* to build something. A plan says *what* to
  build and *when*. Mixing them makes both worse.
- **Guides are for humans.** Setup instructions, configuration walkthroughs -- agents
  rarely need these, and they clutter the agent's search space.
- **Completed work is archived, not deleted.** `docs/plans/completed/` preserves
  history without polluting the active workspace.

### Implementation

1. Create the directories: `reference/`, `guides/`, `specs/`
2. Classify every existing doc and `git mv` it
3. Rewrite `docs/README.md` as an index
4. Update all cross-references in rules and AGENTS.md

**Effort**: ~30 minutes for a project with 40-50 docs.

---

## 2. Master Plan (Living Priority Document)

### Problem

Long roadmap documents (1000+ lines) become write-only. Nobody reads them.
They duplicate information from the task tracker. When priorities change,
the roadmap is the last thing updated.

### Solution: Short, linked, FIFO

`docs/plans/MASTER_PLAN.md` is a <100 line document with four sections:

1. **Current Focus** — what's actively being worked on, linked to Linear issues and specs
2. **Upcoming** — approved work not yet started
3. **Backlog** — ideas needing human approval
4. **Completed** — FIFO list, oldest items eventually drop off

### Why

- **Agents read this first.** It answers "what matters right now?" in 10 seconds.
- **Links, never duplicates.** Details live in Linear issues, specs, and ADRs.
  The master plan is a routing table, not a database.
- **FIFO keeps it fresh.** Completed items eventually cycle off the bottom.
  The document never grows past ~100 lines.

### Sub-plans

Phase-level plans (e.g. `PHASE_2.3_PLAN.md`) contain implementation detail for
a specific body of work. They're referenced from the Master Plan but stand alone.
Each sub-plan links to its Linear project and relevant specs.

---

## 3. Linear Integration with HITL Gates

### Problem

AI agents can create work, implement work, and mark it done -- all without
human oversight. This is dangerous. Agents may implement the wrong thing,
skip quality checks, or create busywork.

### Solution: Status-gated workflow

```
Agent creates issue → Needs Approval → [HUMAN approves] → Approved
Agent implements    → In Progress → Done
Agent/human reviews → In Review → [HUMAN closes] → Done (closed)
```

### Linear statuses used

| Status | Set by | Meaning |
|--------|--------|---------|
| Needs Approval | Agent (on create) | Shaped, awaiting HITL |
| Approved | Human | Go build this |
| In Progress | Agent | Actively implementing |
| Done | Agent | Implementation + tests complete |
| In Review | Agent or human | Awaiting review |

### Labels

| Label | Purpose |
|-------|---------|
| `PersonalAgent` | Scopes issues to this project (required on all issues) |
| `Needs Approval` | Belt-and-suspenders gate alongside status |
| `Review OK` | Review agent verified the work |
| `Review Needs Work` | Review found issues |

### Cursor rules that enforce this

- **`linear-implement-gate.mdc`** (alwaysApply): Every created issue gets
  `state: "Needs Approval"` and `labels: ["Needs Approval", "PersonalAgent"]`.
  Every implementation starts with a status check -- only `Approved` issues
  can be implemented.

### Why HITL matters

Boris Cherny's workflow assumes a human reviews PRs and tags `@.claude` for
corrections. In a solo-developer setup, the HITL gate replaces the team code
review. The human reviews the *intent* (approval gate) and the *result*
(review gate), while the agent handles the *execution*.

### Issue description template

Every Linear issue must contain:

```markdown
## Spec
- `docs/specs/SPEC_NAME.md` (section X.Y)

## ADRs
- ADR-XXXX (relevant decision)

## Files
- `src/...`
- `tests/...`

## Acceptance Criteria
- [ ] Criterion 1
- [ ] Tests pass
- [ ] No new lint errors
```

This gives the implementing agent everything it needs without reading the
entire codebase. The spec link is the plan. The acceptance criteria are
the definition of done.

---

## 4. Agent Roles

### Problem

A single agent session trying to plan, implement, test, debug, and review
exhausts its context window and produces lower-quality work. (Reza Rezvani
measured 60-70% context usage in single-role sessions vs 40% with
role separation.)

### Solution: Three agent roles with clean context boundaries

| Role | Cursor Rule | Responsibility |
|------|-------------|----------------|
| **Implementer** | `session-orientation.mdc`, `linear-implement-gate.mdc` | Read Master Plan -> query Linear for approved issues -> read spec -> implement -> test -> mark Done |
| **Reviewer** | `agent-review.mdc` | Query Linear for Done issues -> validate against spec + acceptance criteria -> label Review OK or Review Needs Work |
| **Planner** | `agent-planning.mdc` | Maintain Master Plan -> create issues from specs -> ensure all issues have spec/ADR links |

### Why separate roles

- **Context protection.** Each agent starts with only the context it needs.
  The implementer doesn't load review history. The reviewer doesn't load
  implementation context.
- **Verification independence.** The reviewer checks the implementer's work
  with fresh eyes (fresh context window). This is Boris Cherny's
  "background verification agent" pattern.
- **Clean responsibility.** The reviewer doesn't fix bugs -- it reports them.
  The planner doesn't implement -- it organizes. No role drift.

### How to run them in Cursor

- **Sequential**: One session, switch roles by invoking the relevant rule
- **Parallel**: Multiple Cursor sessions or Task tool sub-agents, one per role
- **Automated**: Use stop/subagentStop hooks for autonomous handoff

---

## 5. Verification Hooks

### Problem

Agents mark work "done" when they think it's done. Without objective
verification, quality depends on the agent's self-assessment. Boris Cherny
reports 2-3x quality improvement when agents can verify their own work.

### Solution: Cursor hooks for automated verification

Configuration lives in `.cursor/hooks.json`:

```json
{
  "version": 1,
  "hooks": {
    "afterFileEdit": [
      {
        "command": ".cursor/hooks/check-python.sh",
        "matcher": "Write"
      }
    ],
    "stop": [
      {
        "command": ".cursor/hooks/verify-on-stop.sh",
        "loop_limit": 3
      }
    ]
  }
}
```

### Hook: `afterFileEdit` (Python syntax check)

Runs `python3 -m py_compile` on every edited `.py` file. Catches syntax errors
immediately, before they compound into multi-file bugs. Low overhead, high signal.

### Hook: `stop` (test verification on completion)

When the agent finishes, runs `pytest`. If tests fail, returns a `followup_message`
that sends the agent back to fix them. Loops up to 3 times (`loop_limit: 3`).

This is the Cursor equivalent of Claude Code's **Ralph Wiggum** pattern --
an autonomous verification loop. The agent can't declare "done" if tests fail.

### Prerequisites

The test suite must pass at baseline (~100% pass rate) before these hooks
are useful. If 16 tests already fail, the stop hook fires on every completion
and the agent wastes cycles on pre-existing failures.

### Future hooks to consider

| Hook | Purpose |
|------|---------|
| `sessionStart` | Inject project context, set environment |
| `afterFileEdit` (lint) | Run ruff/flake8 on edited files |
| `preCompact` | Log when context window compacts (monitor context health) |
| `sessionEnd` | Log session metrics (duration, tool calls, corrections) |
| `subagentStop` | Loop sub-agents for multi-step autonomous work |

---

## 6. Shared Memory (Cursor Rules as CLAUDE.md)

### Problem

Agents repeat the same mistakes across sessions. Corrections are lost
when the session ends.

### Solution

Boris Cherny uses a `CLAUDE.md` file as collective memory. In Cursor,
this maps to **`.cursor/rules/*.mdc`** files:

| Cherny concept | Cursor equivalent |
|----------------|-------------------|
| `CLAUDE.md` | `.cursor/rules/*.mdc` (always-apply rules) |
| Slash commands | Cursor rules with specific trigger descriptions |
| PR-based learning | Manually updating rules after corrections |
| Team-shared config | Project-level rules committed to git |

### Best practices (from Reza Rezvani's testing)

- **Keep rules terse.** Under ~500 tokens each. Long rules get ignored.
- **Corrections only.** Don't document things the model already knows.
  Only add project-specific patterns and learned mistakes.
- **Mistake-to-rule loop.** Every time you correct the agent, update the
  relevant rule so it doesn't happen again.
- **Activate continual-learning.** The `continual-learning` skill can
  mine past transcripts for recurring corrections and add them to rules
  automatically.

---

## 7. Replicating This in Another Project

### Minimum viable setup (30 minutes)

1. **Create directory structure**: `docs/reference/`, `docs/guides/`, `docs/specs/`,
   `docs/plans/`, `docs/plans/completed/`, `docs/plans/sessions/`
2. **Write `docs/plans/MASTER_PLAN.md`**: Current focus, upcoming, backlog, completed
3. **Copy cursor rules**: `linear-implement-gate.mdc`, `session-orientation.mdc`,
   `file-organization.mdc`, `agent-review.mdc`, `agent-planning.mdc`
4. **Update the project label** in `linear-implement-gate.mdc` and `agent-planning.mdc`
   (e.g. change `PersonalAgent` to `MyOtherProject`)
5. **Create `.cursor/hooks.json`** with afterFileEdit and stop hooks
   (adjust the language-specific check for your stack)
6. **Create Linear labels**: `<ProjectName>`, `Needs Approval`, `Review OK`, `Review Needs Work`

### What to customize per project

| Artifact | What changes |
|----------|-------------|
| `linear-implement-gate.mdc` | Project label name, team name |
| `agent-planning.mdc` | Project label name |
| `hooks/check-*.sh` | Language (Python -> TypeScript, Go, etc.) |
| `MASTER_PLAN.md` | Project-specific phases and priorities |
| `file-organization.mdc` | Source code paths, framework-specific dirs |

### What stays the same across projects

- The HITL approval workflow (Needs Approval -> Approved -> Done -> Review)
- The three agent roles (implementer, reviewer, planner)
- The stop hook verification pattern
- The documentation taxonomy (reference, guides, specs, plans)
- The Master Plan format (short, linked, FIFO)

---

## References

### Boris Cherny (Creator of Claude Code)

- [Original workflow thread](https://x.com/boris_cherny/status/2007179832300581177) (Jan 2, 2026)
- Key principles: parallel sessions, `CLAUDE.md` as collective memory,
  plan mode before implementation, verification as quality multiplier (2-3x),
  Ralph Wiggum autonomous loops, pre-approved permissions
- Production metrics: 259 PRs, 497 commits, ~40K lines in 30 days, 0 human-written lines

### Reza Rezvani (4-day practitioner test)

- Key findings: plan mode is highest-leverage single change (~75-80% time reduction
  on multi-file features), CLAUDE.md should be <500 tokens with corrections only,
  subagents most valuable for context protection (40% vs 70% usage),
  10-20% session abandonment rate is normal
- Correction rate improvement: 1-per-3-interactions -> 1-per-8-10 after CLAUDE.md pruning

### Cursor Hooks Documentation

- [Cursor hooks guide](https://cursor.com/docs/agent/hooks)
- Supports: afterFileEdit, stop (with followup_message and loop_limit),
  sessionStart/End, subagentStart/Stop, preToolUse/postToolUse, and more
- Configuration: `.cursor/hooks.json` (project-level) or `~/.cursor/hooks.json` (global)

### Ralph Wiggum Pattern

- Claude Code plugin for autonomous loops using stop-hook re-injection
- Cursor equivalent: `stop` hook returning `followup_message` with `loop_limit`
- Best for: well-defined tasks with clear completion criteria and test verification
- Not suitable for: architectural decisions, vague requirements, production emergencies
