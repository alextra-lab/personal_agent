# FRE-328 — Evaluation Window

**Status**: gate reset — collecting clean data under new taxonomy
**Opened**: 2026-05-12
**First review gate**: 2026-05-26 — dataset invalidated (186 events, all eval-polluted; FRE-387 eval isolation not yet shipped when data was collected)
**Gate reset**: 2026-05-26 — agent-noun taxonomy deployed (direction 2 chosen); new review gate **2026-06-09**
**Owner**: lextra

---

## What shipped

The full capture + clustering + Linear promotion pipeline for capability-gap
recognition.  Both signal sources land in the same Elasticsearch bucket and
flow through the same aggregation.

| PR | Change |
|----|--------|
| [#43](https://github.com/alextra-lab/personal_agent/pull/43) | Phase 1+2: emit `missing_skill_requested` from `read_skill` on unknown name; `TelemetryQueries.get_missing_skill_buckets`; `InsightsEngine.detect_missing_skill_patterns`; wired into existing CaptainLog → promotion → Linear. |
| [#44](https://github.com/alextra-lab/personal_agent/pull/44) | Skill-index nudge in system prompt; DSPy `missing_skill_names` output field; reflection-time gap capture. |
| [#45](https://github.com/alextra-lab/personal_agent/pull/45) | Emit warnings from main loop (was silently dropped when emitted from `asyncio.to_thread` because `ElasticsearchHandler` requires a running loop). |
| [#46](https://github.com/alextra-lab/personal_agent/pull/46) | ES field-name corrections: `event.keyword` → `event_type`; remove `.keyword` suffix from `requested_name` / `session_id` (both already keyword-mapped). |
| [#47](https://github.com/alextra-lab/personal_agent/pull/47) | Thread `session_id` through `generate_reflection_entry` → `emit_missing_skill_warnings` so the ≥2-distinct-sessions cardinality is meaningful. |

Current thresholds (in `InsightsEngine`):

```
MIN_MISSING_SKILL_REQUESTS = 3
MIN_MISSING_SKILL_SESSIONS = 2
```

---

## Field-test findings (2026-05-12)

Eight live `missing_skill_requested` events captured across one debugging
session.  All pipeline plumbing verified working end-to-end (ES emission,
indexing, aggregation, session_id propagation, threshold logic).

**One unresolved design problem surfaced**: the reflection model names the
same conceptual gap differently across runs, even with identical user
prompts.  Observed for a "track word count for my book" task:

| Turn | Skill name(s) emitted |
|------|------------------------|
| 1 | `word-count-log`, `word-count-history` |
| 2 | `word-count-log-update` |
| 3 | `rolling-sum-7-day`, `rolling-sum-lookup` |

Across the same session run for academic-paper research:

| Turn | Skill name(s) emitted |
|------|------------------------|
| 1 | `citation-count-rank`, `semantic-scholar-paper-fetch`, `bibtex-from-doi` |

Result: every bucket sits at `count=1, sessions=1`.  The
`≥3 across ≥2` threshold cannot be met under the model's current
naming variance.  In short: **the plumbing works; the threshold can't fire
without name normalization**.

---

## Why park instead of fix immediately

1. Real-world distribution of gap-recognition events is unknown.  Designing
   a naming-normalization layer without data risks over-engineering.
2. Two weeks of natural usage will surface (a) which kinds of gaps recur,
   (b) which name variants the model actually uses for the same concept,
   (c) whether the variance is per-prompt, per-session, or genuinely
   capability-axis variance worth preserving as separate signals.
3. The pipeline is already operating in capture-only mode — events land in
   ES, no spurious Linear tickets fire, no user-visible behavior changes.
   There is zero cost to letting it run.

---

## What to evaluate on 2026-05-26

Pull two weeks of `missing_skill_requested` events and answer:

1. **Volume** — how many events per day on average?  Is the signal even
   meaningful at this volume, or do we need more usage first?
2. **Name distribution** — how many unique `requested_name` values?
   Aggregate by `source` (`reflection` vs the read_skill executor path).
3. **Name clustering by inspection** — for the top N names, group them
   manually into conceptual buckets.  How many concepts do the names
   collapse to?  What clustering signal would have worked (substring,
   prefix, embedding similarity)?
4. **Would the threshold ever have fired** if we'd applied a chosen
   clustering rule retroactively?
5. **False-positive check** — for each conceptual cluster that *would have*
   fired, is it a genuinely useful skill to author?  Or is the model
   inventing skills that don't reflect real capability gaps?

Output: a short decision doc choosing one of the directions below (or
"close as won't-do" if data shows the signal is too noisy to act on).

---

## Decision — 2026-05-26

Dataset was fully invalidated: all 186 events came from eval sessions
(`channel=EVAL`), because FRE-387 eval isolation had not shipped yet.
Genuine signals extracted (non-Slack): `health-check`, `system-diagnostics`,
`bash-batch-run` — all plausible but too few to act on.

**Direction chosen: #2 — constrained naming via DSPy prompt.**

Switched from imperative-verb format (`{domain}-{verb}`) to agent-noun
format (`{domain}-{noun}`) matching Claude marketplace naming convention
(analysis of 425+ published skills).  Noun list:
`fetcher, runner, sender, writer, monitor, checker, scanner, analyzer,
summarizer, generator, creator, tracker, detector, validator, notifier`.

Deployed in commit `59f2a2d`, rebuilt gateway 2026-05-26 21:40 UTC.
FRE-387 (eval isolation) shipped same session — future data will be clean.

New review gate: **2026-06-09**.

---

## Candidate directions (decide on 2026-06-09)

Documented now so we don't re-derive them in two weeks.

1. **Embedding-similarity clustering at aggregation time.**  Fetch all
   `requested_name`s in the analysis window, embed via the existing
   embeddings service, cluster by cosine ≥ 0.85, aggregate per cluster.
   Treats the event stream as immutable truth.  Architecturally cleanest;
   most general.
2. **Constrained naming via DSPy prompt.**  Give the reflection model a
   fixed small ontology of skill-name slugs ("pick from this list or
   propose a new one with `new:` prefix").  Cheaper than embeddings;
   caps expressive power.
3. **Lower temperature for reflection model.**  Current `Qwen3.6-A3B` at
   T=0.6 probably accounts for some fraction of the variation.  Cheapest
   change; may not fully solve it.
4. **Aggregate-then-LLM-cluster.**  Periodic background job: feed all
   recent names to an LLM, ask "which are the same skill?"  Output a
   canonical bucketing.  Slow but very general.
5. **Drop the threshold; surface raw events for human triage.**  Build a
   Kibana dashboard showing recent `missing_skill_requested` events with
   `requested_name` faceted; let the operator decide what to author.
   Lowest tech investment; pushes the work to the human.

---

## Three reusable lessons (worth keeping outside FRE-328)

1. **Logs from `asyncio.to_thread` workers don't reach the
   `ElasticsearchHandler`** — it requires a running event loop to schedule
   the async write.  Anything that needs ES emission must run on the main
   loop, or surface the payload back to the main loop before emitting.
2. **ES field-name convention in this project**: structlog `event` is
   stored as `event_type` in agent-logs-*.  Fields already mapped as
   `keyword` (like `requested_name`, `session_id`) do not have a `.keyword`
   sub-field.  Same gotcha likely affects `get_delegation_pattern_buckets`
   and `get_error_events` — flagged for follow-up.
3. **LLM-generated identifiers (names, IDs, keys) are not stable across
   sessions** even with deterministic-looking prompts.  Any pipeline that
   keys on a model-generated string for cardinality or dedup must layer
   normalization on top.

---

## Cross-references

- Linear: [FRE-328](https://linear.app/frenchforest/issue/FRE-328) — to be
  commented with this finding and marked as monitoring.
- Memory: `project_fre_328_naming_stability.md` in agent auto-memory.
- ADR-0066 references this loop as the "skill library growth" feedback
  mechanism.
