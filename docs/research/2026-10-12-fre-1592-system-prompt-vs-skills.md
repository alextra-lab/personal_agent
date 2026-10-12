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
   - The skill bodies stay as skills: in the head they cost 12–77× more.
   - Harness injection has nothing to gain: at `q` = 0.028 it can save at most about 0.05 s per turn.
3. **The system-prompt question is the smallest cache cost in the system.** Moving the reasoning rule
   changes about 0.02–0.10 s per turn. Two larger costs were found on the way:
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
| `s` | Content size | Rules 150–640 tokens; bodies 900–3,600 tokens | chars ÷ 4 until M1 replaces them with tokenizer counts |

### Results with these inputs (Flash-Next)

| Case | In the head | As a skill | Result |
|---|---|---|---|
| All 10 skill bodies (~17,800 tokens) | 12.3 s per turn (`m` = 0.33); 1.9 s (`m` = 0.05) | 0.16 s per turn | Skills are 12–77× cheaper |
| The reasoning rule (~150 tokens), needed on about half the turns | 0.10 s / 0.02 s, present on 100% of the turns | 0.12–0.34 s, present on 11–33% of the turns that need it | The head wins on cost and on coverage |
| One catalog line (~120 tokens) | 0.08 s / 0.01 s per turn | — | The price of offering a skill. A skill that is never loaded pays it for nothing. |

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

## M1 — the inventory (pending)

**Method.** `scripts/eval/fre1592/inventory.py` takes a FRE-1590 capture of the production primary
request (`main 4bc88d2a`, 24 tools). The prompt inputs of that capture are unchanged on `main` today:
the script rebuilds the captured system prompt from the code constants and stops if it differs. It adds
the two production-only sections that the capture gateway lacks: the deployment context and the
operator stanza (placeholder name). It renders the request with llama.cpp `/apply-template` and counts
with `/tokenize`. Each component's count is an ablation (the full request minus the request without
it). The instrument check: the captured body must count to the `prompt_tokens` that llama.cpp
reported for the same body (12,377).

**Result:** pending. It needs `:8600`, which master's FRE-1594 hold blocks until about 18:10Z.

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

**Method.** `scripts/eval/fre1592/channels.py follow`. Two production rules, each with a score that a
script computes:

- **R1, the grounding contract:** the turn holds two `web_search` results with citation markers. Pass
  = the reply copies both markers. The facts are invented, so the reply can take them only from the
  results.
- **R2, the parallel-calls rule:** the prompt needs three independent tool calls. Pass = the first
  response holds three or more calls.

| Arm | Where the rule is |
|---|---|
| `system` | The system prompt, as in production |
| `heavy` | The system prompt, with all 10 skill bodies added to the head (~17,800 more tokens). This tests whether a larger head weakens the rule. |
| `skill` | A `read_skill` result, after the user message |
| `fence` | The `<turn_context>` block of the user message |
| `absent` | Nowhere. This is the control: a rule that the control also passes cannot show a channel effect. |

8 items per rule × 3 draws × 5 arms = 120 calls per rule per model.

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
