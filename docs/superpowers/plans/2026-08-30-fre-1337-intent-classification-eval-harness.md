# FRE-1337 — Intent-classification eval harness

Owner-directed, Urgent, self-approved 2026-08-30. Feeds FRE-1288 with data. Full ticket text:
see Linear FRE-1337.

## Codex plan-review (2026-08-30) — findings incorporated

1. **Model-identity bug found and fixed.** `get_llm_client_for_key(key, budget_role=...)` for a
   **local** deployment returns a bare `LocalLLMClient()` (`factory.py:94`) that discards `key`
   entirely; `LocalLLMClient.respond(role=...)` then re-resolves the model from
   `get_current_selection(role.value)` (empty for a script) falling through to the role's
   `config/model_roles.yaml` binding — **not** the key I asked for. Cloud is unaffected
   (`LiteLLMClient`'s model is fixed at construction; `role` is telemetry-only there). Fix: before
   every `respond()` call, `set_current_selection({"study": model_key})` (`config/selection.py`) —
   `resolve_role_target` honors an explicit `model_key` unconditionally (`model_loader.py:357`),
   so this pins the deployment regardless of placement. `reset_current_selection()` after.
   Belt-and-suspenders: record `raw["model"]` (or equivalent) from the response and assert it
   matches the requested deployment's `id` — surfaced per-row in the JSON output, not just
   asserted once.
2. **Arm-3 isolation gaps, both real.** (a) `docker-compose.eval.yml`'s two eval gateways point at
   the **same Redis DB 0** as production (`AGENT_EVENT_BUS_REDIS_URL: redis://redis:6379/0`,
   identical to `docker-compose.cloud.yml`'s prod gateway) — not fixed here (shared infra file,
   out of ticket scope, and Redis carries no KG/entity data this harness's AC-3 claim depends on);
   documented as a known gap in the harness README, follow-up ticket filed. (b) `make
   eval-infra-down` only stops the gateway containers, it does **not** wipe `neo4j-eval`'s volume
   — corrected below; the harness owns its own wipe via Cypher, not container teardown.
3. **AC-3 mechanism replaced.** The planned scoped `DETACH DELETE ... WHERE originating_session_id
   = $id` misses `:Session`-only nodes with no `originating_session_id` (`service.py:1398`) and
   risks orphaning cross-session-adopted entities (the exact hazard
   `fre1122_absence_probe/ground_truth.py` already warns about). Replaced with a **full-graph
   wipe** (`MATCH (n) DETACH DELETE n`, the same `WIPE_CYPHER` `fre435_memory_recall/harness.py`
   uses) run between every fixture in the behavioral arm — safe here specifically because it never
   targets anything but `neo4j-eval`. Guard: **hardcoded URI allowlist**, not the `Environment`
   enum (`APP_ENV=eval` in the compose file actually resolves to `Environment.DEVELOPMENT` per
   `env_loader.py`'s fallthrough — there is no `Environment.EVAL` — so the existing
   `wipe_substrate()`'s `Environment.TEST` guard doesn't fit and isn't reused as-is). The wipe
   function refuses any URI other than the literal `bolt://localhost:7689`; the behavioral driver
   refuses any `/chat` base other than `http://localhost:9002`. Both hardcoded, not
   flag-overridable, so a misconfigured env can't silently point the wipe at `:7687` (prod).
4. **AC-1 tightened.** All 3 models are mandatory — the live run fails loudly (non-zero exit) if
   any model's probe call errors, rather than silently reporting a 2-model matrix. Reachability
   confirmed live 2026-08-30 (see below), so this should hold.
5. **AC-4 tightened.** `behavioral.py` asserts every required field
   (`tool_call_count`/`web_search_count`/`fetch_url_count`/`input_token_growth`/`wall_time_s`/
   `tool_budget_exhausted`) is non-`None` before writing the report row; a `None` fails the run
   rather than silently shipping a partial row.
6. **AC-5 tightened.** The fixture-level test (deterministic classifier agrees with the fixture's
   `expected_task_type`) stays as a cheap correctness lock, but is **not** the AC-5 evidence. The
   live run adds an explicit post-hoc check over the real confusion matrix: at least one diagonal
   cell (deterministic == live model classification) must have count > 0 for at least one model,
   or the run fails loudly. This is the actual AC-5 gate — a live model agreeing, not a fixture
   asserting what the deterministic side alone does.
7. **Module count reduced** (6 instead of 9): `confusion.py` folds into `harness.py` (small, pure,
   still unit-tested via direct import); `contamination.py` renamed `substrate.py` and now holds
   only the URI-guarded full wipe + the cross-session source check.

## Design decisions (resolving ticket ambiguity)

1. **Three arms, three files, one CLI.**
   - Arm 1 "deterministic" — calls `request_gateway.intent.classify_intent()` directly. No I/O,
     no substrate contact at all.
   - Arm 2 "probe" — a raw, single-turn `client.respond()` call per model with the taxonomy
     injected and **no tools, no history**. Contamination-free *by construction*: there is
     nothing for `search_memory` to be, because no tool definitions are ever passed. This
     satisfies AC-2 and the "search_memory disabled for probe turns" branch of AC-3 in one move.
   - Arm 3 "behavioral" (optional, flag-gated) — drives one fixture through a live `/chat`,
     reads back tool-call/token/wall-clock signals from Elasticsearch (AC-4), and is the one arm
     that can touch a knowledge graph. It targets **only** the isolated eval gateway
     (`docker-compose.eval.yml`'s `seshat-gateway-control` on :9002, backed by
     `neo4j-eval`/`elasticsearch-eval`/`postgres-eval`) — never the production gateway on :9001.
     This satisfies FRE-375 by construction (separate substrate, separate volumes) rather than by
     opt-in flag.

2. **Contamination control for arm 3 (AC-3).** Before each fixture's behavioral run, DETACH
   DELETE any Neo4j nodes carrying `originating_session_id` from a prior fixture in *this run*
   (scoped, parameterized Cypher — never a bare `MATCH (n) DETACH DELETE n`). The proof required
   by AC-3 ("run the same question twice in sequence, show the second run's sources don't
   reference the first") is a dedicated harness mode: run fixture A, capture `session_id_A`; wipe
   nodes tagged with `session_id_A`; run fixture A again, capture `session_id_B`; assert no
   source/tool-result in `session_id_B`'s trace references `session_id_A`. Because this all runs
   against `neo4j-eval` (isolated, disposable volume), wiping is safe and reversible
   (`make eval-infra-down` tears the whole thing down).

3. **Model keys.** `qwen3.6-35b-thinking` (local, current primary post-FRE-1317/1319 revert),
   `qwen3.6-27b-ovh` (OVH-managed), `claude_sonnet` — via
   `llm_client.factory.get_llm_client_for_key(key, budget_role="study")`, matching the
   `scripts/study/categorizer.py` precedent. Reachability confirmed live 2026-08-30: SLM tunnel
   (`curl localhost:8600/v1/models` → 200, lists `unsloth/qwen3.6-35-A3B` and `-27B`) and OVH
   endpoint (`curl oai.endpoints.kepler.ai.cloud.ovh.net/v1/models` → 200) both answer from this
   worktree's network. `budget_role="study"` bills the `study` cost-gate lane (existing,
   unmetered-script-safe per `cost_gate/role_map.py`), against the **TEST** substrate Postgres
   (`postgresql://...localhost:5433`, FRE-375 pattern from `fre1286_entailment/harness.py`) — a
   `CostGate` is registered explicitly in-process exactly as that harness does, since a standalone
   script has no application startup to have registered one.

4. **Taxonomy definitions.** No existing per-`TaskType` prose string exists in the codebase (only
   regex-bank comments in `intent.py`). `taxonomy.py` writes one honest one-line definition per
   member, paraphrased from those comments — reviewed for accuracy, not invented.

5. **Fixtures.** Four, matching the ticket's explicit list: the real GPSR research question (the
   7-for-7 failure case, disagreement expected), a genuinely conversational greeting (AC-5 seeded
   agreement — asserted against `classify_intent()`'s actual current output, not aspirational), an
   unambiguous tool-use request, a memory-recall request.

## Files

- `scripts/eval/fre1337_intent_probe/__init__.py`
- `scripts/eval/fre1337_intent_probe/taxonomy.py` — `TASK_TYPE_DEFINITIONS`, `build_probe_prompt()`
- `scripts/eval/fre1337_intent_probe/fixtures.py` + `fixtures.yaml`
- `scripts/eval/fre1337_intent_probe/probe.py` — `classify_with_model()` (arm 2), pins the
  requested deployment via `set_current_selection` and asserts the response's `raw["model"]`
  matches
- `scripts/eval/fre1337_intent_probe/substrate.py` — URI-guarded full-graph wipe (`neo4j-eval`
  only, hardcoded `bolt://localhost:7689` allowlist) + cross-session source check
- `scripts/eval/fre1337_intent_probe/behavioral.py` — arm 3 driver (ES read, mirrors
  `fre481_decomposition_ab/harness.py`'s `fetch_rounds`/`fetch_routing` helpers, extended with
  `web_search`/`fetch_url` call counts and budget-exhaustion; asserts field completeness).
  Hardcoded `/chat` base allowlist (`http://localhost:9002` only).
- `scripts/eval/fre1337_intent_probe/harness.py` — CLI orchestrator; also owns the confusion
  matrix build/render (pure functions, still unit-tested via direct import — no separate module);
  emits JSON rows + confusion matrix to `telemetry/evaluation/fre1337-intent-probe/`; fails loudly
  (non-zero exit) on a missing model (AC-1) or an absent diagonal cell (AC-5)
- `scripts/eval/fre1337_intent_probe/README.md` — usage, substrate isolation statement, the known
  eval-Redis-shares-prod-DB gap
- `tests/evaluation/test_fre1337_taxonomy.py`
- `tests/evaluation/test_fre1337_fixtures.py`
- `tests/evaluation/test_fre1337_probe_call_shape.py` (mocked client — AC-2 structural proof +
  the `set_current_selection`/model-identity pin + `raw["model"]` assertion)
- `tests/evaluation/test_fre1337_confusion.py` (imports the pure matrix functions from
  `harness.py`)
- `tests/evaluation/test_fre1337_substrate_guard.py` (URI allowlist refuses anything but
  `bolt://localhost:7689` / `http://localhost:9002`; wipe Cypher is the unscoped full-graph
  statement, guarded only by the URI check, never a bare unguarded query)

## Atomic steps

1. `taxonomy.py` + `test_fre1337_taxonomy.py` → verify: prompt contains all 7 `TaskType` values
   verbatim, contains no deterministic-answer leakage, is a pure function (same input → same
   output).
2. `fixtures.py`/`fixtures.yaml` + `test_fre1337_fixtures.py` → verify: 4 fixtures load, labels
   unique, the seeded-agreement fixture's `expected_task_type` equals
   `classify_intent(msg).task_type` today.
3. `probe.py` + `test_fre1337_probe_call_shape.py` (client mocked) → verify: `tools=None`,
   `messages` is exactly `[{"role": "user", "content": <verbatim prompt>}]`, no
   `previous_response_id` / history passed.
4. `confusion.py` + `test_fre1337_confusion.py` → verify: 2x2 matrix counts, markdown table shape.
5. `contamination.py` + `test_fre1337_contamination.py` → verify: `find_cross_session_sources`
   correctly flags/clears; wipe Cypher string always parameterizes `session_id` (regex-asserted
   no bare `MATCH (n) DETACH DELETE n`).
6. `behavioral.py` — no new unit test (ES-read plumbing mirrors `fre481`'s already-precedented,
   untested-by-design driver code); exercised live in step 8.
7. `harness.py` + `README.md` — CLI wiring, no new unit test (thin orchestration).
8. **Live run for evidence** (not gated by `make test`):
   - `uv run python -m scripts.eval.fre1337_intent_probe.harness --models
     qwen3.6-35b-thinking,qwen3.6-27b-ovh,claude_sonnet` → real deterministic+probe confusion
     matrix, all 4 fixtures, all 3 models (or fewer if a model proves unreachable — document
     which).
   - `make eval-infra-up` (postgres-eval/neo4j-eval/elasticsearch-eval +
     `seshat-gateway-control`) → `uv run python -m scripts.eval.fre1337_intent_probe.harness
     --behavioral --contamination-proof` → captures AC-3's and AC-4's live evidence.
   - `make eval-infra-down` after capturing output.
   - Results committed under `telemetry/evaluation/fre1337-intent-probe/` (tracked baseline per
     root CLAUDE.md's telemetry note) or pasted into the Linear handoff if large.

## Test commands

- `make test-file FILE=tests/evaluation/test_fre1337_taxonomy.py`
- `make test-file FILE=tests/evaluation/test_fre1337_fixtures.py`
- `make test-file FILE=tests/evaluation/test_fre1337_probe_call_shape.py`
- `make test-file FILE=tests/evaluation/test_fre1337_confusion.py`
- `make test-file FILE=tests/evaluation/test_fre1337_contamination.py`
- `make mypy` / `make ruff-check` / `make ruff-format`

## Acceptance-criteria mapping

- AC-1 (confusion matrix, real runs) → step 8's first command, `confusion.py` render.
- AC-2 (uncontaminated probe, demonstrated) → step 3's test + `probe.py`'s no-tools/no-history
  call shape + prompt recorded verbatim in every JSON row.
- AC-3 (contamination controlled, evidenced) → `contamination.py` + step 8's
  `--contamination-proof` run against isolated `neo4j-eval`.
- AC-4 (behavioral signals per turn) → `behavioral.py`'s ES read, step 8's `--behavioral` run.
- AC-5 (seeded agreement) → step 2's fixture + assertion, reproduced live in step 8.

## Risk tier

**Standard** — touches cost-gate billing (real `study`-lane spend) and Neo4j writes/deletes
(scoped, against isolated eval substrate only). Codex plan-review required per the build skill.
