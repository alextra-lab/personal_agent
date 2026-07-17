# FRE-580: katex /lib/ toolkit migration 0.16.11 → 0.16.47

Date: 2026-06-19
Ticket: FRE-580 (Approved)
Branch: fre-580-katex-toolkit
ADR: ADR-0089 Addendum A (curated /lib/ toolkit)

## Context

katex must move as an ADR-0089 curated asset — 8 surfaces must stay coherent.
GHSA-cg87-wmx4-v546: `\htmlData` attribute injection, moderate severity.

SRI hashes (computed from jsdelivr CDN bytes, same bytes hosted on our R2):
- katex@0.16.47/katex.min.js:  sha384-CwjPRVHTvLiMBFjEoij+QZViMV5rhTOIp7CJzl24JEqpRDA1sJFHVXXLURktbYYp
- katex@0.16.47/katex.min.css: sha384-nH0MfJ44wi1dd7w6jinlyBgljjS8EJAh2JBoRad8a3VDw2K69vfaaqm4WnR+gXtA

## Steps

### Step 1 — Confirm TDD baseline (tests currently green at 0.16.11)
```bash
make test-k K=test_load_real_substitution_map_shape
```
Expected: PASSED (substitution map has 0.16.11 entries, test checks for 0.16.11)

### Step 2 — config/artifact_lib_manifest.json
Update lines 5-6: `katex@0.16.11` → `katex@0.16.47` in both path values.
File: `config/artifact_lib_manifest.json`

### Step 3 — config/artifact_lib_substitution_map.json
Two entries to update:
  - Entry 1 (katex.min.js): lib_path, public_cdn_url, sri
  - Entry 2 (katex.min.css): lib_path, public_cdn_url, sri
File: `config/artifact_lib_substitution_map.json`

### Step 4 — Confirm TDD failure (real-map test fails after map update)
```bash
make test-k K=test_load_real_substitution_map_shape
```
Expected: FAILED with KeyError: 'lib/katex@0.16.11/katex.min.css'

### Step 5 — src/personal_agent/tools/artifact_tools.py
Lines 990-991: update the two katex URL strings in the system prompt.
File: `src/personal_agent/tools/artifact_tools.py`

### Step 6 — src/personal_agent/observability/artifact_envelope/spec.py
Line 105: update docstring example `katex@0.16.11/katex.min.js` → `katex@0.16.47/katex.min.js`.
File: `src/personal_agent/observability/artifact_envelope/spec.py`

### Step 7 — scripts/build_e2e_artifact_fixtures.py
Lines 65-66: _KATEX_CSS and _KATEX_JS constants → katex@0.16.47.
File: `scripts/build_e2e_artifact_fixtures.py`

### Step 8 — tests/scripts/test_build_e2e_artifact_fixtures.py
Replace all `katex@0.16.11` → `katex@0.16.47` (lines 47, 48, 52, 53, 92, 175).
File: `tests/scripts/test_build_e2e_artifact_fixtures.py`

### Step 9 — tests/personal_agent/storage/test_artifact_export.py
Replace all `katex@0.16.11` → `katex@0.16.47` (lines 122, 124, 129, 132, 145, 147, 150,
163, 165, 168, 209, 211, 213, 223, 230, 231, 328).
File: `tests/personal_agent/storage/test_artifact_export.py`

### Step 10 — tests/observability/artifact_envelope/test_verifier.py
Line 375: STYLE_ASSET path `katex@0.16.11` → `katex@0.16.47`.
File: `tests/observability/artifact_envelope/test_verifier.py`

### Step 11 — seshat-pwa/package.json
`"katex": "0.16.11"` → `"katex": "0.16.47"` (exact pin, no caret).
File: `seshat-pwa/package.json`

### Step 12 — seshat-pwa/src/__tests__/toolkit-convergence.test.ts
Update description string and assertion: `'0.16.11'` → `'0.16.47'`.
File: `seshat-pwa/src/__tests__/toolkit-convergence.test.ts`

### Step 13 — Update seshat-pwa/package-lock.json
```bash
cd seshat-pwa && npm install katex@0.16.47 --save-exact
# Verify no caret was added:
grep '"katex"' package.json
# Must show: "katex": "0.16.47"  (not "^0.16.47")
# Fix if caret added: edit package.json back to exact pin
cd ..
```

### Step 14 — Verify grep clean
```bash
grep -rn "katex@0.16.11" src/ tests/ config/ scripts/ seshat-pwa/src/ seshat-pwa/package.json
```
Expected: 0 matches.

### Step 15 — Backend quality gates
```bash
make test-k K=test_load_real_substitution_map_shape   # the CI failure fixed
make test-k K=artifact_export                          # export path tests
make test-k K=artifact_fixture                         # e2e fixture builder
make test-k K=verifier                                 # test_verifier.py
make test                                              # full suite
make mypy
make ruff-check
make ruff-format
```
Expected: All pass.

### Step 16 — PWA tests
```bash
cd seshat-pwa && npm run test
```
Expected: toolkit-convergence test now asserts katex === '0.16.47'. All 93 tests pass.

### Step 17 — Commit and PR
```bash
git add -p   # review all changes
git commit -m "feat(security): katex /lib/ toolkit migration 0.16.11 → 0.16.47 (FRE-580)"
git push origin fre-580-katex-toolkit
gh pr create ...
```

## Acceptance criteria

- [ ] grep -rn "katex@0.16.11" src/ tests/ config/ scripts/ → 0 matches
- [ ] SRI in substitution map verified: sha384-CwjPRVHTvLiMBFjEoij... (js) + sha384-nH0MfJ44... (css)
- [ ] make test passes (3642+ tests)
- [ ] make mypy clean
- [ ] make ruff-check clean
- [ ] seshat-pwa vitest: toolkit-convergence pins 0.16.47, all 93 pass
- [ ] package.json: "katex": "0.16.47" (exact pin, no caret)

## Post-deploy (master action, not build session)
R2 upload: upload lib/katex@0.16.47/katex.min.{js,css} + fonts/ to artifacts.example.com.
Old lib/katex@0.16.11/ can remain until a cleanup pass.
