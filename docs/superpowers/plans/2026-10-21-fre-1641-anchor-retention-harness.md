# FRE-1641 — ADR-0158 T1: web fixtures and the anchor-retention harness

**Ticket:** FRE-1641 (Approved, owner 2026-10-21). **ADR:** ADR-0158 D1.8, D2.1, D4, D5, D6.1, D6.3.
**Tier:** Standard/Complex (touches `src/` shaping code) → codex plan review.

## Goal

Measure which Exa highlight length × result count (8 search cells) and which ranker arm × passage budget
(12 fetch cells) keep ≥ 95% of the anchors of stored helper findings. Ship no tool behavior change.
Ship the passage split and the ranker arms as a module that T3 imports.

## Sizing (measured 2026-10-21, production ES, read only)

- 86 web-using helpers with findings, 1,119 findings, 784 distinct `source_url`, 48 traces.
- 1,008 `web_search_started` events in those traces, 27 at the 120-character cut, 955 distinct replays.
- Thoroughness: standard 51, thorough 13, quick 4, unknown 18.

## Design decisions (stated, so the reviewer can reject them)

1. **Search attribution is per helper, by time window** (revised after the Codex review). The search
   events carry no task id. A helper capture holds its end `timestamp` and `duration_ms`. Measured
   2026-10-21: in all 124 web-using helpers with `rounds`, the window held exactly the helper's own
   `web_search` count, and no call fell in two windows. A helper whose count differs is excluded.
2. **Five SearXNG instances, not four.** The full form (D5.3) is Exa 2,000 × 10 results. The fifth
   instance runs the production `settings.yml` unchanged, so the full form and the four cells are
   recorded at the same time with the same code. Production `searxng` is never queried.
3. **Raw responses are stored; shaping is offline.** Each instance's raw SearXNG JSON is stored once.
   The result-count axis {5, 10} and the shaping run offline through the production function (D1.8).
4. **The 100-character seeded negative** is the full-form snippet cut to 100 characters (10 results).
   **The 200-character seeded negative** is `fixture[:200]`.
5. **Support window offsets (D5.3, D5.6)** (revised after the Codex review). From each anchor
   occurrence, the support set is the distinct anchors wholly inside the next 600 characters (≥ 2, or
   the only anchor). The window runs from that occurrence to the one that completes the set, and its
   offsets are recorded. A fetch form keeps the finding if its returned text covers one window's
   offsets. Whitespace between two returned passages counts as covered.
6. **Focus = full `spec_task`** (D5.9, literal). It is often longer than the 200-character `focus`
   limit of D3.1 (p50 451). The report gives the length distribution. This is the ADR's stated proxy.
7. **The `focus_matched: false` fallback (D3.3) is modelled** (revised after the Codex review). When no
   chosen passage holds a focus term, the form is the cut from the top at the default `max_chars`.
   `focus_matched` lives in the passage module, so T3 uses the same definition.
8. **Fetch result size** is reported as the passage characters (the budgeted part). D3.2 adds at most
   1,500 characters of fields. Search result size is `len(render_content(shaped output))`, the same
   renderer the dispatch uses (`tools/contract.py`).
9. **Rerank client in the harness, not `memory.reranker.rerank`.** The production client records vendor
   cost to Postgres (`record_vendor_cost`) and falls back to the Mac reranker. The harness must write
   nothing and must measure `rerank-2.5` only. The passage module takes the rerank call as an injected
   function, so T3 wires its own.
10. **Fixtures live outside git**, default `~/.cache/seshat-eval/fre1641/<run_id>/` (`--data-dir`).
    The instance `settings.yml` copies hold vendor keys. They are written 0644 (the container user is
    uid 977) and deleted by `searxng-down`.

## Codex plan review (2026-10-21) — disposition

- Blocking 1 (per-trace attribution): fixed, decision 1.
- Blocking 2 (window coverage): fixed, decision 5.
- Blocking 3 (fallback cut): fixed, decision 7.
- Blocking 4 (T2 changes the shaping): accepted as a T2 duty. The raw responses are stored, so T2 runs
  `score` again at its own commit. The report and the handoff say so.
- Should-fix: offsets are exact through `TextIndex` (both sides normalized, spans kept). Windows are
  deterministic (decision 5). `unknown` thoroughness is pooled only. The 2,000 instance is checked to be
  the production full form. Copies sit in a 0700 directory and are deleted at `searxng-down` or on a
  failed `up`. Fetch parity: the existing `test_fetch.py` suite passes unchanged.

## Steps

### 1. Refactor `tools/web.py` — no behavior change
- Extract `build_search_params(query, categories, engines, language, time_range) -> dict[str, str]`.
- Extract `shape_search_response(data, *, query, categories, engines, max_results) -> dict[str, Any]`:
  the results/answers/infobox/health shaping now inline at `web.py:325–406`.
- `web_search_executor` calls both. Logging stays in the executor.
- Test: `tests/test_tools/test_web_search.py` passes unchanged; new test feeds one synthetic SearXNG
  response through `shape_search_response` and asserts the exact dict.

### 2. Refactor `tools/fetch.py` — no behavior change
- Extract `async def download_text(url: str, *, trace_id: str) -> tuple[str, int]`: the guarded client,
  the private-target hook, the HTTP-error and transport-error mapping, and the extraction. Returns the
  whole extracted text and the HTTP status.
- `fetch_url_executor` keeps validation, the novelty check, the empty-text error and the `max_chars` cut.
- Test: `tests/personal_agent/tools/test_fetch.py` passes unchanged; new test asserts `download_text`
  returns text longer than 50,000 characters uncut.

### 3. New `src/personal_agent/tools/passages.py` (TDD)
- `Passage(index, start, end, text)` frozen dataclass, `text == source[start:end]`.
- `split_passages(text) -> list[Passage]`: blocks from `\n`; a block < 200 characters merges with the
  next; a block > 800 characters splits at a sentence end (`. ! ?` + space), else at whitespace, else
  hard at 800.
- `tokenize(text)`: lower-case word tokens minus `STOP_WORDS`.
- `bm25_order(focus, passages) -> list[int]` (Okapi BM25, k1 1.5, b 0.75, ties by page order).
- `RerankFn = Callable[[str, Sequence[str]], Awaitable[RerankCall]]`, `RerankCall(scores, tokens)`.
- `RankerArm = Literal["R1", "R2", "R3", "R4"]`; `async rank_passages(arm, focus, passages, rerank)
  -> RankOutcome(order, rerank_calls, rerank_tokens, sent_to_vendor)`:
  R1 BM25 all · R2 rerank first 100 · R3 BM25 top 20 then rerank · R4 R1 if ≤ 50 passages else R3.
- `select_within_budget(order, passages, budget) -> list[Passage]`: rank order, skip a passage that
  does not fit, return in page order.
- Tests: `tests/personal_agent/tools/test_passages.py` — offsets are verbatim, merge/split bounds, BM25
  puts the matching passage first, each arm's candidate set (fake rerank), budget never exceeded.

### 4. Harness `scripts/eval/fre1641_anchor_retention/`
- `anchors.py` — D5.1: dates (ISO, `Month D, YYYY`, `D Month YYYY`, `Month YYYY` → `YYYY-MM[-DD]`),
  capitalized names (not stop words), numbers (thousands separators and `%` dropped, decimal comma →
  dot, leading zeros dropped). `occurrences(anchor, text) -> list[(start, end)]`.
- `facts.py` — ES read only (`_search` only; the client refuses any other path). Writes `facts.json`:
  helpers (trace, task, thoroughness, `spec_task`), findings with anchors, searches per trace with the
  120-character exclusion.
- `record.py` — `record-fetch` (production `download_text`, cut at 200,000, concurrency 8) and
  `record-search` (5 instances, production `build_search_params`, concurrency 2 per instance).
- `searxng.py` + `docker-compose.fre1641.yml` — project `fre1641`, 5 services, image read from the
  running `cloud-sim-searxng`, ports `127.0.0.1:1850{0..4}`, no network shared with production.
  `searxng-up` runs a `--dry-run` first and refuses any container name outside `fre1641-*`.
- `rank.py` — Voyage `rerank-2.5`, key from `pass show seshat/AGENT_VOYAGE_API_KEY` (never printed),
  retries on 429, stores order, wall time and `usage.total_tokens` per fixture per arm.
- `score.py` — support, drift, minimum samples, retention, class split, seeded negatives, full-form
  check, selection (D6.1 search, D4 fetch), `report.md` + `report.json` with `git rev-parse HEAD`,
  a dirty flag for the shaping files, and the before/after `StartedAt` of `cloud-sim-searxng`.
- `run.py` — subcommands: `facts`, `preflight`, `searxng-up`, `record-fetch`, `record-search`, `rank`,
  `score`, `searxng-down`.
- Tests: `tests/test_eval/test_fre1641_anchors.py`, `test_fre1641_score.py` (support window, kept,
  seeded negative fails, full form keeps 100%, selection tie-breaks, minimum-sample refusal),
  `test_fre1641_no_copy.py` (the harness imports the production functions and defines none of them).

### 5. Gates, commit, self-review, PR (code only)
`make test` · `make mypy` · `make ruff-check` · `make ruff-format` · `pre-commit run --all-files`.

### 6. Run (after the PR is open, owner-gated by the preflight)
1. `facts`, then `preflight` (1-item trace: one fetch, one search on one instance, one rerank; Brave
   rate-limit headers; projected cost and time). Post the AC-1 comment. **Wait for the owner's go.**
2. `searxng-up` → `record-fetch` → `record-search` → `rank` → `searxng-down` → `score`.
3. Post the report table on the ticket. If a grid fails, put it to the owner (AC-8).

## Projection (for the preflight comment, recomputed there)

- Searches: 955 × 5 instances = 4,775. Exa ≈ $0.007 each → ≈ $33. Brave ≈ $0.005 each → ≈ $24.
- Rerank: ≈ 784 pages × 2 calls, ≈ 30K tokens per page at $0.05 / M → ≈ $1.5.
- Wall time: searches ≈ 35 min (instances in parallel), fetch ≈ 5 min, rerank ≈ 10 min.

## Acceptance criteria → proof

| AC | Proof |
| -- | -- |
| AC-1 | Ticket comment with counts, cost, time and the 1-item trace, timestamped before `record-search` |
| AC-2 | `report.md` table: 8 search + 12 fetch cells, every field; selection rule shown |
| AC-3 | Seeded negatives in the same report, both < 95%; unit test proves the scorer fails them |
| AC-4 | Full-form rows = 100% in the report; unit test |
| AC-5 | Report sample block; `score` refuses selection below the minimums; unit test |
| AC-6 | `StartedAt` before/after in the report; harness ES client is `_search`-only (unit test) |
| AC-7 | `git rev-parse HEAD` in the report; `test_fre1641_no_copy.py` |
| AC-8 | Selection returns "none passes" and the report says so; unit test |
