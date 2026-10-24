# FRE-1668 — Artifact builder side-by-side: Sonnet 5, Sonnet 5.5, gpt-6.1-sol

Ticket: FRE-1668 (owner request 2026-10-24). Design intent: ADR-0122 (build-time builder selection),
ADR-0118 (builder model selection), ADR-0089 (sealed page). Pattern reused:
`scripts/eval/fre1628_captains_log_ab/harness.py`.

This is a measurement. It ships a harness under `scripts/eval/`. It changes no file under `config/`
and no file under `src/`. The run itself waits for the owner's go (AC-1).

## Decision the harness must answer

Move `artifact_builder` from `claude_sonnet` to `claude_sonnet_5_5` or `gpt-6.1-sol`? The report
gives a recommendation. The owner decides any swap.

## Design

**Unit under test.** `artifact_draft_executor(slug, title, summary, plan)` in
`src/personal_agent/tools/artifact_tools.py`, called unchanged. It builds the real system prompt, calls
`get_llm_client_for_key(<arm key>, budget_role="artifact_builder")`, strips fences, renders Mermaid,
validates the HTML, then calls `artifact_write_executor`.

**The plan is the fixed input.** The builder gets `(slug, title, summary, plan)` from the primary
model. A full turn per arm would let the primary write a different plan per arm, and that would hide
the builder difference. The harness therefore freezes one plan per prompt (step `plans`) and sends the
same plan to every arm.

**Arms.** A `claude_sonnet` (baseline) · A' `claude_sonnet` again (noise) · B `claude_sonnet_5_5` ·
C `gpt-6.1-sol`. The builder resolution for a build is the turn-scoped `user_choice` decision set to
the arm key (`set_artifact_builder_resolution`). A test proves that for `claude_sonnet` this path
gives the same client model and mode as the role path (`get_llm_client(role_name="artifact_builder")`).
Arm order alternates per prompt, so an hour-of-day effect does not fall on one arm.

**No production write (AC-6), by construction.**
1. `artifact_tools.artifact_write_executor` is replaced by a capture function. It saves the HTML to
   the run directory and returns the dict shape the caller reads. No R2, no Postgres artifact row.
2. Three objects bind `settings.database_url` at import: the `CostGate`, the cost-tracker singleton
   (`api_costs` rows) and the SQLAlchemy engine (`session_events`). The harness therefore must start
   with `AGENT_DATABASE_URL` set to the eval Postgres `127.0.0.1:5434`. Before the first call, the
   harness asserts that `settings.database_url`, the cost-tracker DSN and the engine URL all name
   that host and port. Any other value stops the run. The R2 settings must be the disabled values
   (`.invalid` endpoint). The password comes from the environment and is never printed.
3. Every build passes no `session_id`. `phase_span` then emits nothing. The harness asserts that the
   event bus is the no-op bus and that no Elasticsearch log handler or OTLP exporter is attached.
4. The harness opens no Neo4j, Elasticsearch or Redis client. It calls no gateway. The Mermaid step
   runs a local `mmdc` process only.
5. AC-6 proof: `prod-count` reads, from production Postgres, the row counts of `artifacts`,
   `session_events` and `api_costs`, and the newest `created_at` of each (read only, through
   `docker exec ... psql`, the superuser stays inside the container). It runs before the first paid
   call and after the last. The report prints both. The render check makes GET requests to the
   artifacts origin for `/lib/` files. These are reads and the report names them.

**Arm validity (B and C must not silently become A).** Before the first paid call the harness loads the
model catalog, requires `settings.anthropic_api_key` and `settings.openai_api_key`, and resolves each arm
through `resolve_effective_artifact_builder_deployment`. It stops unless resolved key = requested key.
The decision is `ConstraintDecision(arm_key, "user_choice")`. The harness resets the context-variable
token after each build. The row records requested key, resolved key, served model and mode or effort.
Exactly one `model_call_completed` event must exist per successful build, and its model must equal the
catalog id of the requested key. Else the row is a failure with that reason.

**Per-build record.** `arm, prompt_id, requested_key, resolved_key, served_model, effort, ok,
failure_reason, render_ok, console_errors, failed_requests, blocked_requests, latency_s, input_tokens,
output_tokens, cost_usd, html_bytes, output_cap_hit`. Latency is wall clock around
`artifact_draft_executor`. Tokens and cost come from the `model_call_completed` log event captured with
`structlog.testing.capture_logs` (as fre1628 does). A build that raises is a row with `ok=false` and the
exception text as `failure_reason`.

**Render check.** A small Node script, `render_check.mjs`, uses Playwright from
`seshat-pwa/node_modules` and the cached Chromium. It loads the saved HTML, waits for network idle,
and records `pageerror`, `console.error`, failed requests, HTTP errors and a blank-page test (visible
text under 40 characters and no canvas, svg or img). Requests to any host other than the artifacts origin
are aborted and counted as `blocked_requests`, which mimics `connect-src 'none'` plus the host
allowlist of ADR-0089. `render_ok` = page loads, zero page errors, zero console errors, zero failed
requests, zero blocked requests, not blank. Limit, stated in the report: this is not the exact served CSP.

**Blind rating (AC-4).** `bundle` writes two places. `<run>/rating/` holds `index.html` (one sandboxed
iframe per build, labelled `P01..Pnn`, order shuffled with a fixed seed) and nothing else. This
directory is the only thing handed to the owner. `<run>/key.json` (label → arm, prompt) stays in the run
directory root. The owner replies with a 1–5 score per label. `report` accepts exactly one valid score
per label, then unblinds. No LLM judge. (The ticket makes one optional. A judge adds paid calls and a
second position-bias problem, and it cannot replace the owner.)

**Report (AC-2, AC-3, AC-5).** Before it prints a table, `report` checks completeness: exactly 32 unique
`(arm, prompt)` rows, a render result for every successful build, one valid score per label. It fails
loudly otherwise. It prints one table per arm (one row per prompt), a cost-per-build line per arm, and a
decision table. For each measure (success rate, render pass rate, median latency, median output tokens,
mean cost, mean owner score) the table gives the noise band `abs(A' − A)`, then the B and C differences
from A. A B or C difference with `abs(diff) <= noise band` is printed "no decision". One recommendation
line, from the owner score first. When no B or C difference clears the band, the line says "no decision".
The report header states the confound: the arms differ in mode as well as in model (Sonnet 5 and
Sonnet 5.5 use adaptive effort `high`; Sol uses reasoning effort `medium`).

## Prompt set (8)

The ticket asks for most prompts from the owner's real requests of the last 60 days. Read-only checks
on 2026-10-24 show three real HTML builds in 60 days (a concept explainer, a bar chart, a prediction
board). The real plan text is not stored, only plan length, and two of the three requests are
follow-up turns that depend on earlier conversation. The harness therefore uses generic, self-contained
rewrites of those three, plus `ar1` from `scripts/eval/fre1615/suite.yaml` verbatim, plus four
synthetic prompts that fill the required categories. No real conversation text is committed.

| id | category | source |
|----|----------|--------|
| p1 | cheat sheet | fre1615 `ar1`, verbatim |
| p2 | data chart | rewrite of the real bar-chart request |
| p3 | interactive page | rewrite of the real concept-explainer request |
| p4 | interactive page | rewrite of the real prediction-board request |
| p5 | document / report | synthetic |
| p6 | diagram | fre1615 `ar2`, verbatim |
| p7 | document (table) | fre1615 `ar3`, verbatim |
| p8 | edit of an existing page | synthetic; the plan embeds `fixtures/edit_source.html` |

`artifact_draft` builds new pages only (its tool text sends existing HTML to `artifact_write`). The
edit item is therefore a plan that carries the old page and the requested change. The report says so.

## Files

| Path | Purpose |
|------|---------|
| `scripts/eval/fre1668_artifact_builder_ab/__init__.py` | package marker |
| `scripts/eval/fre1668_artifact_builder_ab/harness.py` | subcommands `plans`, `build`, `bundle`, `report`, `prod-count` |
| `scripts/eval/fre1668_artifact_builder_ab/prompts.yaml` | the 8 requests |
| `scripts/eval/fre1668_artifact_builder_ab/render_check.mjs` | headless render check |
| `scripts/eval/fre1668_artifact_builder_ab/fixtures/edit_source.html` | page for p8 |
| `tests/scripts/test_fre1668_artifact_builder_ab.py` | unit tests (no network, no LLM) |

Output lands in `telemetry/evaluation/fre1668-artifact-builder-ab/` (gitignored).

## Steps

1. **Test first (red).** In `tests/scripts/test_fre1668_artifact_builder_ab.py`, write tests that
   fail because the module does not exist:
   - `test_eval_gate_url_refuses_production_port`: `assert_eval_db_url` raises for a `:5432` URL and
     accepts `127.0.0.1:5434`.
   - `test_build_never_calls_real_artifact_write`: patch the real `artifact_write_executor` to raise,
     run `build_one` with a fake client, assert the HTML reached the capture file.
   - `test_sonnet_arm_resolves_like_the_role_path`: client model and mode equal for both paths.
   - `test_blind_labels_hide_arm_and_are_reproducible`: same seed, same order; labels carry no arm text;
     `index.html` contains no arm key and no model id.
   - `test_decision_is_no_decision_inside_the_noise_band`: B within the A to A' band → "no decision";
     outside → named.
   - `test_failed_build_row_has_a_reason`: a raising client gives `ok=false` and a non-empty reason.
   - `test_missing_provider_key_stops_the_run`: an arm whose resolved key differs from the requested key
     raises before any call (no silent substitution).
   - `test_served_model_must_match_the_requested_key`: a row whose logged model differs is a failure.
   - `test_db_binding_assert_covers_gate_tracker_and_engine`: any one of the three on a non-eval URL stops the run.
   - `test_rating_dir_holds_no_key`: `rating/` has only `index.html`; `key.json` sits outside it.
   - `test_report_fails_on_incomplete_rows_or_scores`: 31 rows, a duplicate row, or a missing score raises.
   - `test_budget_headroom_stops_before_the_first_call`: projected spend above any of the three caps raises.
   - `test_prompt_ids_p1_p6_p7_match_fre1615_verbatim`: the verbatim prompts equal `scripts/eval/fre1615/suite.yaml`.
   Run: `uv run pytest tests/scripts/test_fre1668_artifact_builder_ab.py -q` → expect failures.
2. **Implement** `harness.py` until step 1 passes. Keep each function typed and Google-documented.
3. **`render_check.mjs`** plus a test that runs it on a tiny good page and a page with
   `throw new Error()` (skipped if Chromium is missing: the test says why).
4. **Prompts and fixture.** Add `prompts.yaml` and the fixture. The harness module docstring
   documents the commands. No other doc changes.
5. **Gates.** `make test` (unit tests only), `make mypy`, `make ruff-check`, `make ruff-format`,
   `pre-commit run --all-files`. Commit. Self-review with `feature-dev:code-reviewer` on
   `git diff origin/main...HEAD`. Run `security-review` (subprocess and file writes).
6. **Preflight (the only paid calls before the go).** Eval stack up (`make eval-infra-up` is not
   needed: only `postgres-eval` is used, and it is already up). Run `prod-count` (before). Run
   `plans --ids p1`, then `build --ids p1` for A, A', B, C. Render each. Read the full trace of arm C
   to prove that Sol completes on the artifact path (the known 2026-10-20 bridge error). Run
   `prod-count` (after). Post the preflight comment on FRE-1668: environment, one trace per arm with
   the rendered page, time and cost projection for the full run, and vendor credit. Anthropic and
   OpenAI expose no balance endpoint to an API key, so the comment says the balance cannot be read
   and asks the owner for the two balances. **STOP. The full run starts after the owner's go is
   recorded on the ticket.**
7. **Full run (after the go).** `plans` for p2..p8, `build` for all arms, `bundle`, hand the owner the
   rating pack, `report` after the scores arrive. The report is posted on the ticket.

## Acceptance criteria → evidence

| AC | Evidence the build produces |
|----|-----------------------------|
| AC-1 | Preflight comment (step 6). Paid calls before the go: only ar1 plan + 4 builds. |
| AC-2 | `report` decision table; test `test_decision_is_no_decision_inside_the_noise_band`. |
| AC-3 | One row per (arm, prompt); test `test_failed_build_row_has_a_reason`; `report` fails loudly if a build is missing. |
| AC-4 | `bundle` hides arms; test `test_blind_labels_hide_arm_and_are_reproducible`; `report` unblinds only after scores load. |
| AC-5 | Report format; `git diff origin/main...HEAD --stat` shows no `config/` path. |
| AC-6 | `prod-count` before and after; `test_build_never_calls_real_artifact_write`; `test_eval_gate_url_refuses_production_port`. |

## Risks and answers

- **Sol bridge error.** The builder sends no tools, so Sol stays on Chat Completions. The preflight
  must show it. If it fails, stop and report. This ticket does not repair dispatch.
- **Plan author bias.** The frozen plans come from `claude_sonnet` acting as primary. The report
  states that arm A and A' wrote the plan style they then read.
- **One sample per cell.** The noise arm measures how large the one-sample spread is. The report
  does not claim more than that.
- **Cost-gate caps.** `config/governance/budget.yaml` sets daily $1 and weekly $5 for `artifact_builder`
  and for `study`, and a weekly `_total` of $10. The gate reserves only 1,024 output tokens per
  `artifact_builder` call, then reconciles to the real cost, so one large build can push a counter over
  a cap and deny the next call. The harness compares current spend plus projected spend with the daily,
  weekly-role and weekly `_total` caps, separately for the preflight and the full run. It stops before the
  first paid call if any cap lacks headroom. The run does not change the budget file (AC-5). If the
  projection exceeds a cap, the preflight comment says so and asks the owner to choose: raise the cap, or
  split the run over days. Plan-capture calls use `budget_role="study"`.
- **Mode confound.** Arm C runs at reasoning effort `medium`, the Anthropic arms at effort `high`. Each
  is the catalog default mode today. The report states the confound and does not tune either arm.
- **Stale docstring.** `artifact_draft_executor` says "thinking disabled". The catalog now gives these
  models adaptive thinking. Not fixed here (no `src/` change). The handoff mentions it.

## Changes during the build (after the Codex plan review and the preflight)

- The first attempt of a build is the scored one. `--retry-failed` adds later attempts, and the report
  lists them and the total spend. A rerun never replaces a result (best-of-N would favour an arm).
- `bundle` runs the completeness check first and refuses to relabel after `scores.json` exists.
- AC-6 proof by trace id: every paid call's trace id is read back from `budget_reservations` and
  `api_costs` on production (expect 0) and on the eval database (expect one reservation per call).
  Production row counts alone drift, because production is live.
- The artifacts origin is behind an access login. The render check answers `/lib/` from the pinned
  public copies in `config/artifact_lib_substitution_map.json`. Fonts have no public copy and are
  listed as `lib_unavailable`. The rating pack rewrites script and style references the same way.
- `--usd-per-build` and `--usd-per-plan` feed the budget check. The default is the worst case. The
  daily `artifact_builder` cap of $1 binds the full run, so the run is split over days or the owner
  raises the cap. The harness never changes `config/`.
