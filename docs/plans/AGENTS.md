# Plans & Sessions

Active project plans, tracking, and session history.

## Structure

```
./
├── OWNER_CONSOLE.md         # Owner's standing directives + trust ladder (start here)
├── LAST_SESSION.md          # Last session's conversational overlay
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

1. Read `OWNER_CONSOLE.md` for the owner's standing directives
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

## The owner console

`OWNER_CONSOLE.md` is **owner-voice only** (ADR-0131 D2). Sessions do not write it. Master may
transcribe a directive the owner gave conversationally — verbatim, attributed, dated — and may retire
one only when its stated condition is met.

There is **no plan document to update**: derived state lives in Linear, git and the dispatch resolver;
analysis and findings live in tickets and research documents. A finding with no home is a one-line
`Backlog` ticket, never a line parked in a file.

## Critical

- Use ISO dates: `YYYY-MM-DD`
- **No personal names** in session logs
- Link to specs/ADRs instead of duplicating content
- Keep session logs <300 lines
