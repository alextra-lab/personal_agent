---
name: artifact-design
description: 'Guides the authoring of a rich, self-contained HTML artifact: the curated /lib/ toolkit
  (KaTeX, Chart.js, three.js, highlight.js, OFL fonts, paged.js) and when to use a library or hand-roll
  inline, each library''s boot recipe and gotchas, publishing-grade native CSS typography, the sealed-box
  constraints, the portability decision, and the rule never to bake secrets into an artifact. Use when
  generating an HTML document artifact: reports, explorables, charts, math, 3-D scenes, code walkthroughs,
  or book and print layouts.'
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

A small, vetted shelf is hosted on the artifact origin (`https://artifacts.example.com`) —
it is the **only** external origin a sealed artifact may load. Reference each asset by its
**exact absolute, version-pinned URL** in its reference file; a relative `/lib/…` path is *not* the expected
reach form and is not counted as a satisfied capability by the demand meter. URLs are immutable
pins (ADR-0089 A3) — never edit a version in place. Source of truth:
`config/artifact_lib_manifest.json`.

Each library has its own file in `references/`, with its boot recipe and its gotchas. Before you
write the artifact, read the file of each library that it uses, for example
`read_skill(name="artifact-design", path="references/katex.md")`:

| Library | Use for | File |
|---|---|---|
| KaTeX (MIT) | math / formula typesetting | `references/katex.md` |
| Chart.js (MIT, global `Chart`) | charts / data visualization | `references/chartjs.md` |
| three.js (MIT, global `THREE`, r171) | 3-D / spatial / physics | `references/threejs.md` |
| highlight.js (BSD-3, global `hljs`) | code / syntax highlighting | `references/highlightjs.md` |
| OFL fonts (SIL OFL, variable woff2) | publishing-grade prose | `references/ofl-fonts.md` |
| paged.js (MIT), experimental and gated | book / print pagination | `references/pagedjs.md` |

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
- `font-feature-settings` requires the font to carry the features (the OFL faces of `references/ofl-fonts.md` do).

---

## Design-System Conventions

Define a complete design system in a `<style>` block in `<head>` (kept consistent with the
generation prompt so this doc is its superset):

- **Colors** as CSS custom properties: `--color-primary`, `--color-secondary`, `--color-accent`,
  `--color-bg`, `--color-surface`, `--color-text`, `--color-muted`.
- **Spacing scale**: `--spacing-1` … `--spacing-8` (0.25rem increments).
- **Typography**: `--font-sans`, `--font-mono` (plus the OFL `--font-serif`/`--font-display`
  of `references/ofl-fonts.md` when prose-grade); size classes `text-xs` … `text-3xl`.
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

## Success

Check the artifact before the answer is final:

- It is one complete `<!DOCTYPE html>…</html>` document of under about 200KB.
- Every external file is an exact absolute, version-pinned URL of the shelf. No other origin is used.
- No `fetch`, `XHR`, `WebSocket`, `sendBeacon`, storage or popup is used. All data, images and
  textures are inline.
- Each library that is used follows its reference file. For example, the KaTeX stylesheet link is
  present, the Chart.js canvas has `width` and `height`, and the three.js scene uses the global
  `THREE`.
- No secret, token, credential, session identifier or internal host name is in the file.

## Recovery

What to do on each failure, with a stop rule:

| Failure | Cause | Next step |
|---|---|---|
| Math shows as boxes | The KaTeX stylesheet `<link>` is missing | Add the stylesheet link (`references/katex.md`). |
| A chart is blank, blurry or 0×0 | The `<canvas>` has no `width` and `height` | Set both attributes, or wrap it and set `maintainAspectRatio: false` (`references/chartjs.md`). |
| A 3-D scene is blank, or a texture fails | A texture is fetched, or `import * as THREE` is used | Use a `data:` URI or a generated texture, and the global `THREE` (`references/threejs.md`). |
| Code has no colors | `hljs.highlightAll()` ran before the DOM was ready, or the theme `<link>` is missing | Call it on `DOMContentLoaded` and link the theme (`references/highlightjs.md`). |
| Text falls back to a system face | A font CDN is used | Declare the shelf faces with `@font-face` (`references/ofl-fonts.md`). |
| The artifact renders broken after it commits | A script, style or font comes from an origin that is not on the shelf | Inline it, or use the shelf library (Sealed-Box Constraints). |
| paged.js does nothing | The live policy can block it (`eval_gated`) | Use the native `@page` recipes (`references/pagedjs.md`, Native Typography). |
| The same failure twice | The cause is not found | Stop. Tell the user what failed and what was tried. |

## Maintenance

The distillation map (this skill to the generation prompt), the E2E verification runbook and the
reference links are in `references/maintenance.md`. Read it only when you change this skill, the
manifest or the generation prompt.
