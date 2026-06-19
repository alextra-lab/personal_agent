---
name: artifact-design
description: Source-of-truth guidance for generating rich, sealed-box HTML artifacts — the curated /lib/ toolkit (KaTeX, Chart.js, three.js, highlight.js, OFL fonts, paged.js), native-CSS typography, the sealed-box constraints, the portability decision, and the standing "never bake secrets" rule. Use when authoring a standalone HTML document artifact (reports, explorables, charts, math, 3-D scenes, code walkthroughs, book/print layouts).
when_to_use: When generating an HTML artifact for the user — deciding whether to reach for a curated /lib/ library or hand-roll inline, getting each library's boot recipe and gotcha right, applying publishing-grade native typography, staying inside the sealed-box constraints, and choosing the right portability lane.
tools: []
keywords:
  - artifact
  - html artifact
  - rich artifact
  - artifact design
  - sealed box
  - curated toolkit
  - lib shelf
  - katex
  - math typesetting
  - latex
  - chart.js
  - data visualization
  - three.js
  - 3-d
  - webgl
  - highlight.js
  - syntax highlighting
  - code block
  - paged.js
  - pagination
  - book layout
  - print layout
  - drop cap
  - typography
  - font-feature-settings
  - hyphenation
  - text-wrap balance
  - column-count
  - portability
  - export standalone
  - data uri
  - never bake secrets
---

# SKILL: Artifact Design

> **Use this skill to** author a complete, standalone HTML artifact that runs inside the
> sealed box: (a) decide when a curated `/lib/` library beats hand-rolling, (b) get each
> library's boot recipe and gotcha right, (c) apply publishing-grade **native** typography,
> (d) stay inside the sealed-box constraints, (e) choose the right **portability** lane, and
> (f) never leak secrets into a file that may leave the sandbox.

> **Tier:** runtime-guidance (not a CLI skill). **ADR:** `docs/architecture_decisions/ADR-0089-artifact-execution-security-model.md` (§D2/D3/D4 + Addendum A).
> **Relationship to the generation prompt:** this doc is the **maintainable source-of-truth**;
> the artifact generation system prompt (`_HTML_GENERATION_SYSTEM_PROMPT`,
> `src/personal_agent/tools/artifact_tools.py`) is **distilled from it**. The generation
> sub-agent is driven by that compressed prompt at runtime today — it does **not** load this
> file. Keep the two paired: when the manifest or a recipe changes, update this doc and
> re-distill the prompt. Versions/URLs are anchored to `config/artifact_lib_manifest.json`.
> **Not** an extension of the `frontend-design` harness skill (that guides the *developer*
> agent; this guides the *runtime artifact-generation* sub-agent — a different consumer).

---

## What This Skill Does

An artifact is a complete `<!DOCTYPE html>…</html>` document that renders in a **sealed box**:
an opaque-origin, CSP-locked, network-denied, storage-denied iframe (ADR-0089 D2/D3). Inside
that box JavaScript runs normally — inline `<script>` and event handlers work — so simulations,
explorable diagrams, charts, animations, calculators, tabs, and filters are all fair game.

The decisive split: **most dynamism needs no library.** Sliders, simulations, explorables,
animation, tabs/filters are all reachable with inline JS + Canvas / WebGL / SVG / CSS, which
ADR-0089 already unlocks. Reach for a curated `/lib/` library **only where the model genuinely
cannot hand-roll the capability well** — real math typesetting, charts, 3-D, syntax
highlighting, publishing-grade fonts, print pagination. Otherwise inline your own code: it is
cheaper, more portable, and fully under your control.

---

## When to Use

- **Reach for `/lib/`** when the capability is hand-roll-impossible at quality: TeX math
  (KaTeX), real charts (Chart.js), 3-D scenes (three.js), syntax highlighting (highlight.js),
  book-grade prose faces (OFL fonts), print pagination (paged.js — experimental).
- **Reach for native CSS/JS** for everything else — design system, layout, interactivity,
  typography refinements. This is the default.
- **Prefer Mermaid server-SVG** for static diagrams that should travel with the file (see
  Portability).

---

## The Curated `/lib/` Shelf

A small, vetted shelf is hosted on the artifact origin (`https://artifacts.frenchforet.com`) —
it is the **only** external origin a sealed artifact may load. Reference each asset by its
**exact absolute, version-pinned URL** below; a relative `/lib/…` path is *not* the expected
reach form and is not counted as a satisfied capability by the demand meter. URLs are immutable
pins (ADR-0089 A3) — never edit a version in place. Source of truth:
`config/artifact_lib_manifest.json`.

### KaTeX → math / formula typesetting (MIT)

```html
<link rel="stylesheet" href="https://artifacts.frenchforet.com/lib/katex@0.16.47/katex.min.css">
<script src="https://artifacts.frenchforet.com/lib/katex@0.16.47/katex.min.js"></script>
```

```html
<span id="eq"></span>
<script>
  katex.render("c = \\pm\\sqrt{a^2 + b^2}", document.getElementById("eq"),
    { throwOnError: false, displayMode: true });
</script>
```

**Gotchas:**
- The **CSS `<link>` is mandatory** — without it KaTeX renders boxes/tofu (the glyph metrics
  and math fonts live in the stylesheet). Loading only the JS is the most common KaTeX failure.
- KaTeX's stylesheet resolves its math fonts from a **co-hosted `fonts/` directory relative to
  the CSS** — do not rewrite or relocate the CSS `url(fonts/…)` references; the shelf hosts them
  at the path the CSS expects.
- Use `throwOnError: false` so a single bad expression degrades to a readable error instead of
  aborting render. `displayMode: true` for block equations, omit for inline.
- KaTeX is **not** auto-render: it does not scan the page for `$…$` delimiters by default.
  Call `katex.render(tex, element, opts)` per equation (the auto-render extension is not on the
  shelf — render explicitly).

### Chart.js → charts / data visualization (MIT, global `Chart`)

```html
<script src="https://artifacts.frenchforet.com/lib/chartjs@4.4.7/chart.umd.js"></script>
```

```html
<canvas id="spend" width="640" height="320"></canvas>
<script>
  new Chart(document.getElementById("spend"), {
    type: "bar",
    data: {
      labels: ["Q1", "Q2", "Q3", "Q4"],
      datasets: [{ label: "Spend", data: [12, 19, 8, 15] }]   // inline the data
    },
    options: { responsive: true, maintainAspectRatio: false }
  });
</script>
```

**Gotchas:**
- Give the `<canvas>` **explicit `width` and `height` attributes** (e.g.
  `<canvas width="640" height="320">`). Chart.js reads the element's attributes for initial
  layout; a CSS-sized parent alone is not enough and an unsized canvas collapses to 0×0 or
  renders blurry. If you want it fluid, wrap the canvas in a sized container **and** set
  `maintainAspectRatio: false`.
- **Inline the data** as a JS array/object literal. There is no `fetch` — never point Chart.js
  at a URL or load CSV/JSON over the network.
- The global is `Chart` (UMD build). One `new Chart(...)` per canvas.

### three.js → 3-D / spatial / physics (MIT, global `THREE`, r171)

```html
<script src="https://artifacts.frenchforet.com/lib/three@0.171.0/three.iife.min.js"></script>
```

```html
<div id="stage" style="width:100%;height:420px"></div>
<script>
  const el = document.getElementById("stage");
  const scene = new THREE.Scene();
  const camera = new THREE.PerspectiveCamera(60, el.clientWidth / el.clientHeight, 0.1, 100);
  camera.position.z = 4;
  const renderer = new THREE.WebGLRenderer({ antialias: true });
  renderer.setSize(el.clientWidth, el.clientHeight);
  el.appendChild(renderer.domElement);

  const mesh = new THREE.Mesh(
    new THREE.IcosahedronGeometry(1, 0),
    new THREE.MeshStandardMaterial({ color: 0x6366f1, flatShading: true })
  );
  scene.add(mesh);
  scene.add(new THREE.DirectionalLight(0xffffff, 1).translateZ(5));
  scene.add(new THREE.AmbientLight(0x404040));

  (function loop() {
    requestAnimationFrame(loop);
    mesh.rotation.x += 0.005; mesh.rotation.y += 0.008;
    renderer.render(scene, camera);
  })();
</script>
```

**Gotchas:**
- **Textures and models must be `data:` URIs or generated in code — never fetched.**
  `connect-src 'none'` blocks `TextureLoader`/`GLTFLoader` over the network. Build geometry
  procedurally, embed textures as base64 `data:` URIs, or draw to an offscreen `<canvas>` and
  use it as a `CanvasTexture`.
- The shelf hosts a **bundled IIFE/global build** exposing `THREE` on `window` — there is no
  ES-module import (the sealed null-origin can't resolve `import`/CORS). Use the global, not
  `import * as THREE`.
- Size the renderer to a container with an explicit height; on resize, update
  `camera.aspect` + `renderer.setSize`. Dispose geometries/materials/renderer if you tear a
  scene down to avoid GPU leaks in long-lived artifacts.

### highlight.js → code / syntax highlighting (BSD-3, global `hljs`)

```html
<link rel="stylesheet" href="https://artifacts.frenchforet.com/lib/highlightjs@11.9.0/github-dark.min.css">
<script src="https://artifacts.frenchforet.com/lib/highlightjs@11.9.0/highlight.min.js"></script>
```

```html
<pre><code class="language-python">def greet(name): return f"hi {name}"</code></pre>
<script>
  document.addEventListener("DOMContentLoaded", () => hljs.highlightAll());
</script>
```

**Gotchas:**
- Put code inside `<pre><code class="language-XXX">…</code></pre>` and call
  `hljs.highlightAll()` **after the DOM is ready** (`DOMContentLoaded`) — calling it before the
  `<code>` elements exist highlights nothing.
- The theme **`<link>` carries the colors**; without it highlighted tokens get class names but
  no styling. `github-dark` is the hosted theme.
- HTML-escape `<`, `>`, `&` inside the code text, or the browser parses them as markup.

### OFL fonts → publishing-grade prose (SIL OFL, variable woff2)

Declare each face with `@font-face`, then wire it into the design-system custom properties:

```css
@font-face {
  font-family: "Source Serif 4";
  src: url("https://artifacts.frenchforet.com/lib/fonts/source-serif-4@4.005/source-serif-4.woff2") format("woff2");
  font-weight: 200 900; font-display: swap;
}
@font-face {
  font-family: "Playfair Display";
  src: url("https://artifacts.frenchforet.com/lib/fonts/playfair-display@2.103/playfair-display.woff2") format("woff2");
  font-weight: 400 900; font-display: swap;
}
@font-face {
  font-family: "JetBrains Mono";
  src: url("https://artifacts.frenchforet.com/lib/fonts/jetbrains-mono@2.304/jetbrains-mono.woff2") format("woff2");
  font-weight: 100 800; font-display: swap;
}
:root {
  --font-serif: "Source Serif 4", Georgia, serif;       /* body prose */
  --font-display: "Playfair Display", Georgia, serif;    /* display headings */
  --font-mono: "JetBrains Mono", ui-monospace, monospace;/* code */
}
```

**Gotchas:**
- These are **variable** woff2 fonts — declare a weight range (`font-weight: 200 900`) so the
  whole axis is usable from one file.
- `font-src https://artifacts.frenchforet.com data:` already admits them — **no CSP change**.
  Google Fonts / other font CDNs silently fail; use these or inline `data:` woff2.
- Use Source Serif 4 for body prose, Playfair Display for display headings, JetBrains Mono for
  code, falling back to system faces only when no shelf font fits.

### paged.js → book / print layout (MIT) — **experimental, gated**

paged.js is on the shelf **but is `eval_gated`**: it may require `eval` under the live CSP
(which omits `'unsafe-eval'`), pending runtime confirmation (FRE-531). Treat it as an
**advanced, experimental** option that **may silently fail under the live page policy** — do
**not** rely on it as a first-class library. For book/print layout, **lead with the native
`@page` / `column-count` / `break-*` recipes below**; reach for paged.js only for
hand-roll-impossible pagination (running heads, footnote placement, generated page numbers) and
only after confirming it loads in the target environment.

---

## Native Typography (no library)

Reach for these **before** any font library — they cost zero bytes and never fail under the CSP.

```css
/* Drop cap */
p.lead::first-letter { float: left; font-size: 3.2em; line-height: 0.8; padding-right: 0.08em; font-weight: 700; }

/* Justified prose with hyphenation — set lang on <html lang="en"> for the dictionary */
.prose { text-align: justify; hyphens: auto; }

/* Balanced headings (no orphan word on the last line) */
h1, h2 { text-wrap: balance; }

/* Ligatures + old-style figures + kerning */
.prose { font-feature-settings: "liga", "onum", "kern"; }

/* Multi-column flow */
.columns { column-count: 2; column-gap: 2rem; column-rule: 1px solid var(--color-muted); }

/* Print / book pages */
@page { margin: 2cm; }
.chapter { break-inside: avoid; }
p { widows: 3; orphans: 3; }
```

**Notes:**
- `hyphens: auto` needs a `lang` attribute on `<html>` (or the block) to pick the hyphenation
  dictionary.
- `text-wrap: balance` is for short blocks (headings, captions) — don't apply it to long body
  paragraphs.
- `font-feature-settings` requires the font to carry the features (the OFL faces above do).

---

## Design-System Conventions

Define a complete design system in a `<style>` block in `<head>` (kept consistent with the
generation prompt so this doc is its superset):

- **Colors** as CSS custom properties: `--color-primary`, `--color-secondary`, `--color-accent`,
  `--color-bg`, `--color-surface`, `--color-text`, `--color-muted`.
- **Spacing scale**: `--spacing-1` … `--spacing-8` (0.25rem increments).
- **Typography**: `--font-sans`, `--font-mono` (plus the OFL `--font-serif`/`--font-display`
  above when prose-grade); size classes `text-xs` … `text-3xl`.
- **Utility classes**: `flex`, `grid`, `gap-1`…`gap-6`, `p-1`…`p-8`, `m-1`…`m-8`, `text-center`,
  `font-bold`, `rounded`, `rounded-lg`, `shadow`, `shadow-lg`, `w-full`.
- **Semantic HTML5**: `header`/`main`/`section`/`article`/`footer`/`nav`/`aside`/`figure`.
- **Responsive** via `@media`; **accessible** heading hierarchy, `alt` text, ARIA labels,
  sufficient contrast.
- Aim for **under 200KB** of HTML text.

---

## Sealed-Box Constraints (hard, enforced by the runtime — design within them)

- **No network.** `fetch` / `XHR` / `WebSocket` / `sendBeacon` are blocked (`connect-src 'none'`).
  Embed **all** your own data inline; inline images and 3-D textures as `data:` URIs or inline
  SVG — never fetch them.
- **No storage.** `localStorage`, `sessionStorage`, `IndexedDB`, and cookies are unavailable.
  Keep state in JS variables or the DOM.
- **No arbitrary CDN.** Only the curated `/lib/` shelf above loads. Any other external
  script/style/font/image (Tailwind CDN, Alpine.js, jQuery, Google Fonts, unpkg, …) **silently
  fails** at view time — the artifact commits intact but renders broken. Inline it instead.
  Default to inline CSS + native JS wherever no shelf library is warranted.
- **No popups, no form submission to external endpoints.**

---

## Portability — choose the lane deliberately

Three distinct lanes; pick by how the artifact needs to travel.

1. **static-SVG-travels.** For static diagrams — flowcharts, architecture, sequence/class
   diagrams — use `<pre class="mermaid">…</pre>` markup. The server renders these to **inline
   SVG** (FRE-396), so the document stays self-contained and viewable anywhere, including in an
   exported file. Example: `<pre class="mermaid">graph LR; A[Start] --> B[End];</pre>`. This is
   strictly better than a client diagram library for static diagrams.
2. **interactive-viewed-on-origin.** JS-driven and `/lib/`-using artifacts run on their hosted,
   Access-gated page. They reference the sealed shelf and are otherwise **view-on-origin-only** —
   the live interactivity is the point, and it lives where the envelope is applied.
3. **exported-standalone** (server-side export, FRE-530). Two modes, both driven by the per-asset
   export substitution map:
   - **Inline (default, offline-portable):** the referenced pinned `/lib/` assets + fonts are
     inlined (scripts inline, CSS inline, fonts/images as `data:` URIs) into one self-contained
     HTML file that renders **offline, anywhere**.
   - **Substitution-map (opt-in, lean/online):** each `/lib/<name>@<version>` reference is
     rewritten to the **same version on a public CDN** with **Subresource Integrity** (`integrity`
     + `crossorigin`) — tiny, human-editable, byte-verified. **three.js and the fonts inline-only**
     (no faithful public classic twin / UA-varying font CDNs); KaTeX / Chart.js / highlight.js
     map cleanly.
   - **Honest limit:** an exported file **leaves the envelope** — it runs **unsandboxed**
     wherever opened (no CSP, no opaque origin), and substitution-map exports depend on a
     third-party CDN at view time (bounded by SRI). This is why the next rule is absolute.
   - **Realized as** `GET /api/v1/artifacts/{id}/export?mode=inline|substitute` (owner-scoped,
     HTML-only). Inline mode SRI-verifies each fetched `/lib/` asset against the pinned map and
     also inlines a stylesheet's own `url(...)` subresources (e.g. KaTeX glyph fonts) as `data:`
     URIs so math renders offline; substitute mode emits the pinned `integrity` and lets the
     **browser** byte-verify at view time. A reference to an **unmapped** `/lib/` path fails the
     export closed (it would otherwise silently point a "portable" file back at the gated origin).

> *Saving work produced **inside** a running artifact* (e.g. a draft typed into the artifact) is a
> different problem — the sandbox omits downloads/storage/network, so a running artifact cannot
> save out. That needs the future parent-brokered model-bridge (ADR-0089 D4a), not an export
> mode. Out of scope here.

---

## Standing Rule: never bake secrets into an artifact (ADR-0089 D4)

**Never bake secrets into an artifact.** Artifacts are untrusted LLM-authored bytes served from
an opaque origin, and an exported file leaves the sandbox entirely — it can be opened, inspected,
and shared anywhere with no envelope around it. Do **not** embed API keys, tokens, credentials,
session identifiers, internal hostnames/URLs, personal data, or anything that must not travel in
a plain file. Inline only the content the artifact is meant to display.

---

## Distillation Map (doc → generation prompt)

The generation prompt (`_HTML_GENERATION_SYSTEM_PROMPT`) is a compressed distillation of this
doc. Keep the pairs in sync when either changes:

| This doc section | Prompt block it feeds |
|---|---|
| Curated `/lib/` Shelf (the absolute URLs) | `CURATED TOOLKIT` bullet (exact `<script>`/`<link>` snippets) |
| Per-library "when to reach for each" | `CURATED TOOLKIT` per-asset one-liners |
| three.js textures-as-data-URI gotcha | `CURATED TOOLKIT` three.js note + `SEALED-BOX` no-network |
| paged.js experimental/gated | `CURATED TOOLKIT` book/print "experimental" line |
| Native Typography | `NATIVE TYPOGRAPHY` bullet |
| Design-System Conventions | `REQUIREMENTS` design-system bullets |
| Sealed-Box Constraints | `SEALED-BOX CONSTRAINTS` bullet |
| Portability (3 lanes) | `PORTABILITY` bullet |
| Never bake secrets | (carried as the standing D4 rule) |

---

## E2E verification (FRE-531 · ADR-0089 Addendum A7)

The done-bar for the curated toolkit is **"it renders under the live policy, and the
envelope is provably applied"** — not "it renders". Two complementary gates cover it:

**1. Hermetic render harness (in-repo, CI-able) — `make verify-artifact-e2e`.**
Builds a real, SRI-pinned fixture set (`scripts/build_e2e_artifact_fixtures.py`: KaTeX
0.16.11 + Chart.js 4.4.7 + paged.js fetched from the substitution-map CDN twins, each
byte-verified against the pinned `sha384`), then runs `e2e/artifact-lib/` on **Chromium
and WebKit**:

- **A — hosted render** under the *exact* artifact CSP directive set (served by a local
  CSP server whose header is emitted by the Python builder from `EXPECTED_CSP_DIRECTIVES`,
  the single source of truth). Asserts the KaTeX MathML annotation echoes the source TeX,
  the live `Chart.getChart()` instance holds the seeded dataset + the canvas painted, the
  CSP header is present, and **zero CSP violations**.
- **B — offline export** — the real `export_artifact_html(mode="inline")` output opened via
  `file://` with **all network aborted**; same semantic render assertions; proves zero
  network requests (truly self-contained).
- **C — paged.js eval-gate** — loads paged.js under the eval-free CSP and asserts it runs
  with **no `eval`/script CSP violation**.

One-time browser install: `cd e2e/artifact-lib && npx playwright install --with-deps webkit chromium`.

**Fidelity gap:** the harness rebinds the CSP host token to its localhost serving origin, so
it proves render under the directive *shape*, not the exact `artifacts.frenchforet.com` tokens.

**2. Live-origin gate (master, post-merge) — `make verify-envelope URL=…` + `make verify-lib`.**
Closes the host-token gap on the real Access-gated origin: the served artifact carries the
exact CSP directive set + MIME + `nosniff`, and every `/lib/` asset serves the correct
executable/typed MIME + `nosniff` reachable under the artifact CSP. Requires a CF Access
service token; runs at the deploy gate. A real-device iOS Safari pass is the owner's check.

**paged.js Scenario C verdict (2026-06-09, record-only):** paged.js 0.4.3 runs **eval-free**
under the eval-free artifact CSP and paginates on **both Chromium and WebKit** in the hermetic
harness — corroborating the static-analysis claim. The `eval_gated` flag is **left set** (the
shelf entry stays *experimental/gated*); un-gating it — which would add paged.js to the default
`verify-lib` assert set — is a separate explicit decision pending the live-origin confirmation.

---

## References

- ADR-0089 — Artifact Execution Security Model (`docs/architecture_decisions/ADR-0089-artifact-execution-security-model.md`), §D2/D3/D4 + **Addendum A** (A2 curated set, A3 hosting/pinning, A4 this doc, A5 export, A7 done-bar).
- `config/artifact_lib_manifest.json` — the single cross-repo source of truth for `/lib/` origin + paths + versions.
- `src/personal_agent/tools/artifact_tools.py` — `_HTML_GENERATION_SYSTEM_PROMPT` (the distilled runtime prompt) + `artifact_draft` tool.
- `docs/skills/mermaid-diagrams.md` — sibling skill; the static-diagram portability lane.
- Linear: FRE-525 (Addendum A), FRE-527 (manifest + hosting), FRE-528 (prompt reframe), FRE-530 (export-to-standalone), FRE-531 (paged.js CSP confirmation + E2E live verify).
