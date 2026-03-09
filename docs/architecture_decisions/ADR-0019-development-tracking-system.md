# ADR-0019: Development Tracking and Plan Management System

**Status**: Accepted
**Date**: 2026-02-22
**Deciders**: System Architect
**Related**: All ADRs (tracking their status), `docs/plans/MASTER_PLAN.md`

---

## 1. Context

### The Tracking Problem

The project has strong documentation (16+ ADRs, 23 architecture specs, detailed session logs, phase plans) but lacks a **living, queryable development tracker**. This creates real pain:

| Symptom | Impact |
| ------- | ------ |
| ADR statuses are stale | Many marked "Proposed" despite accepted implementations |
| Phase docs conflict | Completion docs say "testing pending" but session logs show 86% pass rate |
| ADR-0008 numbering collision | Two files share the same number |
| No centralized "where are we" view | Context recovery requires reading 5+ documents |
| Lost between sessions | Each development session starts with re-orientation |
| Research directions unclear | Architectural evolution ideas (multi-agent, Seshat) were discussed but not tracked |

### What We Need

1. **Single source of truth** for project status (what's done, in progress, blocked, planned)
2. **Context recovery** — a developer (human or AI agent) starting a session can orient in <2 minutes
3. **Decision traceability** — link work items to ADRs and phase plans
4. **Low maintenance overhead** — must not become its own project
5. **AI agent accessibility** — Cursor agents must be able to read and update the tracker

---

## 2. Decision

### Adopt **Linear as the primary planning/tracking system**, with markdown docs in-repo as the implementation source of truth and offline fallback

### 2.1 Primary: Linear (issue lifecycle + prioritization)

Linear is the canonical source for:

- Work status (`Needs Approval` -> `Approved` -> `In Progress` -> `Done`)
- Prioritization and sequencing
- Cross-project visibility and stream dashboards
- Assignment, milestones, and blocking relationships

**Properties:**

- Structured and queryable via MCP from Cursor agents
- Provides workflow state transitions and board views that markdown cannot
- Single canonical tracker to prevent split-brain planning

### 2.2 Source docs in repo: Specs/ADRs/Plans (implementation truth)

Markdown documentation remains authoritative for implementation detail and architectural rationale:

- `docs/specs/` -> what to build
- `docs/architecture_decisions/` -> why we build it this way
- `docs/plans/` -> strategic and phase planning context

`docs/plans/DEV_TRACKER.md` is retained as a lightweight fallback and project index when Linear MCP is unavailable.

### 2.3 Maintenance Protocol

| Trigger | Action |
| ------- | ------ |
| Starting a dev session | Query Linear (`list_projects`, `list_issues state:"Approved"`) for current implementable work |
| Completing a work item | Update the issue in Linear (`Done`) and link/refresh implementation evidence in docs as needed |
| Completing a Linear issue | Set issue state to `Done` in Linear |
| New work identified | Create Linear issue in `Needs Approval` with `PersonalAgent` label and spec/ADR links |
| ADR written or status changed | Update ADR status in-repo and ensure relevant Linear issues reference the ADR |
| Documentation inconsistency found | Create/update a Linear issue to track the fix and link affected docs |
| Phase completed | Close associated Linear issues/milestones and update phase summary docs |
| Monthly | Review backlog priorities in Linear; prune stale or superseded issues |

### 2.4 Cursor Agent Integration

The tracking workflow is designed for AI agent consumption:

```text
Session Start Protocol:
1. Query Linear Approved work (`list_issues`) -> "What can be implemented now?"
2. Read the selected issue (`get_issue`) -> "What exactly is requested?"
3. Read linked spec/plan docs -> "What are the implementation details?"
4. Read relevant ADRs -> "What are the architectural constraints?"
5. If Linear MCP is unavailable, use `docs/plans/DEV_TRACKER.md` as fallback
```

This can be encoded as a Cursor rule (`.cursor/rules/`) to ensure agents always orient before working.

---

## 3. Alternatives Considered

### Alternative A: GitHub Issues + Projects

Use GitHub's native project management.

- **Pros**: Free, integrated with repo, API available, MCP servers exist
- **Cons**: Requires network access to query, not readable inline by Cursor agents without API calls, context switching between code and browser, issue templates add overhead
- **Rejected because**: The primary consumer is a Cursor AI agent that needs instant in-repo access. GitHub Issues require API calls and network.

### Alternative B: Notion

Full-featured workspace with databases, views, and collaboration.

- **Pros**: Flexible, visual, relational databases, API available
- **Cons**: SaaS dependency, not in-repo, requires network, data sovereignty concern (conflicts with project's local-sovereignty principle), MCP integration exists but less mature than Linear
- **Rejected because**: Violates local sovereignty; data lives on Notion's servers. Also adds a heavyweight tool for a single-developer project.

### Alternative C: Markdown Only (No Linear)

Use markdown files (`DEV_TRACKER.md`) as the only tracker.

- **Pros**: Fully local, version-controlled, zero network dependency
- **Cons**: Poor filtering/sorting, no true workflow states, no native board/timeline views, higher manual overhead
- **Rejected because**: It does not scale for active planning and creates drift risk versus issue workflow.

### Alternative D: Dedicated Open-Source Tool (Plane, Taiga, Focalboard)

Self-host an open-source project management tool.

- **Pros**: Full control, local sovereignty, feature-rich
- **Cons**: Operational overhead (another service to maintain), Docker resource usage, API integration needed, no native Cursor/MCP support
- **Rejected because**: Running and maintaining a project management server for a single-developer project is disproportionate overhead. The project already runs PostgreSQL, Elasticsearch, Neo4j, and SLM Server.

---

## 4. Consequences

### Positive

- **Single planning truth**: All execution status lives in one system (Linear)
- **Agent operability**: Cursor agents can query implementable approved work directly
- **Workflow rigor**: Approval gates and explicit state transitions reduce accidental execution
- **Docs remain authoritative**: Specs/ADRs stay in-repo and version controlled

### Negative

- **MCP dependency**: Issue operations depend on Linear availability and network access
- **Dual-surface discipline needed**: Status in Linear, implementation detail in docs; both must stay linked
- **Tooling coupling**: Session workflow now depends on Linear MCP capability

### Risks

- Issue metadata can drift from docs (mitigate: require spec/ADR links in issue descriptions)
- Linear outages can block normal workflow (mitigate: documented `DEV_TRACKER.md` fallback path)

---

## 5. Acceptance Criteria

- [ ] `docs/plans/DEV_TRACKER.md` created with current project status
- [ ] All active work items catalogued with IDs, phases, and priorities
- [ ] Documentation debt items identified and listed
- [ ] Decision log reflects all existing ADRs
- [ ] Cursor rule created for session-start orientation protocol
- [ ] At least one session uses the tracker for context recovery (validated useful)

---

## 6. Implementation Notes

### Immediate Actions

1. Keep Linear as the canonical planning surface for all new and active work
2. Keep spec/ADR links in every implementation issue
3. Maintain session-orientation rules for Linear-first workflow

### Current State: Linear Integration Implemented

1. Linear MCP configured and in active use
2. Work streams/projects created and synced
3. Issues linked to specs/ADRs
4. `DEV_TRACKER.md` retained as fallback/index rather than primary tracker

### Estimated Effort

- Initial tracker setup: completed
- Linear integration and migration: completed
- Ongoing maintenance: continuous issue/document hygiene
