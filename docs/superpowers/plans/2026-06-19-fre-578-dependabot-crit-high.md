# FRE-578 — Dependabot Remediation: 1 critical + 7 high (Python + PWA)
Date: 2026-06-19  
Branch strategy: two feature branches from `origin/main` (NOT from worktree-build), two PRs

---

## Inventory: current → required

| Sev | Package | Ecosystem | Current | Required | Advisory |
|-----|---------|-----------|---------|----------|---------|
| critical | litellm | pip | 1.83.14 | ≥1.84.0 | Auth bypass via host-header injection |
| high | pyjwt | pip | 2.12.0 | ≥2.13.0 | Public-key JWK accepted as HMAC secret → forged HS256 |
| high | starlette | pip | 0.52.1 | ≥1.3.1 ⚠️ MAJOR | SSRF+NTLM via StaticFiles; form() limits ignored |
| high | cryptography | pip | 46.0.7 | ≥48.0.1 | Vulnerable OpenSSL in wheels |
| high | python-multipart | pip | 0.0.28 | ≥0.0.30 | Quadratic-time DoS via semicolon querystring |
| high | undici | npm | 7.26.0 | ≥7.28.0 | TLS cert validation bypass |
| high | vite | npm | 8.0.14 | ≥8.0.16 | server.fs.deny bypass on Windows alternate paths |

**Starlette risk (MAJOR jump):** Codex confirms FastAPI 0.135.1's uv.lock entry has NO `starlette<1.0` upper bound — resolver will not block the bump. Real risk is API-level regression; `make test` is the gate. Escalate to Opus if the starlette bump introduces test failures beyond a trivial import/alias fix.

---

## PR A — Python tranche

### A0 — Baseline green (confirm before touching anything)
```bash
cd /opt/seshat/.claude/worktrees/build && make test
```
Expected: all pass. If already red, stop and note the pre-existing failure — do NOT mask it.

### A1 — Create feature branch from `origin/main`
```bash
# branch FROM origin/main, not from worktree-build (avoids carrying unrelated commits)
git fetch origin
git checkout -b fre-578-python-deps origin/main
```

### A2 — Edit `pyproject.toml`

**[project.dependencies]** — bump direct pins:
- `litellm>=1.83.7` → `litellm>=1.84.0`
- `python-multipart>=0.0.26` → `python-multipart>=0.0.30`

**[tool.uv] override-dependencies** — bump overrides, add new ones:
- `cryptography>=46.0.7` → `cryptography>=48.0.1`
- `python-multipart>=0.0.27` → `python-multipart>=0.0.30`
- ADD: `"pyjwt>=2.13.0"`
- ADD: `"starlette>=1.3.1"`

(Comment each new/changed line: `# FRE-578 Dependabot — <one-line advisory summary>`)

### A3 — Resolve lockfile
```bash
uv sync 2>&1
```
Watch for:
- `starlette` conflict → if FastAPI pins `<1.0`, bump FastAPI in [project.dependencies] too, re-run
- Any other transitive conflict → surface to owner, do NOT force

Verify resolved versions:
```bash
grep -E "^name = \"(litellm|pyjwt|starlette|cryptography|python-multipart)\"" uv.lock -A 2
```
Expected: all at or above required floor.

Verify lockfile consistency (Dockerfile uses `--frozen`):
```bash
uv sync --frozen 2>&1  # must succeed cleanly
```

### A4 — Security rescan
```bash
uv run pip-audit 2>&1 | grep -E "CRITICAL|HIGH|critical|high" | head -30
```
Expected: zero critical/high findings for the 5 patched packages.

### A5 — Quality gates
```bash
make test && make mypy && make ruff-check && make ruff-format
pre-commit run --all-files
```
All must be green. If `make test` fails due to a starlette/litellm API change, investigate — a minor fixture or import alias may need updating. If >1 source file needs changes beyond `pyproject.toml`/`uv.lock`, note in the PR and reassess risk.

### A6 — Commit + push
```bash
git add pyproject.toml uv.lock
git commit -m "chore(security): bump Python deps for Dependabot crit+high (FRE-578)

- litellm 1.83.14 → ≥1.84.0 (auth bypass via host-header injection)
- pyjwt 2.12.0 → ≥2.13.0 (JWK-as-HMAC forged-token bypass)
- starlette 0.52.1 → ≥1.3.1 (SSRF via StaticFiles + form() limit bypass)
- cryptography 46.0.7 → ≥48.0.1 (vulnerable OpenSSL in wheels)
- python-multipart 0.0.28 → ≥0.0.30 (quadratic-time DoS via semicolon)"

git push origin fre-578-python-deps
```

### A7 — Open PR A (targeting main)
Use `.github/PULL_REQUEST_TEMPLATE.md`. Pre-merge items only.

---

## PR B — PWA tranche

### B0 — Create feature branch from `origin/main`
```bash
git fetch origin
git checkout -b fre-578-pwa-deps origin/main
```

### B1 — Baseline: confirm PWA tests pass
```bash
cd /opt/seshat/.claude/worktrees/build/seshat-pwa && npm test -- --run 2>&1
```

### B2 — Bump undici + vite
```bash
npm install undici@^7.28.0
npm install vite@^8.0.16 --save-dev
```
(package.json range is already `^8.0.0` for vite and `^7.25.0` for undici — so this is a within-range patch update)

### B3 — Verify resolved versions
```bash
python3 -c "
import json
with open('package-lock.json') as f:
    pl = json.load(f)
for k in ['undici', 'vite']:
    pkg = pl.get('packages', {}).get(f'node_modules/{k}', {})
    print(f'{k}: {pkg.get(\"version\", \"NOT FOUND\")}')
"
```
Expected: undici ≥7.28.0, vite ≥8.0.16.

### B4 — Security rescan
```bash
npm audit --audit-level high 2>&1
```
Expected: zero high/critical findings for undici/vite.

### B5 — PWA tests + build smoke
```bash
npm test -- --run
npm run build 2>&1 | tail -20
```
Expected: tests green, build completes without errors.

### B6 — Commit + push
```bash
git add package-lock.json package.json
git commit -m "chore(security): bump PWA deps for Dependabot highs (FRE-578)

- undici 7.26.0 → ≥7.28.0 (TLS cert validation bypass)
- vite 8.0.14 → ≥8.0.16 (server.fs.deny bypass on Windows)"

git push origin fre-578-pwa-deps
```

### B7 — Open PR B (targeting main)
Use `.github/PULL_REQUEST_TEMPLATE.md`. Pre-merge items only.

---

## Follow-up ticket (Step 5)
File under Security project, Needs Approval:
> "Dependabot remediation — 24 moderate + 12 low vulnerabilities (second pass)"

---

## Acceptance Verification (for master, post-deploy)
```bash
# 1. Confirm Dependabot alerts closed (after both PRs merged to main + pushed)
gh api "/repos/alextra-lab/personal_agent/dependabot/alerts?state=open&per_page=50" | python3 -c "
import sys, json
alerts = json.load(sys.stdin)
crit_high = [a for a in alerts if a['security_vulnerability']['severity'] in ('critical','high')]
print(f'Remaining crit+high: {len(crit_high)}')
for a in crit_high:
    print(' -', a['security_vulnerability']['package']['name'], a['security_vulnerability']['severity'])
"
# Expected: 0 remaining

# 2. Combined Docker smoke (before cloud deploy — both images together)
#    Run from /opt/seshat (primary repo): make rebuild SERVICE=seshat-gateway
#    then: make rebuild SERVICE=seshat-pwa
#    then: curl http://localhost:9000/health
# (master arranges this; confirm before prod deploy)

# 3. Health smoke against test substrate
curl -s http://localhost:9000/health | python3 -m json.tool
# Expected: {"status": "ok"} or equivalent
```
