# Project Directory Structure — Personal Agent

> **Purpose**: Canonical reference for project organization and filesystem hygiene  
> **Last Updated**: 2026-03-30  
> **Status**: Living document — regenerate the tree below when layout changes materially

---

## Directory tree (representative)

Superseded v0.1 specs and old session logs live in **`docs/archive/`** (see `PRE_REDESIGN_SUMMARY.md` there). Active docs exclude that folder from “current reading” paths.

```
personal_agent/
├── config/                    # Runtime YAML (gitignored except templates); see config/*.template
├── docker/                    # compose services: postgres, elasticsearch, kibana, searxng, …
├── docs/
│   ├── architecture/          # Living conceptual docs (HOMEOSTASIS_MODEL, …); README points to Redesign v2
│   ├── architecture_decisions/  # ADRs, HYPOTHESIS_LOG, experiments/, captains_log/, …
│   ├── archive/               # Historical v0.1 + router-era material (not primary reading)
│   ├── guides/                # How-tos (CONFIGURATION, MCP, Kibana, …)
│   ├── plans/                 # MASTER_PLAN, sessions/, completed/
│   ├── reference/             # Standards, directory structure (this file), PATH_PRIVACY
│   ├── research/              # Eval reports, research notes, context_management_research.md
│   ├── specs/                 # COGNITIVE_ARCHITECTURE_REDESIGN_v2, CONTEXT_INTELLIGENCE_SPEC, …
│   ├── superpowers/plans/     # Slice / feature implementation plans
│   ├── README.md
│   └── VISION_DOC.md
├── experiments/               # Standalone experiment code (dspy, langextract, …)
├── functional-spec/
├── governance/                # Meta governance README (policies live in config/governance/)
├── src/personal_agent/        # Application package (orchestrator, request_gateway, memory, …)
├── telemetry/                 # Runtime logs / eval output (gitignored)
├── tests/                     # Mirror package layout under tests/personal_agent/
├── pyproject.toml
├── uv.lock
├── README.md
└── ROADMAP.md
```

### Top-level directories (from `find`, excluding .git, .venv, archive)

```
.
./config
./docker
./docs
./experiments
./functional-spec
./governance
./src
./telemetry
./tests
```

---

## Legacy detailed tree (deprecated — kept for naming patterns only)

The block below is **not** maintained line-by-line. Prefer the tree above and **`docs/architecture/README.md`**.

```
personal_agent/
│
├── docs/                             # All documentation
│   └── … (see current tree)
│
├── config/                           # Runtime configuration (not in git except templates)
│   ├── governance/                   # Governance policies (modes, tools, models)
│   ├── kibana/                       # Dashboard ndjson / import helpers
│   └── models.yaml.template          # Model endpoint configuration template
│
├── functional-spec/                  # Product requirements
│   └── functional_spec_v0.1.md       # MVP capabilities and scope
│
├── governance/                       # Governance framework (meta)
│   └── README.md                     # Governance process documentation
│
├── models/                           # Model strategy and evaluation
│   └── MODEL_STRATEGY.md             # Model selection philosophy
│
├── src/                              # Source code (Python package)
│   └── personal_agent/               # Main package
│       ├── __init__.py
│       ├── brainstem/                # Autonomic control
│       │   ├── __init__.py
│       │   ├── mode_manager.py       # Mode state machine
│       │   └── sensors.py            # Sensor polling
│       ├── config/                   # Unified configuration (ADR-0007)
│       │   ├── __init__.py           # Exports: settings, AppConfig
│       │   ├── settings.py           # AppConfig class
│       │   ├── env_loader.py         # .env file loading
│       │   └── validators.py         # Custom Pydantic validators
│       ├── governance/               # Policy enforcement
│       │   ├── __init__.py
│       │   ├── config_loader.py      # Load/validate YAML configs
│       │   └── models.py             # Pydantic schemas
│       ├── llm_client/               # 📝 TO CREATE: Model abstraction
│       │   ├── __init__.py
│       │   ├── adapters.py           # API adapters
│       │   ├── client.py             # LocalLLMClient
│       │   └── types.py              # Response types
│       ├── orchestrator/             # 📝 TO CREATE: Task execution
│       │   ├── __init__.py
│       │   ├── channels.py           # Channel definitions
│       │   ├── executor.py           # Main loop + step functions
│       │   ├── session.py            # Session management
│       │   └── types.py              # State machine types
│       ├── telemetry/                # 📝 TO CREATE: Observability
│       │   ├── __init__.py
│       │   ├── events.py             # Event constants
│       │   ├── logger.py             # structlog config
│       │   ├── metrics.py            # Metric readers
│       │   └── trace.py              # TraceContext
│       ├── tools/                    # 📝 TO CREATE: Tool execution
│       │   ├── __init__.py
│       │   ├── executor.py           # ToolExecutionLayer
│       │   ├── filesystem.py         # File tools
│       │   ├── registry.py           # Tool registry
│       │   ├── system_health.py      # Health check tools
│       │   └── web.py                # Web search
│       └── ui/                       # 📝 TO CREATE: User interface
│           ├── __init__.py
│           ├── approval.py           # Human approval workflow
│           └── cli.py                # Typer-based CLI
│
├── telemetry/                        # Runtime observability data (gitignored)
│   ├── logs/                         # Structured JSONL logs
│   │   └── current.jsonl             # Active log file (rotated)
│   ├── sessions/                     # Persisted session state
│   │   ├── archive/                  # Old sessions
│   │   └── <session_id>.json         # Active session snapshots
│   └── metrics/                      # Optional: Derived metrics cache
│
├── tests/                            # Test suite
│   ├── integration/                  # 📝 TO CREATE: E2E tests
│   │   └── test_e2e_flows.py
│   ├── test_brainstem/               # 📝 TO CREATE: Brainstem tests
│   ├── test_governance/              # 📝 TO CREATE: Governance tests
│   ├── test_llm_client/              # 📝 TO CREATE: LLM client tests
│   ├── test_orchestrator/            # 📝 TO CREATE: Orchestrator tests
│   ├── test_telemetry/               # 📝 TO CREATE: Telemetry tests
│   └── test_tools/                   # 📝 TO CREATE: Tool tests
│
├── tools/                            # Development/operational tools
│   └── TOOLS_OVERVIEW.md             # External tool documentation
│
├── .gitignore                        # Git exclusions
├── pyproject.toml                    # Python project config
├── uv.lock                           # Dependency lock file
├── README.md                         # Project overview
├── ROADMAP.md                        # High-level project roadmap
├── PROJECT_DIRECTORY_STRUCTURE.md    # This file
├── VISION_DOC.md                     # 📝 TO CREATE: Vision for AI assistants
└── VALIDATION_CHECKLIST.md           # 📝 TO CREATE: Doc quality checklist
```

---

## Directory Purpose Validation

### ✅ Validated Directories (Will Definitely Use)

| Directory | Purpose | When Created |
|-----------|---------|--------------|
| `docs/architecture/` | System design specifications | ✅ Exists |
| `docs/architecture_decisions/` | ADRs, governance, experiments | ✅ Exists |
| `docs/architecture_decisions/captains_log/` | Agent self-improvement proposals | 📝 Create Week 4 |
| `config/governance/` | Runtime governance policies | 📝 Create Week 1 |
| `docs/plans/` | Project plans, session logs | ✅ Exists |
| `docs/plans/sessions/` | Development session tracking | ✅ Exists |
| `src/personal_agent/` | Main codebase | ✅ Exists (skeleton) |
| `src/personal_agent/config/` | Unified configuration (ADR-0007) | 📝 Create Week 1 |
| `src/personal_agent/telemetry/` | Observability infrastructure | 📝 Create Week 1 |
| `src/personal_agent/governance/` | Policy loading/enforcement | 📝 Create Week 1 |
| `src/personal_agent/orchestrator/` | Task execution engine | 📝 Create Week 1 |
| `src/personal_agent/llm_client/` | Model abstraction | 📝 Create Week 2 |
| `src/personal_agent/tools/` | Tool execution layer | 📝 Create Week 3 |
| `src/personal_agent/brainstem/` | Autonomic control | 📝 Create Week 2 |
| `src/personal_agent/ui/` | User interface (CLI) | 📝 Create Week 2 |
| `telemetry/logs/` | Structured log storage | 📝 Auto-created runtime |
| `telemetry/sessions/` | Session persistence | 📝 Auto-created runtime |
| `tests/` | Test suite | 📝 Create Week 1 |

### ⚠️ Questionable Directories (May Consolidate)

| Directory | Current Use | Recommendation |
|-----------|-------------|----------------|
| `governance/` | Meta-governance docs | **Merge into `architecture_decisions/`** (avoid duplication) |
| `docs/` | User docs + notes | **Keep but consolidate**: USAGE_GUIDE, API_REFERENCE only |
| `telemetry/metrics/` | Derived metrics cache | **Phase 2+**: Not needed for MVP (query logs directly) |

### ❌ To Remove/Consolidate

| Item | Reason | Action |
|------|--------|--------|
| `governance/README.md` | Meta process vs `config/governance/` policies | Keep short; operational policies live in `config/governance/` (see ADR-0005) |
| Empty stub files in root | No clear purpose | Document or remove |

---

## File Naming Conventions

### Architecture Documents

- **Pattern**: `COMPONENT_NAME_SPEC_v0.X.md`
- **Example**: `ORCHESTRATOR_CORE_SPEC_v0.1.md`
- **Versioning**: Increment minor version on significant changes

### ADRs

- **Pattern**: `ADR-XXXX-short-title.md`
- **Example**: `ADR-0004-telemetry-and-metrics.md`
- **Numbering**: Zero-padded 4 digits, sequential

### Experiments (ADD/HDD)

- **Pattern**: `E-XXX-experiment-name.md`
- **Example**: `E-001-orchestration-evaluation.md`
- **Numbering**: Zero-padded 3 digits

### Captain's Log Entries

- **Pattern**: `CL-YYYY-MM-DD-NNN-title.md`
- **Example**: `CL-2025-12-28-001-threshold-tuning-proposal.md`
- **Numbering**: Date + sequential number per day

### Session Logs

- **Pattern**: `SESSION-YYYY-MM-DD-description.md`
- **Example**: `SESSION-2025-12-28-telemetry-implementation.md`

---

## Filesystem Hygiene Rules

### ✅ Do

1. **Version documents explicitly**: Use `v0.1`, `v0.2` suffixes for specs
2. **Date session logs**: Use ISO date format (YYYY-MM-DD)
3. **One concern per file**: Don't mix specs, ADRs, and plans
4. **Use templates**: Create `TEMPLATE.md` files for recurring structures
5. **Git commit early**: Commit architectural docs separately from code
6. **Archive old versions**: Move superseded docs to `archive/` subdirectories

### ❌ Don't

1. **Don't duplicate**: If content exists elsewhere, link to it
2. **Don't use personal names in content**: Use "project owner", "user" in specs/examples; personal names only acceptable in document authoring metadata or validation records
3. **Don't use literal personal paths**: Use `$HOME`, `<project-root>`, or other neutral placeholders instead of machine-specific absolute paths (see `docs/reference/PATH_PRIVACY.md`)
4. **Don't leave empty stubs**: Either populate or remove
5. **Don't mix generated/manual**: Keep telemetry data separate from docs
6. **Don't hard-code secrets**: Use templates and `.gitignore`
7. **Don't version binary files**: Use external storage or git-lfs

---

## `.gitignore` Strategy

```gitignore
# Runtime data (never commit)
telemetry/
config/*.yaml
!config/*.yaml.template

# Python artifacts
__pycache__/
*.pyc
.pytest_cache/
.mypy_cache/
*.egg-info/

# IDE
.vscode/
.idea/
*.swp

# OS
.DS_Store

# Secrets
*.key
*.pem
.env
```

---

## Directory Creation Checklist (Week 1)

```bash
# Create missing directories
mkdir -p docs/plans/{sprints,sessions}
mkdir -p docs/architecture_decisions/captains_log
mkdir -p config/governance
mkdir -p src/personal_agent/{config,telemetry,governance,orchestrator,llm_client,tools,brainstem,ui}
mkdir -p tests/{integration,test_telemetry,test_governance,test_orchestrator,test_llm_client,test_tools,test_brainstem}

# Create README files for clarity
touch docs/plans/README.md
touch docs/architecture_decisions/captains_log/README.md
touch docs/plans/sessions/SESSION_TEMPLATE.md

# Create gitignore for runtime data
echo "telemetry/" >> .gitignore
echo "config/*.yaml" >> .gitignore
echo "!config/*.yaml.template" >> .gitignore
```

---

## Maintenance Schedule

### Weekly

- Archive old session logs (>30 days)
- Review empty/stub files, populate or remove
- Update this document if structure changes

### Per Sprint/Milestone

- Review directory structure against actual usage
- Consolidate duplicate documentation
- Update `.gitignore` if new runtime data types appear

### Ad-Hoc (When Confused)

- **Ask**: "Should this directory exist?"
- **Check**: Does it serve a single, clear purpose?
- **Validate**: Can I explain it to a new contributor in one sentence?

---

## Reference for New Contributors

When a new AI assistant or developer joins:

1. **Read this document first** to understand project organization
2. **Check `docs/VISION_DOC.md`** for high-level goals
3. **Review `docs/plans/MASTER_PLAN.md`** for current work
4. **Scan `docs/architecture_decisions/`** for key decisions
5. **Look at recent `docs/plans/sessions/`** to see what's happening now

---

**Last validated**: 2026-03-30  
**Next review**: When repository layout changes or after major doc migrations
