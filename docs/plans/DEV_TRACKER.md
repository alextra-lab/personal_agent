# Development Tracker

> **Linear workspace:** [FrenchForest](https://linear.app/frenchforest) · Team: FrenchForest
> **Last synced:** 2026-02-22

Linear is the single source of truth for work items, priorities, and status.
Markdown docs (`PHASE_*.md`, ADRs, `IMPLEMENTATION_ROADMAP.md`) are the specs — the *how*, not the *what*.

---

## Stream Projects

| Project | Status | Linear Link |
|---------|--------|-------------|
| **2.3 Homeostasis & Feedback** | In Progress | [View](https://linear.app/frenchforest/project/23-homeostasis-and-feedback-dbce3b171536) |
| **2.4 Multi-Agent Orchestration** | Planned | [View](https://linear.app/frenchforest/project/24-multi-agent-orchestration-4c9ee23c6f51) |
| **2.5 Seshat Memory Librarian** | Planned | [View](https://linear.app/frenchforest/project/25-seshat-memory-librarian-3d30e7d2d24f) |
| **2.6 Conversational Agent MVP** | Planned | [View](https://linear.app/frenchforest/project/26-conversational-agent-mvp-40fbc8c41510) |
| **3.0 Daily-Use Interface** | Planned | [View](https://linear.app/frenchforest/project/30-daily-use-interface-60a517bd90f6) |

## Completed Phases

| Phase | Name | Completed |
|-------|------|-----------|
| 1.0 | MVP (CLI Agent) | 2026-01 |
| 2.1 | Service Foundation | 2026-01-22 |
| 2.2 | Memory & Second Brain | 2026-01-23 |

## For AI Agents (Cursor)

When starting a session:
1. **Query Linear** via MCP (`plugin-linear-linear`) for current issues, priorities, and status.
2. **To implement an issue**: read the issue description in Linear — it contains the spec path, files, and acceptance criteria.
3. **Specs live in**: `docs/plans/PHASE_*.md`, `docs/architecture_decisions/ADR-*.md`, `IMPLEMENTATION_ROADMAP.md`.
4. **When done**: update the Linear issue status via MCP.

Example: *"What should I work on?"* → call `list_issues` filtered by state=Todo or In Progress.  
Example: *"Complete FRE-8"* → call `get_issue` for FRE-8 → read spec path → implement → `update_issue` state=Done.
