# FRE-1592 — what belongs in the system prompt and what in skills: a cost model, measured

**Study:** FRE-1592, 2026-10-12. **Seat:** adr (Opus). **Owner direction:** "Shouldn't the decisions be
math based. I would expect there to be obvious answer - not my opinion." Then "go" on the plan: check the
cache instrument, measure the inventory (M1), preflight M2 and M3, write this note and the decision brief.

This document records what was measured and the arithmetic built on it. Recommendations appear only in
**Decision brief**. The owner decides. No prompt or catalog change ships before that (AC-3).

---

## Summary

The owner asked three things: do we lighten the system prompt, do we inject skills, and how does each
choice affect the prompt cache. The answers below are measured on the production Flash-Next server.

1. **Lightening the system prompt by moving rules gains little.** The rules are 995 of 12,617 tokens
   (7.9%). The tool definitions are 9,979 (79%). (M1)
2. **A larger head does weaken a rule.** With the 18,077 tokens of skill bodies added to the head, a
   test rule held on 4 of 24 items against 12 of 24 (p = 0.03). A second test rule held on 15 of 24
   against 20 of 24: the model kept searching instead of answering on 8 of 24 items. (M3)
3. **Placing a rule near the question does not help on Flash-Next.** In the per-turn block, the test
   rules held on 18 and 9 of 24, against 20 and 12 of 24 in the system prompt (p = 0.72 and 0.56).
   That position also costs a prefill on every turn. (M3, M2)
4. **Injecting skill bodies has nothing to gain.** A skill is loaded on 2.8% of turns. On those turns
   `read_skill` costs 2.2 s more than injection would. That is 0.06 s per turn on average, and it
   needs no classifier. (M2, production telemetry)
5. **The cache rewards one position only.** Content in the head is free after its first cold
   prefill. Content after the head costs its prefill on each turn that carries it. A change in the
   head costs everything after the change. (M2)
6. **The larger cache costs are elsewhere.**
   - **F1:** a cold head on 33% of turns, 23 of 27 after a deploy changed the head.
   - **F2:** follow-up turns prefill the history again. That is 2,824 tokens (7.3 s at 2.6 ms per
     token) at the median on turns 2–10, and 17,043 tokens (44 s) from turn 11 on. FRE-1600 and
     FRE-1134 hold the fixes.
7. **sequential-thinking repeats its rule.** It loaded 0 times in 176 production turns, and its
   catalog line costs on every turn.

---

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
| `c` | Prefill time per uncached prompt token | Flash-Next 2.6 ms at a 12.6k context (M2); 2.10 ms from a fit dominated by small prompts. Gemma 0.79 ms (fit only). | M2 `fence` arm: (10,628 − 814) ms / 3,729 tokens. Fit: `wall_s` on uncached prompt and output tokens, 177 FRE-1590 Part 1 calls per model, R² = 0.98. |
| — | Decode time per output token | Flash-Next 29.8 ms; Gemma 20.8 ms | The same fit |
| `L` | Cost of the extra `read_skill` call | Flash-Next 2.2 s (M2); Gemma ~1.1 s (estimate) | M2 `skill` arm: the model's own `read_skill` call, 56–64 output tokens, 3 draws. Gemma: decode time × 52 tokens. |
| `m` | Share of turns that start with a cold head | 0.33 (27 of 83); 0.05 without prefix changes | Production ES, `model_call_completed`, role `primary`, Flash-Next, 14 days to 2026-10-12. Head miss = fewer than 2,000 cached tokens on the turn's first call. |
| `q` | Share of turns that load a skill | 0.028 for all skills together (5 loads in 176 turns) | Production ES, `tool_call_started` with `tool_name` `read_skill`, since `read_skill` was first registered (2026-10-01 09:27Z) |
| `s` | Content size | Rules 130–552 tokens; catalog lines 54–138; bodies 850–3,728 tokens | M1, measured with the served tokenizer and chat template |

**Erratum (2026-10-16, FRE-1606).** ES holds `read_skill` calls from 2026-07-31, so 2026-10-01 09:27Z is the start of the `q` window, not the first registration. On that window the same query now returns 178 turns, and the 5 calls fall in 4 turns (0.022 per turn). ADR-0156's 2026-10-16 amendment gives the query for each input.

### Results with these inputs (Flash-Next, `c` = 2.6 ms, `L` = 2.2 s)

| Case | In the head | Elsewhere | Result |
|---|---|---|---|
| All 10 skill bodies (18,077 tokens) | 15.5 s per turn (`m` = 0.33); 2.4 s (`m` = 0.05) | As skills: 0.19 s per turn | Skills are 12–80× cheaper |
| The reasoning rule (130 tokens), needed on about half the turns | 0.11 s / 0.02 s per turn | In the per-turn block: 0.17 s per turn (0.34 s on each turn that carries it) | The head is cheaper, and M3 shows no gain in the block |
| One catalog line (54–138 tokens, mean 101) | 0.09 s / 0.01 s per turn | — | The price of offering a skill. A skill that is never loaded pays it for nothing. |

The first draft of this table used `m` = 0.57. That figure counted history misses as head misses. The
check is in **The cache instrument** below.

Break-even sizes for a body at `m` = 0.33: `q` = 0.5 → the head always wins; `q` = 0.1 → about 370
tokens; `q` = 0.02 → about 55 tokens. No skill body is that small (the smallest is 850 tokens).

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

| Turn position in the session | n | Uncached tokens on the turn's first call (p50 / p90) | Prefill time (p50) at 2.6 ms per token |
|---|---:|---:|---:|
| Turns 2–10 | 26 | 2,824 / 3,771 | 7.3 s |
| Turn 11 and later | 13 | 17,043 / 18,160 | 44.3 s |

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

## M2 — the cache cost of each option (AC-2)

**Method.** `scripts/eval/fre1592/channels.py cache`. One skill body (mermaid-diagrams, 3,728 tokens as `read_skill`
returns it; it loads on 12 of 12 draws on both models) is delivered five ways. Each arm has its own nonce. Turn 1 is cold,
and turn 2 is measured with llama.cpp `timings`:

| Arm | Delivery |
|---|---|
| `base` | No body. This measures the template's own boundary cost. |
| `head` | The body in the system prompt on both turns |
| `fence` | The body in turn 2's `<turn_context>` block |
| `skill` | Turn 2, the model's own call (does it call `read_skill`?), then a `read_skill` exchange, then one more call |
| `head_turn2` | The body added to the system prompt on turn 2 only. This is the negative control. It must show the full re-prefill. |

The turn-1 history is kept byte for byte as it was sent, so M2 measures the delivery and not F2.

**Result** (Flash-Next, 3 draws, 2026-10-12 20:05:16–20:16:24Z, run
`telemetry/evaluation/fre1592/full-20261012-2005`, git-ignored, 0 gateway retries). Medians of 3; the
draws agree within ±1 token and ±12% time.

| Arm | Turn 1 (cold): tokens / prefill s | Turn 2: cached / prefilled tokens | Turn 2 prefill s | Extra on the turn that needs the body |
|---|---|---|---:|---:|
| `base` (no body) | 12,599 / 33.1 | 12,595 / 141 | 0.8 | — |
| (a) `head` | 16,327 / 43.2 | 16,323 / 141 | 0.8 | 0 s per turn; +10.1 s once per cold head |
| (b) `skill` | 12,599 / 33.1 | 12,732 / 4,188 | 11.6 | +13.0 s: the `read_skill` call 2.2 s (56–64 output tokens) + 4,188 prefilled tokens |
| (c) `fence` | 12,600 / 33.4 | 12,596 / 3,870 | 10.6 | +9.8 s, again on each turn that injects it |
| `head_turn2` (control) | 12,601 / 33.5 | 10,553 / 5,914 | 16.3 | +15.5 s: the cache breaks at the insertion point |

**Measured inputs that replace the estimates.**
- `L` (the `read_skill` round trip) = 2.2 s, against 1.7 s estimated.
- The prefill cost per token at a 12.6k context is about 2.6 ms ((10,628 − 814) ms / 3,729 tokens in
  the `fence` arm), against 2.10 ms from the FRE-1590 fit, which was dominated by small prompts. The
  larger value makes every cold-prefill figure in this note about 25% larger. No ranking changes.
- The `skill` arm prefills 318 tokens more than the `fence` arm for the same body: the JSON wrapper of
  the `read_skill` result.
- The negative control confirms the mechanism: a change in the head costs everything after the change.
  Here that is 2,043 tokens of the system prompt after the insertion point, plus the body. In a long
  session it also includes the whole history.

## M3 — rule following by channel (ticket question 5)

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

**Result** (Flash-Next, 2026-10-12 20:16:24–20:53:57Z, same run directory as M2, 240 of 240 rows,
0 gateway retries). Pass of 24 (8 items × 3 draws):

| Arm | Rule A (`Sources:` line) | Rule B (`search_memory` with `web_search`) |
|---|---:|---:|
| `system` | 20 (22 answered) | 12 |
| `heavy` | 15 (16 answered) | 4 |
| `skill` | 23 (23 answered) | 20 |
| `fence` | 18 (22 answered) | 9 |
| `absent` (control) | 0 (22 answered) | 0 |

Fisher's exact test, two-sided:

| Comparison | Rule A | Rule B |
|---|---|---|
| `system` against `fence` | 20 vs 18, p = 0.72 | 12 vs 9, p = 0.56 |
| `system` against `heavy` | 20 vs 15, p = 0.19 | 12 vs 4, p = 0.03 |
| `system` against `skill` (context only) | 20 vs 23, p = 0.35 | 12 vs 20, p = 0.03 |

**What this shows.**
- **Both rules can be measured.** The control passed 0 of 48, and every other arm passed some items.
- **Near the question is not better than the system prompt** for either rule on Flash-Next.
- **A larger head weakens a rule.** For rule B, the pass rate fell from 12 to 4 of 24. For rule A,
  the rule still held on 15 of the 16 answered items. But the model kept calling `web_search`, after
  three replies of "No further results", on 8 of 24 items, against 2 of 24 in `system`.
- **The `skill` arm passed most often.** In the owner's model, a rule is not a skill, so this is
  context only. It is also confounded: the rule arrives as a separate instruction just before the
  task, which no production position offers.

**Limits.** One model (Flash-Next). Two test rules, not production rules: both production candidates
were followed unasked (preflight 1). The 24 samples are 8 items × 3 draws, and the draws of one item
are correlated, so the p-values are optimistic.

---

## Validity notes

- **`m` was measured in a period with many deploys** (ADR-0156 migration). Without deploys it is about
  0.05. The arithmetic gives both values.
- **`q` comes from 176 turns, and some were test turns.** The owner is the only user.
- **`c` depends on the context length.** The FRE-1590 fit (2.10 ms) comes mostly from small prompts.
  M2 measures 2.6 ms at a 12.6k context. This note uses 2.6 ms for the formula.
- **The FRE-1590 capture gateway had no memory graph,** so its memory result reads "records could not
  be reached". M1 counts the head, which this does not change. M2 and M3 use a fixed short memory
  result.
- **M3 adds the rule as a skill that the catalog does not list.** The `read_skill` exchange is
  synthetic. This tests how the model follows a rule in a tool result, not whether it chooses to load it.
- **M2 and M3 ran on the production llama.cpp server,** directly on `:8600`, not through an eval
  gateway. The eval stack uses the same server, so the cache figures are the production figures. Gemma
  was not run, by the owner's choice: a Gemma result does not apply to the production primary.
- **M3's rules are not production rules.** Both production candidates were followed unasked
  (preflight 1), so they cannot show a channel effect. Rules A and B are plausible operating rules
  that the model does not follow unasked. A result about them is a result about rule delivery, not
  about the grounding or parallel-calls rules.

---

## Decision brief

**The owner's questions, answered against the owner's model.** The owner's model (asked as a question,
not decided): the system prompt holds an agent's base behaviour, other core work gets another agent,
and skills add capabilities on demand. The measurements fit that model. They do not ask for a change
to it.

| Question | Answer | Evidence |
|---|---|---|
| Do we lighten the system prompt? | Not by moving rules: they are 7.9% of the head. The weight is the tool definitions (79%), and their split is already under way (FRE-1591, FRE-1595). | M1 |
| Does a heavier head cost anything? | Yes, latency and rule strength. | M2: +10.1 s per cold head for 3,728 tokens. M3: a test rule fell from 12 to 4 of 24. |
| Do we inject skills? | No. The most it can save is 0.06 s per turn, and it needs a classifier. | M2, `q` = 0.028 |
| Is a rule better near the question? | No, not on Flash-Next. It costs a prefill on every turn. | M3 `fence` against `system`; M2 `fence` |
| How does each option affect the cache? | The head is free after its first prefill. Content after the head costs on each turn that carries it. A change in the head costs everything after it. | M2 |

**Recommendations.** The owner decides. Nothing ships before that (AC-3).

1. **No change to where a production rule sits.** No production behaviour fails today with telemetry
   evidence, so there is nothing to fix. Record the M3 result as a property of Flash-Next, for the next
   rule that is added: put it in the system prompt, not in the per-turn block.
2. **Keep every skill body as a skill. Do not inject skills.**
3. **sequential-thinking:** remove the "Rules" section, which repeats `REASONING_DISCIPLINE_PROMPT`.
   Keep the procedure part (estimate the steps, mark revisions, name branches, check the answer) as
   the skill, and rewrite its description for that procedure. If it still loads 0 times in the 30 days
   after the change, remove it from the catalog. Its line costs 71 tokens on every turn.
4. **The larger cache levers are already filed.** FRE-1600 (the stored history is not the request that
   was sent) and FRE-1134 (the window moves one pair per turn) hold the F2 costs: 7.3 s on a median
   turn 2–10 and 44 s from turn 11. F1 (cold heads) comes mostly from the migration's deploy rate, and
   needs no ticket.
5. **If the owner adopts the model, record it** in an amendment to ADR-0156 D2, with the cost formula
   and its telemetry inputs (`c`, `L`, `m`, `q`). Future placement questions then need a measurement,
   not an opinion.

**Not recommended here (context only).** The owner parked two ideas that master raised: grouping
rarely used tools behind skills, and a per-rule removal check (FRE-1592 comments of 2026-10-12).

---

## References

- FRE-1592 (comments of 2026-10-12: the owner's model, the parked ideas, the run approvals); FRE-1600; FRE-1595; FRE-1590 and `docs/research/2026-10-12-fre-1590-skill-test-qwen-gemma.md` (F1, F2, per-skill
  table, validity notes); FRE-1549; FRE-1577; FRE-1591; FRE-1134 (the conversation window decision).
- ADR-0081 D1/D2 (the cached head and the append-only layout), ADR-0140 T2 (untrusted input only in a tool
  result), ADR-0156 D2 and amendment A5.8 (the cache-safe reveal), ADR-0154 D7 (the replay probe pattern).
- `src/personal_agent/orchestrator/executor.py` (system prompt assembly and the `<turn_context>` block),
  `src/personal_agent/orchestrator/prompts.py`, `src/personal_agent/service/app.py:418-428, 562-578`,
  `src/personal_agent/llm_client/litellm_client.py:1977-2014`.
- `scripts/eval/fre1592/` (this study), `scripts/eval/fre1590/triggering.py`, `scripts/eval/fre1537/replay.py`.
- [Anthropic, Agent Skills overview](https://platform.claude.com/docs/en/agents-and-tools/agent-skills/overview):
  the catalog metadata is always loaded, and the body loads on demand.
