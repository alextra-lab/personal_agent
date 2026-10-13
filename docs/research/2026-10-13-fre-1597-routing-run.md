# FRE-1597 — the routing run on Flash-Next (ADR-0156 A5, T-D1)

**Study:** FRE-1597, 2026-10-13. **Seat:** build1. **ADR:** ADR-0156 amendment of 2026-10-12 (FRE-1591): AC-F1,
AC-F2 and AC-F3 on the final build, and the routing part of AC-F4.
**Harness:** `scripts/eval/fre1597`. **Plan, with two Codex review rounds:**
`docs/superpowers/plans/2026-10-13-fre-1597-routing-run.md`.

This document records what was measured on the FRE-1596 build. Recommendations appear only in **Proposals**. The owner
decides. The results are a fresh measurement. They are not compared with the FRE-1590 numbers, because the scenarios
are rewritten.

---

## Answer

| Ticket AC | ADR AC | Verdict | Measured |
|---|---|---|---|
| AC-1 — the skills are chosen | AC-F4 routing | **FAIL** | 6 of 7 skills meet their bound. `log-search` is first on 9 of 15 draws (needs 13). 4 area confusions (bound 0). 0 of 60 no-skill loads (pass) |
| AC-2 — the revealed tool completes the loop | AC-F2 | **PASS** | 12 of 12 draws hold all five steps |
| AC-3 — the prompt is smaller | AC-F1 | **FAIL** | The drop is 1,142 tokens against a definition of 1,440: **79.3%**, bound 80%. No difference outside T-C2 |
| AC-4 — the cache cost is bounded | AC-F3 | **PASS** | One costly call per session, at the reveal. 24 pairs scored, 0 void |

Both failures have one cause in the skill catalog (F1, F3). The reveal mechanism itself holds (F4, F5).

---

## Method

### How call 2 is captured

The probe gateway of FRE-1537 runs the final build with a recording stub as its model. For each scenario, the stub
answers the first primary request with one `read_skill` call of the expected skill. The gateway then runs the real
`read_skill`, applies the real reveal and sends call 2, which the stub records. So call 2 is the production body.
Two facts made this necessary, and both were confirmed in the capture:

- Production adds a `_citation` field to every `read_skill` result (`[S2@…]`, a typed retrieval source,
  `grounding/source_registry.py:336`). Its value depends on the turn.
- Production lists a revealed tool at its place in the registry, between `fetch_url` and `create_linear_issue`. The
  FRE-1594 spike appended it at the end.

`scripts/eval/fre1597/calls.py` refuses a capture unless call 2 is call 1's messages plus the scripted call and its
result, with no other key changed, and the added tools are exactly the tools that the skill reveals. 53 of 53
captures passed.

### The four parts

| Part | Population | Calls |
|---|---|---|
| AC-1 routing | 29 scenarios of the 7 area skills + 20 no-skill prompts, × 3 draws = 147 draws | Call 1. Then call 2 only when call 1 is exactly one `read_skill` of the expected skill: call 1's messages, the model's real reply, the captured `read_skill` result and the captured call-2 tools. No tool runs |
| AC-2, AC-4 loop | The four FRE-1594 prompts (2 `log-search`, 2 `model-latency`) × 3 draws = 12 draws, each a treatment session and its control | The FRE-1594 session in the simple form, with the captured call-2 tools and `read_skill` result. The shim runs the production tool on the eval Elasticsearch and Tempo. The control keeps call 1's tools |
| AC-3 prompt size | One no-skill prompt (`ns01`) | `B` captured from a gateway built at `b02503fc` (before T-C2), `B-minus` (`B` without the `query_telemetry` definition), and `A` captured on the final build with its non-system messages aligned to `B`. Each sent once with `cache_prompt: false` and `max_tokens: 1` |

**Owner decisions (2026-10-13, before the run):**
- D-1: a skill without 4 scenarios needs the same share, rounded up: 8 of 9, 10 of 12, 13 of 15.
- D-2: AC-2 uses the four FRE-1594 prompts, because no `log-search` scenario of `evals.yaml` is a `search_logs` task.
- D-3: the second step runs on every skill.
- D-4: no cloud run.

**AC-1 counting rule.** A draw counts as `first` when the first tool call of call 1 is `read_skill` of the expected
skill and no other skill loads. FRE-1590's `right` count ignores the call order. It is reported beside `first`.

**Retries.** Every llama.cpp call retries a transport failure once (a reset, a 502, 503 or 504, or a cut stream).
No call was retried in the whole run.

### Pins

| Pin | Value |
|---|---|
| Model | `unsloth/qwen3.8-flash-next`, quant `IQ4_XS - 4.25 bpw`, MTP draft on (`--spec-type draft-mtp --spec-draft-n-max 2`) |
| llama.cpp build | `b11521-b42b7e6d3` on every call |
| Chat-template hash | `12827f24…aadce` (`/props` file sha256 `71d91c3e…`, equal to slm_server's live copy) |
| Server launch arguments | verbatim in `fingerprint.json` (byte-identical to FRE-1594): `--parallel 3 --kv-unified --cache-prompt`, no checkpoint or host-RAM cache flags |
| Sampling | temperature 1.0, top_p 0.95, top_k 20, min_p 0, presence 0, repetition 1.0, `max_tokens` 12288, thinking on |
| Gateway builds | `B`: `da413fad…` (`b02503fc`). Final: `77a386e4…` |
| Captured call 1 | system prompt `8c15b750…`, tools `a8b49903…` (23 tools, production's list without `query_telemetry`) |
| Scenarios | each id with the hash of its text, in `routing/fingerprint.json` and `loop/fingerprint-local.json` |
| Warm-up | one call before each part: the first item's call 1, its own nonce, `max_tokens` 1 |
| Harness commit | `7428889a` |

**Run times (UTC, 2026-10-13):** dry run 12:10–12:14 · AC-3 12:16 · routing 12:17–12:47 · loop 12:51–13:20. The
owner OKed the preflight at about 12:10. Master held every seat during the window.

---

## Findings

### F1 — AC-1 fails on `log-search`: two skills claim the monitor indices, and two scenarios skip the skill

**Verdict: AC-1 fail.**

| Skill | Draws | first | Need | Pass | right (FRE-1590 rule) |
|---|---|---|---|---|---|
| graph-inspection | 12 | 11 | 10 | yes | 11 |
| interaction-outcomes | 9 | 8 | 8 | yes | 8 |
| **log-search** | 15 | **9** | 13 | **no** | 9 |
| model-latency | 12 | 11 | 10 | yes | 11 |
| service-health | 15 | 13 | 13 | yes | 14 |
| spend-and-tokens | 12 | 12 | 10 | yes | 12 |
| turn-explanation | 12 | 11 | 10 | yes | 11 |

The six `log-search` misses:

| Scenario | Prompt | Draws | Call 1 |
|---|---|---|---|
| `log-search#2` | "What did the agent-monitors-cache-erosion monitor report most recently?" | 3 of 3 | `read_skill("service-health")` |
| `log-search#3` | "Is the agent-monitors-joinability index family daily or monthly? …" | 2 of 3 | `bash` with `curl …/_cat/indices`, no skill |
| `log-search#4` | "Use ES\|QL to count the agent-logs events of the last day by level." | 1 of 3 | `bash` with `curl …/_cat/indices` and `_field_caps`, no skill |

**The cause of `log-search#2`:** both catalog lines claim the monitor indices.

- `log-search`: "… over agent-logs, agent-topology **or the monitor indices**."
- `service-health`: "… and **reads the monitor indices** that record the health of the local model server and of the pipelines."

Neither line has a "Not for…" clause that gives the monitors to the other skill. The model chose `service-health`
on every draw. The scenario expects `log-search`, because the cache-erosion family is outside the tool's three
families (its date field is `run_at`).

**The other area confusion:** `model-latency#0` ("p50, p90 and max … per model for the last 30 days") loaded
`log-search` on 1 of 3 draws.

**No-skill prompts: 0 loads in 60 draws (pass).**

The other misses, for completeness: `graph-inspection#3` called `search_memory` once; `interaction-outcomes#1`
called `recall_personal_history` once; `service-health#1` called `search_memory` before `read_skill` once (right,
not first); `service-health#3` called `search_memory` once; `turn-explanation#2` called `bash` with a Tempo `curl`
once.

### F2 — the second step: after a revealing skill, the model mostly calls the revealed tool

**Verdict: reported, no bound.** Call 2 was sent on 74 draws (the others had another first reply).

| Skill | Call 2 sent | Called the revealed tool | First tool of call 2 |
|---|---|---|---|
| graph-inspection | 10 | — (reveals none) | `bash` 10 (the Cypher script) |
| interaction-outcomes | 8 | — (reveals none) | `bash` 8 |
| log-search | 9 | 2 | `read_skill` 6 (a reference file), `bash` 1, `read` 1, `search_logs` 1 |
| model-latency | 11 | 9 | `measure_model_latency` 8, `read_skill` 3 |
| service-health | 13 | 3 | `bash` 10 (the probe script), `search_logs` 3 |
| spend-and-tokens | 12 | 9 | `search_logs` 9, `bash` 3 |
| turn-explanation | 11 | 9 | `search_logs` 9, `bash` 2 |

The `log-search` and `service-health` scenarios test paths that need `curl` or the probe script, so a low count of
the revealed tool is the expected behaviour there.

### F3 — AC-3 fails by 0.7 points: the new catalog lines cost about three times the ADR's estimate

**Verdict: AC-3 fail.**

| Body | Tokens (`cache_n + prompt_n`, `cache_n` 0) | SHA-256 |
|---|---|---|
| B (before T-C2) | 12,384 | `edf8a9c4…` |
| B-minus (B without `query_telemetry`) | 10,944 | `761b39e6…` |
| A (final build) | 11,242 | `5bb76220…` |

- The definition is 1,440 tokens. The ADR estimated about 870 (3,472 characters at 4 characters per token).
- The drop is 1,142 tokens. 1,142 / 1,440 = 79.3%. The bound is 80%, so A needed to be at most 11,232 tokens: 10
  tokens less.
- `tokens(B) > tokens(B-minus)` holds.

**Where the 298 tokens went.** The diff between B and A holds no change outside T-C2. It holds three system-prompt
blocks and one tool block:

| Block | B characters | A characters |
|---|---|---|
| The skill catalog (10 lines → 11 lines) | 4,528 | 5,868 |
| The read-only tool line (`query_telemetry` removed) | 46 | 29 |
| `Available tools (24 total)` → `(23 total)` | 27 | 27 |
| `read_skill` definition (the skill names in its text) | 728 | 744 |

The catalog grew by 1,340 characters. The ADR assumed "about 100 tokens" for the catalog lines that A4 adds. The A4
lines with their "Not for…" clauses cost about 300.

### F4 — AC-2 passes: the revealed tool completes the loop on the final build

**Verdict: AC-2 pass, 12 of 12.** Every draw loaded the skill, called the revealed tool with valid arguments, ran it,
carried the result and got a usable reply.

In 4 of the 6 `log-search` draws, call 2 held two parallel `search_logs` calls. The shim ran both. The table shows the
first one, as in draw 0.

| Prompt | Revealed call | Call 3 ended with |
|---|---|---|
| `log-search#0` | `search_logs(action="count", index="agent-logs", query={"term": {"level": "ERROR"}}, since="1h")` | a further tool call |
| `log-search#1` | `search_logs(action="search", index="agent-logs", since="24h", query={"bool": …}, …)` | a further tool call |
| `model-latency#0` | `measure_model_latency(since="24h", role="primary", group_by=["model"])` | text |
| `model-latency#1` | `measure_model_latency(since="6h", group_by=["model"])` | text |

### F5 — AC-4 passes: one costly call per session, at the reveal

**Verdict: AC-4 pass.** 24 pairs scored, 0 void, 0 without reuse.

| Call | Treatment shortfall | Control shortfall |
|---|---|---|
| k = 2 (the reveal) | 11,254–11,273 in 12 of 12 sessions | 4 |
| k = 3 | 0 in 12 of 12 sessions | 4 |

The shortfall is `max(0, cache_n + prompt_n of the previous call - cache_n of this call)` (ADR AC-F3, second
amendment). The treatment's call 3 also reused 66–343 of its own generated tokens. This is the FRE-1594 pattern: the
tools change at the reveal, so the whole prompt is processed again once, and the session then continues from its
live state.

### F6 — the reveal was cheaper in the routing run than in the loop

**Verdict: an observation, not checked in the server log.** In the routing run, call 2 after a reveal reused a median
12,110 tokens and processed 2,603 (median 10.6 s for all call-2 sends). In the loop, the treatment's call 2 reused
nothing. The loop puts a nonce at the start of each session. The routing run does not, so consecutive items with the
same revealed tool share their prefix. The server may then restore a cached prompt of an earlier session. This might
mean that, in production, the re-processing cost of a reveal falls on the first session after a new tool set, not on
every session. The llama.cpp log at trace level (`-v`) can confirm it.

---

## Threats to validity

- **Call 2 after another first reply is not measured.** The routing run sends call 2 only after exactly one
  `read_skill` of the expected skill. Production would run any other call, and the harness cannot.
- **The loop's call 1 answers other calls with "not run",** as FRE-1594 did. Every loop call 1 held only the
  `read_skill` call.
- **The `_citation` value is from the capture turn.** It is sent as opaque prompt text, as that turn would have sent
  it.
- **The eval stores.** The shim's tools read the eval Elasticsearch and Tempo. Tempo had no span in the last hour at
  the preflight. The model-latency calls ran and returned results, and the AC-2 step 5 test is mechanical.
- **One no-skill prompt for AC-3.** The tool and catalog blocks are the same for every prompt, so the result depends
  on the prompt only through the shared tokens.
- **Co-tenants.** Master held every seat and slm_server for the window. The production gateway's SLM health probe may
  have run on other slots. It does not touch the scored cache numbers.

---

## Proposals (for the owner)

1. **Give the monitor indices to one skill.** Remove "or the monitor indices" from `log-search`, or remove the
   monitors from `service-health` and add a "Not for…" clause that names the other skill. The scenario
   `log-search#2` expects `log-search`, so the first change follows the scenario. Then run AC-1 again for the area.
2. **Look at `log-search#3` and `#4`.** The model went straight to `curl` for an index listing and an ES|QL count. The
   catalog line names ES|QL and index lookups, but the model did not load the skill on 3 of 6 draws. A shorter line
   with the trigger words first might help. This is a guess, not a measured cause.
3. **Decide AC-3 with the measured cost.** The drop is 79.3% against 80%. Three ways:
   - shorten the A4 catalog lines by about 10 tokens or more, and measure again;
   - accept the result and amend the 20% margin, because it was set on an estimate of about 100 tokens that the
     measure shows is about 300;
   - keep the bound and record AC-3 as failed.

---

## Data

Run directory (git-ignored): `telemetry/evaluation/fre1597-routing-run/20261013-1215/`:
`routing/rows.jsonl`, `routing/report.json`, `routing/fingerprint.json`, `loop/rows-local.jsonl`, `loop/report.json`,
`loop/fingerprint-local.json`, `prompt_size/report.json`, `preflight.json`, and the captures. Dry run:
`20261013-1215-dry`. Pins from master: `telemetry/evaluation/fre1597-routing-run/pins/`.

Score commands:

```bash
uv run python -m scripts.eval.fre1597.routing score --run-dir telemetry/evaluation/fre1597-routing-run/20261013-1215
uv run python -m scripts.eval.fre1597.loop score --run-dir telemetry/evaluation/fre1597-routing-run/20261013-1215
```

**References**
- ADR-0156 A4, A5, AC-F1–AC-F4 and the two AC-F3 amendments:
  `docs/architecture_decisions/ADR-0156-skills-and-tools-follow-the-industry-standard.md`
- The reveal spike (T-B): `docs/research/2026-10-12-fre-1594-reveal-spike.md`
- The FRE-1590 skill test: `docs/research/2026-10-12-fre-1590-skill-test-qwen-gemma.md`
- llama.cpp server README: https://github.com/ggml-org/llama.cpp/blob/master/tools/server/README.md
