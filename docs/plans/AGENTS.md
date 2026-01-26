# Plans & Sessions

Planning documents, session logs, and progress tracking.

## Structure

```
./
├── sessions/            # Development session logs
│   ├── SESSION_TEMPLATE.md
│   └── SESSION-YYYY-MM-DD-*.md
├── IMPLEMENTATION_ROADMAP.md
├── ACTION_ITEMS_YYYY-MM-DD.md
├── VELOCITY_TRACKING.md
└── README.md
```

## Session Logs

### When to Create

Create a session log when:

- Starting significant implementation work (>1 hour)
- Making architectural decisions
- Completing milestones
- Encountering important challenges

**Never** create for:

- Quick bug fixes
- Minor documentation updates
- Simple refactors

### Naming

`SESSION-YYYY-MM-DD-description.md`

Examples:

- `SESSION-2025-12-29-telemetry-implementation.md`
- `SESSION-2025-12-30-orchestrator-state-machine.md`

### Format

```markdown
# Session: [Title] — YYYY-MM-DD

**Date**: YYYY-MM-DD
**Duration**: X hours
**Goal**: [What we set out to accomplish]

## Work Completed

### 1. [Component/Feature]
- Implemented X
- Fixed Y
- Files: `path/to/file.py`

### 2. [Component/Feature]
- ...

## Decisions Made

### Decision: [Title]
- **Context**: Why this came up
- **Decision**: What we decided
- **Rationale**: Why
- **Captured in**: `../architecture_decisions/ADR-XXXX.md` (if significant)

## Challenges

### Challenge: [Issue]
- **Solution**: How resolved
- **Lesson**: What to remember

## Next Session

1. Goal 1
2. Goal 2

## Artifacts

- `src/path/file.py` — Created
- `tests/test_*.py` — Added tests
- `../architecture_decisions/ADR-*.md` — New ADR (if applicable)
```

### What to Include

- **Work completed**: Specific accomplishments, not intentions
- **Decisions**: Only architectural/significant decisions
- **Challenges**: Non-obvious problems and solutions
- **Files changed**: Actual paths

### What to Exclude

- Verbose narratives
- Implementation details (those go in code/specs)
- Personal information
- Timestamps for every action

## Action Items

### When to Update

Update `ACTION_ITEMS_YYYY-MM-DD.md` when:

- Completing items from current list
- New urgent items emerge
- Priorities change

**Create new dated file** monthly or when major milestone shifts.

### Format

```markdown
# Action Items — YYYY-MM-DD

## High Priority (This Week)
- [ ] Item with clear deliverable
- [ ] Item with clear deliverable

## Medium Priority (Next 2 Weeks)
- [ ] Item

## Future / Backlog
- [ ] Item

## Completed (Archive)
- [x] Completed item (YYYY-MM-DD)
```

## Roadmap Updates

Update `IMPLEMENTATION_ROADMAP.md` when:

- Completing major phases
- Significant scope changes
- Timeline adjustments

**Never** update for:

- Individual task completion (that's in action items)
- Minor delays
- Daily progress

Keep roadmap high-level (weeks, not days).

## Commands

```bash
# Find recent sessions
ls -lt ./sessions/ | head -5

# Find action items
ls ./ACTION_ITEMS_*.md

# Search sessions for topic
rg -n "telemetry|orchestrator" ./sessions/
```

## Critical

- Use ISO dates: `YYYY-MM-DD`
- **No personal names** in session logs (use "project owner")
- Link to specs/ADRs instead of duplicating content
- Keep session logs <300 lines
- Archive old action items (move to "Completed" section)

## Pre-Commit

Session logs don't need linting, but:

- Validate markdown syntax
- Check for broken links to specs/ADRs
- Verify ISO date format

## When Unsure

- **Too minor for session log?** Probably don't create one
- **Decision significant?** Create ADR, reference in session
- **Just progress update?** Update action items, not roadmap
