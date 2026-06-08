# FRE-534 (A2) — ES template corrections: reindex / rollover plan

> **Date:** 2026-06-08 · **Ticket:** FRE-534 (A2) · **Project:** Telemetry Surface Audit
> **Pairs with:** `docs/research/2026-06-08-fre-533-telemetry-surface-reconciliation.md` (the A1 table)
> **Templates changed:** `docker/elasticsearch/` (agent-logs corrected; captains split 3-way; insights +
> slm-health authored) · registration in `scripts/setup-elasticsearch.sh`.

## How new templates take effect

ES composable index templates apply to **new indices only**. Every in-scope family rotates **daily**
(`…-YYYY.MM.DD` or `…-YYYY-MM-DD`); `agent-logs` additionally has the `agent-logs` write-alias + ILM
rollover (7d / 1gb). So the corrected mappings land automatically on the next day's index (or next
rollover) with **no action on existing indices**. Existing daily indices keep the mapping they were
created with.

**Deploy step (master, not build):** re-run `bash scripts/setup-elasticsearch.sh` against the target ES.
It is idempotent; the one new behaviour is a `DELETE /_index_template/agent-captains-template` before the
split captures/reflections templates are PUT — required because the retired template's patterns overlap
the replacements at equal priority (110) and ES rejects an equal-priority overlap. Verify afterwards:

```bash
curl -s "$ES_URL/_index_template/agent-captains*" | jq '.index_templates[] | {name, priority:.index_template.priority, patterns:.index_template.index_patterns}'
# expect: captures=110, reflections=110, subagents=120; agent-captains-template ABSENT
```

## Per-family disposition

| Family | What was wrong in existing data | Backfill? | Rationale |
|---|---|---|---|
| `agent-logs-*` | `*_ms`/`*_seconds`/`*_threshold` froze to `long` (0.0 first-seen); 17 long-text fields capped at `keyword:1024` | **No** | Historical `long` durations are integer-valued but still aggregate/sort correctly; the >1024 keyword drop affected indexing of a handful of values, not retrievable `_source`. Correct from the next daily/rollover index. |
| `agent-insights-*` | `evidence.component_id` = `text` (prompt-component join key tokenised); cost/ratio float-by-luck | **No** | Family has **no Kibana index-pattern / dashboard yet** (A1) — nothing consumes the historical join. Correct from next daily index; reindex recipe below if a consumer later needs history. |
| `agent-monitors-slm-health-*` | `trace_id` = `text` (join key tokenised); only 7 of 14 model fields ever mapped | **No** | No index-pattern yet (A1); join has no consumer. Correct from next probe's daily index. The other 7 model fields simply begin mapping when first populated. |
| `agent-captains-captures-subagents` | inherited the captures glob; 10 sub-agent fields fell to dynamic mapping (types happened to be correct) | **No** | Types already correct in the one live index; the carved priority-120 template prevents future drift. |
| `agent-captains-captures-*` / `-reflections-*` | healthy (A1: 0 breaking findings) | **No** | The split is forward-only and changes no data. |

**Net: no backfill required for any family.** Every corrected type either (a) only matters for *new* joins/
aggregations that have no historical consumer, or (b) leaves existing `_source` fully readable.

## Reindex recipe (only if a consumer later needs corrected historical data)

For a family `F` whose corrected template is registered, copy an old daily index into a new one that
inherits the corrected mapping:

```bash
# 1. Create the destination from the (already-registered) corrected template:
curl -s -X PUT "$ES_URL/<F>-<date>-reindexed"
# 2. Reindex source -> dest (dest mapping comes from the corrected template):
curl -s -X POST "$ES_URL/_reindex" -H 'Content-Type: application/json' -d '{
  "source": { "index": "<F>-<date>" },
  "dest":   { "index": "<F>-<date>-reindexed" }
}'
# 3. Spot-check the corrected field type, then swap via alias if desired.
```

Fields whose *type category* changes (e.g. `text`→`keyword` join keys, `long`→`float` durations) are
reindex-compatible — values re-parse cleanly. This recipe is documented for completeness; per the table
above it is **not** part of the FRE-534 deploy.

## Verification performed (build session, non-disruptive)

For each corrected family a throwaway index was built **directly from the template body**, a document with
trap-triggering values was indexed (`wait_ms: 0`, a >1024-byte `denial_reason`, `evidence.component_id`,
`trace_id`, sub-agent `*_chars`, …), and `GET _mapping` confirmed each field resolves to the intended
type — proving the dynamic rules fire, not just the explicit props. Genuine integers
(`threshold_violations_count`, `iteration`) correctly stayed `long`. Temp indices were deleted. No shared
live template was mutated (that is the master deploy step above).
