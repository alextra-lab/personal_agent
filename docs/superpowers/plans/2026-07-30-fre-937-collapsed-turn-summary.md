# FRE-937 — ADR-0123 T4: collapsed per-turn summary in the transcript (AC-7 seam)

**Ticket:** FRE-937 (Approved) · **ADR:** ADR-0123 §7, Implementation Notes, Sequencing step 4
**Depends on:** FRE-936 (T3, merged/Awaiting Deploy — code present on `main`)
**Owns:** AC-7 (assembled seam, proven on the deployed stack by master at the gate)

**Revision note:** this plan was reviewed by codex (2026-07-30) before any code was written. Its
central finding — that clearing the hook's `phases`/`activeTools` state to "collapse" the surface was
an unnecessary, invasive behavior change that would silently break 8 pre-existing tests, not the 5
originally estimated — changed the design. The current plan instead **leaves the hook's live state
untouched** and gates *rendering* on whether the turn has collapsed. See "Design change" below.

## Scope

On turn completion (`DONE`), cancellation (`CANCELLED`), or failure (`RUN_ERROR`), collapse the live
phase surface (`PhaseIndicator`, shipped by T3/FRE-936) into a compact, persistent summary attached to
the turn's `ChatMessage` in the transcript: the phases that ran, their durations, the tools used, and
the terminal state (completed / cancelled / error). Collapsed by default, expandable. No new
server-side storage — derived entirely from client-held state that mirrors already-persisted,
sequenced events (ADR §7's explicit "no new durable schema" constraint).

**Out of scope — stated precisely, not glossed over:** history hydration. ADR §7's "one honest
caveat" names `ChatMessage`'s missing field and offers two routes to fill it: "a client-side field
populated from the replayed event stream, or a derivation at render time." This ticket adds the field
and populates it **for turns the client witnesses live** — the only case AC-7 and AC-9 actually
exercise (both are phrased as "run a real turn... then repeat and cancel it," never "reload the page").
Populating it for a *previously completed* turn loaded via `getSessionMessages` (REST history
hydration) would require the client to replay that turn's full phase-event history on a cold page
load — a materially larger mechanism than anything that exists today (the current replay-from-seq path
only recovers a live turn's *own* mid-flight gap, per FRE-1040 / ADR-0075, not a historical turn's
already-closed one). No AC in ADR-0123 tests this, so it is **not implemented here** and is called out
explicitly in the PR/ticket handoff for master to weigh as a possible follow-up ticket — not silently
declared "fine" by appeal to the ADR text, which does not actually say that.

## Design change from the pre-review draft

**Rejected:** clearing `phases`/`activeTools` state to `[]` after a terminal event.
**Why:** the hook's existing contract is "phase/tool state resolves to a terminal per-node state and
persists until the *next* `sendMessage`" (`useSSEStream.ts` — the reset only happens in `sendMessage`,
never in `DONE`/`CANCELLED`/`RUN_ERROR` today for `phases`; `activeTools` already clears on `DONE` and
`RUN_ERROR` but **not** `CANCELLED` — corrected fact, the original draft wrongly claimed all three
already clear). Eight existing tests in `useSSEStream.phases.test.tsx` assert on post-terminal-event
`phases[...]` state (the five `AC-9 terminal states` tests plus three snapshot-upgrade tests in the
`phase_state snapshot` describe block). Clearing state to make the UI "collapse" would force rewriting
all eight to observe a different location for the same fact — a workaround, not a fix.

**Chosen instead:** leave `phases`/`activeTools` resolution exactly as it is today (zero changes to
those 8 tests). Attach the built summary to the message *in addition to* the existing resolution, and
gate the **footer's rendering** in `StreamingChat.tsx` on a new pure helper,
`isTurnCollapsed(messages): boolean` (true iff the last message is an assistant message with a
non-null `phaseSummary`) — not on `isStreaming`, because `INTERRUPT` also sets `isStreaming` false
mid-turn (a human-wait phase must keep showing, per ADR §1's `Waiting for your choice` phase).

```tsx
const turnCollapsed = isTurnCollapsed(messages);
{!turnCollapsed && <PhaseIndicator phases={phases} />}
{!turnCollapsed && <ToolIndicator tools={activeTools} />}
```

This is strictly additive to the hook's existing state machine and requires no new hook-level state:
`messages` already re-renders `StreamingChat` on every relevant change.

## Files

### New
- `seshat-pwa/src/lib/phase-summary.ts` — `buildTurnSummary()` (pure), `groupByParent()` (pure,
  generic — shared with `PhaseIndicator`'s existing inline grouping so the live and collapsed views
  can never drift into different groupings of the same data), `isTurnCollapsed()` (pure).
- `seshat-pwa/src/lib/phase-labels.ts` — `PHASE_LABELS` + `labelFor()`, extracted verbatim from
  `PhaseIndicator.tsx` so the collapsed summary uses identical copy to the live surface (ADR
  consequence: "a stale phase name is a small lie" — one source, not two).
- `seshat-pwa/src/components/TurnSummaryPanel.tsx` — the collapsed/expandable summary UI (native
  `<details>`/`<summary>` — collapsed by default for free, no extra state, keyboard-accessible).
- `seshat-pwa/src/__tests__/phase-summary.test.ts`
- `seshat-pwa/src/__tests__/TurnSummaryPanel.test.tsx`
- `seshat-pwa/src/__tests__/useSSEStream.summary.test.tsx`
- `seshat-pwa/src/__tests__/ChatMessage.test.tsx` — new file (none exists today); closes the
  "untested integration wiring" gap codex flagged for step 10.

### Edited
- `seshat-pwa/src/lib/types.ts` — add `PhaseSummaryEntry`, `TurnSummary`; add
  `phaseSummary?: TurnSummary` to `ChatMessage`.
- `seshat-pwa/src/components/PhaseIndicator.tsx` — import `PHASE_LABELS`/`labelFor` and
  `groupByParent` instead of the inline copies (no behavior change; existing `PhaseIndicator.test.tsx`
  must pass unmodified — this is the regression guard for the extraction).
- `seshat-pwa/src/hooks/useSSEStream.ts`:
  - Add `phasesRef`/`activeToolsRef` plus two central helpers, `updatePhases(fn)`/`updateTools(fn)`,
    where **the ref is the source of truth**: `const next = fn(ref.current); ref.current = next;
    setX(next); return next;`. Replace every `setPhases((prev) => ...)` / `setActiveTools(...)` call
    site (`PHASE_START`, `PHASE_END`, the `phase_state` STATE_DELTA reconciliation, `CANCELLED`,
    `RUN_ERROR`, `DONE`, `TOOL_CALL_START`, `TOOL_CALL_END`, and the `sendMessage` reset) with the
    helper. This is the synchronous-read mechanism codex confirmed is necessary and the least-invasive
    option available (a `useReducer` migration would be a much larger rewrite for the same guarantee).
  - In `CANCELLED`/`RUN_ERROR`/`DONE`, **after** resolving phases via `updatePhases` (unchanged sweep
    logic) and **before** any pre-existing `activeTools`-clearing call, capture
    `const toolsForSummary = activeToolsRef.current` so the summary sees the turn's tools even where
    existing code already clears them (`DONE`, `RUN_ERROR`) or doesn't (`CANCELLED` — that pre-existing
    asymmetry is untouched, out of scope for this ticket). If `resolvedPhases.length === 0 &&
    toolsForSummary.length === 0`, attach nothing — a trivial turn gets no summary and no placeholder
    row. Otherwise build the summary and merge it into the existing `setMessages` update where one
    already exists (`DONE`'s traceId/complete stamp), or add one (`CANCELLED`/`RUN_ERROR`, which have
    none today): if the last message is `assistant`, attach `phaseSummary` to it; otherwise append a
    new assistant message with empty `content` carrying the summary (the placeholder case — e.g. a
    turn cancelled before any `TEXT_DELTA`).
  - `phases`/`activeTools` are **not** cleared by these handlers (design change above). They keep
    resetting to `[]` on the next `sendMessage`, via `updatePhases(() => [])`/`updateTools(() => [])`
    so the refs reset in step with the state (missing this would leak stale data into the *next*
    turn's summary on an edge-case fast DONE).
- `seshat-pwa/src/components/ChatMessage.tsx`:
  - Render `<TurnSummaryPanel summary={message.phaseSummary} />`.
  - Suppress `CopyButton` when `message.content.length === 0` (the placeholder-row case — copying an
    empty string is meaningless and a visible copy affordance on a blank bubble reads as broken).
- `seshat-pwa/src/components/StreamingChat.tsx` — gate `<PhaseIndicator>`/`<ToolIndicator>` on
  `!isTurnCollapsed(messages)` (see Design change above).
- `seshat-pwa/public/sw.js` — bump `CACHE_NAME` (currently `seshat-v39-per-session-seq` →
  `seshat-v40-turn-summary`), per FRE-395 / this ADR's Implementation Notes.

### Unchanged (explicitly verified, not just assumed)
- `seshat-pwa/src/__tests__/useSSEStream.phases.test.tsx` — all 8 terminal-event/snapshot-upgrade
  tests keep passing with zero edits, because `phases` resolution behavior is untouched.

## Data shapes

```ts
// types.ts
export interface PhaseSummaryEntry {
  phaseId: string;
  phase: PhaseName;
  detail: string | null;
  durationMs: number;
  state: 'completed' | 'cancelled' | 'error';
  parentId: string | null;
}

export interface TurnSummary {
  phases: PhaseSummaryEntry[];
  tools: string[]; // deduped tool names, first-seen order
  terminalState: 'completed' | 'cancelled' | 'error';
}
```

`buildTurnSummary(phases: PhaseNode[], tools: ToolCall[], terminalState, now = Date.now()): TurnSummary`
— `durationMs = (endedAt ?? now) - Date.parse(startedAt)`; a node's `state` maps directly (a
still-`running` node is defensively treated as `terminalState`, though by call time the existing sweep
has already resolved every node — belt-and-braces, one unit case, not a new code path).

**Known, inherited limitation on duration accuracy (flagging for master, not fixing here):**
`PhaseEndData` carries no server end-timestamp (`types.ts` — "PHASE_END carries no server end
timestamp" is T3's own documented design, `PhaseNode.endedAt` is explicitly client-observed
`Date.now()`). `buildTurnSummary`'s `durationMs` inherits this exact approximation — it is what T3's
live counter already displays while running and freezes at, unchanged by this ticket. The gap between
a phase's true server-side end and the client's `endedAt` is bounded by live-socket network latency
(sub-second in practice) and does not need new plumbing to fix; it is called out here so AC-7's "the
summary's phase durations reconcile with the persisted event stream's timestamps" live check has a
known, small, explainable tolerance rather than an unexplained surprise at the gate.

## Implementation steps (atomic, TDD)

1. `phase-summary.test.ts` — write failing tests for `buildTurnSummary` (duration math from
   server-timestamp `startedAt` + client `endedAt`; tool dedupe/order; terminalState fallback for a
   stray running node), `groupByParent` (top-level/children split; orphan child with unknown parent
   falls to top-level — mirrors `PhaseIndicator.test.tsx`'s existing case), and `isTurnCollapsed`
   (false for `[]`, false when last message is `user`, false when last is `assistant` with no
   `phaseSummary`, true when last is `assistant` with one). Run `npm --prefix seshat-pwa test --
   phase-summary` → confirm fails (module doesn't exist).
2. Implement `lib/phase-summary.ts` to pass step 1.
3. Extract `lib/phase-labels.ts` from `PhaseIndicator.tsx`; update `PhaseIndicator.tsx` to import it
   and `groupByParent` in place of its inline grouping loop. Run `npm --prefix seshat-pwa test --
   PhaseIndicator` → must still pass **unmodified** (behavior-neutral extraction; this is the
   regression guard).
4. Add `PhaseSummaryEntry`/`TurnSummary`/`ChatMessage.phaseSummary` to `types.ts`. `npm --prefix
   seshat-pwa run typecheck` (expected to still pass — additive types only).
5. `TurnSummaryPanel.test.tsx` — failing tests: renders nothing for `undefined`; collapsed by default
   (assert the `<details>` element's `.open === false`); expanding (simulate a click/toggle) reveals
   phase rows (label + formatted duration) and tool badges; terminal-state header text differs for
   completed/cancelled/error; never renders `%` or `role="progressbar"` (mirrors `PhaseIndicator`'s
   existing guard — the same ADR §4 prohibition applies to the collapsed view). Run, confirm fails.
6. Implement `TurnSummaryPanel.tsx` to pass step 5.
7. `useSSEStream.summary.test.tsx` — failing tests:
   - DONE with phase events + tool calls → last assistant message gets `phaseSummary` with
     `terminalState: 'completed'`, correct per-phase durations, deduped tool names; **and** `phases`/
     `activeTools` remain resolved-but-non-empty afterward (explicitly asserting the design change —
     NOT cleared).
   - CANCELLED mid-phase, before any `TEXT_DELTA` → a new assistant placeholder message (`content ===
     ''`) is appended carrying the summary with `terminalState: 'cancelled'` (AC-9(a)).
   - RUN_ERROR via the **realistic ordering** `PHASE_END(ok:false)` then `RUN_ERROR` (matching
     `phase_span`'s actual `finally`-before-outer-handler behavior, per the existing code comment in
     `useSSEStream.ts`) → the affected phase's summary entry is `'error'`, `terminalState: 'error'`
     (AC-9(b)).
   - A turn with zero phase events and zero tool calls → DONE attaches no `phaseSummary` and creates
     no placeholder (regression guard against clutter on trivial turns); repeat this specific
     zero-output check for CANCELLED and RUN_ERROR too (the pre-review draft only covered CANCELLED).
   Run, confirm fails.
8. Wire `phasesRef`/`activeToolsRef`, `updatePhases`/`updateTools`, and the summary-attach logic into
   `useSSEStream.ts` to pass step 7. Re-run the full `useSSEStream.phases.test.tsx` suite — confirm
   all 8 previously-identified tests still pass **unmodified**.
9. `ChatMessage.test.tsx` — failing tests: an assistant message with `phaseSummary` renders
   `TurnSummaryPanel`'s content; one without renders none of it; a message with empty `content` does
   not render `CopyButton` (query by its `aria-label`). Run, confirm fails; implement the `ChatMessage.tsx`
   edit to pass.
10. Add `isTurnCollapsed` gating to `StreamingChat.tsx` (already covered by step 1's pure-function
    tests; this is a one-line application, not separately tested at the component level — mounting the
    full `StreamingChat` tree is disproportionate to what's actually new here).
11. Bump `CACHE_NAME` in `public/sw.js`.
12. Full gate: `npm --prefix seshat-pwa test`, `npm --prefix seshat-pwa run typecheck`, `npm --prefix
    seshat-pwa run lint` — all must exit 0.

## Acceptance-criteria mapping

- **AC-7** is the assembled seam, proven **live on the deployed stack** (a real artifact-build turn,
  then a repeat cancelled mid-build) — master's gate action per the ADR ("Master asserts AC-7 at the
  acceptance gate"), not something this unit/component/hook test suite proves by itself. This plan's
  tests prove every *unit* AC-7 depends on: phases captured with correct (T3-inherited-tolerance)
  durations, tools captured, the summary collapses in the transcript, and the live surface actually
  stops rendering once collapsed (`isTurnCollapsed` unit tests + the `ChatMessage` integration test).
  The PR's final ticket comment hands master the exact live-turn steps for the deployed-stack leg,
  including the duration-tolerance note above so it isn't mistaken for a bug at the gate.
- **AC-9(a)** ("collapsed summary records the turn as cancelled with the phases that had run") —
  directly asserted by the CANCELLED unit test in step 7.
- **AC-9(b)** ("summary records" a mid-phase failure) — directly asserted by the realistic
  `PHASE_END(ok:false) → RUN_ERROR` unit test in step 7 (gap identified in review; the pre-review draft
  only asserted AC-9(a)).

## Risk notes

- The `updatePhases`/`updateTools` refactor touches every `setPhases`/`setActiveTools` call site in
  `useSSEStream.ts`, but each replacement is mechanical (same updater function, different call
  wrapper) — the existing 8+ terminal/snapshot tests are the regression guard that this refactor is
  behavior-neutral.
- No backend/Python changes — this is PWA-only, consistent with ADR §7's "no new server-side storage."
- The `activeTools`-not-cleared-on-`CANCELLED` asymmetry pre-dates this ticket and is left as-is; it no
  longer matters for the visible bug (stale footer content after a terminal event) because rendering is
  now gated on `isTurnCollapsed`, not on `activeTools` being empty.
