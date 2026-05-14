# FRE-315 — Mermaid: rendering + authoring skill

**Linear**: [FRE-315](https://linear.app/frenchforest/issue/FRE-315) (Approved · Medium · Sonnet) — *update description to reflect expanded scope before implementation*
**Branch**: `fre-315-mermaid` (off `main`)

---

## Context

Mermaid support has two halves and the ticket originally only covered one:

1. **Rendering (PWA)** — the seshat-pwa chat UI currently shows ` ```mermaid ` fenced blocks as raw text. We integrate [mermaid.js v11](https://mermaid.js.org/) into `MarkdownContent.tsx` to render them as inline SVG figures.
2. **Authoring (agent skill)** — *added during planning*. Without explicit guidance the assistant produces mermaid that doesn't parse (mismatched arrows, reserved words used as node IDs, missing diagram-type header, etc.), which would route every diagram into the rendering fallback path and degrade the feature. A new `docs/skills/mermaid-diagrams.md` enforces syntactic correctness and diagram-type selection at authoring time.

Both halves ship in the same PR so the *first* user-visible mermaid diagram is also the first well-authored one. The Linear ticket description needs an edit to reflect the expanded scope before implementation begins.

---

## Target codebase

- **PWA**: Next.js 15 + React 18 + Tailwind. Bundled via Next/Webpack — no CDN tags.
- **Render path**: SSE → `seshat-pwa/src/hooks/useSSEStream.ts` → `StreamingChat.tsx` → `ChatMessage.tsx` → **`seshat-pwa/src/components/MarkdownContent.tsx`** (react-markdown + remark-gfm + prism syntax highlighting).
- **Integration point**: `MarkdownContent.tsx:90` — the `code()` component in `markdownComponents` already dispatches on language; one new branch handles `language === 'mermaid'`.
- **No test suite** in `seshat-pwa/` — verification is build + lint + manual browser check.
- **No existing mermaid dependency** anywhere in the repo (the only "mermaid" hits are diagrams *inside* ADRs).
- React-markdown does not enable `rehype-raw`, so no untrusted HTML reaches the DOM today; the new code preserves that property.

**Streaming note.** SSE delivers assistant tokens incrementally. The chosen behavior (render only on a closed fence) handles both the streamed and the all-at-once case identically — react-markdown does not emit a `language-mermaid` code node until the closing ``` arrives, so `MermaidBlock` only ever mounts with a complete source string.

---

## Files

### New

- `seshat-pwa/src/components/MermaidBlock.tsx` — lazy-loads `mermaid`, renders the diagram, falls back to a styled raw-code panel on parse error, includes a "figure / source" toggle.
- `docs/skills/mermaid-diagrams.md` — authoring skill (see "Authoring skill" section below for content).

### Modified

- `seshat-pwa/src/components/MarkdownContent.tsx` — one new branch in the `code()` handler returning `<MermaidBlock chart={…} />` when `language === 'mermaid'`; add the import at the top.
- `seshat-pwa/package.json` — add `"mermaid": "^11"` to `dependencies`.
- `seshat-pwa/package-lock.json` — refreshed via `npm install` inside `seshat-pwa/`.
- Linear FRE-315 description — edited to add the authoring-skill scope and link to this plan.

---

## `MermaidBlock` design

Component is marked `'use client'` (browser-only library, uses effects). Internal state:

- `status: 'loading' | 'rendered' | 'error'`
- `view: 'diagram' | 'source'` (auto-pinned to `'source'` on error)
- `svgMarkup: string` — populated from `mermaid.render()`
- `errorMsg: string`
- A stable per-instance id from `useId()` (mermaid requires unique render ids across the document)

**Lifecycle**, inside a `useEffect` keyed on the `chart` prop:

1. Dynamic-import the library (`(await import('mermaid')).default`). This is the laziness mechanism — Next.js code-splits mermaid into a chunk loaded only when a mermaid block first mounts, so initial bundle weight is unchanged for users who never see a diagram.
2. Call `mermaid.initialize({ startOnLoad: false, securityLevel: 'strict', theme: 'dark' })`. Idempotent across instances. `securityLevel: 'strict'` is mermaid's documented XSS guard — it strips embedded HTML and disables click bindings *during parsing*, before the SVG is serialized. This is the trust boundary the design relies on.
3. Call `mermaid.render(id, chart)` and store the returned `svg` string in state.
4. On exception: capture `err.message`, switch to source view, set status to `error`.
5. Cancellation flag in the effect closure to avoid setState after unmount when `chart` changes mid-render.

**Rendering**:

- **`loading`** — subtle "rendering diagram…" placeholder inside the standard header/border frame.
- **`rendered` + diagram view** — inject the SVG string returned by mermaid into a wrapper `<div>` using React's standard mechanism for setting trusted HTML on an element. **Trust justification**: the injected string is *not* the user's raw chart source — it is the SVG mermaid emitted *after* parsing the chart under `securityLevel: 'strict'`. The boundary is "trust mermaid's sanitized output," not "trust the upstream LLM string." This is the approach the official mermaid React integration documents. Layering DOMPurify on top is possible but redundant under strict mode; if the reviewer prefers belt-and-braces, the implementation can run the SVG through DOMPurify with `USE_PROFILES: { svg: true, svgFilters: true }` — call this out in the PR description.
- **`rendered` + source view** — render the raw `chart` through the existing `<SyntaxHighlighter>` (imported from `react-syntax-highlighter` exactly as `CodeBlock` does at `MarkdownContent.tsx:6-7,72-84`), keeping visual parity with normal code fences.
- **`error`** — small red note line with `errorMsg` truncated to ~120 chars, followed by the source view. This is the graceful fallback the ticket requires.

---

## Visual design direction

The PWA's established aesthetic is **refined Claude.ai-style dark minimalism**: a slate palette (`#0f172a` / `#1e293b` / `#334155`), `#3b82f6` as the only accent, the `pulse-dot` keyframe for live indicators, and `@tailwindcss/typography` with backticks stripped. The mermaid block should feel like a *native* extension of that vocabulary — visually distinct from code (so diagrams read as artifacts, not transcripts) without inventing a new design language.

**Concept**: code is a dense technical artifact; a diagram is a *figure*. Treat it like a figure in editorial layouts — slightly elevated surface, generous breathing room, restrained chrome.

### Differentiation from `CodeBlock`

| | `CodeBlock` (existing) | `MermaidBlock` (new) |
|---|---|---|
| Surface | Flat `#0d1117`, edge-to-edge | Subtly elevated: `bg-slate-900/40` with a faint vertical gradient (`from-slate-900/60 to-slate-900/20`) for depth |
| Padding | None (syntax-highlighter sits flush) | `p-6 sm:p-8` around the SVG, `flex items-center justify-center` so small diagrams center rather than left-pin |
| Border | `border-slate-700/50` | `border-slate-800/70` + an inner `ring-1 ring-inset ring-slate-700/30` for the figure-card feel |
| Header chrome | Full bar with language label + Copy | Slim bar: tiny mermaid sigil + label `figure`, toggle right-aligned. No copy button (source view already shows copyable text through `CodeBlock` styling) |
| Header separator | `border-b border-slate-700/50` | `border-b border-slate-800/60` (lighter — less chrome) |
| Vertical rhythm | `my-2` | `my-3` (figures want more breathing room than code) |

### Custom mermaid theme (matches PWA palette)

Instead of mermaid's stock `'dark'`, initialize with `theme: 'base'` + `themeVariables` so diagrams use the seshat palette and the `#3b82f6` accent for primary edges/nodes:

- `primaryColor`: `#1e293b` (slate-800) — node fills
- `primaryTextColor`: `#e2e8f0` (slate-200) — node labels
- `primaryBorderColor`: `#3b82f6` (seshat.accent) — node borders, *the only non-slate color*
- `lineColor`: `#475569` (slate-600) — edges
- `secondaryColor`: `#334155` (slate-700) — secondary fills
- `tertiaryColor`: `#0f172a` (seshat.dark) — bg
- `fontFamily`: inherit the body font (don't introduce a new typeface)

The result: diagrams look intentional and native, not like a generic mermaid embed.

### Loading state

Reuse the existing `animate-pulse-dot` keyframe defined in `tailwind.config.ts:21`. Three `1.5×1.5` slate-600 dots with staggered `animation-delay` of `0ms / 200ms / 400ms`, centered in a `min-h-[120px]` panel matching the eventual diagram area. No text label — visual continuity with the PWA's other live indicators.

### Toggle interaction

- Toggle button: same dimensions as the existing Copy button but no icon — pure type. `text-xs`, `text-slate-500 hover:text-seshat-accent` (introduces the accent only on intent). Text: `figure ↔ source`.
- View transition: 150ms cross-fade between diagram and source. Keep it restrained — this is a figure toggle, not a gimmick. CSS `transition-opacity` on a wrapper, paired with the `view` state.

### Error state

Refined, not alarming. Single line above the source view with:

- A `border-l-2 border-rose-400/60` left bar (no full red panel).
- `text-xs text-rose-300/80` for the truncated `errorMsg`.
- The source view renders below it through the existing `CodeBlock` styling — the user sees exactly what the assistant produced, with a quiet annotation of why it didn't render.

### Mermaid sigil (small icon for the header)

A 12×12 inline SVG: two concentric diamond outlines in `currentColor` at 60% opacity. Marks the block as a diagram without leaning on a mermaid-branded logo. Consistent with the existing geometric icon vocabulary (`CopyIcon`, `CheckIcon` at `MarkdownContent.tsx:14-29`).

### Why not bolder?

The PWA is a chat client embedded in a research project — bold maximalism would fight the editorial calm of the surrounding messages and make diagrams feel like ads. The refinement choices above (figure framing, custom palette, accent-on-intent, hushed error states) raise the perceived design quality without disrupting context. Intentionality, not intensity.

---

## Authoring skill — `docs/skills/mermaid-diagrams.md`

### Hygiene precedent

The runtime skill system (distinct from Claude Code's plugin skills) loads markdown files from `docs/skills/` and routes the agent to them via the skill router (`get_skills_for_query`, the `skill_index_directive`, and ADR-0066/0067). Authoring this skill follows the project's established hygiene, cross-referenced from these sources:

- **`docs/skills/SKILL_TEMPLATE.md`** — the canonical template. Required field: `name`. All other frontmatter fields are optional.
- **`docs/skills/self-telemetry.md`** — most recent fully-featured example (FRE-356, 2026-05-10). Uses the full frontmatter set (`name`, `description`, `when_to_use`, `tools`, `nudge`, ~50 keywords). Match this style.
- **`docs/skills/seshat-knowledge.md`** / **`seshat-observations.md`** — minimal-frontmatter precedent (just `name` / `description` / `when_to_use`). Suitable when the skill is invoked by direct reference rather than via the router.
- **ADR-0066** (skill routing) and **ADR-0067** (nudge directive injection) — define the `nudge:` field and the threshold/feedback loop. New skills with prescriptive behaviors (e.g. "emit the type header on line 1") should set `nudge`.
- **`docs/skills/EMPIRICAL_TEST_RESULTS.md`** — methodology note (FRE-284): future doc-gating must exercise recipes through the actual agent tool API, not via `docker exec`. We borrow that test posture for verification below.

**One hygiene point to flag for the implementer:** this is the project's **first authoring-only skill** — every existing skill documents how to *invoke* a primitive tool (`bash`, `read`, `run-python`, etc.). Mermaid-diagrams provides pure authoring guidance with no tool side-effects. Two compatible options:

1. **Omit `tools`** — the template marks it optional and the field is informational; the loader does not require it. *Preferred*, signals "this is guidance, not invocation."
2. **`tools: []`** — explicitly empty. Slightly noisier but unambiguous.

The implementer should verify which choice the loader accepts cleanly during the boot-time validation step (verification §1 below). If the loader emits a warning on either choice, fall back to the other and note it in the PR description.

### Frontmatter

- `name`: `mermaid-diagrams`
- `description`: One sentence — "Author syntactically-correct mermaid diagrams of the right type for the user's intent (flowchart, sequence, state, ER, gantt, class, pie, mindmap)."
- `when_to_use`: "When the user asks for a diagram, flowchart, sequence diagram, state machine, schema, timeline, mindmap, architecture sketch, or 'visualize this' — or when explanation would land more cleanly as a picture than as prose."
- `tools`: omit (preferred) or `[]` — see hygiene note above.
- `nudge`: `"Emit the mermaid fence with a diagram-type header on the first content line. Do not invent syntax — every arrow, keyword, and bracket must be from the v11 spec."` (per ADR-0067).
- `keywords`: ≈30 entries (target the breadth of `self-telemetry.md`'s ~50-entry list) — mermaid, diagram, flowchart, sequence diagram, state machine, ER, gantt, class diagram, pie chart, mindmap, visualize, sketch, draw, architecture diagram, graph, flowchart syntax, sequence syntax, schema diagram, timeline, decision tree, swimlane, …

### Body content (sections, in order)

1. **When to use a diagram vs prose.** Diagram if: ≥3 entities with non-linear relations; temporal ordering matters; a state machine has ≥3 states; a decision tree branches. Prose if: linear narrative, single entity, the diagram would add chrome without information.

2. **Picking the right type — decision table.**

   | Intent | Type | First-line header |
   |---|---|---|
   | Flow / decision / process | flowchart | `flowchart TD` (or `LR`) |
   | Actors exchanging messages over time | sequence | `sequenceDiagram` |
   | States and transitions | state | `stateDiagram-v2` |
   | Entities + relationships (DB schema) | ER | `erDiagram` |
   | Timeline / project plan | gantt | `gantt` |
   | OO model / class hierarchy | class | `classDiagram` |
   | Proportions of a whole | pie | `pie` |
   | Hierarchical brainstorm | mindmap | `mindmap` |

3. **Syntax invariants** (the most-broken rules, called out explicitly):
   - The first non-empty line MUST be the diagram-type header. Without it, mermaid v11 throws `No diagram type detected`.
   - Node IDs in `flowchart` are bare tokens; if a label contains spaces or punctuation, put the label in brackets: `A[User clicks "Buy"]`, not `User clicks "Buy" --> B`.
   - Arrow syntax differs by diagram type. `flowchart`: `-->`, `-.->`, `==>`, `--text-->`. `sequenceDiagram`: `->>`, `-->>`, `-x`. Don't mix.
   - Reserved words (`end`, `class`, `subgraph`, `direction`, `default`) cannot be used as bare node IDs in `flowchart` — bracket them or rename.
   - `subgraph` blocks must close with `end` on its own line.
   - In `sequenceDiagram`, participants should be declared explicitly (`participant Alice`) when their names contain spaces; otherwise inline use is OK.
   - In `stateDiagram-v2`, `[*]` is start/end; transitions use `-->`; nested states use `state X { ... }`.
   - `erDiagram` cardinality glyphs: `||--o{` (one-to-many), `||--||` (one-to-one), `}o--o{` (many-to-many). Don't invent variants.

4. **Conventions for the seshat PWA specifically:**
   - Keep diagrams ≤30 nodes — bigger ones overflow the chat column.
   - Prefer `TD` (top-down) for ≤6 nodes, `LR` (left-right) for chains, never `BT` / `RL` (uncommon, can confuse readers).
   - Don't set `%%{init: ...}%%` directives — the PWA injects its own theme. Theme directives in the fence override it and produce off-palette diagrams.
   - Don't embed click handlers (`click NodeId href "..."`) — strict mode strips them, and they'd be silently broken anyway.

5. **Verifying mentally before emitting.** A 4-step checklist: (a) first line has the diagram-type header; (b) every arrow uses the syntax for that type; (c) every label with spaces/punctuation is bracketed; (d) `subgraph` opens have matching `end`s.

6. **Three worked examples** — one flowchart, one sequence, one state diagram — each ≤8 lines, demonstrating bracketed labels, the type header, and a non-trivial arrow form. Each is small enough to be obviously correct.

7. **Failure recovery.** If the user reports a diagram didn't render: ask for the rendered error from the PWA's source-view fallback (the error message is shown above the source); diagnose against the syntax-invariants section above; re-emit the corrected fence in a new message.

### Skill verification

After authoring the skill file:

- Confirm it loads via the existing skill router by running a probe prompt that mentions "flowchart" — the router (`get_skills_for_query`) should rank `mermaid-diagrams` in the top results.
- Spot-check that `docs/skills/SKILL_TEMPLATE.md` fields are all present (frontmatter validator runs as part of the gateway boot — any missing required field will surface at startup).
- Read one of the worked examples back through mermaid.live to confirm it parses cleanly under v11 before committing.

**Other details**:

- Use `useId()` (React 18) for the render id rather than `Math.random()` — deterministic across StrictMode double-invocation.
- Re-run the effect whenever `chart` changes (`[chart]` dependency array).
- The `chart` prop value is `String(children).replace(/\n$/, '')` — same trim the existing `CodeBlock` branch applies.

---

## `MarkdownContent.tsx` change

Insert at the top of the `code` handler at **MarkdownContent.tsx:90**, before the `isInline` check:

> when the node is a fenced (non-inline) block whose `language === 'mermaid'`, return `<MermaidBlock chart={String(children).replace(/\n$/, '')} />`.

Add `import { MermaidBlock } from './MermaidBlock';` at the top of the file. Everything else in `MarkdownContent.tsx` is unchanged.

---

## Verification

### Rendering half (PWA)

Run from `/opt/seshat/seshat-pwa`:

1. `npm install` — confirms `mermaid` resolves and lockfile updates cleanly.
2. `npm run lint` — must pass with no new warnings.
3. `npm run build` — must compile cleanly; check the Next build output for a separate `mermaid` chunk (confirms lazy-load works).
4. `npm run dev`, open the PWA in a browser, drive one of:
   - paste a mermaid fence into a synthetic assistant message via dev tools, OR
   - run a live agent prompt asking for a mermaid diagram against the local gateway.

Manual browser checks (golden path + edge cases):

- ✅ `flowchart TD` renders as SVG with the seshat palette (slate fills, blue-500 borders, slate-200 labels).
- ✅ `sequenceDiagram` renders.
- ✅ `gantt` renders.
- ✅ Toggle switches between figure and source views with the 150ms cross-fade.
- ✅ Malformed mermaid (e.g. `flowchart TD\nA --` truncated) shows the fallback raw-code view with the rose-tinted error annotation above it.
- ✅ Non-mermaid code blocks (`bash`, `python`, etc.) still render through `CodeBlock` unchanged.
- ✅ Initial page load Network tab does NOT include the mermaid chunk; chunk only loads after the first mermaid block appears.
- ✅ No console errors; rendered SVG has no `<script>` nodes (confirm via DevTools inspector — validates the `securityLevel: 'strict'` boundary).
- ✅ Loading state shows the three pulsing dots, not a text placeholder.

### Authoring half (skill)

Run from `/opt/seshat`:

1. Gateway boot picks up the new skill — restart the gateway and confirm `mermaid-diagrams` appears in the skill index logged at startup (no frontmatter validation errors).
2. Probe routing: ask the agent "draw me a flowchart of how a PR lands" — the skill router should select `mermaid-diagrams` (visible in the `skill_index_directive` injected into the system prompt for that turn).
3. End-to-end: same prompt, with the PWA open. Expected outcome: assistant emits a fenced ` ```mermaid ` block with a `flowchart TD` first line and bracketed labels; PWA renders it as a figure on first try (no fallback).
4. Each of the three worked examples in the skill body parses cleanly at https://mermaid.live before commit.

---

## Out of scope

- PNG export of rendered diagrams.
- Theme switching beyond the chosen `theme: 'dark'` default (the PWA is dark-mode-only today).
- Server-side rendering of diagrams (Next can't run mermaid SSR without DOM polyfills; client-only is the documented pattern).
- Cleanup of the stale `project_pwa_config_console.md` user-memory note (it claims a PWA Config Console is a future need, but the PWA already exists for chat) — covered separately under memory hygiene.

---

## Commit / PR

- Feature branch + PR per the project convention for code changes.
- Commit message: `feat(pwa): render mermaid fences inline (FRE-315)`.
- PR body: link FRE-315, screenshot of a rendered flowchart, note about the new lazy-loaded chunk size from the build output.
- After merge: update `docs/plans/MASTER_PLAN.md` — move FRE-315 from "Immediately Actionable" to "Recently Completed" and bump the "Last updated" line. Then proceed to FRE-343 per the agreed task order (315 → 343 → 314 → 349).
