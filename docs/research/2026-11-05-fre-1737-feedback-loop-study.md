# FRE-1737: how to close the feedback loop, with the eval pilot's defect list as input

Date: 2026-11-05. Seat: cc-explore. Commission: FRE-1737, from cc-master, after the owner's approval.
Limits: read-only, no live gateway turn, no paid run, no Mac use, no harness.

## Question

What is the smallest change that turns real use and eval defects into tickets that the owner accepts? Which stage is the first one that loses the signal? "Keep it off" is a valid answer if the evidence supports it.

## Result

**The evidence supports "keep it off" for the automated loop. It did not produce one accepted ticket, and it cannot see the class of defect that the owner complains about.** Two paths did produce accepted tickets. In both, a person or a seat read the turn or the transcript.

- **The first stage that loses the signal is reflection.** Its input holds the length of the reply, not the text (G4). It also does not get the next user message. Eval turns have a second stop, because promotion skips them by design (G6).
- **The automated path:** 16 tickets since May, 13 canceled, 3 undecided, 0 accepted (G7).
- **The path that works:** the owner complains and a seat reads the turn. Of 6 defect classes in the owner's complaints since 2026-07-27, 5 have tickets by title and date, most of them created within a day (G9). The pilot followed the same shape: master read the transcripts, and 2 tickets went to In Progress on the same day (G10).
- **Why no outcome reads `shipped`:** none of the 16 tickets reached Done. The job is correct on that point (G7).
- **The cost of reflection is small on real turns:** $0.90 in 30 days. Eval turns spent $6.84 and session summaries $0.76 (G8). My statement in FRE-1727 F11 was wrong, because reflection is 91% of the `captains_log` figure, not a small part of it. Cost is not the reason to keep it off.

The limit of this result: the sample of 13 canceled tickets predates the input fix in FRE-1608. The 3 tickets after that fix are undecided, so reflection on correct inputs has no verdict yet (G7). P1 asks the owner for that verdict.

## The stages of the loop (AC-1)

Windows differ, so each row names its own. Elasticsearch counts are provisional (FRE-1051).

| # | Stage | State today | Measured count | Finding |
|---|---|---|---|---|
| 1 | Capture of the real turn | On | 471 non-eval turns with a reply since 2026-07-27, 395 of them the owner's | G2 |
| 2 | Owner rating | On, with no consumer that acts on one rating | 17 zero ratings since 2026-07-27 | G3 |
| 3 | Next user message | Stored, and nothing reads it as a signal | 42 complaint turns in 19 of 75 multi-turn sessions, by my reading | G9 |
| 4 | Reflection | Off | 170 of 471 real turns had one, and 26 of those held a proposal | G4 |
| 5 | Proposal and dedup | Off, because its input stopped | 1,438 proposals, 1,254 distinct, 52 seen 3 or more times | G5 |
| 6 | Insights analysis | Off | 241 documents in October, 0 since 2026-10-17 (FRE-1727 F6) | G1 |
| 7 | Promotion | On, with no new input | 131 funnel events: 80 throttled, 51 runs, 3 eligible, 3 filed | G6 |
| 8 | Ticket | Open | 16 reflections carry a ticket, in 16 tickets | G7 |
| 9 | Owner decision | Waiting | 0 accepted, 13 canceled, 3 undecided | G7 |
| 10 | Outcome ingestion | On | 108 runs, 6 outcomes recorded in logs, 9 stamps on reflections, 0 `shipped` | G7 |
| 11 | Feedback polling | Off | Not measured here, because the flag is false | G1 |
| 12 | Pattern analysis of feedback | No caller | FRE-1727 F9 | G1 |
| 13 | Grounding verification | Off | 111 of 395 owner captures hold a grounding record. The compliance table's last row is from 2026-10-18 (FRE-1727 F10) | G11 |

## How the pilot's defects meet the loop (AC-2)

The ranking mixes real defects with a simulator fault, so this table uses the transcripts and not the rank (G10). "Reader" means a person or a seat that reads the turn.

| Pilot item | Real? | Reflection (stage 4) | Next user message (stage 3) | Reader | Other stage |
|---|---|---|---|---|---|
| 1. `repeated_request` (7) | No. Simulator role swap. | No | Sees it, and it is noise | Yes, and drops it | None is needed |
| 2. `ignored_user_statement` (4) | Partly, as defect A | No | Yes, where the user corrects | Yes | None |
| 3. `internal_failure_reported` (4) | Partly, as defect B | No | No, the user did not object | Yes | A literal text scan finds defect B |
| 4. `task_not_delivered` (3) | Mostly no | No | Sees it, and it is mostly noise | Yes, and drops most | None |
| 5. `wrong_level` (2) | No. It repeats items 1 and A. | No | Partly | Yes | None |
| A. Assumption stored as a user fact (FRE-1739) | Yes | No, it needs the reply and the graph | Yes, in run A the simulated user corrected it | Yes | The graph quality monitor counts graph shape, and I found no check of a claim against its source |
| B. False `memory_recall` report (FRE-1740) | Yes in eval. 0 of 471 production replies (G10). | No, the reply text is absent and no tool failed | No | Yes | A literal text scan |
| C. "I don't know" when material exists | Candidate | No | Only if the user objects | Partly, it also needs the stored items | Production: 2 of 395 owner replies open this way. The pilot's 19 of 32 are answers to memory questions only, so the two rates are not the same measure |

No top defect can reach a ticket through the automated stages. All of them need the reply text, and only the capture stage holds it.

## Findings

### G1: the live flags

**Verdict:** POSITIVE

`docker exec` on the production gateway, `/app/.venv/bin/python`, `personal_agent.config.settings`, read today:

```
captains_log_reflection_enabled False
insights_enabled False
insights_wiring_enabled False
feedback_polling_enabled False
context_quality_governance_enabled False
grounding_verification_mode off
promotion_pipeline_enabled True
outcome_ingestion_enabled True
graph_quality_governance_enabled True
failure_path_reflection_enabled True
seshat_open_ticket_cap 10
promotion_min_seen_count 3
```

These match FRE-1727 F4. `failure_path_reflection_enabled` is now true, and FRE-1608 listed it as false.

### G2: real turns are stored with the reply

**Verdict:** POSITIVE

```
GET agent-captains-captures-*/_search
filter: timestamp >= 2026-07-27, exists assistant_response; must_not: eval_mode true
rows 471  sessions 207
months: 2026-07 56, 2026-08 218, 2026-09 98, 2026-10 92, 2026-11 7
owner rows 395  owner sessions 147  owner multi-turn sessions 75  owner follow-up turns 248
```

Real volume is low and falling, which matches the owner's statement on FRE-1728.

### G3: the owner's zero ratings exist, and nothing acts on one

**Verdict:** POSITIVE for the counts, NEGATIVE for a consumer of the stream

```
GET user-turn-ratings-*/_search  terms on rating
[(2, 2331), (0, 29), (1, 9), (3, 6)]
by month, rating 0: 2026-06 9, 2026-07 4, 2026-08 11, 2026-09 2, 2026-10 3

GET user-turn-ratings-*/_count  rating 0 AND rated_at >= 2026-07-27
{"count":17}
```

A rating of 2 is the PWA default (`seshat-pwa/src/__tests__/useAgentStream.test.tsx`: "persists the ok default (rating=2, default=true) once on DONE"). So 2 carries no signal, as FRE-1608 found, and any other value is a click. Of the 17 zero ratings, 14 join to a capture, and all 14 are the owner's, in 4 sessions.

The negative claim, that no code consumes `STREAM_USER_TURN_RATED`:

```
docker exec gateway: cd /app/src; grep -rn "STREAM_USER_TURN_RATED" --include=*.py .
./personal_agent/gateway/feedback_api.py:26  (docstring)
./personal_agent/gateway/feedback_api.py:502 (import)
./personal_agent/gateway/feedback_api.py:513 await bus.publish(STREAM_USER_TURN_RATED, event)
./personal_agent/events/models.py:42  (definition)
./personal_agent/events/models.py:333 (docstring)
```

- **Arm 1 (raw instance, same store):** the five lines above. The file hash of `events/models.py` in the container equals `origin/main` (`8a64ab272fe6bfd6`).
- **Arm 2 (same command, identifier varied):** `STREAM_CONSOLIDATION_COMPLETED` returns `brainstem/scheduler.py`, `service/app.py` and `events/models.py`, so a stream with consumers shows them.
- **Arm 3 (scope):** `.py` files under `/app/src` in the gateway, by this name. A consumer that builds the name as a string is not covered.

The index has readers: `telemetry/queries.py` aggregates per callsite for the insights engine, which is off, and `gateway/session_api.py` shows the rating in the PWA. Neither acts on one turn's rating.

### G4: reflection does not receive the reply, and it did not react to complaints

**Verdict:** POSITIVE for the input, NEGATIVE for proposals on the complaint turns

`captains_log/reflection_dspy.py`, `GenerateReflection` inputs: `user_message`, `steps_count`, `final_state`, `reply_length`, `telemetry_summary`, `metrics_summary`, `failure_excerpt`, `had_errors`, `prompt_manifest`. The call site passes `reply_length=len(ctx.final_reply or "")` (`orchestrator/executor.py`, `_trigger_captains_log_reflection`). The container hashes of `reflection.py` and `reflection_dspy.py` equal `origin/main` (`f2827014d169fc6a`, `7bf6e9701f580a1f`). The reflection index has 0 documents with `assistant_response`.

I joined reflections to real turns through the nested `telemetry_refs.trace_id`:

```
real captures since 2026-07-27 with a reflection: 170 of 471
of those with a proposed_change: 26
zero-rated turns with a reflection: 2 of 14, proposals on them: 0
complaint turns (or the turn before) with a reflection: 10 of 42, proposals on them: 0
```

Arms for the two zeros:

- **Arm 1 (raw instance, same store):** the 26 reflections with a proposal, from the same join on the same index.
- **Arm 2 (same query, identifier varied):** the join on all 471 turns returned 170 reflections and 26 proposals. A zero therefore comes from the turns selected, not the join.
- **Arm 3 (scope):** owner turns from 2026-07-27, in production, on reflections that reached Elasticsearch. A reflection lost on the way to Elasticsearch is not counted. FRE-1051 reports such loss, so the zeros are at this scope only. Reflections in this window ran with the inputs before FRE-1608 for most of the period.

A first version of this join used the top-level `trace_id`, which is absent on all 233 recent reflections, and returned 0 of 471. The method appendix records the error.

### G5: what proposals are about

**Verdict:** POSITIVE

```
GET agent-captains-reflections-*  aggregations, all time
proposals 1438  distinct fingerprints 1254  seen_count >= 3: 52
category: performance 632, observability 325, reliability 184, ux 128, knowledge 48, architecture 22, cost 16, safety 6, concurrency 3

Since 2026-07-27: proposals 66, eval_mode true 36, false 30
what matches prompt or skill component names: 31 of 66
what or why matches reply-quality words: 4 of 66
```

The second pattern was `the assistant's reply|answer|response`, `invented`, `fabricat`, `hallucinat`, `wrong answer`, `incorrect fact`, `citation`, `user complain|correct|frustrat`. Proposals follow the input. The model receives the prompt manifest and telemetry, and it proposes changes to prompt composition and telemetry.

```
_count, phrase in proposed_change.what or why: memory_recall 0, assumption 0, hallucinat 0, fabricat 0, false claim 0, contradict 1, user correction 2
```

The three non-zero rows are about telemetry and skill routing. I read them. For the zeros: **Arm 1** is not available as an instance in this store, and the producer is the reflection model's free text, so the zero says only that these phrases are absent. **Arm 2** is the row `user correction` with 2. **Arm 3** is all proposals, in those two fields, as exact phrases. An analyzed-phrase query on a stem such as `hallucinat` can miss a longer word, so I do not rely on the zero alone, and the regex count above is the stronger figure.

### G6: promotion

**Verdict:** POSITIVE

```
GET agent-captains-funnel-events-*  by week
2026-09-07 throttled 32 | 09-14 throttled 13 | 09-21 throttled 1 | 09-28 throttled 6 | 10-05 throttled 22
10-12 throttled 6, runs 3 | 10-19 runs 42, filed 1 | 11-02 runs 7, filed 2
latest throttle: {'current_count': 10, 'threshold': 10} at 2026-10-13T23:50
```

For 5 weeks the cap blocked all filing. FRE-1611 records why: the cap counted 4 tickets that the in-turn tool filed. It is fixed. `promotion.py` also skips `eval_mode` entries by design (FRE-523), so no eval turn can reach a ticket through this path.

### G7: why no outcome reads `shipped`

**Verdict:** POSITIVE

`_classify_outcome` in `brainstem/jobs/outcome_ingestion.py` returns `shipped` only for state `Done`. I read the state of each of the 16 tickets in Linear:

| Tickets | State | Canceled on | ES `ticket_outcome` |
|---|---|---|---|
| FRE-423, 424, 428 | Canceled | 2026-06-26 | canceled-as-noise |
| FRE-430 | Canceled | 2026-06-26 | none |
| FRE-622, 629, 870 | Canceled | 2026-08-18 | none |
| FRE-1373, 1378, 1404, 1412, 1418, 1429 | Canceled | 2026-10-14 | canceled-as-noise |
| FRE-1693, 1725, 1726 | Needs Approval | | none |

So 0 of 16 reached Done, and `shipped` cannot appear. Master canceled the last 6 on the owner's word on 2026-10-14 (comment on FRE-1378), because each rested on a false input (FRE-1608). They read `canceled-as-noise` and not `owner-rejected`, because that value needs the `Rejected` label and they do not carry it. FRE-1610 removed the suppression gap, not this label difference, so the outcome text understates the owner's part.

```
agent-logs-*  sysgraph_outcome_ingested: 6 rows, all at 2026-10-14T08:00, all canceled-as-noise
agent-logs-*  outcome_ingestion_completed: 108 rows. (scanned, ingested): (0,0) 49, (6,0) 40, (1,0) 8, (2,0) 4, (3,0) 4, (5,0) 2, (6,6) 1
sysgraph.ticket: 3 rows (FRE-1693, 1725, 1726).  sysgraph.outcome: 0 rows
```

`sysgraph.outcome` is empty because FRE-1609 cleared the sysgraph state on the owner's decision of 2026-10-14. This settles the disagreement in FRE-1727 F8: FRE-1611 and the stamps were both right at their own dates. I did not find the job that stamped the 3 May tickets.

**The limit.** The 3 open tickets are the only ones filed after the input fix. Eight proposals and 11 reflections exist since 2026-10-15. I did not check their premises against telemetry, and the owner has not decided them. FRE-1693 has waited 12 days.

### G8: the cost of reflection, separated from `session_summary`

**Verdict:** POSITIVE

`api_costs` holds no role column, so I classified each row with `purpose='captains_log'` from 2026-10-06:

- A row is a reflection if its trace holds `reflection_prompt_manifest_built` in `agent-logs-*`.
- Otherwise it is a session summary if a `session_summary_started` or `session_summary_generated` event lies within 180 s.
- For a reflection, the eval flag comes from the capture of that trace.

```
30 days, 199 rows, $8.50:
reflection, eval turn   141 rows  $6.84
reflection, real turn    29 rows  $0.90
session summary          29 rows  $0.76   (28 session_summary_generated events)
other                     0 rows
```

All `captains_log` rows concentrate on 2026-10-09 (87) and 2026-10-10 (56). I did not split those two days by eval flag. FRE-1608 defect 6 explains the rate: eval turns bypass the cadence gate. The time join is an inference, not a key match, because the summary call opens its own trace. A row with a late log event can move between the classes. The result is too lopsided for that to change the conclusion.

**The rows that FRE-1737 asked about.** From 2026-10-24 there are 17 rows. The first 2 (05:34 and 06:15 on 2026-10-24, on `gpt-6.1-sol`) are reflections, from before the flag went false that day. The other 15, which include 2026-10-25, 2026-11-02 and 2026-11-05, are session summaries. Reflection itself has no row after 06:15 on 2026-10-24.

**Correction to FRE-1727 F11.** I wrote that reflection "may be much lower" than the column total. It is 91% of it. The separate fact that holds is that real-turn reflection costs about $0.03 per call, so about $0.90 per 30 days at today's use. The July row of 428 calls and $14.13 fell in the week of the cost halt. I did not classify it.

### G9: the path that produces accepted tickets

**Verdict:** POSITIVE for the counts. The classes are my reading, with one reader.

I read the first 105 characters of all 248 owner follow-up messages since 2026-07-27, and 42 are complaints or corrections. They fall in 19 of the 75 multi-turn sessions, with 3 in July, 29 in August, 3 in September, 7 in October and 0 in November so far. I give no text here, by the rule on owner turns. All 14 zero-rated turns sit in those 19 sessions, so a zero rating alone catches 4 of 19 sessions.

| Class | Complaint turns | Dates | Ticket by title and date | State |
|---|---|---|---|---|
| Invented fact or invented action, with no tool call behind it | 22 | 07-30 to 09-05 | ADR-0138, accepted 08-23. FRE-1282, FRE-1283 (08-23). FRE-1327 (08-29). FRE-1416, FRE-1417 (09-05). | Done, except FRE-1327 Approved |
| Identity or memory mixed up | 8 | 07-28 to 08-06 | FRE-1150 (08-04), which cites the turn's trace. FRE-1153 (08-05). | Done |
| Wrong date or time | 4 | 07-28, 08-05, 10-23 | None found by a title search | |
| Turn ended with no answer | 3 | 08-05, 10-25 | FRE-1676 (10-24), FRE-1697 (10-25) | Awaiting Deploy, Done |
| Artifact does not work | 3 | 10-24, 10-25 | FRE-1677, FRE-1678, FRE-1679 and ADR-0159 (10-24) | Awaiting Deploy, Done, In Progress |
| Off-target answer or missing source | 2 | 10-23, 10-24 | FRE-1662 (10-23) covers the source. None found for the off-target answer. | Done |

I searched Linear by words and read titles and dates. Only FRE-1150 and FRE-1676 to FRE-1679 were checked against the trace. A title match is not proof that a complaint caused the ticket. Even so, the pattern differs clearly from the automated path, which has 0 accepted of 16.

**What this path depends on.** The owner must notice, and must relay it. FRE-1327 records a case where the owner read a fabricated analysis and believed it. A second agent caught it. So this path has a known blind spot, which is the defect that the owner does not see.

### G10: the pilot's transcripts

**Verdict:** POSITIVE

I checked master's reading of the top labels against the transcripts in the run directory, instead of the ranking. All 32 tasks: 29 `satisfied`, 3 `max_turns`, and 22 single-turn.

```
multi-turn tasks: 10
tasks where the assistant's text states the role swap (regex on "swapped sides", "pasted back", "my own message", "mine to supply" and similar): 6 of 10
all 3 max_turns tasks are among the 6
simulator feedback_type over all turns: satisfied 29, followup 23, correction 8, add_context 2, too_shallow 1
```

I read the first two simulated messages of each of the 10 tasks. In the 6 tasks, the simulated user asks the assistant to supply the user's own facts. This confirms master's reading: most of `repeated_request` is a simulator fault. Of the 8 `correction` turns, 1 is defect A (run A, user_015, episode 3), 1 is the case where a simulated user denies recalled items, and most of the rest come from the role swap. So a correction is a usable pointer in eval as well, with the same noise.

**Defect B in production.** Master did not check this. The scan is on replies, in Python, over the capture rows of G2:

```
"memory_recall" in reply: 0 of 471
"search_memory" in reply: 2 of 471
"memory recall" in reply: 1 of 471 (I read it: a statement about a recalled result, not a false tool error)
pilot transcripts with "memory_recall": run B, user_015, episodes 1 and 4
```

- **Arm 1 (live producer):** `MEMORY_RECALL_TOOL = "memory_recall"` at `orchestrator/untrusted_channel.py:24`, in the deployed code path that adds the exchange. The two pilot tasks prove that the path produces this text. They are in the eval store, not this one, so they do not satisfy the same-store form.
- **Arm 2 (same query, identifier varied):** `search_memory` returned 2.
- **Arm 3 (scope):** non-eval replies since 2026-07-27, as this literal string. A false report in other words is not covered.

So the false report has not appeared in these words in production. FRE-1740 remains valid, since the mechanism exists.

**Volume.** A pilot of $2.10 gave 2 tickets that the owner approved, and it found defects that real use did not surface. The stop rule ended this simulator (FRE-1728), so no more volume comes from this source.

### G11: two free checks that I tried, and grounding

**Verdict:** POSITIVE for the counts

By the owner's rule of 2026-11-04, I tried these on real data before P1.

**A regex for correction wording in the next user message.** 6 hits in 264 follow-up turns of all users, of which I read all 6. Five are true pointers. It misses 37 of the 42 complaints that I found by reading, because complaints use many forms.

**A regex for a reply that claims an action with no tool in `tools_used`.** 7 hits in 158 no-tool owner replies, against 20 in 237 tool-using replies. I read all 7. Two are true (2026-09-05), and the owner had already caught those. One is a candidate (2026-09-11), and `tools_used` undercounts worker activity, so I cannot call it. Four are false. It found no new defect.

**Grounding.** **UNVERIFIABLE:** whether the grounding record separates complaint turns from other turns. Only 2 of the 42 turns before a complaint hold a grounding record, because most complaints predate it and it stopped on 2026-10-18. Of the other 109, 91 have `passed_count` 0, so the field as recorded separates little. I searched owner captures since 2026-07-27 on the `grounding` field. FRE-1727 F10 records that I could not find when the mode went off.

## Answers to the inputs of the ticket

| Input | Result |
|---|---|
| Reflection, insights, polling and context governance are off | True, G1 |
| ADR-0127: 1,957 reflections, 7 linked tickets | Today 1,648 reflections in Elasticsearch, 16 linked, 0 accepted (G7). The earlier index held more, and retention removed part of it. |
| `analyze_feedback_patterns` has no caller | Still true at the deployed hash (FRE-1727 F9). Turning polling on gives outcomes with no analysis. Outcome ingestion already reads ticket state, so polling adds little. |
| `captains_log` rows on 10-24, 10-25 and 11-02 | Session summaries, except 2 reflections early on 10-24 (G8) |
| Separate reflection cost from `session_summary` (F11) | Done, G8 |
| Explain why no outcome reads `shipped` (F8) | Done, G7 |

## What can overturn this result

- The owner accepts FRE-1693, FRE-1725 and FRE-1726. Then reflection on fixed inputs has value for the infrastructure class, which is not the class this study is about. Both can be true.
- A reply-aware reflection might catch conversation defects. I did not test it, because that needs paid calls. Its real-turn cost is small (G8), so cost does not rule it out. FRE-1628 shows that stored turns can carry such a test.
- The trial in P1 finds only what the owner had already relayed. Then a read adds nothing, and the honest answer is that the owner's own relay is the loop. The stop rule measures this.

## Method appendix

**Stores.** Production Elasticsearch on `:9200`, read through `_search` and `_count`. Production Postgres `api_costs` and the `sysgraph` schema, by `psql` as the `agent` user, read-only statements only. Eval Elasticsearch on `:9202` and eval Postgres on `:5434`, which were already up, read-only. The gateway container, for flags and file hashes. Linear, by the MCP tools. The pilot run directory on the VPS. No live turn, no paid call, no Mac use, no eval stack start. No owner text, trace id or user id appears in this document, by the rule on owner turns.

**Eval store result, not used as a finding.** During the pilot, eval `agent-captains-reflections-*` gained 0 documents, because reflection is effectively off on that overlay. So the pilot cannot test reflection directly. Eval `api_costs` shows 18 `captains_log` rows at $0.409, which are session summaries at about $0.023 each. That agrees with G8.

**Errors of my own, found and fixed.**

- Top-level `trace_id` on reflections is absent. The first join returned 0 of 471. The right field is nested `telemetry_refs.trace_id`.
- `api_costs.trace_id` is a dashed UUID, and Elasticsearch holds it with no dashes. The first cost join returned 0 of 1,036. A reflection marker from the document join then found 111, and most rows had no document. `reflection_prompt_manifest_built` in `agent-logs-*` gave the classification in G8.
- A join of captures on trace returned more documents than cost rows, because one trace can have more than one capture. I counted by row.

**Rejected.**

- Using the pilot's rank as the input. It mixes units and holds the role swap (FRE-1728).
- Treating all non-eval rows as the owner's. One user id holds 395 of the 471 rows, and I used it for the owner counts. The next largest id holds 34.
- Printing or storing the complaint list outside the scratch directory, which is private to this session.

**Identifier checks.** `event_type` values were read from aggregations before use. Field names were read from the index mapping.

**Provisional counts.** Every Elasticsearch count here may be low (FRE-1051). The per-call series comes from `api_costs`.

## Filed tickets

- FRE-1741 (Backlog): the 30-day read trial in P1.

## Proposals

**P1. Keep reflection, insights and feedback polling off, and run a 30-day trial of one weekly read by a seat. Accept or reject.**

What it does:

1. Each week, master runs one `_count` of zero ratings since the last read, and checks whether an eval run ended.
2. If either is non-zero, the explore seat reads the zero-rated turns, the follow-up user messages of the period and any new eval transcripts.
3. The seat files at most 3 tickets in Backlog, with the trace id and the measured cause. The owner accepts or rejects each one.
4. In the same step, the owner decides FRE-1693, FRE-1725 and FRE-1726, because they are the only sample of reflection on fixed inputs.

Why this one:

- It needs no code and no paid call. It is the shape that already worked (G9, G10), and it gives the zero rating its first consumer.
- Each read is small. This study read 248 short messages, which were 42 complaints.
- It costs nothing to stop.

Stop rule: after 30 days or 4 reads, count the accepted tickets that no earlier owner message had raised. If the count is 0, stop. Then the loop is the owner's own relay, and no stage adds to it.

What it does not do: it does not raise volume. Real use is low (G2), and the pilot's simulator is stopped. It will not by itself produce the major advances that the owner asked about.

Ticket: FRE-1741.
