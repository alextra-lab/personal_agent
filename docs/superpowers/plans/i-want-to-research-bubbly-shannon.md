# Research note: HTML5 / Generative-UI output for the agent

> **Status**: superseded by **ADR-0069** (R2-Backed Artifact Substrate) and **ADR-0070** (Output Channel Model — Markdown for Agents, Rich for Humans), both 2026-05-15.
> This file is preserved as the discussion record that produced those ADRs. For decisions, read the ADRs. For the reasoning trail and alternatives that were considered along the way, read on.
>
> Implementation tickets: **FRE-227** (substrate), **FRE-368** (agent artifact tools), **FRE-369** (user upload UX), **FRE-370** (Cloudflare Access 720h session duration — operational follow-up).
>
> Originally drafted 2026-05-15 as research / discussion only — not a build plan.

## What the trend actually is

Three distinct ideas often collapsed into one phrase:

1. **HTML-over-Markdown for final agent output.** Thariq Shihipar (Anthropic) and Addy Osmani's argument: markdown was a *source* format, HTML was always the target. For human-facing reports, HTML carries ~10× the information per token of "shape" (layout, tables, SVG, styling) at the cost of ~3× more tokens.
2. **Generative UI (Google Research, Nov 2025).** The model synthesizes a *bespoke* interactive UI per prompt — not just a styled doc but a tool/widget/game/sim. Gemini 3 in AI Mode is the production exemplar.
3. **MiniApps / UI runtimes (A2UI v0.9, Montage, etc.).** The model doesn't emit HTML; it emits a *declaration* and a trusted runtime renders it. Decouples authorship from rendering, gives the host control over component catalog and theme.

These are a stack, not alternatives: HTML output is the floor, generative UI is the ceiling, UI runtimes are how you'd ship the ceiling safely at scale.

## What the codebase makes possible right now

Mapping the trend onto what exists in `/opt/seshat`:

| Layer | Current state | What this means for HTML output |
|---|---|---|
| Orchestrator | `OrchestratorResult.reply: str` (flat string) | All five surfaces share one channel. No typed content blocks. Any "rich" content rides inside that string today. |
| AG-UI transport | Typed event union (`TextDeltaEvent`, `ToolStartEvent`, `ToolApprovalRequestEvent`, …) in `transport/agui/events.py` | Already discriminated; adding a `ContentBlockEvent` would be a natural extension, not a redesign. |
| PWA | `MarkdownContent.tsx` already intercepts ` ```mermaid ` code fences and routes to `MermaidBlock.tsx` (FRE-315) — lazy load, SVG-in-HTML, download/copy actions, strict security. | A working precedent for "intercept a fence, render a specialized component." HTML support is the next iteration of the same pattern, not new architecture. |
| Tools | Tier-1 native Python is the default (ADR-0028). Sandbox tool already emits `scratch_files: list[str]`. | If we want an *artifact* model rather than inline blocks, the seam exists — but there is no `/artifacts/*` static route yet. |
| CLI | `Rich.Markdown` over a `response` string. | HTML degrades to "show source" gracefully. Acceptable as a fallback. |
| Reflection / Captain's Log | Text-only output stored in ES. | Untouched by the trend — but this is arguably where generative UI would *earn its keep* (dashboards beat paragraphs for trend data). |

The mermaid precedent is the most important fact here: the team has already accepted "LLM emits a typed code fence → PWA renders a sandboxed specialized component." HTML5 output is the same shape with a wider blast radius.

## The interesting design questions

These are what's worth discussing — not "should we ship X by Y" but where the real tradeoffs live.

### 1. Inline vs artifact

Inline (` ```html ` fence → `<iframe srcdoc>`) keeps everything in the message. Cheap, no orchestrator changes, mirrors mermaid. But:

- Re-rendered on every transcript scroll → cost of carrying a 5 KB HTML blob through every replay
- Not shareable as a URL
- LLM can't *reference* a prior artifact ("update the dashboard I made") — it's just text in scrollback

Artifact (`render_ui` tool → file → `/artifacts/{id}` route) is the inverse: more plumbing (tool + route + governance + PWA card), but addressable, persistent, referenceable, and decoupled from the message stream — which matters more for the **dashboard / Captain's-Log** use case than for one-shot styled answers.

Real question: **how often does the agent want to produce something the user will look at twice?** If "rarely" → inline is sufficient forever. If "this becomes how the agent reports", artifacts are the right substrate.

### 2. JS or no JS

The bench-test of "generative UI" claims is interactivity. Without `allow-scripts`, you get rich *documents* (tables, layouts, SVG charts) but not *apps* (sliders, calculators, mini-tools). Three plausible postures:

- **Documents-only forever.** Lowest risk, covers 80% of the value, mirrors the mermaid security stance.
- **Documents by default, scripts opt-in.** Fence info-string like ` ```html interactive ` gates the scripted variant through governance. Aligns well with the existing mode-aware policy in `tools.yaml`.
- **Scripts on by default with `sandbox="allow-scripts"`.** The MiniApps / generative-UI vision in full. Bigger attack surface even with a sandboxed iframe (popups, navigation, fetch to external origins) — would warrant an ADR.

Real question: **is the agent's job to make documents or to make tools?** Today it's mostly the former, but the second-brain / homeostasis / brainstem story arguably wants the latter (a "show me the current mode and budgets" interactive panel is a tool, not a doc).

### 3. Author HTML directly, or call a UI runtime

The Montage / A2UI argument: the LLM is bad at HTML/CSS authoring and good at semantic intent. Better to have it emit a JSON declaration ("dashboard with 3 cards: budget, mode, recent gates") and a typed React runtime in the PWA renders it from a fixed component catalog. Trade:

- Pro: smaller token cost, no style drift, no escaping bugs, designer can theme centrally
- Pro: governable in a way raw HTML never is — you can statically validate the declaration
- Con: build cost (define a schema, register components, evolve them) — and the gateway needs to know the schema to prompt well
- Con: loses the "any frontier model can do this with zero integration" property of raw HTML

This matters more for the **Captain's-Log dashboard** end of the spectrum than for one-off styled answers, because that's where you want consistency across renders.

### 4. Where does this earn its keep first

Three candidate first uses, in increasing order of architectural impact:

1. **One-shot styled answer.** "Compare these three approaches in a table with code samples" → HTML > markdown. Pure inline-fence territory. Today, blocked by nothing except prompt + PWA wiring.
2. **Tool-result formatting.** `run_python` produces a matplotlib plot; today it's a scratch file path. Could be auto-rendered as an HTML/SVG artifact. Bridges the sandbox's existing artifact concept to the PWA.
3. **Reflection dashboard.** Captain's Log weekly summary as a single self-contained HTML page with charts. This is where text genuinely fails and HTML genuinely wins, but it touches the orchestrator-vs-background-job boundary and needs its own design.

## Open threads worth pulling on next

- Whether the agent's "voice" should ever produce HTML, or whether HTML is something *tools* produce and the agent embeds. (This is the inline-vs-artifact question reframed.)
- Whether the `OrchestratorResult.reply: str` channel should evolve to `content_blocks: list[ContentBlock]` independently of HTML — that change has value for tool-call surfacing, citations, and approval prompts regardless of HTML.
- Whether the existing AG-UI typed event union is the right place to add `ContentBlockEvent`, or whether it should ride inside `TextDeltaEvent` as protocol-transparent markdown.
- Where the **UI runtime** idea sits relative to the existing skill system — both are "trusted components the model invokes from a catalog."

## Not in scope of this note

- File paths, atomic steps, verification checklists, Linear issue plumbing — this is a research note, not an implementation plan. If a thread becomes the real work, that gets a separate `docs/superpowers/plans/YYYY-MM-DD-fre-XXX-*.md`.
