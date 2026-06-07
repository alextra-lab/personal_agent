# ADR-0070: Output Channel Model — Markdown for Agents, Rich for Humans

**Status**: Implemented — FRE-368 shipped 2026-05-21 (PRs #66 + #67). D8 measurement window open; review gate ≥ 2026-06-04. **D7 (sandbox="") superseded by ADR-0089 D2/D3** — iframe is `sandbox="allow-scripts"` as of FRE-510 (PR #182, 2026-06-07).
**Date**: 2026-05-15
**Deciders**: Project owner
**Related**: ADR-0069 (R2-Backed Artifact Substrate), ADR-0064 (Inbound User Identity), ADR-0063 (Primitive Tools / Action-Boundary Governance), FRE-227, FRE-368, FRE-369, FRE-315 (PWA Mermaid block rendering — precedent), FRE-209 (approval modal — precedent)
**Implementation Plan**: `docs/superpowers/plans/implement-the-next-master-sharded-pinwheel.md`

---

## Context

In early May 2026 a public debate crystallized around the format question for LLM agent output:

- Anthropic engineer Thariq Shihipar argued that markdown was originally designed by John Gruber as a *source* format whose conversion target was always HTML — and that for human-facing agent output, HTML is the better final representation
- Google Research formally introduced **Generative UI** in November 2025: Gemini 3 in AI Mode and the Gemini app dynamically generate fully custom interactive interfaces per prompt. Human raters preferred generative-UI outputs over standard text/markdown
- **A2UI v0.9** (April 2026) proposed a framework-agnostic standard for declarative UI from agents
- The MiniApps research (arXiv 2603.09652) framed rendered HTML responses as a new human–LLM interaction paradigm
- Practical writeups (e.g., beam.ai analysis) put rough numbers on the tradeoff: HTML conveys ~10× more information per unit of attention than markdown, at ~3× the token cost
- Cloudflare in turn launched "Markdown for Agents" — a feature explicitly built to *strip HTML down* before feeding pages to AI systems

The common thread across both directions: **format should be chosen by who is reading**. Markdown is good for machines reading; HTML is good for humans reading.

Seshat's current pipeline is uniformly markdown end-to-end:

- `OrchestratorResult.reply: str` is plain markdown
- `MarkdownContent.tsx` in the PWA renders it via `react-markdown` + `remark-gfm` with a Mermaid code-fence interception precedent (FRE-315)
- Tool results are lossily summarized to markdown strings at `executor.py:2451` before reaching the model context
- Captain's Log reflections, memory storage, sub-agent communication, MCP tool results, Elasticsearch traces — all markdown or markdown-derived

ADR-0069 unlocks rich output technically by establishing the artifact substrate. This ADR decides the *semantic policy*: **which channels carry which format, and why** — and explicitly defers the orchestrator-level dual-representation refactor until measured usage justifies it.

Two failed framings preceded the accepted one. The first ("HTML for everything human-facing, markdown for everything agent-facing") collapsed two distinct human-facing surfaces (the transient chat reply and the persistent artifact) into one HTML channel and ignored the replay cost of HTML in the conversation transcript. The second ("markdown chat references HTML artifacts via URL") imposed an out-of-band context-switch on every rich output even when the content was genuinely conversational. The accepted framing preserves the strengths of both without their costs.

---

## Decision

Adopt a **three-channel output model** with the consumer determining the format, and a **hybrid chat surface** that scales from plain markdown to artifact cards. Defer the orchestrator-level dual-representation refactor until empirical signal justifies it.

### D1 — Three channels, consumer determines format

| Channel | Consumer | Format | Examples |
|---|---|---|---|
| **Agent ↔ agent** | LLMs, sub-agents, tools, memory, ES traces, captain's log, MCP wire | **Markdown** | Orchestrator context, message history, memory storage, delegation payloads, sub-agent results, MCP tool returns |
| **Agent → human, conversational** | The user, reading in real time | **Rich** (markdown + Mermaid + artifact cards) | The chat reply, tool-call surfaces, approval modals |
| **Agent → human, persistent** | The user, later (revisit, share, bookmark) | **HTML in R2** (per ADR-0069) | Reports, dashboards, charts, briefings, generated documents |

**The rule**: format depends on who is reading. Not what content is being expressed.

A 200-row comparison table viewed in chat now is rich (rendered as a styled table component or an inline card); the same content emitted to a Captain's Log entry is markdown (machine-replayed in context); the same content as a Friday briefing the user will revisit on Monday is HTML in R2 with a URL.

### D2 — Chat surface: three richness tiers, single surface

The chat surface (PWA `ChatMessage` → `MarkdownContent`) stays markdown-first with three progressive richness tiers:

| Tier | Content | Rendering |
|---|---|---|
| **Tier 1** | Prose, lists, tables, code blocks, blockquotes, inline emphasis | `react-markdown` + `remark-gfm` (existing) |
| **Tier 2** | Specialized enhancement blocks: ` ```mermaid `, future ` ```chart `, ` ```timeline ` | Code-fence interception + specialized component (existing pattern from FRE-315 / `MermaidBlock.tsx`) |
| **Tier 3** | Artifact references (URL in message body) | **Inline artifact card**: title, summary, optional thumbnail, click-to-expand or open-in-drawer |

Tiers 1 and 2 cover the vast majority of conversational rich output. Tier 3 handles content that warrants a stable URL (persistence), is too heavy for inline rendering (replay cost), or needs a viewer affordance the chat surface doesn't provide (full-screen modal, side drawer).

The artifact card is the unit of Tier 3 — not iframe-srcdoc embedding. See D5.

### D3 — Markdown stays canonical for agent context

`OrchestratorResult.reply: str` remains a single markdown field. Specifically:

- Agent context replay carries markdown (cheap, lossless, prompt-cache-friendly)
- Memory storage receives markdown summaries
- Captain's Log reflections store markdown
- Sub-agent results return markdown to the parent
- MCP tool results continue to be summarized to markdown per existing `_summarize_tool_result` (executor.py:2451)
- Elasticsearch traces and captain's log entries store markdown

The chat surface re-renders this markdown rich-side via PWA components (Tier 1/2), plus artifact-URL extraction (Tier 3 — detect artifact URLs in the markdown, render as cards). The model authors markdown; the PWA elevates it for human consumption.

### D4 — Dual representation is deferred until measured

A natural extension of the trend literature would be **`reply_markdown` + `reply_html`** as twin fields on `OrchestratorResult`, with the model authoring both per turn. This ADR explicitly defers that refactor. Justification:

1. **Capability uncertainty.** The local SLM (currently `qwen3-8b`) produces serviceable HTML but nothing distinguished; Claude- and GPT-4-class models produce excellent HTML. Whether dual-representation pays back depends on which model serves the turn — and the cost-vs-benefit is not stable across the model fleet.
2. **Context cost uncertainty.** With prompt caching active (Anthropic's prompt cache, OpenAI's input cache), the marginal cost of long markdown context is far lower than naive math suggests. Whether the dual-representation refactor pays back depends on actual cache hit rates, which are not yet measured.
3. **Empirical premise unverified.** The premise that "rich HTML in chat outperforms rich markdown + cards" is widely claimed in the trend literature but not yet validated in *this* harness with *this* user. FRE-368 is the experimental rig.
4. **Reversibility.** The dual-representation refactor is strictly additive to the substrate and the orchestrator state machine. It can be added later without disturbing anything decided here.

If FRE-368 ships and post-deployment evidence (subjective user feedback + objective measurement, per D8) indicates markdown + cards is insufficient, a follow-up ADR upgrades the orchestrator to dual representation. Until then, single markdown field.

### D5 — Artifact card is the Tier 3 unit, not iframe-srcdoc

Tier 3 content renders as **inline cards**, not as inline iframes with `srcdoc`. Cards are:

- **Small** — title + summary + optional ~64×64 thumbnail; ~200 bytes of metadata
- **Cheap to replay** — the chat transcript carries the artifact URL, not the artifact bytes
- **Composable** — a card can be tapped to expand inline, opened in a drawer, or opened in a new tab via "view standalone"
- **Uniform across content types** — HTML artifact, image, PDF, JSON, captured webpage — all render as a card with appropriate metadata and an appropriate viewer affordance

Inline iframe-srcdoc embedding is **rejected** for the same reason an inline 5MB HTML blob in every message is rejected: replay cost on every PWA mount, transcript scroll, and session resume — especially expensive on iOS PWAs where DOM size matters. The artifact URL pattern keeps the chat transcript small; the viewer (drawer/modal/inline expansion) loads heavy bytes only when invoked.

### D6 — Single surface with depth, not two screens

The expansion affordance for Tier 3 content is **single-surface progressive disclosure**:

- **Inline expand** — card grows in place; chat content reflows below; click again to collapse. Best for content that supports the conversation in progress (a comparison table the agent wants you to look at right now).
- **Drawer / bottom sheet** — slides in from the side on desktop (~60/40 split with chat) or up from the bottom on iOS PWA (~70% sheet with chat peeking at top). Dismiss with swipe-down or click-outside. Best for medium-weight artifacts requiring focus without abandoning conversation context.
- **Open standalone** — `artifacts.frenchforet.com/{id}` in a new tab via an explicit "view standalone" affordance. Optional, not primary. For *later* uses: revisit tomorrow, share with someone, pull up on another device.

There is no "second screen" mode within the chat session. The artifact's persistent URL (per ADR-0069) is what makes "later uses" cheap; the in-conversation experience never asks the user to context-switch.

### D7 — Sandboxing posture: documents not apps (default)

Following the Mermaid precedent (`securityLevel: 'strict'` in `MermaidBlock.tsx`, FRE-315), HTML artifacts render in iframes with `sandbox=""` (no scripts, no same-origin) by default. This:

- **Allows** rich documents: layouts, tables, SVG, callouts, color, embedded raster images, links
- **Disallows** script execution, popups, navigation, cross-origin fetches

**D7 amendment — Mermaid diagrams (FRE-396, 2026-05-28):** `artifact_draft` now supports Mermaid diagram markup. The sub-agent emits `<pre class="mermaid">…</pre>` blocks; the artifact pipeline converts them to static inline SVG server-side via `mmdc` before validation. The `<script>` ban and `sandbox=""` posture are unchanged — the output is always a script-free document. Render failures degrade gracefully to a `<pre>` fallback (never a crash).

Interactive artifacts (sliders, calculators, mini-apps with JS) are **out of scope** for this ADR. If interactivity is later required, it must:

- Be explicitly opted in via a fence-info marker (e.g., ` ```html interactive `) or artifact metadata flag (`interactive=true`)
- Pass governance approval per call (per ADR-0063 patterns)
- Be documented in an ADR amendment establishing the script-allowed posture

The "documents by default" stance matches the conservative-by-default posture established by ADR-0063 (action-boundary governance) and keeps the threat model small.

### D8 — Build to learn — measurement plan

FRE-368 (agent artifact tools) is the experimental rig. This ADR commits to gathering the following data before deciding any further direction:

| Question | Measurement | Source |
|---|---|---|
| How often does the agent choose to emit an artifact vs inline markdown? | Per-turn ratio of `artifact_write` invocations to total assistant turns | ES tool-call events |
| Is click-through on artifact cards friction or graceful disclosure? | Subjective user feedback + click-rate from PWA card-click telemetry | PWA telemetry + reflection |
| Are there content shapes Tier 1/2 cannot express but native HTML in chat could? | Curated examples from real usage; classify each as Tier 1/2/3 with justification | Manual review at the review gate |
| Does the SLM produce HTML good enough to make full-HTML-chat viable, or does it require Claude-tier? | Subjective quality check on artifact outputs from each model tier | Manual sampling |

The minimum useful observation window is **two weeks of regular use** post-FRE-368 deployment. The follow-up decision is captured as an open question in this ADR, not a scheduled gate. The decision branches:

- If **Tier 1/2/3 cover the content needs** → expand Tier 2 vocabulary (new enhancement blocks, richer cards). No orchestrator refactor.
- If **content consistently appears that requires native HTML in the chat reply** → write a follow-up ADR upgrading to dual representation (`reply_markdown` + `reply_html`) and execute the refactor.
- If **the SLM is the bottleneck** but Claude-class output is rich → the channel decision becomes model-aware: HTML-in-chat enabled for frontier models, markdown + cards for local SLM.

The point is to keep the next decision empirical, not architectural.

---

## Consequences

### Positive

- **Architecture stays small.** Single `reply: str` field, single Postgres `messages.content` column, single rendering pipeline. No dual-representation refactor before it's needed.
- **Mermaid precedent extends naturally.** Tier 2 is "more Mermaid-style enhancement blocks"; Tier 3 is "Mermaid-pattern for artifact URLs." Adding richness is incremental, not architectural — each addition is a self-contained PWA component.
- **Replay cost stays low.** Markdown context is cheap. Artifact bytes are not in the transcript. PWA mount, transcript scroll, and session resume remain fast on iOS PWA.
- **The agent's voice stays portable.** Sub-agents, future MCP integrations, memory consumers, captain's log readers all speak markdown — no model-specific HTML quirks bleed into agent-to-agent communication.
- **Channels decouple.** A new consumer (e.g., a future Slack notification surface, a CLI viewer, a third-party integration) renders the same markdown without needing to know about HTML / cards. The PWA elevates; the backend stays flat.
- **Reversible if wrong.** D4's deferred dual-representation is strictly additive; if FRE-368 measurement (D8) calls for it, the upgrade path is clean and uncontested.
- **Single-surface UX preserves attention.** No second-screen mode means the conversation flow is never interrupted, even when the agent produces something heavy enough to warrant its own viewer.

### Negative / Risks

- **The empirical claim is untested.** "Markdown + cards is sufficient for human-facing rich output" is a hypothesis. If D8 measurement shows it isn't, the deferred dual-representation work still has to happen — but no more cost than if it had been done speculatively now, and with the benefit of knowing it's the right move.
- **Tier 3 click-through has non-zero friction.** Even with progressive disclosure (inline expand, drawer), "tap card → see content" is more attention cost than "see content directly." Mitigation: inline-expansion option for content the agent expects the user to read immediately; reserve drawer for heavier artifacts.
- **Sandbox restrictions limit some content shapes.** No-script default rules out genuinely interactive widgets (e.g., a slider that re-renders a chart inline). If this becomes a real pain, D7 calls out the upgrade path.
- **Cards require thumbnails or summaries** the agent must author. Adds a small per-artifact prompt overhead. Acceptable: a one-sentence summary is a routine LLM ask, and the `summary` field in the `artifacts` table (per ADR-0069 D4) accommodates it.

### Neutral

- **Captain's Log and reflections** remain markdown-internal (D1, agent-to-agent channel). A future "weekly briefing to the human" is HTML in R2 (D1 channel 3), authored from the same captain's log data through the artifact substrate.
- **Mermaid rendering** is unchanged. It sits in Tier 2 and continues working as today.
- **The CLI** continues to render markdown via Rich. Tier 3 artifact cards degrade gracefully — the CLI prints the artifact URL as plain text. No crash, no broken UX, just a less-rich representation.

---

## Alternatives Considered

### A. HTML everywhere the agent emits, markdown only internally

*Rejected.* Forces the model to author HTML for every assistant turn, including purely conversational ones ("yes", "let me think about that"). Inflates output tokens with no benefit for messages that have no rich content. Also creates a re-rendering cost on every PWA replay if HTML is stored in the message column. The consumer-determines-format principle isolates the cost to where it pays back.

### B. Markdown everywhere; artifacts are the only HTML

*Rejected.* The earlier framing of this conversation, before the hybrid resolution. Fails the "click is a context switch" critique: forcing every piece of rich output to an out-of-band artifact imposes friction on content that's genuinely conversational (a small comparison table that supports the explanation in progress). Tier 2 (inline specialized blocks) solves this case without resorting to artifacts at all.

### C. Dual `reply_markdown` + `reply_html` from the start

*Rejected for now; D4 defers it explicitly.* Real cost (orchestrator schema change, message persistence change, PWA dual-render path, sub-agent passthrough question, model prompting changes) for unproven benefit. The empirical question — whether HTML in the chat-reply outperforms markdown + cards for *this* user with *this* model fleet — is not yet answerable. Ship FRE-368, gather data per D8, revisit.

### D. UI runtime / declarative components (A2UI / Montage pattern)

*Rejected for now.* The model emits a typed JSON declaration (e.g., `{type: "comparison", panels: [...]}`) and a trusted PWA runtime renders it from a fixed component catalog. Properties: smaller token cost than HTML, style/theme consistency, statically validable, governable.

Cost: define and evolve a schema, register PWA components, prompt the model to obey the schema, keep schema and renderer in sync across versions. Real engineering load, justified only when the surface stabilizes and the consistency win pays for the schema overhead.

This is a plausible future state — likely the next move *if* D4's dual-representation route also proves insufficient. Not the right move from here; ADR amendment territory.

### E. Inline iframe-srcdoc embedding (current "HTML in chat" pattern in some products)

*Rejected.* Replay cost on every transcript scroll and session resume. Mobile-hostile (DOM size on iOS PWAs). The artifact-card pattern achieves equivalent UX without carrying bytes inline.

### F. Mode-dependent format (e.g., HTML in NORMAL mode, markdown in DEGRADED)

*Rejected.* Adds an axis (mode) to a decision the consumer already determines (channel). The consumer-based rule is simpler; mode-aware behavior, if needed, applies at the governance layer (approval thresholds, rate limits per ADR-0063), not at the output-format layer.

### G. Per-tool default format declared in `tools.yaml`

*Rejected as redundant.* Tools that produce rich output (e.g., `artifact_write`) inherently produce rich content; tools that don't (e.g., `web_search`) inherently produce markdown summaries. Adding a `produces_format` field to governance config duplicates information already implicit in the tool's contract.

---

## Implementation Pointers

This ADR is realized incrementally across the substrate work and the consumer tickets:

- **FRE-227** (substrate) — provides the artifact substrate per ADR-0069; no chat-format changes
- **FRE-368** (agent artifacts) — adds `artifact_write` tool, the inline artifact card component in the PWA, sandboxed iframe viewer, single-surface drawer affordance
- **FRE-369** (user uploads) — bytes flowing the other direction; same card pattern displays upload chips in chat
- **Future**: a small follow-up issue when D8 measurement yields a decision — either "expand Tier 2 vocabulary" or "upgrade to dual representation per a new ADR"

No changes are required in this ADR's slice to: `OrchestratorResult`, message persistence schema, sub-agent protocol, MCP gateway, memory storage, captain's log, or insights pipeline. The model's job is unchanged (author markdown). The PWA's job extends incrementally (intercept more block types, render cards for artifact URLs).

---

## Verification

1. **Channel discipline**: every assistant message stored in Postgres `messages.content` is plain markdown (no HTML strings). Audited by a one-time query at FRE-368 ship time.
2. **Tier 1 + Tier 2 unchanged**: existing message rendering (prose, lists, tables, Mermaid) continues working after FRE-368 ships; no regression in the existing chat surface.
3. **Tier 3 round-trip**: agent calls `artifact_write` → assistant message contains the artifact URL → PWA detects the URL → renders inline card → click expands in drawer → standalone URL opens artifact behind Access (per ADR-0069 verification).
4. **Replay cost**: PWA mount of a 10-message session containing 3 artifact references measures `<1MB` DOM weight (no inline HTML payloads). Sanity-check via PWA dev tools.
5. **CLI fallback**: `uv run agent "make me a comparison table"` returns markdown text; if an artifact is referenced, the URL is printed without crash and without malformed output.
6. **Sandbox posture**: an HTML artifact containing `<script>alert(1)</script>` renders without executing the script (iframe `sandbox=""` enforced).
7. **Measurement instrumentation** (D8 prerequisite): `artifact_write` invocations are counted in ES with `tool_name=artifact_write`; PWA logs card-click events with a `card_click` telemetry event. Two-week post-deploy review captures the four D8 questions.
8. **Cross-consumer markdown**: a sub-agent result containing an artifact URL is correctly summarized to markdown when stored in parent context; no HTML leaks into the agent-to-agent channel.

---

## Related

- **ADR-0069** — R2-Backed Artifact Substrate (physical layer this ADR rides on)
- **ADR-0064** — Inbound User Identity via Cloudflare Access (auth for artifact URLs)
- **ADR-0063** — Primitive Tools / Action-Boundary Governance (sandbox/approval patterns D7 inherits)
- **FRE-315** — PWA Mermaid block rendering (Tier 2 precedent — pattern this ADR generalizes)
- **FRE-209** — Approval modal (Tier 3 card precedent — same component architecture)
- **FRE-227** — substrate implementation
- **FRE-368** — agent-side artifact tools (the experimental rig)
- **FRE-369** — user upload UX
- Discussion record: `docs/superpowers/plans/i-want-to-research-bubbly-shannon.md`
