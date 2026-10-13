# FRE-1598 — the benefit run (ADR-0156 A5, T-D2)

The run answers one question: do the seven area skills make an answer worse? Each scenario in
`part2_scenarios.yaml` runs as one full turn on two eval gateways, on Flash-Next. Two blind judges then
label each reply against the scenario's `expect` text.

| Arm | Gateway | Skills | `search_logs`, `measure_model_latency` |
|---|---|---|---|
| treatment | :9003 | the catalog | hidden until a `read_skill` reveals them (production) |
| control | :9002 | none (`SKILL.md` masked) | visible from call 1: `seed.py` writes them into the session before `/chat` |

The owner chose this control arm on 2026-10-13: the arms differ only in the skill text and the reveal step.

## Before the run

1. From `/opt/seshat`, run `make eval-infra-up`. The eval image must be built from `main`.
2. Start the two gateways with `scripts/eval/fre1590/docker-compose.fre1590.yml`. Use the command in
   its header, with the dry run through `compose_plan_guard` first.
3. Run `uv run python -m scripts.eval.fre1590.envdiff`. Every difference must be expected.
4. Set `NEO4J_PASSWORD` and `POSTGRES_PASSWORD` to the eval passwords.
5. Get a saved llama-server `/props` reply and the launch arguments. The `:8600` router refuses `/props`.

## Run

```bash
RUN=telemetry/evaluation/fre1598-benefit-run/$(date +%Y%m%d-%H%M)
uv run python -m scripts.eval.fre1598.run run --run-dir $RUN --props-file <props.json> --server-args '<args>' --limit 1
uv run python -m scripts.eval.fre1598.run run --run-dir $RUN --props-file <props.json> --server-args '<args>'
uv run python -m scripts.eval.fre1598.run report --run-dir $RUN
```

Do the `--limit 1` run first and read both rows. `run` writes `$RUN/fingerprint.json` before the first
turn. A later `run` with other pins stops.

A row is an error row, and is not judged, when any check fails:

- control: no catalog, the seed reads back both tools, and every primary call lists both tools;
- treatment: a catalog, an empty seed, a first primary call without either tool, and each call after a
  reveal lists the revealed tools;
- both: every primary call in `NORMAL` mode, no `orchestrator_call_failed`, not the generic error reply.

Each row also records `tool_not_revealed` refusals and the event types of the turn's `ERROR` events.

Each row records the path of the turn: `planner_decision`, `sub_agents` (the `sub_agent_start` count),
`model_calls_by_role` and `tool_calls`. A sub-agent sees every skill-provided tool and no catalog in
either arm, so an expanded turn does not use the skill. `tool_calls` holds the calls of the primary and
of the sub-agents: `tool_call_started` carries no role.

## Judge

FRE-375: the judge process runs with its database on postgres-eval, so the cost gate's reservations and
the client's `api_costs` rows land in the eval ledger, not in production. `judge label` refuses to start
otherwise.

```bash
export AGENT_DATABASE_URL="postgresql+asyncpg://agent:<POSTGRES_PASSWORD>@localhost:5434/personal_agent"
uv run python -m scripts.eval.fre1598.judge blind --run-dir $RUN
uv run python -m scripts.eval.fre1598.judge label --run-dir $RUN --judge 1
uv run python -m scripts.eval.fre1598.judge label --run-dir $RUN --judge 2
uv run python -m scripts.eval.fre1598.judge third --run-dir $RUN
uv run python -m scripts.eval.fre1598.judge score --run-dir $RUN
```

- The judges are catalog keys: 1 `claude_sonnet`, 2 `gpt-5.4-mini`, 3 `claude_haiku` (third judge, only
  for a PASS/FAIL split). `judging/judges.json` pins them with the prompt hashes.
- A judge call sees one reply, its prompt and its `expect` text. It does not see the arm, the tools or the
  other reply. `judging/key.json` maps each opaque id to its scenario and arm.
- The calls go through the agent's LLM client on the `study` budget lane of the eval ledger (daily cap
  $1.00, weekly $5.00). A cap denial stops the labelling. Each label row records the cost that the client
  returns.
- `score` writes `judging/report.json`. AC-1 is `incomplete` until AC-2 passes.

## Tests

`uv run pytest -q tests/test_eval/test_fre1598.py tests/test_eval/test_fre1590_help.py` runs offline.
