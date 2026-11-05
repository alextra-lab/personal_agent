# FRE-1727 — Decision models such as Jev, and whether they have a place in Seshat

> **Ticket:** [FRE-1727](https://linear.app/frenchforest/issue/FRE-1727) · **Date:** 2026-11-05 · **Seat:** explore · **Paid cost:** $0 · **Mac used:** no · **Commission:** the owner, 2026-11-02, through master

## Question

The owner, verbatim: "I want a session that re-explores using the now-popular decision models like JEV and if there is a place for them in our agent." The owner then set two more instructions:

- "Earlier work" means the testing and research on deterministic gate checks and on the use of a model for routing.
- Look forward: find how other agents use these models, and find a path to consider for a test.

The owner also said that the feedback loop is off, and told this seat to find which loops are off.

A decision model is like a multiple-choice test taker, not an essay writer. It takes state and typed questions, and it returns a choice or a score with a probability. It writes no text.

## Result

**One decision in Seshat has the right shape and a real defect: expand or decline. The evidence for a decision model there is weak.** The case does not rest on cost or speed. The base Laya checkpoints are near chance on their own typed-decision benchmark, and an earlier small router failed on this exact decision (F12, E3). The other candidates have no possible test today, or no defect, as the AC-2 table shows.

**The feedback loops are off at the start of the chain. The chain also has no shipped outcome in the data that exists** (F4 to F9). A decision model does not fix this.

The one recommendation is at the end of this document.

## Decision models examined (AC-1)

| Model | What it is | Primary source and date | Status in this study |
|---|---|---|---|
| Jev (TypeSafe) | Hosted model. Choice, Score and Noul questions (Noul is a value from 0 to 1). No text output. | [TypeSafe launch post](https://typesafe.ai/blog/introducing-system-one-models-and-jev), byline 2026-09-15, page metadata 2026-10-28 | Examined from vendor sources. Not run. |
| Laya (Convai Innovations) | Apache-2.0 local model with the same API shape. 421M and 322M parameters. | [Repository](https://github.com/NandhaKishorM/laya), created 2026-09-18. PyPI 0.1.0 on 2026-09-18, current 0.4.24. | Examined from its README. Not run. |
| Arch-Router | A small router that this project tested on the planner fixtures. | ADR-0152, 2026-09-14 | Tested earlier. Failed (F12). |
| RouteLLM, CARGO, vLLM Semantic Router | Learned routers that pick a model with a threshold. | `docs/research/2026-07-16-model-routing-sota-survey.md` | Survey only. Not examined again. |
| JEPA | A method that learns representations and predicts in a latent space. It is not a decision model. | LeCun, "A Path Towards Autonomous Machine Intelligence", OpenReview, 2022-06-27 | Out of scope. It was master's guess for "JEV" before the owner gave the link. |

## External evidence

The figures in this section are not independent unless the item says so. The seat read E1, E2 and E4 through a page summarizer. It read E3 and E5 directly from their READMEs through the GitHub API.

**E1 — Jev, vendor evaluation** ([evals.typesafe.ai](https://evals.typesafe.ai)). Mean agreement over four workflows, with cost and time per case:

| Model | Mean agreement | Cost per case | Time per case |
|---|---|---|---|
| Sol | 74.1% | $0.0836 | 23.3 s |
| Opus 5 | 73.1% | $0.1761 | 37.8 s |
| Terra | 67.9% | $0.0304 | 10.1 s |
| Sonnet 5 | 67.8% | about $0.117 | about 78 s |
| Jev | 67.8% | $0.0004 | 0.4 s |

The reference is GPT-6 Astra and Fable 5.1, not ground truth. The page gives no sample size or variance. FRE-1640 showed that an LLM judge can prefer its own vendor (Sol preferred OpenAI models 120 of 120), so this reference has a known risk.

**E2 — Jev, independent use.** [arXiv 2609.24052](https://arxiv.org/abs/2609.24052) (Rafe and Das, 2026-09-21) coded crash narratives and reports F1 0.908 against 2,416 blinded human judgments. This is one narrow extraction task. The seat found no independent reproduction of the vendor's evaluation.

**E3 — Laya, from its own README.** The README says: "The base checkpoints are near chance on typed-decisions zero-shot -- 0.362 and 0.352 against a 0.318 random baseline and a 0.461 majority-class baseline." The 0.766 figure comes from a checkpoint tuned on that benchmark's own training split. The README calls Laya "a fast base to specialise, not a zero-shot decision engine". Other limits that apply here:

- Negated requests can fail. One multilingual answer gave the wrong option a probability of 0.9998.
- Labels such as `yes` and `no` are unsafe in choice questions.
- The default English context is 512 tokens, which limits the history that a follow-up can send.
- Narrower tasks score higher: English XNLI 0.860 and English MASSIVE intent 0.783. These are not our decision.

**E4 — How other agents use these models.** Three positions recur:

- before the model call (routing)
- around a tool call (risk)
- after a tool call (checking the result)

The uses that this seat found:

- LangChain ships `ModelRouterMiddleware` and `AutoModeMiddleware` as a pre-release ([docs](https://docs.langchain.com/oss/python/integrations/providers/typesafe)).
- [awesome-jev-tools](https://github.com/v-modal/awesome-jev-tools) lists model routers, approval gates, compaction tools and browser loops. Most entries give no measurement, and the list states that inclusion verifies nothing.
- TypeSafe says that Jev does not replace the LLM in a coding agent ([docs](https://docs.typesafe.ai/introduction/coding-agents)).

**E5 — The best public test of a gate.** [jev-engineering](https://github.com/eugeniughelbur/jev-engineering), run of 2026-09-20. Regex rules and an allowlist run first, then one Jev call. Dangerous commands that passed: 0 of 30 under a blunt injection, 3 of 30, 1 of 30 and 1 of 30 under false approval claims. A confidence floor that caught all attacks escalated 58% of normal traffic. The author says that the gate "catches mistakes, not a determined attacker".

**E6 — Data handling.** The TypeSafe pages that this seat read give no retention statement. The LangChain docs warn about secrets in tool arguments and conversation state.

**E7 — Laya status.** The FRE-1727 comment of 2026-11-05 records that `slm_server` searched the Mac by name and found no test and no download. A search of the VPS and Linear on 2026-11-02 also found none. Deleted sessions and the contents of weight files are not covered.

## Findings

All Postgres queries ran on 2026-11-05 at about 12:13Z with `docker exec cloud-sim-postgres psql -U agent -d personal_agent`. Elasticsearch (ES) counts are provisional (FRE-1051).

### F1 — The planner made 36 recorded decisions since the field started

**Verdict:** POSITIVE

```sql
SELECT planner_decision, count(*) n, min(created_at)::date first_day, max(created_at)::date last_day
FROM route_traces WHERE planner_decision IS NOT NULL GROUP BY 1 ORDER BY 1;
```
```
 declined | 16 | 2026-10-13 | 2026-11-05
 expanded | 20 | 2026-10-13 | 2026-11-02
```

Each row has its own trace. The first row is from 2026-10-13, so the window is 24 days. It is not 30 days. Master's count of 34 was correct at that time. Two more declines came after it.

### F2 — A declined planner call is a small part of a turn

**Verdict:** POSITIVE

```sql
SELECT planner_decision, planner_deployment, count(*) n,
  round(avg(planner_duration_ms)::numeric/1000,1) avg_s,
  round((percentile_cont(0.5) WITHIN GROUP (ORDER BY planner_duration_ms))::numeric/1000,1) p50_s,
  round(avg(latency_total_ms)::numeric/1000) turn_avg_s
FROM route_traces WHERE planner_decision IS NOT NULL GROUP BY 1,2 ORDER BY n DESC;

SELECT round(avg(planner_duration_ms/NULLIF(latency_total_ms,0))::numeric*100,1) pct_of_turn, count(*) n
FROM route_traces WHERE planner_decision='declined' AND planner_duration_ms IS NOT NULL AND latency_total_ms>0;
```
```
 declined | qwen3.8-flash-next           | 14 |  5.2 |  4.8 | 150
 expanded | qwen3.8-flash-next           | 10 | 11.9 | 10.7 | 697
 expanded | glm-5.3-scaleway             |  4 |  4.9 |  3.4 |  58
 expanded | gpt-6.1-sol                  |  2 | 50.9 | 50.9 | 413
 expanded | deepseek-v4.1-flash-scaleway |  2 |  8.8 |  8.8 | 220
 expanded | claude_sonnet                |  2 |  7.8 |  7.8 |  27
 declined | glm-5.3-scaleway             |  2 |  4.8 |  4.8 |  19

 pct_of_turn 9.3 | n 16
```

Only 24 of the 36 decisions ran on the local model. The expanded and declined turn times are different tasks, so they are not a comparison.

### F3 — All recorded planner decisions carry the strategy `hybrid`, and 29 rows have no planner mode

**Verdict:** POSITIVE

```sql
SELECT coalesce(planner_decision,'(none)') planner_decision, decomposition_strategy,
  coalesce(planner_gate_reason,'(none)') gate, count(*) n
FROM route_traces WHERE created_at >= '2026-10-13' AND orchestration_event IS NOT NULL
GROUP BY 1,2,3 ORDER BY n DESC LIMIT 14;
```
```
 (none)   |        | (none)              | 38
 (none)   | single | planner_mode_absent | 25
 declined | hybrid | (none)              | 16
 expanded | hybrid | (none)              | 16
 (none)   | single | (none)              |  5
 expanded | hybrid | planner_mode_absent |  4
```

The code frames the planner message as `Strategy: {strategy}` (`expansion_controller.py::_frame_planner_query`). A declined decision also carries `hybrid`, which agrees with the FRE-1695 report. I did not read the 29 `planner_mode_absent` rows further.

### F4 — The live flags of the feedback chain

**Verdict:** POSITIVE

Read from the running process (`docker exec cloud-sim-seshat-gateway`, `/app/.venv/bin/python`, `personal_agent.config.settings`):

```
captains_log_reflection_enabled     False   (default True)
insights_enabled                    False   (default True)
insights_wiring_enabled             False   (default True)
feedback_polling_enabled            False   (default True)
context_quality_governance_enabled  False   (default False)
grounding_verification_mode         off     (default off)
promotion_pipeline_enabled          True
outcome_ingestion_enabled           True
graph_quality_governance_enabled    True
```

The recorded origin of the first four is the cost halt of 2026-07-26 (FRE-990, FRE-1293). FRE-1293 links insights to that halt and lists feedback polling as "Cost halt; no decision". FRE-1611 records that Streams 2 and 4 stay off by the owner's decision. I did not confirm which flags those stream numbers name.

### F5 — No reflection exists after 2026-10-23

**Verdict:** NEGATIVE, at the scope below

```
GET agent-captains-reflections-*/_count  {"query":{"range":{"timestamp":{"gte":"2026-10-24"}}}}
{"count":0}
```

- **Arm 1 (raw instance, same store):** `{'entry_id': 'CL-20261023-130818-52d7391f-001', 'timestamp': '2026-10-23T13:08:18.233826Z', 'type': 'reflection'}`
- **Arm 2 (same query, earlier bound only):** `{"range":{"timestamp":{"gte":"2026-10-01"}}}` returned `{"count":36}`. The index holds 1,648 documents in total.
- **Arm 3 (scope):** every monthly reflection index, on the `timestamp` field, with no other filter. FRE-990 set the flag to false on 2026-10-24.

### F6 — The insights analysis stopped on 2026-10-13, not at the July halt

**Verdict:** NEGATIVE, at the scope below

```
GET agent-insights-*/_count  {"query":{"range":{"timestamp":{"gte":"2026-10-17"}}}}
{"count":0}
```

- **Arm 1 (raw instance, same store):** `{'timestamp': '2026-10-13T23:50:52.935989+00:00', 'insight_type': 'missing_skill'}`
- **Arm 2 (same query, earlier bound only):** `{"range":{"timestamp":{"gte":"2026-10-01"}}}` returned `{"count":241}`.
- **Arm 3 (scope):** every monthly insights index, on the `timestamp` field, with no other filter.

FRE-1611 records why the flag did not stop the analysis earlier. The flag gated only the scheduler start, so the analysis ran on each consolidation event.

### F7 — Promotion filed 3 tickets, and its input stopped

**Verdict:** POSITIVE

```
GET agent-captains-funnel-events-*/_search  size 1000, sorted by @timestamp, summed in Python
rows 131  first 2026-09-08T00:07  last 2026-11-05T11:11
Counter({'throttled_budget': 80, 'promotion_run': 51})
eligible 3  filed 3  linked 0
filed days Counter({'2026-10-24': 1, '2026-11-02': 1})
runs since 11-03: 2   eligible since 11-03: 0

Linear, label pipeline-filed: FRE-1693 (2026-10-24), FRE-1725 (2026-11-02), FRE-1726 (2026-11-02)
```

All three tickets came from reflections before the stop, and all three are in Needs Approval. An entry needs 7 days and 3 sightings (FRE-1611). No new reflection means no new input.

### F8 — Outcome ingestion runs daily, and every recorded outcome is noise

**Verdict:** POSITIVE for the counts, NEGATIVE for `ticket_outcome_at`

```
GET agent-logs-*/_search  query_string "outcome_ingestion_completed"
total 108  first 2026-08-01  last 2026-11-05T08:00:53Z
latest row: {'event_type': 'outcome_ingestion_completed', 'scanned': 3, 'ingested': 0}

GET agent-captains-reflections-*/_search  exists ticket_outcome
Counter({'canceled-as-noise': 9})  reflection dates 2026-05-27 to 2026-09-06

GET agent-captains-reflections-*/_count  exists linear_issue_id
{"count":16}

GET agent-captains-reflections-*/_count  exists ticket_outcome_at
{"count":0}
```

Arms for the `ticket_outcome_at` count:

- **Arm 1 (live producer):** `_STAMP_OUTCOME_SCRIPT` in `brainstem/jobs/outcome_ingestion.py` writes this field and `ticket_outcome` together. The job runs: the latest row above is from today.
- **Arm 2 (same query, identifier varied):** `exists ticket_outcome` returned 9.
- **Arm 3 (scope):** every monthly reflection index, with no other filter.

The result is 16 reflections with a ticket, 9 outcomes, and 0 outcomes marked `shipped`. FRE-1610 fixed a defect where an owner cancel counted as noise, so some of the 9 may be mislabeled.

Two other statements disagree with this finding. The Grafana panel text says that the outcome bar stays at 0 while feedback polling is false. FRE-1611 says that the graph holds no outcomes. The scheduler gates the two jobs with separate flags (`brainstem/scheduler.py`). I did not query the graph.

### F9 — `analyze_feedback_patterns` has no caller in the deployed code

**Verdict:** NEGATIVE, at the scope below

```
docker exec cloud-sim-seshat-gateway: cd /app/src; grep -rn "analyze_feedback_patterns" --include=*.py .
./personal_agent/insights/engine.py:918:    async def analyze_feedback_patterns(self, days: int = 30) -> list[
```

- **Arm 1 (raw instance, same store):** the definition above. The container file hash equals `origin/main` (`baf39f53623cd6a5`).
- **Arm 2 (same command, identifier varied):** `generate_reflection_entry` returned 6 lines, including the call at `orchestrator/executor.py:4817`.
- **Arm 3 (scope):** `.py` files under `/app/src` in the gateway container, by name. A dynamic lookup by a built string is not covered.

### F10 — Grounding verification is off, and span extraction has no calls since 2026-10-18

**Verdict:** NEGATIVE, at the scope below

```sql
SELECT count(*) span_since_oct19 FROM api_costs WHERE purpose='span_extraction' AND timestamp>='2026-10-19';
```
```
 0
```

- **Arm 1 (raw instance, same store):** `2026-10-18 | anthropic | anthropic/claude-sonnet-5 | span_extraction`
- **Arm 2 (same query, identifier varied):** `purpose='main_inference'` returned 218.
- **Arm 3 (scope):** production `api_costs`, all models, from 2026-10-19.

`grounding_compliance_observations` holds 293 rows, and the last is also from 2026-10-18. The live mode is `off`, and the setting says "off: nothing runs". `grounding_entailment_sample` has 4 rows in `agent-logs-*`, the last on 2026-10-09. **UNVERIFIABLE:** when the mode changed, and who changed it. I found no record that names this change.

### F11 — `api_costs.purpose='captains_log'` is not a count of reflections

**Verdict:** POSITIVE

```sql
SELECT model, count(*) n, round(sum(cost_usd),2) usd, min(timestamp)::date first_day, max(timestamp)::date last_day
FROM api_costs WHERE purpose='captains_log' AND timestamp>now()-interval '30 days' GROUP BY 1 ORDER BY usd DESC;
```
```
 anthropic/claude-sonnet-5 | 190 | 8.38 | 2026-10-09 | 2026-11-05
 openai/gpt-6.1-sol        |   8 | 0.09 | 2026-10-21 | 2026-10-24
```

Rows continue after the reflection flag went false. `cost_gate/role_map.py` bills `session_summary` to this budget role, and `session_summary` binds to Sonnet 5. The 198 rows compare with 36 reflections since 2026-10-01 (F5). The purpose column therefore cannot give the cost of reflection, which may be much lower than this total. The full cloud spend in 30 days is $24.67 over 2,643 calls.

### F12 — The planner's decision is noisy, and two cheap fixes already failed

**Verdict:** POSITIVE (read from the record, with no new draws)

| Source | Measured |
|---|---|
| FRE-1695 | Prompt `74d78c3a` declined correctly on 70 of 80 draws on b11751 (88%). The D7 floor is 28 of 30 (93%). |
| FRE-1694 | The same prompt on the same engine declined 41 of 50 (82%) in one run and 29 of 30 (97%) in another. The study reports a spread of about 15 points, with the floor "close to the center of the noise". |
| ADR-0154 | The planner samples at temperature 1.0. `config/models.yaml` confirms this for the `planner` mode. |
| ADR-0152 | "A lower planner temperature did not help either": at 0.6, agreement fell to 89%. Temperature 0 does not appear in ADR-0152 or ADR-0154. |
| ADR-0152 | "A dedicated router in front of the planner does not work with the model tested. Arch-Router answers in 0.2 s and declines 39 of 57 draws, including most research questions." |
| FRE-1498 | The regex stage labeled 10 of 12 research questions "conversational". The planner declined 7 of 7 command, greeting, tool and follow-up requests, and expanded 12 of 12 research questions. |
| FRE-1615 | Stage 4 labeled 24 of 24 tasks `simple`. The deterministic pre-gate was rejected, because it had no signal. On 6 expanded tasks, the planner took 2.3 times the wall clock of the forced single path. Judge points were equal (67 and 67 of 72) on the 18 valid tasks where the two configurations differ. |

Master reports that production moved to llama.cpp b12138 with the Unsloth v3.1 UD-IQ4_XS on 2026-11-05. All planner figures above describe other builds. The spread in FRE-1694 means that the drop to 88% may not be an engine effect at all.

### F13 — The labeled data is small

**Verdict:** POSITIVE

- `scripts/eval/fre1537/fixtures.py`: 27 fixtures (19 single-turn from FRE-1498 and 8 follow-ups). 26 are scored, 10 decline and 16 expand (FRE-1694).
- FRE-1498 raw rows on the VPS, outside the repo: 400.
- FRE-1615: 24 tasks with expect lines. I did not find its git-ignored rows under the main tree, so I did not count them.
- `route_traces`: 1,496 rows, 36 with a planner decision, and none with a correctness label.

The FRE-1498 fixtures are a subset of the D7 fixtures, so the labeled total is 27, not 46. Laya's tuned figure used a benchmark of 2,000 decisions.

### F14 — Production limits for a local model

**Verdict:** POSITIVE

```
docker inspect cloud-sim-seshat-gateway --format '{{.HostConfig.Memory}} {{.HostConfig.NanoCpus}}'
2147483648 1000000000
nproc: 8     free -g: 22 total, 15 used
```

The gateway has 2 GiB and 1 CPU. The 421M checkpoint needs about 1.7 GB at 32 bits (arithmetic, not a measurement), so it cannot share the gateway. The README gives 192 to 453 ms on an Intel Mac Pro CPU with 28 threads, for four questions. **UNVERIFIABLE:** latency on the VPS CPU, and free memory on the Mac.

## Which feedback loops are off

| Stage | State | Evidence |
|---|---|---|
| Captain's Log reflection | **Off** since 2026-10-24, near off since 2026-07-26 | F4, F5 |
| Insights analysis and wiring | **Off**. The analysis ran until 2026-10-13. | F4, F6 |
| Feedback polling | **Off** | F4 |
| Pattern analysis of feedback | **Not wired**, with any flag value | F9 |
| Context quality governance | **Off** by default | F4 |
| Grounding verification | **Off**, with no calls since 2026-10-18 | F4, F10 |
| Promotion to tickets | On, and its input stopped | F7 |
| Outcome ingestion | On. It scans 3 tickets and has no `shipped` outcome. | F8 |
| Graph quality monitor | On | F4 |

Memory consolidation also runs: `consolidation_attempts` holds 5,465 rows, with 6 in the last 7 days.

**The chain did not close, even when it was on.** ADR-0127 counted 1,957 reflections and 7 linked tickets in July. Today the index holds 1,648 reflections, 16 with a ticket, 9 with an outcome, and 0 with a `shipped` outcome (F8).

## Candidate decisions (AC-2)

| Decision | File and function | Input data today | Measurement of a benefit | Result |
|---|---|---|---|---|
| Expand or decline | `orchestrator/expansion_controller.py::_run_planner`, `_classify_planner_answer`, `request_gateway/decomposition.py::assess_decomposition` | 27 fixtures, 400 raw rows, 36 live decisions (F1, F13) | Decline-correct and expand-correct on the D7 fixtures against the planner on the current build, plus the same answer on reworded requests | Right shape, real defect, weak evidence (F12, E3) |
| Stage 4 label | `request_gateway/intent.py::classify_intent` | 1,496 rows, no correctness label | Agreement with the 27 fixtures | Same test as the row above. It drives context assembly, not expansion. |
| Reflect or skip | `orchestrator/executor.py`, `captains_log/reflection.py::generate_reflection_entry` | 1,648 reflections, 16 with a ticket, 0 `shipped` (F8) | Share of reflections that reach a shipped ticket | **Not testable.** No positive label exists. |
| Keep or drop a tool result | `orchestrator/within_session_compression.py::needs_hard_compression`, `compress_in_place` | FRE-1667: the primary request peaked at 58,707 tokens, and 2 of 122 worker runs stopped on the context limit | Tokens, cache reuse and reply quality on replayed turns | No label source. It can discard the prompt cache. |
| Citation verdict | `grounding/entailment.py::EntailmentVerdict`, `parse_judgement` | 413 span calls, 293 compliance rows, mode `off` (F10) | Agreement with a labeled set of claims | **No live decision to change.** The mode is off. |
| Tool approval | `tools/executor.py::_request_approval` | Not counted. My query on two guessed event names returned 0, so I make no claim from it. | False allows under false approval claims | **Rejected** on E5. It fails where a false approval is the attack. |
| Skill routing | `tools/read_skill.py::read_skill_executor` | FRE-1603: 147 draws | Area confusions | **Rejected.** FRE-1603 measured 0 area confusions and 0 of 60 false loads, so no defect exists. |

## Master's feedback, and my answers

Master sent six points on 2026-11-05. Two of them show gaps in my report of 2026-11-02.

**1. The benefit is small: 34 planner decisions in 30 days.** **Agree.** The count is now 36 in 24 days (F1). A declined call costs about 5 s (F2), and all cloud spend is $24.67 in 30 days. The larger cost is a wrong expansion (F12). I have no count of wrong expansions in production, because the live rows carry no label (F13).

**2. FRE-1695 is cheaper and comes first.** **Agree.** F3 agrees with the `Strategy: HYBRID` report. FRE-1694 already shows a variant that declined `tool_logs` 5 of 5, against 3 of 5 for the shipped prompt. Five draws per fixture cannot prove that gap. The spread of one prompt between runs is about 15 points. A single before-and-after figure across b11751 and b12138 therefore cannot separate an engine effect from noise.

**3. The test may fail or leak: 0.362 base and 0.766 tuned against a 93% floor.** **Agree, and the case is stronger than master states.**

On 2026-11-02 I told the owner that these models come pretrained, which understated the problem. The README calls the base checkpoints "near chance" on that benchmark, below its majority-class baseline (E3). Tuning is not realistic either: 27 labeled fixtures against 2,000 decisions (F13). Any tuning on those fixtures leaks, and 27 fixtures leave little to hold out. One limit on this point: the benchmark is a set of four other workflows, and the README shows higher scores on narrower tasks. Our decision has two options. Only a direct try can give a figure for it.

**4. New planner draws use the owner's Mac and need a hold on seat tests.** **Partly.** This is true for planner draws. A direct try of Laya itself needs no llama.cpp, but it still needs a machine. The hold applies if that machine is the Mac during a measurement window. The planner baseline must come from the current build. The draws from FRE-1695 can supply it, so the accepted order removes the need for separate draws.

**5. A pass does not say where Laya runs in production.** **Agree.** The accepted order makes this the first question of step 3. The known limits are in F14. The gateway container cannot hold it. A separate container on the VPS adds load, and the project has a rule against criteria that load the VPS. On the Mac, a cloud-primary turn gains a dependency on the Mac.

**6. This adds a classifier, and FRE-1498 showed the planner beating one.** **Partly on the citation, and I agree with the conclusion.**

The classifier in FRE-1498 was a regex ladder, not a learned model, so FRE-1498 alone does not settle it. But ADR-0152 did test a small learned router on this decision, and Arch-Router declined 39 of 57 draws (F12). My report of 2026-11-02 did not cite that result, and it is the closest earlier work. It is one model on an earlier fixture set, not a verdict on the class. A classifier also adds a second decider, and the planner must still run on every expansion to write the plan.

### Corrections to this seat's report of 2026-11-02

| I said | The measurement shows |
|---|---|
| 3 tickets filed "all today" | One on 2026-10-24 and two on 2026-11-02 (F7) |
| Outcome ingestion is "probably empty" | It runs daily, with 108 recorded completions and 9 outcomes (F8) |
| Insights are off from the July halt | The analysis ran until 2026-10-13 (F6) |
| `api_costs` shows no entailment calls | Entailment bills to the `entity_extraction` budget role (`role_map.py`), so that column cannot show it. The mode `off` is the current reason that nothing runs (F10). |
| These models "come pretrained", so little data is needed | The README says that the base checkpoints are not a zero-shot engine (E3) |
| Pinned weights give stability | This remains true for the same text, but a stable wrong answer has no value, and rewording is untested |

A search summary also called Laya an earlier model from 2025. The repository and PyPI show a first release on 2026-09-18, three days after Jev.

## Method appendix

**Stores:** production Postgres (`route_traces`, `api_costs`, `consolidation_attempts`, `grounding_compliance_observations`), production ES (`agent-logs-*`, `agent-captains-reflections-*`, `agent-captains-funnel-events-*`, `agent-insights-*`), the running gateway process, and Linear. No write to any of them.

**Windows:** as each finding states. No live gateway turn, no paid call, and no use of the Mac. This study ran no decision model.

**Identifier resolutions:**

- A term query on `event_type.keyword` in `agent-logs-*` returned 0 for `outcome_ingestion_completed`. A `query_string` query for the same name returned 108. The zero came from the query, so I report only the `query_string` result. The same query shape returned 0 for `promotion_run`, which is why I read that event from its own index.
- A sort on `@timestamp` in `agent-insights-*` returned a row from April. The field that orders those documents is `timestamp`.
- `_cat` counts for the reflection indices sum to far more than the `_count` of 1,648. I use `_count`.
- The planner call logs with role `primary` (FRE-1615), so I read planner counts from `route_traces.planner_decision`.

**Rejected:** a new planner run, because master's instruction forbids a measurement that needs the Mac. A Laya run, for the same reason and because the order sets entry conditions. A second read of E1, E2 and E4, so those figures rest on one summarized read. The arithmetic means in E1 agree with the per-workflow figures on the same page.

**Limits:** the vendor and community figures are self-reported. The planner figures are from older builds. The 36 live decisions have no correctness labels. No text from the owner's turns appears here. Fixture names with personal content are omitted.

**Earlier work read:** `docs/research/2026-07-16-model-routing-sota-survey.md`, `docs/research/2026-09-12-fre-1498-what-the-model-chooses-when-nothing-forces-it.md`, `docs/research/2026-10-13-fre-1603-routing-rerun.md`, `docs/research/2026-10-18-fre-1615-block1-planner-arms.md`, `docs/research/2026-10-24-fre-1667-opencode-context-and-prompts.md`, ADR-0127, ADR-0152, ADR-0154, FRE-990, FRE-1293, FRE-1611, FRE-1694, FRE-1695.

## Filed tickets

None. Master files the feedback loop study. The Laya test gets no ticket, by master's instruction of 2026-11-05.

## Proposals

**P1 — the one recommendation (AC-3): keep the order that the owner accepted on 2026-11-05. Do not test a decision model before its entry conditions are met.**

1. FRE-1728, the eval pilot.
2. FRE-1695, on the current build, with a noise arm, so that a change can be separated from the spread of about 15 points in F12.
3. The Laya test, only if the pilot ranks expand-or-decline among its top defects, or FRE-1695 does not restore the floor. It is a direct try on the existing fixtures, with no harness. It first answers where Laya runs (F14). A held-out set is fixed before any tuning. It uses the FRE-1695 draws as its baseline, and tests reworded requests. The expected result is a failure (E3, F12), and "none" is a valid answer. Jev stays optional, because it sends turn text to a new vendor (E6).
4. A study on how to close the feedback loop, with the pilot's defect list as its input. Its inputs from this document are F4 to F11. It must separate the cost of reflection from `session_summary` (F11), and must explain why no outcome reads `shipped` (F8).

Tool approval and skill routing stay rejected.

The owner accepted the order on 2026-11-05 ("3 ok"). The owner can accept or reject the new additions as one decision:

- the noise arm in step 2
- the baseline and the reworded requests in step 3
- the two inputs in step 4
