# Claude Code Configuration for Personal Agent

> Last updated: 2026-05-24

Project-specific rules, policies, and non-obvious patterns. Architecture and commands live in the root `CLAUDE.md`.

---

## Project Overview

**Personal Agent** — cognitive architecture research project: biologically-inspired agentic AI with persistent memory, knowledge graphs, and local LLM inference.

- **Type**: Research & Learning (not production-ready)
- **Primary Language**: Python 3.12+
- **Storage**: PostgreSQL (sessions/metrics) + Elasticsearch (logs/traces) + Neo4j (knowledge graph)
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
| `/docs/plans/` | MASTER_PLAN, DEV_TRACKER (project-level only) |
| `/docs/plans/sessions/` | Session logs |
| `/docs/architecture_decisions/` | ADRs |
| `/docs/superpowers/plans/` | Implementation plans (canonical location) |
| `/config/` | Runtime configuration templates |
| `/telemetry/` | Runtime telemetry data (gitignored) |
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
1. Read `docs/plans/MASTER_PLAN.md`
2. Check `docs/plans/completed/` for recent context
3. Review relevant ADRs in `docs/architecture_decisions/`

### 6. Agent Planning & Review Workflows

- Plan in ADR format; create Linear issues from validated specs; link specs/ADRs
- Review: verify against spec, check standards, confirm tests pass

### 7. Model Routing Policy

Full policy: `$HOME/.claude/MODEL_ROUTING_POLICY.md` (global) · `.claude/MODEL_ROUTING_POLICY.md` (project copy)

| Tier | Model | Role |
|------|-------|------|
| 1 | Opus | Architect — specs, plans, ADRs, complex debugging |
| 2 | Sonnet | Implementer — feature work from plans, first-pass debugging (3 attempts max) |
| 3 | Haiku | Executor — Linear issues, git ops, linting, boilerplate |

**Plan is ready for Sonnet when ALL five are true**: complete code (not pseudocode) · exact file paths · exact test commands with expected output · atomic steps (2-5 min) · no deferred design decisions.

**Escalation**: 3 failed Sonnet attempts OR same error twice / self-revert / circular reasoning → escalate to Opus with full error context.

**Subagent dispatch**: `model` param — `"opus"` / `"sonnet"` / `"haiku"`.

**Linear labeling**: every issue gets exactly one label: `Tier-1:Opus`, `Tier-2:Sonnet`, or `Tier-3:Haiku`.

---

## Development Workflow

### Worktree → Main Merge (Gotcha)

Cannot `git checkout main` from a worktree — main is checked out in the primary repo. Always merge from the primary:

```bash
cd <path-to-primary-repo-clone> && git merge <branch> --no-edit && git push origin main
```

### Implementation Plan Naming Convention

**One canonical location:** `docs/superpowers/plans/YYYY-MM-DD-fre-XXX-<slug>.md`

**Never write to** `/plans/` (Claude Code scratch dir, gitignored) or `docs/plans/` (project-level docs only).

### Before Starting Work

1. Check Linear: `list_issues` with `state: "Approved"`
2. Read `docs/plans/MASTER_PLAN.md`
3. Check `docs/plans/completed/` for recent context
4. Review relevant ADRs
5. Start in Plan Mode

### Starting a Feature/Fix

1. **Verify Linear Issue** is `Approved` via `get_issue`
2. **Create Implementation Plan** in `docs/superpowers/plans/` — link specs/ADRs; atomic steps
3. **Write Tests First** (TDD)
4. **Implement** — docstrings, structlog, no `print()`
5. **Quality checks**: `make test` · `make mypy` · `make ruff-check` · `make ruff-format`
6. **PR** — address review feedback rigorously

### Debugging & Troubleshooting

**Service Won't Start:**
```bash
make ps
make logs SERVICE=postgres && make logs SERVICE=elasticsearch
make down && make up
```

**Port conflicts**: Personal Agent `:9000` · SLM Server `:8000` · check with `lsof -i :9000`

**Database:**
```bash
make shell SERVICE=postgres
# then: psql -U agent -d personal_agent
```

**Elasticsearch:**
```bash
curl http://localhost:9200/_cluster/health
./scripts/setup-elasticsearch.sh
```

---

## Key Files

| File | Purpose |
|------|---------|
| `docs/specs/COGNITIVE_ARCHITECTURE_REDESIGN_v2.md` | Current architecture (primary reference) |
| `docs/plans/MASTER_PLAN.md` | Current priorities |
| `docs/superpowers/plans/` | Implementation plans |
| `docs/architecture_decisions/ADR-*.md` | Design decisions |
| `docs/reference/TOOL_INTEGRATION_GUIDE.md` | Tool tier decision guide |

---

## Pre-Merge Checklist

- [ ] Issue is `Approved` in Linear
- [ ] Type hints on all public APIs
- [ ] Google-style docstrings on public classes/functions
- [ ] No `print()`, `os.getenv()`, bare `except:`
- [ ] `trace_id` in all structlog calls
- [ ] `make test` passes
- [ ] `make mypy` passes
- [ ] `make ruff-check` + `make ruff-format` clean
- [ ] Files placed per file organization rules (§2)
- [ ] ADRs/specs linked in commit message and PR
- [ ] No Alembic migrations — schema changes go in `docker/postgres/init.sql` + `docker/postgres/migrations/`; **run migrations as the `agent` superuser via `AGENT_DATABASE_ADMIN_URL`, not the app's `AGENT_DATABASE_URL`** (the restricted `seshat_app` role cannot run DDL — FRE-808)
- [ ] One pytest process at a time (convention — the enforcing hook was removed 2026-07-18)
- [ ] New tools use Tier 1/2; Tier 3 (MCP) requires ADR justification
- [ ] **PWA changes**: `cd seshat-pwa && npm run lint` exits 0 (mirrors backend `ruff-check`; FRE-395)

---

*Update this file when workspace policies change or new patterns are discovered.*
