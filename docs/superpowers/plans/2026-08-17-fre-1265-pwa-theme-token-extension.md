# FRE-1265: extend theme-aware tokens to remaining hardcoded-dark PWA components

## Plan revision after codex plan-review

Codex review surfaced one blocking finding and several incomplete-mapping
findings, both verified empirically against a real Tailwind build (not taken
on faith either way):

- **Blocking, confirmed via a real `tailwindcss` build**: `bg-accent/15`,
  `hover:bg-line/40` etc. compile to **zero CSS output** today — `line`/
  `accent`/etc. are defined as plain `var(--x)` strings in
  `tailwind.config.ts`, which Tailwind 3.4 cannot alpha-modify (no
  `color-mix()` fallback for this form, contrary to what the original plan
  assumed). Confirmed the same class is already dead in the shipped
  `ChatInput.tsx` (`hover:bg-line/40` — zero matches in the real `.next`
  build output). **Fix**: convert all 7 tokens (`bg`/`surface`/`line`/`ink`/
  `ink-muted`/`accent`/`accent-hover`) in `globals.css`'s `:root`/`.dark` from
  hex to space-separated RGB triplets, and `tailwind.config.ts`'s color
  entries to `rgb(var(--x) / <alpha-value>)` — the standard Tailwind pattern,
  confirmed working via a local build probe. Also wrap the two raw
  `var(--line)`/`var(--ink-muted)` usages in `globals.css`'s scrollbar rules
  in `rgb(...)`, since a bare triplet isn't a valid CSS color on its own. This
  is a genuine bug-fix side effect (`ChatInput.tsx`'s dead hover state), not
  scope creep — noted in the PR/handoff per build skill § 5.
- **Missing entries / non-`bg|text|border` prefixes**: `ring-`, `divide-`,
  `from-`/`to-` slate usages exist (`MermaidBlock.tsx`'s `ring-slate-700/30`,
  `ArtifactsIndex.tsx`'s `divide-slate-800(/60)`, `MermaidBlock.tsx`'s
  `from-slate-900/60 to-slate-900/20`) that the original per-file table missed
  and that `palette-centralization.test.ts`'s regex doesn't catch either.
  Widening the regex to include those prefixes is safe — confirmed none of
  the 9 already-FRE-1264-converted files use them (would otherwise regress
  that test).
- **Bare-status-color audit was incomplete**: `ApprovalModal.tsx`'s risk
  chips use only 40%-opacity dark fills (`bg-green-900/40` etc.) that will
  wash out against a light `bg-surface` card, not a fully self-contained
  chip as first assumed — needs a real light/dark dual treatment, not a
  pass. Same file's countdown text, Deny button, and violet tool-name span
  are bare text on the modal background and need the `-700 dark:-400` split.
  `TurnRating.tsx`'s `LEGACY_CLASS` is `bg-transparent` (bare text on page
  background), also needs the split. `ClassifiedErrorCard.tsx`'s fixed dark
  card (`bg-[#1b1416]`) combined with token text (`text-ink`, which is
  near-black in light mode) is an actual dark-text-on-dark-card contradiction
  — redesigned below as a full light/dark alert card, not a straight
  substitution.

The per-file conversion notes below are corrected for these findings; the
original "Files and their conversions" table entries for `ApprovalModal.tsx`,
`ClassifiedErrorCard.tsx`, `TurnRating.tsx`, and `MermaidBlock.tsx` are
superseded by the more detailed treatment in this revision section — see the
per-file task descriptions for exact class-level decisions rather than
duplicating them here.

---

Follow-up to FRE-1264 (theme-aware visual system). That ticket converted 9 files
(`layout.tsx`, `ChatMessage.tsx`, `MarkdownContent.tsx`, `ToolIndicator.tsx`,
`StreamingChat.tsx`, `TurnStatusBar.tsx`, `SessionList.tsx`, `ChatInput.tsx`,
`TurnSummaryPanel.tsx`) to the `bg`/`surface`/`line`/`ink`/`ink-muted`/`accent`/
`accent-hover` CSS-custom-property tokens (`seshat-pwa/src/app/globals.css`,
`tailwind.config.ts`). This ticket converts everything else still hardcoding the
old dark-only `slate-`/`blue-` palette, confirmed via
`grep -rl "slate-\|blue-[0-9]" src/components/*.tsx`:

`ApprovalModal.tsx`, `DecisionCard.tsx`, `ClassifiedErrorCard.tsx`,
`ModelPicker.tsx`, `PhaseIndicator.tsx`, `TurnRating.tsx`, `LocationConsent.tsx`,
`ArtifactViewer.tsx`, `ArtifactExportMenu.tsx`, `ArtifactCard.tsx`,
`ArtifactsIndex.tsx`, `ObserveView.tsx`, `MermaidBlock.tsx` — 13 files.

## Scope decision: AC-3 (light code-block theme) — OUT of scope

The ticket marks AC-3 optional, "scope to be confirmed at pickup." Adopting a
light `highlight.js` theme means either a second conditionally-loaded
stylesheet or token-level overrides of every highlight.js token class — a
distinct piece of work from "extend the existing token system to more
components," with its own design tradeoffs (bundle size vs. FOUC vs. override
completeness). Not taking it on here; AC-1 and AC-2 are the deliverable.

## Design decisions carried over from FRE-1264 (established pattern, not new)

**Mechanical substitution** — direct `bg`/`text`/`border` slate→token, blue→accent:

| Old utility | New token | Notes |
|---|---|---|
| `bg-slate-900`, `bg-slate-900/NN` | `bg-bg`, `bg-bg/NN` | page-level background |
| `bg-slate-800`, `bg-slate-800/NN` | `bg-surface`, `bg-surface/NN` | card/panel background |
| `bg-slate-700`, `bg-slate-700/NN` | `bg-line`, `bg-line/NN` | chip/hover background (matches `hover:bg-line/40` already in `ChatInput.tsx`) |
| `text-slate-100`, `text-slate-200` | `text-ink` | primary text |
| `text-slate-300/400/500/600` | `text-ink-muted` | secondary/meta text |
| `border-slate-600/700/800` | `border-line` | borders/dividers |
| `divide-slate-800` | `divide-line` | list dividers |
| `text-blue-400`, `hover:text-blue-400` | `text-accent`, `hover:text-accent-hover` | links / "open" actions (matches `MarkdownContent.tsx`'s existing `text-accent underline hover:text-accent-hover`) |
| `bg-blue-900/NN` (selected-state bg) | `bg-accent/15` | opacity modifier on a CSS-var color resolves via Tailwind 3.4's `color-mix()` fallback — already proven in-repo (`ChatInput.tsx`'s `bg-line/40`) |

**Status colors (green/red/yellow/amber/emerald/violet/sky) are semantic, not
part of the slate/blue conversion** — `palette-centralization.test.ts`'s regex
only matches `slate`/`blue`, and design intent (risk chips, rating chips,
tool-status icons) is unchanged. But where a status color is used as bare
text/icon color directly against the page/card background (not inside a
self-contained colored chip that already carries its own background), pair it
with a light-legible variant using the `-700 dark:-400` split already
established in `ToolIndicator.tsx` (`text-amber-700 dark:text-amber-400`,
`text-emerald-700 dark:text-emerald-400`) and `TurnStatusBar.tsx`. Applies to:
`PhaseIndicator.tsx`'s running/completed/error status text+icons (same idiom
as its sibling `ToolIndicator.tsx`, which already got this treatment in
FRE-1264 despite not being slate/blue) and `LocationConsent.tsx`'s
`text-amber-400` retry/denied copy. Self-contained chips with their own
background (risk chips in `ApprovalModal.tsx`, rating chips in
`TurnRating.tsx`, the placement dot in `ModelPicker.tsx`/`ObserveView.tsx`)
are left as-is — already legible against either page background because they
carry their own contrasting fill.

**`MermaidBlock.tsx` — diagram canvas stays a fixed dark island, same
precedent as `MarkdownContent.tsx`'s `CodeBlock`.** The outer chrome (header
bar, action buttons, borders, loading-state dots) converts to tokens. The
mermaid-rendered SVG itself keeps its current dark `themeVariables` (hardcoded
hex, not Tailwind utilities — outside AC-1's regex entirely) and the
`PNG_BACKGROUND` export constant is untouched — re-theming the diagram itself
means either re-initializing mermaid on every theme toggle or shipping a
second theme variant, which is a distinct, larger piece of work outside "wire
up the existing token system." Bracket the fixed-dark diagram-surface
utilities (`bg-gradient-to-b from-slate-900/60 to-slate-900/20`,
`bg-slate-900/40` loading state) with `// palette-allowlist:start/end`,
matching `MarkdownContent.tsx`'s existing exemption mechanism, and add a
one-line comment (not a duplicate manual-test-plan) noting the deliberate
choice.

**`DecisionCard.tsx`** already has partial light/dark support (light-mode
`sky-*` classes with explicit `dark:` overrides, not yet on the token system).
Its only `slate` references are inside `dark:` variants
(`dark:text-slate-100`, `dark:text-slate-300`) — swap those to `dark:text-ink`
/ `dark:text-ink-muted` (keeps the `dark:` prefix so the light-mode sky
palette is undisturbed; only the dark-mode text color routes through the
token, matching its current dark value since `--ink` under `.dark` is the
same `#f3f6fa` FRE-1264 already ported from the old slate-100).

**`LocationConsent.tsx`** toggle switch: `bg-violet-600` (on state) is a
distinct semantic "enabled" color, not `slate`/`blue` — untouched. Its "off"
state `bg-slate-600` → `bg-line`.

## Files and their conversions (mechanical detail is applied at implementation
time following the table above; call out only the non-mechanical points)

1. `ApprovalModal.tsx` — modal chrome (bg/border/text) → tokens; risk chips
   (green/yellow/red) untouched; `focus:ring-offset-slate-800` →
   `focus:ring-offset-surface`.
2. `DecisionCard.tsx` — per the design decision above.
3. `ClassifiedErrorCard.tsx` — `text-slate-*` → ink/ink-muted; the error card's
   own `bg-[#1b1416]`/`bg-[#9f2d22]` arbitrary-hex values are a deliberate
   dark-red-tinted error surface (not slate/blue) — untouched;
   `border-slate-600`/`hover:bg-slate-800` (secondary button) → tokens.
4. `ModelPicker.tsx` — trigger + dropdown chrome → tokens;
   `bg-blue-900/30` (selected row) → `bg-accent/15`; `text-blue-400` → `text-accent`.
5. `PhaseIndicator.tsx` — chrome (`border-slate-800`, `text-slate-400/500`) →
   tokens; running/completed/error status text+icon → `-700 dark:-400` split
   per the design decision above (error stays `red-700 dark:red-400` to match
   sibling components; cancelled `text-slate-500` → `text-ink-muted`).
6. `TurnRating.tsx` — only slate reference is `UNSELECTED_CLASS`'s
   `text-slate-500 hover:text-slate-300 hover:border-slate-600` → ink-muted /
   ink / line. Chip `selectedClass` values (red/emerald/gold) untouched.
7. `LocationConsent.tsx` — per the design decision above; `border-slate-700/50`
   → `border-line`, `text-slate-400/500` → `text-ink-muted`.
8. `ArtifactViewer.tsx` — drawer/sheet chrome → tokens; the sandboxed iframe's
   own `bg-white` is unrelated (content frame, not chrome) — untouched.
9. `ArtifactExportMenu.tsx` — dropdown chrome → tokens; amber error banner
   (`border-amber-700/60 bg-amber-950/80 text-amber-200`) is a status surface,
   left untouched (self-contained, own background).
10. `ArtifactCard.tsx` — card chrome + skeleton → tokens; fallback link
    `text-blue-400` → `text-accent`.
11. `ArtifactsIndex.tsx` — page chrome, header, list rows, skeleton → tokens;
    `text-red-400` error copy gets the `-700 dark:-400` treatment (bare text on
    page background).
12. `ObserveView.tsx` — page chrome, header, role-binding table, provider
    table → tokens; "Open" role badge `bg-blue-900/40 text-blue-300` →
    `bg-accent/15 text-accent`.
13. `MermaidBlock.tsx` — per the design decision above.

## Test plan (TDD)

1. **AC-1 (mechanical token-only check)** — extend
   `seshat-pwa/src/__tests__/palette-centralization.test.ts`'s `CONVERTED_FILES`
   array with all 13 `components/*.tsx` paths above. Write this first, confirm
   it fails (red) against the current unconverted sources, then convert each
   file until it passes.
   - `cd seshat-pwa && npx vitest run src/__tests__/palette-centralization.test.ts`
2. **`MermaidBlock.tsx`'s allowlist guard** — extend the existing
   "allowlist actually contains what it exists to exempt" test (currently
   only checks `MarkdownContent.tsx`) to also cover `MermaidBlock.tsx`'s new
   allowlisted region, mirroring the existing assertion shape.
3. **AC-2 (real rendered legibility, both themes, 390px)** — extend
   `seshat-pwa/e2e/theme.spec.ts` (or a new `e2e/theme-extended.spec.ts` if
   triggering all 13 surfaces cleanly in one file gets unwieldy) with a
   contrast check per surface, reusing the existing `contrastRatio` /
   `parseRgb` / `relativeLuminance` helpers. Not every surface is reachable
   without heavy stubbing (`ApprovalModal`, `DecisionCard`,
   `ClassifiedErrorCard` need SSE/WS event stubs; `ArtifactViewer` needs an
   artifact fixture) — prioritize the surfaces reachable with the existing
   `stubRest`/`stubWebSocket` helpers (`/artifacts` → `ArtifactsIndex`,
   `/observe` → `ObserveView`) and static-render the harder-to-trigger ones
   directly via a small test harness page if reachable surfaces don't cover
   all 13; if genuinely not Playwright-reachable within reasonable effort,
   document which surfaces got a live contrast check vs. a manual visual
   check in the PR body, per the ticket's "How checked: Playwright,
   emulateMedia both ways, contrast check" — don't silently skip.
   - `cd seshat-pwa && npx playwright test e2e/theme.spec.ts` (or the new file)
4. **Manual verification** — `make dev` (or `npm run dev` inside `seshat-pwa`)
   and visually check each of the 13 surfaces in both light and dark mode at
   a 390px viewport, per CLAUDE.md's "test the golden path in a browser"
   requirement for frontend changes.

## Quality gates

- `cd seshat-pwa && npm run lint` (FRE-395 requirement, must exit 0)
- `cd seshat-pwa && npx vitest run`
- `cd seshat-pwa && npx playwright test e2e/theme.spec.ts` (plus any new file)
- No `make mypy` / `make ruff-*` — this ticket touches only `seshat-pwa/`, no
  Python.

## Self-review routing (build skill § 8)

Diff class: **self-serve**. This is frontend-only (`seshat-pwa/src/components/`,
test files), read-only against no production data, no production write path,
no schema/cost/governance code. Both reviewers (`feature-dev:code-reviewer`,
`security-review`) run at the pre-PR gate per the skill; findings fixed
on-branch.
