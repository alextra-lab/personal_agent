# FRE-532 — PWA toolkit convergence: align rendering libs (share curation, not hosting)

**Ticket:** FRE-532 (Approved · Tier-2:Sonnet · project "Artifact Execution Security")
**Refs:** ADR-0089 Addendum A6 · PR #188 · substitution map `config/artifact_lib_substitution_map.json` (FRE-527 version reference)
**Related:** FRE-527 (toolkit host), FRE-525 (toolkit research), FRE-549 (PWA export trigger)

## Goal

Align the PWA chat's rendering libraries + **versions** with the curated artifact toolkit,
**bundling the PWA's own pinned npm copies** (share the *curation*, not the *hosting*). Close the
chat-math gap by adding KaTeX. **Hard rule:** the PWA (high-trust origin) must never load executable
scripts from the artifact origin (untrusted LLM bytes). Everything stays npm-bundled under the PWA's
own origin — which the current code already does; this ticket keeps it that way while converging
lib choices + versions.

## Current state (the divergence)

| Concern | PWA today | Toolkit (substitution map) | Decision |
|---|---|---|---|
| Code highlight | `react-syntax-highlighter` (Prism) + `prismjs` | `highlight.js@11.9.0` + `github-dark` | **Switch to highlight.js@11.9.0 + github-dark** |
| Diagrams | client `mermaid ^11` (npm, dynamic import) | server-rendered SVG (artifact pipeline only) | **Keep client mermaid; pin `11.15.0`** |
| Math | **none** (chat LaTeX unrendered) | `katex@0.16.11` | **Add `katex@0.16.11` + remark-math + rehype-katex** |

### Decision rationale

- **highlight.js (not prismjs):** the toolkit pins `highlightjs@11.9.0` with `github-dark.min.css`.
  Aligning means matching lib + version + theme exactly. Removing `react-syntax-highlighter` /
  `prismjs` also removes the divergence the ticket calls out and drops the prismjs CVE override.
- **Mermaid stays client-side:** server-SVG exists *only* inside the artifact-generation pipeline
  (`src/personal_agent/tools/artifact_tools.py`), where the origin is untrusted and exported
  artifacts must be self-contained. The PWA is the **high-trust** origin already running client JS;
  a self-bundled, pinned mermaid satisfies the hard rule and preserves the existing rich
  `MermaidBlock` (SVG/PNG export, view-source). Converging chat onto server-SVG would mean new
  backend endpoint work and a UX regression for zero security gain. Pin `^11 → 11.15.0` (currently
  resolved) for reproducibility — mermaid is not in the toolkit, so there is no version to match,
  only a pin to add.
- **KaTeX 0.16.11:** matches the toolkit pin exactly; `remark-math@6` + `rehype-katex@7` are the
  unified-v11 majors compatible with `react-markdown@9`. KaTeX CSS + its woff2 fonts are
  npm-bundled and served from the PWA origin (passive, same-origin).

## Files

### Changed
- `seshat-pwa/package.json`
  - deps: **remove** `react-syntax-highlighter`; **add** `highlight.js: 11.9.0`,
    `katex: 0.16.11`, `remark-math: ^6.0.0`, `rehype-katex: ^7.0.1`; **change**
    `mermaid: ^11 → 11.15.0`.
  - devDeps: **remove** `@types/react-syntax-highlighter`, `prismjs`; **add** `@types/katex: ^0.16.7`.
  - `overrides`: **remove** `prismjs` (keep `postcss`).
- `seshat-pwa/src/app/layout.tsx`
  - add `import 'katex/dist/katex.min.css'` and `import 'highlight.js/styles/github-dark.css'`
    **after** `import './globals.css'` — keeps third-party global CSS order deterministic relative
    to the Tailwind layers (avoids FOUC / specificity races a leaf-component import can cause; per
    codex review).
- `seshat-pwa/src/components/MarkdownContent.tsx`
  - drop Prism imports.
  - add `remarkMath` to `remarkPlugins`, `rehypeKatex` to a new `rehypePlugins`.
  - `CodeBlock` body: replace `<SyntaxHighlighter>` with `<CodeHighlight language code />`.
- `seshat-pwa/src/components/MermaidBlock.tsx`
  - drop Prism imports; render the source-fallback view with `<CodeHighlight>` instead of
    `<SyntaxHighlighter>`.

### New
- `seshat-pwa/src/components/CodeHighlight.tsx` — minimal highlight.js wrapper (shared by
  MarkdownContent + MermaidBlock). Uses `highlight.js/lib/common` (the toolkit's `cdn-assets`
  "common" build is *generated from* this same 11.9.0 `lib/common`, so the language set matches by
  construction). Language dispatch, hardened per codex review:
  - `language && hljs.getLanguage(language)` → `hljs.highlight(code, {language, ignoreIllegals:true})`
  - else → `hljs.highlightAuto(code)`
  - wrap in `try/catch`; on throw fall back to the escaped plain text (hljs throws on an
    unregistered language — never let it bubble).
  Output is hljs-escaped HTML rendered via `dangerouslySetInnerHTML` on `<code class="hljs">`
  (standard hljs React pattern; hljs escapes any `<`/`>`/`&` in the source, so a `<script>` inside a
  code fence is rendered as inert text, never a live node).
- `seshat-pwa/src/__tests__/MarkdownContent.test.tsx` — render assertions (math + code + mermaid route).
- `seshat-pwa/src/__tests__/toolkit-convergence.test.ts` — package.json version/lib guard.

## Steps (TDD)

1. **Write failing tests.**
   - `MarkdownContent.test.tsx`:
     - inline math `$E=mc^2$` → `container.querySelector('.katex')` not null.
     - display math `$$\int_0^1 x\,dx$$` → `.katex-display` not null.
     - fenced ` ```python\ndef f():\n    pass\n``` ` → `code.hljs` present and a `.hljs-keyword` span exists.
     - fenced ` ```fakelang\nx=1\n``` ` (unknown language) → renders without throwing (highlightAuto path).
     - fenced code containing `<script>alert(1)</script>` → no live `<script>` node; text present escaped.
     - fenced ` ```mermaid\ngraph LR;A-->B\n``` ` → renders `MermaidBlock` (figure label present), no throw.
   - `toolkit-convergence.test.ts`: deps.katex===`0.16.11`, deps['highlight.js']===`11.9.0`,
     `remark-math`/`rehype-katex` present, no `react-syntax-highlighter`/`prismjs` anywhere.
   - **Verify fail:** `cd seshat-pwa && npm install && npm run test` → the two new files fail.
2. **package.json** edits (as above) → `npm install` to regenerate `package-lock.json`.
3. **CodeHighlight.tsx** new component.
4. **MarkdownContent.tsx** — plugins + CSS imports + CodeBlock swap.
5. **MermaidBlock.tsx** — source fallback swap; remove Prism.
6. **Verify pass:** `npm run test` (all green), `npm run build` (compiles), `npm run lint` (clean).

## Verification / quality gates

- `cd seshat-pwa && npm run test` — all pass (new + existing).
- `cd seshat-pwa && npm run build` — Next build succeeds (CSS imports resolve, no Prism refs).
- `cd seshat-pwa && npm run lint` — clean.
- `make ruff-check` / `make mypy` — unaffected (no Python touched); run for the checklist.
- `pre-commit run --all-files` — `check_no_personal_paths` clean.
- **Hard-rule check:** `grep -rn "artifacts.example" seshat-pwa/src` shows only the existing
  artifact-*card/URL-detection* references (passive data/links), **no `<script src>` to the artifact
  origin** — structurally guaranteed since all libs are npm deps.

## Accepted trade-offs (codex review)

- **KaTeX duplication:** mermaid carries a transitive `katex ^0.16.25`; our top-level `katex@0.16.11`
  nests cleanly (regular dep, not a peer → no install failure). mermaid uses its own copy lazily;
  chat math uses ours. Benign extra bytes only inside the already-dynamic mermaid chunk.
- **Bundle size:** net-neutral — drops `react-syntax-highlighter` + `prismjs` + `refractor`, adds
  `highlight.js/lib/common`. No analyzer is configured; adding one is out of scope.

## Out of scope / follow-ups

- No CSP header changes (PWA CSP is enforced at the edge, not `next.config.js`).
- No e2e/Playwright additions (vitest unit coverage suffices for acceptance; browser-visual checks —
  KaTeX fonts, FOUC, mermaid error boundary — are master's post-merge live verification).
- FRE-549 (export trigger wiring) is separate.
```
