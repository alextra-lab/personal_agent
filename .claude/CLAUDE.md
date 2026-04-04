# Claude Code Configuration for Personal Agent

> Last updated: 2026-04-04

This file provides Claude with comprehensive context about the **Personal Agent** project structure, architectural decisions, development standards, and workflow policies. Claude should apply all guidelines in this file when assisting with development tasks.

---

## Project Overview

**Personal Agent** is a cognitive architecture research project exploring biologically-inspired agentic AI systems with persistent memory, knowledge graphs, and local LLM inference.

### Key Characteristics

- **Type**: Research & Learning Project (not production-ready)
- **Primary Language**: Python 3.12+
- **Architecture**: Service-based FastAPI backend with MLX-optimized local LLM inference
- **Storage**: PostgreSQL (sessions/metrics) + Elasticsearch (logs/traces) + Neo4j (knowledge graph)
- **Infrastructure**: Docker-based with structured services on separate ports
- **Development Model**: Linear issue tracking (FrenchForest team) with approval gates

### Critical Context

- **This is a learning project** exploring cognitive systems, agent orchestration, and memory consolidation
- **Not production-ready** – experimental design and frequent architectural shifts
- **Apple Silicon optimized** – uses MLX framework for local model inference
- **Service-oriented** – runs as persistent service (port 9000) separate from SLM Server (port 8000)

---

## Boris Cherny's Claude Code Workflow

This `.claude/CLAUDE.md` file itself is inspired by **Boris Cherny's actual Claude Code practices**. Cherny is the creator of Claude Code at Anthropic and has shared his team's workflow publicly. These practices are what the Claude Code team uses daily to ship production code.

### Key Practices

| Practice | What It Does | Why It Matters |
|----------|-------------|-----------------|
| **Parallel Execution with Git Worktrees** | Run 3–5 Claude sessions in parallel, each with isolated context | Eliminates context switching; allows async task execution; scales reasoning |
| **Plan Mode First** | Start every non-trivial task with `/plan` (Shift+Tab twice) | Good plans enable one-shot implementation; clarifies acceptance criteria |
| **Invest in CLAUDE.md** | Document project-specific rules, workarounds, and patterns | Claude learns faster when explicit; reduces repeated mistakes over time |
| **Verification Loops** | Give Claude a way to verify its work (tests, browser, CLI) | Feedback loops improve quality 2–3x; prevents hallucination in isolation |
| **Subagents for Parallelism** | Delegate independent exploration/analysis to separate agents | Keeps main context clean; enables deep reasoning on specific domains |
| **Custom Slash Commands** | Automate repetitive tasks (e.g., `/commit-push-pr`) | Removes manual boilerplate; consistent execution; commands live in git |

### How We Apply These

1. **This CLAUDE.md is your primary tool** – Update it after every correction; Claude learns from it
2. **Use worktrees for feature branches** – Each with its own Claude session, git branch, and context
3. **Start with Plan mode** – Don't skip this even for small changes; clarity compounds
4. **Verification is non-negotiable** – Tests, type checking, linting, or manual checks before claiming done
5. **Subagents for code exploration** – When the codebase is large, spawn agents to explore different areas in parallel
6. **Commands > Prompts** – If you repeat a workflow, automate it as a slash command or hook

### Real Example from Cherny's Setup

Boris doesn't write much code himself anymore. His workflow:

- 5–10 Claude sessions running in parallel (desktop + web + terminals)
- Each working on independent tasks with git worktrees
- System notifications when any Claude needs input
- Most sessions start in Plan mode
- Verification via live browser testing (Claude opens the app, tests it, iterates)
- `CLAUDE.md` updated constantly as the team discovers new patterns

Result: 259 pull requests and 40,000+ lines shipped in one month (Dec 2025) without opening an IDE.

---

## Workspace Rules & Policies

All of the following workspace rules **MUST** be followed consistently:

### 1. Linear Implement Gate

**Policy: New == Needs Approval. Implement == Approved.**

- **Creating Issues**: Always set state to `"Needs Approval"` and add label `"PersonalAgent"` (no `"Needs Approval"` label exists — use state only)
- **Before Implementation**: Call `get_issue` to verify the issue has state/label **Approved**
- **Never implement unapproved work** – tell the user to move the issue to Approved first
- **List implementable work** using `list_issues` with filter `state: "Approved"`

**Linear MCP Details:**

- Team: `FrenchForest`
- Tool: `save_issue` for creating; `get_issue` for verification; `list_issues` for filtering

### 2. File Organization

**Root-level policy**: Only essential project config, README, and core directories allowed.

| Location | Purpose |
|----------|---------|
| `/src/personal_agent/` | Production source code |
| `/tests/` | Test suite |
| `/docs/` | All documentation |
| `/docs/reference/` | Standards, policies, checklists |
| `/docs/specs/` | Technical specifications |
| `/docs/guides/` | How-to and setup guides |
| `/docs/plans/` | Project planning and tracking |
| `/docs/plans/sessions/` | Session logs |
| `/docs/architecture/` | Architecture specs |
| `/docs/architecture_decisions/` | ADRs (Architecture Decision Records) |
| `/docs/research/` | Research notes and analysis |
| `/config/` | Runtime configuration templates |
| `/telemetry/` | Runtime telemetry data (gitignored) |
| **Never at root** | Session logs, action items, validation checklists, temporary files |

### 3. Coding Standards

**Based on Boris Cherny's "Programming TypeScript" principles, adapted for Python.**

#### Type-Driven Development (Cherny Core Philosophy)

- **Sketch type signatures first, fill in values later** – This is the primary design approach
- Define function signatures with complete type hints before implementation
- Let types guide architecture and catch errors at compile-time
- Use types as executable documentation
- **In Claude Code context**: Start with Plan mode to design signatures and contracts before implementation

```python
# Good: Type signature first (defines contract clearly)
def consolidate_entities(
    entities: Sequence[Entity],
    knowledge_graph: KnowledgeGraph,
    ctx: TraceContext
) -> dict[str, ConsolidationResult]:
    """Implementation follows from type contract."""
    # ... implementation ...

# Bad: Implementation-driven, types added after
def consolidate_entities(entities, knowledge_graph, ctx):
    # ... then figure out types ...
```

#### Type Hints (Mandatory for Public APIs)

- Use modern syntax: `str | None` not `Union[str, None]`
- Always annotate return types, including `-> None`
- For collections: prefer `collections.abc` (e.g., `Sequence[str]`) over built-ins
- **Never use `Any`** – Cherny principle: use `Unknown` pattern (Protocol or defensive type narrowing) instead
- Avoid implicit `Any` – mypy strict mode catches this

```python
# Good: Explicit types that guide behavior
def process_message(msg: str | None) -> dict[str, Any]: ...

# Bad: Implicit Any (loses type safety)
def process_message(msg): ...

# Bad: Explicit Any (defeats purpose of types)
def process_message(msg: Any) -> Any: ...

# Good: Use Protocol for structural typing instead of Any
from typing import Protocol
class Loggable(Protocol):
    def log(self) -> str: ...

def handle_object(obj: Loggable) -> None:
    # Type system knows obj has .log() method
    print(obj.log())
```

#### Discriminated Unions for State Modeling

- Use union types with explicit discriminators to **make invalid states unrepresentable**
- Python: Use `Literal` + Union + dataclasses/Pydantic for clean state machines
- This prevents entire categories of bugs at the type level
- In Claude Code: These patterns make verification faster because the state space is explicit

```python
# Good: State machine with discriminated unions
from typing import Literal
from dataclasses import dataclass

@dataclass
class TaskPending:
    type: Literal["pending"] = "pending"
    created_at: datetime

@dataclass
class TaskRunning:
    type: Literal["running"] = "running"
    started_at: datetime
    process_id: int

@dataclass
class TaskComplete:
    type: Literal["complete"] = "complete"
    result: Any
    duration_seconds: float

TaskState = TaskPending | TaskRunning | TaskComplete

def handle_task(state: TaskState) -> None:
    match state:
        case TaskPending():
            # Type narrowed: only TaskPending methods available
            start_task(state.created_at)
        case TaskRunning():
            # Type narrowed: only TaskRunning methods available
            monitor_process(state.process_id)
        case TaskComplete():
            # Type narrowed: only TaskComplete methods available
            log_result(state.result)
```

#### Immutability Where Possible (Cherny Principle)

- Use frozen dataclasses and Pydantic models with `frozen=True`
- Avoid mutable default arguments
- Prefer `collections.abc.Sequence` over `list` in function signatures (signals immutability intent)
- Reduces entire classes of bugs (shared mutable state)

```python
# Good
from dataclasses import dataclass

@dataclass(frozen=True)
class Entity:
    name: str
    type: str
    metadata: Mapping[str, Any]  # Immutable view

# Good: Pydantic frozen
from pydantic import BaseModel, ConfigDict

class Config(BaseModel):
    model_config = ConfigDict(frozen=True)
    timeout: float
```

#### Docstrings (Google Style)

- **Google style** for all public classes/functions
- Required sections: Args, Returns, Raises (if applicable)
- Include Examples for complex logic
- Document type constraints that can't be expressed in signatures

#### Error Handling & Logging

- **Never** use bare `except:` clauses
- **Always** use project exceptions from `personal_agent.exceptions`
- **Always** use structured logging via `structlog` (never `print()`)
- Include `trace_id` in all logs
- Never log secrets/PII – redact first
- Model errors as discriminated unions when possible (like state machines)
- **Verification principle**: Every change must have a way to verify it works (tests, CLI output, browser, etc.)

```python
# Good: Error modeling as union type
@dataclass
class ToolSuccess:
    type: Literal["success"] = "success"
    result: Any

@dataclass
class ToolFailure:
    type: Literal["failure"] = "failure"
    error: ToolExecutionError
    trace_id: str

ToolResult = ToolSuccess | ToolFailure

# Type system guarantees error is handled
def handle_result(result: ToolResult) -> None:
    match result:
        case ToolSuccess():
            process(result.result)
        case ToolFailure():
            log.error("tool_failed", error=str(result.error), trace_id=result.trace_id)
```

#### Verification (Cherny's Most Important Principle)

Cherny's rule: **"The most important thing to get great results out of Claude Code is giving it a way to verify its work."**

- Every code change must have a verification step defined before implementation
- Verification can be: tests, CLI checks, browser testing, live app testing, or logs
- Feedback loops improve Claude's output quality 2–3x
- Never claim code is done without verification proof

#### Configuration Access

- **NEVER** use `os.getenv()` or `os.environ`
- **ALWAYS** use `from personal_agent.config import settings`
- All config is type-safe, validated via Pydantic (principle: catch errors early via types)

#### Async/Await

- Use async for I/O-bound operations
- Always pass `TraceContext` through async call chains
- Use `asyncio.to_thread()` for sync functions in async context

#### Naming Conventions

- Modules: `snake_case.py`
- Classes: `PascalCase`
- Functions: `snake_case`
- Constants: `UPPER_SNAKE_CASE`
- Private members: `_single_underscore` (not `__double__`)

#### Anti-Patterns (Never Do)

- Bare `except:` clauses
- `print()` statements
- `os.getenv()` or `os.environ` access
- Global mutable state
- Magic strings (use enums or Literal types)
- Comments that just narrate code
- Using `Any` type (use Protocol or defensive narrowing instead)
- Mutable default arguments in functions

### 4. Testing Standards

**Applied to all test writing and debugging:**

- **Unit tests** for business logic with mocked external dependencies
- **Integration tests** for cross-module interactions (use real DB/services in test containers)
- **Test isolation**: Each test completely independent, no shared state
- **Real LLM tests**: Only when explicitly required; use mocks/fixtures for CI
- **Coverage target**: 80%+ for core orchestrator, brainstem, and telemetry modules

**Test File Placement:**

- Mirror `src/` structure: `tests/personal_agent/orchestrator/test_routing.py` for `src/personal_agent/orchestrator/routing.py`
- Use `conftest.py` for shared fixtures

### 5. Session Orientation

**Before starting implementation work:**

- Read the current Master Plan at `docs/plans/MASTER_PLAN.md`
- Understand the current phase/iteration
- Check recently completed work in `docs/plans/completed/`
- Verify which systems are operational vs. in-progress
- Be aware of known limitations and experimental systems

**Key Phases:**

- **Phase 2.1**: Service architecture (✅ complete)
- **Phase 2.2**: Knowledge graph + memory (✅ complete)
- **Redesign v2 — Slice 1**: Pre-LLM Gateway, single-brain, MemoryProtocol, Stage A delegation (✅ complete)
- **Redesign v2 — Slice 2**: Decomposition, sub-agents, HYBRID execution, memory promotion, Stage B delegation, insights (✅ complete)
- **Evaluation Phase**: Building real usage traces to evaluate Slices 1 & 2 (🚀 in progress)
- **Redesign v2 — Slice 3**: Intelligence — proactive memory, programmatic delegation, self-improvement (📋 planned, blocked on evaluation data)

### 6. Agent Planning & Review Workflows

**When maintaining the Master Plan:**

- Plan work in ADR format (decision-driven)
- Create Linear issues from validated specs
- Link specs/ADRs in Linear issues
- Use explicit approval gates

**When reviewing completed work:**

- Verify implementation against spec
- Check coding standards compliance
- Ensure tests pass and coverage adequate
- Validate with real system if applicable

### 7. Model Routing Policy

**Full policy:** `$HOME/.claude/MODEL_ROUTING_POLICY.md` (global) · `.claude/MODEL_ROUTING_POLICY.md` (project copy)

**Decision tree — apply to every task, plan, issue, and subagent dispatch:**

```
Does this task require a design decision?
  YES → Tier-1: Opus
  NO ↓

Is there a detailed plan with complete code?
  NO → Tier-1: Opus (write the plan first)
  YES ↓

Might the executor need to adapt to surprises?
  YES → Tier-2: Sonnet
  NO ↓

Is it purely mechanical (copy/paste/run)?
  YES → Tier-3: Haiku / Qwen
  NO  → Tier-2: Sonnet (safe default)
```

**Tier summary:**

| Tier | Model | Role | Examples |
|------|-------|------|----------|
| 1 | Opus | Architect | Specs, plans, plan review, ADRs, complex debugging (escalated) |
| 2 | Sonnet | Implementer | Feature implementation from plans, first-pass debugging (3 attempts max) |
| 3 | Haiku/Qwen | Executor | Linear issues, git ops, linting fixes, boilerplate, template docs |

**Spec quality test** — a plan is ready for Sonnet (plan-driven implementation) when ALL five are true:

1. Complete code (not pseudocode)
2. Exact file paths
3. Exact test commands with expected output
4. Atomic steps (2-5 min each)
5. No design decisions deferred

**Escalation:** Sonnet debugging → 3 failed attempts OR floundering (same error twice, self-revert, circular reasoning, wrong layer) → escalate to Opus with full error context including files modified.

**Cross-tier tasks:** Decompose into subtasks at different tiers when independently executable; otherwise run at the highest tier required.

**Linear labeling:** Every issue gets exactly one label: `Tier-1:Opus`, `Tier-2:Sonnet`, or `Tier-3:Haiku`. Plans include a Model column in the summary table.

**Subagent dispatch:** Use the `model` parameter — `"opus"` for plan review, `"sonnet"` for implementation, `"haiku"` for mechanical tasks.

---

## Architecture Overview

### Cognitive Architecture (Redesign v2)

*Full spec: `docs/specs/COGNITIVE_ARCHITECTURE_REDESIGN_v2.md`*
*Status: Slices 1 & 2 Implemented — Evaluation Phase (building usage traces before Slice 3)*

```
+---------------------------------------------------------------------+
|                        INTERFACE LAYER                               |
|  CLI . API /chat (port 9000) . Future: mobile, voice                |
+--------------------------------+------------------------------------+
                                 |
+--------------------------------v------------------------------------+
|                      PRE-LLM GATEWAY (7 stages)                     |
|                                                                     |
|  Security -> Session -> Governance -> Intent Classification         |
|     -> Decomposition Assessment -> Context Assembly -> Budget       |
+--------------------------------+------------------------------------+
                                 |
              +------------------v--------+
              |      PRIMARY AGENT        |
              |   Qwen3.5-35-A3B         |
              |                           |
              |   Conversational core     |
              |   Tool calling (MCP +     |
              |     native)               |
              |   Delegation composer     |
              +--+----------+--------+----+
                 |          |        |
    +------------v--+  +---v-----+  +v---------------------------+
    |   TOOLS       |  |  SESHAT |  |   EXPANSION LAYER          |
    |               |  | MEMORY  |  |                             |
    |  MCP Gateway  |  |         |  |  Internal sub-agents        |
    |  Native tools |  | Protocol|  |  (ephemeral, task-scoped)   |
    |  (extensible) |  | Neo4j   |  |  External delegation        |
    |               |  |         |  |  (Claude Code, Codex, etc.) |
    +---------------+  +----+----+  +-------------+---------------+
                            |                     |
              +-------------v---------------------v--------------+
              |              BRAINSTEM                            |
              |  Homeostasis . Sensors . Mode manager             |
              |  Consolidation . Expand/contract signals          |
              +------------------------+-------------------------+
                                       |
              +------------------------v-------------------------+
              |           SELF-IMPROVEMENT LOOP                  |
              |  Captain's Log . Insights engine                 |
              |  Promotion pipeline                              |
              +------------------------+-------------------------+
                                       |
              +------------------------v-------------------------+
              |              INFRASTRUCTURE                      |
              |  PostgreSQL (sessions, metrics, cost)            |
              |  Elasticsearch (traces, logs, insights)          |
              |  Neo4j (knowledge graph, memory)                 |
              +--------------------------------------------------+
```

### Core Modules

| Module | Purpose | Status |
|--------|---------|--------|
| `request_gateway/` | Pre-LLM Gateway: 7-stage deterministic pipeline (intent, decomposition, budget) | ✅ Slice 1 & 2 |
| `orchestrator/` | Request orchestration + sub-agents + HYBRID expansion | ✅ Operational |
| `memory/` | Seshat memory: protocol, service, episodic→semantic promotion | ✅ Slice 1 & 2 |
| `insights/` | Cross-data analysis engine (delegation patterns, cost anomalies) | ✅ Slice 2 |
| `brainstem/` | Homeostasis sensors, scheduling, expand/contract signals | ✅ Operational |
| `llm_client/` | OpenAI-compatible LLM client | ✅ Operational |
| `mcp/` | MCP Gateway for tool discovery | ✅ Operational |
| `telemetry/` | Structured logging (structlog/ES) | ✅ Operational |
| `captains_log/` | Self-improvement data capture | ✅ Operational |
| `service/` | FastAPI service layer | ✅ Operational |
| `config/` | Pydantic-based configuration | ✅ Operational |

### Key Data Flows

**Request Flow (Redesign v2):**

1. Client sends message via API (`/chat`)
2. Pre-LLM Gateway processes request (7 stages: security → session → governance → intent → decomposition → context → budget)
3. Gateway outputs: intent classification, decomposition strategy (SINGLE/HYBRID/DECOMPOSE/DELEGATE), assembled context
4. Primary agent processes with assembled context
5. If HYBRID: sub-agents spawned, results synthesized
6. If DELEGATE: delegation package composed for external agent
7. Response returned to client
8. Telemetry emitted to Captain's Log + Elasticsearch

**Memory Flow:**

1. Task captured in fast structured log
2. Brainstem consolidation scheduler triggered
3. Entity extraction via qwen3-8b
4. Entities stored in Neo4j knowledge graph
5. Episodic memories promoted to semantic via `promote()` pipeline
6. Memory queries use multi-factor relevance scoring

---

## Development Workflow

### Claude Code Best Practices (From Cherny)

**Before every coding session, remember these 5 principles:**

1. **Use Plan Mode first** – Type `/plan` (Shift+Tab) at the start of non-trivial tasks. Good plans enable one-shot implementation.
2. **This CLAUDE.md is your primary feedback mechanism** – After each session, update this file with patterns, gotchas, or new conventions Claude should follow. Claude learns faster from explicit rules than from hints.
3. **Verification is non-negotiable** – Before claiming a task is done, specify how Claude will verify the work (tests, type checking, browser test, CLI output, etc.).
4. **Use subagents for exploration** – When exploring large codebases or independent tasks, spawn dedicated agents instead of bloating the main session.
5. **Consider parallel execution** – For independent features/fixes, run multiple Claude sessions with git worktrees to parallelize work.

### Worktree → Main Merge (Gotcha)

Cannot `git checkout main` from a worktree — main is checked out in the primary repo. Always merge from the primary:

```bash
cd <path-to-primary-repo-clone> && git merge <branch> --no-edit && git push origin main
```

### Implementation Plan Naming Convention

Plans live at `docs/superpowers/plans/YYYY-MM-DD-slice-N-theme.md`
Example: `docs/superpowers/plans/2026-03-18-slice-2-expansion.md`

### Before Starting Work

1. **Check Linear Issues**: Use `list_issues` with filter `state: "Approved"` to see implementable work
2. **Read Master Plan**: Review current phase and priorities at `docs/plans/MASTER_PLAN.md`
3. **Check Recent Work**: See `docs/plans/completed/` for context
4. **Understand Specs**: Review relevant ADRs in `docs/architecture_decisions/`
5. **Start in Plan Mode** – Spend time on the plan, not the implementation

### Starting a Feature/Fix

1. **Create/Verify Linear Issue**:
   - New issues must have state `"Needs Approval"` and label `"PersonalAgent"` (no `"Needs Approval"` label exists)
   - Wait for human approval before implementation
   - When approved, verify issue is moved to `Approved` state

2. **Create Implementation Plan**:
   - Document approach in ticket
   - Link relevant specs/ADRs
   - Break into testable steps

3. **Write Tests First** (TDD):
   - Unit tests for business logic
   - Integration tests for cross-module interactions
   - Mock external dependencies in unit tests
   - Ensure tests pass before implementation

4. **Implement**:
   - Follow coding standards strictly
   - Include docstrings for all public APIs
   - Use structured logging with trace_id
   - No print() statements

5. **Run Quality Checks**:
   - `uv run pytest` – all tests pass
   - `uv run mypy src/` – type checking
   - `uv run ruff check src/` – linting
   - `uv run ruff format src/` – formatting

6. **Code Review**:
   - Request review via GitHub PR
   - Address feedback rigorously
   - Don't rationalize away standards

### Debugging & Troubleshooting

**Service Won't Start:**

```bash
docker-compose ps
docker-compose logs postgres
docker-compose logs elasticsearch
docker-compose down && ./scripts/init-services.sh
```

**Port Conflicts:**

- Personal Agent: 9000
- SLM Server: 8000
- Check: `lsof -i :9000` and `lsof -i :8000`

**Database Issues:**

```bash
docker-compose exec postgres psql -U agent -d personal_agent
```

**Elasticsearch Issues:**

```bash
curl http://localhost:9200/_cluster/health
./scripts/setup-elasticsearch.sh
```

---

## Quick Reference

### Key Files to Know

- **Current Architecture**: `docs/specs/COGNITIVE_ARCHITECTURE_REDESIGN_v2.md`
- **Slice 1 Plan**: `docs/superpowers/plans/2026-03-16-slice-1-foundation.md`
- **Slice 2 Plan**: `docs/superpowers/plans/2026-03-18-slice-2-expansion.md`
- **Master Plan**: `docs/plans/MASTER_PLAN.md`
- **Vision**: `docs/VISION_DOC.md`
- **Coding Standards**: `.cursor/rules/coding-standards.mdc`
- **Testing Standards**: `.cursor/rules/testing-standards.mdc`
- **ADRs**: `docs/architecture_decisions/ADR-*.md`

### Running the System

```bash
# Start infrastructure
./scripts/init-services.sh

# Start SLM Server (separate terminal)
cd ../slm_server && ./start.sh

# Start Personal Agent service (separate terminal)
uv run uvicorn personal_agent.service.app:app --reload --port 9000

# Chat with agent
uv run agent "Your question here"

# Run tests
uv run pytest

# Type checking
uv run mypy src/

# Linting
uv run ruff check src/
```

### Important URLs

| Service | URL | Purpose |
|---------|-----|---------|
| Personal Agent API | <http://localhost:9000> | Main service |
| API Docs | <http://localhost:9000/docs> | Swagger UI |
| SLM Server | <http://localhost:8000> | LLM inference |
| Kibana | <http://localhost:5601> | Log visualization |
| Neo4j Browser | <http://localhost:7474> | Knowledge graph |

---

## When Claude Should

### Tool Integration (ADR-0028 — CLI-First, as of 2026-04-04)

**RULE: New tools MUST justify why Tier 1 or Tier 2 is insufficient before using Tier 3 (MCP).**

Three-tier decision tree:
1. **Tier 1 — Native Python** (`src/personal_agent/tools/<name>.py`): Default for REST APIs, local services. Zero subprocess overhead. Pattern: httpx + `ToolDefinition` + executor + governance entry + tests.
2. **Tier 2 — CLI + SKILL.md** (`docs/skills/<name>.md`): When a mature CLI exists (gh, docker, git, curl). No Python code needed; agent reads SKILL.md and invokes CLI directly.
3. **Tier 3 — MCP** (reserved): Only for browser automation, bidirectional streaming, or stateful protocol requirements. Requires explicit ADR justification.

Full guide: `docs/reference/TOOL_INTEGRATION_GUIDE.md`

### ✅ DO

- **Always** check Linear issue status before implementing (must be Approved)
- **Always** follow coding standards strictly – they're not suggestions
- **Always** read `.cursor/rules/` files for current policies
- **Always** use structured logging with `trace_id`
- **Always** write tests before implementation code
- **Always** include docstrings (Google style) for public APIs
- **Always** verify with `mypy` and `ruff` before claiming code is ready
- **Always** place files according to file organization rules
- **Always** use `personal_agent.config.settings` for configuration access
- **Always** ask for clarification if a task seems to violate standards
- **Always** use Tier 1/2 for new tools; justify before choosing Tier 3 (MCP)

### ❌ DON'T

- **Never** implement unapproved Linear issues
- **Never** use `print()` – use structured logging
- **Never** use `os.getenv()` – use config settings
- **Never** use bare `except:` clauses
- **Never** skip type hints on public APIs
- **Never** create files at root that belong in `docs/`
- **Never** skip docstrings for public functions/classes
- **Never** ignore linter errors without fixing them
- **Never** claim work is complete without running tests + mypy + ruff
- **Never** rationalize away coding standards

---

## Experimental Systems & Known Limitations

### Redesign v2 — Evaluation Phase (Current)

**Implemented (Slices 1 & 2):**

- Pre-LLM Gateway (7-stage deterministic pipeline)
- Single-brain primary agent (no role-switching)
- Request gateway: intent classification, decomposition assessment
- HYBRID expansion path with sub-agent spawning
- Delegation Stage B (structured DelegationPackage/DelegationOutcome)
- Seshat MemoryProtocol with episodic→semantic promotion
- Expansion/contraction budget signals
- Context budget management
- Insights engine (delegation pattern analysis)
- Kibana dashboards for Slice 2 telemetry

**In Evaluation:**

- Building real usage traces to evaluate implemented architecture
- Gathering delegation pattern data for Slice 3 decisions
- Graphiti experiment comparison pending

**Known Limitations:**

- Knowledge graph entity extraction depends on qwen3-8b availability
- Memory query relevance scoring is multi-factor but not ML-based
- Sub-agent spawning not yet tested under production load
- Delegation Stage C (programmatic orchestration) not yet implemented — Slice 3

### When Making Changes to Experimental Systems

1. Document assumptions in ADR
2. Include comprehensive integration tests
3. Add telemetry metrics
4. Flag breaking changes prominently in PR description
5. Be prepared for iteration – this is a research project

---

## Documentation Structure

All documentation follows this hierarchy:

```
docs/
├── VISION_DOC.md                     # Project vision and philosophy
├── specs/                            # Technical specifications
│   ├── COGNITIVE_ARCHITECTURE_REDESIGN_v2.md  # Current architecture (primary reference)
│   └── *.md (CLI, conversation, telemetry specs)
├── reference/                        # Standards, policies, checklists
│   ├── CODING_STANDARDS.md
│   ├── CODING_CONVENTIONS.md
│   ├── TESTING_STANDARDS.md
│   ├── PR_REVIEW_RUBRIC.md
│   ├── ROOT_LEVEL_POLICY.md
│   └── PROJECT_DIRECTORY_STRUCTURE.md
├── guides/                           # How-to guides
│   ├── USAGE_GUIDE.md
│   ├── CONFIGURATION.md
│   ├── KIBANA_DASHBOARDS.md
│   └── *.md (GPU metrics, telemetry, MCP setup)
├── plans/                            # Project planning
│   ├── MASTER_PLAN.md               # Current priorities
│   └── completed/                    # Archived plans & roadmap
├── superpowers/plans/                # Slice implementation plans
│   ├── 2026-03-16-slice-1-foundation.md
│   └── 2026-03-18-slice-2-expansion.md
├── architecture/                     # Historical architecture specs (see superseded banners)
│   ├── SERVICE_IMPLEMENTATION_SPEC_v0.1.md
│   └── *.md (homeostasis, routing, brainstem — mostly historical)
├── architecture_decisions/           # ADRs
│   └── ADR-*.md (ADR-0017 superseded by Redesign v2)
└── research/                         # Research notes
    └── *.md
```

---

## Contact & Escalation

- **Linear Team**: FrenchForest
- **Configuration**: Use `.env` file (based on `.env.example`)
- **Issues**: Report via Linear with clear reproduction steps
- **Questions**: Review relevant docs first, then check ADRs for design rationale

---

## Final Checklist for Claude

Before submitting work, verify:

- [ ] Issue is Approved (for implementation tasks)
- [ ] All type hints present on public APIs
- [ ] Google-style docstrings for all public functions/classes
- [ ] No `print()`, `os.getenv()`, or bare `except:` clauses
- [ ] Structured logging includes `trace_id`
- [ ] All tests pass: `uv run pytest`
- [ ] Type checking passes: `uv run mypy src/`
- [ ] Linting passes: `uv run ruff check src/`
- [ ] Code formatted: `uv run ruff format src/`
- [ ] Files placed according to file organization rules
- [ ] ADRs/specs linked in commit message and PR
- [ ] No breaking changes without communication
- [ ] Ready for code review

---

*This document is the source of truth for Claude's behavior in this workspace. Update it when workspace policies change.*
