# FRE-1590 — skill test on Flash-Next (Qwen) and Gemma: does each skill load when needed, and does it help?

**Study:** FRE-1590, 2026-10-11 to 2026-10-12. **Seat:** build1. **Method:** Anthropic `skill-creator`
adapted to Seshat. Each test case runs with the skills and without them, then the descriptions are tuned
from the findings. Owner decisions, all recorded on FRE-1590 before the run event they govern:

1. The full run starts only after the owner's OK on the preflight numbers (~20:00Z, "Ok. Yes.").
2. Grounding verification is off on the eval arms. Production runs `observe`, which blocks nothing.
3. Part 2 keeps 1 to 2 scenarios per skill, only where the skill holds knowledge that the tool does not
   have. It drops the scenarios whose recipes read production Elasticsearch or Tempo.
4. The eval arms get the production values of five settings. `AGENT_AGENT_ID` stays unset.
5. "Artifact building is a dedicated, isolated process": artifact-design leaves the skill catalog
   (PR #1263). Its results here are invalid for production.
6. Gemma runs as UD-Q8_K_XL with MTP draft depth 2, slm_server's configuration.
7. Part 2 on Gemma needs a new owner OK, based on Gemma's Part 1 result.
8. After Part 1 (~07:20Z on 2026-10-12): Part 2 runs on Gemma, and the D7 Flash-Next probe runs again on
   the 24-tool capture. The other answers are under **Proposals**.

This document records what was measured. Recommendations appear only in **Proposals**. The owner decides.

---

## Summary

| Part | Flash-Next (Qwen) | Gemma |
|---|---|---|
| Part 1: the right skill on the first call (39 scenarios × 3 draws) | 94/117 (80%) | 90/117 (77%) |
| Part 1: a wrong skill loaded | 1 | 0 |
| Part 1: a skill loaded on a no-skill prompt (20 × 3) | 0/60 | 0/60 |
| Part 2: does a skill help? (10 scenarios, two blind judges, points of 20) | with skills 13–14, without 11 (18 tools) | with skills 12 / 12, without 7 / 10 (24 tools) |
| D7 planner probe (ADR-0154), 24-tool capture | FAIL 12/13: expand-correct 42/48 (bound 43) | FAIL 11/13: decline-correct 24/30 |
| D7 planner probe, 18-tool capture (FRE-1581) | PASS 13/13 | — |
| Cloud cost | $1.29 (Part 2, Captain's Log only) | $1.02 (Part 2: Captain's Log $0.96, one builder call $0.06) |

---

## Scope as run

| Part | Where | Window (UTC) | Configuration |
|---|---|---|---|
| Preflight | stub gateway, llama.cpp, eval gateways | 10-11 19:29–19:45 | `main` `0ebdfc62` |
| Part 1 Flash-Next, first run (superseded) | stub gateway → llama.cpp | 10-11 20:13–20:42 | 18 tools, 11 skills |
| Part 2 Flash-Next | eval control and treatment gateways | 10-11 20:42–21:34 | 18 tools, 11 skills, `0ebdfc62` |
| Part 1 Flash-Next, rerun | stub gateway → llama.cpp | 10-12 05:06–05:29 | 24 tools, 10 skills, `main` `4bc88d2a` |
| Part 1 Gemma | llama.cpp, Gemma window | 10-12 05:37–05:51 | 24 tools, 10 skills |
| D7 Gemma | llama.cpp, Gemma window | 10-12 05:52–06:48 | 24 tools, planner prompt `ffe08113…` |
| D7 Flash-Next, 24-tool rerun | llama.cpp | 10-12 07:16–08:43 | 24 tools, planner prompt `ffe08113…` |
| Part 2 Gemma | eval control and treatment gateways, second Gemma window | 10-12 08:44–09:09 | 24 tools, 10 skills, `4bc88d2a` |

- **Part 1** saves each item's first primary request through the fre1537 stub gateway, once per model key.
  So each request carries that deployment's sampling. It then replays only that first call to llama.cpp,
  unchanged except for `model` and `stream`. Score: the skill that the first `read_skill` call loads.
  Code: `scripts/eval/fre1590/triggering.py`.
- **Part 2** runs each scenario as a full turn on two eval gateways at the production build fingerprint.
  - The control arm masks every `SKILL.md` with a read-only `/dev/null`. The catalog is empty, and the
    skill scripts stay.
  - The treatment arm has the skills.
  - Both arms have the same tools, consolidation off and a 5 s pause timeout. `envdiff.py` showed that
    the arms differ only in `HOSTNAME`.
  - Two blind judges scored each reply against the scenario's `expect` line. The replies had random
    labels and no tool lists. Code: `scripts/eval/fre1590/help.py`, `envdiff.py`,
    `docker-compose.fre1590.yml`.
- **D7 Gemma** uses the fre1537 probe. Gemma's catalog planner mode, `{enable_thinking: false}` with its
  default sampling, ran as `--mode thinking_off`, because `--mode planner` reads only Flash-Next's
  catalog entry. `catalog_planner_mode('gemma-4-26b-a4b')` returns the same parameters.

---

## Findings

### F1 — Part 1, Flash-Next (24 tools, 10 skills): 94/117 right, 1 wrong skill, 0/60 no-skill loads

**Query:** `triggering capture` (both model keys), then `replay --model qwen3.8-flash-next --llama-model
unsloth/qwen3.8-flash-next --quant UD-IQ4_XS --draws 3`, then `score`. Run dir
`telemetry/evaluation/fre1590-skill-test/p1v2-20261012-0506` (git-ignored). Before the replay, all 118
captured requests listed exactly production's 24 tool names.

| Skill | Right of 12 (9) | Misses (scenario: right of 3, what the first call did instead) |
|---|---|---|
| infrastructure-health | 10 | #2: 1/3, bash |
| mermaid-diagrams | 12 | — |
| neo4j-direct | 11 | #1: 2/3, search_memory |
| query-elasticsearch | 2 | #0, #1, #2: 0/3, `query_telemetry`. #3: 2/3, bash |
| query-tempo | 11 | #0: 2/3, `query_telemetry` |
| self-telemetry | 11 | #3: 2/3, no tool call |
| sequential-thinking | 3 of 9 | #1, #2: 0/3, search_memory and bash |
| system-diagnostics | 10 | #0: 2/3, bash. #1: 2/3, **system-metrics** (the only wrong skill) |
| system-metrics | 12 | — |
| turn-forensics | 12 | — |

177 calls, 0 errors, median 3.75 s, p90 13.7 s. llama.cpp `b11521-b42b7e6d3`.

### F2 — Part 1, Gemma: 90/117 right, 0 wrong skills, 0/60 no-skill loads

Same captures (`--model gemma-4-26b-a4b`) and command, with `--llama-model unsloth/gemma-4-26B-A4B-it
--quant UD-Q8_K_XL`. Thinking is on, as in the production primary request.

| Skill | Gemma | Flash-Next |
|---|---|---|
| infrastructure-health | 12 | 10 |
| mermaid-diagrams | 12 | 12 |
| neo4j-direct | 11 | 11 |
| query-elasticsearch | **1** | 2 |
| query-tempo | 8 | 11 |
| self-telemetry | 12 | 11 |
| sequential-thinking | **1 of 9** | 3 of 9 |
| system-diagnostics | 12 | 10 |
| system-metrics | 11 | 12 |
| turn-forensics | 10 | 12 |

Every Gemma miss is "no skill". On the first call, Gemma called:
- `query_telemetry` on the telemetry tasks (9 of 27 misses);
- `search_memory` on the reasoning tasks;
- bash once.

177 calls, 0 errors, median 4.6 s.

### F3 — Part 2, Flash-Next: with skills 13–14 points of 20, without 11

**Query:** `help run --model qwen3.8-flash-next`, then `help report`. Run dir
`telemetry/evaluation/fre1590-skill-test/full-20261011-2012`. The two artifact-design scenarios are left
out (see the validity notes).

| Scenario | Without skills (judge 1 / 2) | With skills (judge 1 / 2) | Time without / with |
|---|---|---|---|
| infrastructure-health#0 | FAIL / FAIL | **PASS / PASS** | 49 / 37 s |
| mermaid-diagrams#0 | PARTIAL / PARTIAL | PARTIAL / PARTIAL | 12 / 39 s |
| mermaid-diagrams#2 | PARTIAL / PARTIAL | PARTIAL / PARTIAL | 56 / 53 s |
| neo4j-direct#1 | FAIL / FAIL | **PARTIAL / PARTIAL** | 45 / 48 s |
| query-elasticsearch#3 | PARTIAL / PARTIAL | PARTIAL / PARTIAL (no skill loaded) | 27 / 51 s |
| self-telemetry#0 | PASS / PASS | PARTIAL / PASS | 59 / 23 s |
| self-telemetry#1 | PARTIAL / PARTIAL | **PASS / PASS** | 70 / 83 s |
| system-diagnostics#2 | PASS / PASS | PASS / PASS | 58 / 30 s |
| system-metrics#3 | PASS / PASS | PASS / PASS | 68 / 80 s |
| turn-forensics#1 | **PARTIAL / PARTIAL** | FAIL / FAIL | 168 / 162 s |

Points: PASS 2, PARTIAL 1, FAIL 0. The judges agreed on 19 of 20 verdicts. Part 2 ran with the 18-tool
list of 2026-10-11, before the R2 tools were added.

### F4 — D7 Gemma: FAIL, 11 of 13 thresholds

**Query:** `replay --mode thinking_off --tag gemma_planner --model unsloth/gemma-4-26B-A4B-it --quant
UD-Q8_K_XL --arms decide,paired`, then `longhist` with the same options, then `score --tag
gemma_planner`. Run dir `telemetry/evaluation/fre1537-planner-probe/fre1590-gemma-20261012-0530`.

| Check | Bound | Gemma |
|---|---|---|
| Decline-correct | ≥ 28/30 | **24/30 FAIL** |
| Expand-correct | ≥ 43/48 | 48/48 |
| Follow-ups decline / expand | ≥ 11/12 | 12/12, 12/12 |
| Declined call time (p50) | ≤ 2 s | 0.51 s |
| Planner call 60k, extended / cold | ≤ 10 s / ≤ 60 s | 3.8 s / 21.3 s |
| Paired added delay < 8k | ≤ 3 s, ≥ 28/30 counted | 0.76 s, **24/30 counted FAIL** |
| Paired added delay ≥ 8k | ≤ 10 s, ≥ 8/9 | 3.18 s, 9/9 |

The other checks pass. Both FAILs come from the same two decline fixtures, 0/3 each:
- `tool_logs`: a HYBRID plan with one task, for a single log query.
- `susan_3_thought_i_asked`: a research plan for a follow-up complaint.

The report is on FRE-1515.

### F5 — Part 2, Gemma: with skills 12 points of 20, without 7–10

**Query:** `help run --model gemma-4-26b-a4b`, then `help report`. Run dir
`telemetry/evaluation/fre1590-skill-test/p2-gemma-20261012-0844`. 10 scenarios × 2 arms, `RESULT:
PAIRED`, 0 error rows. Gemma UD-Q8_K_XL with MTP depth 2. The same two-judge blind method as F3.

| Scenario | Without skills (judge 1 / 2) | With skills (judge 1 / 2) | Time without / with |
|---|---|---|---|
| infrastructure-health#0 | FAIL / PARTIAL | PARTIAL / PARTIAL | 23 / 26 s |
| mermaid-diagrams#0 | PARTIAL / PASS | PARTIAL / PARTIAL | 59 / 17 s |
| mermaid-diagrams#2 | PARTIAL / PASS | PARTIAL / PASS (no skill loaded) | 14 / 22 s |
| neo4j-direct#1 | FAIL / FAIL | FAIL / FAIL | 75 / 73 s |
| query-elasticsearch#3 | FAIL / FAIL | FAIL / FAIL (no skill loaded) | 56 / 56 s |
| self-telemetry#0 | PARTIAL / PARTIAL | PASS / PARTIAL | 17 / 25 s |
| self-telemetry#1 | PARTIAL / PARTIAL | **PASS / PASS** | 82 / 29 s |
| system-diagnostics#2 | PASS / PASS | PASS / PASS | 21 / 15 s |
| system-metrics#3 | PARTIAL / PARTIAL | **PASS / PASS** | 52 / 29 s |
| turn-forensics#1 | FAIL / FAIL | **PARTIAL / PARTIAL** | 88 / 101 s |
| **Points of 20** | **7 / 10** | **12 / 12** | median 54 / 27 s |

- The judges agreed on 15 of 20 verdicts. Judge 2 counted a step that the reply does not show as met,
  and judge 1 counted it as missed. Both put the skill arm ahead.
- The gain from skills is +2 to +5 points on Gemma, and +2 to +3 on Flash-Next (F3). Gemma without skills
  scores lower: on system-metrics#3 it inferred that the GPU is busy from an SLM probe.
- With skills, Gemma's median turn is 27 s, against 54 s without.
- turn-forensics#1: the skill arm is ahead on Gemma, the opposite of Flash-Next.
- F3 ran with 18 tools and F5 with 24, so the two models also differ in tool list.

### F6 — D7 Flash-Next on the 24-tool capture: FAIL, 12 of 13 thresholds

**Query:** `replay --mode planner --arms decide,paired --quant UD-IQ4_XS`, then `longhist --mode
planner`, then `score --tag planner`. Run dir
`telemetry/evaluation/fre1537-planner-probe/fre1590-flash-next-24tools-20261012-0713`.

| Check | Bound | 24 tools (this run) | 18 tools (FRE-1581) |
|---|---|---|---|
| Decline-correct | ≥ 28/30 | 29/30 | 29/30 |
| **Expand-correct** | ≥ 43/48 | **42/48 FAIL** | 46/48 |
| Declined call time (p50) | ≤ 2 s | 0.69 s | 0.67 s |
| Planner call 60k, extended / cold | ≤ 10 s / ≤ 60 s | 6.5 s / 44.2 s | 6.7 s / 45.2 s |
| Paired added delay < 8k | ≤ 3 s, ≥ 28/30 | 0.79 s, 30/30 | 0.87 s, 30/30 |
| Paired added delay ≥ 8k | ≤ 10 s, ≥ 8/9 | 6.38 s, 9/9 | 6.54 s, 9/9 |

The other checks pass.
- The planner prompt (`ffe08113…`), the llama.cpp build and the sampling are the same as in the FRE-1581
  PASS. The only change is the six R2 tools in the captured primary.
- The six expand misses are all vague requests: `c2_noverb` 2/3, `c4_deliverable` 2/3, `c4_process`
  1/3, `c5_verb` 1/3. One decline draw expanded (`tool_logs` 1/3).
- Hypothesis, not tested: with an artifact tool and note tools in view, the planner more often judges
  that the primary can deliver a vague deliverable alone.
- 42/48 is one draw under the bound, and FRE-1581's 46/48 lies inside its 95% interval (75–94%). Whether
  the drop is stable is open: **a rerun of the decide arm waits for the owner.**
- **Shared llama.cpp time:** a live production artifact turn (master, owner OK) ran on the same engine
  from 07:27:08 to about 07:42Z.
  - The decide arm had ended at 07:25:23Z, so it is clean.
  - The 12 paired pairs whose span overlapped 07:27:00–07:42:00Z are kept in
    `rows/planner/paired.quarantine.jsonl` and were run again at 08:29–08:42Z:
    - draw 0: laptop_decline, lisbon_decline, susan_1–4, tariffs_decline, tool_logs;
    - draw 1: boiler_decline, greeting, laptop_decline, lisbon_decline.
  - The short paired median went from 1.04 s, with the 12 included, to 0.79 s.
  - The longhist arms (08:03–08:28Z) ran after the shared period.
  - The disturbed pairs did not cause the FAIL, which comes from the clean decide arm.

---

## Findings per skill, with cause and proposed change (AC-4)

| Skill | Finding | Cause | Proposed change |
|---|---|---|---|
| query-elasticsearch | Loads on 2/12 (Flash-Next) and 1/12 (Gemma). | The description says "Starts with the query_telemetry tool", and the tool description is a full instruction with near-copy examples. Scenarios #0–#2 test tasks that the tool does alone. | The owner decides how the skill and the tool divide the work (open since FRE-1581). Then rewrite #0–#2 to test what the tool cannot do: data that needs curl, index-family rules. |
| query-tempo | 11/12 Flash-Next, 8/12 Gemma (#0 0/3 on Gemma: `query_telemetry` latency). | The same overlap, for latency over a window. | The same decision. |
| sequential-thinking | 3/9 and 1/9. | Its rules are in the primary system prompt on every turn (FRE-1549), so a load adds nothing. | Take it out of the catalog, or keep it and score "no load" as right. The owner decides. |
| turn-forensics | Part 1 good (12/12, 10/12). Part 2: with the skill, FAIL. Without it, PARTIAL. | The skill names `turn_status.context_tokens` (`SKILL.md:118`), and the model asked `query_telemetry` for it. The tool refused. The skill's multi-step method then hit the 6-step pause, and a forced answer ended the turn before the method finished. The prompt also points to "the last turn", which a fresh session does not have. | State that `context_tokens` is not a queryable field. Shorten the method to the `input_tokens` ladder first. Give the scenario an explicit trace id. |
| self-telemetry | #0: the judges split. | The skill's hit-rate formula `cached / (input + cached)` (`SKILL.md:59`) counts the cache twice for llama.cpp, where `input_tokens` includes the cached tokens. Example: 10,438 prompt tokens with 10,434 cached. | Give the formula per provider: `cached / input` where the input includes the cache. |
| system-diagnostics / system-metrics | One draw of system-diagnostics#1 loaded system-metrics. | The descriptions overlap (FRE-1581 group A). | Sharpen the "Not for…" lines of both. |
| neo4j-direct | #1 2/3 and #3 2/3: `search_memory` instead. Part 2: the skill arm is better. | `search_memory` reads the same graph, so the model does not see that the skill's Cypher route gives other facts, such as `first_seen`. | The description names the facts that only Cypher reads (creation and last-seen times, counts, schema). |
| infrastructure-health | #2: 1/3 (bash). Part 2: the skill wins. | #2 is a one-command health check (group C in FRE-1581). | Rewrite #2 to need the probe script. |
| mermaid-diagrams | 12/12 and 12/12. Part 2: equal. | On eval, the validation script cannot run (see the validity notes), so its value was not measured. | Measure again where bash can run the script. |
| artifact-design | — | It is the builder's source document, and it is out of the catalog (PR #1263). | Done. |

---

## Validity notes

- **Part 1 (first run) and Part 2 used 18 tools, and production has 24.** `artifact_draft`,
  `artifact_write`, `artifact_list`, `artifact_read`, `notes_write` and `notes_search` register only when
  R2 is configured (`src/personal_agent/tools/__init__.py:129-138`). The eval stack had no R2 settings.
  - The Part 1 rerun and D7 Gemma used disabled R2 settings (`http://r2-disabled.invalid`), so the six
    tools register and any call fails at a host that never resolves. Their captures list production's
    24 tool names.
  - The R2 client is created only on a tool call. Tool-result compression, which would write to R2, is
    off in production.
  - The FRE-1581 D7 Flash-Next PASS used the 18-tool capture. Master takes a rerun to the owner.
- **The disabled R2 settings stop the write, not the Sonnet builder.** In F5, on
  `mermaid-diagrams#0` without skills, Gemma called `artifact_draft`. The `artifact_builder` (Claude
  Sonnet) ran and cost $0.06, then the write to `r2-disabled.invalid` failed, and Gemma answered
  inline. On `neo4j-direct#1` without skills, `artifact_list` and `notes_search` failed. In production,
  these calls succeed. So a turn that calls an R2 tool on eval does not take the production path.
- **The two Part 1 Flash-Next runs differ in two things:** the descriptions (PR #1262) and the tool list.
  So a change between them cannot be credited to one of them.
- **Part 2 understates the skill arm for skills with scripts.** On an eval deployment, bash runs as
  `nobody`, with an allowlisted environment and `HOME=/tmp` (FRE-1505,
  `src/personal_agent/tools/primitives/bash.py:65-74, 224, 338`). Production does not.
  - `probe_services.py` and `cypher.py` failed with "AGENT_DATABASE_URL / AGENT_NEO4J_PASSWORD is not
    set in this environment".
  - `validate_mermaid.py` could not find `chrome-headless-shell` under `/tmp/.cache/puppeteer`.
    Production has `mmdc` and `/usr/bin/chromium`.
- **The artifact-design results are invalid for production.** Production builds an artifact through
  `artifact_draft` and the Sonnet builder (32,768 output tokens). On eval, with no such tool, the
  primary wrote the page itself.
  - With the skill, it hit its 12,288-token limit in 3 of 3 samples. The calls that hit the limit held
    2,500–5,000 tokens of reasoning and one tool call that held the whole document.
  - Without the skill, it wrote a page in 86–94 s.
- **The Part 1 stub gateway has no memory graph.** The memory step of each captured request reads
  "records could not be reached". Production puts real memory there.
- **Cost.** Each completed eval turn still runs the Captain's Log reflection on Claude Sonnet ($0.055).
  No setting turns it off. The row's own cost misses it, because the event lands after the 20 s
  settle. The total is taken from ES per run: $1.29 for Part 2.
- **`graph_nodes_after` = 1 on every Part 2 turn.** It is the owner's `Person` node, created on each
  request, not a consolidation write.
- **The eval gateways log a trace-exporter error every few seconds.** No trace collector is set on eval,
  by design. These events have no trace id.

---

## Proposals

The owner answered the first proposals on 2026-10-12, at about 07:20Z (recorded on FRE-1590). Each item
shows its state.

- **The telemetry skills and `query_telemetry`** (decided). The owner follows master's recommendation, and
  goes further: the skill names (query-elasticsearch, query-tempo) name products, not objectives. That
  becomes its own ticket, filed by master. The data shows the cost of the overlap today:
  query-elasticsearch loads on 1–2 of 12.
- **The four skill-text fixes and sequential-thinking out of the catalog** (decided: a separate study).
  The owner wants a deeper study instead: a lighter system prompt against injected skills, and the
  effect on the prompt cache. Master files it. The fixes named in this note are input for that study,
  and none is applied here:
  - turn-forensics: `context_tokens`, and a shorter method;
  - self-telemetry: the formula;
  - neo4j-direct: name the facts that only Cypher reads;
  - system-diagnostics and system-metrics: the "Not for…" lines.
- **Part 2 on Gemma** (decided, done): F5.
- **D7 Flash-Next on the 24-tool capture** (decided, done): F6, FAIL 12/13. **Open with the owner:** a
  rerun of the decide arm, to show whether expand-correct 42/48 is stable.
- **Measure skills with scripts where bash can run them** (open). This is a security design question for
  the owner: give the eval bash child the script's own credential, or run that part with isolation off.
- **An eval stub for R2 writes** (open). The disabled R2 settings register the six tools, but a call to
  `artifact_draft` still runs the Sonnet builder before the write fails. A turn that reaches an R2 tool
  on eval therefore costs money and does not take the production path.

---

## References

- FRE-1590 (comments of 2026-10-11 and 2026-10-12), FRE-1581, FRE-1515, ADR-0156, ADR-0154 D7 and
  Amendment 1.
- [Anthropic, Skill authoring best practices](https://platform.claude.com/docs/en/agents-and-tools/agent-skills/best-practices).
- `scripts/eval/fre1590/README.md`, `scripts/eval/fre1537/README.md`.
- `docs/research/2026-10-11-fre-1581-adr-0156-gate-run.md`.
