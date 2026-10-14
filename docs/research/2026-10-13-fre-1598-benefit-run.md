# FRE-1598 — the benefit run on Flash-Next (ADR-0156 A5, T-D2)

**Ticket:** FRE-1598. **ADR:** ADR-0156, amendment of 2026-10-12, AC-F4 benefit. **Plan:**
`docs/superpowers/plans/2026-10-13-fre-1598-benefit-run.md`. **Harness:** `scripts/eval/fre1598/` on
`scripts/eval/fre1590/help.py`. **Run:** 2026-10-13, 20:54–22:03Z (69 min). **Judging:** 2026-10-14, 03:15Z.

## Result

| AC | Verdict | Evidence |
|---|---|---|
| AC-1: no scenario scores lower with skills | **PASS** | 0 scenarios lower. Treatment higher on 6, equal on 5. Totals: treatment 22, control 9 (of 44) |
| AC-2: every reply has both labels; every split has a third label | **PASS** | 22 labels from judge 1, 22 from judge 2, 2 PASS/FAIL splits with 1 third label each, 0 missing, 0 unexpected |

The skills did not make any answer worse. Of the 13-point difference, 11 points come from the 4 scenarios in which
both arms answered without expansion and only the treatment loaded a skill. The other 2 points are one label each
on two turns where both arms expanded. Of the 5 scenarios with no difference, the planner expansion explains 2
(log-search#2 and #3: the treatment expanded before it saw its catalog).

## Method

- **Arms.** Both gateways run the same build. Treatment (:9003) has the skill catalog, and `search_logs` and
  `measure_model_latency` stay hidden until a `read_skill` reveals them (production). Control (:9002) has every
  `SKILL.md` masked. Before `/chat`, the harness writes both tools into the new session's
  `sessions.revealed_tools` in postgres-eval, so control sees them from call 1 (owner decision, 2026-10-13).
- **Turns.** 11 scenarios × 2 arms = 22 full gateway turns on Flash-Next. The arm order alternates. The eval
  graph is wiped before each turn.
- **Instrument checks.** A row that fails one is an error row and is not judged. Control has no catalog, the
  seed reads back both tools, and every primary call lists both tools. Treatment has a catalog and an empty
  seed, its first primary call lists neither tool, and each call after a reveal lists the revealed tool. Each
  primary call is in `NORMAL` mode. No turn has `orchestrator_call_failed` or the generic error reply.
  **Result: 22 rows, 0 error rows, `RESULT: PAIRED`.**
- **Judges.** Each call sees one reply, its request and its `expect` text, and nothing else. The order is a
  seeded shuffle (seed 20261013) with opaque ids. Judge 1 is `claude_sonnet` (`claude-sonnet-5`), judge 2 is
  `gpt-5.4-mini`, and the third judge is `claude_haiku` (`claude-haiku-4-5-20251001`). The judge system prompt
  is quoted in the preflight comment on FRE-1598.
- **Score.** PASS 2, PARTIAL 1, FAIL 0. A scenario's score is the sum of both judges' points. On a PASS/FAIL
  split, the third label replaces the lower label.

## Pins

| Pin | Value |
|---|---|
| Model | `qwen3.8-flash-next` = `unsloth/qwen3.8-flash-next`, UD-IQ4_XS, context 131,072 |
| Sampling (`default` mode) | thinking on, temperature 1.0, top_p 0.95, top_k 20, min_p 0, presence 0, repeat 1.0, max_tokens 12,288 |
| llama.cpp | build `b11521-b42b7e6d3`, chat template `12827f24…aadce`, 3 slots. slm_server confirmed engine pid 22594 unchanged at 20:53:40Z |
| Gateways | both `388ade22…ddd4b`, from `main` `5f60cc54` |
| Scenarios | 11, each with the SHA-256 of its prompt and of its `expect` text in `fingerprint.json` |
| Judge prompts | system `aa3ecfba…82387`, user template `44d00f18…dbbf` |

The launch arguments and the `/props` reply are pinned in the run directory. They hold host paths, so they
stay out of the repository.

## Every label

| Scenario | Arm | Reply | J1 | J2 | J3 | Points | Planner | Sub-agents | Skill loaded | Tool calls | Wall (s) |
|---|---|---|---|---|---|---|---|---|---|---|---|
| graph-inspection#1 | control | r10 | FAIL | FAIL | - | 0 | declined | 0 | none | 5 | 88 |
| graph-inspection#1 | treatment | r08 | PASS | FAIL | FAIL | 2 | declined | 0 | graph-inspection | 2 | 46 |
| interaction-outcomes#1 | control | r16 | FAIL | FAIL | - | 0 | declined | 0 | none | 7 | 122 |
| interaction-outcomes#1 | treatment | r02 | PASS | PASS | - | 4 | declined | 0 | interaction-outcomes | 3 | 28 |
| interaction-outcomes#2 | control | r05 | FAIL | FAIL | - | 0 | not run | 0 | none | 6 | 88 |
| interaction-outcomes#2 | treatment | r01 | PARTIAL | FAIL | - | 1 | not run | 0 | interaction-outcomes | 3 | 105 |
| log-search#2 | control | r06 | FAIL | FAIL | - | 0 | declined | 0 | none | 7 | 52 |
| log-search#2 | treatment | r20 | FAIL | FAIL | - | 0 | expanded | 1 | none | 17 | 111 |
| log-search#3 | control | r03 | FAIL | FAIL | - | 0 | declined | 0 | none | 2 | 25 |
| log-search#3 | treatment | r14 | FAIL | FAIL | - | 0 | expanded | 2 | none | 20 | 373 |
| model-latency#3 | control | r12 | FAIL | FAIL | - | 0 | expanded | 2 | none | 13 | 164 |
| model-latency#3 | treatment | r18 | FAIL | FAIL | - | 0 | declined | 0 | model-latency | 4 | 118 |
| service-health#0 | control | r11 | FAIL | FAIL | - | 0 | declined | 0 | none | 7 | 90 |
| service-health#0 | treatment | r13 | PASS | PASS | - | 4 | declined | 0 | service-health | 3 | 73 |
| spend-and-tokens#0 | control | r17 | PASS | PASS | - | 4 | expanded | 1 | none | 20 | 147 |
| spend-and-tokens#0 | treatment | r21 | PASS | PASS | - | 4 | declined | 0 | spend-and-tokens | 2 | 25 |
| spend-and-tokens#1 | control | r09 | PASS | PASS | - | 4 | declined | 0 | none | 8 | 113 |
| spend-and-tokens#1 | treatment | r07 | PASS | PASS | - | 4 | declined | 0 | spend-and-tokens | 4 | 50 |
| turn-explanation#1 | control | r22 | FAIL | FAIL | - | 0 | expanded | 2 | none | 53 | 441 |
| turn-explanation#1 | treatment | r19 | PARTIAL | FAIL | - | 1 | expanded | 2 | none | 56 | 575 |
| turn-explanation#2 | control | r04 | PARTIAL | FAIL | - | 1 | expanded | 1 | none | 26 | 238 |
| turn-explanation#2 | treatment | r15 | PASS | FAIL | FAIL | 2 | expanded | 2 | sequential-thinking, turn-explanation | 40 | 375 |

"Points" is the reply's share of the scenario score. "Planner: not run" means that the turn has no
`planner_outcome` event. "Tool calls" counts the calls of the primary and of the sub-agents together:
`tool_call_started` carries no role, so the two cannot be told apart.

**Third-judge cases:** r08 (graph-inspection#1, treatment) and r15 (turn-explanation#2, treatment). Both are
PASS from judge 1 and FAIL from judge 2. The third judge gave FAIL on both, so each reply keeps 2 points.

**Scenario scores (control → treatment):** graph-inspection#1 0 → 2, interaction-outcomes#1 0 → 4,
interaction-outcomes#2 0 → 1, log-search#2 0 → 0, log-search#3 0 → 0, model-latency#3 0 → 0, service-health#0
0 → 4, spend-and-tokens#0 4 → 4, spend-and-tokens#1 4 → 4, turn-explanation#1 0 → 1, turn-explanation#2 1 → 2.

## Path or skill: what explains each difference

| Path of the pair | Scenarios | Difference | What explains it |
|---|---|---|---|
| Both answered directly, the treatment loaded its skill | graph-inspection#1, interaction-outcomes#1, interaction-outcomes#2, service-health#0, spend-and-tokens#1 | +2, +4, +1, +4, 0 | The skill. The control replies state that they did not know the method: no Cypher script, no captures index, no reflections index, no probe script |
| Both expanded, no skill in either arm | turn-explanation#1 | +1 | Not the skill: neither arm loaded one. One PARTIAL label from judge 1 |
| Both expanded, the treatment primary loaded the skill in synthesis | turn-explanation#2 | +1 | Not shown to be the skill: both replies used the Tempo `/api/v2/traces/<id>` GET, and eval Tempo holds no such trace. Judge 1 gave the treatment PASS and the control PARTIAL |
| The treatment expanded, the control did not | log-search#2, log-search#3 | 0, 0 | The path. The treatment never saw its catalog before the sub-agents ran |
| The control expanded, the treatment did not | model-latency#3, spend-and-tokens#0 | 0, 0 | Neither. On model-latency#3 the treatment loaded its skill, and eval refused its follow-up command (below) |

### Why 3 treatment turns loaded no skill (master's question)

FRE-1603 measured 0 of 60 no-skill loads. Here 3 of 11 treatment turns loaded no skill (log-search#2,
log-search#3, turn-explanation#1), and all 3 expanded. The traces give the same event order on all 4 expanded
treatment turns (`6530f7e4…`, `803c9e67…`, `6ee32b52…`, `3060c3f5…`):

1. The gateway logs `decomposition_assessed` with strategy `hybrid` and reason `planner_asked`.
2. The first primary call is the planner call: 820 input tokens on log-search#2, no tools. No
   `skill_index_assembled` and no `tools_passed_to_llm` come before it, so the planner sees no catalog.
3. `planner_outcome` `expanded`, then 1 or 2 sub-agents. A sub-agent gets no catalog.
4. The first `skill_index_assembled` of the turn comes after `hybrid_expansion_complete`, on the synthesis call.
5. In synthesis, the primary loaded the skill on turn-explanation#2 only. On the other 3 turns, it answered
   from the sub-agents' digest.

So the expansion happens before the primary can load the skill. The primary did not decline it. The two
results do not conflict: FRE-1597 and FRE-1603 replay the captured tool-loop call with the catalog, and the
planner call comes before that call, so that instrument cannot see an expanded turn. The gap is real in
production: on an expanded turn, the skill cannot steer the method. Each arm expanded 4 times of 11.

## Spend

| Lane (eval ledger) | Calls | Cost |
|---|---|---|
| `captains_log` (reflections, one per turn) | 22 | $1.226 |
| `study` (judges) | 46 | $0.112 |
| `embedding` | 56 | $0.0002 |
| **Total** | | **$1.34** (projection: about $1.35) |

## Threats to validity

- **Expansion bypasses the skill.** The planner decides on a short prompt without the catalog. It expanded 8 of
  22 turns, 4 in each arm. On those turns, the skill could act only in synthesis.
- **The judges disagree on method versus result.** Judge 2 graded the result. Judge 1 followed the prompt's
  third rule ("a reply that reports a failed command ... can still do what EXPECTED describes, if EXPECTED is
  about the method"). Both splits are this case: the treatment ran the right command, and the eval environment
  could not give the result (no Neo4j credential on r08, a trace id that eval Tempo does not hold on r15). The
  third judge sided with judge 2. This understates the treatment.
- **The eval environment refuses some commands.** The approval UI is disabled on eval, so a command that needs
  approval returns `approval_unavailable`. Three replies report a refusal: interaction-outcomes#2 control,
  model-latency#3 treatment and service-health#0 control. The row's `permission_denials` field reads 0 on all
  22 rows, so it does not count these refusals.
- **Eval bash runs as `nobody` with no credential** (FRE-1505). `cypher.py` and `probe_services.py` cannot
  connect on either arm.
- **Two Elasticsearch paths.** `search_logs` reads eval Elasticsearch. A curl recipe in a skill reaches
  production Elasticsearch by its service name (read-only). On interaction-outcomes#1 and #2, the treatment
  replies quote production captures and reflections. Both arms had both paths. The control did not know the
  index names.
- **One draw per arm.** Each scenario ran once per arm at temperature 1.0. A one-point difference (the PARTIAL
  on turn-explanation#1) is inside the noise of one draw.
- **A reply can name its skill.** The replies were not edited. A judge can infer the arm from a reply that
  names a skill or a skill script.

## Proposals (for the owner)

1. **Give the planner the catalog lines, or give a sub-agent the catalog.** Today the planner expands a turn
   before any model sees the skills, so a skill area can lose its method on any expanded turn. This changes
   ADR-0156 or the planner contract, so it needs the owner's decision first.
2. **For a future judged run, state in `expect` what the reply must show when the environment cannot give the
   result.** The judge pair then has one reading of the third rule, and the splits caused by an eval limit go
   away.
3. **Count `approval_unavailable` refusals in the row.** The current `permission_denials` field reads 0 while
   replies report refusals.

## Reproduce

`scripts/eval/fre1598/README.md` gives the commands. The run directory
`telemetry/evaluation/fre1598-benefit-run/20261013-2054/` holds the rows, `fingerprint.json`, the blinding key,
every label with its reason, and `judging/report.json`. It stays out of git, because the rows hold full replies.
