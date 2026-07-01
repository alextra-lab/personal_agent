# Kibana Lens via Playwright — CREATE-layer technical findings (FRE-593)

**Author:** build session (CREATE layer of the "create a data visualization" skill).
**Context:** built the FRE-593 "Context Window Occupancy" stacked-area Lens dashboard end-to-end
by driving Kibana 8.19 (`cloud-sim-kibana`, `http://localhost:5601`) with the Playwright MCP,
then exported the real saved object and proved it renders. This note is the raw *how + why* so
the SCRUTINIZE (four-gate) + ITERATE loop has a reliable CREATE primitive to call.

Scope reminder: the whole reason this is UI-driven is the render trap — a Lens object needs
`attributes.visualizationType` (`lnsXY`), which is **optional at import but required at render**.
Hand-authoring omits it → imports "OK", draws "Visualization type not found." So CREATE = *let
Kibana author the object*, and our job is to drive its UI reliably and extract a stable artifact.

---

## 1. Playwright ↔ Kibana interaction techniques

### 1a. React-controlled inputs — the single biggest time sink

**The core fact:** Kibana inputs are React *controlled* components. `<input value={state}>` with an
`onChange` that setState's. There are two distinct ways their handlers read the value, and this
determines whether DOM-injection works:

- Handlers that read **`event.target.value` at dispatch time** → DOM injection works.
- Handlers/validators that read **React state at a later moment** (e.g. a form's submit button
  reads `useState` title) → DOM injection silently fails: the DOM node shows your value, but
  React's internal state is still empty, so submit validates against empty and no-ops.

**What did NOT work:**

- `el.value = 'x'` (plain assignment). React installs a value tracker on the input; a plain
  assignment is invisible to React and gets clobbered on the next render.
- Playwright `locator.fill()` on the **library-finder search box** — set the DOM value but did
  not trigger the debounced filter (results stayed unfiltered).
- **Mixing** JS injection then typing on the same field — the injected value and typed chars
  interleaved at the cursor: I got `"Context WiContext Window Occupancy (FRE-593)ndow Occupancy"`.
  Never mix the two on one field.

**What DID work — native value setter + dispatched events (for fields read at dispatch):**

```js
// WORKED for the Lens dimension "Name" field (metric legend labels).
// The dimension editor commits on the dispatched change event, so React sees it.
const el = document.querySelector('input[placeholder="Sum of context_occupancy.memory_tokens"]');
const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
setter.call(el, 'Memory');
el.dispatchEvent(new Event('input',  { bubbles: true }));
el.dispatchEvent(new Event('change', { bubbles: true }));
```

The native prototype setter bypasses React's value-tracker, so the subsequent synthetic `input`
event is seen by React as a real change. The exported object confirmed it stuck (`customLabel:
"Memory"`). This worked for the three metric names and (with an Update-button click) the KQL bar.

**What DID work — real keyboard typing (for fields read at submit/validate):**

The **save-modal title** ignored the native-setter trick — the read-back `titleVal` was correct,
but clicking `confirmSaveSavedObjectButton` did nothing because React's title state was still `''`.
Fix: real keystrokes, and clear first so no stale injected value lingers:

```
click  [data-test-subj="savedObjectTitle"]
press  ControlOrMeta+a
press  Delete
type   "Context Window Occupancy (FRE-593)"   // Playwright pressSequentially / fill
```

**Rule of thumb for the skill:** for anything that *gates a submit or a filter* (titles, search
boxes), use real keyboard typing with an explicit clear (click → Ctrl+A → Delete → type). Reserve
the native-setter trick for inline fields that commit on their own change event (dimension name).
When unsure, type — it's slower but never has the state-desync failure mode.

### 1b. Direct DOM `.click()` vs Playwright actionability click

- **Playwright `locator.click()`** does actionability checks: visible, stable, *receives pointer
  events* (hit-test at the click point). It's correct for buttons you want to prove are truly
  clickable. I used it for `savedObjectTitle`, `confirmSaveSavedObjectButton`, `#add-to-library-option`.
- It **FAILED** on the finder result row with
  `<span ...>Your data is not secure</span> ... subtree intercepts pointer events` → 5s timeout.
  A toast overlay sat on top of the click coordinate.
- **Direct `.click()` via `browser_evaluate`** invokes the element's click handler *without* a
  hit-test, so overlays don't block it:

```js
const row = [...document.querySelectorAll('[data-test-subj="savedObjectFinderTitle"]')]
  .find(e => /Context Window Occupancy \(FRE-593\)/.test(e.textContent));
(row.querySelector('button,a,[role="button"]') || row).click();
```

  I also used evaluate-`.click()` for all the Lens config-panel controls (Sum function, chart-type
  menu items, dimension "Edit …" buttons, dialog Close) — plain buttons where a handler-invoke is
  enough and text/aria matching is easier than deriving a stable locator.

**Trade-off:** evaluate-`.click()` can't catch a genuinely-unclickable element (no hit-test), so
it's not a substitute for actionability when you're *testing* clickability. For a known-good
element behind a nag overlay, it's the robust choice.

### 1c. Timing / debounce / render-complete

- **Finder search debounce:** after a real keystroke I had to **press Enter and wait ~1.2 s**
  before the list filtered from 10 → 1. `fill()` alone never filtered; injection never filtered.
  I polled with `setTimeout(…, 1200)`. (EUI's nominal debounce is ~250 ms, but Enter + 1.2 s was
  reliable across runs.)
- **Render-complete signal — do NOT eyeball a screenshot; assert the DOM flag.** Elastic Charts
  sets `data-ech-render-complete="true"` on the chart container when it finishes drawing (Kibana
  panels also carry `data-render-complete="true"`). This is the deterministic "it drew" signal:

```js
() => new Promise(res => setTimeout(() => res({
  renderComplete: document.querySelectorAll(
    '[data-render-complete="true"], [data-ech-render-complete="true"]').length,   // === 3 here
  chartCanvas:    document.querySelectorAll('.echChart canvas, canvas.echCanvasRenderer').length,
}), 4000))
```

  I used a 3.5–4 s settle before the check; the flags then reported 3 (panel + chart layers).

### 1d. Overlays that intercept clicks

- The **"Your data is not secure"** EUI security-nag toast is the recurring offender. Detect it
  from the Playwright click error (`… "Your data is not secure" … intercepts pointer events`) or
  proactively, and dismiss before interacting:

```js
[...document.querySelectorAll('[data-test-subj="toastCloseButton"], .euiToast button')]
  .forEach(b => { try { b.click() } catch {} });
```

  The nag re-appears across navigations, so dismiss it again after each `browser_navigate`, or
  just prefer evaluate-`.click()` which ignores it.

### 1e. Selector strategy (reliable vs flaky)

- **Reliable — `data-test-subj`:** `savedObjectTitle`, `confirmSaveSavedObjectButton`,
  `saveCancelButton`, `savedObjectFinderSearchInput`, `savedObjectFinderTitle`,
  `dashboardAddFromLibraryButton`, `toastCloseButton`, and the save-modal radio ids
  `#existing-dashboard-option` / `#new-dashboard-option` / `#add-to-library-option`.
- **Flaky — accessible-name / text matching** (needed for Lens config-panel controls, which lack
  stable test-subjs I could find):
  - Chart-type menu item text is **concatenated** — the label + its description render as one text
    node: `"AreaCompare distributions of cumulative data trends."`. My `/^Area\b/` failed (no word
    boundary between `Area` and `Compare`). Working matcher:
    `t.startsWith('Area') && /cumulative data trends/i.test(t)`.
  - The dimension **Edit** button aria-label encodes the current aggregation + field:
    `"Edit Median of context_occupancy.tool_tokens configuration"` — so the selector *changes after
    you switch Median→Sum*. Match on the field name, act before/after the agg change accordingly.
  - The **Save** button was hidden under a collapsed **"Open menu"** app-menu at the test viewport
    width — had to open that menu first. Widen the viewport or open the app menu before looking.
- **Token efficiency:** a full `browser_snapshot` of a Kibana page is ~55 KB of YAML. Prefer
  **targeted** snapshots (`target: <region ref>`, e.g. the Config-panel region) and, better,
  `browser_evaluate` returning a small JSON of exactly the state you need (button list with
  `disabled`/`data-test-subj`, input values, result counts). I read modal/finder state this way
  throughout instead of re-snapshotting.

---

## 2. Seeding data so the fields are selectable

**The gotcha that blocks everything:** in Lens, a field with **zero matching docs** appears under
**"Empty fields"** and is *not* in the default "Available fields" list — you cannot add it to a
metric. `context_occupancy.*` had just gone live in the mapping but **0 docs carried it**, so
without seeding I literally could not build the chart. Kibana's field list derives from the data
view's `_field_caps`; a field only becomes "available" once at least one matched index has a value.

**How I seeded — isolated, deletable, local-only:**

- Target: `cloud-sim-elasticsearch` at `http://localhost:9200` — the **local** stack, **not** the
  cloud gateway / prod. No `/chat` gateway turn was fired (that costs money and pollutes the KG —
  standing rule). Seeding sample docs into a scratch index is the safe substitute.
- Index name: **`agent-logs-fre593-sample`** — a dedicated name that (a) is matched by the
  `agent-logs-*` data-view pattern (so field_caps exposes the fields to Lens) yet (b) is a single
  `DELETE` away and never touches the real dated `agent-logs-YYYY.MM.DD` indices.
- The `agent-logs-*` index template was already registered locally (from master's deploy), so the
  scratch index inherited the correct `context_occupancy` object mapping (4 × `long`) — verified
  with `_mapping/field/context_occupancy.*` before building.
- Payload: ~48 `context_budget_applied` docs over a 4-hour window, each with
  `context_occupancy.{memory,tool,reasoning}_tokens` + `total` (and realistic variance; reasoning
  0 on some docs to mimic non-thinking turns). Injected via one `POST /_bulk?refresh=true`
  (`urllib`, `application/x-ndjson`). `refresh=true` so the docs were immediately queryable.
- **Cleanup:** `DELETE /agent-logs-fre593-sample` at the end (confirmed `404`). Left zero residue.

Safety rails, restated for the skill: **local stack only; a dedicated deletable index; never seed
prod; never fire a gateway turn to manufacture data.** Seeding is a build-time scaffold, deleted
before finishing — which is also why the final render-proof (§4) is done against a *clean* re-import.

---

## 3. Export + stable-id rewrite

**Export (deep):**

```
POST /api/saved_objects/_export
{ "objects":[{"type":"dashboard","id":"<dash-uuid>"}],
  "includeReferencesDeep": true, "excludeExportDetails": true }
```

Returns 3 ndjson objects: the `index-pattern`, the `lens` (carrying `visualizationType: "lnsXY"` —
the whole point), and the `dashboard`.

**Stable-id rewrite — why:** Kibana mints random UUIDs for UI-saved objects. If you commit those,
a re-import *adds* a new dashboard and leaves the old broken one → duplicates. Rewriting the
UUIDs to the **stable committed ids** (`context-occupancy-over-time`, `context-occupancy-dashboard`)
makes `_import?overwrite=true` **replace the broken objects in place** — the fix cleans up the
prod duplicate as a side effect.

**Where a volatile UUID hides — the FULL set (I missed one on the first pass):**

1. `lens.id`
2. `dashboard.id`
3. `dashboard.references[].id` — the entry whose `type:"lens"` points at the lens UUID.
4. **`dashboard.attributes.panelsJSON` → `embeddableConfig.savedObjectId`** — and `panelsJSON` is a
   **JSON-encoded string**, so a structured "rewrite `references[].id`" pass does **not** touch it.
   **This is the one I got wrong first.** My first rewrite fixed ids + references but left the
   stale UUID inside `panelsJSON`. It imported fine and *rendered locally* — because the old-UUID
   lens still existed in my local Kibana. That is the exact "passes locally, breaks on prod" trap:
   on prod the old UUID won't exist and the panel can't resolve its saved object. I only caught it
   by dumping the imported dashboard's `panelsJSON` and eyeballing the id.

**The robust fix — global string-replace over the serialized object**, so the stringified
`panelsJSON` is covered too:

```python
s = json.dumps(o, separators=(",", ":")).replace(LENS_OLD, LENS_NEW).replace(DASH_OLD, DASH_NEW)
o = json.loads(s)
# then assert the old UUIDs appear NOWHERE in the final file:
assert LENS_OLD not in raw and DASH_OLD not in raw
```

**What NOT to rewrite (over-rewriting is its own bug):**

- The reference **`name`** strings, e.g.
  `"ec418778-…:panel_ec418778-…"` and `"ec418778-…:indexpattern-datasource-layer-cd39ef01-…"`.
  The `ec418778-…` prefix is the **panelIndex** (a panel-instance id, internal to this dashboard),
  and `cd39ef01-…` is the Lens **layerId** — both are internal and must stay **consistent between
  `panelsJSON` and `references[].name`**, not aligned to the object id. `panelRefName` in
  `panelsJSON` must equal the reference `name`. Leave all of these exactly as exported; only the
  **saved-object ids** (lens, dashboard) and the reference that points *at* the lens id change.
- The `index-pattern` id (`eabfafeb-…`) — it's a stable shared data view already committed in
  `data_views.ndjson`; keep it.
- Also strip pure export noise: `created_at`, `updated_at`, `version`, `managed`. Keep
  `coreMigrationVersion`, `typeMigrationVersion`, `attributes`, `references`, `id`, `type`.

Emit deps-first (index-pattern → lens → dashboard) and validate JSON-per-line.

---

## 4. Render-proof sequence (prod-equivalent)

The proof must run on a **clean slate**, or leftover scratch objects mask a stale-reference bug
(§3.4). Sequence:

1. **Delete the scratch objects** so nothing can resolve a stale UUID:
   `DELETE /api/saved_objects/lens/<uuid>?force=true`,
   `DELETE /api/saved_objects/dashboard/<uuid>?force=true`. Also delete the stable-id objects from
   any earlier import so the next import is a clean create.
2. **Re-import the committed file exactly as the deploy script does:**
   `POST /api/saved_objects/_import?overwrite=true` (multipart file) → expect
   `success:true, successCount:3, errors:null`.
3. **Navigate in view mode** with a time range covering the (still-present, until cleanup) sample
   data: `/app/dashboards#/view/context-occupancy-dashboard?_g=(time:(from:…,to:…))`.
4. **Assert the DOM** (after a ~4 s settle):
   - `document.body.innerText` does **not** match
     `/Visualization type not found|could not be found|Could not locate|Error loading/i` → `null`.
   - `[data-ech-render-complete="true"]` count ≥ 1 (I saw 3).
   - a chart canvas exists (`.echChart canvas`).
   - legend labels === `["Memory","Tool definitions","Reasoning"]`, panel title correct.

Note ordering: I ran the render-proof *before* deleting the sample index, so the chart drew with
data (bands visible). The **empty-after-cleanup** state is expected and is NOT a render failure —
"renders" (type resolved, axes/legend draw) is a strictly weaker property than "has data". Master's
four-gate SCRUTINIZE (esp. "verified against real recent data") is what closes that gap; CREATE's
job is to hand over an artifact that provably *renders*, plus a note that live data is still pending.

---

## 5. Dead ends / time sinks (what I'd skip next time)

1. **Native-setter injection on submit/filter-gating inputs.** Cost several dead save/search
   attempts (modal stayed open; finder stayed unfiltered). Next time: **type** into any title/search
   box (click → Ctrl+A → Delete → type), and only use the setter trick for inline commit-on-change
   fields. This one change removes most of the flakiness.
2. **Mixing injection + typing** on one field → interleaved garbage string. Pick one method per
   field; always clear first.
3. **Assuming `_import` success == renders.** It never does for Lens. The only proof is the DOM
   render-check on a **clean** re-import. Budget for the seed→build→export→delete→reimport→assert
   loop, not a one-shot.
4. **Structured id-rewrite that misses `panelsJSON.savedObjectId`.** Use a whole-object
   string-replace and assert the old UUIDs are absent from the final text. (Nearly shipped a
   renders-locally / breaks-on-prod artifact.)
5. **Text-matching Lens menu items** — labels are concatenated with their descriptions; naive
   `^Label\b` regexes fail. Match `startsWith(label) && /distinctive-substring/`. Where a
   `data-test-subj` exists, always prefer it.
6. **Default aggregation is Median, not Sum.** "Add field to workspace" auto-builds `@timestamp` X
   + **Median**(field) Y. For a composition you want **Sum** — every metric needs a Median→Sum edit.
   Easy to miss and it silently produces a wrong-but-plausible chart (a SCRUTINIZE "accurate" fail).
7. **Default series type is Bar.** Had to switch Bar→**Area stacked** (stacking was already
   "Stacked"). One extra menu step; don't assume the export is area just because you wanted area.
8. **Duplicate-title save.** Saving a dashboard with a title that already exists triggers a
   "Save as new dashboard / duplicate title" second modal — needs an extra confirm, and it's why
   stable-id + overwrite (not new UUIDs) is the right call.
9. **Full-page `browser_snapshot` is ~55 KB.** Expensive and mostly noise. Use targeted region
   snapshots and compact `browser_evaluate` state reads instead.

---

## Appendix — the concrete build recipe (condensed)

1. Seed ~40–50 sample docs carrying the new field into `agent-logs-fre593-sample` (local,
   `_bulk?refresh=true`); verify the mapping.
2. `browser_navigate` to `/app/lens#/?_g=(time:(from:…,to:…))`; dismiss the security toast.
3. Click **Add `<field>` to workspace** (auto-builds @timestamp X + Median Y).
4. For each metric: open its **Edit** dimension button → click **Sum** → set the **Name** via the
   native-setter snippet → **Close**. Add the other two fields to the Vertical axis, same treatment.
5. Chart-type menu → **Area stacked** (`startsWith('Area') && /cumulative data trends/`).
6. Set the KQL bar (`event_type: "context_budget_applied"`) → click **Update/Refresh**.
7. **Save** (open the app menu if the button is hidden): type the title with real keystrokes,
   choose **Add to library** (`#add-to-library-option`), confirm.
8. Add it to a new dashboard from library (finder search needs Enter + ~1.2 s), save the dashboard.
9. `_export` deep → strip noise → **global string-replace** UUIDs → stable ids → validate.
10. Delete scratch objects → `_import?overwrite=true` the committed file → view-mode DOM
    render-assert (`data-ech-render-complete`, no "Visualization type not found", legend correct).
11. Delete the sample index. Hand off with the render proof + "live data still pending a real turn".
