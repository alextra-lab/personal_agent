# FRE-1594 — the reveal spike (ADR-0156 A5, T-B)

**Study:** FRE-1594, 2026-10-12. **Seat:** build2. **ADR:** ADR-0156 amendment A5 (A5.8, A5.9, AC-F2, AC-F3).
**Harness:** `scripts/eval/fre1594` (PR #1270). **Plan:** `docs/superpowers/plans/2026-10-12-fre-1594-reveal-spike.md`.

A skill-provided tool becomes visible to the primary only after `read_skill` loads the skill. The spike measured
two forms of that reveal:

- **cache-safe:** the `tools` array does not change. The tool's definition is in the `read_skill` result, as
  `provided_tools`.
- **simple:** the tool's definition is added to the `tools` array at the reveal.

This document records what was measured. Recommendations appear only in **Proposals**. The owner decides.

---

## Answer

**The simple form, for both dialects.** The cache-safe form fails AC-1 on both primaries, 0 of 12 each. W3 does not
apply: the simple form holds 12 of 12 on the local primary.

| | llama.cpp, Qwen3.8 Flash-Next UD-IQ4_XS | Anthropic, `claude_sonnet` (claude-sonnet-5) |
|---|---|---|
| AC-1 cache-safe | **0 / 12**, all fail at step 2: the revealed tool is never called | **0 / 12**, all fail at step 2: the tool is called, every argument is a string, the executor refuses it |
| AC-1 simple | **12 / 12** | **12 / 12** |
| AC-2 cache-safe (local) | pass: k = 2 shortfall 0 against control 4, in 12 of 12 pairs (moot, because AC-1 fails) | — |
| AC-2 simple (local) | pass: one costly call per session (k = 2, about 10.85k tokens re-processed), then 0 against 4 | — |
| AC-3 (cloud) | — | cache-safe reveal: no cache cost. Simple reveal: a full miss on the first session (about 16k tokens written), then mostly absorbed by the cross-session cache |
| Cost of the simple reveal | about 30 s of extra prefill, once per session (median 31.4 s against 1.4 s) | one cache write of the tools block per new tool set |

---

## Method

Each draw is a treatment session and its control. The control resends each treatment request with its own nonce and
without the reveal. Its replies are discarded, and its timings are kept.

| Call | Treatment | AC-1 step |
|---|---|---|
| 1 | The base request (the captured primary request of 2026-10-12 09:34, 23 tools, without `query_telemetry`, with the `log-search` and `model-latency` skills) and the prompt | 1: `read_skill` names the prompt's skill |
| 2 | The transcript plus the synthetic `read_skill` result, in the form's reveal | 2: the revealed tool is called with valid arguments. 3: it runs |
| 3 | The transcript plus the tool result | 4: the request carries the result. 5: HTTP 200, a parsed reply, text or a well-formed tool call |

- Population: 4 prompts (2 for each skill) × 3 draws = 12 draws for each form and provider.
- The revealed tools are `search_logs` and `measure_model_latency`, split from `query_telemetry`. The production
  `query_telemetry` executor runs them on the eval Elasticsearch (:9202) and Tempo, read only.
- Local: llama-server build `b11521-b42b7e6d3`, `--parallel 3 --kv-unified`, MTP draft on (`--spec-type draft-mtp
  --spec-draft-n-max 2`), no checkpoint or host-RAM cache flags (llama.cpp defaults). The fingerprint pins the
  launch arguments and the chat-template hash `12827f24…aadce`.
- Cloud: `claude_sonnet`, through the production `LiteLLMClient`. The cost gate writes only to the eval Postgres
  (:5434).

**AC-2 shortfall** (owner, 2026-10-12, after dry draw 1; ADR-0156 AC-F3 states the same rule):
`max(0, cache_n + prompt_n of the previous call - cache_n of this call)`. A pair is void if `cache_n` is above the
previous call's whole context (`cache_n + prompt_n + predicted_n`), or if the provider rejected a call.

Run times (UTC, 2026-10-12): cloud 16:18:13–16:23:49 · local cache-safe 16:22:11–16:39:33 · local simple
16:39:33–17:07:56. No call failed, and no pair was void.

---

## Findings

### F1 — cache-safe fails AC-1 on llama.cpp: the model cannot call a tool that is not in `tools`

**Verdict: AC-1 fail, 0 / 12.** Call 1 loaded the right skill in 12 of 12 draws. Call 2 never called the revealed
tool:

| Prompts | Call 2 called |
|---|---|
| `log-search#0`, `log-search#1` (6 draws) | `search_memory` 6 / 6 |
| `model-latency#0`, `model-latency#1` (6 draws) | `run_python` 2, `bash` 2, `recall_personal_history` 1, `read` 1 |

**Probable mechanism (not checked in the llama.cpp source):** llama.cpp constrains a tool call with a lazy grammar
built from the request's `tools`. The verbose server log shows `Grammar triggered on token 248058 (<tool_call>)` on
every tool call, including the cache-safe call 2 (dry draw 2, task 69). The data matches a grammar that admits only
declared names:

- `search_memory` is the only declared tool that starts with `search_`, and the log-search draws all ended there.
- No declared tool starts with `measure`, and the model-latency draws scattered across unrelated tools.

If this mechanism holds, no prompt or skill text can make the cache-safe form work on llama.cpp with tool grammar on.

### F2 — cache-safe fails AC-1 on Anthropic: the tool is called, but every argument is a string

**Verdict: AC-1 fail, 0 / 12.** In 12 of 12 draws, call 2 was a structured tool call to the revealed tool
(`search_logs` 6, `measure_model_latency` 6), with `finish_reason` `tool_calls`. Every argument value was a string,
including objects (`query`, `aggs`), a number (`size`) and an array (`group_by`). The executor refused each call
before it sent anything:

```
refused: a query node must be an object with exactly one operator
```

The tool's schema is in the `read_skill` result, not in `tools`, so the provider has no input schema to type the
arguments. I found no production step that turns a JSON string argument into an object (`executor.py` parses the
argument JSON, then the tool executor filters and validates it). So production refuses the same calls.

### F3 — the simple form holds the loop on both primaries

**Verdict: AC-1 pass, 12 / 12 on each.** On both providers, every draw loaded the skill, called the revealed tool
with valid arguments, ran it, carried the result and got a usable reply. Locally, call 3 ended with text in 4 draws
and with a further tool call in 8.

### F4 — the cache cost of the simple reveal on llama.cpp: one full re-processing per session

**Verdict: AC-2 pass.** At k = 2 the treatment re-processed the whole prompt (10839–10858 tokens) in 12 of 12
sessions, against a control shortfall of 4. At k = 3 the treatment lost 0 against 4. So each session has exactly
one costly call.

| Median, local | Treatment call 2 | Control call 2 |
|---|---|---|
| tokens processed (`prompt_n`) | 11826 | 336 |
| prefill time | 31.4 s | 1.4 s |

The tool block opens the token stream, so any change to `tools` invalidates the whole cache. This is inferred: the
first calls of two arms, which differ only in the nonce at the start of the system text, share about 78.6% of their
tokens (`f_sim` 0.786 in the server log). The local primary is a hybrid model (Gated DeltaNet). llama.cpp cannot cut
its recurrent state back to a shorter prefix, so the call re-processes the whole prompt. Each control call 2 and 3
restored the checkpoint 4 tokens before the end of its previous prompt (verbose server logs of dry draws 1–3).

### F5 — the cache cost of each reveal on Anthropic

**Verdict: reported, no bound (AC-3).** Cache tokens (`cache_read_input_tokens` / `cache_creation_input_tokens`) at
call 2, the reveal:

| Form | Treatment call 2 | Control call 2 |
|---|---|---|
| cache-safe | read about 30210, written 0, in 12 of 12 | read about 30210, written 0 (1 draw: read 23966, written 3122) |
| simple, first draw of each skill | read 0, written 16349 (log-search) or 15689 (model-latency) | read about 30212, written 0 |
| simple, later draws | read 26456 or 25132, written about 3122 | read about 30212, written 0 (1 draw: read 23966, written 3122) |

The cache-safe reveal costs no cache. The simple reveal misses the whole cache once, because the tools block opens
the prefix. The later draws reuse the revealed tool block that the first draw wrote. So in production, the cost falls
on the first session after a new tool set, and the cache lifetime decides how often it recurs.

### F6 — the local primary reuses its own generated tokens; a replayed control cannot

**Verdict: a measurement fact, folded into the rule.** A treatment call that followed its own reply continued from
the live end state, so `cache_n` passed the previous prompt (`generated_reuse` 58–308 tokens). A control call
carries the treatment's assistant message, not its own generation, so it restores a checkpoint. The first scoring
rule (the previous whole context as the baseline) charged the control for tokens it never had: in dry draw 1, the
treatment lost 1 and the control 108. The owner approved the prompt-only rule after that dry draw.

---

## Projections against the measured values

| Item | Projected (preflight) | Measured | Why |
|---|---|---|---|
| Cloud spend, both forms | $5.96 "worst case, no cache" | **$6.12** | A cache write costs 1.25× the base input rate, so a run with no cache is not the worst case. The run wrote about 16k cache tokens on each new prefix. The $10 cap held |
| Local time, cache-safe | 0.82 h (49 min) | **17 min** | Every draw stopped at step 2, so the third call of each session never ran |
| Local time, simple | 0.82 h (49 min) | **28 min** | The prefill projection used the whole prompt on each call. Calls 2 and 3 reuse the cache, except the treatment's call 2 |

---

## Defects found and fixed before the study

The dry draws (one prompt, one draw for each form, llama-server `--verbose`) found three harness defects. Each was
fixed, reviewed and tested before the full runs.

1. **The old skill names leaked.** The base request replaced only the two catalog lines. The `read_skill` enum and
   examples, two catalog cross-references and two `query_telemetry` mentions kept the old names. A control called
   `read_skill("query-elasticsearch")`. The base build now renames every old name and refuses a request that still
   names one.
2. **The whole-context baseline was biased** (F6).
3. **A cut stream counted as a successful call.** A stream that ended without a `finish_reason` was `ok`, and a cut
   reply with text could pass step 5. It now counts as a failed call.

**Dry draw 2 was cut by another session's tests (FRE-1599).** The router's watchdog restarted llama-server after
two malformed tool requests ("Failed to parse tools: Missing tool type") at 15:54:14Z and 15:55:05Z. They came from
another seat's `make test`: `test_the_long_history_step_runs_the_mode_that_prepare_resolved` left
`run_longhist_paired` real and reached the live server on :8600. Master reported 16 such restarts on 2026-10-12.
slm_server deployed a watchdog fix at 16:07Z (a malformed request now gets HTTP 400 and no restart), and PR #1273 adds
a conftest guard against port 8600. The simple dry draw was run again (dry draw 3) and was clean.

---

## Threats to validity

- **One prompt set.** Four prompts, two skills, three draws. A form that holds 0 / 12 or 12 / 12 is a clear signal,
  but other tools or argument shapes may behave differently. F2 depends on arguments that are objects or arrays. A
  tool with only string arguments may pass the cache-safe form on Anthropic.
- **One local model and one llama.cpp build.** F1 depends on the tool grammar of this build.
- **The reveal note** told the model where the tool is ("in `provided_tools` in this result" or "now in your tool
  list"). Other wording was not tested.
- **Cross-draw cache on Anthropic.** The arms and draws share the tools block before the nonce. The first-draw rows
  of F5 are the single-session cost.
- **A known co-tenant.** The production gateway's SLM health probe (every 300 s, 11-token requests, no generated
  token) ran on other slots during every window. It does not touch the spike's slot or the scored cache numbers. It
  can add decode time, which is not scored.

---

## Proposals (for the owner)

1. Use the simple form for A5 on both dialects. Accept one full re-processing per session on the local primary (about
   30 s at the measured prefill rate) and one cache write per new tool set on Anthropic.
2. If the cache-safe form is ever reconsidered, two conditions are necessary: a server that does not constrain tool
   names to `tools` (or a grammar that includes the revealed tools), and a step that types the arguments against the
   revealed schema. Neither exists today.

---

## Data

Run directory (git-ignored): `telemetry/evaluation/fre1594-reveal-spike/20261012-1610/` — `rows/local/*.jsonl`,
`rows/cloud/*.jsonl`, `rows/cloud/ledger.jsonl`, `fingerprint-local.json`, `fingerprint-cloud.json`,
`report.json`. Dry draws: `20261012-1500-dry`, `20261012-1550-dry`, `20261012-1610-dry`. Server-log extracts:
`props/dry-draw-server-log.txt`, `props/dry-draw-2-server-log.txt`, `props/dry-draw-3-server-log.txt`.

Score command: `uv run python -m scripts.eval.fre1594.spike score --run-dir telemetry/evaluation/fre1594-reveal-spike/20261012-1610`.

**References**
- llama.cpp server README: https://github.com/ggml-org/llama.cpp/blob/master/tools/server/README.md
- llama.cpp server source (checkpoints, restore, forced re-processing): https://github.com/ggml-org/llama.cpp/blob/master/tools/server/server-context.cpp
- ADR-0156 A5 and AC-F3: `docs/architecture_decisions/ADR-0156-skills-and-tools-follow-the-industry-standard.md`
