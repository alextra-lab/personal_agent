# FRE-1592 — what belongs in the system prompt and what in skills: a cost model, measured

**Study:** FRE-1592, 2026-10-12. **Seat:** adr (Opus). **Owner direction:** "Shouldn't the decisions be
math based. I would expect there to be obvious answer - not my opinion." Then "go" on the plan: check the
cache instrument, measure the inventory (M1), preflight M2 and M3, write this note and the decision brief.

This document records what was measured and the arithmetic built on it. Recommendations appear only in
**Decision brief**. The owner decides. No prompt or catalog change ships before that (AC-3).

---

## Summary

1. **The placement of a rule is a latency question with one formula.** Content of `s` tokens costs
   `s × c × m` per turn in the head, and `q × (L + s × c)` per turn as a skill. The inputs are measured
   (table below). The head wins when the content applies on at least `m` of the turns. Otherwise it
   wins only below a break-even size `s* = q × L / (c × (m − q))`.
2. **With today's inputs the answer is clear.**
   - The always-on rules (reasoning, grounding, tool rules, identity) stay in the head.
   - The skill bodies stay as skills: in the head they cost 12–83× more.
   - Harness injection has nothing to gain: at `q` = 0.028 it can save at most about 0.05 s per turn.
3. **The system-prompt question is the smallest cache cost in the system.** Moving the reasoning rule
   changes about 0.01–0.09 s per turn. Two larger costs were found on the way:
   - **Head misses after deploys:** `m` = 0.33 in the last 14 days, 23 of 27 after a prefix change.
   - **Follow-up turns prefill the history again:** 2,824 tokens (5.9 s) at the median on turns 2–10,
     and 17,043 tokens (35.8 s) on turn 11 and later, where the history window moves.
4. **sequential-thinking duplicates its rule.** Production loaded it 0 times in 176 turns.

M1 (the measured inventory), M2 (the cache cost of each option on the eval stack) and M3 (rule
following by channel) are **pending**. They run after the owner's OK on the preflight numbers.

---

## The cost model

For content of `s` tokens, the expected latency cost per primary turn is:

| Delivery | Cost per turn | What it buys |
|---|---|---|
| (a) In the head (system prompt) | `s × c × m` | The content is present on every turn |
| (b) A skill, loaded with `read_skill` | `q × (L + s × c)` | The content is present only when the model loads it |
| (c) Harness injection after the head | `q_c × s × c` | The content is present when the signal fires |
| Injection into the head (any per-turn change of the head) | `s_head × c` on every change | Nothing: it discards the cached head |

The head wins over a skill when `s × c × (m − q) < q × L`. If `q ≥ m`, the head always wins.

### Inputs

| Symbol | Meaning | Value | Source |
|---|---|---|---|
| `c` | Prefill time per uncached prompt token | Flash-Next 2.10 ms; Gemma 0.79 ms | Least-squares fit of `wall_s` on uncached prompt tokens and output tokens, 177 FRE-1590 Part 1 calls per model, R² = 0.98 for each. Flash-Next agrees with the independent "~30 s for 13k tokens" of a cold prefill. |
| — | Decode time per output token | Flash-Next 29.8 ms; Gemma 20.8 ms | The same fit |
| `L` | Cost of the extra `read_skill` call | Flash-Next ~1.7 s; Gemma ~1.1 s | ~52 output tokens (median of the `read_skill` first calls) × decode time + the fixed term |
| `m` | Share of turns that start with a cold head | 0.33 (27 of 83); 0.05 without prefix changes | Production ES, `model_call_completed`, role `primary`, Flash-Next, 14 days to 2026-10-12. Head miss = fewer than 2,000 cached tokens on the turn's first call. |
| `q` | Share of turns that load a skill | 0.028 for all skills together (5 loads in 176 turns) | Production ES, `tool_call_started` with `tool_name` `read_skill`, since `read_skill` was first registered (2026-10-01 09:27Z) |
| `s` | Content size | Rules 130–552 tokens; catalog lines 54–138; bodies 850–3,728 tokens | M1, measured with the served tokenizer and chat template |

### Results with these inputs (Flash-Next)

| Case | In the head | As a skill | Result |
|---|---|---|---|
| All 10 skill bodies (18,077 tokens) | 12.5 s per turn (`m` = 0.33); 1.9 s (`m` = 0.05) | 0.15 s per turn | Skills are 12–83× cheaper |
| The reasoning rule (130 tokens), needed on about half the turns | 0.09 s / 0.01 s, present on 100% of the turns | 0.11–0.31 s, present on 11–33% of the turns that need it | The head wins on cost and on coverage |
| One catalog line (54–138 tokens, mean 101) | 0.07 s / 0.01 s per turn | — | The price of offering a skill. A skill that is never loaded pays it for nothing. |

The first draft of this table used `m` = 0.57. That figure counted history misses as head misses. The
check is in **The cache instrument** below.

Break-even sizes at `m` = 0.33: `q` = 0.5 → the head always wins; `q` = 0.1 → about 350 tokens;
`q` = 0.02 → 52 tokens.

---

## The cache instrument

- **The field is the server's own count.** `model_call_completed.cache_read_tokens` comes from
  `usage.prompt_tokens_details.cached_tokens`, which llama.cpp fills from `timings.cache_n`
  (`llm_client/litellm_client.py:1977-2014`). The streaming adapter passes `usage` through unchanged
  (`llm_client/adapters.py:52-62, 407-414`). A count of 0 is logged as null (`:2014`), so null is read as 0.
- **The first reading mixed two effects.** "Less than 50% cached" counted 53 of 93 first calls as misses.
  Many of them reused exactly the head (one session: 10,349 cached tokens on every turn, out of
  ~27,000). That is a history miss, not a head miss. With "fewer than 2,000 cached tokens" as the head
  miss, `m` = 27 of 83 = 0.33. Calls below 2,000 input tokens hold no head and are left out.

### F1 — Head misses: 0.33 of turns, 23 of 27 after a prefix change

27 of 83 turn-first primary calls on Flash-Next reused fewer than 2,000 tokens. In 23 of them the
`prompt_static_prefix_hash` differed from the previous primary turn: a deploy changed the head. The
14 days held 20 distinct prefix hashes, during the ADR-0156 migration. The other 4 came after gaps of
7 to 95 hours.

### F2 — Follow-up turns prefill the history again

| Turn position in the session | n | Uncached tokens on the turn's first call (p50 / p90) | Prefill time (p50) |
|---|---:|---:|---:|
| Turns 2–10 | 26 | 2,824 / 3,771 | 5.9 s |
| Turn 11 and later | 13 | 17,043 / 18,160 | 35.8 s |

Same session, same prefix hash, head cached. 12 of the 13 later turns reused only the head. 2 of 25
sessions reached turn 11.

**Cause, from the code:**
- The service stores the raw user text (`service/app.py:421-428`) and only the final reply text
  (`service/app.py:562-578`). The previous turn's `<turn_context>` block, memory exchange, tool calls and
  tool results are not stored. So the next turn differs from the previous request from the previous
  user message on.
- Production keeps a window of 20 messages (`AGENT_CONVERSATION_MAX_HISTORY_MESSAGES=20`,
  `service/app.py:418-419`). From turn 11, the window drops the oldest pair on each turn. So everything
  after the head changes, and the whole history is prefilled again.

The executor comment that says the stored history "equals the wire form sent now"
(`orchestrator/executor.py`, FRE-434 block) holds within one turn only.

---

## Overlap between the system prompt and the skills (ticket question 2)

| Skill | Overlap | Depth beyond the system prompt |
|---|---|---|
| sequential-thinking | Its "Rules" section restates `REASONING_DISCIPLINE_PROMPT` (FRE-1549) | Estimate the steps, state the current step, mark revisions, name branches, check that the answer holds |
| infrastructure-health, query-elasticsearch, self-telemetry | The Docker DNS addresses of the deployment context | None for that part. Each recipe needs the address, so the repeat is small and useful. |

No skill restates the grounding contract, the parallel-calls rule or the step budget.

sequential-thinking loaded 0 times in 176 production turns, and on 3 of 9 (Flash-Next) and 1 of 9
(Gemma) draws in FRE-1590. With the rule already in the head, "no load" can be the correct behaviour.
Nobody has measured whether its depth adds quality: FRE-1590 Part 2 did not test it.

---

## M1 — the inventory (AC-1)

**Method.** `scripts/eval/fre1592/inventory.py` takes a FRE-1590 capture of the production primary
request (`main 4bc88d2a`, 24 tools). The prompt inputs of that capture are unchanged on `main` today:
the script rebuilds the captured system prompt from the code constants and stops if it differs. It adds
the two production-only sections that the capture gateway lacks: the deployment context and the
operator stanza (placeholder name). It sends each request to the production llama.cpp
`/v1/chat/completions` with `max_tokens` 1 and reads `usage.prompt_tokens`. The server's `/tokenize`
and `/apply-template` answer 403 behind the slm_server router, and master approved this method on
2026-10-12. Two properties of the count:

- it includes the generation prompt that the chat template adds after the last message, as every
  production call does;
- a cached prefix does not change it: `prompt_tokens` counts cached and evaluated tokens alike.

Each component's count is an ablation (the full request minus the request without it). A plain text
(a catalog line, a skill body) is counted as a user message minus an empty user message. The
instrument check: the captured body must count to the `prompt_tokens` that llama.cpp reported for the
same body in the FRE-1590 replay (12,377).

**Result** (2026-10-12 18:40:42–18:43:22Z, 57 calls, run `telemetry/evaluation/fre1592/slot2-20261012-1840`, git-ignored). The instrument check passed: the captured body counts 12,377 tokens, the figure of the FRE-1590 replay. With the production head, the request is 12,617 tokens.

| Component | Tokens | Share | Applies on |
|---|---:|---:|---|
| Tool definitions (24 tools) | 9,979 | 79.1% | every turn |
| Skill catalog (10 lines and a header) | 1,078 | 8.5% | every turn (the offer); the body only on a load |
| Tool-use rules (`TOOL_USE_NATIVE_PROMPT`) | 552 | 4.4% | every turn |
| Grounding contract | 313 | 2.5% | every turn |
| Tool awareness | 178 | 1.4% | capability questions only |
| Deployment context | 153 | 1.2% | infrastructure tasks only |
| Reasoning rule (`REASONING_DISCIPLINE_PROMPT`) | 130 | 1.0% | every non-simple turn |
| Operator stanza | 87 | 0.7% | every turn |
| Conversation (user message, memory exchange) | 132 | 1.0% | — |

The rows sum to 12,602; the other 15 tokens are the joins between sections.

**Tools.** Counted one at a time, the 24 tools sum to 14,510 tokens against 9,979 together: each single-tool request carries about 197 tokens of the shared tool-block header. Net of that header, the largest are `query_telemetry` (~1,440), `web_search` (~920), `bash` (~540), `search_memory` (~510), `artifact_draft` (~490) and `artifact_write` (~490). The smallest is `list_linear_projects` (~130).

**Skills.**

| Skill | Catalog line | Body (what `read_skill` returns) |
|---|---:|---:|
| mermaid-diagrams | 101 | 3,728 |
| self-telemetry | 101 | 3,014 |
| query-elasticsearch | 107 | 2,164 |
| turn-forensics | 117 | 2,066 |
| neo4j-direct | 54 | 1,762 |
| query-tempo | 137 | 1,746 |
| system-metrics | 104 | 965 |
| sequential-thinking | 71 | 927 |
| system-diagnostics | 138 | 855 |
| infrastructure-health | 80 | 850 |
| **Total** | **1,010** | **18,077** |

**What this shows.** The rules that the system prompt holds are 995 tokens (7.9%). Moving all of them out would save less than one tenth of the head. The tools are four fifths of it.

## M2 — the cache cost of each option (pending, preflight first)

**Method.** `scripts/eval/fre1592/channels.py cache`. One skill body (mermaid-diagrams, which loads
on 12 of 12 draws on both models) is delivered five ways. Each arm has its own nonce. Turn 1 is cold,
and turn 2 is measured with llama.cpp `timings`:

| Arm | Delivery |
|---|---|
| `base` | No body. This measures the template's own boundary cost. |
| `head` | The body in the system prompt on both turns |
| `fence` | The body in turn 2's `<turn_context>` block |
| `skill` | Turn 2, the model's own call (does it call `read_skill`?), then a `read_skill` exchange, then one more call |
| `head_turn2` | The body added to the system prompt on turn 2 only. This is the negative control. It must show the full re-prefill. |

The turn-1 history is kept byte for byte as it was sent, so M2 measures the delivery and not F2.

## M3 — rule following by channel (pending, preflight first)

**Method.** `scripts/eval/fre1592/channels.py follow`. One rule is placed in each channel, and a
script scores the reply. Each arm has its own nonce, and its items share the cached head.

| Arm | Where the rule is |
|---|---|
| `system` | The system prompt, after the reasoning rule, as a production rule sits |
| `heavy` | The same, with all 10 skill bodies (18,077 tokens) added to the head. This tests whether a larger head weakens a rule. |
| `skill` | A `read_skill` result, after the user message |
| `fence` | The `<turn_context>` block of the user message |
| `absent` | Nowhere. This is the control: a rule that the control also passes cannot show a channel effect. |

**The rules.** A rule can show a channel effect only if the model does not follow it unasked.

- **Rule A (answer format):** "When your answer uses a search result, end it with one final line
  that starts with `Sources:` and lists every citation marker you used." The turn holds two search
  results with citation markers. The facts and URLs are invented, so the reply can take the facts
  only from the results. Pass = the final answer stops normally, and its last line starts with
  `Sources:` and holds a marker. A further tool call gets a reply in production's shape, for up to
  3 rounds: `fetch_url` gets the ADR-0156 D6 error for HTTP 404, and any other tool gets "No further
  results".
- **Rule B (tool behaviour):** "Before you call web_search for a fact, call search_memory with the
  same query in the same response." The prompts are plain web facts. Pass = the first response
  calls both `search_memory` and `web_search`.

8 items per rule × 3 draws × 5 arms = 120 calls per rule.

**Gateway errors.** `:8600` sits behind a Cloudflare tunnel. A 502, 503 or 504 is retried once, and
the row records `gateway_retries`. An item fails on a gateway error only if the retry fails too.

**Preflight history (both on Flash-Next, 1 item per arm).**

1. 17:09–17:22Z, with the production grounding rule and the parallel-calls rule. Neither rule could
   discriminate. The parallel-calls rule passed in all 5 arms, including `absent`: Flash-Next
   batches independent calls unasked. With the grounding rule, `absent` copied both markers too.
   The arms with the rule searched again instead of answering: the `example.org` URLs looked like
   placeholders.
2. 18:43–18:48Z, rules A and B. `system` did not answer: it called `fetch_url` on the invented
   URLs three times, and the stub's "No further results" did not stop it. `heavy`, `skill` and
   `fence` answered and passed. `absent` got a 502 from the Cloudflare tunnel (master traced it:
   the model finished the request with status 200), and the run stopped there. Rule B did not
   start. The harness changes since then: the production 404 error for `fetch_url`, 3 further
   rounds and the gateway retry.

---

## Validity notes

- **`m` was measured in a period with many deploys** (ADR-0156 migration). Without deploys it is about
  0.05. The arithmetic gives both values.
- **`q` comes from 176 turns, and some were test turns.** The owner is the only user.
- **`c` comes from replayed first calls,** most of them with a small uncached part. The fit is tight
  (R² = 0.98), and it agrees with an independent cold-prefill figure.
- **The FRE-1590 capture gateway had no memory graph,** so its memory result reads "records could not
  be reached". M1 counts the head, which this does not change. M2 and M3 use a fixed short memory
  result.
- **M3 adds the rule as a skill that the catalog does not list.** The `read_skill` exchange is
  synthetic. This tests how the model follows a rule in a tool result, not whether it chooses to load it.
- **M3's rules are not production rules.** Both production candidates were followed unasked
  (preflight 1), so they cannot show a channel effect. Rules A and B are plausible operating rules
  that the model does not follow unasked. A result about them is a result about rule delivery, not
  about the grounding or parallel-calls rules.

---

## Decision brief

*Completed after M1–M3. The recommendations below follow from the measured inputs. M1–M3 can change
the numbers, and M3 is the only result that can change a recommendation.*

1. **Keep the always-on rules in the system prompt** (reasoning, grounding, tool rules, identity).
2. **Remove sequential-thinking from the catalog,** or measure its depth first. Its rule is in the head.
3. **Keep every other skill body as a skill.**
4. **No harness injection.**
5. **Bind the formula in an amendment to ADR-0156 D2,** with its inputs re-measured from telemetry.
6. **The larger levers are F1 and F2.** A fix for F2 (store the wire form, and move the window in steps,
   not one pair per turn) saves 5.9 s on a median turn 2–10 and 35.8 s on turn 11 and later.

---

## References

- FRE-1592; FRE-1590 and `docs/research/2026-10-12-fre-1590-skill-test-qwen-gemma.md` (F1, F2, per-skill
  table, validity notes); FRE-1549; FRE-1577; FRE-1591; FRE-1134 (the conversation window decision).
- ADR-0081 D1/D2 (the cached head and the append-only layout), ADR-0140 T2 (untrusted input only in a tool
  result), ADR-0156 D2 and amendment A5.8 (the cache-safe reveal), ADR-0154 D7 (the replay probe pattern).
- `src/personal_agent/orchestrator/executor.py` (system prompt assembly and the `<turn_context>` block),
  `src/personal_agent/orchestrator/prompts.py`, `src/personal_agent/service/app.py:418-428, 562-578`,
  `src/personal_agent/llm_client/litellm_client.py:1977-2014`.
- `scripts/eval/fre1592/` (this study), `scripts/eval/fre1590/triggering.py`, `scripts/eval/fre1537/replay.py`.
- [Anthropic, Agent Skills overview](https://platform.claude.com/docs/en/agents-and-tools/agent-skills/overview):
  the catalog metadata is always loaded, and the body loads on demand.
