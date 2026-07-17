# FRE-528 — Reframe the artifact generation prompt to advertise `/lib/` + native typography

**Ticket:** FRE-528 (Approved, Tier-2:Sonnet) · Project: Artifact Execution Security
**Implements:** ADR-0089 Addendum A4 (PR #188)
**Blocked by:** FRE-527 (Done — `/lib/` hosted + manifest/substitution map committed, PR #191)
**Related:** FRE-511 (sandbox-not-sanitize prompt reframe), FRE-526 (demand meter), FRE-529 (#4 SKILL doc, not yet authored), FRE-531 (#6 E2E live verify)

## Scope (what changes)

Rewrite `_HTML_GENERATION_SYSTEM_PROMPT` in `src/personal_agent/tools/artifact_tools.py:963`
(currently lines 963–1012). Replace the blanket *"no external resources / everything inline /
no external CDN — fully self-contained"* framing with the **sanctioned curated toolkit**, while
keeping the still-true sealed-box constraints.

Single source of truth for paths/versions: `config/artifact_lib_manifest.json` (origin
`https://artifacts.example.com`). ADR-0089 A3 + the meter regex (`_SCRIPT_SRC_RE`,
artifact_tools.py:802) require **absolute, version-pinned** `/lib/` URLs (a relative `/lib/`
path is *not* the expected reach form, and is not counted as host-allowed demand-met).

## Exact toolkit snippets to embed in the prompt (from the manifest)

Origin: `https://artifacts.example.com`

- **KaTeX → math** (MIT)
  - `<link rel="stylesheet" href="https://artifacts.example.com/lib/katex@0.16.11/katex.min.css">`
  - `<script src="https://artifacts.example.com/lib/katex@0.16.11/katex.min.js"></script>`
- **Chart.js → data viz** (MIT) — global `Chart`
  - `<script src="https://artifacts.example.com/lib/chartjs@4.4.7/chart.umd.js"></script>`
- **three.js → 3-D** (MIT) — global `THREE`, r171; inline geometry + **textures as `data:` URIs, never fetch**
  - `<script src="https://artifacts.example.com/lib/three@0.171.0/three.iife.min.js"></script>`
- **highlight.js → code** (BSD-3)
  - `<link rel="stylesheet" href="https://artifacts.example.com/lib/highlightjs@11.9.0/github-dark.min.css">`
  - `<script src="https://artifacts.example.com/lib/highlightjs@11.9.0/highlight.min.js"></script>`
- **Fonts → prose** (OFL, variable woff2) via `@font-face` `src: url(...) format('woff2')`:
  - Source Serif 4 → `…/lib/fonts/source-serif-4@4.005/source-serif-4.woff2` (body serif)
  - Playfair Display → `…/lib/fonts/playfair-display@2.103/playfair-display.woff2` (display)
  - JetBrains Mono → `…/lib/fonts/jetbrains-mono@2.304/jetbrains-mono.woff2` (mono)
- **paged.js → book/print** — see decision below.

### `mermaid` is unchanged

Static diagrams that travel keep the existing `<pre class="mermaid">…</pre>` server-render path
(self-contained inline SVG). The PORTABILITY block stays.

## Native typography recipes (no library) to add

- Drop cap: `p::first-letter { float: left; font-size: 3.2em; line-height: 0.8; padding-right: .08em; }`
- Justified prose: `text-align: justify; hyphens: auto;` (+ `lang` on `<html>`)
- Balanced headings: `text-wrap: balance;`
- Ligatures / old-style figures: `font-feature-settings: "liga", "onum", "kern";`
- Multi-column: `column-count`, `column-gap`, `column-rule`
- Print/book layout: `@page { margin … }`, `break-inside: avoid;`, `widows`/`orphans`

## DECISION TO SURFACE — paged.js

The ticket lists `paged.js → book/print layout`, but FRE-527's committed manifest marks paged.js
`"eval_gated": true` and **excludes it from the verify-lib default assert set until confirmed
eval-free under the artifact CSP (which omits `'unsafe-eval'`)** — runtime confirmation is FRE-531.

**Plan default (conservative):** lead print/book layout with the **native CSS `@page` /
`column-count` / `break-*`** recipes, and mention paged.js only as an *advanced, experimental*
option that may be restricted under the live CSP (pending FRE-531). This avoids steering the
generator toward an artifact that silently fails. *Alternative:* advertise paged.js as a
first-class lib like the others. → **Owner to confirm before coding.**

## Steps

1. **TDD — extend the static-assertion tests** in
   `tests/personal_agent/tools/test_artifact_tools.py` (alongside the two existing prompt tests
   at ~1386–1411). New test `test_system_prompt_advertises_curated_lib_toolkit`:
   - asserts each verified snippet substring is present with the exact manifest version pin
     (katex@0.16.11, chartjs@4.4.7, three@0.171.0, highlightjs@11.9.0, the three font paths),
   - asserts the absolute origin `https://artifacts.example.com/lib/` is used (not a bare
     relative `/lib/`),
   - asserts native-typography tokens present (`::first-letter`, `hyphens: auto`,
     `text-wrap: balance`, `font-feature-settings`, `column-count`, `@page`),
   - asserts arbitrary-CDN steering is retained (some token e.g. "curated" / "only" wording).
   - **Drift guard (codex):** loop over the manifest assets and assert every non-`eval_gated`
     asset appears in the prompt as its **full absolute URL** — `f"{origin}/lib/{path}"` —
     NOT the bare relative manifest `path`. A path-only assertion would pass even if the prompt
     emitted a relative `/lib/...` form, which the meter does not count as demand-met
     (`_SCRIPT_SRC_RE` excludes relative srcs). Use the real full font paths (no ellipses).
   - **paged.js assertion (codex):** the drift guard skips `eval_gated` entries, so add one
     explicit assertion that paged.js is NOT presented as a first-class snippet — i.e. its bare
     `<script src>` URL is absent and the prompt marks it experimental/gated (e.g.
     `assert "experimental" in prompt.lower()` near the paged.js mention).
   - Run: `make test-file FILE=tests/personal_agent/tools/test_artifact_tools.py` → confirm the
     new test FAILS first.
2. **Implement** — rewrite `_HTML_GENERATION_SYSTEM_PROMPT`:
   - Keep tokens the existing tests assert: `JavaScript is available`, `No network`, `No storage`,
     `PORTABILITY`, `travel`, `mermaid`, `<pre class="mermaid">`; keep absence of `REJECTED`,
     `JavaScript-free`, `cannot run`.
   - Replace the "No external CDN links — fully self-contained" line and the SEALED-BOX
     "No external resources" bullet with: curated `/lib/` shelf is admitted; **arbitrary** CDNs
     still silently fail; inline your own data + 3-D textures as `data:` URIs, never fetch.
   - Add the toolkit snippets + "when to reach for each" + native typography recipes.
   - Keep "inline your own CSS/data + native JS" as the default where no library is warranted.
3. **Re-run** `make test-file FILE=tests/personal_agent/tools/test_artifact_tools.py` → green.
4. **Quality gates:** `make test` (module then full) · `make mypy` · `make ruff-check` +
   `make ruff-format` · `pre-commit run --all-files`.
5. **PR** with template; pre-merge checklist only; STOP (master merges/deploys/verifies live
   via FRE-531).

## Note — meter/manifest gap is NOT in FRE-528's scope (codex)

`_is_lib_reach` (artifact_tools.py:872) only checks netloc + `/lib/` prefix; it does not validate
against manifest entries or version pins, so any `https://artifacts.example.com/lib/*.js`
counts as demand-met. The prompt reframe does not (and should not) close this — it's the
verifier's job. Reviewers should not read prompt-correctness as meter-correctness.

## Files touched

- `src/personal_agent/tools/artifact_tools.py` (prompt only; no logic/signature change)
- `tests/personal_agent/tools/test_artifact_tools.py` (one new test, manifest-driven drift guard)

(The plan doc itself is committed but is not part of the implementation surface.)

## Out of scope

- Authoring `docs/skills/artifact-design.md` (that is FRE-529 / #4).
- Live render verification under CSP (FRE-531 / #6).
- Any meter / verifier / manifest change (FRE-526/527 shipped).
