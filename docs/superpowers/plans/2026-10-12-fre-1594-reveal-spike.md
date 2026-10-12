# FRE-1594 — Reveal spike (ADR-0156 A5, T-B)

**Ticket:** FRE-1594 · **ADR:** ADR-0156, amendment of 2026-10-12 (A5.8, A5.9, AC-F2, AC-F3)
**Tier:** Standard (paid cloud calls, the input of an ADR decision). Codex plan review required.

## Question

A skill-provided tool is visible to the primary only after `read_skill` loads a skill that provides it.

1. Does the model call a tool that appears in the middle of a turn, and does the provider accept the
   transcript that follows? (AC-1)
2. What does each reveal form cost in prompt cache? (AC-2 local, AC-3 cloud)

## What already exists and is reused

| Need | Reused code |
|---|---|
| A production primary request (system prompt, skill catalog, `<turn_context>`, memory exchange, 24 tools, sampling) | A captured body from the fre1537 probe gateway (`scripts/eval/fre1537/capture.py`, `scripts/eval/fre1590/triggering.py capture`). The spike takes it as `--base <file>` and pins its hash. No gateway bring-up in the spike. |
| Nonce line of an arm | `scripts/eval/fre1537/replay.py:nonce_line` |
| Stream aggregation and tool-call parsing, as production does it for a local model, including the text fallback parser | `personal_agent.llm_client.adapters._aggregate_streaming_chunks`, `adapt_chat_completions_response`, `normalise_tool_call_indices`; `history_sanitiser.sanitise_messages` (the same calls as `litellm_client.py:1760-1805`) |
| Cloud call through the production client, with the cost gate on the eval Postgres only | `scripts/eval/fre1537/cloud.py`: `resolve_target`, `CloudSession` (eval-DB guard, pricing registration, `LiteLLMClient.respond`) |
| Tool result text in the D6 contract shape | `personal_agent.tools.contract.render_content` |
| The tool code that runs the call | `personal_agent.tools.telemetry_query.query_telemetry_executor` |

## Design

### Requests

**Base request `B(prompt)`** = the captured body with three changes:
1. `query_telemetry` is removed from `tools` (under W4 the primary does not hold it).
2. In the system prompt's `## Skills` catalog, the `query-elasticsearch` line becomes the `log-search` line and
   the `query-tempo` line becomes the `model-latency` line (A4 objectives and "Not for…" lines). The other
   catalog lines do not change.
3. The user text after `</turn_context>\n\n` becomes the prompt text.

The harness memory exchange (`memory_recall` call and result) stays, as production sends it.

**Experimental tools** (A5.1), in the wire format of the captured `query_telemetry` definition:
- `search_logs`: `action` (enum `search`, `count`), `index`, `query`, `aggs`, `fields`, `size`, `since`.
  Description: the Elasticsearch lines of the `query_telemetry` description.
- `measure_model_latency`: `since` (required), `span`, `group_by`, `role`, `model`, `limit`.
  Description: the latency lines.

Both descriptions are built from the allowlist constants of `telemetry_query.py`, so they match the tool.

**Synthetic `read_skill` result** = `render_content(output={"skill_name", "base_dir", "body", "files": []})`,
the production shape of `read_skill_executor`. The body is a short skill text from `scenarios.yaml`.

| Form | Treatment at the reveal | Control at the same step |
|---|---|---|
| cache-safe | `tools` unchanged. The result holds `provided_tools: [<definition>]`. | The same result without `provided_tools`. |
| simple | The definition is appended to `tools` from the reveal on. The result holds no definition. | `tools` unchanged. The same result. |

Each skill reveals only its own tool (`log-search` → `search_logs`, `model-latency` → `measure_model_latency`).

### One draw = a treatment session, then its control session

Treatment (production sampling, streamed, `max_tokens` as captured):
1. **Call 1** — `B(prompt)` with the arm nonce. Step 1 holds if a `read_skill` call names the expected skill.
   Other calls in the same response get a D6 failure result (`not_run_in_spike`). A call to the revealed tool
   in the same response as its `read_skill` is refused with the A5.5 D6 refusal (not revealed yet).
2. **Call 2** — the transcript plus the assistant message (built as `executor.py:6436-6451` builds it,
   `reasoning_content` included) and the synthetic result, in the form's reveal. Step 2 holds if a call to the
   revealed tool parses as a JSON object and the tool does not refuse it (`TelemetryQueryError` code `refused`).
   Step 3 holds if the shim runs it and it returns a result, not a `store_*` error.
3. **Call 3** — the transcript plus the tool result. Step 4 holds if this request carries the result under the
   call's id. Step 5 holds if the reply is HTTP 200, parses, and holds non-empty text or a well-formed tool call.

The session stops after call 3. A step that fails ends the session. The revealed call must come in call 2.
A reply with `finish_reason == "length"` fails its step: production drops its content and calls and runs none
of them (`executor.py:6411-6429`, Codex finding 4).

The assistant message uses the production `_build_assistant_tool_calls` (round-prefixed call ids) and keeps
`reasoning_content`. A test asserts the full wire form of the assistant and tool messages (Codex finding 5).

Control: runs only if step 1 held. It sends the treatment's requests again, call by call, with its own nonce,
and only the reveal left out. Its replies are discarded. Its timings are kept.

Every request is passed through `sanitise_messages` and `normalise_tool_call_indices`, as the client does.
Every row records the SHA-256 of the exact request body.

### AC-2 measure (local only)

`shortfall(call k) = (cache_n + prompt_n + predicted_n of call k-1) - cache_n of call k`, for k = 2, 3, in each
session. It is the part of the previous call's whole context that call k did not reuse.

- **No timings fails the form** (ADR AC-F3: "*Fails if* … a call has no timings"). The ticket text says "voids";
  the ADR is the stricter rule and wins. The note reports both readings.
- **Lineage:** a pair (treatment call k, control call k) is void if either call's `cache_n` is larger than
  `cache_n + prompt_n + predicted_n` of its previous call. Each pair records that excess, and
  `generated_reuse` (the reused tokens past the previous prompt) for each arm.
- **No reuse is not a result:** a pair in which neither call reused a token is `no_reuse` and is not scored.
  The treatment then equals the control, which proves nothing about the reveal.
- **No vacuous verdict:** a form with no scored pair gets `uninformative` (a `no_reuse` pair exists) or
  `undecided`, never `pass`.
- cache-safe passes if, in every scored pair at k = 2, treatment shortfall ≤ control shortfall.
- simple passes if each session has at most one k with treatment shortfall > control shortfall.

**Change to the ADR lineage rule (owner, 2026-10-12).** The ADR voids a pair when `cache_n` is above the
previous call's prompt. The llama.cpp server README and `tools/server/server-context.cpp` show why that rule
fails on the local primary:

- Flash-Next is a hybrid model (Gated DeltaNet, `qwen4exp`). llama.cpp cannot cut a recurrent state back to a
  shorter common prefix.
- The server continues from the live end state of the slot, which holds the generated tokens. If the new
  prompt does not extend that state, it restores a context checkpoint. Checkpoints are made at the last user
  message and a few tokens before the prompt end (`--ctx-checkpoints` 32, `--checkpoint-min-step` 8192). If no
  checkpoint fits, it re-processes the whole prompt.
- Reuse of generated tokens is therefore the normal path, and the ADR rule voids it. The baseline above
  includes `predicted_n`. Treatment and control use the same rule, so the comparison stays fair.

Other cache settings: chunk reuse (`--cache-reuse`, default 0) can reuse tokens after a mismatch. The host-RAM
prompt cache (`--cache-ram`, default 8192 MiB, with `--cache-idle-slots`) can restore a saved prompt, so
`cache_n` does not always come from the previous call. The nonce opens the system message, so the arms share
no useful prefix. The fingerprint pins every cache key of `GET /props` and the llama-server launch arguments
(`--server-args`). The dry draw runs with llama-server `-v`, because the checkpoint decisions log at trace
level. If chunk reuse is on, or the dry draw shows a lineage excess, an `uninformative` form or a forced
re-processing on a control call, the run stops before the full population and the seat reports to the owner.

### AC-3 measure (cloud)

`cache_read_input_tokens` and `cache_creation_input_tokens` of every call, and the AC-1 count, for each form.
Controls run on the cloud too, so the cache figures have a reference. No bound.

### The shim (authorization + execution)

`run_tool(name, arguments, revealed)`:
- a name that is not in `revealed` → D6 refusal (`not_revealed`), lists the providing skill. Not executed.
- `search_logs(**a)` → `query_telemetry_executor(action=a.action, ...)`; `measure_model_latency(**a)` →
  `query_telemetry_executor(action="latency", ...)`.
- It refuses to start unless `settings.elasticsearch_url` is the eval Elasticsearch (port 9202). Tempo is the one
  Tempo, read only (the eval gateways read it too: `docker-compose.eval.yml:273`). The run passes its address.

### Pinning (`fingerprint.json`, AC-F pinning list)

Base body SHA-256, system prompt SHA-256, tools SHA-256 and names, the SHA-256 of each prompt text and skill
body, the llama.cpp model name and quant (arguments), the llama.cpp build (`system_fingerprint` of the replies),
the chat-template SHA-256 and the cache keys (`GET /props`), the llama-server launch arguments, the sampling keys of the base body, the cloud deployment key, model id
and mode, draws, the warm-up procedure, and the git HEAD. A rerun with a different fingerprint is refused.

**Warm-up:** one request before the first draw: the base body of prompt 1 with nonce `warmup`, `max_tokens` 1.
Its row is kept and marked `warmup`.

### Cost and safety

- Local: no money. It uses the owner's GPU, so it runs only in a window that master provides.
- Cloud: `--max-usd` cap (default 10). Before each call, the spike adds the cost of every row of its own run
  directory and a reservation for the call: the full request (`len / 3` tokens at the input rate) plus the
  call's `max_tokens` at the output rate. If the sum reaches the cap, the run stops (Codex finding 3: the
  fre1537 `Budget` scans other files and counts only the system and user text). The cost gate writes only to
  the eval Postgres (`assert_eval_database`).
- `preflight` makes no model call. It builds every request of the first call, prints their token estimates and
  the time and cost projection, reads `GET /props`, and runs one `search_logs` count and one
  `measure_model_latency` call through the shim.
- The run starts only after the owner's OK on the preflight numbers. Master provides the llama.cpp window, the
  eval stack and the cloud credentials.

## Files

| Path | What |
|---|---|
| `scripts/eval/fre1594/README.md` | Prerequisites, steps, what each step writes, which steps call a model |
| `scripts/eval/fre1594/scenarios.yaml` | 2 skills (catalog line, replaced line, body, tool) and 4 prompts |
| `scripts/eval/fre1594/reveal.py` | Pure request building: base, experimental tools, skill result, reveal and control transforms, assistant message |
| `scripts/eval/fre1594/shim.py` | Authorization and execution of the two tools |
| `scripts/eval/fre1594/transport.py` | `LlamaTransport` (stream, timings, production adapters), `CloudTransport` (production client, budget) |
| `scripts/eval/fre1594/session.py` | One draw: treatment then control; the five steps |
| `scripts/eval/fre1594/score.py` | AC-1 counts, AC-2 pairs and verdict, AC-3 table |
| `scripts/eval/fre1594/spike.py` | CLI: `preflight`, `run --provider local\|cloud --form cache_safe\|simple`, `score` |
| `tests/scripts/test_fre1594_reveal_spike.py` | Unit tests, no network |
| `.gitignore` | `telemetry/evaluation/fre1594-reveal-spike/` (bodies hold full prompts) |
| `docs/research/2026-10-1x-fre-1594-reveal-spike.md` | The research note, after the run |

## Steps (TDD)

1. Tests for `reveal.py` → fail → implement. Verify: `make test-file FILE=tests/scripts/test_fre1594_reveal_spike.py`.
   - `B(prompt)` has no `query_telemetry`, has the two new catalog lines, the prompt text, and is otherwise equal.
   - The two tools hold exactly their argument sets.
   - For each form, the treatment and control requests at each step are equal except for the nonce and the reveal.
2. Tests for `shim.py` (executor mocked): not revealed → refused, not executed; argument mapping; `refused` →
   step 2 false; `store_unavailable` → step 3 false.
3. Tests for `transport.py` with `httpx.MockTransport`: SSE with structured tool calls; SSE with a `<tool_call>`
   text reply recovered by the production adapter; timings taken from the last chunk; HTTP 400 → step 5 false.
4. Tests for `session.py` with a scripted transport: a full draw holds all five steps; each step's failure ends the
   session at that step; the control replays the treatment requests.
5. Tests for `score.py`: shortfall values; void on missing timings and on the lineage rule; cache-safe and simple
   verdicts; AC-1 counts per form and provider.
6. `spike.py` and README. `preflight` against the captured base, offline except `GET /props` and the shim smoke.
7. Gates: `make test`, `make mypy`, `make ruff-check`, `make ruff-format`, `pre-commit run --all-files`.
8. Self-review (code-reviewer agent), commit, push, PR. Post the preflight numbers on the ticket for master and the
   owner. **Stop.** The run waits for the window, the credentials and the owner's OK.
9. After the OK: dry draw (1 prompt × 1 draw, each form, local, llama-server `-v`). If any pair is void, a form is
   `uninformative`, or the server log shows a forced re-processing on a control call, stop and report before the
   full run. Then the full run, score, and the research note on the same PR.

## Acceptance criteria → evidence

| AC | Evidence |
|---|---|
| AC-1 | `report.json`: per provider and form, draws holding all five steps out of 12, and the failing step of each other draw. W4 fails if both forms are below 10 of 12 on the local primary; then the note names W3. |
| AC-2 | `report.json`: per local form, the shortfall pairs at k = 2 and 3, the void pairs and their reason, and the verdict against the bound. |
| AC-3 | `report.json`: per cloud form, `cache_read_input_tokens` per call and the AC-1 count. |
| Pinning | `fingerprint.json` with every item of the ADR list. |
| Deliverable | The research note names the form for each dialect (llama.cpp Qwen, the cloud dialect), or W3. |

## Choices for the owner (in the preflight)

- **Cloud primary:** `claude_sonnet` (Anthropic dialect) is recommended: it is the selectable cloud primary, and its
  cache is a prefix cache with explicit breakpoints, the case where the reveal form matters (S12).
- **Base capture:** the capture of 2026-10-12 09:34 (24 tools, git HEAD of FRE-1515) or a fresh capture at run time.

## Dry draw of 2026-10-12 (15:08:27Z to 15:13:19Z)

One prompt (`log-search#0`) and one draw per form, with llama-server `--verbose`. The run stopped before the full
population, because the dry draw showed two harness defects.

- **Defect 1, fixed:** the base body replaced only the two catalog lines. The `read_skill` enum and examples, two
  catalog cross-references and two `query_telemetry` mentions kept the old names. The cache-safe control called
  `read_skill("query-elasticsearch")`. `base_body` now renames every old skill name, applies `system_edits`
  (`scenarios.yaml`), and refuses a body that still names an old skill or `query_telemetry`.
- **Defect 2, waits for the owner:** the control resends the treatment's requests, so its call k holds the
  treatment's assistant message, not its own generation. The treatment can continue from its live end state, the
  control cannot. The whole-context baseline charges the control for that (cache-safe k = 2: treatment 1, control
  108). The proposed rule is `max(0, cache_n + prompt_n of the previous call - cache_n)`.
- **Recorded:** the rows now keep the MTP counts `draft_n` and `draft_n_accepted`. They are not scored.
- **Server log** (`--verbose`): every scored control call (2 and 3) restored the checkpoint 4 tokens before the end
  of its previous prompt. Each treatment call that followed its own generation continued from the live state. The
  log shows "forcing full prompt re-processing" only on calls with `cache_n` 0: the call-1s, a warm-up and the
  simple treatment call 2. The stop condition did not hold.
- **Template order:** the first calls of the two arms share about 78.6% of their tokens. The Qwen3.8 template
  renders the tool block before the system text, so the nonce does not open the token stream. Cross-arm reuse
  needs a checkpoint inside that shared prefix. None existed (the first checkpoint was at about 8814 tokens).
  Calls 1 are not scored.
- **Known co-tenant:** the production gateway's SLM health probe (`slm_health_probe_interval_seconds` = 300 s,
  `brainstem/scheduler.py:218`) sent three 11-token requests on slots 0 and 2 during the window (tasks 512, 567,
  568). They generate nothing and do not touch the spike's slot, so the scored cache numbers are not affected.
  They can add a little decode time, which is not scored. Expect about 20 probe rounds in a 1.64 h local run.
  The probe is not paused, because that changes production. The research note records it.

## Risks

- **Hybrid model cache.** The local primary reuses its cache only from the live end state or a checkpoint (see
  "Change to the ADR lineage rule"). If the template does not re-render the generated tokens exactly, both arms
  fall back to a checkpoint or to a full re-processing. A form where neither arm reuses a token is
  `uninformative`, not a pass. The dry draw shows this before the full run.
- **Routing confound.** Step 1 needs the model to choose the skill. The four prompts are plain requests for the two
  objectives, and `query_telemetry` is absent, so `bash` with `curl` is the only other path. A draw that takes it
  fails step 1, as it would in production.
- **The probe gateway has no memory graph**, so the system prompt has no `## Operator` section. The fre1537 README
  records this. It does not change the reveal.
