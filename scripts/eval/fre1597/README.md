# FRE-1597 — the routing run on Flash-Next (ADR-0156 A5, T-D1)

The run measures four things on the FRE-1596 build, on the production primary (llama.cpp, Qwen3.8 Flash-Next):

| Ticket AC | ADR AC | Module | Question |
|---|---|---|---|
| AC-1 | AC-F4 routing | `routing.py` | Does the first call load the right area skill, and no skill on a no-skill prompt? |
| AC-2 | AC-F2 | `loop.py` | After `read_skill`, does the revealed tool complete the five-step loop? |
| AC-3 | AC-F1 | `prompt_size.py` | Is the first request smaller by about the `query_telemetry` definition? |
| AC-4 | AC-F3 | `loop.py` | Does the simple reveal cost at most one costly call per revealed tool, against a control? |

The plan, with the two Codex review rounds: `docs/superpowers/plans/2026-10-13-fre-1597-routing-run.md`.

## How call 2 is captured

The probe gateway's stub (`scripts/eval/fre1537/stub.py`) answers the first primary request with one scripted
`read_skill` call (`next_replies.json`). The gateway runs the real `read_skill`, applies the real reveal and sends
call 2, which the stub records. So call 2 is the production body: the `read_skill` result with its `_citation`
field, and the `tools` array with the revealed tool where the registry puts it. `calls.py` refuses a capture that
is not "call 1, then call 1's messages plus the assistant message and its result".

A replay swaps the stub's assistant message for the model's real reply. It sends call 2 only when the reply is
exactly one `read_skill` of the expected skill. Production would run any other call, and the harness cannot.

## Prerequisites

- The eval substrates run, Elasticsearch-eval included: `make eval-infra-up` (from `/opt/seshat`).
- `POSTGRES_PASSWORD`, `NEO4J_PASSWORD` and `AGENT_OWNER_EMAIL` are in the environment (`gateway up` only).
- Export `AGENT_OWNER_NAME`, `AGENT_CONVERSATION_MAX_HISTORY_MESSAGES` and `AGENT_LOCATION_ENABLED` from
  `/opt/seshat/.env` before each `gateway up`. They shape the primary's prompt, and `gateway up` passes them
  through only when they are set. Without `AGENT_LOCATION_ENABLED`, the capture lacks `get_location`, which
  production lists (the first FRE-1597 capture, 2026-10-13).
- `AGENT_ELASTICSEARCH_URL=http://127.0.0.1:9202` and `AGENT_TEMPO_URL` (the preflight smoke and `loop run`).
- llama.cpp serves the production primary on `:8600` (the model steps only). Save `/props` of the llama-server
  behind the router and pass it with `--props-file`. Pass the launch arguments with `--server-args="..."`.

```bash
RUN=telemetry/evaluation/fre1597-routing-run/$(date +%Y%m%d-%H%M)
```

The run directory is git-ignored: the captures and rows hold full prompts.

## Steps (from the repo root of this worktree)

| # | Command | Calls a model |
|---|---------|---------------|
| 1 | `git worktree add <scratch>/pre-tc2 b02503fc`, then in it `uv run python -m scripts.eval.fre1537.gateway up --run-dir <abs $RUN>` | no |
| 2 | `uv run python -m scripts.eval.fre1597.prompt_size capture --run-dir $RUN --label B` | no (stub) |
| 3 | `uv run python -m scripts.eval.fre1537.gateway down`, then `git worktree remove <scratch>/pre-tc2` | no |
| 4 | `uv run python -m scripts.eval.fre1537.gateway up --run-dir $RUN` | no |
| 5 | `uv run python -m scripts.eval.fre1597.prompt_size capture --run-dir $RUN --label A` | no (stub) |
| 6 | `uv run python -m scripts.eval.fre1597.routing capture --run-dir $RUN` | no (stub) |
| 7 | `uv run python -m scripts.eval.fre1597.loop capture --run-dir $RUN` | no (stub) |
| 8 | `uv run python -m scripts.eval.fre1537.gateway down` | no |
| 9 | `uv run python -m scripts.eval.fre1597.preflight --run-dir $RUN` | no |
| 10 | **The owner's OK on the preflight.** Master opens the llama.cpp window and holds every seat's tests and evals. | |
| 11 | Dry run: `routing replay --run-dir $RUN-dry ... --limit 2 --draws 1` and `loop run --run-dir $RUN-dry ... --limit 1 --draws 1` (copy the captures first) | yes, a few calls |
| 12 | `prompt_size measure --run-dir $RUN <local args>` | yes, 3 calls |
| 13 | `routing replay --run-dir $RUN <local args>` | yes, 147 call 1 and at most 87 call 2 |
| 14 | `loop run --run-dir $RUN --provider local <local args>` | yes, at most 72 calls |
| 15 | `routing score --run-dir $RUN` and `loop score --run-dir $RUN` | no |

`<local args>` are `--llama-model <name> --quant <quant> --server-args="<arguments>" --props-file <file>`.
`loop run --provider cloud` measures the cloud primary with no bound (ADR AC-F2). It is a paid call, so it runs
only on the owner's word.

Every llama.cpp call goes through `common.RetryOnce`. A transport failure (a reset or another error without an
HTTP status, a 502, 503 or 504, or a stream cut before its `finish_reason`) is sent once more. The first failure is
kept in that call's `extra.retried`. An item fails only if the second attempt fails too. A request error (any
other status, for example 400 or 500) is not retried. A retried call's timings are those of the second attempt.

Each rerun of a capture, `replay` or `run` resumes. A run whose configuration differs from the recorded
fingerprint is refused.

## Scoring

- **AC-1 (`routing score`).** `first`: the first tool call of call 1 is `read_skill` of the expected skill, and
  no other skill loads. Each area skill needs `ceil(draws × 10 / 12)`: 8 of 9, 10 of 12, 13 of 15 (owner decision
  D-1). The Part 1 `right` count (FRE-1590's rule, which ignores the call order) is reported beside it. No area
  skill may load where another one was expected. No skill may load on the 60 no-skill draws. Every call must
  report the same llama.cpp build. A missing draw makes the verdict `incomplete`.
- **AC-2 (`loop score`).** At least 10 of 12 draws hold all five steps (`fre1594.score.loop_count`).
- **AC-3 (`prompt_size measure`).** `tokens(B) > tokens(B-minus)` and `tokens(B) - tokens(A) >= 0.8 ×
  (tokens(B) - tokens(B-minus))`, each count `cache_n + prompt_n` with `cache_n` 0. It also fails on a difference
  between `B` and `A` that is not a T-C2 change (`body_diff`, `unexpected`).
- **AC-4 (`loop score`).** The FRE-1594 cache verdict of the simple form, made `fail` by any lineage-void pair or
  a call without timings.

## Tests

`uv run pytest -q tests/test_eval/test_fre1597_*.py` runs offline: no gateway, no model, no Docker.
