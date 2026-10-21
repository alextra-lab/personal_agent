# FRE-1640 — the cloud-role model matrix, judged by an Opus 5.5 + gpt-6.1-sol panel in both orders

> **Ticket:** [FRE-1640](https://linear.app/frenchforest/issue/FRE-1640) · **Date:** 2026-10-21 · **Seat:** explore · **Paid cost:** $4.78 (budget about $11) · **Status:** measurements complete. The owner decides every swap. This study changes no binding.

## Question

The owner wants to move the cloud roles to more capable models, using the whole catalog. FRE-1628, FRE-1630 and FRE-1633 tested only the cheaper models, mostly at their lowest setting, with an older judge panel that showed a strong position bias. This study does two things:

1. **Part 1** builds a new judge panel — a fresh Claude Code Opus subagent and `gpt-6.1-sol` through the API — and calibrates it against the old panel (Sonnet 5, gpt-5.4-mini, Haiku 4.5).
2. **Part 2** measures each cloud role against stronger and cheaper candidates, at stated settings, with one table per job: the bar result, quality, cost per 30 days and latency.

## Summary for the owner

| job | today | passes its bar | fails its bar | the closest call |
|---|---|---|---|---|
| `captains_log` reflection | Sonnet 5 ($7.95 / 30 d) | **Sol** 20/20 ($2.45), **Haiku 5.5** 18/20 ($0.38), **Sonnet 5.5** 17/20 ($8.53) | Luna medium 15/20, Luna low 12/20 | Luna medium: one pair short |
| `session_summary` | Sonnet 5 ($0.43) | **Sol** ($0.25), **Luna low** ($0.01) | — | the dollar saving is $0.41 at most |
| `span_extraction` | Sonnet 5 ($6.25) | none | Sol (2 checks), Luna none (2), Luna low (5) | Sol: 2 small exempt classes over the FP bar |
| `entity_extraction` | gpt-5.4-mini ($0.40) | none | Sol, Luna low, Luna medium (2 bars each) | all three lose personal facts |

Bars: the ticket's, as preregistered on the ticket before each run. The `captains_log` rule is master's stricter rule (a pair is worse if either judge prefers Sonnet 5 in both orders), set before that run.

Two points matter for every reading below:

- **Sol as a judge prefers OpenAI reflections absolutely.** It picked the OpenAI candidate on all 120 of its votes (Sol, Luna low, Luna medium). Opus decides every OpenAI candidate under master's rule. Opus also prefers Sol's reflections (38 of 40 votes), so Sol's own pass does not rest on Sol's votes.
- **Sonnet 5 is the weakest reflection writer in the matrix under this panel.** Both judges prefer every candidate to it on most pairs. The one exception is Opus on Luna low (21 to 17).

## Findings

Each finding states its verdict, the query as run, and the actual output. A negative finding carries the admissibility arms of the explore skill.

### F1 — The new judges have almost no position bias

**Verdict: POSITIVE.**

**Query.** For each judge, over the 40 Part 1 captain's log presentations (2 candidates × 10 pairs × 2 orders, the stored and swapped orders of FRE-1630/1633): the share of A verdicts among parsed verdicts, and the number of pairs where the judge picked the same model in both orders. Old-panel labels: `labels-{haiku,luna}[-swapped].jsonl` (run date 2026-10-20). New labels: `part1/labels-cl-sol.jsonl` and the Opus `labels.jsonl` of sets 2 to 5, mapped through `part1/key-cl.json`.

**Output.**

| judge | slot-A rate | same winner in both orders |
|---|---|---|
| old Sonnet 5 | 0.425 | 15/20 |
| old gpt-5.4-mini | 0.450 | 14/20 |
| old Haiku 4.5 | 0.575 | 11/20 |
| Opus 5.5 | 0.525 | 19/20 |
| gpt-6.1-sol | 0.500 | 19/20 |

Part 2 (180 presentations, 5 candidates) confirms it: Opus slot-A 0.45 to 0.525 and 15 to 19 of 20 consistent per candidate, Sol 0.50 to 0.55 and 14 to 20 of 20.

### F2 — The new judges agree poorly with the old panel and with each other

**Verdict: POSITIVE** (measured κ values).

**Query.** Cohen's κ (FRE-1628 result 5 method). The κ code reproduces the FRE-1628 old-panel value: old judge 1 against old judge 2 on Block 1 = 0.530, 43 of 54 agree. Block 1: 54 blinded replies of FRE-1615, labels PASS/PARTIAL/FAIL, unchanged FRE-1598 judge prompt (no run date). Captain's log: the 40 presentations of F1, labels candidate / Sonnet / tie.

**Output — Block 1.**

| pair | all 54 | without the 9 research rows |
|---|---|---|
| old 1 (Sonnet 5) ~ old 2 (gpt-5.4-mini) | 0.530 | 0.355 |
| Opus ~ old 1 | 0.164 | 0.318 |
| Opus ~ old 2 | 0.231 | 0.393 |
| Sol ~ old 1 | 0.246 | 0.273 |
| Sol ~ old 2 | 0.184 | 0.184 |
| Opus ~ Sol | 0.209 | 0.235 |

Label use: old 1 PASS 40 / PARTIAL 5 / FAIL 9; old 2 38/3/13; Opus 44/8/2; Sol 30/19/5. On the 9 research rows, old 1 and old 2 each gave FAIL on 6 (the known missing-run-date defect of that prompt). Opus gave PASS on 8, Sol on 5.

**Output — captain's log.** Opus ~ old Sonnet 0.162, ~ old mini 0.333, ~ old Haiku −0.011. Sol ~ old Sonnet −0.009, ~ old mini 0.064, ~ old Haiku −0.126. Opus ~ Sol 0.165.

**Reading.** The old panel's 0.530 depends on the research rows that both old judges failed for the same wrong reason. Without them, Opus agrees with each old judge about as well as the old judges agree with each other. Sol is the outlier on Block 1. It gives PARTIAL four times as often.

**Confound.** A Claude Code subagent sees today's date in its own environment. Sol does not, and the Block 1 prompt states no date. Sol also passed most research rows, so the date gap is not only an Opus effect.

### F3 — Sol as a judge picks the OpenAI reflection on all 120 votes

**Verdict: POSITIVE.**

**Query.** Part 2 captain's log, Sol labels (`part2/cl/labels-cl2-sol.jsonl`) mapped to winners through `key-cl2.json`, per candidate, 40 votes each (20 pairs × 2 orders).

**Output (candidate / Sonnet 5 / tie).**

| candidate | Sol's votes | Opus's votes |
|---|---|---|
| gpt-6.1-sol | 40 / 0 / 0 | 38 / 2 / 0 |
| gpt-6-luna medium | 40 / 0 / 0 | 22 / 12 / 6 |
| gpt-6-luna low | 40 / 0 / 0 | 21 / 17 / 2 |
| claude_haiku_5_5 | 35 / 2 / 3 | 35 / 5 / 0 |
| claude_sonnet_5_5 | 24 / 11 / 5 | 37 / 1 / 2 |

Part 1 showed the same pattern on Luna `none`: Sol 20 of 20, Opus 12 of 20.

**Reading.** Under the ticket's first rule (worse only if Sonnet wins ≥ 3 of 4 votes), Sol's 2 votes alone keep any pair "not worse". That rule passes every OpenAI candidate on Sol's word. Master replaced it before the `captains_log` run with "worse if either judge prefers Sonnet 5 in both orders". Under that rule, Opus decides every OpenAI candidate.

### F4 — `captains_log`: Sol, Haiku 5.5 and Sonnet 5.5 pass; Luna fails at low and medium

**Verdict: POSITIVE** (measured bar results).

**Query.** 20 real production turns: c01 to c10 stored by FRE-1630, c11 to c20 newly pulled (production ES, read only, 2026-09-28 to 2026-10-20, one per session, the stored sessions excluded). Unchanged `harness.generate` → production `generate_reflection_entry` (DSPy), DSPy cache off. Unchanged harness judge prompt, run date 2026-10-21 (Part 1 Haiku pairs: 2026-10-20). Both judges, both orders. Bar: no reflection off the DSPy path, and not worse on ≥ 16 of 20.

**Output.**

| candidate (setting) | **master's rule** | Opus alone | Sol alone | ticket's first rule | DSPy path | proposals | worse pairs (master's rule) |
|---|---|---|---|---|---|---|---|
| gpt-6.1-sol (medium) | **20 — PASS** | 20 | 20 | 20 | 20/20 | 20/20 | — |
| claude_haiku_5_5 (medium) | **18 — PASS** | 18 | 19 | 19 | 20/20 | 20/20 | c02, c05 |
| claude_sonnet_5_5 (high) | **17 — PASS** | 20 | 17 | 20 | 20/20 | 20/20 | c06, c14, c15 (Sol) |
| gpt-6-luna medium | **15 — FAIL** | 15 | 20 | 20 | 20/20 | 19/20 | c01, c03, c05, c09, c20 (Opus) |
| gpt-6-luna low | **12 — FAIL** | 12 | 20 | 20 | 20/20 | 15/20 | c02, c05, c06, c09, c12, c13, c16, c20 (Opus) |

Sonnet 5 control: 20/20 on DSPy, 20/20 proposals.

**Cost and latency.**

| arm | per reflection | 30-day projection | median latency per DSPy call |
|---|---|---|---|
| claude_sonnet (production) | $0.0267 | **$7.95** (actual) | 19.9 s |
| claude_sonnet_5_5 | $0.0287 | $8.53 | 11.5 s |
| gpt-6.1-sol | $0.0082 | $2.45 | 12.3 s |
| claude_haiku_5_5 | $0.0013 | $0.38 | 9.8 s |
| gpt-6-luna medium | $0.0006 | $0.18 | 8.6 s |
| gpt-6-luna low | $0.0004 | $0.13 | 6.8 s |

30-day base: production ES `agent-logs-*`, `event_type=dspy_job_settled`, `budget_role=captains_log`, now-30d, sum `settled_usd`: **168 jobs, $7.9494**. Projection = base × (candidate per-reflection cost ÷ Sonnet 5 per-reflection cost on the same 20 turns). Sonnet 5.5 made 25 litellm calls for 20 reflections (5 DSPy repeats); its cost includes them.

Note: PR #1320 bound `captains_log` to `claude_haiku_5_5`, and PR #1321 (`69f2eff8`) reverted it on 2026-10-21. Main and production both bind `claude_sonnet`. This study's control names Sonnet 5 explicitly, so the revert does not change any result here.

### F5 — `session_summary`: both candidates pass

**Verdict: POSITIVE.**

**Query.** Unchanged `scripts/eval/session_digest_eval.py`, all default sets, frozen synthetic fixtures, `session_summary` bound in memory per arm. Ticket rule: a candidate passes if it passes every criterion that the same-day Sonnet 5 control passes.

**Output.**

| criterion | Sonnet 5 (control) | gpt-6.1-sol (medium) | gpt-6-luna low |
|---|---|---|---|
| AC-8 | pass | pass | pass |
| AC-10 | fail: agreement 0.625, dominant tag 0.80 | fail: 0.143, 0.889 (`mixed` 8 of 9) | fail: 0.583, 0.524 |
| AC-12 | pass: 8/8, 0 FP | pass: 8/8, 0 FP | pass: 8/8, 0 FP |
| AC-13 | pass 2/2 | pass 2/2 | pass 2/2 |
| **ticket rule** | — | **PASS** | **PASS** |
| per call / 30 days / median latency | $0.00364 / **$0.43** / 2.2 s | $0.00212 / $0.25 / 4.0 s | $0.00010 / $0.01 / 2.0 s |

30-day base: production ES, `event_type=model_call_completed`, `prompt_callsite=role.session_summary`, now-30d: **17 calls, $0.4266**. With F4's 168 reflection jobs ($7.9494), this equals production `api_costs` `purpose='captains_log'` over 30 days exactly: 185 calls, $8.3760.

All 31 forced-tool calls of Sol and Luna went through the guarded Responses route (`aresponses_as_chat`, FRE-1627). Haiku 5.5 failed AC-12 here in FRE-1628 (2 `schema_invalid`). Both GPT-6 candidates have 0 schema failures.

### F6 — `span_extraction`: no candidate passes; Sol misses 2 small exempt classes

**Verdict: POSITIVE** (measured bar results).

**Query.** Unchanged `scripts/eval/fre1281_span_extraction/harness.py --samples 3 --model-key <key>`, whole corpus. Rule (FRE-1628 preflight 2): meet all 15 bars that Sonnet 5 met in its 2026-08-24 record, and `connective_evaluative` FP ≤ 0.267.

**Output.**

| bar (`bars.py` v1) | need | Sol (medium) | Luna none | Luna low |
|---|---|---|---|---|
| recall.overall | ≥ 0.90 | 1.000 | 0.986 | 1.000 |
| precision.overall | ≥ 0.80 | 0.853 | 0.814 | **0.771** |
| decomposition.boundary_f1 | ≥ 0.75 | 0.843 | 0.807 | 0.803 |
| per-class recall (8) | ≥ 0.85 | all 1.000 | all ≥ 0.897 | all 1.000 |
| fp attributed_restatement | ≤ 0.15 | **0.300** | **0.167** | **0.233** |
| fp code_body | ≤ 0.15 | 0.000 | 0.117 | **0.183** |
| fp derived_arithmetic | ≤ 0.15 | **0.167** | 0.100 | 0.107 |
| fp system_record | ≤ 0.15 | 0.000 | 0.000 | **0.182** |
| fp connective_evaluative | ≤ 0.267 | 0.200 | **0.433** | **0.467** |
| degraded documents (unanchored quotes) | — | 0 | 24 | 38 |
| failed documents (transient, F10) | — | 1 | 0 | 2 |

Sonnet 5 record: recall 0.953, precision 0.862, boundary F1 0.804, `connective_evaluative` 0.267, the 4 other FP classes "met" with no value recorded. Sol's per-class values cannot be compared with Sonnet's on those 4 classes (**UNVERIFIABLE** from the record).

**Cost and latency.** Scale measured on the same 10 documents, 1 sample: Sonnet 5 $0.0469 for 10 calls, Sol $0.0177 for 9.

| arm | per call (corpus) | 30-day projection | median latency |
|---|---|---|---|
| claude_sonnet | ≈ $0.0048 | **$6.25** (actual) | 2.0 s on the corpus |
| gpt-6.1-sol | $0.0020 | $2.62 | 3.8 s |
| gpt-6-luna none | $0.00009 | $0.12 | 1.8 s |
| gpt-6-luna low | $0.00010 | $0.13 | 1.9 s |

30-day base: production `api_costs` `purpose='span_extraction'`, now-30d: 185 calls, $6.2457, mean 3,822 in / 1,754 out tokens, median `latency_ms` 10,276. Production calls are larger than corpus calls (about 2,500 input tokens), so the projection assumes that the cost ratio holds at production size.

### F7 — `entity_extraction`: all three candidates fail the same 2 bars, on personal facts

**Verdict: POSITIVE** (measured bar results).

**Query.** Unchanged `scripts/eval/fre630_extraction_quality/harness.py --samples 3`, 50 gold cases, prompt hash `6a17f2476cb9`, `entity_extraction` bound in memory, same-day gpt-5.4-mini control. Bars: FRE-1628 preflight 1.

**Output.**

| bar | mini (control) | Sol (medium) | Luna low | Luna medium |
|---|---|---|---|---|
| entity_f1 ≥ ctl − 0.05 | 0.679 | 0.820 | 0.791 | 0.784 |
| entity_type_accuracy ≥ ctl − 0.05 | 0.833 | 0.887 | 0.881 | 0.888 |
| knowledge_class_accuracy ≥ ctl − 0.02 | 0.956 | 0.966 | 0.952 | 0.952 |
| hallucination_rate ≤ ctl + 0.02 | 0.000 | 0.000 | 0.000 | 0.000 |
| dedup_convergence ≥ ctl − 0.02 | 1.000 | 1.000 | 1.000 | 1.000 |
| forbidden_edge_type_rate ≤ ctl + 0.02 | 0.028 | 0.000 | 0.005 | 0.000 |
| **empty_fallback_rate ≤ ctl + 0.02** | 0.000 | **0.107** | **0.120** | **0.120** |
| **claim_case_level_recall ≥ ctl** | 3/12 | **1/12** | **1/12** | **2/12** |
| per call / 30 days / median latency | $0.00204 / **$0.40** / 1.6 s | $0.00679 / $1.35 / 6.4 s | $0.00041 / $0.08 / 4.9 s | $0.00047 / $0.09 / 5.7 s |

30-day base: production `api_costs`, `purpose='entity_extraction' AND model='openai/gpt-5.4-mini'`, now-30d: 58 calls, $0.4045. The production median `latency_ms` for mini is 4.8 s against 1.6 s here. This run does not explain that difference.

**Reading.** The GPT-6 models extract world entities better (F1 +0.10 to +0.14, fewer forbidden edges). On personal statements, they return no `Location` for a visit and fewer of the expected claim facets. Luna `none` showed this in FRE-1628, and Sol shows it too, so a stronger OpenAI model does not remove it. Instrument check: on `visit-not-residence-lagoon`, Sol's raw result is valid JSON with `entities: []` and one `Personal` claim (`facet: lagoon_island_visit`). It is a model behaviour, not a parse failure.

### F8 — The `empty_fallback_rate` bar counts correct claim-only outputs

**Verdict: POSITIVE.**

**Query.** `metrics.extraction_empty(n, gold_has_positive)` returns `n == 0 and gold_has_positive`. `GoldCase.has_positive_label` includes `expect_claims`. Per arm, the empty samples split by whether the gold case expects entities.

**Output.** 5 gold cases expect claims and no entities. Of the empty samples: Sol 9 of 16 on those cases, Luna low 12 of 18, Luna medium 12 of 18, mini 0 (it emits spurious entities there, for example `Coastal town`, `Humidity`, `Salt-air upkeep` on `claim-relocation`). Without those samples, the candidates are still over the bar: Sol 7/150 = 0.047, Luna low 0.040, Luna medium 0.040. No verdict changes. Filed as FRE-1644.

### F9 — The DSPy response cache returns old reflections and books unpaid spend

**Verdict: POSITIVE.**

**Query.** A second `harness.generate` of turn c11 on the same arm and settings, with a timer around `litellm.completion`. Then the test ledger: `SELECT purpose, model, count(*), sum(cost_usd) FROM api_costs WHERE timestamp >= '2026-10-21 03:30:00+00' GROUP BY 1,2` on the test ledger (:5433).

**Output.** The repeat made 0 litellm calls (timer: 0) and returned the first reflection. The harness row reported the first call's cost. The ledger holds a row for each repeat: `gpt-6.1-sol` 56 `captains_log` rows, $0.2637, which equals 33 digest calls + 20 reflections + 3 trace rows, one of them the cached repeat ($0.0098). Likewise `claude-sonnet-5-5` 23 rows, with the cached repeat ($0.0201). The production rate of cache hits is **UNVERIFIABLE**: no event records a DSPy cache hit. Filed as FRE-1645.

### F10 — Sol and Luna `low` fail a span document now and then; the cause is not recorded

**Verdict: UNVERIFIABLE** (cause).

**What was measured.** `span_extraction_document_failed`: Sol 1 of 156 scorings in the full run and 1 of 10 in the ratio run; Luna low 2 of 156. Each failed document passed 3 of 3 times on a re-run. The Sol run logged `litellm_commit_failed` once. My driver kept only warning counts in those runs, so the error text is not on record. The driver kept full warning lines from then on. A rate of about 1 % per call is the only established fact.

### F11 — The extraction and span harness calls book no ledger row

**Verdict: NEGATIVE** (no `api_costs` row for those harness calls).

**Query.** `SELECT purpose, model, count(*), sum(cost_usd) FROM api_costs WHERE timestamp >= '2026-10-21 03:30:00+00' GROUP BY 1,2` on the test ledger (:5433).

**Output.** Rows exist only for `captains_log` (5 models, 212 rows) and `study` (`gpt-6.1-sol`, 275 rows, $1.5529). No `entity_extraction` row, no `span_extraction` row.

- **Arm 1 (provenance).** The identifiers are `purpose` values in the `api_costs` table. Raw instance in the same store: production `api_costs` holds `purpose='entity_extraction'` (58 mini rows in 30 days) and `purpose='span_extraction'` (185 rows), so both values are real in this schema. The cause is on record in each run: `cost_record_missing_identity` was logged once per call (150 per entity arm, 142 to 144 per span arm), with `reason=trace_ctx_missing_session_id`.
- **Arm 2 (liveness).** The same query on the same ledger and window returns `captains_log` and `study` rows (above), so the table, the window and the filter work.
- **Arm 3 (scope).** The test ledger (:5433), rows from 2026-10-21 03:30Z, this study's runs only. The span harness also bills `budget_role="entity_extraction"` (`harness.py:177`), not `span_extraction` as production does.

**Consequence for AC-3.** The paid cost for those two jobs comes from the `cost_usd` on each run's `model_call_completed` lines, not from the ledger.

### F12 — The blinded Opus judges read only their own directories

**Verdict: NEGATIVE** (no tool call outside the directory).

**Query.** For each of the 15 labelling subagents (sets 1 to 15) and the 5 failed first spawns, every `tool_use` block in the subagent transcript, with any path argument compared against the batch directory (`audit.py`).

**Output.** Sets 1 to 15: only Read (22 to 112 per set), Write (1 to 3) and, on set 6, Edit (4, on its own `labels.jsonl`, to correct one id). Every path is inside the batch directory. Every assistant turn ran on `claude-opus-5-5`. The 5 failed first spawns made 2 Reads each, inside their directories, plus one ToolSearch for Glob.

- **Arm 1 (provenance).** The audited identifier is the `file_path` / `path` argument of a `tool_use` block in the same transcripts. Raw instances exist in every transcript: for example set 2's 24 Read calls, each with a `file_path` under `.../scratchpad/opus/set-2/`.
- **Arm 2 (liveness).** The same audit, with the predicate "inside the directory", returns 22 to 112 matches per set, so the parser reads the tool calls.
- **Arm 3 (scope).** The 20 subagent transcripts of this study (`tasks/<agent-id>.output`). It says nothing about other sessions.

## Method appendix

**Environment.** FRE-375 test stack only. A wrapper (a copy of the FRE-1630 wrapper) exports `AGENT_ANTHROPIC_API_KEY` and `AGENT_OPENAI_API_KEY` from `/opt/seshat/.env` by exact name and pins Postgres :5433, ES :9201 and Neo4j :7688. Production stores were read only: ES `agent-logs-*` and `agent-captains-captures-*` (the harness `pull` and the 30-day sums), and `api_costs` on the production Postgres (30-day sums). No repo file changed for any run. Branch `explore-fre-1640-cloud-role-model-matrix` from main `adafd9ea`.

**In-memory settings.** A driver ran each unchanged harness with: `resolve_role_model_key(role)` returning the arm's key in every module that imported it; for Luna `low` and `medium`, a new catalog mode `{reasoning_effort: <setting>}` (no temperature) as the default mode of the cached `gpt-6-luna` entry; a timer around `litellm.acompletion`, `litellm.completion` and `aresponses_as_chat`, which also recorded the `reasoning_effort` and `temperature` sent. The resolved mode and the reasoning tokens confirm that each setting reached the provider (for example, entity extraction: 0, 33, 117 and 231 mean reasoning tokens for mini, Sol, Luna low and Luna medium).

**Judging.** Each judge text was built once with the harness's own builder parts. A dry run of the unchanged `harness.judge` with a fake judge gave identical system and user text on 40 of 40 presentations. Sol got the text as system + user message through `get_llm_client_for_key(..., budget_role="study")`, its default mode (reasoning medium). Each Opus batch was a fresh `general-purpose` subagent on `model: opus`, given only a directory with `PROMPT.md` (the same system text), `INDEX.txt` and one file per item, with opaque ids. The key was written after the directories, outside them. One Opus batch per (candidate, order), so no Opus context saw a pair twice. Subagents have no Glob tool, so a first spawn without `INDEX.txt` wrote no labels (F12).

**Bars.** Each bar was fixed and posted on the ticket before its run. `captains_log` used master's stricter rule, set after Part 1 and before that run.

**Rejected or changed.**
- The ticket's first `captains_log` rule (≥ 3 of 4 votes): replaced by master after F3. Its results are still reported.
- Block 1 "both orders": a single reply has no slot to swap, so each judge labelled each reply once.
- `span_extraction` with no control: scaled by a 10-document Sonnet 5 and Sol run on the same documents.
- `claude_sonnet_5_5` on `span_extraction` and `session_summary` (forced `tool_choice` returns 400) and `claude_haiku_5_5` on `entity_extraction` (the producer sends a `temperature`): not testable without code changes, as the ticket states. Not run.

**Identifier resolutions.**
- ES event names live in `event_type`, not `event`. A first sum on `event` returned 0 for both lane queries. The same query on `event_type` returned 168 and 17, which match `api_costs` exactly.
- The 30-day `captains_log` lane splits into reflection (`dspy_job_settled`, `budget_role=captains_log`) and `session_summary` (`model_call_completed`, `prompt_callsite=role.session_summary`).

**Cost (AC-3).**

| part | paid |
|---|---|
| Part 1 (Sol judge) | $0.445 |
| `entity_extraction` | $1.495 |
| `session_summary` | $0.206 |
| `span_extraction` | about $0.395 |
| `captains_log` (generation $1.046, Sol judge $1.114, trace $0.077) | $2.237 |
| **Total** | **$4.78** |

Sources: `cost_usd` on each run's `model_call_completed` lines, `settled_usd` on the reflection rows, `cost_usd` on the judge label rows. The test ledger confirms the `study` and `captains_log` parts, apart from the cached-repeat rows in F9 and the one Part 1 trace row lost on an asyncpg `ConnectionDoesNotExistError`. Opus judging ran on the Claude Code plan: about 2.0 M subagent tokens (1.73 M for the 15 labelling batches, 0.27 M for the 5 failed first spawns), with no API cost.

**Data handling.** The captain's log inputs hold real conversation text. They stay in the gitignored `telemetry/evaluation/fre1630-captains-log-ab/fre1640/` and the session scratchpad, and are not committed. The driver scripts live in the scratchpad (tmpfs).

## Proposals

1. **`captains_log`: the owner decides among three passing candidates.** Sol (20/20, $2.45 per 30 days, 12.3 s), Haiku 5.5 (18/20, $0.38, 9.8 s), Sonnet 5.5 (17/20, $8.53, 11.5 s). Main and production bind `claude_sonnet` (PR #1321 reverted the PR #1320 Haiku 5.5 binding). This study changes no binding.
2. **`session_summary`: the owner decides.** Sol and Luna low both pass. The saving is $0.18 or $0.41 per 30 days. Luna low is the only arm that meets AC-10's dominant-tag bar.
3. **`span_extraction` stays on Sonnet 5 under the bars.** Sol is the near miss: recall 1.000, precision 0.853, and over the FP bar only on two small exempt classes. If the owner wants Sol here, the next step is a look at those classes' false positives against `ADJUDICATION.md`, not a new paid run.
4. **`entity_extraction` stays on gpt-5.4-mini.** The personal-fact gap is shared by Sol and Luna, so no catalog model fixes it. A fix is prompt or compile work on claim facets (FRE-764 is the existing compile ticket), not a model swap.
5. **Adopt the Opus + Sol panel with master's rule for future pair studies, and retire the old `JUDGES`.** Never let Sol judge OpenAI candidates alone (F3). Judge 3 of the old panel is `claude-haiku-4-5-20251001`, past its earliest retirement date. If adopted, keep the blinding procedure (directory, `INDEX.txt`, key outside, transcript audit) as a committed script, because this study's drivers live on tmpfs.
6. **Fix the `empty_fallback_rate` metric** (FRE-1644) before the next extraction study.
7. **Turn the DSPy cache off for paid cloud roles, or book no cost on a cache hit** (FRE-1645). Eval re-runs of stored inputs need it off.
8. **Give the extraction and span harness calls a session id, and bill the span harness to `span_extraction`.** Today their spend is invisible to the ledger and the caps (F11). No ticket filed: FRE-1628 also met it, and master can fold it into the next harness change.

## Filed tickets

- [FRE-1644](https://linear.app/frenchforest/issue/FRE-1644) — FRE-630 extraction harness: `empty_fallback_rate` counts a correct claim-only output as an empty fallback (Backlog).
- [FRE-1645](https://linear.app/frenchforest/issue/FRE-1645) — DSPy response cache: a cached Captain's Log reflection makes no call, yet books an `api_costs` row and reports a cost (Backlog).
