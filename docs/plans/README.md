# Plans Directory

Project-level tracking documents and session history.

**Implementation plans live in [`docs/superpowers/plans/`](../superpowers/plans/) — not here.**
Name them `YYYY-MM-DD-fre-XXX-<slug>.md`. Do not write implementation plans into this directory.

## Key Files

| File | Purpose |
|------|---------|
| [`MASTER_PLAN.md`](MASTER_PLAN.md) | Current priorities, sequencing, and project status |
| [`DEV_TRACKER.md`](DEV_TRACKER.md) | Linear workspace links and quick-reference index |
| [`PHASE_2.3_PLAN.md`](PHASE_2.3_PLAN.md) | Active phase sub-plan |
| [`VELOCITY_TRACKING.md`](VELOCITY_TRACKING.md) | Development velocity metrics |

## Subdirectories

| Directory | Contents |
|-----------|----------|
| [`sessions/`](sessions/) | Development session logs |
| [`completed/`](completed/) | Archived plans, summaries, and completed phase docs |

## What belongs here vs. elsewhere

| Content | Location |
|---------|----------|
| Implementation plans (`YYYY-MM-DD-fre-XXX-*.md`) | `docs/superpowers/plans/` |
| Project priorities and status | `docs/plans/MASTER_PLAN.md` |
| Architecture decisions | `docs/architecture_decisions/ADR-*.md` |
| Technical specifications | `docs/specs/` |
| Session scratch (plan-mode output) | `/plans/` (gitignored, never commit) |

## Workflow

1. **Check priorities**: Read `MASTER_PLAN.md`
2. **Get tasks**: Query Linear via MCP for approved issues
3. **Find the spec**: Issue description links to `docs/specs/` or `docs/architecture_decisions/`
4. **Implement**: Follow the spec and acceptance criteria
5. **Log the session**: Use `sessions/SESSION_TEMPLATE.md`

Specs live in `docs/specs/`. ADRs live in `docs/architecture_decisions/`.
