# FRE-531 — E2E verification: /lib/ render under live CSP + offline export

**Ticket:** FRE-531 (Approved, Tier-2:Sonnet) · Project: Artifact Execution Security
**Implements:** ADR-0089 Addendum A7 (the observable-first done-bar, inherits ADR-0088)
**Blocked by (all Done):** FRE-527 (host /lib/ + manifest + verify-lib), FRE-528 (prompt), FRE-530 (export)
**Branch:** `fre-531-e2e-lib-verification` (off `origin/main` @ 5460a32)

---

## 1. What the ticket asks

Against a **real serve under the live CSP**:
1. An artifact loading hosted `/lib/` (KaTeX formula + Chart.js chart minimum) **renders** in drawer + standalone — Chromium **and** WebKit/iOS.
2. The same artifact **exported inline** (FRE-530) opens and renders **offline** (no network/Access), libs + fonts inlined.
3. `make verify-envelope` (extended in FRE-527) confirms `/lib/` serves correct executable-MIME + `nosniff`, reachable under the artifact CSP.
4. Bar: "it renders" is not enough — the **envelope must still be provably applied** (CSP header present + exact directive set) on the served artifact; a bare serve must still be flagged by FRE-506 telemetry.

## 2. The honest build-vs-master seam

The build session **cannot reach the live, Access-gated origin** (`artifacts.frenchforet.com/lib/`) — that needs CF Access service tokens and the live Worker. So:

| Acceptance piece | Owner |
|---|---|
| Reproducible **render harness** (KaTeX+Chart.js) under the CSP *directive shape*, Chromium+WebKit | **build (this PR)** |
| **Offline-export render** via the real `export_artifact_html` | **build (this PR)** |
| **paged.js eval-gate** resolution under the eval-free CSP | **build (this PR)** |
| Live `make verify-envelope` / `verify-lib` **green on the real origin** (exact host tokens, real Worker) | **master + owner, post-merge** (Linear comment, not PR checklist) |
| Real-device WebKit/iOS pass | **owner, post-merge** (optional, beyond CI WebKit engine) |

The hermetic harness proves render + offline + eval-gate under the **exact CSP directive set** (host tokens rebound to the local serving origin — the one fidelity gap). The live `verify-envelope` on a real artifact URL closes that gap by asserting the exact `artifacts.frenchforet.com` tokens + real Worker MIME/nosniff. Together they satisfy the acceptance.

**Real library bytes without live Access:** the substitution map (`config/artifact_lib_substitution_map.json`) carries public-CDN twins (jsdelivr) + pinned `sha384` SRI for KaTeX, Chart.js, highlight.js, paged.js — all `cors_verified`. The harness fetches those twins at setup and **byte-verifies against the pinned SRI** (fail-closed on CDN drift). Real bytes, no vendoring, no repo bloat.

## 3. Files

### New
- `scripts/build_e2e_artifact_fixtures.py` — Python fixture builder. Fetches SRI-pinned CDN bytes; writes a `/lib/` mirror, a hosted-style `artifact.html` (refs `/lib/…`), and a `standalone.html` produced by the **real** `export_artifact_html(mode="inline")`. Emits a build manifest JSON.
- `tests/personal_agent/scripts/test_build_e2e_artifact_fixtures.py` — unit tests for the builder's pure parts (SRI verify gate, standalone has zero `/lib/` refs, fonts inlined as `data:`), fetcher mocked.
- `e2e/artifact-lib/playwright.config.ts` — dedicated Playwright project (Chromium + WebKit), no Next.js webServer.
- `e2e/artifact-lib/csp-server.ts` — ~40-line Node static server that serves the fixture dir with the **exact `EXPECTED_CSP_DIRECTIVES`** as a response-header CSP (host token rebound to its own origin).
- `e2e/artifact-lib/artifact-lib.spec.ts` — the three scenarios (below).
- `e2e/artifact-lib/package.json` — pins `@playwright/test` only.
- `docs/skills/artifact-design.md` — append a "## E2E verification (FRE-531)" runbook section (how to run the harness; the live `verify-envelope` post-merge gate).

### Modified
- `Makefile` — add `verify-artifact-e2e` target (builds fixtures + runs the Playwright project).

**Owner decisions (2026-06-09):** WebKit — install + run both engines. paged.js — **record-only** (report the Scenario C verdict in the runbook; do **not** flip `eval_gated` — that stays a separate explicit decision). Bytes — **fetch CDN twins at setup**, SRI-pinned (single source of truth = substitution map; skip cleanly if CDN unreachable).

## 4. The three Playwright scenarios

All assert **zero `securitypolicyviolation` events** (a `page.on` listener collects them) in addition to render.

**Assertion depth (per codex review):** existence of a `.katex` node / non-blank canvas is necessary but not sufficient — they can pass on a broken render. Each render scenario therefore also asserts *semantic* fidelity:
- **KaTeX:** the fixture formula is a known input (e.g. `E = mc^2`); assert KaTeX produced the expected math structure — a `.katex-html` subtree containing the variable/operator `.mord`/`.mbin` spans for that formula (not merely `.katex` present), and that the source `\(…\)`/`$$…$$` delimiters were consumed (no raw TeX left in `textContent`).
- **Chart.js:** assert the live `Chart` instance reflects the fixture data — read `Chart.getChart(canvas).data.datasets[0].data` equals the seeded array and `chart.getDatasetMeta(0).data.length` matches the point count — **and** the canvas has non-background pixels. (Instance-state + pixels together rule out both "blank canvas" and "wrong data".)

- **A — hosted render under the CSP.** `csp-server` serves `artifact.html` + `/lib/` mirror under the exact directive set. Assert the KaTeX + Chart.js semantic checks above + zero CSP violations. Chromium + WebKit.
- **B — offline export render.** Open `standalone.html` via `file://` with **all network aborted** (`page.route('**', r => r.abort())` + `context` offline). Same semantic + CSP assertions. Proves the FRE-530 inline export renders with no network. Chromium + WebKit.
- **C — paged.js eval-gate (record-only).** Load `paged.polyfill.min.js` under the CSP (which omits `'unsafe-eval'`). Collect violations; record whether it runs **without an eval/script CSP violation** and paginates (`.pagedjs_page` appears). The verdict is **reported in the runbook only** — `eval_gated` is **not** flipped (owner decision: separate explicit call). The test asserts the *observed* verdict so a future regression is caught, but does not mutate config.

## 5. Build sequence (TDD)

1. **Scaffold + failing builder test** → write `test_build_e2e_artifact_fixtures.py` (SRI-mismatch raises; standalone has no `/lib/` refs; font→`data:`), confirm it fails (no module). → verify: `uv run pytest tests/personal_agent/scripts/test_build_e2e_artifact_fixtures.py` red.
2. **Implement `build_e2e_artifact_fixtures.py`** (httpx fetch + SRI verify reusing `verify_sri`/`compute_sri`; build fixtures via real `export_artifact_html`). → verify: builder test green; run `uv run python scripts/build_e2e_artifact_fixtures.py --out /tmp/fre531` and eyeball the three outputs + manifest.
3. **Playwright project + csp-server + spec** (Scenarios A/B/C). Install WebKit: `cd e2e/artifact-lib && npm i && npx playwright install --with-deps webkit chromium`. → verify: `make verify-artifact-e2e` green on both engines; capture Scenario C verdict.
4. **paged.js Scenario C verdict** — record-only; capture the observed eval-free verdict in the runbook (no config flip).
5. **Makefile `verify-artifact-e2e` + runbook** in artifact-design.md.
6. **Quality gates** — `make test` (targeted then full), `make mypy`, `make ruff-check`, `make ruff-format`, `pre-commit run --all-files`.
7. **PR** — pre-merge checklist only; post-merge live-verify steps go in a Linear comment.

## 6. Risks / decisions for owner

- **WebKit install** in CI/sandbox (heavier dep). Fork: install + run both engines now, vs Chromium-only in-repo + defer WebKit/iOS to owner's real device.
- **paged.js**: auto-flip `eval_gated:false` on a clean Scenario C, vs record-only (leave gated, separate decision).
- **Bytes source**: CDN-fetch-at-setup (SRI-pinned, lean repo, needs network during the harness) vs vendor bytes (offline harness, +~1 MB).

## 7. Out of scope / follow-ups (file as Needs Approval)
- Live `verify-envelope` green on the real origin → master post-merge.
- three.js IIFE render scenario (manifest has it; ticket minimum is KaTeX+Chart.js) → candidate follow-up.
