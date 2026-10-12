# FRE-1590 skill test

## Part 1: first-call skill loading

Part 1 measures one thing: does the primary model's first call load the right skill?

The items are the 43 scenarios of `skills/*/evals.yaml` and the 20 no-skill prompts of
`scripts/eval/fre1581/no_skill_prompts.yaml`. `load_items()` of `scripts/eval/fre1581/g2_skill_choice.py`
loads them. The models are the catalog keys `qwen3.8-flash-next` and `gemma-4-26b-a4b`. llama.cpp serves both
on `http://127.0.0.1:8600`, one model at a time.

### Prerequisites

- The eval substrates run: `make eval-infra-up` (steps 1 to 3 only).
- `POSTGRES_PASSWORD`, `NEO4J_PASSWORD` and `AGENT_OWNER_EMAIL` are in the environment (step 1 only).
- llama.cpp serves the model of the catalog key (step 5 only).

Set a run directory once. Use the same directory for every step.

```bash
RUN=telemetry/evaluation/fre1590-skill-test/$(date +%Y%m%d-%H%M)
KEY=qwen3.8-flash-next    # or gemma-4-26b-a4b
```

The captured bodies hold full prompts. `.gitignore` lists `telemetry/evaluation/fre1590-skill-test/`.
Do not commit a run directory outside it.

### Steps (run from the repo root)

| # | Command | Writes | Calls a model |
|---|---------|--------|---------------|
| 1 | `uv run python -m scripts.eval.fre1537.gateway up --run-dir $RUN` | two containers | no |
| 2 | `uv run python -m scripts.eval.fre1590.triggering capture --run-dir $RUN --model $KEY` | `$RUN/captured/$KEY/*.json` | no (stub) |
| 3 | `uv run python -m scripts.eval.fre1537.gateway down` | removes the two containers | no |
| 4 | Load the model of `$KEY` in llama.cpp. | | |
| 5 | `uv run python -m scripts.eval.fre1590.triggering replay --run-dir $RUN --model $KEY --llama-model <name> --quant <quant>` | `$RUN/rows/$KEY/part1.jsonl`, `fingerprint.json` | yes, 63 items x 3 draws |
| 6 | `uv run python -m scripts.eval.fre1590.triggering score --run-dir $RUN --model $KEY` | `$RUN/rows/$KEY/report.json` | no |

Do step 2 once for each key while the gateway is up. Do steps 4 to 6 once for each key.

Step 5 calls the owner's llama.cpp. Ask the owner first.

Each step takes `--limit N` (steps 2 and 5) to run the first N items only. Use it for a dry run.

### What each step does

- **`capture`** runs one turn per item through the fre1537 probe gateway, in a new session. The gateway's
  model is the recording stub. The stub answers "OK", so no model runs. The step sends `$KEY` as the `model`
  of `/chat`, so the captured request carries the sampling of that deployment. It saves the primary request
  body with the id, kind, expected skill and prompt. The file name is the id with `#` changed to `__`. A rerun
  skips a file that exists. A failed item gets no file, and the step exits 1.
- **`replay`** sends each captured body to llama.cpp, once per draw, not streamed. The default is 3 draws.
  The request is the captured body with two changes: `model` is `--llama-model`, and `stream` is false. The
  step drops `stream_options` only, because it applies to a streamed reply. Every other key stays, so
  `max_tokens`, the sampling and `chat_template_kwargs` (the thinking mode) are the production ones. One row
  per call records the tool names in order, the `name` of every `read_skill` call, every `read_skill` call
  without a readable `name`, `finish_reason`, `usage`, the wall seconds, the content length, the reasoning
  length and the llama.cpp build (`system_fingerprint`). A transport or HTTP error writes an error row. A
  rerun skips each (id, draw) that has a row without an error.
- **`score`** prints the table and writes `report.json`. It uses the last row without an error for each
  (id, draw).

### The fingerprint

`replay` writes `fingerprint.json` before its first call. It holds the catalog key, the llama.cpp model name,
the quant, the llama.cpp build, the git HEAD, the SHA-256 of the captured system prompt, the SHA-256 of the
captured tools, the tool count, a hash of the sorted tool names and the draws. The first reply fills the build.

`replay` stops when the rows hold a different fingerprint. It also stops when a reply reports a different
llama.cpp build, and it does not write that reply. Use a new run directory for a new configuration. The git
HEAD is recorded but does not stop a run: a change to the request shows in the prompt and tool hashes.

### The scorer

A draw is the first call of one item.

| Item | Outcome | Rule |
|------|---------|------|
| Scenario | right | The `read_skill` names hold the expected skill and no other skill. A repeat counts as right. |
| Scenario | wrong | A `read_skill` call loads any other skill, with or without the expected skill. |
| Scenario | malformed | No other skill loads, and a `read_skill` call has no readable `name`. |
| Scenario | none | The first call has no `read_skill` call. |
| No-skill prompt | load | The first call has any `read_skill` call, a malformed one too. |

The report gives:

- per expected skill: the scenarios, the draws, and the right, wrong, none and malformed counts, with the
  other skills that loaded;
- per no-skill prompt: the draws and the loads;
- **missing**: each item without a row without an error, with its reason: "not captured", "every replay
  errored" (with the last error), or "captured, not replayed";
- **incomplete**: each item with fewer draws than the fingerprint, with its reason.

The score uses all 63 items. A `--limit` run therefore shows the other items as missing.

### Tests

`uv run pytest -q tests/test_eval/test_fre1590_triggering.py` runs offline: no gateway, no model, no Docker.

## Part 2 — Does a skill help?

Part 2 runs each scenario in `part2_scenarios.yaml` as one full turn on two eval gateways:
- treatment (port 9003) has the skill catalog;
- control (port 9002) has no skill catalog.

Each turn starts a new session. The model is a catalog key: `qwen3.8-flash-next` or
`gemma-4-26b-a4b`. The scenario list is a draft. The owner and master confirm it in the preflight.

The control arm masks every `SKILL.md` with `/dev/null`. An empty `/app/skills` is not
used, because the permission rules then fail to load and every tool call is denied. The control
arm also gets the primitive tools, as treatment has them, so the two arms differ only in the
catalog.

### Before the run
1. From /opt/seshat, run `make eval-infra-up`.
2. Start the two gateways with the override. Use the command in the header of
   `docker-compose.fre1590.yml`. Do the dry run through `compose_plan_guard` first.
3. Run `uv run python -m scripts.eval.fre1590.envdiff`. Every difference must be expected. If a
   key is UNEXPECTED, stop and ask the owner.
4. Set `NEO4J_PASSWORD` to the eval Neo4j password.

### Run
    uv run python -m scripts.eval.fre1590.help run --run-dir DIR --model KEY --limit 1
    uv run python -m scripts.eval.fre1590.help run --run-dir DIR --model KEY

Do the run with `--limit 1` first. Read the two rows. Each row must have no error, a reply, and
`model_calls` above 0. The control row must have `skill_catalog_chars` 0. The treatment row must
have `skill_catalog_chars` above 0.

Before each turn, the harness clears neo4j-eval and waits until no local model call is in flight.
After each turn, it waits 20 seconds and reads the turn's events from eval Elasticsearch. The rows
go to `DIR/rows/KEY/part2.jsonl`. If you start the run again, it skips each scenario and arm that
has a row without an error.

### Report
    uv run python -m scripts.eval.fre1590.help report --run-dir DIR --model KEY

The report shows the two arms of each scenario side by side. It writes
`DIR/rows/KEY/part2_report.json`. AC-3 fails if a scenario has only one arm. The report does not
judge the replies. A judge compares each reply with the `expect` line in the row.

### After the run
Run `make eval-infra-up` to start the gateways again without the override.
