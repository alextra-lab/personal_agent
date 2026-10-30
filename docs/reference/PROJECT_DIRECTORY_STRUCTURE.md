# Project Directory Structure — Personal Agent

> **Purpose**: Canonical reference for project organization and filesystem hygiene  
> **Last Updated**: 2026-10-30 (FRE-1707: tree, directory list and `.gitignore` excerpt rebuilt from `git ls-files` on `main`)  
> **Status**: Living document — regenerate the tree below when layout changes materially

---

## Directory tree (tracked files only)

The tree shows the directories that git tracks. Runtime directories that git ignores (`.venv/`, the caches, `telemetry/logs/`) are not in the tree. Superseded v0.1 specs and old material live in **`docs/archive/`** (see `docs/archive/PRE_REDESIGN_SUMMARY.md`). The root-file policy is in `docs/reference/ROOT_LEVEL_POLICY.md`.

```
personal_agent/
├── .ast-grep/                 # ast-grep rule files (sgconfig.yml loads .ast-grep/rules/)
├── .claude/                   # Claude Code project config: CLAUDE.md, settings.json, hooks/, skills/, agents/
├── .cursor/                   # Cursor rules and hooks
├── .github/                   # PR template and CI workflows
├── config/                    # Tracked runtime config: models.yaml, model_roles.yaml, governance/, grafana/, otel/, …
├── docker/                    # Service assets: postgres (init.sql + migrations/), elasticsearch templates, searxng, tempo, sandbox, mcp
├── docs/
│   ├── architecture/          # Conceptual docs (HOMEOSTASIS_MODEL, …); README points to the Redesign v2 spec
│   ├── architecture_decisions/  # ADR-XXXX files, HYPOTHESIS_LOG, experiments/
│   ├── archive/               # Superseded v0.1 and router-era material (not current reading)
│   ├── external-skills/       # SKILL.md files for external agents (seshat-knowledge, -observations, -sessions)
│   ├── guides/                # How-tos (CONFIGURATION, MCP, …)
│   ├── internal/              # Internal development notes
│   ├── plans/                 # OWNER_CONSOLE, LAST_SESSION, completed/, templates/, sessions/ (dormant)
│   ├── postmortems/           # Incident post-mortems
│   ├── reference/             # Standards and references (this file, CODING_STANDARDS, PATH_PRIVACY, …)
│   ├── research/              # Dated research and eval write-ups
│   ├── runbooks/              # Operational runbooks
│   ├── specs/                 # COGNITIVE_ARCHITECTURE_REDESIGN_v2, SESHAT_PROGRAM_ARCHITECTURE, …
│   ├── superpowers/           # plans/ (implementation plans, canonical location) and specs/
│   ├── README.md
│   └── VISION_DOC.md
├── e2e/                       # artifact-lib/ Playwright render harness
├── experiments/               # Standalone experiment code (concurrency_research, dspy_prototype, langextract_evaluation)
├── functional-spec/           # functional_spec_v0.1.md
├── governance/                # README only (enforced policies live in config/governance/)
├── infrastructure/            # VPS scripts/ (deploy, hardening) and systemd/ unit files
├── models/                    # MODEL_STRATEGY.md
├── scripts/                   # Operational scripts: dispatch/, eval/, study/, audit/, monitors/, check_*.py guards, migrate_*.py
├── seshat-pwa/                # The PWA web client (Next.js)
├── skills/                    # Agent skills: skills/<name>/SKILL.md (ADR-0156) and SKILL_TEMPLATE.md
├── src/personal_agent/        # Application package (orchestrator, request_gateway, memory, llm_client, tools, …)
├── telemetry/                 # Runtime output; git tracks only TELEMETRY_OVERVIEW.md, captains_log/README.md and named evaluation/ baselines
├── tests/                     # Test suite; new tests mirror the package under tests/personal_agent/
├── tools/                     # TOOLS_OVERVIEW.md and an SLM-server test script
├── CLAUDE.md
├── Dockerfile.gateway, Dockerfile.pwa, Dockerfile.filebeat
├── docker-compose.yml, .cloud.yml, .eval.yml, .study.yml, .test.yml
├── Makefile
├── pyproject.toml
├── uv.lock
├── README.md
└── LICENSE
```

### Top-level directories (tracked)

Command: `git ls-files | cut -d/ -f1 | sort -u`, directories only.

```
.ast-grep
.claude
.cursor
.github
config
docker
docs
e2e
experiments
functional-spec
governance
infrastructure
models
scripts
seshat-pwa
skills
src
telemetry
tests
tools
```

`tests/` holds two layouts. New tests go under `tests/personal_agent/<module>/`, which mirrors `src/personal_agent/`. The older `tests/test_<module>/` directories still exist and still run.

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

- **Location**: `telemetry/captains_log/`. These are runtime files. Git ignores them (`telemetry/captains_log/**/*.json`).
- **Pattern**: `CL-YYYYMMDD-HHMMSS-<id>-NNN-<slug>.json`
- **Example**: `CL-20260101-120000-0a1b2c3d-001-example-task.json` (an illustration, not a real entry)

### Session Logs

- **Status**: Dormant. Do not write new session logs (see `.claude/CLAUDE.md`). Session decisions go in `docs/plans/LAST_SESSION.md` and on the Linear ticket.
- **Old pattern**: `SESSION-YYYY-MM-DD-description.md` in `docs/plans/sessions/`

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

Git tracks the config YAML files in `config/`. Real secret values stay out of the repo. A tracked `.example` file carries the placeholders (`.env.example`, `config/governance/budget.yaml.example`, `docker/searxng/settings.yml.example`). Git does not ignore `telemetry/` as a whole. It ignores the runtime subdirectories and the raw eval run outputs, and it tracks the named evaluation baselines.

The block below is an **excerpt** of the real `.gitignore` lines. Read `.gitignore` for the full list.

```gitignore
# Logs
telemetry/archive/
telemetry/logs/
*.log
*.jsonl

# Captain's Log entries (runtime artifacts, not source code)
telemetry/captains_log/**/*.json

# Python cache
__pycache__/
*.py[cod]

# Testing & type checking cache
.pytest_cache/
.mypy_cache/
.ruff_cache/

# Environment variables (secrets, local configuration)
.env
.env.*
!.env.example

# Real operational cost caps — owner ruling 2026-08-09 (FRE-1209): real config
# values do not belong in the public repo. budget.yaml.example is the template.
config/governance/budget.yaml

docker/searxng/settings.yml

# Claude Code
.claude/worktrees/

.DS_Store
```

---

## Maintenance Schedule

### Weekly

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
3. **Review `docs/plans/OWNER_CONSOLE.md`** for the owner's standing directives
4. **Scan `docs/architecture_decisions/`** for key decisions
5. **Read `docs/plans/LAST_SESSION.md`** to see what the last session decided

---

**Last validated**: 2026-10-30  
**Next review**: When repository layout changes or after major doc migrations
