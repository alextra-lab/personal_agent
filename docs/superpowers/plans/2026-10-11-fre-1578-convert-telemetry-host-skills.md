# FRE-1578 (ADR-0156 T6) — Convert the 8 telemetry and host skills

> Ticket: FRE-1578. Design intent: ADR-0156 D5.1–D5.5, D8.7. Blockers FRE-1576, FRE-1577 and
> FRE-1568 are merged. Risk tier: **Standard** (new scripts that run on the gateway, edits to
> five existing tests).

## Scope

Convert `query-elasticsearch`, `query-tempo`, `self-telemetry`, `turn-forensics`,
`infrastructure-health`, `system-diagnostics`, `system-metrics` and `neo4j-direct`:

1. `evals.yaml` first, 4 scenarios per skill (the ticket asks for at least 3).
2. A `## Success` and a `## Recovery` section in every body.
3. Bodies under 500 lines and 5,000 tokens. `query-elasticsearch` splits into `SKILL.md` and
   `references/`.
4. Fragile command sequences move into tested `scripts/`.
5. The telemetry skills lead with `query_telemetry`. Shared Elasticsearch recipes live in one
   skill and the other skill names them.
6. Every command passes the D8 rules: allow, or a documented `# ask:`.

Owner input from master (2026-10-11): `query-elasticsearch` and `self-telemetry` lead with
`query_telemetry` for counts, aggregations and field questions. Curl stays only for what the
tool cannot do, and each skill says which. Evidence: a primary ran `curl` with a field that
does not exist, and Elasticsearch returned empty buckets with no error.

## Acceptance criteria and proof

| AC | Proof written in this PR |
|---|---|
| AC-1 conformance | `tests/docs/test_skill_validator.py`: the strict-xfail marks of the 8 skills are removed and the 8 pass. `tests/docs/test_skill_conversion.py`: each `evals.yaml` has `skill`, at least 3 scenarios, and a `prompt`, an `expected_skill` (equal to the folder) and an `expect` on each. No prompt repeats across skills. |
| AC-2 the commands run | A subprocess test per script: `--help` exits 0, success exits 0, a failed check exits 1, bad usage exits 2, and each failure prints one `error:` line with no traceback and no secret. The classifier is tested the same way after the move. The D8.7 test reads `SKILL.md` and `references/*.md`, and asserts that the expected reference files were scanned. A new test builds every `query_telemetry(...)` call that a skill shows, with the tool's own validator (seeded negative: `fake_field`). A new test checks that every `docs/skills/<name>/scripts/<file>` path in a body exists. |
| AC-3 no retired content | `tests/docs/test_skill_conversion.py`: every sentence of the 8 skills' files that names a retired field or index also says "retired". Seeded negatives: a table row, a mixed sentence, a `logs-*` index. |
| AC-4 distinct descriptions | `tests/docs/test_skill_conversion.py` extracts the "Use when" clause of each description and asserts 8 distinct values. The catalog table is also in the PR body. |

## Decisions

- **D1 — `metadata.requires-tools` stays as it is.** The catalog filter is T8 (FRE-1580). Adding
  `query_telemetry` here would change who sees a skill before that filter exists.
- **D2 — Script calls ask.** `python3` asks (owner, 2026-10-10). Each script call carries a
  `# ask:` line. An allow rule for one named script path is a governance decision for the
  owner. This PR does not make it. The handoff names the option.
- **D3 — One home per shared recipe.** Generic Elasticsearch mechanics (schema inspection,
  ES|QL, the `_search` template) live in `query-elasticsearch/references/`. Captain's Log
  recipes and the budget-cap snapshot live in `self-telemetry` (they are about outcomes and
  spend). The other skill names the file as `<skill>/<file>` (a path with a leading skill name
  is not read as a file of its own folder by the validator).
- **D4 — `latency` leaves `query-elasticsearch`.** The existing FRE-1568 test requires that
  the description has no "latency". Latency questions go to `query-tempo`, whose first call is
  `query_telemetry` with `action="latency"`. The long `jq` recipe moves to
  `query-tempo/references/` and the FRE-1568 test follows it.
- **D5 — Wrong content found while reading, fixed here.** Live check on 2026-10-11:
  `trace_id` is a pure `keyword` in `agent-logs-*` (no `.keyword` subfield), but two skills
  tell the model to use `trace_id.keyword`. `self-telemetry` also uses `latency_ms`,
  `duration_ms` and the ES|QL command `FIELDS` (the command is `KEEP`). All three go.
- **D6 — Scripts are standalone.** They read `AGENT_*` environment variables with the same
  defaults as `config/`, because importing `personal_agent.config` starts the app config and
  logs into stdout. Each script says so in its docstring.
- **D7 — The read-only guarantee of the Cypher script is the server's, not ours** (Codex
  review, finding 8). The script opens the session in read access mode and runs the query
  with `execute_read`. A clause check runs first, only to give a plain error early. It is not
  the guard. Neo4j Community has no roles, so a read-only user is not available. The test on
  the test stack (`:7688`) runs `CREATE`, `MERGE`, `SET`, `REMOVE`, `DELETE`, a schema write,
  a two-statement input and a write procedure, and asserts that each is refused and that the
  node count is the same after.
- **D8 — A script that reads stdin fails on bad input** (finding 9). Empty, malformed or
  incomplete input exits 1 with one `error:` line. Pipelines use `curl -fsS`, so an HTTP
  error stops the data. The classifier gets this change when it moves.
- **D9 — "Plain error text" is one definition** (finding 11). One line on stderr, starting
  with `error:`. No traceback. No password, token or URI user-info. Exit codes: 0 ok, 1 the
  check or the query failed, 2 bad usage. A test per failure class asserts all three.
- **D10 — `requires-tools` stays `bash`, and T8 owns the change** (finding 13, declined).
  D9 hides a skill unless the worker holds every listed tool. Listing `query_telemetry` next
  to `bash` hides these skills from a worker with the tool and no `bash`, which is the
  worker that needs the tool path most. The format has no "any of" form. The handoff passes
  this to T8.
- **D11 — A path to a file of another skill is plain text.** `self-telemetry` says "load the
  `query-elasticsearch` skill" and names `query-elasticsearch/references/<file>`. A test
  checks that each named `<skill>/references/<file>` and each
  `docs/skills/<name>/scripts/<file>` exists, because the validator does not read these
  spellings (finding 5).

## Files

| Path | Change |
|---|---|
| `docs/skills/<8 skills>/evals.yaml` | new |
| `docs/skills/<8 skills>/SKILL.md` | rewritten: description, lead, success, recovery |
| `docs/skills/query-elasticsearch/references/curl-recipes.md`, `esql.md` | new (split) |
| `docs/skills/query-tempo/references/latency-recipe.md` | new (the `jq` recipe moves) |
| `docs/skills/query-elasticsearch/scripts/es_index_granularity.py` | `git mv` from `scripts/` |
| `docs/skills/infrastructure-health/scripts/probe_services.py` | new |
| `docs/skills/neo4j-direct/scripts/cypher.py` | new |
| `docs/skills/turn-forensics/scripts/call_timeline.py` | new |
| `tests/docs/test_skill_validator.py` | remove the T6 xfail marks |
| `tests/docs/test_skill_conversion.py` | new: evals, retired names, `query_telemetry` calls |
| `tests/docs/test_skill_scripts.py` | new: the three new scripts |
| `tests/docs/test_es_index_granularity.py` | `git mv` from `tests/scripts/`, import by path |
| `tests/personal_agent/governance/test_skill_commands_permissions.py` | scan `SKILL.md` and `references/*.md`, assert the expected reference files were scanned, new thresholds |
| `tests/personal_agent/orchestrator/test_latency_skill_paths.py` | the description checks stay on `SKILL.md`. Pattern 5, the `jq` programs and the allowlist checks read `references/latency-recipe.md` |
| `tests/personal_agent/orchestrator/test_cost_skill_total_spend.py` | the two `query-elasticsearch` recipe tests become one pointer test. The body keeps `embedding_generated`, `reranker_applied` and the "LLM-only" caveat, so the two parametrised tests stay unchanged |
| `docs/skills/SKILL_TEMPLATE.md` | the `evals.yaml` example gets `expected_skill` on each scenario |
| `docs/guides/TELEMETRY_ELASTICSEARCH_INTEGRATION.md` | script path |
| `docs/reference/PROMPT_CORPUS.md` | regenerated with `scripts/render_prompt_corpus.py` |

## Steps (TDD order)

1. **Red.** Remove the T6 marks in `test_skill_validator.py`. Write `test_skill_conversion.py`
   and `test_skill_scripts.py`. Run
   `uv run pytest tests/docs -q` → the 8 skills and the new tests fail.
2. **Evals first.** Write the 8 `evals.yaml`. Boundary scenarios sit in the skill that is
   right: "how slow were model calls" is a `query-tempo` scenario, not a
   `query-elasticsearch` scenario.
3. **Scripts.** `git mv` the classifier and its test. Write `probe_services.py`, `cypher.py`
   and `call_timeline.py` with `--help`, plain error text and exit codes (0 ok, 1 the check
   failed, 2 bad usage). Run `uv run pytest tests/docs/test_skill_scripts.py -q` → green.
4. **Bodies.** Rewrite the 8 skills. Run
   `uv run pytest tests/docs tests/personal_agent/governance/test_skill_commands_permissions.py -q`.
5. **Coupled tests.** Update the three tests in the table. Run
   `uv run pytest tests/personal_agent/orchestrator tests/personal_agent/captains_log/test_turn_evidence.py tests/evaluation/test_fre453_canonical_evalset.py tests/evaluation/test_skill_routing_analysis.py -q`
   (the last three only to prove no change is needed).
6. **Regenerate** `docs/reference/PROMPT_CORPUS.md` with `scripts/render_prompt_corpus.py`,
   now that the bodies are final.
7. **Live read checks** (no writes): each kept curl recipe against `localhost:9200` and
   `tempo:3200`; each script against the test stack (`:7688`, `:5433`, `:9201`) and against a
   stub. Paste the real output in the PR.
8. **Gates.** `make test` (one pytest at a time), `make mypy`, `make ruff-check`,
   `make ruff-format`, `pre-commit run --all-files`. Then `git status` shows only the
   intended files.
9. **Self-review** on the committed branch: `feature-dev:code-reviewer` and `security-review`
   (the scripts run model-written input: Cypher, a trace id, a service name).
10. **PR and handoff** on FRE-1578.

## Plan review (Codex, 2026-10-11)

16 findings. Accepted and folded in: 1–12, 14, 15 (tightened to the sentence), 16. Declined:
13 (see D10). Findings 3, 6 and 7 are read from `permissions.py` and `permissions.yaml`, not
run, so the D8.7 test run in step 4 is the proof.

## Risks

- **The Cypher script runs model-written text on the production graph.** It opens a read
  transaction and refuses a write clause before it connects. Both are tested. The test stack
  proves what the server does in a read transaction (not assumed).
- **Counts in the D8.7 test fall** because recipes become tool calls. The thresholds change
  with a stated reason, and the set-equality check on the four telemetry skills stays.
- **`.keyword` on `trace_id`** is wrong today. The fix is checked against the live mapping.
