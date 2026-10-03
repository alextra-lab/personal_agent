# Planner probe (ADR-0154 D7)

The committed probe that qualifies a planner configuration. A deployment receives a `planner` mode in
`config/models.yaml` only after this probe passes on it. The thresholds are in `score.py` and mirror
ADR-0154 D7. Only an owner-approved amendment of the ADR changes one.

The probe measures design A: an isolated planner call, in a chosen planner mode. Designs B and C are rejected
in the ADR and are not here.

## Prerequisites

- The eval substrates run: `make eval-infra-up` (steps 1 to 4 only).
- `POSTGRES_PASSWORD`, `NEO4J_PASSWORD` and `AGENT_OWNER_EMAIL` are in the environment (steps 1 to 4 only).
  Steps 5 to 7 need no secret.
- The llama.cpp server answers on `http://127.0.0.1:8600` (steps 5 and 6 only).
- Docker. The probe never runs `docker compose`.

Set a run directory once. It is git-ignored, because the captured request bodies hold full prompts.

```bash
RUN=telemetry/evaluation/fre1537-planner-probe/$(date +%Y%m%d-%H%M)
```

## Steps (run from the repo root)

| # | Command | Writes | Calls a model |
|---|---------|--------|---------------|
| 1 | `uv run python -m scripts.eval.fre1537.gateway up --run-dir $RUN` | two containers | no |
| 2 | `uv run python -m scripts.eval.fre1537.capture --run-dir $RUN` | `$RUN/captured/*.json` | no (stub) |
| 3 | `uv run python -m scripts.eval.fre1537.gateway render --run-dir $RUN` | `$RUN/prompts.json` | no |
| 4 | `uv run python -m scripts.eval.fre1537.gateway down` | removes the two containers | no |
| 5 | `uv run python -m scripts.eval.fre1537.replay --run-dir $RUN --mode thinking_off` | `$RUN/rows/thinking_off/{decide,timing}.jsonl`, `fingerprint.json` | yes, about 100 calls |
| 6 | `uv run python -m scripts.eval.fre1537.longhist --run-dir $RUN --mode thinking_off` | `$RUN/rows/thinking_off/longhist.jsonl` | yes, 6 planner calls and 3 primary calls |
| 7 | `uv run python -m scripts.eval.fre1537.score --run-dir $RUN --tag thinking_off` | `report.txt`, `report.json` | no |

Steps 5 and 6 call the owner's llama.cpp. Ask the owner first.

`score` exits 1 when any threshold is FAIL. Post the report, with its fingerprint, on the ticket of the change
that adds the `planner` mode.

## What each step does

- **`gateway up`** builds the gateway image under its own tag, `fre1537-probe-gateway:<build fingerprint>`.
  It starts `fre1537-stub` and `fre1537-gateway` on the network of the eval substrates, with a label on both.
  The gateway uses eval stores only, primitive tools and prefer-primitives on (as production), and the stub as
  its model endpoint. The event bus is off, so a capture turn writes to no knowledge graph.
  It stops before it starts anything if an eval substrate is missing or unhealthy, if a container of the same
  name exists without the probe label, or if the substrates share more than one network. It checks that
  `cloud-sim-searxng` is the same container before and after.
- **`capture`** runs one `/chat` turn per fixture through that gateway. The 19 fixtures of
  `scripts/eval/fre1498/fixtures.yaml` run as one turn. The 8 follow-ups in `followups.yaml` run as two turns in
  one session. The primary's request body of the last turn is saved. It posts bare HTTP turns and not
  `IsolatedArmRunner` turns, because the gateway has no memory graph and no event bus, and its model is a stub.
  The gateway runs without the memory graph, so the captured primary prompt has no `## Operator` section and its
  memory line reads "records could not be reached". The planner request holds no primary prompt, so no D7
  threshold depends on this. The tools, the sampling and the planner prompts are the production ones. The
  fingerprint records the captured primary's tool count, tool-name hash and system prompt length, so a change
  shows. The 2026-10-03 capture of Appendix A had 16 tools and a 9,779-character system prompt.
  The eval gateway needs two cloud API key settings to boot. The probe passes an inert placeholder for both,
  because its model is a stub, so no cloud call can spend money.
- **`render`** builds the planner prompts with the production functions `_build_planner_system_prompt` and
  `_render_planner_history`, inside the gateway, where the settings are those of the image. The ADR-0152 decline
  rule is inserted, because production code does not carry it yet.
- **`replay`** sends the planner request of each fixture (3 draws each) and the timing arm to llama.cpp. It
  records the configuration fingerprint first. A rerun resumes. A tag refuses rows of a different configuration.
- **`longhist`** runs the long-history arm at 8,000, 30,000 and 60,000 characters: a cold planner call, a primary
  call, then the extended planner call.
- **`score`** checks every D7 threshold. See below.

## Planner modes

| Mode | Parameters | Use |
|------|------------|-----|
| `planner` | read from the `planner` mode of `qwen3.8-flash-next` in `config/models.yaml` | The mode that ships (ADR-0154 D4). Use it to qualify a catalog change. |
| `thinking_off` | `chat_template_kwargs.enable_thinking: false` | The D4 measurement configuration, fixed in the probe. |
| `server_default` | none | Thinking on, as production runs the planner today. It fails the reasoning-characters threshold. |

Sampling is that of the captured primary request. The `planner` mode refuses to load when the catalog gives
it a sampler that differs from the default mode's, because the probe cannot measure that. Pass `--mode planner`
to `replay` and `longhist`, and `--tag planner` to `score`. The fingerprint records the mode's parameters, so
a catalog change needs a new run.

The user message of every arm comes from the production `build_planner_user_message`, so the probe runs the
bound and the framing that ship.

## A managed deployment (ADR-0154 D7, FRE-1516)

The OVH `qwen3.8-27b-ovh` and `claude_sonnet` deployments qualify on the same probe. They are paid. Ask
the owner before the run, and set a cap.

Steps 1 to 4 (capture and render) are the same: they call no model and cost nothing. Replace steps 5 to 7
with the commands below. Run them one deployment at a time.

```bash
D=qwen3.8-27b-ovh        # or claude_sonnet
CAND='{"temperature": 1.0, "reasoning_effort": "none"}'   # claude_sonnet: '{"effort": "low"}'
# The credential of the provider, and the EVAL Postgres (port 5434). Never the production database.
export AGENT_MANAGED_EMBEDDING_TOKEN=...   # OVH   (claude_sonnet: AGENT_ANTHROPIC_API_KEY)
export AGENT_DATABASE_URL=postgresql+asyncpg://agent:...@127.0.0.1:5434/<database>
uv run python -m scripts.eval.fre1537.replay   --run-dir $RUN --deployment $D --mode planner --candidate "$CAND" --tag $D-planner --max-usd 5
uv run python -m scripts.eval.fre1537.longhist --run-dir $RUN --deployment $D --mode planner --candidate "$CAND" --tag $D-planner --max-usd 5
uv run python -m scripts.eval.fre1537.score    --run-dir $RUN --tag $D-planner
```

- `--candidate` is the `planner` mode of a deployment whose catalog has none yet. It is checked against the
  dialect of the deployment (ADR-0145), so a field that the catalog loader refuses is refused here.
- The request is the production one. The probe calls `LiteLLMClient.respond` the way the planner call does,
  so the dialect parameters, the provider's base URL and the Anthropic cache blocks are the gateway's.
  It never calls `litellm` directly (ADR-0141 AC-6).
- `respond` reserves and records cost through the `CostGate` (ADR-0065). The probe registers a real gate. It
  refuses to start unless `AGENT_DATABASE_URL` is the eval Postgres, so no cost row reaches production
  (FRE-375). `make eval-infra-up` starts that database.
- `--max-usd` is required. It caps the cost of every tag of the run directory together. A call is refused
  when the spent total plus its worst-case input cost reaches the cap. Output cost counts when the call
  returns. Every row records `cost_usd`.
- The timing arm and the primary call of the long-history arm prime the llama.cpp prefix cache and decide no
  threshold. A managed run skips both.
- The production cloud client reports `reasoning_trace: None` whatever the model did. The probe reads the
  reasoning evidence from the provider message instead. The reasoning threshold fails on any reasoning
  character, any thinking block (a redacted one too) and any reasoning token that the provider reports.
- A managed API reports no engine build and no quant. The fingerprint says `managed (provider reports no
  build)` and `managed (not reported)`. The model name that a reply reports goes to `engine.served_model`,
  and a change of it is a different configuration.
- To prove that the instrument sees reasoning, run `--mode default --labels <expand fixtures> --trials 1`
  under another tag. The default mode thinks, so the report must show reasoning on it.

**After a pass,** add the mode to `config/models.yaml`. Then repeat the `replay` command with the same tag,
`--mode planner` and no `--candidate`. The rows are complete, so it makes no call. It stops with "different
configuration" when the catalog differs from the run that passed.

## A digest run (ADR-0154 AC-8)

`replay --digest-file <file>` inserts the file's text after the history and before the query. The system prompt
does not change. The tag is `<mode>-digest` unless `--tag` is set, and the fingerprint records the digest hash.
The message framing and the total bound are production code (FRE-1541). The digest text is still the file's,
because production has no digest builder yet (ADR-0154 D5). When it has one, feed its output to the probe.

## The scorer

| Threshold | Rule |
|-----------|------|
| Decline-correct | at least 28 of 30 draws |
| Expand-correct | at least 43 of 48 draws |
| Each follow-up direction | at least 11 of 12 draws |
| Plans that fail to parse or validate | 0 |
| Reasoning characters, any call | 0 |
| Completion tokens, declined calls | p50 at most 40 |
| Declined call time, single-turn fixtures | p50 at most 2 s |
| Planner call, 60,000 characters, extended | at most 10 s |
| Planner call, 60,000 characters, cold | at most 60 s |
| Configuration fingerprint | engine build, model, quant, planner mode and prompt hash all known |

- A rate prints with its 95% Wilson interval. The report prints no pooled figure.
- A threshold with fewer draws than its denominator is FAIL. It is never scaled.
- A reply is a decline when the strategy is `SINGLE` with no task. It is an expansion when the strategy is
  `HYBRID` or `DECOMPOSE` with at least one task. Any other reply is invalid, counts as incorrect, and counts
  in the parse line. This is the validation rule of ADR-0154 D2.
- `c5_noverb` is not scored.
- The p50 is the value at index `round(0.5 * (n - 1))` of the sorted values (ADR-0154 Appendix A4).
- If llama.cpp does not report its build or quant, pass `--engine-build` and `--quant` to `replay`. An unknown
  value fails the fingerprint threshold.

## Tests

`uv run pytest tests/test_eval/test_fre1537_*.py -q` runs offline: no model, no Docker.
