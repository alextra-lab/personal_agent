# FRE-1035 — Correct the ES skill docs' index-granularity recipe

> **Linear**: [FRE-1035](https://linear.app/frenchforest/issue/FRE-1035) — Tier-2:Sonnet, Approved, Urgent
> **Plan author**: Claude (Sonnet, build session)
> **Plan date**: 2026-07-31
> **Backing ADRs**: none (documentation/tooling correctness fix, not a new architectural surface)

## What happened (from the ticket)

A live turn asked the agent about ES index granularity. The agent normalised index
names with two shell substitutions — one stripping a trailing dash-date, one
stripping a trailing dot-date but only the month/day (leaving the year attached).
Run against a dot-dated name, the second substitution produced `agent-logs-2026`, a
string that looks like a yearly index but is a substitution artifact. The agent
reported it as real. A follow-up grep for four-digit-year names found its own
artifact and "confirmed" the mistake.

No skill doc in this repo actually contains that broken sed recipe — grepped
`docs/skills/*.md` and the wider repo for date-stripping shell patterns and found
none. The defect is an absence, not a stale recipe: nothing in
`docs/skills/query-elasticsearch.md` tells the agent how to safely determine a
family's granularity, so it improvised, and the improvisation was fragile in
exactly the way the ticket describes.

## Current live state (verified 2026-07-31, read-only `_cat/indices` against this
VPS's real cluster, `:9200`)

FRE-1036 (index consolidation, merged to main, **Awaiting Deploy** in Linear) is
already live-writing new `<family>-YYYY-MM` (dash, monthly) indices — this dev
environment's running service is the live service (uvicorn --reload against the
same cluster), so merged code takes effect immediately regardless of the Linear
deploy-gate state. The old daily-shaped indices have **not** been migrated/deleted
yet (that's FRE-1036's own remaining migration step). So right now, for real:

- `agent-logs`: 60 dot-daily indices (26 with a `-v2` suffix) **+** 1 dash-monthly
  (`agent-logs-2026-07`).
- `agent-insights`: dash-daily indices (2026-04 through 2026-06) **+** dash-monthly
  indices (`agent-insights-2026-04` … `-2026-07`).
- `agent-monitors-slm-health`: dot-daily **+** dash-monthly.
- `agent-monitors-joinability`: dot-daily **+** dash-monthly.
- `agent-captains-captures`: dash-daily (some `-v2`-suffixed) **+** dash-monthly.

So all four shapes the ticket names — dash-daily, dot-daily, either with a `-v2`
suffix, and genuinely monthly — are simultaneously present, live, right now,
across multiple families. This is a broader, more current instance of the same
heterogeneity the ticket reports, not a hypothetical.

## Codex plan-review (2026-07-31, `docs/superpowers/plans` review pass) — findings applied below

1. **Recipe priority reversed.** Min/max `@timestamp` span cannot reliably tell
   daily from monthly for a sparse or newly-created index (a monthly bucket with
   one day of data so far looks "daily" by span) and misses empty indices
   entirely. The tested name-classifier is the correct **primary** answer for "what
   shape is this index" — that's a naming-convention question, and the classifier
   is a robust, tested replacement for the ticket's fragile ad hoc shell
   substitution, not a re-introduction of the same risk. Live aggregation is
   **secondary**: useful as coverage evidence ("did this query actually see all
   the data it should have"), not as the granularity classifier.
2. **Family-aware timestamp field, confirmed.** `agent-captains-captures-*` and
   `agent-captains-reflections-*` use a plain `timestamp` field, not `@timestamp`
   (`docs/skills/self-telemetry.md:96-98`, `src/personal_agent/captains_log/capture.py:64-67,247-255`,
   `src/personal_agent/captains_log/models.py:225-229`, `docker/elasticsearch/captains-captures-index-template.json:53-55`,
   `docker/elasticsearch/captains-reflections-index-template.json:53-55`). Any
   coverage-check recipe must name the field per family. This also surfaced a
   live, pre-existing bug in the same doc family (below).
3. **Missing shape: dot-monthly (`YYYY.MM`).** The FRE-1036 migration script
   itself still recognizes dot-monthly for `agent-monitors-slm-health` and
   `user-turn-ratings` (`scripts/migrate_fre1036_monthly_indices.py:146-154`) —
   the classifier and its fixture need a sixth shape, not five.
4. **Sibling-prefix protection needs its own test.** `agent-captains-captures-*`
   also matches `agent-captains-captures-subagents-*`; production code already
   excludes that sibling explicitly (`src/personal_agent/captains_log/capture.py:555-559`).
   The classifier's `family_prefix` argument must not misclassify a sibling
   family's name as the parent's, and a test must prove it.
5. **The existing "Actual indices" table is directly contradictory, not just
   incomplete.** `docs/skills/query-elasticsearch.md:83-96` currently states there
   are four families, all daily, and instructs the agent to use *only* those
   patterns. Appending a new subsection after it leaves two conflicting sources of
   truth in the same doc. The table itself must be corrected, not just supplemented.
6. **`TELEMETRY_ELASTICSEARCH_INTEGRATION.md`'s whole partition table is stale,
   not just the insights/slm-health rows** (`:164-185`) — `agent-logs` and
   `agent-monitors-joinability` are also listed as strictly daily, which is
   inconsistent with what's live today and with FRE-1036's monthly target
   (`docs/superpowers/plans/2026-07-31-fre-1036-es-ilm-monthly-rollover.md:150-164`).
   Correct the whole table, framed as a migration-window condition tied to
   FRE-1036 (not a permanent state) — instruct inventory-at-query-time via the new
   recipe rather than re-asserting a fixed shape per family.
7. **Drop the plan's "manual live sanity check" as a required build step.** It
   assumed sandbox access to `:9200` that a review/build agent may not have. I
   (the build agent) already ran the equivalent check directly against the real
   cluster during investigation (see "Current live state" above and the PR/ticket
   evidence) — that observation stands as the live proof; the repo-side
   deliverable stays fixture-only (unit tests), no required live step.
8. **Out of scope, noted for FRE-1036 tracking (not actioned here):** the
   migration script's source-pattern config for `agent-monitors-slm-health` only
   recognizes dot-*monthly* sources, but live data also has dot-*daily* sources
   for that family — worth flagging to FRE-1036, not this ticket's concern.

## What to change

### 1. `scripts/es_index_granularity.py` (new) — the tested, robust classifier

A small, pure module (no live-ES call in the pure path):

- `classify_index_period(index_name: str, family_prefix: str) -> IndexPeriod | None`
  — matches the suffix after `family_prefix + "-"` against each known shape
  **exactly** (anchored regex per shape: dash-daily, dot-daily, either with a
  trailing `-v2`, dash-monthly, dot-monthly — six shapes total per finding #3).
  Returns `None` for anything that matches no known shape, including a name
  belonging to a sibling family sharing the prefix (finding #4) — it never falls
  back to truncating/guessing. This is the direct fix for the ticket's root
  cause: the failure mode was reconstructing a plausible-looking name from a
  partial strip; this function only ever returns a period it matched in full,
  for the exact family requested.
- `report_family_granularity(index_names, family_prefix) -> FamilyGranularityReport`
  — buckets a family's index names into daily / monthly / unrecognized, with an
  `is_mixed` property. `unrecognized` is always surfaced, never silently dropped.
- A small `main()` CLI: reads index names on stdin (e.g. piped from
  `curl .../_cat/indices?h=index`), takes the family prefix as `argv[1]`, prints
  the counts, flags `MIXED`, and lists any unrecognized names.

This mirrors this repo's existing pattern for "the shell needs to do something
nontrivial correctly" (`scripts/migrate_fre1036_monthly_indices.py` already solves
the same per-shape-regex problem for the migration's own source enumeration;
`tests/scripts/test_es_templates.py` is the static-test precedent). Not reusing
the migration script directly — it is one-off migration tooling with its own
family config tied to the migration's lifecycle, and coupling a standing skill
recipe to it would break the recipe once that script is deleted post-migration.

### 2. `tests/scripts/test_es_index_granularity.py` (new) — the guard test

Fixture covering all six observed shapes (dash-daily, dot-daily, dash-daily+v2,
dot-daily+v2, dash-monthly, dot-monthly) plus the adversarial cases that prove
"invents nothing":
- The exact bug artifact from the ticket (`agent-logs-2026`, a truncated
  month/day-only strip) → `None`.
- A name for a different, unrelated family prefix → `None`.
- A sibling-family name under the parent prefix (`agent-captains-captures-subagents-2026-04-15`
  classified with `family_prefix="agent-captains-captures"`) → `None` (finding #4).
- `report_family_granularity` on a mixed real sample (e.g. the live
  `agent-monitors-joinability` shape today) → `is_mixed is True`, correct counts,
  nothing unrecognized silently missing.

### 3. `docs/skills/query-elasticsearch.md` — correct the recipe and the table

- **Rewrite** the "Actual indices" section/table (currently `:83-96`, claims four
  daily-only families) rather than appending contradicting guidance after it —
  state plainly that granularity is currently mixed per-family and must be
  checked, not assumed from a fixed pattern.
- New subsection: **"Determining a family's index granularity — don't strip dates
  in the shell."**
  - States the failure mode plainly (a month/day-only shell strip leaves the year
    attached and fabricates a plausible-looking nonexistent index — this is
    exactly what produced a wrong answer once, FRE-1035) and says explicitly:
    never reconstruct a date pattern by truncating another one.
  - Primary recipe — pipe `_cat/indices` through
    `scripts/es_index_granularity.py <family-prefix>` (the tested classifier from
    #1) instead of ad hoc `sed`/`cut`.
  - Secondary recipe — as coverage evidence only (not granularity classification,
    per finding #1): one `_search` aggregation (`terms` on `_index`, nested
    `min`/`max` on the family's real timestamp field) against the family wildcard.
    States explicitly that the timestamp field is family-specific — `@timestamp`
    for `agent-logs`/`agent-insights`/`agent-monitors-*`, plain `timestamp` for
    `agent-captains-captures-*`/`agent-captains-reflections-*` (finding #2).
  - States today's empirically-verified reality, framed as a migration-window
    condition tied to FRE-1036 (finding #6): several families currently have
    **both** legacy daily-shaped indices and new monthly indices live
    simultaneously — a query or index-name assumption scoped to only one shape
    will silently miss data; always use the family's full wildcard, with the
    caveat that some wildcards intentionally catch a sibling family (e.g.
    `agent-captains-captures-*` also matches `-subagents-*`) so a per-family
    inventory check is still worth running, not just a wildcard search.

### 4. `docs/guides/TELEMETRY_ELASTICSEARCH_INTEGRATION.md` — correct the whole
partition table, not just two rows

Lines ~164–185 state flatly that families are strictly daily or strictly
monthly (`agent-logs`/`agent-monitors-joinability` "daily"; `agent-insights`/
`agent-monitors-slm-health` "monthly"), with no mention that both shapes are
live simultaneously for all of them right now. This is exactly the "second way
the current guidance produces a confident undercount" the ticket names, and per
finding #6 it's the whole table, not the two rows I first scoped. Corrected to
state the current coexistence as a migration-window condition (dated, same
verification as above, linked to FRE-1036) and point at the new
granularity-check recipe in `query-elasticsearch.md` rather than re-deriving it
here.

### 5. `docs/skills/self-telemetry.md:239` — fix the pre-existing `@timestamp` bug

`FROM agent-captains-captures-* | WHERE @timestamp > NOW()-24hours ...` filters
on a field that doesn't exist on that index (finding #2) — `agent-captains-captures-*`
uses `timestamp`. This ES|QL query returns empty today, silently. Same root
cause class as the ticket (a wrong assumption about ES field naming producing a
confidently wrong/empty result) and directly adjacent to the family-aware
timestamp guidance being added in #3 — folding in per Step 5 (supporting fix,
not separate-ticket-worthy). One-line fix: `@timestamp` → `timestamp`.

## Not in scope

- Migrating/deleting any live index — that's FRE-1036's own remaining step, not
  this ticket's.
- The FRE-1036 migration script's dot-daily-vs-dot-monthly source-pattern gap for
  `agent-monitors-slm-health` (finding #8) — flagged for FRE-1036, not fixed here.
- Any change to `src/personal_agent/` — this is a docs + ops-script fix, no
  runtime behavior changes.

## Acceptance criteria (this ticket's own)

1. The guard test (`tests/scripts/test_es_index_granularity.py`) passes and
   demonstrably classifies all six observed shapes correctly, returns
   `None`/unrecognized (never a guess) for the ticket's own reproduced bug
   artifact, and correctly rejects a sibling-family name under a parent prefix.
2. `docs/skills/query-elasticsearch.md`'s index-inventory guidance no longer
   contradicts itself and no longer leaves the agent to improvise a
   date-stripping recipe — the "Actual indices" table reflects current mixed
   reality, the tested script is the primary granularity check, and the
   secondary aggregation recipe names the correct field per family.
3. `docs/guides/TELEMETRY_ELASTICSEARCH_INTEGRATION.md`'s partition table no
   longer implies any in-scope family is monthly-only or daily-only when it
   currently is not.
4. `docs/skills/self-telemetry.md:239`'s `@timestamp`/`timestamp` field mismatch
   is fixed.
5. `make test`, `make mypy`, `make ruff-check`/`make ruff-format` all clean on
   the new/changed files.

## Test plan

1. `uv run pytest tests/scripts/test_es_index_granularity.py -v` — write first,
   confirm failing (module doesn't exist), then implement until green.
2. `make mypy` / `make ruff-check` / `make ruff-format` on
   `scripts/es_index_granularity.py`.
3. Live evidence already captured during investigation (this session, read-only,
   this VPS's real `:9200`) stands as the PR's live-verification proof — no
   further live step required to close this ticket (finding #7):
   `curl -s 'http://localhost:9200/_cat/indices?h=index' | grep '^agent-monitors-joinability-' | python3 scripts/es_index_granularity.py agent-monitors-joinability`
   → `MIXED`, matching the live state captured above.
