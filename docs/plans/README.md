# Plans Directory

Project-level tracking documents and session history.

**Implementation plans live in [`docs/superpowers/plans/`](../superpowers/plans/) — not here.**
Name them `YYYY-MM-DD-fre-XXX-<slug>.md`. Do not write implementation plans into this directory.

## Key Files

| File | Purpose |
|------|---------|
| [`OWNER_CONSOLE.md`](OWNER_CONSOLE.md) | The owner's standing directives + the trust ladder (ADR-0131) |
| [`LAST_SESSION.md`](LAST_SESSION.md) | The last session's conversational overlay (written at wind-down) |
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
| The owner's standing directives | `docs/plans/OWNER_CONSOLE.md` |
| What we do next, in order | The dispatch resolver (`scripts/dispatch/next_resolver.py`) + Linear |
| Per-ticket state and status | [Linear](https://linear.app/frenchforest) — never a file |
| Architecture decisions | `docs/architecture_decisions/ADR-*.md` |
| Technical specifications | `docs/specs/` |
| Session scratch (plan-mode output) | `/plans/` (gitignored, never commit) |

## Workflow

1. **Check standing directives**: Read `OWNER_CONSOLE.md`; the computed queue comes from the dispatch resolver
2. **Get tasks**: Query Linear via MCP for approved issues
3. **Find the spec**: Issue description links to `docs/specs/` or `docs/architecture_decisions/`
4. **Implement**: Follow the spec and acceptance criteria
5. **Log the session**: Use `sessions/SESSION_TEMPLATE.md`

Specs live in `docs/specs/`. ADRs live in `docs/architecture_decisions/`.
