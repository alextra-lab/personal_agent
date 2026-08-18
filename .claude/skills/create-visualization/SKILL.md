---
name: create-visualization
description: Use when a task includes creating or changing a data visualization — a Grafana panel, over any datasource (Postgres, Elasticsearch, Tempo). Build the viz in the tool's own UI driven by Playwright, export a stable artifact, then run it through the create → scrutinize → iterate loop. NEVER hand-author visualization JSON. Triggers — "create a widget", "add a panel", "add a Grafana panel", "Grafana dashboard", "visualize <telemetry>", "Postgres datasource panel".
---

# Create a Visualization (Grafana panel)

A rendering chart can still be wrong, useless, or misleading. So the deliverable is never "a chart
exists" — it is **a human reaches the correct conclusion from real data.**

The work runs a loop: **Create → Scrutinize → Iterate.** Build owns Create and self-scrutinizes before
handoff; master scrutinizes independently at the gate (that is master's nature, not a step build can skip).

**Composing a whole dashboard** — shared controls, reading order, cross-panel drill-down, whether the
*set* answers one coherent question — is a separate concern. This skill is per-viz.

## The absolute rule

**Never hand-author visualization JSON — in any tool.** Let the tool's own UI author the object; your
job is to drive that UI reliably and extract a stable artifact. Never use our own committed objects as
the reference for "correct" — they may be the broken artifact.

Two worked instances of the same failure, in two different tools:

- **Kibana / Lens** (retired, FRE-1214 — kept as evidence for the rule). A Lens saved object needed
  `attributes.visualizationType` (e.g. `lnsXY`), which was *optional at import but required at render*
  — hand-authored objects omitted it, persisted fine, and never drew (FRE-406/593/702).
- **Grafana.** FRE-1072 shipped 16 dashboards and 68 panels of hand-authored JSON. Measured against
  `config/grafana/dashboards/*.json`: **all 68 panels carry exactly one key set** —
  `datasource, description, gridPos, id, targets, title, type` — and **zero** carry `fieldConfig`,
  `options` or `pluginVersion`. Grafana's UI emits all three unconditionally. So: **0 units, 0
  thresholds, 0 overrides**; `latency_ms`, `cost_usd` and `input_tokens` all render as bare numbers.
  Nothing failed loudly. The panels draw. They just do not mean anything.

The Grafana case is the more dangerous of the two, and worth internalising: **the Lens failure announced
itself** ("Visualization type not found"), **the Grafana failure did not.** A hand-authored Grafana panel
renders, passes a render assertion, and ships.

## Documentation-first (unskippable)

For **anything** tool-specific — which attribute a chart needs, chart-type semantics, aggregation
behavior (Sum vs Median vs cumulative), units, thresholds, controls & filters, export/import — read the
**official documentation FIRST**, before building, asserting, or reverse-engineering. Do not answer from
memory; do not infer the "right" shape from our own objects. **Match the docs to the running version** —
behavior and schema differ by version.

**Grafana (running 13.1.3 — verify with `GET /api/health`):**
- Panels & visualizations: https://grafana.com/docs/grafana/v13.1/panels-visualizations/
- Units / thresholds / standard options: https://grafana.com/docs/grafana/v13.1/panels-visualizations/configure-standard-options/
- Postgres data source: https://grafana.com/docs/grafana/v13.1/datasources/postgres/
- Dashboard JSON model: https://grafana.com/docs/grafana/v13.1/dashboards/build-dashboards/view-dashboard-json-model/
- Provisioning: https://grafana.com/docs/grafana/v13.1/administration/provisioning/
- The unit ids are **not** in the docs — they are the `id` fields in
  `packages/grafana-data/src/valueFormats/categories.ts` at the matching tag. Read them there.

This is the exact failure that created FRE-702 (on the now-retired Kibana/Lens arm): two dashboards
shipped broken because nobody checked the docs — the fix was one documented attribute. When in doubt,
go to the docs; do not make the reviewer push you there.

## Step 0 — Inspect the raw data (and confirm the viz mechanics in the docs) first (non-negotiable)

Before drawing anything, read the actual rows/events/docs you will visualize:
- What fields exist, and what do they *mean* (per-turn? cumulative? a subset of a larger whole?).
- What **constraints / denominators** are emitted that make the viz digestible — ceilings, caps, totals
  (e.g. `context_budget_applied` carries `max_tokens`=120000, `message_count`, `trimmed`/`overflow_action`).
  A quantity is only interpretable against its ceiling; show usage **against the denominator**, not bare
  absolute bands.
- Whether the aggregation you intend (Sum? Average? %) reflects the truth, or confounds volume with
  composition.
- **What the number's unit actually is.** You cannot set a unit in Step 1 that you did not establish here.

## Step 1 — CREATE (build owns)

**Read this first, or the loop will not work.** Every fact below was verified live against the running
instance; each is a place the obvious approach silently fails.

| Constraint | Consequence |
|---|---|
| Provisioned dashboards report `meta.provisioned: true, canEdit: false, canSave: false` (all 16 checked) | **You cannot edit a committed dashboard in the UI.** Not one of them. |
| Anonymous org role is `Viewer`; `/dashboard/new` redirects to `/` | **A Viewer cannot author anything.** |
| `GF_AUTH_DISABLE_LOGIN_FORM=true`; `POST /login` returns `400 auth.client.notConfigured` | **You cannot sign in through the UI to upgrade the session.** |
| Basic auth authenticates the **HTTP API** (`curl -u admin:…`) but does not give the browser a session | The API is not an authoring path — driving it *is* hand-authoring. |

So authoring needs its own instance. **Do not widen the shared instance's anonymous role** — it is
reachable through the Cloudflare tunnel, so that is a live security-posture change, not a dev convenience.

1. **Stand up an ephemeral Editor instance**, loopback-only, on the compose network so the provisioned
   datasources resolve. Mount the provisioning and dashboards read-only. Do **not** mount the Grafana
   data volume.
   ```bash
   NET=$(docker inspect cloud-sim-grafana --format '{{range $k,$v := .NetworkSettings.Networks}}{{$k}}{{end}}')
   docker run -d --rm --name grafana-authoring --network "$NET" -p 127.0.0.1:3004:3000 \
     -e GF_AUTH_ANONYMOUS_ENABLED=true -e GF_AUTH_ANONYMOUS_ORG_ROLE=Editor \
     -e GF_AUTH_DISABLE_LOGIN_FORM=true \
     -v "$PWD/config/grafana/provisioning:/etc/grafana/provisioning:ro" \
     -v "$PWD/config/grafana/dashboards:/etc/grafana/dashboards:ro" \
     grafana/grafana:13.1.3
   ```
   **This is an unauthenticated Editor.** It is loopback-bound, but any local process can write to it
   while it runs. Tear it down the moment you are done (step 6) — including on the failure path.
   Match the image tag to the running version; a different tag emits a different `schemaVersion`.
2. **Build in a scratch dashboard**, never in a provisioned one: `/dashboard/new` → sidebar
   `[data-testid="data-testid sidebar add new panel"]` → **Edit visualization**.
3. **Choose a visualization type before anything else.** A panel saved without one serializes as
   `"type": "__unconfigured-panel"` **with no `fieldConfig` at all** — it looks exactly like the broken
   corpus. The type picker is the **All visualizations** tab; the "Start without data" shortcut only
   offers Text / Alert list / Dashboard list.
4. Set the query, then the **standard options** — find them with the options pane's `Search for...` box.
   `input#unit` is the unit combobox; `Add threshold` is under Thresholds.
   - **The unit dropdown has a trap.** Typing `Dollars` offers both `Currency / Dollars ($)` (→
     `"currencyUSD"`) and `Custom unit: Dollars` (→ a free-text unit). Take the registry entry. Reach for
     a custom unit only when nothing in the registry fits, and say why in the panel description.
5. **Extract the artifact via `GET /api/dashboards/uid/<uid>`'s `.dashboard` field — not Dashboard
   settings → JSON Model.** The two are **not equivalent on 13.1.3**: the JSON Model tab shows the new
   dashboard-editor's internal v2 schema (`kind: "Panel"`, `elements`, `layout: GridLayout`,
   `vizConfig`) — a different shape entirely from the classic v1 schema (`panels[]`, `gridPos`,
   `datasource: {type, uid}`, `schemaVersion`) every committed file in this repo uses and every
   reader (the Gate 0 jq script, the acceptance tests, file provisioning itself) assumes. The
   classic REST API still returns v1 — Grafana converts on the way out — so it, not the settings
   tab, is the correct extraction point (found live, FRE-1209: the settings tab's export silently
   produces a schema this repo's own tooling cannot read). **Before saving, confirm any Title/Tags
   edits actually persisted** — editing them in the Settings panel only updates in-browser React
   state until you click the dashboard's own Save (a title collision with a same-named *provisioned*
   dashboard already mounted read-only in the authoring instance will reject that save with "A
   dashboard or a folder with the same name already exists"; use a temporary working title, save,
   then correct `title`/`uid` by hand in the exported JSON before committing).

   Once extracted, copy the panel object out. A correctly UI-authored panel looks like this — this
   exact object came out of the loop above, and it is what a good panel minimally carries:
   ```json
   {
     "type": "stat",
     "pluginVersion": "13.1.3",
     "fieldConfig": {
       "defaults": {
         "color": { "mode": "thresholds" },
         "thresholds": { "mode": "absolute", "steps": [
           { "color": "green", "value": 0 }, { "color": "red", "value": 80 }
         ]},
         "unit": "currencyUSD"
       }
     }
   }
   ```
   **Export hygiene:** drop `id` (the db assigns it), set `version: 1`, and keep `uid` **stable** across
   re-exports so file provisioning overwrites in place instead of creating a duplicate. Commit to
   `config/grafana/dashboards/<name>.json`.
6. **Tear the authoring instance down — always, including after a failure:**
   `docker rm -f grafana-authoring`, then confirm with `docker ps`. Leaving it up leaves an
   unauthenticated Editor running.
7. **Verify on the real instance, not the authoring one — but check which tree it actually mounts
   first.** File provisioning reloads on `updateIntervalSeconds: 30`
   (`config/grafana/provisioning/dashboards/dashboards.yaml`), then render-assert (Step 2). **From a
   worktree** (this repo runs several in parallel — `build`, `build2`, `adrs`, …): confirm with
   `docker inspect cloud-sim-grafana --format '{{range .Mounts}}{{.Source}} -> {{.Destination}}{{"\n"}}{{end}}'`
   before waiting on a reload that can never come. The shared dev stack's `cloud-sim-grafana` mounts
   `/opt/seshat` (the **primary checkout, main branch**), not any worktree path — a branch's dashboard
   JSON living only in its worktree is invisible to it no matter how long you wait (found live,
   FRE-1209: two separate polling loops ran for minutes against a file that could never change).
   **Fix:** stand up a second, throwaway instance the same way as the authoring one but
   `GF_AUTH_ANONYMOUS_ORG_ROLE=Viewer` (matching prod, not Editor) and read-only-mount *your own
   worktree's* `config/grafana/provisioning` and `config/grafana/dashboards` — same network, so the
   `pg-ledger` / ES datasources still resolve against the real backing stores. Render-assert and
   raw-data cross-check against that; tear it down after. Only a build session working directly in
   `/opt/seshat` itself (not a worktree) can skip this and use `cloud-sim-grafana` directly.

**Grafana render-assert** (these two selector names are the vendor's own, from `grafana-e2e-selectors`
at the matching tag):
```js
document.querySelectorAll('[data-testid="data-testid panel content"]').length          // must equal the panel count
document.querySelectorAll('[data-testid="data-testid Panel status error"]').length     // must be 0
```
**This assertion passes on all 68 of today's unconfigured panels.** It is gate 1 and nothing more.
Never report it as evidence the panel is good.

**Playwright input rules (the biggest time sink):**
- Titles / search boxes (gate a submit or filter): **real keyboard** — click → Ctrl+A → Delete → type.
  The native-value-setter desyncs React state and the submit silently no-ops.
- Inline commit-on-change fields (dimension name): native-value-setter + dispatched `input`/`change`.
- **Never mix** injection and typing on one field. Prefer `browser_evaluate` element `.click()` to
  bypass overlay hit-tests; dismiss any blocking toast after each navigate.

## Step 2 — SCRUTINIZE (master re-applies at the gate)

### Gate 0 — the field-config gate (Grafana; mechanical, run it first)

A number without a unit is not a measurement. This gate is mechanical so it cannot be argued with, and it
runs before the human gates because it is cheap.

A panel is **rejected** if any of:
1. **`pluginVersion` is absent** — Grafana's UI always emits it, so its absence means the panel was
   hand-authored. This alone fails the absolute rule.
2. **`fieldConfig.defaults.unit` is absent, empty, or `"none"`.** `none` is a *real* Grafana unit id
   meaning "Number" — a gate that only checks "is set" passes a panel that explicitly declares no unit.
   A dimensionless count is `short`, not absent and not `none`.
3. **Thresholds are missing where the panel is read against a bar** — that is, the panel is a `stat`,
   `gauge` or `bargauge` (a single value the eye compares to *something*), **or** its title/description
   names a budget, target, limit, SLO, cap, ceiling or quota. Required: `fieldConfig.defaults.thresholds.steps`
   with ≥2 steps, at least one carrying a numeric `value`.

```bash
jq -s -r '
[ .[] | .title as $d | .panels[] | {
    dash: $d, panel: .title,
    ui: (has("pluginVersion")),
    unit: (.fieldConfig.defaults.unit // null),
    steps: (.fieldConfig.defaults.thresholds.steps // []),
    needsThresholds: ((.type == "stat" or .type == "gauge" or .type == "bargauge")
      or ((.title + " " + (.description // ""))
          | test("\\b(budget|target|limit|SLO|cap|ceiling|quota)\\b"; "i")))
  } ]
| map(. + {reasons: (
    (if .ui then [] else ["not-UI-authored (no pluginVersion)"] end)
  + (if (.unit == null or .unit == "" or .unit == "none") then ["no-unit"] else [] end)
  + (if (.needsThresholds and ((.steps | length) < 2 or ([.steps[] | select(.value != null)] | length) < 1))
     then ["no-thresholds"] else [] end))})
| .[] | select(.reasons | length > 0) | "REJECT  \(.dash) :: \(.panel)  — \(.reasons | join("; "))"
' config/grafana/dashboards/*.json
```

Empty output means every panel passed. Today it rejects **68 of 68** (68 not-UI-authored, 68 no-unit,
15 no-thresholds) — that is the gate's calibration: **a gate that passes the current corpus is not a
gate.** It also passes a correctly-authored panel, which is the other half of the calibration.

**Keep the keyword test word-anchored.** Written unanchored it matches substrings: `SLO` fires on
*"Slowest traces (top 20)"* and `cap` on *"capacity"*. Caught in review — it had inflated the
no-thresholds count from 15 to 19.

### The four gates (ALL required, in order)

1. **Renders** — draws, no panel-status error, no "Visualization type not found." Necessary, never
   sufficient — see the Grafana render-assert note above.
2. **Accurate** — bands/axes represent what they claim (check the axis title, the aggregation, the labels,
   **and that the query actually answers the title**: an empty query string counts every document in the
   index and reads as a real, large, meaningless number).
3. **Useful to a human** — a person can reach a *correct* decision: shows the whole quantity (a
   denominator / total, not a confounded slice), normalized so volume does not masquerade as composition,
   honest title/axis (**no ticket IDs in a human-facing title**).
4. **Verified against REAL recent data** — the decisive gate. Seeded/empty data lets a *misleading* chart
   pass gates 1-3; only real data exposes it (FRE-593 v1: "100% memory" vs the true 36%). Pull the raw
   rows, replicate the viz's aggregation in a query, and confirm the chart's story matches the numbers.
   - **Check the panel at its own default time range.** A panel whose description asserts a 30d/90d/1-year
     finding, sitting on a dashboard that opens at `now-24h`, renders **empty while its description
     asserts a conclusion**. Nine of the 16 committed dashboards are in exactly this state.
   - **Cache-bust the render-check.** The dashboard SPA caches — a stale panel can render the OLD version
     and give a false verdict (FRE-593 v2 first showed a cached v1). Confirm the *live* object is the new
     one, then force a fresh load.

A chart that leads a human to the wrong answer is worse than no chart. If any gate fails → Iterate.

## Step 3 — ITERATE

Bounce the *specific* scrutiny findings (not "make it better") back into Create; rebuild; re-scrutinize
against real data. Repeat until a human concludes correctly. This loop, not a one-shot ship, is what
produces a viable result.

## Definition of done

The panel **visibly renders AND a human reads the correct story from real data** — proven, not asserted:
a Playwright render-assert plus a raw-data cross-check that the chart's numbers match, and (Grafana) a
clean field-config gate. "The JSON imports", "it provisioned" and "it renders" are all weaker than this
and do not close the ticket. A visualization's acceptance criterion must name **the decision it enables**,
not "a chart exists."

## Anti-patterns

- Hand-authoring / hand-editing visualization JSON in any tool, or copying our own (possibly-broken)
  objects as the template.
- Driving the HTTP API to create the panel — that is hand-authoring with extra steps.
- Treating "import succeeded", "it provisioned" or "it rendered" as done.
- Shipping a number with no unit; using `none` as a unit; a `Custom unit:` where a registry unit exists.
- Absolute bands with no denominator/ceiling (unreadable to a human).
- Sum-over-time when you meant per-turn composition (volume masquerades as composition).
- A description that asserts a finding the panel's own time range cannot show.
- Hardcoding an id (a trace id, a session id) into a query instead of using a dashboard variable.
- Shipping without checking the raw data first.
- Leaving the ephemeral authoring instance running.

## Related

- Dashboard-level composition (shared controls, consistent filters, reading order, cross-viz drill-down,
  whether the *set* answers one coherent question) is planned as a separate **Build** skill,
  `compose-a-dashboard` — **not yet written; do not go looking for it.** It is FRE-1234, blocked on the
  FRE-1207 render audit, because three of its six rules need rendered evidence that no static
  measurement can supply. Until it exists, composing a whole dashboard has no contract: say so rather
  than improvising one.
- Memory: `feedback_visualizations_build_in_ui_not_hand_authored`.
- Grafana corpus evidence and the loop's verification: FRE-1206, and
  `docs/superpowers/plans/2026-08-08-fre-1203-grafana-migration-program.md` § 1.
