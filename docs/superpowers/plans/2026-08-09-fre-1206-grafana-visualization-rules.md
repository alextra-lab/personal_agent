# FRE-1206 (T0) — Apply the visualization rules to Grafana

> **Ticket:** FRE-1206, Approved, `stream:build1`, Tier-1:Opus
> **Parent plan:** `docs/superpowers/plans/2026-08-08-fre-1203-grafana-migration-program.md` § T0
> **Date:** 2026-08-09

Contract authoring, not implementation. Three deliverables: generalize `create-visualization`,
author `compose-a-dashboard`, update the memory.

---

## 1. Evidence base — re-measured this session, not inherited

Every number below was computed in this session against `config/grafana/dashboards/*.json` at
`origin/main`, not copied from the parent plan. Where my measurement differs from the plan's, mine
is stated and the difference is called out.

| # | Defect | Measurement | Command |
|---|---|---|---|
| D1 | Panels with **no `fieldConfig` key at all** | **68 of 68** | `jq -s '[.[].panels[] \| select(has("fieldConfig")\|not)] \| length'` |
| D2 | Panels carrying a unit | **0 of 68** | `jq -s '[.[].panels[] \| select(.fieldConfig.defaults.unit != null)] \| length'` |
| D3 | Dashboards with a description | **1 of 16** | `jq -s '[.[] \| select((.description // "") != "")] \| length'` |
| D4 | Dashboards with a `templating` key | **0 of 16** | `jq -s '[.[] \| select(has("templating"))] \| length'` |
| D5 | Panel types used | **4** — `timeseries` 30, `barchart` 28, `table` 5, `stat` 5 | `jq -s '[.[].panels[].type] \| group_by(.)'` |
| D6 | Panels sharing a row band (same `gridPos.y` as another panel) | **0, in every one of the 16** | per-file `jq '[.panels[].gridPos.y] \| group_by(.) \| map(select(length>1)) \| length'` |
| D7 | Panel widths | 63 × `w:12`, 5 × `w:24` — of a 24-column grid | `jq -s '[.[].panels[].gridPos.w] \| group_by(.)'` |
| D8 | `terms` aggregations hardcoded to `size: 10` | **48 of 48** | `jq -s '[.[].panels[].targets[]? \| .. \| objects \| select(.size != null) \| .size]'` |
| D9 | Panels with an **empty query string** | **7** | `jq -s '.[] as $d \| $d.panels[] \| select(.targets[]?.query == "")'` |
| D10 | Hardcoded UUID in a query | **1** — `763278fa-…` in `request_traces.json` | `grep -o '<uuid-regex>'` |
| D11 | Ticket id in a human-facing dashboard title | **1** — `FRE-1072 Health Check` | `jq -s '.[] \| select(.title \| test("FRE-"))'` |
| D12 | Dashboards at `now-24h` while descriptions claim 30d/90d/1y | 15 of 16 at `now-24h`; `health_check` at `now-1h` | `jq -s '.[] \| .time.from'` |

**Divergence from the parent plan, stated:** the plan names **one** empty-query panel
(`turn_session_artifact` panel 4). I measure **seven** — five on `monitors_joinability_slm`, one on
`self_improvement_funnel`, one on `turn_session_artifact`. The larger number is used.

## 2. Environment facts — verified live this session

Each was checked against the running instance, not assumed. These are what make the Grafana arm
*executable* rather than plausible.

| Fact | How verified | Result |
|---|---|---|
| Grafana version | `GET /api/health` | `13.1.3` |
| Provisioned dashboards are **not editable in the UI** | `GET /api/dashboards/uid/fre1072-cost-budget` | `meta.provisioned=true`, `canSave=false`, `canEdit=false` |
| Anonymous role is Viewer, and a **Viewer cannot author** | Playwright `GET /dashboard/new` on :3003 | redirected to `/` |
| The UI login form is **disabled**, so you cannot sign in to get an editor session | `POST /login` | `400 auth.client.notConfigured` |
| Basic auth works for the **API** only | `curl -u admin:… /api/user` | `isGrafanaAdmin: true`, has `dashboards:create` |
| An **ephemeral Editor instance** unblocks authoring | throwaway container, `GF_AUTH_ANONYMOUS_ORG_ROLE=Editor`, loopback-only | `/dashboard/new` loads the panel editor |
| Render-assert selectors exist and are correct | Playwright on `/d/fre1072-cost-budget` | 6 × `data-testid panel content`, 0 × `data-testid Panel status error` |
| Selector names are the vendor's, not invented | `grafana-e2e-selectors/src/selectors/components.ts` @ `v13.1.3` | `"data-testid panel content"`, `"data-testid Panel status ${status}"` |
| The five example unit ids are real | `packages/grafana-data/src/valueFormats/categories.ts` @ `v13.1.3` | `currencyUSD`, `ms`, `short`, `percentunit`, `bytes` all present; **`none` is also a real id meaning "Number"** |

**The two findings that change what the skill must say:**

1. **The parent plan's build loop is not executable as written.** It says "build at `http://127.0.0.1:3003`
   (anonymous Viewer works)". Anonymous Viewer works for *viewing*; it cannot open the panel editor, and
   the provisioned files are `canEdit:false`, and the login form is disabled so there is no way to
   upgrade the session in the UI. The skill must document the ephemeral-Editor-instance step or AC-3 fails.
   **The shared cloud-sim instance must not be widened to Editor** — it is reachable through the
   Cloudflare tunnel, so that would be a live posture change, not a dev convenience.
2. **`none` is a valid unit id.** A gate written as "`unit` is set" passes a panel that explicitly says
   "no unit". The gate must reject absent, empty **and** `none`.

## 3. Deliverables

### T0.1 — `.claude/skills/create-visualization/SKILL.md`

Unchanged (tool-agnostic and correct): the create→scrutinize→iterate loop, the four gates, Step 0,
documentation-first, the anti-patterns, the definition of done.

Changed:
- **Frontmatter description/triggers** — add Grafana, panel, dashboard, Postgres datasource.
- **Absolute rule restated tool-neutrally** — never hand-author visualization JSON in any tool. Keep
  the Lens `visualizationType` story; add the Grafana instance (D1: 68/68 no `fieldConfig`).
- **Split Step 1 into a Kibana arm and a Grafana arm.** Kibana arm unchanged. Grafana arm carries the
  §2 verified facts: the ephemeral Editor instance, build in a scratch dashboard, extract via JSON
  Model / `/api/dashboards/uid/<uid>`, export hygiene (`id` removed, `version: 1`, stable `uid`),
  provisioning reload ≤30s, tear the scratch instance down.
- **Grafana render-assert** — count of `[data-testid="data-testid panel content"]` equals the panel
  count **and** zero `[data-testid="data-testid Panel status error"]`. State that this passes today's
  68 unconfigured panels, so it is gate 1 only and never sufficient.
- **Grafana v13.1 documentation-first links**, version-pinned.
- **The field-config gate**, with the exact `jq` that applies it, and `none` explicitly rejected.

### T0.2 — `compose-a-dashboard` — **split out to FRE-1234** (see §5)

### T0.3 — memory `feedback_kibana_lens_build_in_ui_not_hand_authored.md`

Record that the rule is tool-agnostic, that the Kibana-scoped trigger is what let FRE-1072 through,
and that `compose-a-dashboard` is no longer parked. Add the `[[…]]` link.

## 4. Acceptance criteria → proof

| # | Criterion | Proof |
|---|---|---|
| AC-1 | Trigger fires on a Grafana task | Frontmatter `description` names Grafana, panel, dashboard, Postgres datasource; grep shows it |
| AC-2 | Absolute rule forbids hand-authoring in any tool | The rule sentence names no vendor; grep for `Lens` shows it only in the worked example |
| AC-3 | Grafana build loop executable as written | Every step in §2 verified live; the loop is driven once end to end and the resulting UI-authored panel JSON is quoted in the skill as the worked example |
| AC-4 | The field-config gate **can fail** | The skill's own `jq` gate, copy-pasted and run from the repo root against `config/grafana/dashboards/*.json`, returns **68 rejected, 0 passed** (68 not-UI-authored, 68 no-unit, 15 no-thresholds), **and passes a correctly-authored panel** — both halves of the calibration |
| AC-5 | `compose-a-dashboard` cites measured evidence | **Moved to FRE-1234** with the deliverable — see §5 |

## 5. AC-5 / T0.2 — split out to FRE-1234

AC-5's check reads "traces to a numbered defect in **the T3 audit**". FRE-1207 is `Approved`,
unstarted, with no `stream:` label; `docs/research/2026-08-08-grafana-dashboard-render-audit.md`
does not exist. FRE-1206 anticipates this: *"land T0.1 first and T0.2 after T3."*

**The plan's first draft chose to write T0.2 now** from this session's static measurements. The codex
plan-review refuted that, and the refutation held up against the measurements: of the six rules
`compose-a-dashboard` must carry, **three are not supported by any static measurement** —

- *layout is reading order / forbid the 12/0 checkerboard* — D6 measures 0 shared row bands and D7
  measures 63/68 at `w:12`, but "the reading order is wrong" is a **rendered** judgment, not a
  `gridPos` fact;
- *no redundant or contradictory panels* — nothing in the JSON measures redundancy or contradiction;
- *drill-down* — not measured at all.

Writing those three now would put unevidenced instructions into the skill that governs T4, the
exemplar eight rebuilds copy — which is the exact failure FRE-1206 exists to end. The memory's
parking condition (*"do NOT write from theory"*) is not yet discharged.

**Chosen course: T0.1 + T0.3 land here; T0.2 becomes FRE-1234**, `Needs Approval`, blocked by
FRE-1207, carrying AC-5. FRE-1206's own AC-1–AC-4 are proven on this PR. This is surfaced to master
rather than decided silently.

## 5b. Codex plan-review — disposition

Run before implementation. Findings acted on, and the two rejected with reasons:

| Finding | Disposition |
|---|---|
| CRITICAL — "any panel with a good/bad reading carries thresholds" is unfalsifiable | **Fixed.** Replaced with a checkable predicate: required for `stat`/`gauge`/`bargauge`, or when title/description names a budget/target/limit/SLO/cap/ceiling/quota; shape is ≥2 `steps` with ≥1 numeric `value`. |
| MAJOR — the unit check passes any non-empty string | **Fixed, and the trap turned out to be real.** Grafana's unit dropdown offers `Custom unit: <text>` beside the registry entry, so free-text units are one keystroke away. The skill names the trap and restricts custom units to "nothing in the registry fits, say why in the description". |
| MAJOR — the gate is only shown rejecting a corpus that lacks `fieldConfig` entirely | **Fixed.** Added a positive control: the gate passes the real UI-authored panel and a unit-only timeseries, and rejects six negative fixtures (`none`, empty, stat-without-thresholds, base-step-only, budget-titled-without-thresholds, no-`pluginVersion`) each for the right reason. |
| MAJOR — no evidence missing `fieldConfig` is the *same failure class* as the Lens defect | **Fixed with stronger evidence than asked for.** All 68 panels share **exactly one** key set and carry no `pluginVersion` or `options`, both of which the UI emits unconditionally — hand-authoring is proven from shape alone, independent of the render question. |
| CRITICAL — "executable" was verified only as far as the editor opening | **Fixed.** Drove the loop end to end: ephemeral instance → scratch dashboard → visualization type → unit + threshold → save → `GET /api/dashboards/uid/` → the extracted panel object, which is now the skill's worked example. Found two failure modes on the way (a typeless panel serializes as `__unconfigured-panel` with no `fieldConfig`; the type picker is behind "All visualizations", not "Start without data"). |
| MAJOR — ephemeral-container security/hygiene | **Fixed.** The skill states it is an unauthenticated Editor, binds it to loopback, mounts config read-only and no data volume, forbids widening the shared tunnel-exposed instance, and requires teardown **including on the failure path**. |
| CRITICAL — T0.2 should not be written before T3 | **Accepted** — see §5. |
| MAJOR — split into a router + per-platform skills | **Rejected.** FRE-1206 specifies generalizing the one skill and keeping the loop/gates/anti-patterns unchanged; a router refactor diverges from the design the ticket and parent plan set out (ADR-0130 D3 design adherence). Length is mitigated by clearly-labelled arms. Worth revisiting as its own proposal if the file grows again. |
| MAJOR — several D-claims overreach their `jq` | **Fixed by measuring, not softening.** All 16 dashboards confirmed `canEdit:false` (not one). All 48 `size:10` confirmed to be `terms` aggregations (exactly 48 exist). The long-window claim measured: **9 of 16 dashboards, 31 panels**. |
| MINOR — "basic auth works for the API only" overstated | **Fixed.** Restated as what was measured: basic auth authenticates the HTTP API and does not give the browser a session. |
| MAJOR — "provisioning reload ≤30s" asserted without a timed trial | **Accepted, and the claim withdrawn.** The skill cites `updateIntervalSeconds: 30` from the provisioning config as configuration, not as a measured reload time. A timed trial would require writing into the shared instance's dashboard directory while another session is working in it. |

## 6. Steps

1. Drive the Grafana authoring loop once end to end; capture the UI-authored panel JSON. → verify: JSON carries `fieldConfig.defaults.unit` and `thresholds.steps`. **Done** — `unit: "currencyUSD"`, 3 threshold steps, `pluginVersion: "13.1.3"`.
2. Write the `jq` field-config gate; run it against the 68 and against positive/negative fixtures. → verify: 68 rejected, 0 passed; fixtures 2 pass / 6 reject, each for the right reason. **Done.**
3. Rewrite `create-visualization/SKILL.md`. → verify: AC-1, AC-2 greps. **Done.**
4. File T0.2 as its own ticket blocked by FRE-1207. → verify: ticket id recorded. **Done — FRE-1234.**
5. Rename + update the memory (the Kibana-scoped *name* is the same defect as the Kibana-scoped trigger). → verify: MEMORY.md index line and the skill's § Related both point at the new name; no stale refs. **Done.**
6. Tear down the ephemeral authoring container. → verify: `docker ps` shows no `grafana-authoring`. **Done.**
7. Quality gates + self-review, then PR.

## 6b. Self-review disposition (`feature-dev:code-reviewer`, on the committed diff)

Diff class: **self-serve** — process/skill wording, no production write path, no schema, no deleting
path, no cost/governance code, no change to the trust ladder. Two Important findings, both confirmed
against the real corpus, both fixed on-branch:

| Finding | Fix |
|---|---|
| **The `needsThresholds` keyword test was unanchored, and misfired on the live corpus.** `SLO` matched the substring in *"Slowest traces (top 20)"*, flagging two `request_traces` panels as needing an SLO threshold. The stated calibration was therefore partly an artifact of the bug. | Anchored to `\b(…)\b`. Re-measured: **no-thresholds 19 → 15**; total rejected stays 68/68. The skill now carries the trap as a written warning, since anyone re-typing the regex would reintroduce it. |
| **`## Related` claimed `compose-a-dashboard` exists and "is written from the FRE-1207 render audit"** — contradicting §5 of this very plan, and pointing a future session at two files that do not exist. Introduced when T0.2 was split out and that section was not updated. | Restated as a forward pointer: not yet written, FRE-1234, blocked on FRE-1207, and *"until it exists, composing a whole dashboard has no contract: say so rather than improvising one."* |

The reviewer independently verified and found sound: the `jq` slurp/shape, `null + string` safety, the
`steps`/`unit` predicates, the 68/68 `pluginVersion`+`fieldConfig` claim, the `docker inspect --format`
template and its quoting, `$PWD` resolution from repo root, the `updateIntervalSeconds: 30` claim, and
all four cited Grafana v13.1 doc URLs. No secrets, personal paths or deployment identifiers.

## 7. Out of scope

- The CI gate repoint (`scripts/audit/telemetry_surface_check.py`) — that is T8.1, its own ticket.
- Any change to the running dashboards — that is T4/T5/T7.
- Any change to the shared Grafana instance's auth posture.
