# Claude Code Configuration for Personal Agent

> Last updated: 2026-05-24

Project-specific rules, policies, and non-obvious patterns. Architecture and commands live in the root `CLAUDE.md`.

---

## Project Overview

**Personal Agent** — cognitive architecture research project: biologically-inspired agentic AI with persistent memory, knowledge graphs, and local LLM inference.

- **Type**: Research & Learning (not production-ready)
- **Dev environment**: VPS at `/opt/seshat` (Debian); SLM Server is separate (MLX/Apple Silicon)
- **Development Model**: Linear issue tracking (FrenchForest team) with approval gates

---

## Workspace Rules & Policies

### 1. Linear Implement Gate

**Policy: New == Needs Approval. Implement == Approved.**

- **Creating Issues**: state `"Needs Approval"` + label `"PersonalAgent"` (no "Needs Approval" label — use state only)
- **Before Implementation**: call `get_issue` to confirm `Approved` state
- **Never implement unapproved work**
- **List implementable work**: `list_issues` with `state: "Approved"`

Linear MCP: Team `FrenchForest` · `save_issue` to create · `get_issue` to verify · `list_issues` to filter

### 2. File Organization

**Root-level policy**: only essential project config, README, and core directories.

| Location | Purpose |
|----------|---------|
| `/src/personal_agent/` | Production source code |
| `/tests/` | Test suite |
| `/docs/reference/` | Standards, policies, checklists |
| `/docs/specs/` | Technical specifications |
| `/docs/plans/` | OWNER_CONSOLE, LAST_SESSION, DEV_TRACKER (project-level only) |
| `/docs/plans/sessions/` | Session logs |
| `/docs/architecture_decisions/` | ADRs |
| `/docs/superpowers/plans/` | Implementation plans (canonical location) |
| `/config/` | Runtime configuration templates |
| `/telemetry/` | Runtime telemetry data (evaluation runs, logs, etc. — mostly gitignored; see `.gitignore` for tracked baseline results) |
| **Never at root** | Session logs, action items, temp files |

### 3. Coding Standards

**Type-Driven Development** — sketch type signatures first, fill in values later.

**Type hints** (mandatory on all public APIs):
- Modern syntax: `str | None` not `Union[str, None]`; always annotate `-> None`
- Collections: prefer `collections.abc.Sequence[T]` over `list[T]` in signatures
- **Never `Any`** — use Protocol or defensive type narrowing instead

**Discriminated unions** — `Literal` + Union + dataclasses/Pydantic to make invalid states unrepresentable; use `match` for exhaustive dispatch.

**Immutability** — frozen dataclasses; `ConfigDict(frozen=True)` for Pydantic models.

**Docstrings** — Google style, required on all public classes/functions: Args, Returns, Raises.

**Error handling & logging**:
- Never bare `except:` — use `personal_agent.exceptions`
- Always `structlog` with `trace_id` (never `print()`)
- Never log secrets/PII

**Configuration**: `from personal_agent.config import settings` — never `os.getenv()`.

**Async**: all I/O async; pass `TraceContext` through call chains; `asyncio.to_thread()` for sync callouts.

**Naming**: modules `snake_case` · classes `PascalCase` · functions `snake_case` · constants `UPPER_SNAKE_CASE` · private `_single_underscore`.

**Schema changes**:
- No Alembic migrations — schema changes go in `docker/postgres/init.sql` + `docker/postgres/migrations/`; **run migrations as the `agent` superuser via `AGENT_DATABASE_ADMIN_URL`, not the app's `AGENT_DATABASE_URL`** (the restricted `seshat_app` role cannot run DDL — FRE-808)

### 3b. Investigating code — reach for `ast-grep`, not `grep`

**Trigger, stated as the reflex rather than the technique:** you are about to `grep` for a *call site*, a *signature*, a *usage*, or "every place that does X". Those are **shape** questions; `grep` only answers *text* questions. `ast-grep` is installed — `ast-grep run -p '<pattern>' -l py <path>`.

**The trap, and it is worse than grep's.** A pattern matching the wrong *node kind* returns a short, precise, authoritative-looking list while silently answering a different question — cross-check any new pattern against a known result before trusting its count. Grep's imprecision is visible; a wrong AST pattern's is not.

Invoke the `ast-grep` skill for worked patterns, the node-kind pitfalls, codemods, duplication hunting, and rule files rather than improvising.

### 4. Testing Standards

- Unit tests for business logic (mocked external deps)
- Integration tests for cross-module interactions (real DB/services)
- Each test fully independent, no shared state
- Coverage target: 80%+ for core orchestrator, brainstem, telemetry
- Mirror `src/` structure: `tests/personal_agent/<module>/test_<file>.py`
- Shared fixtures in `conftest.py`
- See root CLAUDE.md for test substrate isolation policy (FRE-375) — tests redirect to :7688/:9201/:5433.

### 5. Session Orientation

Before starting implementation:
1. Read `docs/plans/OWNER_CONSOLE.md` (standing directives + trust ladder)
2. Check `docs/plans/completed/` for recent context
3. Review relevant ADRs in `docs/architecture_decisions/`

---

## Development Workflow

### Port conflicts

Personal Agent `:9000` · SLM Server `:8000`. Service triage is `make ps` / `make logs SERVICE=<name>`.

---

## Key Files

| File | Purpose |
|------|---------|
| `docs/specs/COGNITIVE_ARCHITECTURE_REDESIGN_v2.md` | Current architecture (primary reference) |
| `docs/plans/OWNER_CONSOLE.md` | Owner's standing directives + the trust ladder (ADR-0131) |
| `docs/superpowers/plans/` | Implementation plans |
| `docs/architecture_decisions/ADR-*.md` | Design decisions |
| `docs/reference/TOOL_INTEGRATION_GUIDE.md` | Tool tier decision guide |

---

*Update this file when workspace policies change or new patterns are discovered.*
