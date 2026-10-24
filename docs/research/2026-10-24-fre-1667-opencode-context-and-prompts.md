# FRE-1667 — OpenCode: auto compaction, long-context management, and the prompts and skills that run the harness

> **Ticket:** [FRE-1667](https://linear.app/frenchforest/issue/FRE-1667) · **Date:** 2026-10-24 · **Seat:** explore · **Paid cost:** $0 · **Commission:** master, on the owner's request of 2026-10-24 · **Upstream:** [sst/opencode](https://github.com/sst/opencode) at commit `20a6cc72917586eb5314be075f96dbb21d16c480` (2026-10-24, packages at version 2.1.4) · **License:** MIT

## Question

The owner, verbatim: "I am very impressed with OpenCode and its auto compression and long context management. Very impressive. Again, have a session study it and the integrated prompts and skills they use to run the harness."

The study asks four things:

1. How does OpenCode compact a long session: trigger, what it keeps, how it summarizes, and how it protects the prompt cache?
2. How does it manage long context: windowing, tool output, token accounting?
3. How are its system prompt, instructions, agents and skills layered?
4. Which of these ideas fix a problem that our production system shows today?

The fourth question is the test. A mechanism that is excellent in OpenCode is worth porting only if our sessions reach the condition it handles.

## Summary for the owner

- **OpenCode's compaction is well built** (G1). It summarizes the whole conversation into a fixed seven-section checkpoint and keeps the newest 15,000 tokens word for word. It sends the summary request as the normal conversation plus one appended instruction, so the request reuses the prompt cache. Later compactions update the checkpoint instead of starting again.
- **Our main conversation never gets near a context limit** (G2). In 30 days the largest primary request was 58,707 tokens, and the smallest window in use was 131,072. Stage 7 trimmed nothing in 224 turns, and our own compression never fired. OpenCode's trigger would not have fired either.
- **What we do instead is drop old turns by count** (G3). We keep the last 20 messages and drop the rest with no summary, even when they would fit easily: the history we keep is at most about 17,000 tokens. 5 of 54 recent sessions passed 20 messages. Our telemetry cannot see these drops (FRE-1673). This is the place where OpenCode's design helps us. The decision already has a ticket, FRE-1134, and this study gives it a concrete design to test.
- **Workers are where context really grows** (G4). One worker call reached 126,852 tokens, and 2 of 122 worker runs stopped on the context limit in 30 days. That bears on FRE-1138. Two in a month is too few to justify a new build now.
- **Our cache layout already matches OpenCode's idea** (G6). The system prompt stays fixed, and per-turn content goes into the user turn. The cacheable prefix changed on only 3 of 164 consecutive turns.
- **Prompts and skills are close to parity** (G7). OpenCode's base system prompt is 756 bytes, and its skill list carries only an id, a name and a description, loaded by a tool. Our ADR-0156 design is the same shape.

| Proposal | Ticket | Who decides |
|---|---|---|
| Q1 — test OpenCode's checkpoint design as the compression arm of FRE-1134's window experiment | FRE-1134 (comment) | owner and adr, through FRE-1134 |
| Q2 — make the history telemetry see the window drop messages | FRE-1673 | master (bugfix) |
| Q3 — do not port background compaction, native compaction or the tool-output file store now | none | master |

## Findings

Every upstream claim is a code reading of OpenCode at commit `20a6cc72917586eb5314be075f96dbb21d16c480`, cited as `path:line`. We do not run OpenCode, so an upstream claim is about what the code does. Every claim about our system is a measurement against production, unless the finding says otherwise. Elasticsearch counts are provisional (FRE-1051).

### G1 — How OpenCode compacts a session

**Verdict:** POSITIVE (upstream code reading).

| Part | OpenCode at `20a6cc72` | Path |
|---|---|---|
| Trigger | The estimated context reaches the ceiling: the model's input window minus a reserve of 15% of the window, or 16,000 tokens if that is more. | `packages/core/src/session/compaction.ts:120-132`, `:409-418`, `:1104-1112` |
| Token count | The last response's provider-measured prompt tokens (input + cache read + cache write) for the current provider, plus a local estimate of the messages after it. | `compaction.ts:1067-1102`, `:1122-1127` |
| What it keeps | The newest `keep` tokens, 15,000 by default, word for word, starting on a user message. | `compaction.ts:255`, `:954-996` |
| Summary model | The session's own model, unless `agents.compaction.model` names another. | `compaction.ts:264-282`; `services/www/src/docs/content/compaction.mdx:91-105` |
| Summary request | The whole conversation, unchanged, plus the summary instruction as one appended user message. The prefix is the normal request's prefix, so the provider's prompt cache serves it. | `compaction.ts:420-439`, `:459-467`, `:486-500` |
| Summary form | A fixed template: Objective, Requirements, Decisions, Work State (Completed, Active, Blocked), Next Move, Relevant Files (at most 15), Important Context. Rules: terse bullets, keep exact paths, commands, error strings and identifiers, carry forward only open questions, keep the commit and review state. | `compaction.ts:159-198` |
| Instructions left out | The summary leaves out the system setup and instruction files, because "the next agent receives current versions of all of these separately". | `compaction.ts:202` |
| Template check | A reply without the template headings gets one reminder. A second miss is a failed compaction. | `compaction.ts:233-234`, `:455-484` |
| Later compactions | Update the existing checkpoint: "Newer history always takes precedence… Preserve previous information unless newer history clearly contradicts, supersedes, resolves, or makes it stale." | `compaction.ts:214-230` |
| Result | One user message `<conversation-checkpoint><summary>…</summary><recent-context>…</recent-context></conversation-checkpoint>` after the system prompt. | `compaction.ts:429-435` |
| Output room | At least 32,000 tokens for the summary, or the room left if less. | `compaction.ts:132`, `:1138-1151` |
| Too-long rejection | Shrink to 85%, 70%, 50%, then 35% of the rejected size. If the conversation still does not fit, shorten each exchange in steps: tool outputs cut to 2,000 characters, then removed, then user text only, then the exchange left out. One retry. | `compaction.ts:120`, `:133-143`, `:296-300`; `compaction.mdx:198-206` |
| Background compaction | Experimental and off by default. At 85% of the ceiling, write the summary in the background from the cached prefix, hold it, and swap it in when compaction is due. | `compaction.ts:146-157`, `:321-376`; `compaction.mdx:107-136` |
| Native compaction | Optional: an OpenAI Responses endpoint returns an encrypted checkpoint. | `compaction.ts:506-520`; `compaction.mdx:138-175` |
| Tool-output pruning | Removed. The v2 config warns that `compaction.prune` and `tail_turns` are unsupported. | `packages/core/src/config/normalize.ts:352-353`; `services/www/src/docs/content/migrate-v1.mdx:291-292` |

The design has one central idea: never rewrite the cached prefix until compaction is due, and make the compaction request itself a cache hit.

### G2 — Our primary conversation never comes near a context limit

**Verdict:** POSITIVE for the sizes. NEGATIVE for "our within-session compression fired", with its arms below.

**Query 1 — request sizes** (`agent-logs-*`, `event_type: model_call_completed`, 2026-09-24 to 2026-10-24, grouped by `role`; script `tokens.py`, method appendix):

```
role                     calls     p50     p90     p99     max  cache_read/input
sub_agent                  949   16504   65105  108251  126852              0.82
primary                    590   18218   29978   44311   58707              0.76
span_extraction            184    3250    5460   10729   13052              0.67
entity_extraction           74    4471    6018    7815    7815              0.32
```

**Query 2 — the windows the primary ran with** (`context_budget_applied`, same window, terms on `max_tokens`): `131072: 203`, `200000: 10`, `1050000: 7`, `1000000: 4`.

**Query 3 — trimming** (`context_budget_applied`, same window): 224 events, `trimmed: true` on 0, `overflow_action` null on all 224, largest `total_tokens` 17,551.

**Corroboration** (Postgres `api_costs`, last 30 days, `purpose = 'main_inference'`, cloud calls only): 120 calls, max `input_tokens` 117,061. That max is the worker call in Query 1 with the same count (`openai/gpt-6.1-sol`, 2026-10-20 19:54). The ledger records cloud calls only, so it confirms the cloud rows and says nothing about local ones.

**What it means.** Our hard trigger fires at 0.85 × the model's window (`src/personal_agent/orchestrator/within_session_compression.py:378-385`, `src/personal_agent/config/settings.py:1273-1274`). On the smallest window in use that is 111,411 tokens. The largest primary request was 58,707. OpenCode's ceiling on the same window would be 131,072 − 19,660 = 111,412 tokens. Neither trigger can fire on our primary traffic.

**The negative: within-session compression did not fire in this window.**

- **Query:** `_count` on `agent-logs-*` for `event_type` in `within_session_compression_completed` and `within_session_compression_recorded`, 2026-09-24 to 2026-10-24, and all time.
- **Output:** 0 and 0, in the window and all time.
- **Arm 1 (1b, live producer):** the emit `within_session_compression_completed` is at `src/personal_agent/orchestrator/within_session_compression.py:532`. The deployed file matches the repo by SHA-256 (`7dc6e3b0911a`). Its enclosing path is the primary LLM step, whose sibling emit `model_call_completed` with `prompt_callsite: orchestrator.primary` returns 524 in the same window. No raw instance exists in this store under the current name.
- **Identifier history:** FRE-1131 (2026-08-03) counted this mechanism under the older names `within_session_compression_hard_trigger` and `within_session_compression_recorded`. Both return 0 in every index today, and the hard-trigger name has no emit in `src/`. I did not find where the earlier 260 events went. A retention cut or a rename before 2026-07 can explain it, but I did not prove either.
- **Arm 2 (path liveness):** the same `_count`, index and window with `event_type: cache_reset_decision` returns 224.
- **Arm 3 (scope):** index `agent-logs-*` on the production host, 2026-09-24 to 2026-10-24. The two eval gateways write to another Elasticsearch (FRE-1666 F4). The verdict is "did not fire in production in these 30 days". The sizes in Query 1 explain it.

### G3 — We drop old turns by count, with no summary, and the telemetry cannot see it

**Verdict:** POSITIVE.

**The mechanism (code, deployed by hash).** `src/personal_agent/service/app.py:2387-2389` keeps `db_messages[-max_history:]`, and `:2528-2529` hands that list to the session manager. `conversation_max_history_messages` defaults to 20 (`src/personal_agent/config/settings.py:1228-1232`). The live gateway sets `AGENT_CONVERSATION_MAX_HISTORY_MESSAGES`, and its value equals 20 (checked by comparison, not printed). Nothing summarizes the dropped messages for the conversation. The session digest is written after 900 s idle, for long-term memory, not for the next turn (`src/personal_agent/config/settings.py:2911`).

**Query 1 — sessions longer than the window** (Postgres `sessions`, `last_active_at` in the last 30 days, `purged_at is null`):

```sql
with s as (select session_id, jsonb_array_length(messages) n from sessions
  where last_active_at >= now() - interval '30 days' and purged_at is null and jsonb_typeof(messages)='array')
select count(*), count(*) filter (where n > 20), count(*) filter (where n > 40),
  percentile_disc(0.5) within group (order by n), percentile_disc(0.9) within group (order by n), max(n) from s;
```

**Output:** `54 | 5 | 0 | 4 | 20 | 40`. So 5 of 54 sessions passed the window, and the longest held 40 messages.

**Query 2 — the size of the history we keep** (`conversation_context_loaded`, 2026-09-24 to 2026-10-24): 224 events. `estimated_tokens` p50 2,067, p90 9,509, max 17,041. `total_messages_in_db` p50 6, p90 20, max 20. `messages_truncated > 0` on 0 events.

**Why the telemetry reads 20 and 0.** `total_messages_in_db` is `len(ctx.messages)` after the cut (`src/personal_agent/orchestrator/executor.py:5459-5463`). On the gateway path, `messages_truncated` is a literal 0 (`executor.py:5676-5682`). The event cannot show a drop. Postgres shows sessions of up to 40 messages. Q2 (FRE-1673) fixes the instrument.

**What it means.** We cut the conversation at about 17,000 tokens in a window of 131,072 or more. A dropped turn returns only if memory recall happens to find it. OpenCode keeps 15,000 tokens word for word and summarizes everything older into a checkpoint, so nothing leaves the request without a trace. FRE-1134 (Approved) already asks for this decision through an experiment. It names an arm C, "larger window with compression thresholds lowered so mechanisms B/D genuinely engage". G1 gives that arm a concrete, tested design (Q1). FRE-1134's own numbers are older: it cites a window of 10 at `settings.py:932`. The window is now 20.

### G4 — Worker context is where size really grows; 2 of 122 worker runs hit the limit in 30 days

**Verdict:** POSITIVE.

**Query 1:** the `sub_agent` row of G2 Query 1: 949 calls, p90 65,105, p99 108,251, max 126,852. 99 calls of all roles had 64,000 input tokens or more, and 21 had 100,000 or more. All 21 are `sub_agent` calls.

**Query 2** (`sub_agent_forced_synthesis`, same window, terms on `stop_reason`): `completed: 79`, `cap: 2`, `context_reserve: 2`. The two `context_reserve` stops have matching `sub_agent_landing_reserved` events:

```
2026-10-10T12:07:16 reason=context estimated_prompt_tokens=119294 context_length=131072 tool_iterations=10
2026-10-10T14:24:18 reason=context estimated_prompt_tokens=123871 context_length=131072 tool_iterations=8
```

`sub_agent_start` returns 122 in the same window.

**What it means.** The worker loop does not compact. It stops and writes its report when it nears the window. That happened twice in 30 days, both on 2026-10-10, a heavy test day on a local 131,072-token model. OpenCode would compact inside the loop instead. FRE-1138 (Approved) already owns "within-turn growth is the ungoverned axis". Two stops in a month is too few to justify a build now (Q3).

### G5 — Our compression request is a separate, small, cache-cold call

**Verdict:** POSITIVE (code reading on both sides, deployed by hash on ours).

| Part | Ours | OpenCode |
|---|---|---|
| Summary model | role `compressor`, bound to `gpt-5.4-mini` (`config/model_roles.yaml:119`) | the session's model by default (G1) |
| Request shape | its own system prompt plus the evicted messages as formatted text (`src/personal_agent/orchestrator/context_compressor.py:255-262`). No shared prefix, so no cache reuse. | the normal request plus one appended instruction: a cache hit |
| Size | "Maximum 200 words total", `max_tokens=320` (`context_compressor.py:131`, `:258`) | at least 32,000 output tokens of room |
| Form | four bullets: Decisions, Entities, Facts, Open Items (`context_compressor.py:118-131`) | seven sections, including Work State and Next Move |
| Later runs | the whole summary is rewritten, with the earlier summary folded in as input | the checkpoint is updated in place, newer history wins |
| Placement | an `assistant`-role message (`src/personal_agent/orchestrator/within_session_compression.py:50`, `:215`) | one user message wrapped in `<conversation-checkpoint>` |

This mechanism never fires today (G2), so these differences cost nothing now. They matter the day FRE-1134 chooses to summarize. A 200-word, four-bullet summary written cold by a small model is a weaker carrier than OpenCode's checkpoint. Q1 carries the OpenCode shape into the experiment.

### G6 — Our cacheable prefix is stable across turns, as OpenCode's is

**Verdict:** POSITIVE.

**OpenCode.** The system prompt is fixed for an "epoch". Instruction files and the skill list are frozen as a baseline at the start of the epoch (`packages/core/src/session/instruction-state.ts:119-137`, `:172-182`). A later change goes into history as a delta message (`instruction-state.ts:38-61`; `packages/core/src/skill/instructions.ts:35-64`). The baseline moves only at compaction (`instruction-state.ts:139-154`, called from `packages/core/src/session/projector.ts:759`).

**Ours.** The system prompt is stable, and the per-turn content (date, highlights, planning note) goes into a `<turn_context>` block at the start of the user turn. Memory arrives as a tool exchange (`src/personal_agent/orchestrator/executor.py:7344-7422`).

**Query** (`model_call_completed`, `prompt_callsite: orchestrator.primary`, 2026-09-24 to 2026-10-24, the first call of each turn, consecutive turns in one session):

```
QUERY model_call_completed prompt_callsite=orchestrator.primary rows 524
turn pairs=164 hash changed=3 (model changed 0) gaps={'>=60min': 3} flips back next turn=0
next turn first-call cache_read/input: unchanged 0.67 (n=161) changed 0.26 (n=3)
```

The prefix changed on 3 of 164 turn pairs, each after a gap of an hour or more. When it changed, the cache-read share of the next turn fell from 0.67 to 0.26. That is the cost OpenCode's epoch design avoids, and we already avoid it on 98% of turns. No proposal.

### G7 — System prompt, agents and skills: close to parity with ADR-0156

**Verdict:** POSITIVE (upstream code reading; our side cited).

- **Base prompt.** OpenCode's whole default system prompt is 756 bytes (`packages/core/src/session/runner/prompt/system.txt`). It adds tool guidance only for tools that the agent holds: shell rules only with `shell`, edit rules only with `edit` (`packages/core/src/session/system-prompt.ts:9-27`). The request's system part is that prompt plus the epoch baseline (`packages/core/src/session/model-request.ts:92-111`). FRE-1592 studied our own split between the system prompt and skills, so this study does not repeat it.
- **Agents.** Built-in `build` and `plan` (primary), `general` and `explore` (subagents), and hidden `compaction`, `title` and `summary` agents (`services/www/src/docs/content/agents.mdx:133-158`). Each agent is a permission rule set plus an optional model and prompt.
- **Skills.** A `SKILL.md` with `name` and `description`. The model sees a sorted list of id, name and description, never the body (`packages/core/src/skill/instructions.ts:16-33`, `:79-96`). The `skill` tool loads the body, the skill's base folder and up to ten supporting file paths (`services/www/src/docs/content/skills.mdx:182-205`). A per-skill permission is `allow`, `ask` or `deny` (`skills.mdx:207-226`). `opencode/autoinvoke: false` hides a skill from the list but keeps it loadable by id (`skills.mdx:148-157`).
- **Ours.** The catalog lines are name and description in the system prompt, the body arrives only through `read_skill`, and a worker sees the skills its tools provide (ADR-0156; `src/personal_agent/tools/read_skill.py:1-9`; FRE-1616).

The two designs are the same shape. The one OpenCode feature we lack is the skill-list delta message: a change to the list mid-session appends a message instead of changing the system prompt. Our skills change only at deploys (G6 saw 3 prefix changes in 30 days), so the gain is negligible. No proposal.

### G8 — Tool output: OpenCode stores the full output; we cut it and keep nothing

**Verdict:** POSITIVE for the code on both sides. UNVERIFIABLE for how often our cut fires.

**OpenCode.** A tool result over 2,000 lines or 50 KiB is cut, and the full output is written to a file kept for 7 days. The marker names the path: `[showing lines 1-N of M; full output saved to …]` (`packages/core/src/tool-output.ts:13-16`, `:69-120`).

**Ours.** A result over the tool's `max_result_tokens` (25,000 at most) becomes `{"truncated": true, "partial": …, "next_action": …}` (`src/personal_agent/tools/types.py:15`, `src/personal_agent/tools/contract.py:219-300`). The full output is not stored. The ADR-0085 store with `expand_tool_result` exists but is off (`tool_result_compression_enabled=False`, `src/personal_agent/config/settings.py:1330-1334`; ADR-0085 is Parked).

**Why the frequency is UNVERIFIABLE.** No event records the cut. A search of every `event_type` in the window for `truncat`, `clip` or `oversize` returns only `skill_bodies_truncated: 6` and `sub_agent_output_clipped: 1`. FRE-1666 F9 measured the one cut with data behind it: `fetch_url` loses 2.5% of supported facts. No proposal.

## Method appendix

**Upstream.** `git clone --depth 1 https://github.com/sst/opencode.git` into the session scratchpad, outside the repo. Commit `20a6cc72917586eb5314be075f96dbb21d16c480`, 2026-10-24T12:20:37Z, packages `@opencode/core` and `@opencode/cli` 2.1.4. No install script and no OpenCode code ran. The v2 docs in `services/www/src/docs/content/` were read as the maintainers' description, and each claim used here was checked against the code where the code exists. The older v1 docs in `packages/web/src/content/docs/` describe `prune` and `reserved`. They are superseded and were not used.

**License.** MIT (`LICENSE`, "Copyright (c) 2025 opencode"). The summary template and rules (`compaction.ts:159-198`), the update prompt (`:200-231`) and the skill-list wording (`packages/core/src/skill/instructions.ts:25-64`) may be copied into this repo if the MIT notice comes with them. Q1 needs the template. A copy must carry the notice, or the template must be rewritten in our own words.

**Our side.** Code at `main` `0ff3dd12`. A read-only subagent mapped the compaction, cache, budget and skill code with `file:line` citations. I checked its key citations by hand: `config/model_roles.yaml:119`, `context_compressor.py:118-131`, `:255-262`, `within_session_compression.py:378-385`, `settings.py:1228-1232`, `:1273-1274`, `:1330-1334`, `request_gateway/budget.py:162-170`, `service/app.py:2387-2389`, `:2528-2529`. Deployed identity by SHA-256 against the production gateway: `orchestrator/within_session_compression.py` `7dc6e3b0911a`, `orchestrator/executor.py` `743abe37fba0`, `service/app.py` `b954f571f0d8`. All match.

**Stores and windows.**
- Elasticsearch `agent-logs-*`, `_search` and `_count` only, 2026-09-24 to 2026-10-24 (about 12:45 UTC). Events: `model_call_completed`, `context_budget_applied`, `conversation_context_loaded`, `cache_reset_decision`, `context_assembled`, `sub_agent_forced_synthesis`, `sub_agent_landing_reserved`, `sub_agent_start`, and the compression names in G2. Provisional (FRE-1051).
- Postgres `cloud-sim-postgres`: two read-only `select`s, on `api_costs` and `sessions`. The owner approved the second Postgres query of this session after first denying it.

**Scripts** (session scratchpad, not committed): `es.py`, `tokens.py`, and inline `_search` scripts for G3, G4 and G6.

**Identifier resolutions.**
- The compression events of FRE-1131 (`within_session_compression_hard_trigger`) no longer exist in `src/`. The current names come from `src/personal_agent/orchestrator/within_session_compression.py` and `src/personal_agent/telemetry/within_session_compression.py`.
- `prompt_callsite` has two values for `role: primary`: `orchestrator.primary` (with component ids) and `role.primary` (no component ids). G6 uses only `orchestrator.primary`.

**Rejected readings.**
- **A first G6 count said the prefix changed on 74 of 165 turn pairs.** It compared the first call of each turn across both call sites. The turns that begin on `role.primary` have a different system prompt by design, so every switch between call sites counted as a change. On `orchestrator.primary` alone the count is 3 of 164. The 74 was an instrument error, and this document does not use it.
- **A ratio of the provider-measured input to `context_budget_applied.total_tokens`** (p50 3.44). The two count different things: the budget figure has no system prompt and no tool schemas. It says nothing about estimate error, so it is not reported as a finding.
- **`total_messages_in_db` as a session length.** It reads the list after the cut (G3).

**Not measured.** How often our tool-result cut fires (G8). Whether summaries beat dropped turns for answer quality: that is FRE-1134's experiment, and it needs live turns, which the commission forbids.

## Proposals

At most ten. Three here.

1. **Q1 — run OpenCode's checkpoint design as the compression arm of FRE-1134's window experiment.** No new ticket: a comment on FRE-1134 carries it. The arm:
   - Keep the newest ~15,000 tokens word for word.
   - Summarize everything older into one checkpoint user message, in OpenCode's seven-section template, updated in place on each later compaction.
   - Send the summary request as the normal request plus one appended instruction, so it reuses the cache.
   - Count tokens from the last provider-measured prompt plus an estimate of the new tail.

   Evidence: G1, G2, G3, G5. FRE-1134's existing arms A (window) and B (full history) stay as they are. FRE-1673 must land first, so the experiment can see what the window drops.
2. **Q2 — make `conversation_context_loaded` report the stored message count and the number of messages the window dropped (FRE-1673).** Evidence: G3.
3. **Q3 — do not port three OpenCode features now.** No ticket.
   - Background compaction: it is experimental upstream, and our primary never reaches the trigger (G2).
   - Native provider compaction: it ties a session to one OpenAI model, and our sessions switch models.
   - The tool-output file store: no measurement shows that our cut loses facts often (G8; FRE-1666 F9).
   Reopen when FRE-1134 adopts summaries, or when worker stops on the context limit pass a few a week (G4, FRE-1138).

## Filed tickets

- [FRE-1667](https://linear.app/frenchforest/issue/FRE-1667) — this study's anchor (Backlog). Master commissioned the study without a ticket, so explore filed this one to give the study an id. Filed on 2026-10-24 together with FRE-1666, the anchor of the Hermes study.
- [FRE-1673](https://linear.app/frenchforest/issue/FRE-1673) — Q2 (Backlog).
- Comments, not tickets: Q1 on [FRE-1134](https://linear.app/frenchforest/issue/FRE-1134), and a pointer to G4 on [FRE-1138](https://linear.app/frenchforest/issue/FRE-1138).
