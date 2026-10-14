# FRE-1598 — the benefit run on Flash-Next (ADR-0156 A5, T-D2)

**Ticket:** FRE-1598. **ADR:** ADR-0156, amendment of 2026-10-12, AC-F4 benefit. **Seat:** build1.
**Harness base:** `scripts/eval/fre1590/help.py` (FRE-1590 Part 2).

## Goal

Run each pinned scenario of the seven area skills as one full turn on two eval gateways, on Flash-Next.
Two blind judges label each reply against the scenario's `expect` text. The skills pass if no scenario
scores lower with skills than without them.

## Owner decision (2026-10-13, before the plan)

**Control arm = same build, tools pre-revealed.** Both arms run the final build (`5f60cc54` or later).

- Treatment (:9003): the skill catalog, and `search_logs` / `measure_model_latency` hidden until a
  `read_skill` reveals them (production).
- Control (:9002): every `SKILL.md` masked with `/dev/null` (the FRE-1590 override). Before `/chat`, the
  harness writes both tools into the new session's `sessions.revealed_tools` in postgres-eval. So
  both tools are visible from call 1, and no skill can load.

The arms then differ in the skill text and the reveal step only.

## Acceptance criteria (from the ticket)

| AC | Pass when | Evidence |
|---|---|---|
| AC-1 | For every scenario, score(treatment) ≥ score(control). Score = sum of both judges' points (PASS 2, PARTIAL 1, FAIL 0). A PASS/FAIL split goes to a blind third judge, whose label replaces the lower label | `judge.py score` → `ac1` verdict and the per-scenario table |
| AC-2 | Every reply has a label from judge 1 and judge 2, and every PASS/FAIL split has exactly one third label; the note lists each label, the third-judge cases and both arms' scores | `judge.py score` → `ac2`; with a missing third label, `ac1` is not computed (reported as `incomplete`); the research note |

Instrument checks (not ticket ACs, but a row that fails one is an error row, not a scored row). Codex
plan review, 2026-10-13: a database read-back alone does not prove that the model saw the tools.

- The per-call tool list comes from `tools_passed_to_llm` (`executor.py:7068`, DEBUG). DEBUG events reach
  eval Elasticsearch (854 found on 2026-10-13), and the events carry `tool_names` and `mode`.
- Every primary call of the turn has `mode` `NORMAL`.
- Control: `skill_catalog_chars` 0 (existing check). The seeded `revealed_tools` read back equals the
  two tools. The **first** primary call lists both `search_logs` and `measure_model_latency`.
- Treatment: `skill_catalog_chars` > 0 (existing check). The session's `revealed_tools` is empty before
  `/chat`. The first primary call lists neither tool. After a `skill_tools_revealed` event, every later
  primary call lists the revealed tools.
- Either arm: an `orchestrator_call_failed` event, or the generic reply "An error occurred processing
  your request", makes an error row (`app.py:2432` turns an orchestrator failure into HTTP 200).
- Recorded, not an error: `tool_not_revealed` refusals, read from `tool_call_completed` with
  `success=false` and `error_code="tool_not_revealed"` (`tool_dispatch.py:110`), and the event types of
  every `level: ERROR` event of the turn. Tool failures stay legal: two scenarios expect bash scripts
  to fail on eval.

## Scenarios (proposal; the owner confirms in the preflight)

Rule of FRE-1590 (owner): 1 to 2 per skill, only where the skill holds knowledge that the system prompt
and the two tool descriptions do not give. The tool descriptions now state the 30-day and 7-day caps,
`probed_at`, `slm_chat` prefill and `complete=false`, so the scenarios that test only those are left out.

| id | Knowledge only the skill gives |
|---|---|
| graph-inspection#1 | `first_seen`, not `created_at`; the Cypher script |
| interaction-outcomes#1 | captures range on `timestamp`, not `@timestamp`; terms on `outcome` |
| interaction-outcomes#2 | the reflections index and `proposed_change.seen_count` |
| log-search#2 | the cache-erosion family is outside the tool, its date field is `run_at` |
| log-search#3 | the joinability family and the index classifier script |
| model-latency#3 | the tool filters on role and model only; the TraceQL curl recipe |
| service-health#0 | the probe script opens a real connection; curl to 5432 means nothing |
| spend-and-tokens#0 | `cache_read_tokens` on `model_call_completed`; local rate = cached / input |
| spend-and-tokens#1 | `api_cost_recorded` is the whole-spend ledger |
| turn-explanation#1 | the `input_tokens` ladder; `context_tokens` is not queryable (the ADR's named regression) |
| turn-explanation#2 | the Tempo curl GET of a trace by id (the tool returns no spans) |

11 scenarios × 2 arms = 22 turns. The list goes in `scripts/eval/fre1598/part2_scenarios.yaml`.

## Judges (proposal; the owner confirms in the preflight)

- Judge 1: catalog key `claude_sonnet` (`claude-sonnet-5`, Anthropic). Judge 2: `gpt-5.4-mini`
  (OpenAI). Third judge: `claude_haiku` (`claude-haiku-4-5-20251001`). Two families for the pair; the
  third model is a third distinct model. Each judge is pinned by catalog key and resolved model id, and
  the judging prompt's SHA-256 goes in the fingerprint.
- Each call sees one reply alone: the user prompt, the `expect` text and the reply. It does not see the
  arm, the tools, the trace or the other reply. Reply order is a seeded shuffle (seed `20261013`), and
  each reply has an opaque id. The id → (scenario, arm) key is a separate file.
- Called through the agent's LLM client on the `study` budget lane (ADR-0141 AC-6 refuses a raw litellm
  call outside `llm_client/`, found at the pre-commit gate). Catalog default sampling. JSON output
  `{"label", "reason"}`. A reply that
  does not parse is retried once, then recorded as a judge error (AC-2 then fails for that reply).
- Cost: the `cost_usd` that the client returns, per label row.

## Steps

1. **`help.py`: two options, one row field.** (`scripts/eval/fre1590/help.py`)
   - `--scenarios FILE` (default `part2_scenarios.yaml`), passed to `load_scenarios` and `_report`.
   - `--control-reveal NAMES` (comma list, default empty). When set, every turn on both arms first
     creates its session with `POST /sessions` `{}` (the body `/chat` itself uses). On control, the
     harness then writes the names into `sessions.revealed_tools` of postgres-eval and reads them back.
     On treatment, it reads back an empty set. Then `/chat` with that `session_id`.
   - A new `tool_not_revealed` count in `read_turn` (from `tool_call_failed` `error_type`), and the row
     holds `prompt_sha256` and `expect_sha256`.
   - The Postgres writer is a small class in a new module `scripts/eval/fre1598/seed.py`. It connects
     only to `localhost:5434` (asserted, the eval port), with `POSTGRES_PASSWORD`, and updates one row
     by `session_id`. An update count other than 1 is an error row.
   - verify: `make test-file FILE=tests/test_eval/test_fre1590_help.py` and the new tests pass.
2. **`fingerprint.json`** (`scripts/eval/fre1598/pins.py`, run once before the turns by `run`): the model
   key, the catalog entry of the key (sampling, hashed and verbatim), both gateways' `build_fingerprint`,
   the git HEAD, llama.cpp `/props` (or a saved file) and the server launch arguments, and each scenario
   id with the SHA-256 of its prompt and of its `expect`. A second `run` with a different fingerprint
   stops. verify: unit test.
3. **`judge.py`** (`scripts/eval/fre1598/judge.py`), subcommands:
   - `blind` → `judging/items.jsonl` (opaque id, prompt, expect, reply) + `judging/key.json`.
   - `label --judge 1|2` → `judging/labels-<n>.jsonl` (resumable: skips an id that has a label).
   - `third` → labels the PASS/FAIL splits with judge 3 → `judging/labels-3.jsonl`.
   - `score` → `judging/report.json` + a text table: per scenario, each judge's label per arm, the third
     label, the points of both arms, AC-1 and AC-2 verdicts, judge cost.
   - verify: unit tests with a fake judge (no network): blinding hides the arm, a split goes to judge 3,
     the replacement rule, AC-1 fails on a seeded lower scenario, AC-2 fails on a missing label.
4. **Gates:** `make test`, `make mypy`, `make ruff-check`, `make ruff-format`, `pre-commit run --all-files`.
   Commit.
5. **Preflight** (needs master: eval stack rebuilt from `/opt/seshat` at main, the override up, the
   llama.cpp window, the judge keys):
   - `envdiff`: every difference expected.
   - Dry run: `help run --limit 1 --scenarios … --control-reveal search_logs,measure_model_latency`
     (2 turns). Read both rows and the full trace: no error, `model_calls` > 0, the seed read back,
     no `tool_not_revealed` on control, cost events.
   - Judge the two dry replies with judges 1 and 2 (4 calls). Read the labels and the cost.
   - Projection: FRE-1590 Part 2 measured 20 turns in 52 min on Flash-Next, so 22 turns ≈ 60 min. Cost:
     Captain's Log reflection $0.055 per turn ≈ $1.21, judges ≈ $0.5 (measured on the dry run).
   - Post the preflight on the ticket (scenarios, judges and their prompt, numbers). **Stop for the
     owner's OK.**
6. **Run** (after the OK; master holds every seat): `help run` full → `help report` (PAIRED) →
   `judge blind`, `label 1`, `label 2`, `third`, `score`.
7. **Research note** `docs/research/2026-10-13-fre-1598-benefit-run.md`: method, pins, the full label
   table (AC-2), AC-1 verdict, threats to validity (eval bash as `nobody`: the credential scripts of
   service-health and graph-inspection fail on eval; replies may name a skill), proposals.
8. **PR + handoff comment.**

## Known limits, stated in the preflight

- Eval bash runs as `nobody` with no credential (FRE-1505). `probe_services.py` and `cypher.py` fail on
  both arms. This understates the treatment on service-health#0 and graph-inspection#1 (FRE-1590 note).
- A curl recipe reaches production Elasticsearch by its service name (read-only). `search_logs` reads
  eval Elasticsearch. Both arms have both paths.
- The `expect` text describes a method. A judge sees the reply only, so a step that the reply does not
  show counts as missed. This is the FRE-1590 method that the ADR names.
