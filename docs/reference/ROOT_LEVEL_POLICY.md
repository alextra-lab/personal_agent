# Root Level File Policy

> **Purpose**: Define what files are allowed at project root to prevent clutter
> **Philosophy**: Root should contain only essential project metadata and entry points

---

## ✅ Allowed at Root

### Project Configuration
- `pyproject.toml` — Python project configuration
- `uv.lock` — Dependency lock file
- `.gitignore` — Git exclusions
- `.pre-commit-config.yaml` — Git hook definitions (path privacy and other checks)
- `.python-version` — Python version specification (if needed)

### Essential Documentation
- `README.md` — Project overview and quickstart
- `ROADMAP.md` — High-level timeline and milestones

### Directories
- `config/` — Runtime configuration
- `docs/` — All documentation (architecture, ADRs, plans, research)
- `functional-spec/` — Product requirements
- `governance/` — Governance framework (if not consolidated)
- `models/` — Model strategy
- `src/` — Source code
- `telemetry/` — Runtime observability data (gitignored)
- `tests/` — Test suite
- `tools/` — Development/operational tools

---

## ❌ Not Allowed at Root

### Planning & Session Files
- ❌ Session logs → `docs/plans/sessions/`
- ❌ Action items → `docs/plans/`
- ❌ Implementation roadmaps → `docs/plans/`
- ❌ Velocity tracking → `docs/plans/`

### Process & Quality Documentation
- ❌ Validation checklists → `docs/`
- ❌ PR review rubrics → `docs/`
- ❌ Vision documents → `docs/`
- ❌ Directory structure docs → `docs/`

### Generated/Runtime Files
- ❌ Telemetry logs → `telemetry/logs/`
- ❌ Session state → `telemetry/sessions/`
- ❌ Compiled Python → `__pycache__/` (gitignored)

### Temporary Files
- ❌ `TODO.md`, `NOTES.md` → `docs/`
- ❌ Scratch files → Delete or move to appropriate location

---

## 📋 Root Level Audit Checklist

Run this periodically to ensure root stays clean:

```bash
# List all files at root (excluding directories)
ls -1 | grep -v '/$'

# Expected output should only show:
# - pyproject.toml
# - uv.lock
# - .gitignore
# - README.md
# - ROADMAP.md
# - (possibly .python-version)
```

If other files appear, move them to appropriate locations:
- Planning → `docs/plans/`
- Documentation → `docs/`
- Configuration → `config/`
- Source code → `src/`
- Tests → `tests/`

---

## 🎯 Rationale

### Why Keep Root Minimal?

1. **Clarity**: New contributors see essential info first
2. **Organization**: Forces discipline in file placement
3. **Scalability**: Prevents "junk drawer" accumulation
4. **Standards**: Matches professional project conventions
5. **Navigation**: Easier to find things when they're in logical places

### Why Allow README & ROADMAP at Root?

- **README.md**: Universal entry point, expected by GitHub/GitLab
- **ROADMAP.md**: High-level orientation, frequently referenced

These are the "front door" of the project.

---

## 🔍 What If I'm Unsure?

Ask these questions:

1. **Is it essential for project setup?** (config files) → Root
2. **Is it the first thing newcomers need?** (README) → Root
3. **Is it process/quality documentation?** → `docs/`
4. **Is it planning/tracking?** → `docs/plans/`
5. **Is it architecture/design?** → `docs/architecture/` or `docs/architecture_decisions/`
6. **Is it code?** → `src/`
7. **Is it research?** → `docs/research/`

**When in doubt, NOT at root.**

---

## 🚨 Enforcement

1. **Weekly audit**: Run root level audit checklist
2. **Pre-commit hook** (future): Warn if new .md files added to root
3. **Session logs**: Note any root level file moves in session summary
4. **Validation**: Include root cleanliness in quality reviews

---

**Keep the root clean, keep the project professional.**

---

Last updated: 2025-12-28
