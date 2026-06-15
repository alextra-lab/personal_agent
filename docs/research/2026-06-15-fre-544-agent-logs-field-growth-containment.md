# FRE-544 — Bounding `agent-logs-*` dynamic field growth (decision record)

> **Date:** 2026-06-15 · **Ticket:** FRE-544 (Tier-1:Opus) · **Project:** Telemetry Surface Audit
> **Governing ADR:** ADR-0090 (Telemetry Surface Contract) — §D2 disciplines, §D5 enforcement (FRE-540)
> **Refs:** FRE-533 (A1 — 768-field finding, 1023-row CSV) · FRE-534 (A2 trap fixes, preserved here)

## Decision

`agent-logs-*` stays **Guarded-dynamic** (ADR-0090 §D2 — the discipline already assigned to this
family because it is an open-ended telemetry catch-all; **Locked**/`dynamic:false` is reserved for
*closed* field sets like `user-turn-ratings-*`). FRE-544 adds the **field-count bound** the discipline
lacked:

1. **`index.mapping.total_fields.limit: 300`** + **`index.mapping.total_fields.ignore_dynamic_beyond_limit: true`**
   — caps mapped fields; dynamic leaves beyond the cap are skipped to `_source` and the document is
   **still indexed** (never dropped). Cuts the 768-field runaway by >60% with headroom over the ~118
   legitimate explicit fields.
2. **`arguments` → `object` / `dynamic:false`** keeping its 3 dashboard-referenced subfields
   (`name`, `title`, `trace_id` — the last a join key, `keyword`). Collapses the single biggest +
   fastest-growing subtree (66 leaves → 3); new tool-arg keys go to `_source` only.
3. **All 45 dashboard-referenced fields are explicit** (20 newly promoted), so the cap can never skip
   a panel field. The promoted `*_ms`/`offset_ms` durations are explicit `float`, which also hardens
   them against the `0.0→long` trap.
4. **Preserved:** `dynamic:true`, the 5 `dynamic_templates`, all existing explicit props, the
   FRE-534/536 trap fixes, and the `event`/`event_type` `keyword` split.

Drift between emit ↔ mapping ↔ dashboard is enforced by **FRE-540** (ADR-0090 §D5 reconciliation
checker), not by `dynamic:false`. That is the answer to "what process is allowed to create new fields
without syncing observability": none — a new indexed field is a version-controlled template edit, and
FRE-540 flags emit sites that drift from it.

## Alternatives — refuted by measurement on the live local ES (8.19)

Per the standing "you always get the mappings wrong first pass" rule, the strategy was chosen by
probing throwaway indices, not by assertion:

| Candidate | Probe | Verdict |
|---|---|---|
| `dynamic:"runtime"` | A novel **nested object** (`context.deep`) → `illegal_state_exception`, **HTTP 500 — whole doc dropped**. The `default_string_keyword` catch-all still **indexed** every string (runtime never engaged). | **Refuted** — drops telemetry; doesn't even bound. |
| `dynamic:"strict"` | rejects any unknown-field doc | Refuted — drops telemetry. |
| `dynamic:false` (Locked) | safe for nested objects, but **disables `dynamic_templates`** (loses `*_ms`→float, `*_id`→keyword, free-text typing) and makes the 507 sprawl leaves `_source`-only. Needs a full *consumer* inventory (dashboards + self-telemetry skill + ES\|QL + scripts) or query paths silently go dark. | Rejected for an open catch-all (would be an ADR-0090 revision). |
| **Guarded-dynamic + cap + `ignore_dynamic_beyond_limit` + `arguments` collapse** | over-cap doc (40 novel leaves, cap 12) → **HTTP 201, indexed**, excess skipped to `_source`; nested objects index fine; `dynamic_templates` + FRE-534 typing intact; `arguments` dynamic:false keeps 3 subfields, ignores novel args (3000-char arg fine). | **Chosen.** |

**Real-artifact check:** the actual edited template (renamed to a throwaway pattern) PUT cleanly →
index created → doc indexed (201) → `_settings` shows `limit:300, ignore_dynamic_beyond_limit:true`,
`arguments.dynamic:false` (only name/title/trace_id mapped), `cpu_load` `0.0`→`float` (trap avoided),
`actual_cost_usd`→`double`. Probe indices cleaned up.

## Field data (from the A1 1023-row CSV)

- `agent-logs-*`: **768** mapped leaves; **507** generic "emitted-but-unmapped" sprawl (broad, **not**
  concentrated in `arguments` — that subtree is 66 of 768).
- **45** fields are dashboard-referenced; 25 already explicit, **20 promoted** here.
- Legitimate explicit set after this change ≈ **118**; cap **300** leaves ~180 dynamic slots for
  ad-hoc/incident fields while bounding the runaway.

## Reindex / age-out

No reindex. The bound applies to **new** daily indices created after the template is re-registered.
Existing ≤768-field `agent-logs-YYYY.MM.DD` indices are **left untouched** and **age out via the 30d
ILM** (`docker/elasticsearch/ilm-policy.json`). No historical data is dropped or quarantined.

## Follow-up (not in this PR)

ADR-0090 §D2 wants Guarded-dynamic `dynamic_templates` to cover **numerics** so a *new, unlisted*
float first-seen as `0.0` doesn't infer `long`. The current `agent-logs` rules cover `*_ms`-style
names but not a generic numeric-name pattern; the explicit-prop promotions here cover the *known*
numerics. A generic numeric `dynamic_template` is a trap-correctness extension (FRE-534/A2 lineage),
worth a follow-up if recurrence is observed.
