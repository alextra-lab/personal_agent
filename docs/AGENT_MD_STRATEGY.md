# `.agent.md` File Strategy for AI-Assisted Development

> **Purpose**: Guide placement, content, and maintenance of `.agent.md` context files for AI coding assistants
> **Audience**: Project owner, AI assistants, future contributors
> **Version**: 1.0
> **Date**: 2025-12-29

---

## What Are `.agent.md` Files?

`.agent.md` files are **context markers** that help AI coding assistants understand:

- **What this directory/component does**
- **Key architectural decisions relevant here**
- **Important conventions and constraints**
- **Common tasks and where to find things**

Think of them as "you are here" markers in a map, optimized for AI comprehension.

---

## Philosophy

### Goals

- ✅ **Reduce context switching**: AI doesn't need to search entire codebase
- ✅ **Encode local knowledge**: Directory-specific patterns and conventions
- ✅ **Link to authoritative docs**: Point to specs/ADRs, don't duplicate them
- ✅ **Accelerate onboarding**: New AI assistants understand faster

### Non-Goals

- ❌ **Not duplicating documentation**: Link to `architecture/` and `architecture_decisions/`
- ❌ **Not a README substitute**: README is for humans, `.agent.md` is for AI
- ❌ **Not comprehensive**: Brief pointers, not full explanations
- ❌ **Not static**: Update as component evolves

---

## Placement Strategy

### Where to Put `.agent.md` Files

```
personal_agent/
├── .agent.md                           # 🎯 Root: Project overview, high-level context
├── architecture/
│   └── .agent.md                       # Architecture docs navigation
├── architecture_decisions/
│   └── .agent.md                       # ADR overview, governance pointers
├── src/personal_agent/
│   ├── .agent.md                       # Codebase overview, package structure
│   ├── orchestrator/
│   │   └── .agent.md                   # 🎯 Orchestrator component context
│   ├── telemetry/
│   │   └── .agent.md                   # 🎯 Telemetry component context
│   ├── governance/
│   │   └── .agent.md                   # 🎯 Governance component context
│   └── tools/
│       └── .agent.md                   # 🎯 Tools component context
├── tests/
│   └── .agent.md                       # Test strategy, fixtures, conventions
└── docs/
    └── .agent.md                       # Documentation overview

```

### Placement Rules

1. **Project root**: Always have one (project-wide context)
2. **Component roots**: Major subsystems (`orchestrator/`, `telemetry/`, etc.)
3. **Special-purpose directories**: `tests/`, `architecture_decisions/`
4. **Skip simple directories**: Don't need `.agent.md` for every subdirectory
5. **Skip documentation-only directories**: Architecture/docs have their own READMEs

**Rule of thumb**: If a directory has >3 modules or complex interactions, consider `.agent.md`.

---

## Content Template

### Root `.agent.md` (Project-Wide)

```markdown
# Personal Agent — AI Assistant Context

## Project Overview
A locally-sovereign AI collaborator with biologically-inspired architecture.

**Status**: Pre-implementation (extensive design docs, no code yet)
**Philosophy**: Homeostasis, determinism, observability, local-first

## Key Architectural Principles
1. **Homeostasis First**: Control loops for all important behavior
2. **Deterministic Orchestration**: Explicit state machines
3. **Observability by Default**: Structured logging everywhere
4. **Mode-Aware**: All behavior respects operational modes

See: `architecture/HOMEOSTASIS_MODEL.md` for control loop architecture.

## Tech Stack
- **Language**: Python 3.12+
- **LLM**: Local Qwen-based models (via LM Studio)
- **Orchestration**: LangGraph-style state machine
- **Logging**: structlog with JSON output
- **Testing**: pytest, mypy (strict), ruff

## Critical Documents
- `docs/VISION_DOC.md` — Philosophical foundation
- `docs/CODING_STANDARDS.md` — Python style guide
- `architecture/system_architecture_v0.1.md` — Technical design
- `architecture_decisions/` — ADRs and governance

## Component Structure
```

src/personal_agent/
├── orchestrator/   # State machine execution engine
├── telemetry/      # Observability infrastructure
├── governance/     # Policy enforcement layer
├── llm_client/     # Model abstraction
├── tools/          # Tool execution layer
└── brainstem/      # Autonomic control (modes)

```

## Before Making Changes
1. Read relevant spec in `architecture/`
2. Check ADRs in `architecture_decisions/`
3. Review `CODING_STANDARDS.md` and `TESTING_STRATEGY.md`
4. Validate with `VALIDATION_CHECKLIST.md`

## Common Tasks
- **Run tests**: `./run_tests.sh --cov=app --cov-report=term-missing`
- **Lint/format**: `ruff check src/ && ruff format src/`
- **Type check**: `mypy src/personal_agent`
- **Start agent**: (TBD — CLI not yet implemented)

## Questions? Uncertainties?
- Ask project owner (don't guess)
- Check `.cursorrules` for collaboration model
- Reference `docs/PR_REVIEW_RUBRIC.md` for quality criteria
```

### Component `.agent.md` (e.g., `src/personal_agent/orchestrator/.agent.md`)

```markdown
# Orchestrator Component — AI Assistant Context

## Purpose
Deterministic state machine for task execution. Think "nervous system" of the agent.

**Spec**: `architecture/ORCHESTRATOR_CORE_SPEC_v0.1.md`
**ADR**: `architecture_decisions/ADR-0006-orchestrator-runtime-structure.md`

## Key Responsibilities
1. Execute task graph (channels → state transitions)
2. Invoke cognitive modules (planner, critic, tool executor)
3. Maintain session state and history
4. Coordinate with governance layer for permissions
5. Emit structured telemetry for all transitions

## Architecture Constraints
- **Deterministic**: Same inputs → same state transitions
- **Observable**: Every transition logged with trace_id
- **Mode-aware**: Check mode before high-risk operations
- **Stateless nodes**: Nodes read state, return updates, don't mutate

## Key Types
- `TaskGraph`: Defines channels and edges
- `State`: Session state with history
- `TraceContext`: Trace and span IDs
- `ExecutionConfig`: Runtime configuration

## Module Structure
```

orchestrator/
├── **init**.py
├── executor.py      # Main execution loop
├── channels.py      # Channel definitions
├── session.py       # Session management
└── types.py         # Pydantic models

```

## Dependencies
- `governance`: For mode checks and tool permissions
- `telemetry`: For structured logging and tracing
- `llm_client`: For LLM calls in cognitive nodes
- `tools`: For tool execution

## Testing Strategy
- **Unit tests**: Mock LLM/tool calls, test state transitions
- **Integration tests**: Full flow with recorded LLM responses
- **Property tests**: State machine invariants (no invalid transitions)

## Common Patterns
- **Node signature**: `async def node(state: State, config: Config) -> StateUpdate`
- **Error handling**: Catch, log, transition to error channel
- **Tracing**: Always pass TraceContext through call chain

## Anti-Patterns
- ❌ Mutable global state
- ❌ Direct LLM calls (use llm_client abstraction)
- ❌ Tool execution without governance check
- ❌ Transitions without telemetry

## Before Modifying
1. Read `ORCHESTRATOR_CORE_SPEC_v0.1.md`
2. Check current tests in `tests/test_orchestrator/`
3. Understand mode interactions (see `HOMEOSTASIS_MODEL.md`)
4. Review error handling strategy in spec

## Related Components
- `governance/`: Provides permissions and mode state
- `brainstem/`: Triggers mode transitions based on sensors
- `telemetry/`: Receives all orchestrator events
```

---

## Content Guidelines

### Do Include

- ✅ **Purpose summary** (1-2 sentences)
- ✅ **Key responsibilities** (bullet list)
- ✅ **Links to authoritative docs** (specs, ADRs)
- ✅ **Architecture constraints** (non-negotiable patterns)
- ✅ **Module structure** (file layout)
- ✅ **Dependencies** (what this relies on)
- ✅ **Common patterns** (code examples)
- ✅ **Anti-patterns** (what not to do)
- ✅ **Testing strategy** (how to test this component)

### Don't Include

- ❌ **Full documentation** (link to specs instead)
- ❌ **Implementation details** (code is the source of truth)
- ❌ **Duplicated content** (from other docs)
- ❌ **Personal information** (use "project owner", not names)
- ❌ **Outdated info** (must be kept current)

### Tone & Style

- **Concise**: Bullet points over paragraphs
- **Specific**: Exact file paths, class names, ADR numbers
- **Actionable**: Tell AI what to do/check before modifying
- **Authoritative**: Point to specs, don't speculate

---

## Maintenance

### When to Update

- ✅ **New component added**: Create `.agent.md` for it
- ✅ **Major refactor**: Update affected `.agent.md` files
- ✅ **ADR changes behavior**: Reflect in relevant component `.agent.md`
- ✅ **New patterns emerge**: Document in `.agent.md`

### Update Workflow

1. **Change happens**: New feature, refactor, ADR
2. **Identify affected components**: Which `.agent.md` files need updates?
3. **Update content**: Add/modify sections
4. **Validate links**: Ensure references to specs/ADRs are current
5. **Commit with code**: Keep `.agent.md` in sync with implementation

### Staleness Detection

- **Manual**: Review `.agent.md` files monthly
- **Automated** (future): Script to check if referenced ADRs/specs have changed
- **Session logs**: Note `.agent.md` updates in session summaries

---

## Benefits of This Approach

### For AI Assistants

- **Faster context loading**: Don't need to read entire codebase
- **Better accuracy**: Understand constraints before implementing
- **Fewer mistakes**: Anti-patterns are explicit
- **More autonomy**: Can explore codebase with local maps

### For Project Owner

- **Less repetition**: Don't re-explain component structure each session
- **Consistency**: AI follows same patterns across sessions
- **Onboarding speed**: New AI assistants productive faster
- **Quality guard**: Constraints and anti-patterns are documented

### For Future Contributors

- **Clear entry points**: Know what to read for each component
- **Local context**: Don't need to understand entire system to modify one part
- **Pattern library**: Learn project conventions from examples

---

## Comparison with Other Documentation

| Document Type | Purpose | Audience | Detail Level |
|---------------|---------|----------|--------------|
| **README.md** | Project overview, getting started | Humans (new users) | High-level |
| **`.agent.md`** | Component context for AI assistants | AI assistants | Medium (pointers) |
| **Architecture specs** | Detailed design, interfaces | Implementers | Very detailed |
| **ADRs** | Decision rationale, alternatives | Reviewers, future devs | Justification-focused |
| **Docstrings** | API documentation | Code readers | API-level |

**`.agent.md` is the bridge** between high-level architecture and low-level code.

---

## Example: Minimal `.agent.md` for Simple Directory

For simpler directories, keep it brief:

```markdown
# Tools Component — AI Assistant Context

## Purpose
Tool execution layer with sandboxing and governance integration.

**Spec**: `architecture/TOOL_EXECUTION_VALIDATION_SPEC_v0.1.md`

## Key Files
- `executor.py`: Main ToolExecutionLayer
- `registry.py`: Tool registration and discovery
- `filesystem.py`, `web.py`, `system_health.py`: Tool implementations

## Testing
- Mock tool calls in unit tests
- Use fixtures for tool results
- See `tests/test_tools/` for examples

## Before Adding Tools
1. Read tool execution spec
2. Register in `registry.py`
3. Add governance permission entry in `config/governance/tools.yaml`
4. Write tests in `tests/test_tools/`
```

**Short and sweet**: No need for verbose explanations if component is straightforward.

---

## Advanced: Conditional Context

For components with mode-specific behavior:

```markdown
## Mode-Specific Behavior

- **NORMAL**: All tools available
- **ALERT**: Some tools require human approval
- **DEGRADED**: Reduced toolset (read-only preferred)
- **LOCKDOWN**: Tools blocked except system health checks
- **RECOVERY**: Gradual re-enablement based on self-checks

See: `config/governance/tools.yaml` for permissions matrix.
```

For experimental features:

```markdown
## Experimental Features (Phase 2+)

- **Self-learning**: Agent proposes tool enhancements (not yet implemented)
- **Parallel execution**: Concurrent tool calls (under evaluation)

Check `architecture_decisions/experiments/` for active experiments.
```

---

## Anti-Patterns in `.agent.md` Files

### 1. Copy-Pasting Specs

**Bad**:

```markdown
## Orchestrator Design
[Full copy of ORCHESTRATOR_CORE_SPEC_v0.1.md]
```

**Good**:

```markdown
## Orchestrator Design
See: `architecture/ORCHESTRATOR_CORE_SPEC_v0.1.md` for detailed design.

Key points:
- Deterministic state machine
- Nodes are stateless functions
- Tracing via TraceContext
```

### 2. Outdated Information

**Bad**:

```markdown
## Testing
We use unittest (no longer true after switching to pytest)
```

**Good**:
Keep `.agent.md` in sync with code. If you change testing framework, update `.agent.md`.

### 3. Too Much Detail

**Bad**:

```markdown
## executor.py Implementation
[Line-by-line explanation of code]
```

**Good**:
Code should be self-documenting. `.agent.md` provides context, not implementation guide.

### 4. Personal Information

**Bad**:

```markdown
## Contact
Ask Alex if you have questions about the orchestrator.
```

**Good**:

```markdown
## Questions
Check spec first, then ask project owner if unclear.
```

---

## Rollout Plan

### Week 1 (Immediate)

- [x] Create root `.agent.md` (project overview)
- [ ] Create `src/personal_agent/.agent.md` (codebase overview)
- [ ] Create `tests/.agent.md` (testing overview)

### Week 2 (During Component Implementation)

- [ ] Create component `.agent.md` files as components are built:
  - `src/personal_agent/telemetry/.agent.md`
  - `src/personal_agent/governance/.agent.md`
  - `src/personal_agent/orchestrator/.agent.md`

### Week 3+ (As Needed)

- [ ] Create `.agent.md` for `llm_client/`, `tools/`, `brainstem/`
- [ ] Refine based on what AI assistants actually need
- [ ] Add advanced sections (mode-specific, experimental)

### Evaluation

- After each session, note:
  - Did `.agent.md` help AI assistant orient faster?
  - What questions did AI ask that `.agent.md` should answer?
  - What content was unused (remove)?

**Iterate based on actual usage.**

---

## Tools & Automation (Future)

### Staleness Detection Script

```bash
#!/usr/bin/env bash
# tools/check_agent_md_freshness.sh
# Check if referenced ADRs/specs have changed since .agent.md was last updated

# For each .agent.md file:
#   - Extract referenced files (regex for markdown links)
#   - Compare git timestamps
#   - Warn if referenced file is newer than .agent.md
```

### Linter for `.agent.md` (Future)

- Validate links to specs/ADRs
- Check for personal info
- Ensure required sections present
- Flag copy-pasted content (>50% similarity to source docs)

---

## Summary

### Quick Reference

| Question | Answer |
|----------|--------|
| **When to create?** | For project root, component roots, special-purpose directories |
| **What to include?** | Purpose, responsibilities, links to specs, patterns, anti-patterns |
| **What to avoid?** | Duplication, personal info, outdated content, excessive detail |
| **When to update?** | New component, major refactor, ADR changes, new patterns |
| **How to maintain?** | Commit with code changes, review monthly, validate links |

### Success Metrics

- ✅ AI assistants orient faster in new components
- ✅ Fewer "where do I find X?" questions
- ✅ Consistent patterns across sessions
- ✅ Reduced onboarding time for new AI assistants

---

**`.agent.md` files are living maps of your codebase, optimized for AI comprehension. Keep them brief, current, and action-oriented.**

---

## Document History

| Version | Date | Changes |
|---------|------|---------|
| 1.0 | 2025-12-29 | Initial .agent.md strategy document |
