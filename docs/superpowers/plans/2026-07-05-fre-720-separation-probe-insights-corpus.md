# FRE-720 — Separation-probe measurement gate on the insights corpus (ADR-0105 D10 / AC-8)

**Ticket:** FRE-720 (Approved, stream:build2, Tier-2:Sonnet)
**Backing ADR:** ADR-0105, decision D10, acceptance criterion AC-8
**Blocks:** FRE-721 (T7 — generation-time semantic dedup) must not commit before this reports.

## Scope

Reuse the FRE-670/ADR-0103 separation-probe *instrument* (the pure cosine-separation
statistics: `summarize_separation`, `propose_floor`/`sweep_floor`, `best_separation_at_observed`
in `scripts/eval/fre435_memory_recall/{separation_report,calibration}.py`) on the **real**
proposal corpus (`agent-captains-reflections-*`, excluding `eval_mode: true` docs) to decide:
does the deployed embedder open a clean cosine floor between "same idea, reworded" (positive)
and "same category, genuinely distinct idea" (hard negative) proposals?

This is a **measurement gate**, not a mechanism build — it produces a versioned artifact and a
branch decision (semantic-dedup-viable vs category+facet-fallback) per ADR-0105 D10. No `src/`
production code changes.

## Why this design

- The existing `scripts/eval/fre435_memory_recall/separation_benchmark.py` is entity/query-recall
  shaped (a corpus of entities + queries expecting specific entity matches) — not directly
  reusable for a proposal-vs-proposal pairwise-similarity question. What **is** reusable and
  substrate-free is the pure geometry/statistics layer (`separation_report.py`,
  `calibration.py`) — importing those directly satisfies "reuse the instrument" without forcing
  an entity/query shape onto a fundamentally different (pairwise) probe.
- The real corpus (queried live against the local ES on this VPS — `agent-captains-reflections-*`,
  1,857 non-eval docs with `proposed_change`) shows the exact pattern ADR-0105 describes: e.g. ~50+
  reworded duplicates of "add a fast-path to skip orchestration steps for simple queries"
  (`category=performance`), and a "capture_write_failed silently drops telemetry" idea that
  recurs under **both** `category=observability` and `category=reliability` — i.e. category alone
  cannot group it, which is exactly the case semantic dedup must catch.
- Labeled pairs are hand-verified against real proposal text (same method FRE-670's
  `semantic_probe.yaml` used): positive pairs are two real proposals about the same underlying
  idea in different words; hard-negative pairs share a `category` **and** a topical family
  (e.g. "verify X is reachable before proceeding") but are demonstrably distinct proposals — e.g.
  `reliability`-tagged Elasticsearch-retry vs. `reliability`-tagged Linear-connectivity-check vs.
  `reliability`-tagged docker-compose-visibility-check (real docs, same verification-family, same
  category label, genuinely different concrete asks).

## Revisions after codex plan-review (2026-07-05)

Codex's second opinion (full text in the FRE-720 build session) confirmed the pure-statistics
reuse and the overall structure are sound, and flagged three things to tighten before coding:

1. **Some negative pairs were too easy** (e.g. a travel-itinerary proposal vs. an Elasticsearch-
   retry proposal, both merely labeled `reliability`) — a negative that's obviously unrelated
   inflates the apparent separation rather than testing the hard case D10 cares about ("is there
   an equivalent existing proposal?" on genuinely adjacent ideas). **Fix:** dropped the two
   travel-itinerary docs entirely; replaced the weakest negatives with same-family,
   different-mechanism pairs — e.g. the fast-path/reduce-LLM-calls cluster vs. a real proposal to
   gate the *proactive-memory-suggestion* pipeline behind a token-length threshold (same "skip
   work for trivial input" family, different subsystem/mechanism) — a genuinely hard near-miss.
2. **AC-8's second clause** ("shipped dedup branch is mechanically checked against the artifact")
   was under-specified — which exact field, checked how. **Fix:** added the explicit contract
   below.
3. **Provenance/reproducibility gap** — `probe_code_version` via `git rev-parse --short HEAD`
   alone doesn't capture whether the corpus/pairs files matched that commit. **Fix:** the artifact
   also records `corpus_sha256`/`pairs_sha256` (hash of the two committed YAML files as loaded)
   and a `git_dirty` boolean, so a later re-run can prove which corpus/pairs version produced which
   verdict independent of commit history.

## Downstream contract (AC-8 clause 2 — mechanical branch check)

`probe_result.json["decision"]` is the single field FRE-721 (T7) must consume: it is exactly
`"semantic"` or `"fallback"`, produced by the pure `decide_branch()` function. FRE-721's own test
suite must assert that its shipped dedup code path matches this value — e.g.
`assert dedup_uses_vector_clustering() == (json.load(open("scripts/eval/fre720_insights_separation/probe_result.json"))["decision"] == "semantic")`
— so "the artifact said X but the build shipped Y" is a CI-catchable failure, not a review-time
judgment call. This repo does not implement FRE-721; this plan only commits to the field name/
value contract FRE-721 must read.

## Files

- `scripts/eval/fre720_insights_separation/corpus.yaml` (new) — the 35 real proposal texts
  (`entry_id -> {text, category, scope}`) referenced by the pair set below. `text` is
  `what + "\n\n" + why`, copied verbatim from the live `agent-captains-reflections-*` documents
  pulled 2026-07-05 (committed so the probe replays without live ES access).
- `scripts/eval/fre720_insights_separation/pairs.yaml` (new) — 25 positive + 24 negative labeled
  pairs referencing `corpus.yaml` entry_ids, each with a one-line human justification note.
  Negatives are same-category **and same topical family** (e.g. "verify X is reachable before
  proceeding", "add telemetry for X", "skip work for trivial input") but a demonstrably distinct
  concrete proposal — hard near-misses, not random cross-topic pairs.
- `scripts/eval/fre720_insights_separation/probe_pairs.py` (new) — pure loader: `PairCase`,
  `Corpus`, `load_corpus(path)`, `load_pair_set(path)`, `PairSetError(ValueError)` — mirrors the
  `probes.py` idiom (degenerate-set guard: reject if no positive pair or no negative pair present).
  No `personal_agent` import (substrate-free, fully unit-testable, mirrors `separation_report.py`'s
  "no substrate import" discipline).
- `scripts/eval/fre720_insights_separation/decision.py` (new) — pure branch-decision function:
  `decide_branch(stats: SeparationStats) -> Literal["semantic", "fallback"]` — `"semantic"` iff
  `stats.clean_floor` (the ADR-0103 definition: `max(negatives) < min(positives)`), else
  `"fallback"`. Kept as its own tiny pure function so the D10 gate logic is independently unit
  tested against synthetic `SeparationStats`, without needing a live embedder run.
- `scripts/eval/fre720_insights_separation/separation_probe.py` (new) — the runner:
  1. Pin defensive test-substrate env vars before any `personal_agent` import (mirrors
     `separation_benchmark.py`'s pattern — this script touches no substrate, but stays past the
     ADR-0099 validator by convention).
  2. Load `corpus.yaml` + `pairs.yaml`.
  3. Embed each unique corpus text **once** via
     `personal_agent.memory.embeddings.generate_embeddings_batch(texts, mode="document")` — the
     real deployed 0.6B embedder (`embeddings:8503`/`localhost:8503`), matching ADR-0105 D10's
     "reuse the existing prod embedder" instruction. Both sides of a pair are proposals (no
     query/document asymmetry), so both embed in `"document"` mode.
  4. Fail loud (`SystemExit`) on any zero-vector or wrong-length embedding (mirrors
     `separation_benchmark.py::_assert_vectors`) — never silently score a degenerate vector.
  5. Compute cosine per pair (`personal_agent.memory.embeddings.cosine_similarity`); split into
     `positives`/`negatives` cosine lists by the pair's label.
  6. Call `summarize_separation(positives, negatives)` (reused) and `propose_floor(...)` (reused)
     for the Youden's-J floor; call `decide_branch(stats)` for the D10 verdict.
  7. Write the **versioned probe artifact** (all AC-8-required fields — see below) to both:
     - `telemetry/evaluation/fre720-insights-separation/separation-report-<run_id>.json`
       (gitignored, full detail — mirrors the FRE-435 harness's output convention), and
     - `scripts/eval/fre720_insights_separation/probe_result.json` (**committed** — the durable,
       versioned artifact AC-8 requires; a small curated summary, not the full per-pair dump).
  8. Print the run record + verdict to stdout.

- `tests/test_eval/test_fre720_separation_probe.py` (new) — pure unit tests, **no substrate, no
  embedder call** (mirrors `tests/test_eval/test_recall_calibration.py`'s pattern):
  1. `load_pair_set` rejects a degenerate set (all-positive, all-negative, or empty) —
     `PairSetError`.
  2. `load_pair_set` rejects a pair referencing an `entry_id` absent from the loaded corpus.
  3. `decide_branch` returns `"semantic"` for a synthetic `SeparationStats` with `clean_floor=True`
     and `"fallback"` for `clean_floor=False` (both branches of the D10 gate, table-driven).
  4. The artifact-writing function (given synthetic pre-computed positives/negatives, not a live
     embedder call) produces a JSON object containing every AC-8-required field: corpus source +
     query description, time window, item counts, labeled pair counts (positive/negative split),
     cosine distributions (`SeparationStats.__dict__`), chosen floor, pass/fail decision, probe
     code version string, and a run id.

## Versioned probe artifact — required fields (AC-8)

Per ADR-0105 AC-8, `probe_result.json` (the committed artifact) records:
- `corpus_source`: `"agent-captains-reflections-* (ES), proposed_change field, eval_mode != true"`
- `query_description`: how the 35-doc corpus subset was selected (real fast-path/observability/
  reliability/ux clusters + hard-negative singletons; see `pairs.yaml` notes for provenance)
- `time_window`: the real corpus's timestamp span pulled (min/max `timestamp` across the 35 docs)
- `item_counts`: `{corpus_docs: 35, total_corpus_at_pull_time: 1857}`
- `pair_counts`: `{positive: 25, negative: 24}`
- `cosine_distributions`: the full `SeparationStats` (pos/neg min/median/max/p5/p95, overlap counts)
- `chosen_floor`: the `propose_floor` result (floor, recall, fpr, Youden's J)
- `decision`: `"semantic"` or `"fallback"` (from `decide_branch`) — the field FRE-721 mechanically
  checks against (see Downstream contract above)
- `probe_code_version`: git short SHA at run time (`git rev-parse --short HEAD`)
- `git_dirty`: boolean — whether the working tree had uncommitted changes at run time
- `corpus_sha256` / `pairs_sha256`: SHA-256 of the loaded `corpus.yaml` / `pairs.yaml` file bytes,
  so the artifact is reproducible-provable independent of commit history
- `run_id`: a stable id for this run (timestamp + short SHA)

## Test plan

1. `uv run pytest tests/test_eval/test_fre720_separation_probe.py -v` — pure unit tests, must pass
   before the live run (TDD: write the failing tests first for `load_pair_set`/`decide_branch`/
   the artifact writer, then implement).
2. `uv run python scripts/eval/fre720_insights_separation/separation_probe.py` — the actual
   measurement run against the real embedder (`localhost:8503`, confirmed reachable) using the
   committed `corpus.yaml`/`pairs.yaml`. This is the real AC-8 deliverable: whatever cosines come
   back drive the committed `probe_result.json` and this ticket's final decision — the plan does
   not pre-suppose separates-vs-not.
3. `make mypy` / `make ruff-check` / `make ruff-format` / `pre-commit run --all-files` on the new
   files only (no `src/` touched).

## Acceptance criteria mapping (AC-8)

- "Versioned probe artifact... a value, not a claim" → `probe_result.json`, committed, with every
  required field populated from the real run (not placeholder numbers).
- "Recommended dedup branch is stated and justified by the artifact, so T7 can be mechanically
  checked against it" → `decision` field + `decide_branch` is a pure, mechanically-checkable
  function keyed only on `stats.clean_floor` — FRE-721 can import `decision.py` and assert its own
  branch matches.

## Out of scope (explicitly, per ADR-0105 D10 + AC-10 scoping)

- No reranker involvement — cosine-only, matching D10's "no reranker" scoping for the System KG.
- No `src/personal_agent/sysgraph` or `dedup.py` changes — this ticket is the measurement gate
  only; FRE-721 (blocked on this) implements the actual dedup branch.
- No writes to `sysgraph` or any production substrate — read-only ES query (already run, corpus
  committed) + a local embedder call.
