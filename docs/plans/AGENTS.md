# Plans & Sessions

Active project plans, tracking, and session history.

## Structure

```
./
├── MASTER_PLAN.md           # Current priorities and sequencing (start here)
├── DEV_TRACKER.md           # Linear workspace links
├── PHASE_*.md               # Active phase sub-plans
├── VELOCITY_TRACKING.md     # Development velocity metrics
├── sessions/                # Development session logs
│   ├── SESSION_TEMPLATE.md
│   └── SESSION-YYYY-MM-DD-*.md
└── completed/               # Archived plans and summaries
```

Specs live in `docs/specs/`. ADRs live in `docs/architecture_decisions/`.
Plans track *what* and *when*; specs and ADRs track *how* and *why*.

## Workflow

1. Read `MASTER_PLAN.md` for current priorities
2. Query Linear via MCP for approved issues
3. Read the linked spec in `docs/specs/` or ADR
4. Implement, test, validate acceptance criteria
5. Update Linear issue status

## Session Logs

### When to Create

- Significant implementation work (>1 hour)
- Architectural decisions made
- Milestone completions
- Important challenges encountered

**Skip for**: quick bug fixes, minor doc updates, simple refactors.

### Naming

`SESSION-YYYY-MM-DD-description.md` — use the template in `sessions/SESSION_TEMPLATE.md`.

### What to Include

- **Work completed**: specific accomplishments, not intentions
- **Decisions**: only architectural/significant ones (link to ADRs)
- **Challenges**: non-obvious problems and solutions
- **Files changed**: actual paths

### What to Exclude

- Verbose narratives
- Implementation details (those belong in code/specs)
- Personal information

## Master Plan Updates

Update `MASTER_PLAN.md` when:
- Phases complete (move to Completed, FIFO)
- Priorities shift (reorder Current Focus)
- New work is approved

**Never** update for individual task completion (that's in Linear).

## Critical

- Use ISO dates: `YYYY-MM-DD`
- **No personal names** in session logs
- Link to specs/ADRs instead of duplicating content
- Keep session logs <300 lines
