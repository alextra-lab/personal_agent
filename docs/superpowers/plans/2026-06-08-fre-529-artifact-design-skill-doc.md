# FRE-529 — Author `docs/skills/artifact-design.md` (runtime-guidance source-of-truth)

**Ticket:** FRE-529 (Approved, Tier-2:Sonnet) · Project: Artifact Execution Security
**Implements:** ADR-0089 Addendum A4 (PR #188), the `#4` toolkit ticket
**Sibling pattern:** `docs/skills/mermaid-diagrams.md` (self-describing frontmatter)
**Related:** FRE-528 (#3 prompt reframe — distills *from* this doc; shipped a42cd16),
FRE-527 (manifest + hosted `/lib/`), FRE-530 (#5 export-to-standalone), FRE-531 (#6 E2E live verify)

## What this is (and is not)

`docs/skills/artifact-design.md` is the **maintainable source-of-truth** the artifact
generation system prompt (`_HTML_GENERATION_SYSTEM_PROMPT`) is distilled from, and the future
target for a skill-loading mechanism. Per ADR-0089 A4 it is a **distinct runtime-guidance
artifact** — NOT an extension of the `frontend-design` harness skill (different consumer: the
runtime artifact-generation sub-agent vs the Claude Code developer agent).

Today the generation sub-agent is driven by the *system prompt*, which does **not** load
SKILL.md at runtime (ADR-0089 A4). So this doc is the **superset** the compressed prompt is
distilled from; it is authored in the skills self-describing pattern so a future loader can
pick it up. The doc therefore carries richer recipes/gotchas than the prompt; the **manifest
URLs/versions are the shared anchor** that keeps doc ↔ prompt ↔ hosted `/lib/` in lockstep.

## Single source of truth for paths/versions

`config/artifact_lib_manifest.json` (origin `https://artifacts.frenchforet.com`). Every
`/lib/` URL in the doc is the **full absolute, version-pinned** URL `f"{origin}/lib/{path}"`
(matching the FRE-528 prompt exactly — a relative `/lib/` path is not the expected reach form).

Verified assets (non-`eval_gated`):
- KaTeX `katex@0.16.11/katex.min.js` + `katex@0.16.11/katex.min.css`
- Chart.js `chartjs@4.4.7/chart.umd.js` (global `Chart`)
- highlight.js `highlightjs@11.9.0/highlight.min.js` + `highlightjs@11.9.0/github-dark.min.css`
- three.js `three@0.171.0/three.iife.min.js` (global `THREE`, r171)
- Fonts (OFL woff2): `fonts/source-serif-4@4.005/source-serif-4.woff2`,
  `fonts/playfair-display@2.103/playfair-display.woff2`,
  `fonts/jetbrains-mono@2.304/jetbrains-mono.woff2`
- `eval_gated`: `pagedjs@0.4.3/paged.polyfill.min.js` — **experimental**, present in the doc as
  an advanced/gated option (FRE-531 confirms eval-free under the live CSP), never first-class.

## Doc structure (sections to author)

Frontmatter (sibling to mermaid-diagrams.md): `name: artifact-design`, `description`,
`when_to_use` (worded for the artifact-generation context), `tools: []`,
`keywords` (artifact, html artifact, katex, chart.js, three.js, highlight.js, drop cap,
typography, pagination, export, portability …). No `nudge` (generic enough).

Body:
1. **What this skill does / when to use** — the sealed-box artifact-generation surface; reach
   for a `/lib/` library only when the model genuinely can't hand-roll the capability; most
   dynamism (sliders, sims, explorables, tabs) needs **no library** (inline JS + Canvas/WebGL/SVG).
2. **The curated `/lib/` shelf** — per-library recipe + gotcha for each verified asset, with the
   exact absolute snippet from the manifest:
   - **KaTeX → math**: `<link>` CSS + `<script>`; gotcha — CSS is required (glyphs break
     without it); render via `katex.render(tex, el, {throwOnError:false})` or auto-render
     delimiters; co-hosted `fonts/` resolves relatively (don't rewrite the CSS).
   - **Chart.js → data viz** (global `Chart`): needs a `<canvas>` with explicit
     width/height-or-container; **inline the data array** (no fetch); set
     `options.animation` sanely; gotcha — give the canvas a sized parent or it collapses.
   - **three.js → 3-D** (global `THREE`, r171): scene/camera/renderer boot snippet;
     **textures and models must be `data:` URIs or generated in code — never fetched**
     (`connect-src 'none'`); `requestAnimationFrame` loop; dispose on teardown.
   - **highlight.js → code** (global `hljs`): theme `<link>` + `<script>`;
     `hljs.highlightAll()` on DOMContentLoaded; put code in `<pre><code class="language-…">`.
   - **OFL fonts → prose**: `@font-face` with `src: url('…') format('woff2')` for the three
     families; wire them into the design-system `--font-*` custom properties; Source Serif 4
     body, Playfair Display display, JetBrains Mono code.
   - **paged.js → book/print** *(experimental / eval_gated)*: page-box setup sketch, but lead
     with native `@page`; flag it may be restricted under the live CSP (FRE-531).
3. **Native typography (no library)** — drop caps (`::first-letter`), justify + `hyphens: auto`
   (+ `lang`), `text-wrap: balance`, `font-feature-settings` (`liga`/`onum`/`kern`),
   `column-count`/`column-gap`/`column-rule`, `@page` + `break-inside: avoid` + widows/orphans.
4. **Design-system conventions** — the CSS-custom-property palette / spacing / type scale /
   utility classes the prompt defines (kept consistent with the prompt so the doc is its superset).
5. **Sealed-box constraints** — no network (fetch/XHR/WS/beacon blocked → inline all data &
   textures), no storage (localStorage/IndexedDB/cookies → keep state in JS/DOM), **no arbitrary
   CDN** (only the curated `/lib/` shelf loads; Tailwind/Alpine/jQuery/Google-Fonts/unpkg
   silently fail → inline), no popups / no external form submission.
6. **Portability decision** — three lanes, choose deliberately:
   - **static-SVG-travels**: Mermaid `<pre class="mermaid">` → server-rendered inline SVG,
     self-contained, viewable anywhere.
   - **interactive-viewed-on-origin**: JS/`/lib/` artifacts run on the hosted (Access-gated) page.
   - **exported-standalone** (FRE-530): server-side export — **inline mode** (default,
     offline-portable; libs+fonts inlined) vs **substitution-map mode** (opt-in; rewrite to
     public CDN + SRI; three.js + fonts inline-only); exported file leaves the envelope
     (unsandboxed) — which is why ↓.
7. **Standing rule: never bake secrets into an artifact** (ADR-0089 D4) — artifacts are
   untrusted bytes on an opaque origin and an exported file leaves the sandbox entirely; no
   tokens/keys/PII/internal URLs ever embedded.
8. **Distillation map** — short table: doc section → the prompt block it feeds, so the
   "traceably distilled from it" acceptance is explicit and future edits stay paired.
9. **References** — ADR-0089 + Addendum A, the manifest path, FRE-527/528/530/531,
   `mermaid-diagrams.md`.

## Steps

1. **TDD — drift-guard test first.** Add `test_artifact_design_doc_matches_manifest` to
   `tests/personal_agent/tools/test_artifact_tools.py` (co-located with the FRE-528 prompt
   drift-guard). It:
   - loads `config/artifact_lib_manifest.json` and reads `docs/skills/artifact-design.md`
     (both via repo-root `Path`, mirroring how the prompt test resolves the manifest),
   - asserts the doc has frontmatter (`name: artifact-design`),
   - for every **non-`eval_gated`** asset, asserts the **full absolute URL**
     `f"{origin}/lib/{path}"` appears verbatim in the doc (lockstep with manifest + prompt),
   - asserts paged.js is present but **not first-class** — marked `experimental` (mirrors the
     prompt test's paged.js assertion),
   - asserts the native-typography tokens are present (`::first-letter`, `hyphens: auto`,
     `text-wrap: balance`, `font-feature-settings`, `column-count`, `@page`),
   - asserts the secrets rule is present (`never bake secrets` substring).
   - Run `make test-file FILE=tests/personal_agent/tools/test_artifact_tools.py` → confirm the
     new test FAILS first (doc absent).
2. **Author** `docs/skills/artifact-design.md` per the structure above.
3. **Re-run** the module test → green.
4. **Quality gates:** `make test` (module then full) · `make mypy` · `make ruff-check` +
   `make ruff-format` · `pre-commit run --all-files`. (mypy/ruff are no-ops for the .md but the
   one new test function is Python and must pass lint.)
5. **PR** with template; pre-merge checklist only; **STOP** (master merges; no deploy needed —
   doc-only + a test).

## DECISION TO SURFACE — drift-guard test (recommended, mild scope addition)

The ticket acceptance is just "file exists with frontmatter + recipes; prompt traceably
distilled from it." A pure-markdown deliverable normally carries no test. I propose the small
manifest-driven drift-guard test above because:
- it makes "traceably distilled" **mechanically enforced** (doc ↔ manifest ↔ prompt locked on
  the same pinned URLs), matching the FRE-528 prompt test's shape, and
- the owner is documentation-drift-sensitive (a recurring guardian concern).

*Alternative:* author the doc with no test (lighter; relies on review to keep it in sync).
→ **Owner to confirm** the test is wanted before coding (default: include it).

## Files touched

- `docs/skills/artifact-design.md` (new)
- `tests/personal_agent/tools/test_artifact_tools.py` (one new drift-guard test, if approved)
- `docs/superpowers/plans/2026-06-08-fre-529-artifact-design-skill-doc.md` (this plan)

## Out of scope

- Any change to `_HTML_GENERATION_SYSTEM_PROMPT` (FRE-528, shipped) or the manifest/meter (527/526).
- Export-to-standalone implementation (FRE-530) — the doc *describes* the lanes, doesn't build them.
- Wiring a runtime SKILL-loading mechanism (future; the doc is the target, not the loader).
- Live render verification under CSP (FRE-531).
