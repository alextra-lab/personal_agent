# FRE-1329 Acceptance Criteria Verification Guide

This document provides exact steps for master to verify each AC. The build seat cannot run deploy commands (`ENV=cloud make rebuild`), so verification is left for master.

## Dockerfile Audit (AC-3)

**Finding:** All Dockerfiles checked. Only `Dockerfile.pwa` has the vulnerability.

| File | Context | Risk | Status |
|------|---------|------|--------|
| `Dockerfile.pwa` | `.` (root) | **VULNERABLE** — Line 23 `COPY seshat-pwa/` overwrites line 22's clean node_modules | Requires fix |
| `Dockerfile.gateway` | `.` (root) | Safe — Copies only source, uses `uv sync` | No change needed |
| `Dockerfile.filebeat` | `.` (root) | Safe — Config file only | No change needed |
| `docker/sandbox/Dockerfile.python` | `docker/sandbox/` (subdirectory) | Safe — Uses `pip install` in image | No change needed |

**Additional compose files checked:**
- `docker-compose.cloud.yml` — three root-context builds (gateway, PWA, Filebeat)
- `docker-compose.eval.yml` — also builds gateway with root context

## AC-1: Seeded Negative Test (Shadow Cannot Recur)

**Objective:** Verify `.dockerignore` prevents host's stale `next` from entering the image.

**Setup:**
```bash
cd /opt/seshat
mkdir -p seshat-pwa/node_modules/next
cat > seshat-pwa/node_modules/next/package.json << 'EOF'
{
  "name": "next",
  "version": "15.5.18"
}
EOF
```

**Build & inspect:**
```bash
ENV=cloud make rebuild SERVICE=seshat-pwa
docker run --rm seshat-pwa:latest \
  sh -c "cat node_modules/next/package.json | grep version"
```

**Expected output:**
```
"version": "15.5.24"
```

**Why it matters:** The image must report `15.5.24` (from lockfile in this commit), NOT `15.5.18` (the stale seed). If the stale version appears, `.dockerignore` is not working.

**Cleanup:**
```bash
rm -rf /opt/seshat/seshat-pwa/node_modules/next
```

## AC-2: Build Context Shrinkage

**Objective:** Measure that build context actually excludes ignored files.

**Build WITH `.dockerignore`:**
```bash
cd /opt/seshat
ENV=cloud make rebuild SERVICE=seshat-pwa 2>&1 | grep "transferring context"
```

**Expected:** Line like `#5 [internal] load build context / #5 transferring context: 7.67kB 0.1s done`

**Record:** The exact byte count (e.g., `7.67kB`).

## AC-4: PWA Functional Test (No Regression)

**Objective:** PWA still builds, serves, and has correct cache version.

**Verify build succeeded:**
```bash
docker inspect seshat-pwa:latest > /dev/null && echo "Image exists"
```

**Verify container health:**
```bash
docker-compose -f docker-compose.cloud.yml up -d seshat-pwa
sleep 5
docker ps | grep seshat-pwa  # Should show healthy after grace period
```

**Verify HTTP 200 on root:**
```bash
curl -s -o /dev/null -w "%{http_code}" http://localhost:3000/
# Expected: 200
```

**Verify CACHE_NAME (the real test):**
```bash
curl -s http://localhost:3000/sw.js | grep "const CACHE_NAME" | head -1
# Expected: CACHE_NAME = "seshat-v53-dependabot-security-floors" (or current from public/sw.js line 13)
```

**Cleanup:**
```bash
docker-compose -f docker-compose.cloud.yml down seshat-pwa
```

## Summary for Handoff

- **AC-1 (seeded negative):** Image has lockfile version despite stale host copy
- **AC-2 (context shrinkage):** `transferring context:` output shows bytes transferred
- **AC-3 (Dockerfile audit):** All Dockerfiles enumerated, PWA flagged, others documented as safe
- **AC-4 (functional test):** HTTP 200, CACHE_NAME present and correct

**Codex review:** Completed on commit `ec5df973`. Key amendments made: added `.mypy_cache`, `.ruff_cache`, `test-results`, and environment files to ignore list.
