# Root Level File Policy

> **Purpose**: Define which files and directories the project root can hold, to prevent clutter
> **Philosophy**: The root holds only project metadata, build files and entry points
> **Source of the lists**: `git ls-files` and `git ls-tree` on `main`, 2026-10-30

---

## ✅ Allowed at Root

### Project configuration

| Entry | Purpose |
|-------|---------|
| `pyproject.toml` | Python project configuration. It also defines the `agent` CLI entry point. |
| `uv.lock` | Dependency lock file |
| `.python-version` | Python version specification |
| `.gitignore` | Git exclusions |
| `.dockerignore` | Docker build context exclusions |
| `.pre-commit-config.yaml` | Git hook definitions (path privacy and other checks) |
| `.markdownlint.jsonc` | Markdown lint rules |
| `.mcp.json` | MCP server configuration for Claude Code |
| `sgconfig.yml` | `ast-grep` configuration. It points to the rules in `.ast-grep/`. |
| `.env.example` | Template for the `.env` file. The `.env` file itself is gitignored. |

### Build and deployment files

| Entry | Purpose |
|-------|---------|
| `Makefile` | The project commands. Run `make help`. |
| `Dockerfile.gateway` | Image of the `seshat-gateway` service |
| `Dockerfile.pwa` | Image of the PWA |
| `Dockerfile.filebeat` | Image of the Filebeat service |
| `docker-compose.yml` | Local stack |
| `docker-compose.cloud.yml` | Production (cloud) stack |
| `docker-compose.eval.yml` | Evaluation stack |
| `docker-compose.study.yml` | Study stack |
| `docker-compose.test.yml` | Test stack |
| `init_personal_agent_structure.sh` | Script that creates the first project directory structure |

### Essential documentation

| Entry | Purpose |
|-------|---------|
| `README.md` | Project overview and quickstart |
| `CLAUDE.md` | Instructions for Claude Code sessions (commands, architecture, policies) |
| `LICENSE` | The license |

### Directories

| Entry | Purpose |
|-------|---------|
| `src/` | Source code (`src/personal_agent/`) |
| `tests/` | Test suite |
| `config/` | Runtime configuration (`models.yaml`, `model_roles.yaml`, `governance/` and others) |
| `docs/` | All documentation (guides, reference, specs, ADRs, plans, research, runbooks) |
| `docker/` | Support files for the compose stacks (Elasticsearch, Postgres and others) |
| `infrastructure/` | Deployment and host scripts |
| `scripts/` | Development and operations scripts |
| `skills/` | Runtime agent skills that the gateway image copies (ADR-0156 A1) |
| `seshat-pwa/` | The PWA client |
| `e2e/` | End-to-end tests |
| `tools/` | Development and operations tools |
| `telemetry/` | Runtime observability data (mostly gitignored; see `.gitignore` for the tracked baseline results and curated summaries) |
| `experiments/` | Research experiment code |
| `functional-spec/` | Product requirements |
| `governance/` | Governance framework notes |
| `models/` | Model strategy |
| `.claude/` | Claude Code settings, skills and hooks |
| `.cursor/` | Cursor hooks |
| `.github/` | CI workflows, the pull request template and the Dependabot configuration |
| `.ast-grep/` | `ast-grep` rules |

The local tools also create gitignored directories at the root. Examples are `.venv/`, `.mypy_cache/`, `.pytest_cache/` and `.ruff_cache/`. These are not part of the policy.

---

## ❌ Not Allowed at Root

### Planning and session files
- ❌ Session logs. Do not write them. See `.claude/CLAUDE.md` section 2.
- ❌ Action items → `docs/plans/`
- ❌ Implementation plans → `docs/superpowers/plans/`
- ❌ Roadmaps and velocity tracking → `docs/plans/`

### Process and quality documentation
- ❌ Validation checklists → `docs/reference/`
- ❌ PR review rubrics → `docs/reference/`
- ❌ Vision documents → `docs/`
- ❌ Directory structure documents → `docs/reference/`

### Generated and runtime files
- ❌ Telemetry logs → `telemetry/logs/`
- ❌ Compiled Python → `__pycache__/` (gitignored)

### Temporary files
- ❌ `TODO.md`, `NOTES.md` → `docs/`
- ❌ Scratch files → Delete them, or move them to the correct location

---

## 📋 Root Level Audit Checklist

Run this check periodically. It lists the tracked entries at the root.

```bash
# Tracked files at the root
git ls-files | grep -v /

# Tracked directories at the root
git ls-tree --name-only -d HEAD
```

Compare the output with the tables above. If another entry appears, move it:
- Planning → `docs/plans/`
- Documentation → `docs/`
- Configuration → `config/`
- Source code → `src/`
- Tests → `tests/`

---

## 🎯 Rationale

### Why keep the root minimal?

1. **Clarity**: New contributors see the essential files first.
2. **Organization**: The rule forces discipline in file placement.
3. **Scalability**: The root does not become a junk drawer.
4. **Standards**: The layout matches common project conventions.
5. **Navigation**: Files are easier to find in logical places.

### Why allow `README.md` and `CLAUDE.md` at the root?

- **README.md**: the universal entry point that GitHub and GitLab expect.
- **CLAUDE.md**: Claude Code loads it from the project root at the start of each session.

---

## 🔍 If you are unsure

Ask these questions:

1. **Is the file essential for project setup or build?** → Root
2. **Is it the first thing newcomers need?** (`README.md`) → Root
3. **Is it process or quality documentation?** → `docs/reference/`
4. **Is it planning or tracking?** → `docs/plans/`
5. **Is it architecture or design?** → `docs/specs/` or `docs/architecture_decisions/`
6. **Is it code?** → `src/`
7. **Is it research?** → `docs/research/`

**When in doubt, do not put the file at the root.**

---

## 🚨 Enforcement

1. **Periodic audit**: Run the root level audit checklist.
2. **Review**: Check new root entries during review of a pull request. No automatic check exists.

---

**Keep the root clean, keep the project professional.**

---

Last updated: 2026-10-30
