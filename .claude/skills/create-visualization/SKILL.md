---
name: create-visualization
description: Use when a task includes creating or changing a data visualization — a Kibana dashboard widget / Lens chart. Build the viz in the Kibana UI driven by Playwright, export a stable artifact, then run it through the create → scrutinize → iterate loop. NEVER hand-author Lens ndjson. Triggers — "create a widget", "add a dashboard panel", "Kibana chart", "Lens visualization", "visualize <telemetry>".
---

# Create a Visualization (widget / Lens chart)

A rendering chart can still be wrong, useless, or misleading. This skill exists because a hand-authored
Lens object imports cleanly and draws *"Visualization type not found"* (FRE-406/593), and even a chart
that renders can tell a human the opposite of the truth (FRE-593 v1 read "100% memory" when memory was
36% of the window). So the deliverable is never "a chart exists" — it is **a human reaches the correct
conclusion from real data.**

The work runs a loop: **Create → Scrutinize → Iterate.** Build owns Create and self-scrutinizes before
handoff; master scrutinizes independently at the gate (that is master's nature, not a step build can skip).

## The absolute rule

**Never hand-author Lens JSON.** A Lens saved object needs `attributes.visualizationType` (e.g. `lnsXY`),
which is *optional at import but required at render* — hand-authored objects omit it, persist fine, and
never draw. Let Kibana author the object via its UI; your job is to drive that UI reliably and extract a
stable artifact. Never use our own committed objects as a reference for "correct" — they may be the broken
artifact.

## Documentation-first (unskippable)

For **anything** Kibana / Lens / Elastic-specific — which `visualizationType` a chart needs, chart-type
semantics, aggregation behavior (Sum vs Median vs cumulative), controls & filters, saved-object
export/import — read the **official Elastic documentation FIRST**, before building, asserting, or
reverse-engineering. Do not answer from memory; do not infer the "right" shape from our own objects.
Match the docs to the **running Kibana version (currently 8.19)** — behavior and schema differ by version.
- Lens: https://www.elastic.co/docs/explore-analyze/visualize/lens
- Dashboards & controls: https://www.elastic.co/docs/explore-analyze/dashboards
- Saved objects (export/import): https://www.elastic.co/guide/en/kibana/8.19/managing-saved-objects.html

This is the exact failure that created FRE-702: two Lens dashboards shipped broken because nobody checked
the docs — the fix was one documented attribute (`visualizationType`). When in doubt, go to the docs; do
not make the reviewer push you there.

## Step 0 — Inspect the raw data (and confirm the viz mechanics in the docs) first (non-negotiable)

Before drawing anything, read the actual event/docs you will visualize (and confirm any unfamiliar Lens
mechanic against the documentation above):
- What fields exist, and what do they *mean* (per-turn? cumulative? a subset of a larger whole?).
- What **constraints / denominators** are emitted that make the viz digestible — ceilings, caps, totals
  (e.g. `context_budget_applied` carries `max_tokens`=120000, `message_count`, `trimmed`/`overflow_action`).
  A quantity is only interpretable against its ceiling; show usage **against the denominator**, not bare
  absolute bands.
- Whether the aggregation you intend (Sum? Average? %) reflects the truth, or confounds volume with
  composition.

## Step 1 — CREATE (build owns)

Full firsthand technique note (read it — it is the reliable primitive):
`docs/research/2026-07-01-kibana-lens-playwright-build-findings.md`.

Condensed recipe (drive `http://localhost:5601` with the Playwright MCP, **local cloud-sim only**):
1. **Seed** ~40-50 sample docs carrying the new field into a *deletable local* index
   (`agent-logs-<ticket>-sample`, `POST /_bulk?refresh=true`, `:9200` local only). A field with zero docs
   hides under "Empty fields" and cannot be added. **Never seed prod; never fire a gateway turn to
   manufacture data.** Delete the index when done.
2. Build the chart in the Lens UI: add field → **fix the aggregation** (default is Median; you usually
   want **Sum**) → **fix the chart type** (default is Bar; e.g. **Area stacked**) → set the KQL filter →
   name each metric.
3. **Playwright input rules** (the biggest time sink — get them right):
   - Titles / search boxes (gate a submit or filter): **real keyboard** — click → Ctrl+A → Delete → type.
     The native-value-setter desyncs React state and the submit silently no-ops.
   - Inline commit-on-change fields (dimension name): native-value-setter + dispatched `input`/`change`.
   - **Never mix** injection and typing on one field. Prefer `browser_evaluate` element `.click()` to
     bypass overlay hit-tests (the "Your data is not secure" toast); dismiss the toast after each navigate.
   - Assert `data-ech-render-complete="true"` + a chart canvas — **do not eyeball a screenshot.**
4. Save to library → add to a dashboard → **export deep**
   (`_export`, `includeReferencesDeep:true`).
5. **Stabilize ids:** global **string-replace** the volatile export UUIDs → stable committed ids over the
   *serialized* object, so re-import overwrites in place (no duplicate). A structured `references[].id`
   rewrite MISSES `panelsJSON.savedObjectId` (a JSON-encoded string) → renders locally, breaks on prod.
   Assert the old UUIDs appear nowhere. Do NOT rewrite reference `name`s / panelIndex / layerId.
6. Commit the exported ndjson to `config/kibana/dashboards/`; keep it registered in `import_dashboards.sh`.

## Step 2 — SCRUTINIZE (four gates, ALL required; master re-applies at the gate)

1. **Renders** — draws, no "Visualization type not found." Necessary, never sufficient.
2. **Accurate** — bands/axes represent what they claim (check the axis title, the aggregation, the labels).
3. **Useful to a human** — a person can reach a *correct* decision: shows the whole quantity (a
   denominator / total, not a confounded slice), normalized so volume does not masquerade as composition,
   honest title/axis (no ticket IDs in a human-facing title).
4. **Verified against REAL recent data** — the decisive gate. Seeded/empty data lets a *misleading* chart
   pass gates 1-3; only real data exposes it (FRE-593 v1: "100% memory" vs the true 36%). Pull the raw docs,
   replicate the viz's aggregation in a query, and confirm the chart's story matches the numbers.
   - **Cache-bust the render-check.** The dashboard SPA caches saved objects — a stale panel can render the
     OLD version and give a false verdict (FRE-593 v2 first showed a cached v1). Confirm the *live saved
     object* is the new one (query the saved-objects API + check `updated_at`), then force a fresh load
     (navigate away first, add a cache-bust query param, and confirm the page title/structure changed).

A chart that leads a human to the wrong answer is worse than no chart. If any gate fails → Iterate.

## Step 3 — ITERATE

Bounce the *specific* scrutiny findings (not "make it better") back into Create; rebuild; re-scrutinize
against real data. Repeat until a human concludes correctly. This loop, not a one-shot ship, is what
produces a viable result.

## Definition of done

The panel **visibly renders AND a human reads the correct story from real data** — proven, not asserted:
a Playwright render-assert (no error, canvas + `data-ech-render-complete`) plus a raw-data cross-check that
the chart's numbers match. "The ndjson imports" and "it renders" are both weaker than this and do not close
the ticket. A visualization's acceptance criterion must name **the decision it enables**, not "a chart exists."

## Anti-patterns

- Hand-authoring / hand-editing Lens JSON, or copying our own (possibly-broken) objects as the template.
- Treating "import succeeded" or "it rendered" as done.
- Absolute bands with no denominator/ceiling (unreadable to a human).
- Sum-over-time when you meant per-turn composition (volume masquerades as composition).
- Shipping without checking the raw event first.

## Related

- Dashboard-level composition (shared controls, consistent filters, placement, cross-viz drill-down) is a
  separate, larger concern — a **Build** skill of its own (compose-a-dashboard), authored once a real
  multi-panel dashboard earns it. This skill is per-viz.
- Memory: `feedback_kibana_lens_build_in_ui_not_hand_authored`.
