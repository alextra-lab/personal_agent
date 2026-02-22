# ADR-0019: Development Tracking and Plan Management System

**Status**: Proposed
**Date**: 2026-02-22
**Deciders**: System Architect
**Related**: All ADRs (tracking their status), IMPLEMENTATION_ROADMAP.md

---

## 1. Context

### The Tracking Problem

The project has strong documentation (16+ ADRs, 23 architecture specs, detailed session logs, phase plans) but lacks a **living, queryable development tracker**. This creates real pain:

| Symptom | Impact |
|---------|--------|
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

### Adopt a **markdown-based dev tracker** in the repo as the primary tracking system, with optional **Linear** integration for visual project management.

### 2.1 Primary: Markdown Dev Tracker (`docs/plans/DEV_TRACKER.md`)

A single markdown file that serves as the project's status dashboard:

**Structure:**
- Quick Status (phase table)
- Active Work Items (in progress + ready to start)
- Backlog (prioritized future work)
- Completed Milestones (archive)
- Documentation Debt (known inconsistencies)
- Decision Log (quick-reference ADR history)

**Properties:**
- Lives in the repo → always available to Cursor agents
- Version-controlled → history of project evolution
- Human-readable → no tooling required to access
- Markdown tables → structured enough for parsing, readable enough for scanning
- Single file → one place to look, not scattered across docs

### 2.2 Optional: Linear Integration

Linear provides:
- Visual board/timeline views
- Sprint planning
- Mobile access
- Team collaboration (future)

**Integration path**: Linear has an official Cursor MCP integration (`https://mcp.linear.app/mcp`) that allows Cursor agents to create, update, and query issues via natural language. This means the AI agent can sync between the markdown tracker and Linear.

**When to adopt Linear**: When either (a) the markdown tracker becomes insufficient (>50 active items, need filtering/sorting beyond what markdown provides), or (b) collaborators join the project.

### 2.3 Maintenance Protocol

| Trigger | Action |
|---------|--------|
| Starting a dev session | Read `DEV_TRACKER.md` for context |
| Completing a work item | Move to "Completed Milestones", update "Quick Status" |
| New work identified | Add to "Backlog" with phase and priority |
| ADR written or status changed | Update "Decision Log" |
| Documentation inconsistency found | Add to "Documentation Debt" |
| Phase completed | Update "Quick Status" table, archive milestone items |
| Monthly | Review backlog priorities, archive old completed items |

### 2.4 Cursor Agent Integration

The tracker is designed for AI agent consumption:

```
Session Start Protocol:
1. Read docs/plans/DEV_TRACKER.md        → "Where are we?"
2. Read docs/plans/IMPLEMENTATION_ROADMAP.md → "What's the plan?"
3. Read relevant PHASE_*.md              → "What are the details?"
4. Read relevant ADRs                     → "What are the constraints?"
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

### Alternative C: Linear Only (No Markdown)

Use Linear as the single source of truth.

- **Pros**: Excellent UI, keyboard-driven, great API, official Cursor MCP integration
- **Cons**: SaaS dependency, not available offline, requires network for every query, AI agent needs MCP call for every status check (latency), data not in repo
- **Rejected because**: AI agent needs instant, zero-latency access to project status. Markdown is always there. Linear is a good *complement*, not a good *replacement*.

### Alternative D: Dedicated Open-Source Tool (Plane, Taiga, Focalboard)

Self-host an open-source project management tool.

- **Pros**: Full control, local sovereignty, feature-rich
- **Cons**: Operational overhead (another service to maintain), Docker resource usage, API integration needed, no native Cursor/MCP support
- **Rejected because**: Running and maintaining a project management server for a single-developer project is disproportionate overhead. The project already runs PostgreSQL, Elasticsearch, Neo4j, and SLM Server.

---

## 4. Consequences

### Positive

- **Zero-latency context recovery**: Agents read one file, know where we are
- **No new infrastructure**: Just a markdown file in the repo
- **Version-controlled**: Can diff project status over time
- **Low maintenance**: Update when things change, not on a schedule
- **Composable**: Can add Linear later without replacing the markdown tracker
- **Agent-friendly**: Designed explicitly for AI agent consumption

### Negative

- **Manual maintenance**: Must be updated by human or AI agent (not auto-synced from code)
- **Limited views**: Markdown tables don't provide filtering, sorting, or timeline views (Linear fills this gap if needed)
- **Single-developer assumption**: Doesn't scale to teams without adding Linear or similar

### Risks

- Tracker becomes stale if not maintained (mitigate: Cursor rule that prompts agents to check/update it at session boundaries)
- Markdown tables become unwieldy past ~50 items (mitigate: archive aggressively, adopt Linear at that point)

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

1. Create `docs/plans/DEV_TRACKER.md` (done — created alongside this ADR)
2. Create Cursor rule for session-start orientation
3. Fix documentation debt items:
   - Resolve ADR-0008 numbering collision
   - Update stale ADR statuses
   - Update Phase 2.2 completion doc to reflect testing results

### Future: Linear Integration

If/when Linear is adopted:
1. Configure Linear MCP in `.cursor/mcp.json`
2. Create project and map phases to Linear projects
3. Sync backlog items as Linear issues
4. Keep `DEV_TRACKER.md` as the canonical quick-reference (Linear becomes the rich view)

### Estimated Effort

- Tracker creation: 1 day (done)
- Cursor rule: 30 minutes
- Documentation debt cleanup: 1-2 hours
- Linear setup (future): 2-3 hours
