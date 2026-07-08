# FRE-778 — ADR-0104 multipath A/B driver: the FRE-724 proof instrument

**Ticket:** FRE-778 (Approved, In Progress) · **Backing:** ADR-0104, FRE-724 (Awaiting Deploy, blocked on this),
FRE-705, FRE-706, FRE-489, FRE-670, FRE-658 · **Project:** Memory Recall Quality

## Why (recap)

FRE-724 (multi-path recall pipeline) is merged and — per the live MASTER_PLAN header — was actually
**flipped ON in production** on 2026-07-07 (2-arm: multi_query + lexical, floor 0.60), but its
`multipath_recall` telemetry emits no latency field, so p50 is unmeasurable from prod traces and the
flag can't graduate. This ticket is unrelated to that prod telemetry gap: it builds an **isolated,
test-substrate A/B instrument** that measures the multipath pipeline off-vs-on directly (wall-clock
latency, not telemetry), on a non-trivial haystack, so FRE-724 has a real measured number to close on.

Master already confirmed (2026-07-04) that no valid instrument exists today:
- FRE-435's per-case-isolation harness is trivial (R@5 = 1.00 for every case, off byte-identical to on).
- Its only haystack mechanism (`fetch_live_distractors`) reads production Neo4j (port 7687) using the
  **same** `AGENT_NEO4J_PASSWORD`/`settings.neo4j_password` the test-substrate connection uses — an
  unsafe credential-reuse pattern if the two passwords ever diverge (`docker-compose.test.yml:59` and
  `docker-compose.yml:57` currently both default from the identical `${NEO4J_PASSWORD}` var, which is
  why it "works" today and why it's a latent trap).
- FRE-655's existing A/B script only toggles the ADR-0100 `relevance_bounded_recall_enabled` flag,
  never the ADR-0104 multipath flags.

## What already exists (confirmed by direct file reads, not assumed)

All under `scripts/eval/fre435_memory_recall/`:

| File | Role |
|---|---|
| `harness.py` | `wipe_substrate`, `seed_replay` (offline entity+turn seeding, no LLM), `fetch_live_distractors`/`load_distractors` (the unsafe prod-read path), `store_turn`, `detect_embedding_backend` |
| `ab_relevance_bounded.py` | FRE-655's A/B driver. Has a `--mode calibrate` path (`calibrate()`, lines 287–421) that **already co-seeds a full probe set with no wipe between cases** and captures positive/negative cosine distributions over the co-resident corpus — this is the exact "co-resident haystack" mechanism FRE-778 needs, just wired to cosine-capture instead of full recall@k A/B |
| `probes.py` | `ProbeCase`/`load_probe_set` — `bespoke_probe.yaml` (FRE-489, 21 cases), `semantic_probe.yaml` (FRE-670, 54 cases) |
| `metrics.py` | Pure `recall_at_k` |
| `calibration.py` | Pure `sweep_floor`/`propose_floor` (Youden's J) |

**Config drift since FRE-655 was written (2026-06-28), confirmed live in this repo today:**

1. **ADR-0112 substrate-profile seam** (`config/substrate.py` + `config/substrate.yaml`, landed
   ~2026-07-03) now gates the embedder backend: `memory/embeddings.py:88-92`
   (`_resolve_embedder_kind`) calls `resolve_substrate(settings.substrate_profile).backends["embedder"].kind`.
   `tests/conftest.py:24` already sets `AGENT_SUBSTRATE_PROFILE=test` for pytest — but **neither
   `harness.py` nor `ab_relevance_bounded.py` set this env var**, so run standalone today they inherit
   whatever `.env` declares. `.env:679` currently has `AGENT_SUBSTRATE_PROFILE=managed_embedder` (live
   since FRE-821, 2026-07-08) — meaning re-running either existing script *right now*, unmodified,
   would silently call the paid OVH-managed embedder instead of the local one.
2. Both existing scripts hard-set `AGENT_MODEL_CONFIG_PATH=config/models.cloud.yaml`
   (`ab_relevance_bounded.py:31`, `harness.py:48`), whose `embedding.endpoint` is
   `http://embeddings:8503/v1` (`config/models.cloud.yaml:226`) — a Docker-internal DNS name that does
   not resolve from a host CLI process. `config/models.yaml:216` has the same role at
   `http://localhost:8503/v1`, which does resolve from the host.
3. The local embedder container is currently **stopped**: `docker ps -a` shows
   `cloud-sim-embeddings  Exited (0) 23 hours ago` (retired when the managed-embedder profile went live
   today). It must be started before this driver runs and stopped again after (mirrors the
   already-documented `make rebuild`-revives-embeddings discipline).

**Net fix for this driver:** pin `AGENT_SUBSTRATE_PROFILE=test` + `AGENT_MODEL_CONFIG_PATH=config/models.yaml`
(not `models.cloud.yaml`) in the env-pin block, and fail fast (reusing `harness.detect_embedding_backend()`)
if the embedder isn't reachable, with an actionable error telling the operator to
`docker start cloud-sim-embeddings` first.

## Codex plan review (2026-07-08, second opinion before coding)

Confirmed 6 problems in the first draft of this plan; all folded into the steps below:

1. **Env pin must hard-assign, not `setdefault`.** `os.environ.setdefault(...)` is a no-op if the key is
   already present in the process environment — and if this shell/session has `.env`'s
   `AGENT_SUBSTRATE_PROFILE=managed_embedder` already exported, `setdefault` would silently lose to it.
   Fix: use direct assignment (`os.environ[key] = value`) for every key in the pin block, not `setdefault`
   — a hard pin, not a default.
2. **The FRE-658 window check must call `MemoryService.query_memory(MemoryQuery(hard_recency_days=...), query_text=...)` directly** — `MemoryRecallQuery` (the adapter's dataclass, `protocol.py:88-118`) has no
   `hard_recency_days` field, and `MemoryServiceAdapter.recall()` builds a `MemoryQuery` without it
   (`protocol_adapter.py:53-60`). Only `MemoryQuery.hard_recency_days` (`models.py:228-234`) reaches
   `_filter_turns_by_hard_recency` (`service.py:3624-3629`). The window check must bypass the adapter and
   call `service.query_memory(...)` on the raw `MemoryQuery`, and must pass a non-empty `query_text` (the
   multipath dispatch at `service.py:2494-2507` only fires when `query_text` is truthy).
3. **The floor-invariant check is dense-vector-arm-only, not a whole-pipeline proof.** `_capture_cosines`
   (adapted from `ab_relevance_bounded.py:121-138`) calls `_query_entity_vector_candidates`
   (`service.py:2885-2916`), which only returns the dense arm's `{name, score}` rows — the lexical arm
   returns full-text rank, not cosine (`service.py:3004-3088`), and the fused/reranked set has no single
   comparable score threshold (`service.py:3201-3224`, RRF is rank-based by design). The report must label
   this "dense-arm floor invariant" explicitly, not imply it validates the fused/lexical output too.
4. **Decision 1 (co-resident-only) needs explicit owner framing, not just a scope call.** FRE-724's own
   ticket text already says live verification + the deploy flag-flip is master-owned/deploy-gated — so a
   test-substrate instrument is the right shape for FRE-778 by the ticket's own division of labor. But an
   earlier FRE-724 planning doc (`docs/superpowers/plans/2026-07-02-fre-724-multipath-recall-seam.md:211-215`)
   describes the p50/floor proof as needing "live corpus + prod embedder." I'll state explicitly in the
   report and PR that this driver is **a test-substrate proof instrument for FRE-724 to consume**, not
   itself the live/prod flag-graduation proof — surfaced to the owner as an open item below, not decided
   silently.
5. (same fix as #2 — folded into the window-check step.)
6. **The A/B baseline must explicitly pin `relevance_bounded_recall_enabled = False`** for the whole run
   (both OFF and ON states) so the ADR-0100 flag doesn't confound the ADR-0104 multipath measurement —
   the broad path reads `relevance_bounded_recall_enabled and bool(query_text)` independently
   (`service.py:3916-3918`) and would apply its own floored candidate expansion if left at an inherited
   value. Report the pinned value in the output metadata so it's auditable.

## Design decisions (flagging trade-offs per the ticket's own "optionally" wording — not deciding silently)

**Decision 1 — distractor/haystack strategy: co-resident cases only, no live-prod read.**
The ticket explicitly lists this as a safe, sufficient option ("Optionally adds a distractor background
sourced SAFELY: *either co-resident cases only*, or a production read..."). Recommendation: **co-resident
only**, i.e. seed the full 21-case (FRE-489) or 54-case (FRE-670) gate set into the test graph with no
wipe between cases — mirrors `ab_relevance_bounded.py`'s existing `calibrate()` mode exactly, which
already proves this produces non-trivial cosine separation (positives/negatives distributions in the
committed FRE-655 report). This satisfies the acceptance bar ("a NON-trivial haystack where the baseline
is below 1.00 on at least some cases") without touching production Neo4j at all — the "production read
must use an explicit separate credential, fail-closed" acceptance clause becomes vacuously satisfied
(no production read happens). Building the safe live-distractor path (a second Neo4j credential env var,
fail-closed wiring) is real additional scope not required to pass acceptance; I'd rather file it as a
follow-up than build it speculatively (CLAUDE.md simplicity-first).

**Decision 2 — fix `harness.fetch_live_distractors`'s credential-reuse bug in this PR (owner decision,
overriding my initial "file as follow-up" recommendation).** Even though this new driver never calls that
function (Decision 1), the owner asked to fix it now since it's a real, already-identified safety issue
in shared code. Fix: `harness.py::fetch_live_distractors` currently does
`os.environ.get("AGENT_NEO4J_PASSWORD") or settings.neo4j_password` — falling back to the **same**
password the test-substrate connection uses if no shell override is present. Replace with a dedicated,
required `FRE435_LIVE_NEO4J_PASSWORD` env var (mirrors the existing `FRE435_LIVE_NEO4J_URI` override
naming), with **no fallback to `settings.neo4j_password`** — raise `RuntimeError` before opening any
driver connection if it's unset. This is a breaking change to `ab_relevance_bounded.py`'s current default
behavior (`--distractor-background 40` is its default, so today it silently attempts a live read on
every default run) — call this out explicitly in the PR description: anyone re-running FRE-655's script
with distractors now needs `FRE435_LIVE_NEO4J_PASSWORD` set, or must pass `--distractor-background 0`.

**Decision 3 — drive both the entity-path and broad-path recall, matching FRE-724's converged paths.**
`memory/service.py:2494-2499` routes `query_memory` through `_multipath_query_memory` when
`multipath_recall_enabled` and `query_text` are present; `service.py:836` does the analogous thing for
the broad path. I'll drive both via `MemoryServiceAdapter.recall(...)` / `.recall_broad(...)`, mirroring
`ab_relevance_bounded.py`'s `_entity_recall`/`_broad_hit` helpers (which I'll reuse almost verbatim).

**Decision 4 — AC-3 tail-win is a derived metric, not a new fixture.** FRE-670's semantic_probe.yaml is
already "vocabulary-divergent... designed to make keyword search fall over" (confirmed via its
`register:imagery` tags) — exactly the OOV case AC-3 wants. I'll reuse `ab_relevance_bounded.py`'s
`recovered` concept (`entity_recall_off == 0.0 and entity_recall_on > 0.0`) as the tail-win signal per
case; no new probe case needed for AC-3.

**Decision 5 — FRE-658 window check needs one small dedicated probe, not the gate-set YAMLs.** Neither
`bespoke_probe.yaml` nor `semantic_probe.yaml` carries timestamp-controlled multi-turn history suited to
proving "older-than-window → empty; window-omitted → the older turn". I'll write this as a small,
self-contained helper (`_run_window_check`) that seeds one uniquely-tagged Turn 40 days in the past,
then queries via `MemoryQuery(hard_recency_days=7)` (should return empty — the FRE-658 hard-bound path,
`_filter_turns_by_hard_recency` in `service.py:3629`) vs `MemoryQuery(hard_recency_days=None)` (should
return the older turn — de-gated per ADR-0100 AC-1a), with multipath ON throughout (the arm this ticket
is provisioning). This exercises the *live* path end-to-end; the tool-level unit tests already exist at
`tests/test_tools/test_memory_search.py:220-277`.

## Flag semantics (confirmed by reading `memory/service.py` directly)

- `multipath_recall_enabled` (`settings.py:651`, env `AGENT_MULTIPATH_RECALL_ENABLED`, default `False`)
  gates `_multipath_query_memory` / `_multipath_broad_entities`.
- `lexical_arm_enabled` (`settings.py:672`) and `multiquery_arm_enabled` (`settings.py:681`) gate the
  two additional arms inside `_multipath_fused_recall` (`service.py:3250`, `3256`).
- `recall_similarity_floor` (`settings.py:575`, default `0.0`) is read directly by `dense_recall_arm`
  (`service.py:3129`) as a noise guard **independent of** `relevance_bounded_recall_enabled` — so I set
  it to the FRE-706 owner-confirmed `0.60` for **both** OFF and ON states (the ticket's "with the
  similarity floor at 0.60" reads as a constant condition, not something toggled), and only toggle the
  three multipath/lexical/multiquery flags together for OFF vs ON.
- `reciprocal_rank_fusion` lives in `memory/fusion.py:119` (pure, already unit-tested — not touched here).

## Implementation steps

1. **New driver:** `scripts/eval/fre435_memory_recall/ab_multipath.py` (same package as the FRE-655/435
   family so it can import their helpers directly, matching existing convention).
   - Module-top env pin (fixed per the config-drift findings above) — **hard assignment, not
     `setdefault`** (codex finding #1: `setdefault` loses to an already-exported `.env` value):
     ```python
     _TEST_SUBSTRATE_ENV = {
         "APP_ENV": "test",
         "AGENT_SUBSTRATE_PROFILE": "test",
         "AGENT_MODEL_CONFIG_PATH": "config/models.yaml",
         "AGENT_NEO4J_URI": "bolt://localhost:7688",
         "AGENT_ELASTICSEARCH_URL": "http://localhost:9201",
         "AGENT_DATABASE_URL": "postgresql+asyncpg://agent:agent_dev_password@localhost:5433/personal_agent",
         "AGENT_ELASTICSEARCH_INDEX_PREFIX": "agent-logs-test",
         "AGENT_CAPTAINS_LOG_INDEX_PREFIX": "agent-captains-test",
     }
     for _key, _value in _TEST_SUBSTRATE_ENV.items():
         os.environ[_key] = _value  # hard pin — NOT setdefault
     ```
   - Pre-flight: call `harness.detect_embedding_backend()`; if `"zero-vector"`, print an actionable
     error (`docker start cloud-sim-embeddings`, retry) and exit non-zero rather than running a
     degenerate all-zero-vector A/B.
   - `_set_multipath(enabled: bool) -> None`: sets `settings.multipath_recall_enabled`,
     `settings.lexical_arm_enabled`, `settings.multiquery_arm_enabled = enabled`; always sets
     `settings.recall_similarity_floor = 0.60` and **`settings.relevance_bounded_recall_enabled = False`**
     (codex finding #6 — pin this explicitly so the ADR-0100 flag can't confound the ADR-0104 A/B; record
     the pinned value in the report metadata). Reset floor to `0.0` and all flags to `False` in a
     `finally`, matching `ab_relevance_bounded.py`'s cleanup pattern.
   - Reuse verbatim: `wipe_substrate`, `seed_replay` (harness.py); `recall_at_k` (metrics.py);
     `load_probe_set`/`ProbeCase` (probes.py); adapt `_entity_recall`/`_broad_hit` from
     `ab_relevance_bounded.py` (same signatures, same `flatten_recall`/`_capitalized_entity_hints` reuse).
   - Per gate set (`bespoke_probe.yaml`, `semantic_probe.yaml`), run once each:
     `wipe_substrate` → co-seed **every** case in the set (`seed_replay` per case, no wipe between) →
     for each case, run entity-path + broad-path recall **twice** (multipath OFF, then ON), timing each
     call with `time.perf_counter()` → compute recall@5 off/on, `recovered` (AC-3), broad hit off/on,
     and collect per-query latencies for the ON state.
   - **Dense-arm floor invariant** (codex finding #3 — label explicitly, this is not a whole-pipeline
     check): reuse the `_capture_cosines`-style query against `service._query_entity_vector_candidates`
     over the co-resident set to get each case's best expected-entity cosine; report `min(positives)` vs
     `0.60` as `dense_floor_invariant_ok` in the output, with a docstring/field name that makes clear it
     covers the dense arm's candidate scores only, not the lexical arm (rank-only, no cosine) or the fused
     RRF output (no single comparable threshold by design).
   - p50 latency: `statistics.median` over the collected ON-state per-query latencies; report must state
     the ≤17s ceiling and pass/fail against it (AC-6c / FRE-724 AC-6b).
   - `_run_window_check(service)` (FRE-658 / Decision 5, **corrected per codex finding #2/#5**): seed one
     Turn 40 days old under a uniquely-tagged entity name (e.g. `"FRE778 Window Probe"`) not used
     elsewhere in either gate set. With multipath ON, call `service.query_memory(...)` **directly on the
     raw `MemoryQuery`** (bypassing `MemoryServiceAdapter`, which has no `hard_recency_days` field) with a
     non-empty `query_text` (required for the multipath dispatch at `service.py:2494-2507` to fire):
     `MemoryQuery(query_text="FRE778 Window Probe", hard_recency_days=7, ...)` (expect empty — older than
     the window) vs `MemoryQuery(query_text="FRE778 Window Probe", hard_recency_days=None, ...)` (expect
     the turn present — de-gated). Report both outcomes + pass/fail.
   - Output: `MultipathABReport` dataclass (run_id, timestamp, floor=0.60,
     relevance_bounded_recall_enabled=False (pinned, per codex finding #6), per-gate-set
     `GateSetReport` [recall_off/on means, lift, recovered count, broad_off/on, p50_latency_on_s,
     ceiling_ok, dense_floor_invariant_ok, dense_floor_invariant_min_positive],
     `window_check: WindowCheckResult`) →
     JSON at `telemetry/evaluation/fre778-multipath-ab/ab-{run_id}.json` + a printed human summary
     (mirrors `_print_summary` in `ab_relevance_bounded.py`).
   - CLI: `--run-id`, `--gate-set {lexical,semantic,both}` (default `both`; `lexical`→bespoke_probe.yaml,
     `semantic`→semantic_probe.yaml), `--prod-k` (default 5), `--out`.

2. **Gitignore:** add `telemetry/evaluation/fre778-multipath-ab/` to `.gitignore` (matches the existing
   `fre817-corpus-ab/`/`fre720-insights-separation/` per-ticket pattern).

3. **Fix the `fetch_live_distractors` credential-reuse bug (owner-approved, Decision 2):** in
   `scripts/eval/fre435_memory_recall/harness.py`, replace the
   `os.environ.get("AGENT_NEO4J_PASSWORD") or settings.neo4j_password` fallback with a required
   `FRE435_LIVE_NEO4J_PASSWORD` env var; raise `RuntimeError` before constructing the Neo4j driver if it's
   unset (fail-closed, no silent reuse of the test-substrate password). Update
   `ab_relevance_bounded.py`'s module docstring / `--distractor-background` help text to note the new
   requirement. Note in the PR description that this changes FRE-655's script's default behavior.

4. **Unit tests** (pure logic only, no substrate) — new file `tests/test_eval/test_fre778_multipath_ab.py`
   mirroring `tests/test_eval/test_recall_calibration.py`'s convention:
   - p50/latency-ceiling pass/fail computation given a list of durations.
   - Floor-invariant computation given positive-cosine lists (including the empty-list edge case).
   - `_set_multipath` toggles exactly the three flags + floor, and resets cleanly (can be tested against
     a lightweight fake/namespace object rather than the real `AppConfig` singleton if that's simpler).
   - `fetch_live_distractors` raises `RuntimeError` (before any network/driver call) when
     `FRE435_LIVE_NEO4J_PASSWORD` is unset — add to `tests/evaluation/test_fre435_isolation.py` or a new
     test alongside it, matching whichever file already covers `harness.py`'s substrate-safety guards.

5. **Live run** (manual, not CI — mirrors existing FRE-435/655 scripts): with the test substrate up
   (already running: `seshat-neo4j-test-1`/`seshat-postgres-test-1`/`seshat-elasticsearch-test-1`,
   healthy) and `docker start cloud-sim-embeddings` first:
   ```bash
   uv run python scripts/eval/fre435_memory_recall/ab_multipath.py --run-id fre778-$(date +%Y%m%d) --gate-set both
   docker stop cloud-sim-embeddings   # restore managed-embedder-only posture afterward
   ```
   Confirm the JSON report shows: recall lift > 0 on at least one gate set, ≥1 `recovered` case (AC-3
   tail-win), p50 ≤ 17s, floor invariant holds (or is honestly reported as failing with the actual
   minimum), window check passes both branches.

6. **Curated summary:** `docs/research/2026-07-08-fre-778-multipath-ab-driver.md` (dated at actual
   write time) — the numbers from the live run, following the FRE-655 report's structure. Raw JSON stays
   gitignored per Step 2.

## Quality gates

`make test` (module: `make test-file FILE=tests/test_eval/test_fre778_multipath_ab.py`, then full) ·
`make mypy` · `make ruff-check` + `make ruff-format` · `pre-commit run --all-files`. Effort-sized
self-review at the pre-PR gate: **high** (touches eval-harness `src`-adjacent logic reused against a
real substrate, plus the credential-safety design decision) — code-review + security-review both run
before PR per the build skill.

## Owner sign-off (2026-07-08)

1. **Distractor/haystack strategy: co-resident cases only** — confirmed. No live-prod-read path built.
2. **`fetch_live_distractors` credential bug: fix now**, not as a follow-up — confirmed (overrides my
   initial recommendation). Folded into Step 3 above.
3. **Scope framing: test-substrate proof instrument only** — confirmed. This driver does not attempt to
   approximate FRE-724's live/prod graduation proof; it gives FRE-724 and master a real measured
   off-vs-on number from an isolated environment.

Plan approved; proceeding to TDD implementation.
