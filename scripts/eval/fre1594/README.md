# Reveal spike (FRE-1594, ADR-0156 A5, T-B)

The spike answers two questions for ADR-0156 A5.8:

1. Does the model call a tool that is revealed after `read_skill`, and does the provider accept the transcript
   that follows? (AC-1, the full tool loop)
2. What does each reveal form cost in prompt cache? (AC-2 on the local primary, AC-3 on the cloud primary)

The two forms:

- **cache-safe:** the `tools` array does not change. The tool's definition is in the `read_skill` result, as
  `provided_tools`.
- **simple:** the tool's definition is added to the `tools` array at the reveal.

The plan, with the Codex review findings: `docs/superpowers/plans/2026-10-12-fre-1594-reveal-spike.md`.

## What a draw does

A draw is a **treatment** session, then its **control** session.

| Call | Treatment | AC-1 step |
|---|---|---|
| 1 | The base request: the captured primary request, without `query_telemetry`, with the `log-search` and `model-latency` catalog lines (`scenarios.yaml`) and the prompt. | 1: a `read_skill` call names the prompt's skill |
| 2 | The transcript plus the synthetic `read_skill` result, in the form's reveal. | 2: the revealed tool is called with valid arguments. 3: the shim runs it |
| 3 | The transcript plus the tool result. | 4: the request carries the result. 5: the reply is HTTP 200, parses, and holds text or a well-formed tool call |

The control runs only when step 1 held. It sends each treatment request again, with its own nonce and without
the reveal. Its replies are discarded and its timings are kept. A reply that stopped on its length bound fails
its step.

The shim runs the two experimental tools with the production `query_telemetry` code. It refuses a tool that is
not revealed yet, as A5.5 says. It reads the eval Elasticsearch only and refuses any other.

## Prerequisites

- A captured primary request (`--base`). Use a capture of the fre1537 probe gateway, for example
  `uv run python -m scripts.eval.fre1590.triggering capture` (see `scripts/eval/fre1590/README.md`). The
  capture must hold `query_telemetry`, and its system prompt must hold the `query-elasticsearch` and
  `query-tempo` catalog lines.
- `AGENT_ELASTICSEARCH_URL=http://127.0.0.1:9202` (the eval Elasticsearch, `make eval-infra-up`) and
  `AGENT_TEMPO_URL` set to an address of the Tempo container that this host can reach.
- **local:** llama.cpp serves the production primary on `--url` (default `http://127.0.0.1:8600`). The
  slm_server router on `:8600` refuses `/props` (403, 2026-10-12). Save `/props` of the llama-server behind it
  on the Mac and pass it with `--props-file`, so the fingerprint pins the chat-template hash and shows any chunk
  reuse setting.
- **cloud:** the deployment's API key, and `AGENT_DATABASE_URL` set to the eval Postgres (port 5434). The cost
  gate refuses any other database.

Set a run directory once. It is git-ignored, because the rows hold full requests and replies.

```bash
RUN=telemetry/evaluation/fre1594-reveal-spike/$(date +%Y%m%d-%H%M)
BASE=<path to a captured request .json>
```

## Steps (run from the repo root)

| # | Command | Calls a model |
|---|---------|---------------|
| 1 | `uv run python -m scripts.eval.fre1594.spike preflight --run-dir $RUN --base $BASE` | no |
| 2 | `uv run python -m scripts.eval.fre1594.spike run --run-dir $RUN-dry --base $BASE --provider local --form <form> --llama-model <name> --quant <quant> --limit 1 --draws 1`, once per form | yes, at most 7 local calls per form |
| 3 | `uv run python -m scripts.eval.fre1594.spike run --run-dir $RUN --base $BASE --provider local --form <form> --llama-model <name> --quant <quant>`, once per form | yes, at most 73 local calls per form |
| 4 | `uv run python -m scripts.eval.fre1594.spike run --run-dir $RUN --base $BASE --provider cloud --form <form>`, once per form | yes, at most 72 paid calls per form, under `--max-usd` (default 10) |
| 5 | `uv run python -m scripts.eval.fre1594.spike score --run-dir $RUN` | no |

**Steps 2 to 4 start only after the owner's OK on the preflight numbers.** Step 2 is the dry draw. It uses its
own run directory, because the fingerprint pins the draw count. Read its rows before step 3. Stop and report if
any pair has a lineage excess (`score` reports `pairs_void`), or if `/props` shows chunk reuse
(`cache_reuse` > 0).

A rerun of `run` resumes: it skips each (prompt, draw) that has a row. A run whose configuration differs from
the recorded `fingerprint-<provider>.json` is refused.

## Outputs

| File | What |
|---|---|
| `preflight.json` | The first-request token counts, the time and cost projection, `/props` and the shim smoke |
| `fingerprint-<provider>.json` | The pins of ADR-0156 A5: base, system prompt and tool hashes, prompt and skill hashes, sampling, model, quant, chat-template hash, deployment, draws, warm-up, git HEAD |
| `rows/<provider>/<form>.jsonl` | One row per draw: the five steps, the revealed call and its run, every call of both arms with its request hash, timings, usage and cost |
| `rows/local/warmup.jsonl` | The warm-up call of each local invocation |
| `report.json` | Per provider and form: the AC-1 count; the AC-2 verdict and pairs (local); the AC-3 cache table (cloud); `w3` |

## Scoring

- **AC-1:** the draws that hold all five steps. W3 applies if both local forms hold fewer than 10 of 12.
- **AC-2 (local):** the shortfall of a call is `(cache_n + prompt_n of the previous call) - cache_n of this
  call`. A call without timings fails the form (ADR AC-F3). A pair is void if a call's `cache_n` is larger than
  its previous call's prompt. cache-safe passes if the treatment's shortfall at call 2 is never above the
  control's. simple passes if no session has more than one call with a shortfall above the control's. A form
  with no non-void pair is `undecided`.
- **AC-3 (cloud):** `cache_read_input_tokens` and `cache_creation_input_tokens` of every call, with no bound.
