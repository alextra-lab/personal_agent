# Router design review — the ladder is not the router; the fallback is

**FRE-1377** · explore session · 2026-09-04 · read-only study against the live system

**Deployed revision under measurement:** `main@16af9e28` (FRE-1376's word-boundary fix included).
Confirmed by container file hash, not by board state — see [Method](#method-appendix), M1.

> **Correction, 2026-09-04 12:30, after owner challenge.** One finding — **F10** — rested on
> sub-agent execution, and the sub-agent path was under active repair on the day it was measured.
> F10 is **withdrawn** as evidence about expansion's value, and the two recommendations that hung on
> it (P1's "keep the expansion denial", P3's "repair expansion before widening the door") are
> withdrawn with it. See **F18** for the deploy chain and **the retraction notice on F10**. The other
> sixteen findings do not touch sub-agent execution and are unaffected; F18 states which is which.
>
> **Extended, 2026-09-04 15:30, at the owner's request.** Three findings added from the discussion
> that followed: **F19** (the owner already routes manually, by prefixing a keyword — which withdraws
> this study's objection to the routing selector, P5), and **F20**/**F21** on what the sub-agent
> mechanism actually is (no tools, ever; a 2,000-character prefix slice for a "digest"). The last two
> are retrospective and cannot test the owner's forward hypothesis about a *properly configured*
> sub-agent — they establish what the parts are today, so the gap between them and that hypothesis
> is legible. That distinction was the owner's, not this study's.

---

## Verdict, up front

The commission asks whether deterministic keyword classification has reached its limit. The
measurements say the question is aimed one rung too high, and that the two obvious answers to it —
classify better, or let a model classify instead — are both worse bets than they look.

**The ladder's ordering decides 3.8% of turns. Its no-match fallback decides 78%.** Every ticket in
this family — FRE-1288, FRE-1376, and the 2026-09-04 incident — has been about a *rule that fired*.
The corpus says the dominant behaviour is that **no rule fires at all**, and the system reads that
absence of evidence as positive evidence of a low-capability request: `no_special_patterns` →
`CONVERSATIONAL` → tool iterations capped at 6, expansion denied permanently.

Five further measurements reshape the recommendation:

1. **The router barely routes.** 95.2% of recent real turns and 99.0% of the full three-month
   corpus resolve to `SINGLE`. A four-valued enum with one value in 19 turns out of 20 cannot be
   improved much by classifying better. `DECOMPOSE`, specifically, has produced an outcome
   `HYBRID` would not have produced exactly **once in 649 turns**.
2. **The per-turn expansion budget bounds nothing.** Two turns on 2026-09-04 carried
   `expansion_budget=1` from the brainstem and spawned **four** sub-agents. Master's hypothesis on
   the ticket — that the strategy name is "largely redundant with the budget it accompanies" — is
   backwards: the strategy name is the *only* thing bounding fan-out, and the budget is a boolean.
3. ~~**Routing to expansion currently makes the answer worse.**~~ **Withdrawn — see F10 and F18.**
   The comparison was real but it was taken against an expansion path under active repair, two of
   whose three fixes had not yet shipped. It measured a construction site, not a design. What
   survives is narrower and still worth stating: **nobody has a clean measurement of what expansion
   is worth**, because every expansion turn on record predates the repair.
4. **A model classifier is not an oracle.** FRE-1337's arm 2 has now been run — first on its own
   seven fixtures, where three models agreed unanimously, and then on 60 real messages, where the
   two models agree with the deterministic cascade **70%** of the time and with **each other only
   84%**. The fixture unanimity is a property of the fixtures.
5. **A model call cannot go in the hot path.** Measured: 7.93s median on the local primary (59% of a
   median conversational turn), 2.26s on Sonnet — and 3 of 60 local calls returned `503` from the
   shared single GPU, one after 324.8 seconds of retries.

So the recommendation is not "let the model decide", not "weight by position", and not "add a
selector". It is: **fix the polarity of the fallback, make the ceiling that already exists actually
bind, and show the user what the router chose before offering them a control over a decision that is
95% constant.** Whether the expansion denial should be lifted at the same time is left open, not
answered — see P1, and F10's retraction for why this document is not entitled to an opinion on it.

Two ideas are tested here and **recommended against**, with the numbers: position-weighted signals
(F3 — would change 2 messages in 2,125, one of them for the worse) and a length-gated model arbiter
(F17 — a design this study itself drafted, then measured, then dropped, because the disagreements it
was meant to catch turn out to live in messages under fifteen words).

Where this study disagrees with its commission is stated inline, per finding, marked
**↯ argues with the brief**.

---

## Findings

### F1 — 95% of real turns route to `SINGLE`; the four-valued strategy enum has one value in practice

**Verdict: POSITIVE.**

**The query** — replay of the *deployed* `classify_intent` + `assess_decomposition` over every
`agent-captains-captures-*` document carrying a non-empty `user_message` (2,125 messages,
2026-05-01 → 2026-09-04), with `GovernanceContext(mode="NORMAL", expansion_permitted=True,
expansion_budget=3)` and `delegation_enabled=False` (the live value — M2):

```
uv run python matrix2.py        # scratchpad script; see M9 for what each one does
```

**Its actual output:**

```
ALL captures                       n= 2125  single= 2104 ( 99.0%) hybrid= 18 decompose=  3 delegate=  0
  ts 2026-05                       n= 1541  single= 1541 (100.0%) hybrid=  0 decompose=  0 delegate=  0
  ts 2026-06                       n=  136  single=  135 ( 99.3%) hybrid=  1 decompose=  0 delegate=  0
  ts 2026-07                       n=  185  single=  176 ( 95.1%) hybrid=  8 decompose=  1 delegate=  0
  ts 2026-08                       n=  249  single=  244 ( 98.0%) hybrid=  4 decompose=  1 delegate=  0
  ts 2026-09                       n=   14  single=    8 ( 57.1%) hybrid=  5 decompose=  1 delegate=  0
REAL (non-eval, >=2026-07)         n=  417  single=  397 ( 95.2%) hybrid= 17 decompose=  3 delegate=  0
```

Corroborated independently by the production ledger, which records what actually happened rather
than what a replay says would happen:

```sql
SELECT decomposition_strategy AS strat, count(*) FROM route_traces
WHERE task_id IS NULL GROUP BY 1 ORDER BY 2 DESC;
```
```
   strat   | count
-----------+-------
 single    |   610
 hybrid    |    33
 delegate  |     4
 decompose |     2
(4 rows)
```

(649 turn-level rows, 2026-06-07 → 2026-09-04.)

610/649 = 94.0%, against the replay's 95.2% on the overlapping window. The two instruments agree.

**Why it matters.** Keyword accuracy is a lever on a mechanism whose output is constant 19 times in
20. This does not make the misroutes harmless — F10 shows one cost 900 seconds — but it does mean
that "classify better" is optimising the 5%.

---

### F2 — the ladder's *order* decides 3.8% of messages; its *fallback* decides 78%

**Verdict: POSITIVE.**

Master's comment on FRE-1377 asked for exactly one number: *"for each recorded turn, replay all
rules and count the turns where more than one rule would have matched. Master has not run it and
will not guess at the answer."* Here it is.

**The query** — each of the six positive rungs of `classify_intent` evaluated **independently**
(not as a ladder) over the same 2,125-message corpus, at the deployed revision.

**Its actual output:**

```
INSTRUMENT VALIDATION — replay vs recorded task_type on the joined set:
  agree 561/563 = 99.6%   disagree 2
    recorded=conversational  replayed=tool_use         n=1
    recorded=delegation      replayed=analysis         n=1

RULE-COLLISION CENSUS over all 2125 messages
  rules matching = 0:  1664  ( 78.3%)
  rules matching = 1:   380  ( 17.9%)
  rules matching = 2:    80  (  3.8%)
  rules matching = 3:     1  (  0.0%)
  >= 2 rules match: 81 (3.8%) — these are the turns where LADDER ORDER, not a rule, chose the outcome

  Which pair-of-rungs collided (winner <- loser), top 15:
    analysis       beat tool_use        n=80
    planning       beat analysis        n=1
    planning       beat tool_use        n=1
```

Two things to read off this.

**The instrument validates.** 561 of 563 messages that join to a production `route_traces` row
replay to the recorded `task_type`. The two disagreements are both explained and both expected: one
is the 2026-09-04 incident itself (`recorded=delegation`, `replayed=analysis` — the replay runs
post-fix code against a pre-fix trace, which is precisely FRE-1376 landing), the other a `tool_use`
pattern added after its trace was written. A replay that reproduces production 99.6% of the time is
a usable instrument; everything else in this document that depends on replay rests on this number.

**Ordering is a small problem, and it is not the problem that has been filed.** Where two rungs do
collide, it is `analysis` beating `tool_use` 80 times out of 81 — and `analysis` beating `tool_use`
is almost certainly the right call, since it is the one that grants the larger budget. The coding
rung — the rung every filed ticket is about — beats nothing in the corpus at the deployed revision.

**↯ argues with the brief.** The commission and master's comment both treat rung ordering as
load-bearing and undocumented. It *is* undocumented. It is not, measurably, load-bearing: it decides
one turn in 26, and when it decides, it decides in favour of the more capable lane.

---

### F3 — position-weighted signals would change the outcome of 2 messages in 2,125

**Verdict: POSITIVE.**

Master's comment proposed position-weighted signals — *"a first-word imperative is a speech act about
the whole request; a keyword 1,400 characters in is usually a subject the request is about"*. The
2026-09-04 incident is exactly that shape, so the proposal deserves a number rather than an opinion.

**The query** — for every colliding message (the 81 from F2), compare the rung the ladder chose
against the rung whose match *starts earliest in the string*. A purely position-weighted ladder picks
the latter; the two differ exactly where position weighting would change the answer.

**Its actual output:**

```
colliding messages: 81
  ladder winner IS the earliest match : 79
  ladder winner is NOT the earliest   : 2  <- position weighting would flip these
     ladder chose planning     but earliest match was analysis      n=1
     ladder chose analysis     but earliest match was tool_use      n=1

messages matching the analysis rung anywhere: 86; of those, match starts at char 0: 17
```

**Two messages in 2,125 (0.09%)** — and one of the two flips *away* from `analysis` and toward
`tool_use`, which is the less capable lane. The ladder already agrees with position on 79 of 81
collisions, because the rungs that collide (`analysis` vs `tool_use`) happen to be ordered the same
way position would order them.

**↯ argues with the brief.** Position weighting is a well-aimed idea drawn from a real incident — but
the incident it was drawn from no longer occurs at the deployed revision (FRE-1376 removed the rule
that produced it, F4), and no second instance of the shape exists in three months of traffic. Adding
it now would put another hand-tuned heuristic on the pile — master's own stated concern — to move two
messages, one of them in the wrong direction. **This study recommends against it**, and that
recommendation is the answer to master's question, not a deferral of it.

### F4 — FRE-1376's word-boundary fix changed exactly one classification in 2,125

**Verdict: POSITIVE.**

**The query** — same corpus, classified twice: once with the deployed
`_CODING_KEYWORD_PATTERN` (word-boundaried), once with the pre-FRE-1376 bare-substring matcher
(`any(kw in message.lower() for kw in _CODING_KEYWORDS)`), diffed.

**Its actual output:**

```
=== B. FRE-1376's word-boundary fix: what it moved ===
  turns whose classification changes pre->post fix: 1 (0.05%)
    coding           -> analysis         n=1
```

That one is the incident itself (trace `cf25bc13-5c64-c0ae-2b9f-f184912514d0`).

This is the commission's own thesis, measured: *"That is not one bad keyword… The next false positive
arrives through a different phrase, and we file another ticket."* Correct — and the corollary is that
the false-positive rate of the coding rung was **1 in 2,125 to begin with**. The mechanism is not
leaking; it is barely firing. Both halves of that matter: a narrow fix was the right call for
FRE-1376, *and* the class of defect it belongs to is not primarily a false-positive class.

---

### F5 — the classifier reads the user message alone, with no conversation context

**Verdict: POSITIVE (enumeration).**

`pipeline.py:96` calls `classify_intent(user_message)` — a single string. Stage 6 (context assembly)
runs *after* stages 4 and 5, so no session history, no prior turn, and no assembled memory is
available to the classifier by construction.

**Why it matters, measured.** In the recent real window the median user message is **10 words**, and
**81 of 339** turns that matched no rung are five words or shorter. Among the ten longest-running
`conversational` turns in the whole corpus, the user messages include, verbatim: `'Amy responde?'`
(902.9s, one tool), `'Yes'` (902.7s, two tools), `'Are you stuck?'` (676.8s, two tools), and `'Yes'`
again (412.0s, three tools including `web_search` and `fetch_url`). Those are follow-ups, and the
work they triggered is invisible in their text. There is no classifier — deterministic, probabilistic
or human — that can route `'Yes'` correctly from the string `'Yes'`.

This is a structural ceiling on *any* stage-4 design that reads only the current message, and it
bounds what a model-based classifier could achieve too. It is the single strongest argument that the
classification is being made in the wrong place: the model, mid-turn, has the history; the gateway,
pre-turn, does not.

---

### F6 — the per-turn expansion budget is computed, emitted, and ignored

**Verdict: POSITIVE.**

**The query** — two `gateway_output` events from the live ES logs, joined by `trace_id` to their
production ledger rows:

```
GET agent-logs-*/_search  {"query":{"term":{"event_type":"gateway_output"}},"sort":[{"@timestamp":"desc"}]}
```
```
{'@timestamp': '2026-09-04T07:01:21.201439Z', 'event_type': 'gateway_output',
 'trace_id': '94fda7de4fce7905273532e78ccda063', 'task_type': 'analysis', 'complexity': 'moderate',
 'confidence': 0.8, 'signals': ['analysis_pattern'], 'mode': 'NORMAL', 'expansion_permitted': True,
 'expansion_budget': 1, 'strategy': 'hybrid', ...}
{'@timestamp': '2026-09-04T05:01:31.574878Z', 'event_type': 'gateway_output',
 'trace_id': 'a4619c7035dcc6b0071197c130438008', 'task_type': 'analysis', 'complexity': 'moderate',
 'confidence': 0.8, 'signals': ['analysis_pattern'], 'mode': 'NORMAL', 'expansion_permitted': True,
 'expansion_budget': 1, 'strategy': 'hybrid', ...}
```

```sql
SELECT trace_id, task_type, decomposition_strategy, sub_agent_count
FROM route_traces WHERE task_id IS NULL AND trace_id IN
 ('94fda7de-4fce-7905-2735-32e78ccda063','a4619c70-35dc-c6b0-0711-97c130438008');
```
```
               trace_id               | task_type | complexity | decomposition_strategy | sub_agent_count
--------------------------------------+-----------+------------+------------------------+-----------------
 94fda7de-4fce-7905-2735-32e78ccda063 | analysis  | moderate   | hybrid                 |               4
 a4619c70-35dc-c6b0-0711-97c130438008 | analysis  | moderate   | hybrid                 |               4
```

**Budget 1. Four sub-agents. Twice, on the same day.**

The mechanism: `governance.expansion_budget` has exactly one reader in the whole of `src/` —
`decomposition.py:59`, `if governance.expansion_budget <= 0: return SINGLE`. It is a zero-check. The
number never reaches the executor. What actually bounds fan-out is
`expansion_controller.py:45` — `_MAX_TASKS = {"HYBRID": 4, "DECOMPOSE": 6}` — a constant keyed on the
strategy *name*. The brainstem's load-shed signal (`expansion_budget_computed`, `budget: 1` under CPU
16.5% / memory 75.1%) is discarded.

**↯ argues with the brief.** The ticket records master's reading that "the strategy name is largely
redundant with the budget it accompanies". The measurement inverts it. The budget is redundant — it
is a boolean wearing an integer. The strategy name is the only load-bearing magnitude in the system.
This matters directly for the owner's proposed shape ("the harness enforces the envelope as a
budget; the model spends it as the work requires"): **two of the three moving parts do not exist
yet.** The budget is not a ceiling today; it is an on/off switch.

---

### F7 — the DELEGATE route has never delegated

**Verdict: NEGATIVE.** Arms carried below.

**The query:**

```sql
SELECT created_at::date AS d, trace_id, task_type, complexity,
       tool_iteration_count AS iters, final_reply_chars AS reply, latency_total_ms AS ms,
       delegate_result_passed_to_synthesis AS passed, orchestration_event AS ev
FROM route_traces WHERE task_id IS NULL AND decomposition_strategy='delegate' ORDER BY created_at;
```

**Its actual output:**

```
     d      |               trace_id               | task_type  | complexity | iters | reply |    ms    | passed |       ev
------------+--------------------------------------+------------+------------+-------+-------+----------+--------+-----------------
 2026-07-10 | ea15a0f6-6d36-4620-8c53-5e8239466c41 | delegation | simple     |     2 |  3587 | 31470.47 | f      | primary_handled
 2026-07-28 | be622285-62ce-489b-bff2-ca4fb14bf09d | delegation | moderate   |     0 |   701 |  7616.33 | f      | primary_handled
 2026-07-29 | 19f54c22-c5a8-4cad-a9c2-df5790db22e8 | delegation | simple     |     3 |  1148 | 21608.59 | f      | primary_handled
 2026-09-04 | cf25bc13-5c64-c0ae-2b9f-f184912514d0 | delegation | complex    |     6 | 23732 |          | f      | primary_handled
```

Four turns have ever taken the DELEGATE strategy. All four recorded
`orchestration_event = primary_handled` and `delegate_result_passed_to_synthesis = f`. The primary
answered; nothing was handed anywhere.

**Arm 1 — target-identifier provenance (1a, raw instance).** The four rows above are themselves
instances of `decomposition_strategy = 'delegate'` drawn verbatim from the store the finding queries
(production Postgres `route_traces`, `task_id IS NULL`). The identifier under test —
`orchestration_event`, and the value `primary_handled` — is exhibited in the same rows.

**Arm 2 — path liveness (same query, identifier varied).**

```sql
SELECT orchestration_event, count(*) FROM route_traces WHERE task_id IS NULL GROUP BY 1 ORDER BY 2 DESC;
SELECT delegate_result_passed_to_synthesis, count(*) FROM route_traces WHERE task_id IS NULL GROUP BY 1;
```
```
 orchestration_event | count            passed | count
---------------------+-------          --------+-------
 primary_handled     |   614            f      |   616
 delegate_called     |    33            t      |    33
 fallback_triggered  |     2
```

The column is populated and discriminating: `delegate_called` returns 33 and
`delegate_result_passed_to_synthesis = t` returns 33 on the identical query with only the value
varied. (Those 33 are the HYBRID sub-agent turns — the column name refers to sub-agent dispatch, not
external delegation.) The index is reachable and the predicate well-formed.

**Arm 3 — scope match.** Store: production Postgres, database `personal_agent`, table `route_traces`,
predicate `task_id IS NULL` (turn-level rows only). Window: all 649 rows, 2026-06-07 → 2026-09-04.
The verdict is stated at exactly that scope: *within the ledger's entire recorded history, no turn
has executed an external delegation.* It says nothing about turns predating the ledger.

**Corroborating live config** (M2): `settings.delegation_enabled = False` on the deployed process, so
the route is currently unreachable by construction as well as unused historically. `delegation/adapters/`
holds three modules; `claude_code.py` (192 lines) is a real CLI subprocess adapter, `codex.py` (73)
and `generic_mcp.py` (88) declare themselves stubs in their own docstrings. An exhaustive grep of
`src/` finds no construction site for any of the three outside their own package — a code reading,
offered as provenance for the live measurement, not as a substitute for it.

---

### F8 — HYBRID and DECOMPOSE have produced a different outcome exactly once in 649 turns

**Verdict: POSITIVE.**

**The query:**

```sql
SELECT decomposition_strategy, expansion_strategy, sub_agent_count, count(*),
       min(created_at)::date, max(created_at)::date
FROM route_traces WHERE task_id IS NULL GROUP BY 1,2,3 ORDER BY 1,3;
```

**Its actual output:**

```
   strat   |    exp    | n_sub | count |   first    |    last
-----------+-----------+-------+-------+------------+------------
 decompose | decompose |     2 |     1 | 2026-07-16 | 2026-07-16
 decompose | decompose |     6 |     1 | 2026-08-31 | 2026-08-31
 delegate  |           |     0 |     4 | 2026-07-10 | 2026-09-04
 hybrid    | hybrid    |     2 |    12 | 2026-06-07 | 2026-09-03
 hybrid    | hybrid    |     3 |    14 | 2026-06-07 | 2026-09-02
 hybrid    | hybrid    |     4 |     7 | 2026-06-07 | 2026-09-04
 single    |           |     0 |   610 | 2026-06-07 | 2026-09-04
```

`DECOMPOSE` has been selected twice. One of those spawned 2 sub-agents — a fan-out `HYBRID`'s cap of
4 would have permitted unchanged. The other spawned 6. So the entire practical content of the
distinction between two named strategies, over three months, is **one turn**.

The commission's reading — "two real states and a magnitude knob" — is confirmed, and understated:
the knob has moved once.

Note also that expansion, when selected, always happens: 33/33 `hybrid` and 2/2 `decompose` rows
carry a non-zero `sub_agent_count`. There is no silent-no-op in the expansion path. Whatever is
wrong with expansion (F10) is not that it fails to fire.

---

### F9 — the routing decision reaches no client-facing surface, while two *other* routing controls already do

**Verdict: POSITIVE (exhaustive enumeration of the deployed contract).**

**The query** — the live OpenAPI document served by the running gateway, not the repo:

```
docker exec cloud-sim-seshat-gateway curl -s http://localhost:9001/openapi.json
```

**Its actual output** — the complete parameter list and response schema of the deployed `/chat`
operation:

```
params: [('message','query'), ('session_id','query'), ('model','query'),
         ('skill_routing_mode','query'), ('channel','query')]
responses: {"200": {"content": {"application/json": {"schema":
    {"type":"object","additionalProperties":{"type":"string"},"title":"Response Chat Chat Post"}}}}}
```

```
"task_type" occurrences in the live OpenAPI schema: 0
"strategy"  occurrences in the live OpenAPI schema: 0
"intent"    occurrences in the live OpenAPI schema: 0
"routing"   occurrences in the live OpenAPI schema: 3   (all three are skill_routing_mode)
"decomposition" occurrences: 1  (prose in an /observations/sub-agents/{trace_id} description)
```

No negative arm is required here: a parameter list and a response schema are *enumerations*, not
searches. The deployed contract is quoted in full and the routing decision is not in it. The 200
response is an untyped `{string: string}` map — there is not even a typed envelope to add a field to.

**This cuts both ways, and the second way is the useful one.** `model` and `skill_routing_mode` are
already per-request routing controls on the live endpoint. The owner's proposal is therefore not
"introduce user-facing routing control" — it is *the third instance of a pattern the deployed API
already has twice*. That materially weakens the friction objection recorded in the brief, and it
means the implementation note in the ticket ("the model picker and `session_model_selections` are
already this exact shape") is stronger than stated: the shape exists at the HTTP layer too.

---

### F10 — WITHDRAWN — the correctly-routed turn produced a 160-character answer; the misrouted one produced 23,732

> **RETRACTED 2026-09-04 12:30, on owner challenge, and the challenge was right.**
>
> The numbers below are accurate. The **inference drawn from them was not**, and it was the one
> conclusion in this document that a study measuring a harness under active development had no
> business drawing.
>
> The owner's objection, in their words: *"Sub agent use has been failing and you are now basing
> decisions on it."* Correct. The 2026-09-04 07:16 turn ran **in the middle of a three-fix repair
> sequence on the sub-agent path**, of which one fix was live and two were not (F18). A turn that
> loses half its workers to a deadline that a merged-but-undeployed fix exists to address measures
> the state of the repair, not the value of expansion.
>
> **This finding is therefore withdrawn as evidence about whether expansion helps or hurts**, and
> with it P1's recommendation to keep `conversational_always_single`, and P3's "repair expansion
> before widening the door" — which this document called its strongest recommendation. It was its
> weakest, because it was the only one resting on a moving part.
>
> **What survives, and it is not nothing:** there is currently **no clean measurement of what
> expansion is worth**. Every expansion turn in the ledger predates the repair, so the corpus cannot
> answer the commission's "what does a wrong strategy cost in answer quality" in either direction.
> That is an absence of evidence, and this document previously reported it as evidence of absence.
>
> The measurement is retained below rather than deleted, because it is the correct *before* half of
> the comparison that should now be run.

The numbers, as measured, with the inference removed.

**The query:**

```sql
SELECT created_at, substr(trace_id::text,1,8), task_type, complexity, decomposition_strategy,
       sub_agent_count, tool_iteration_count, final_reply_chars, input_tokens, output_tokens,
       round(cost_authoritative_usd::numeric,4)
FROM route_traces WHERE task_id IS NULL
  AND trace_id IN ('94fda7de-...','cf25bc13-...');
```

**Its actual output:**

```
          created_at           |    tr    | task_type  | complexity |  strat   | subs | iters | reply | in_tok | out_tok |  cost
-------------------------------+----------+------------+------------+----------+------+-------+-------+--------+---------+--------
 2026-09-04 06:32:22.034601+00 | cf25bc13 | delegation | complex    | delegate |    0 |     6 | 23732 |  13590 |    4096 | 0.1001
 2026-09-04 07:16:25.021066+00 | 94fda7de | analysis   | moderate   | hybrid   |    4 |     4 |   160 |   2690 |     248 | 0.0105
```

`cf25bc13` is the FRE-1376 incident: a 1,900-character research brief misrouted to `DELEGATION`,
denied expansion, handled by the primary — and it returned a 23,732-character answer after 921.9
seconds and $0.10.

`94fda7de` is a research brief 44 minutes later that the ladder classified correctly (`analysis`,
`analysis_pattern`), routed to `HYBRID`, and expanded. Its sub-agent rows:

```sql
SELECT substr(task_id::text,1,8), error_type, final_reply_chars
FROM route_traces WHERE trace_id='94fda7de-...' AND task_id IS NOT NULL;
```
```
   task   |    error_type    | chars
----------+------------------+-------
 8308e398 |                  |  1404
 85372281 |                  |  2072
 4e0a68e9 | sub_agent_failed |     0
 3ff9e010 | sub_agent_failed |     0
```

and their captures, from `agent-captains-captures-subagents-*`:

```
{'error': 'None', 'duration_ms': 26276.0}
{'error': 'None', 'duration_ms': 32307.0}
{'error': 'Timeout after 85.0s', 'duration_ms': 85003.0}
{'error': 'Timeout after 85.0s', 'duration_ms': 85008.0}
```

Two of four sub-agents never got a slot — the FRE-1374 defect, on a local server with
`max_concurrency: 3` and a 4-way fan-out. The synthesis over the two survivors produced **160
characters** from 3,476 characters of sub-agent output.

**What this does NOT support (the retracted claim, kept visible on purpose).** This document
originally read the pair as evidence that a right route can cost a worse answer, and concluded that
repairing expansion was a prerequisite for any routing change. That conclusion is withdrawn. Two
turns, one of them running inside an in-flight repair, cannot carry it. The generation arithmetic
alone explains the deaths without any appeal to design: `sub_agent_max_tokens` is 4096 and per-request
throughput on the shared GPU falls to 14.42 tok/s at concurrency 3, so 4096 tokens needs ~284s against
an 85s deadline. Serialized at 37.08 tok/s the same budget fits in ~110s — which is why FRE-1380
exists, and it is now live (F18).

---

### F11 — both routing lanes hit the same 900-second wall on the same question

**Verdict: POSITIVE.**

`settings.orchestrator_task_timeout_seconds = 900` on the deployed process (M2). Eleven turns in the
2,125-message corpus ran to it:

```
turns >= 895s (the 900s orchestrator_task_timeout wall): 11 of 2125
  by replayed rung: {'analysis': 5, 'conversational': 4, 'memory_recall': 1, 'tool_use': 1}
   921.9s  2026-09-04T06:32:21  rung=analysis       tools=['fetch_url','web_search']  'Research how skills, memory, and subagents are actually used in state-'
   903.7s  2026-09-04T07:16:24  rung=analysis       tools=['fetch_url','web_search']  "Research What's changed in the EU's revised General Product Safety Reg"
   902.5s  2026-09-03T22:15:05  rung=analysis       tools=['web_search','fetch_url']  "Research What's changed in the EU's revised General Product Safety Reg"
   902.5s  2026-08-29T21:38:21  rung=conversational tools=['web_search','fetch_url','read_skill']  "What's changed in the EU's revised General Product Safety Regulation\ne"
   900.0s  2026-08-23T21:24:44  rung=conversational tools=['web_search','bash']  'Which running shoe brand is best for flat feet?'
   ... (6 more)
```

The GPSR question appears **twice in two phrasings**: once without a leading "Research" (classified
`conversational`, 902.5s) and once with it (classified `analysis` → `hybrid` → 4 sub-agents,
903.7s). Different lane, different budget, different expansion — **same wall**.

Read alongside F10 this is the sharpest available evidence that the router is not, today, the
binding constraint on research-shaped turns. Something downstream of it is.

---

### F12 — FRE-1288's third stated effect no longer exists

**Verdict: POSITIVE (enumeration).**

FRE-1288 (Approved, Urgent, unstarted) lists three effects of `conversational`, the third being
*"Is the sole entry gate to memory-recall reclassification — `request_gateway/recall_controller.py`,
Gate 1 returns `None` for anything not CONVERSATIONAL"*, and asks as its fourth decision question
whether that asymmetry still makes sense.

The module is gone. The deployed container's `request_gateway/` package, listed in full:

```
docker exec cloud-sim-seshat-gateway ls /app/src/personal_agent/request_gateway/
__init__.py  budget.py  context.py  decomposition.py  delegation.py  delegation_types.py
governance.py  intent.py  pipeline.py  protocols.py  types.py  __pycache__
```

Eleven modules and no `recall_controller.py`. It was deleted on `main` in `b0600ec3`
(*"fix(request_gateway,captains_log): delete dead Stage-6 assembly outputs; close false session-fact
admission (FRE-1135)"*).

Two of the three effects survive and are the real content of FRE-1288: the 6-iteration cap
(`orchestrator_max_tool_iterations_by_task_type`, live value in M2) and
`conversational_always_single` (`decomposition.py:99`).

---

### F13 — the 6-iteration cap binds on 2.6% of conversational turns; the expansion denial binds on 100%

**Verdict: POSITIVE.**

**The query:**

```sql
SELECT task_type, count(*) AS turns, max(tool_iteration_count) AS max_iters,
       count(*) FILTER (WHERE tool_iteration_count >= 6) AS at_or_over_6,
       round(avg(tool_iteration_count)::numeric,2) AS avg_iters,
       percentile_disc(0.9) WITHIN GROUP (ORDER BY tool_iteration_count) AS p90
FROM route_traces WHERE task_id IS NULL GROUP BY 1 ORDER BY 2 DESC;
```

**Its actual output:**

```
   task_type    | turns | max_iters | at_or_over_6 | avg_iters | p90
----------------+-------+-----------+--------------+-----------+-----
 conversational |   504 |         6 |           13 |      1.42 |   4
 tool_use       |    66 |        24 |           12 |      3.27 |   9
 analysis       |    44 |        15 |           12 |      4.50 |  11
 memory_recall  |    18 |         3 |            0 |      1.22 |   2
 planning       |    12 |        10 |            5 |      4.08 |   6
 delegation     |     4 |         6 |            1 |      2.75 |   6
 self_improve   |     1 |         1 |            0 |      1.00 |   1
```

`conversational`'s `max_iters` is exactly 6 — the cap, visible as a ceiling in the data. It binds on
13 of 504 turns (2.6%). Meanwhile `tool_use` reaches 24 and `analysis` reaches 15, so turns in the
other lanes routinely need more than 6.

**The asymmetry is the finding.** The iteration cap is a modest, occasionally-binding constraint. The
expansion denial (`conversational_always_single`) applies to **504 of 504** conversational turns
unconditionally, and 78–81% of all traffic is classified conversational by a *fallback*. FRE-1288 is
right that the second effect is the larger one, and the sizes are now on record.

---

### F14 — what the `conversational` bucket actually did

**Verdict: POSITIVE.**

Behavioural adjudication of the FRE-1337 AC-4 shape, computed for every capture in the recent real
window (non-eval, ≥ 2026-07, n=417; `conversational` subset n=339):

```
REAL (non-eval, >=2026-07): n=417  conversational=339 (81.3%)  tool_use=34 analysis=32 coding=3 planning=5 memory_recall=3
   words: p50=10 p90=30
   conversational subset n=339: >=1 tool 204, >=3 tools 29, web_search 128, >=60s 55
   conversational messages <=5 words: 81 / 339
```

**128 of 339 turns the router called "conversational" went and searched the web.** 55 ran for a
minute or more. At the same time 81 of them are five words or fewer — genuine follow-ups that the
label fits.

The bucket is not homogeneous, and that is exactly the problem with deciding capability from it: a
single label, assigned by the *absence* of a keyword, covers both `'Yes'` and a multi-source
regulatory comparison, and grants both the same 6-iteration, never-expand allowance.

---

### F15 — FRE-1337's arm 2, run for the first time: three models, one disagreement, and it is unanimous

**Verdict: POSITIVE.**

FRE-1337 (Awaiting Deploy) built the instrument this study needs and its probe arm had never been
executed. It has now been.

**The query** — the merged harness, unmodified, over its own committed fixture set. Two deviations
from its README, both forced and both recorded: the local model key was changed from
`qwen3.6-35b-thinking` to `qwen3.8-flash-next` because that is the only model the SLM server is
currently serving (M3), and `AGENT_SLM_BASE_URL` was pointed at the Caddy egress the deployed gateway
itself uses (M3):

```
AGENT_SLM_BASE_URL=http://localhost:8600 \
uv run python -m scripts.eval.fre1337_intent_probe.harness \
    --run-id 2026-09-04-fre1377 \
    --models qwen3.8-flash-next,qwen3.6-27b-ovh,claude_sonnet
```

**Its actual output** (`telemetry/evaluation/fre1337-intent-probe/2026-09-04-fre1377.md`, verbatim):

```
## qwen3.8-flash-next
| deterministic | model | count |
| conversational | analysis | 3 |
| conversational | conversational (agree) | 2 |
| memory_recall | memory_recall (agree) | 1 |
| tool_use | tool_use (agree) | 1 |

## qwen3.6-27b-ovh
| conversational | analysis | 3 |
| conversational | conversational (agree) | 2 |
| memory_recall | memory_recall (agree) | 1 |
| tool_use | tool_use (agree) | 1 |

## claude_sonnet
| conversational | analysis | 3 |
| conversational | conversational (agree) | 2 |
| memory_recall | memory_recall (agree) | 1 |
| tool_use | tool_use (agree) | 1 |
```

Three matrices, byte-identical. A local 3B-active MoE, a 27B on OVH, and Claude Sonnet 5 produce the
same classification on all seven fixtures, and the only off-diagonal cell in any of them is the same
one: **the three research-shaped fixtures the cascade calls `conversational`, every model calls
`analysis`.** The four fixtures where the cascade fires a positive rule get unanimous agreement.

The seeded-agreement case (AC-5) passes on all three models, so the harness is capable of producing a
diagonal cell and the off-diagonal is not an artifact of a broken comparison.

**What this does and does not establish.** It does not establish that the models are right — the
ticket is explicit that "disagreement is not evidence the model is right", and neither classifier is
ground truth. What it establishes is narrower and more useful: **the disagreement is not model-
specific, not scale-dependent, and not vendor-dependent.** It is a single, reproducible,
directional disagreement about one class of input. That is a property of the taxonomy's fallback,
not of any model's opinion. And F14's behavioural record adjudicates in the models' favour on the
population level: 128 of 339 `conversational`-classified turns went and searched the web.

**Do not stop reading here.** This unanimity is a property of seven hand-written fixtures. F16 points
the same probe at 60 real messages and the picture changes materially.

### F16 — on real traffic the models agree with the cascade 70% of the time, and with *each other* only 84%

**Verdict: POSITIVE.** This is the finding that most changes the recommendation.

F15's matrix rests on seven hand-written fixtures, on which three models agreed perfectly. That is a
property of the fixtures. The same probe was therefore pointed at real traffic.

**The query** — FRE-1337's own `classify_with_model()`, unmodified, over a seeded random sample
(seed 1377) of 60 turns drawn from the recent real population (non-eval captures, timestamp
≥ 2026-07, n=417). Two models: `qwen3.8-flash-next` (the local primary actually being served) and
`claude_sonnet`. Every call individually timed. Errors recorded as `ERROR`, never dropped.

**Its actual output:**

```
n = 60 real production turns (non-eval, >= 2026-07), seeded random sample (seed 1377)

qwen3.8-flash-next: usable 57/60  errors/invalid 3
   probe latency  p50=  7.93s  p90= 14.41s  min= 2.77s  max=  20.18s  mean=  8.45s
claude_sonnet:      usable 60/60  errors/invalid 0
   probe latency  p50=  2.26s  p90=  2.66s  min= 1.42s  max=  15.43s  mean=  2.40s

  qwen3.8-flash-next   agreement 40/57 = 70.2%
      det=conversational  model=conversational   29  (agree)
      det=analysis        model=analysis          6  (agree)
      det=conversational  model=memory_recall     4
      det=conversational  model=tool_use          4
      det=tool_use        model=tool_use          4  (agree)
      det=conversational  model=analysis          3
      det=tool_use        model=conversational    3
      det=delegation      model=analysis          1
      det=conversational  model=self_improve      1
      det=memory_recall   model=memory_recall     1  (agree)
      det=analysis        model=tool_use          1

  claude_sonnet        agreement 42/60 = 70.0%
      det=conversational  model=conversational   30  (agree)
      det=analysis        model=analysis          6  (agree)
      det=conversational  model=analysis          6
      det=conversational  model=tool_use          5
      det=tool_use        model=tool_use          5  (agree)
      det=conversational  model=memory_recall     2
      det=tool_use        model=delegation        2
      det=delegation      model=analysis          1
      det=conversational  model=self_improve      1
      det=memory_recall   model=memory_recall     1  (agree)
      det=analysis        model=tool_use          1

MODEL-vs-MODEL agreement (where both usable):  48/57 = 84.2%
    flash-next=conversational  sonnet=analysis        n=3
    flash-next=memory_recall   sonnet=conversational  n=2
    flash-next=conversational  sonnet=delegation      n=2
    flash-next=analysis        sonnet=conversational  n=1
    flash-next=conversational  sonnet=tool_use        n=1
```

Three things fall out of this, and all three cut against the direction the brief leans.

**The seven-fixture unanimity does not survive contact with real traffic.** On fixtures the models
agreed 100%; on 57 real messages they disagree with each other on 9 of them. A model classifier is
not an oracle to be swapped in for a regex — it is a second opinion with its own 16% internal
disagreement rate. FRE-1337's own AC-4 anticipated this precisely ("disagreement is not evidence the
model is right"); the number is now on record.

**The off-diagonal is not the story the tickets tell.** FRE-1288 and the brief frame the defect as
research-shaped questions falling into `conversational`. That cell exists (3 and 6 turns) but it is
not dominant. The larger cells are `conversational → tool_use` (4 and 5), `conversational →
memory_recall` (4 and 2), and — running the other way — `tool_use → conversational` (3) and
`tool_use → delegation` (2). The cascade and the models disagree in every direction, which is what
disagreement between two non-authoritative classifiers looks like.

**The local primary is not reliable enough for a hot path.** 3 of 60 calls to `qwen3.8-flash-next`
returned `503 ServiceUnavailable` from the SLM server — one after 324.8 seconds of litellm retries —
while the live gateway was sharing the same single GPU. A 5% hard-failure rate on a call that must
complete *before any work begins* is a different risk class from a 5% failure rate on a call inside a
turn that can retry.

---

### F17 — the disagreements are concentrated in *short* messages, where a length gate cannot find them

**Verdict: POSITIVE**, with an explicit power caveat: the gated subpopulation in this sample is n=5.

P6 in an earlier draft of this document proposed gating a model arbiter to "no rung matched AND the
message is at least 25 words" — a population F14 sizes at 8.4% of real turns, of which 27/35 used a
tool. That gate is attractive on paper. The probe says it points at the wrong messages.

**The query** — the same 60-turn sample, split by word count within the `det=conversational` bucket.

**Its actual output:**

```
ALL det=conversational  n=44
   flash-next {'conversational': 29, 'memory_recall': 4, 'tool_use': 4, 'self_improve': 1, 'analysis': 3, 'ERROR': 3}
   sonnet     {'conversational': 30, 'memory_recall': 2, 'tool_use': 5, 'self_improve': 1, 'analysis': 6}

GATE: det=conversational AND >=25 words  n=5
   flash-next {'conversational': 4, 'memory_recall': 1}
   sonnet     {'conversational': 3, 'memory_recall': 1, 'analysis': 1}

det=conversational AND <15 words  n=38
   flash-next {'conversational': 25, 'tool_use': 4, 'memory_recall': 2, 'self_improve': 1, 'analysis': 3, 'ERROR': 3}
   sonnet     {'conversational': 27, 'tool_use': 5, 'self_improve': 1, 'analysis': 5}
```

In the long-message gate the models mostly **agree with the cascade** (4 of 5, 3 of 5). Every
substantive disagreement in the sample lives in the short-message bucket — 8 of 38 for `sonnet`,
10 of 38 for `flash-next`.

And the two turns where *both* models overruled the cascade to `analysis` are nine words long:

```
  [2026-09-02T06:11:45] tools=['web_search','fetch_url'] secs=51 words=9
     'What do tourist say about visiting Norway mid-end september?'
  [2026-08-23T16:34:29] tools=['search_memory','recall_personal_history'] secs=23 words=9
     'Which brand of chocolate should I buy in Belgium?'
```

Both went and did real work — one searched and fetched for 51 seconds — and neither contains a single
keyword any rung looks for, nor enough text for a length heuristic to notice. This is FRE-1288's
canonical case ("Which tinned tuna should I buy in France") reproduced twice in live traffic, and it
is *short*.

**This kills the length gate, and it points at F5.** What distinguishes these turns is not length and
not vocabulary — it is that answering them requires work the system cannot see from the message text.
The signal that would separate them is either the conversation the message sits in (which stage 4
never receives, F5) or the model's own reading mid-turn (which is not a gateway decision at all).

**Stated honestly:** n=5 in the gate is too few to *size* a gate. It is enough to say the gate does
not point where the disagreements are, because the disagreements are visibly elsewhere in the same
sample.

### F18 — the sub-agent path was mid-repair when F10 was measured, and has since moved twice

**Verdict: POSITIVE.** Added 2026-09-04 12:30 in response to the owner's challenge. This finding
exists to bound the blast radius of F10's retraction — i.e. to say precisely which of this document's
claims touch sub-agent execution and which do not.

**The query** — every commit touching the sub-agent path, hashed against the file contents inside the
running container, so the deployed code is pinned to a commit rather than inferred from ticket state:

```
docker exec cloud-sim-seshat-gateway sha256sum /app/src/personal_agent/orchestrator/{sub_agent,expansion_controller}.py
```

**Its actual output** (abridged to the matching rows):

```
--- orchestrator/sub_agent.py            container=1ae96184d304
    cb236602 2026-09-04 10:57 1ae96184d304 fix: a killed sub-agent reports partial progress (FRE-1379)  <== CONTAINER
--- orchestrator/expansion_controller.py container=82b7462e3366
    75eb12e4 2026-09-04 11:47 82b7462e3366 fix: correct _run_dispatch's Returns docstring (FRE-1380)    <== CONTAINER

docker inspect cloud-sim-seshat-gateway --format '{{.Created}}'  ->  2026-09-04T11:57:19Z
```

**The chain, and where the measured turn sits in it:**

| Fix | Merged | Live at 07:16 (when F10's turn ran)? | Live now |
|---|---|---|---|
| FRE-1374 — per-worker clock, fan-out respects the ceiling | `d39c85d8` 05:05 / `a1036ce3` 05:30 | **Yes** | Yes |
| FRE-1379 — a killed sub-agent reports partial progress | `cb236602` 10:57 | **No** | Yes |
| FRE-1380 — serialize the fan-out | `bcd7abd7` 11:42 | **No** | Yes |

FRE-1374 being live at 07:16 is not inferred from its ticket (which reads `Done` at 06:10, while its
merge commit is timestamped 08:01 — board state and git disagree, and neither is the instrument). It
is established from the data: the observed kill message is `Timeout after 85.0s`, and
`worker_hard_deadline_seconds = 85.0` was introduced by FRE-1374's own commit `d39c85d8`. A turn
reporting that number was running that code.

So F10's turn had per-worker clocks (FRE-1374) but **still ran concurrently** (no FRE-1380) and its
killed workers **still reported nothing** (no FRE-1379). Both of the fixes that would have changed its
outcome were absent. It is a *before* measurement, and this document read it as a property.

**A second correction this forces: M1's deploy anchor is now stale.** The container was rebuilt at
**11:57:19**, between the last of these measurements and this correction. Every finding here is
anchored to the pre-11:57 build. The router files (`intent.py`, `decomposition.py`) are untouched by
that rebuild, so the classifier findings are unaffected; the expansion path is not.

**Which findings touch sub-agent execution at all.** This is the useful part:

| Finding | Rests on | Affected by the repair? |
|---|---|---|
| F1, F2, F3, F4, F5 | classifier replay over the capture corpus; `pipeline.py` call shape | **No** — no sub-agent, no tool, no inference |
| F15, F16, F17 | FRE-1337's probe: stateless single-turn model calls, no tools, no history | **No** |
| F7, F9, F12, F13 | the ledger's routing columns, the live OpenAPI document, the container's own file listing, live config | **No** |
| F14 | capture-level behaviour of turns that never expanded (all `SINGLE`) | **No** |
| F6, F8 | `sub_agent_count`, which counts workers **dispatched**, not workers that succeeded | **No** — the plumbing claim is about how many were spawned, and the repair does not change that a budget of 1 produced a fan-out of 4 |
| F11 | the 900s `orchestrator_task_timeout` wall | **Partly** — one of the eleven wall-hitting turns is an expansion turn whose wall included sub-agent time |
| **F10** | sub-agent success, digests, synthesis output | **Yes — withdrawn** |

Sixteen of eighteen findings are untouched. One is partly affected and says so. One is withdrawn.

**What should happen now, and it is a measurement, not a proposal.** All three fixes are live as of
11:57. The comparison F10 attempted is worth running properly: the same research brief, once through
`SINGLE` and once through `EXPAND`, on the repaired path, with FRE-1379's partial-progress reporting
making a killed worker legible and FRE-1380's AC-4 digest-vs-full-output figures showing what context
isolation actually bought. That is the measurement that would answer the commission's question about
answer quality. This study cannot answer it and should not have implied that it had.

### F19 — the owner is already the router, and does it by prefixing a keyword

**Verdict: POSITIVE.** Added 2026-09-04 in discussion with the owner, whose own account is the
hypothesis this tests: *"Right now I am at the mercy of the keyword router which basically only ever
chooses single conversation unless I expressly tell it to do research, build an artifact, chain
multiple requests in 1 query etc."*

**The query** — every capture whose `user_message` contains "General Product Safety", ordered by
timestamp, with the replayed rung and the observed behaviour alongside. The same question, asked
eight times over six days, in two phrasings.

**Its actual output:**

```
the SAME question, both phrasings (n=8):
  2026-08-29T21:38  rung=conversational secs=   902 tools=3  "What's changed in the EU's revised General Product Safety "
  2026-08-30T07:06  rung=conversational secs=   462 tools=3  "What's changed in the EU's revised General Product Safety "
  2026-08-30T07:09  rung=conversational secs=   110 tools=3  "What's changed in the EU's revised General Product Safety "
  2026-08-30T07:11  rung=conversational secs=   150 tools=4  "What's changed in the EU's revised General Product Safety "
  2026-09-03T22:15  rung=analysis       secs=   903 tools=2  "Research What's changed in the EU's revised General Produc"
  2026-09-04T04:03  rung=analysis       secs=   859 tools=2  "Research What's changed in the EU's revised General Produc"
  2026-09-04T05:15  rung=analysis       secs=   821 tools=3  "Research What's changed in the EU's revised General Produc"
  2026-09-04T07:16  rung=analysis       secs=   904 tools=2  "Research What's changed in the EU's revised General Produc"
```

One word, prepended, moves the classification deterministically. And the pattern generalises across
the recent real window:

```
recent real turns n=417
  begin with a router-steering imperative: 21  (5.0%)
  of those, the ladder classified: {'analysis': 14, 'tool_use': 5, 'planning': 1, 'conversational': 1}
  and NOT conversational: 20/21
  turns WITHOUT the imperative n=396: conversational 338 (85.4%)
```

(Steering imperative = the message opens with `research|analyz|analys|investigat|evaluat|compar|
build|create|generate|make|plan|outline|decompos|break down`.)

**↯ argues with this study's own earlier position.** P5 originally objected to the owner's routing
selector on the grounds that overrides would be too few and too uniform to be a useful dataset. That
objection is withdrawn, and this finding is why. **A manual routing control already exists — it is
just undocumented, unlabelled, and spelled as an incantation at the start of a sentence.** The
proposed selector does not add a control the user lacks; it replaces a hidden one with a visible one,
and it captures a choice the owner is already making that nothing currently records.

It also sharpens F2's headline. The 78% fallback rate is not simply "the router misses things" — it
is the rate at which a user who has *not* learned the incantation gets the low-capability lane.

---

### F20 — sub-agents have never been granted a tool, and neither planner can request one

**Verdict: NEGATIVE.** Arms carried below.

The owner's hypothesis, stated 2026-09-04: *"I hypothesize that using a properly configured subagent
would improve context management. Apparently this is already demonstrated."* The mechanism assumed —
and the one FRE-1380's ticket describes — is that *"a sub-agent runs the tool calls, absorbs the raw
results, and returns a digest."*

**The query** — every document in `agent-captains-captures-subagents-*` (scroll API, all 104 docs,
2026-06-07 → 2026-09-04), tallying `tools_granted` and `tools_used`.

**Its actual output:**

```
sub-agent captures: 104  window 2026-06-07 .. 2026-09-04
  success 88  failed 16
  tools_granted: [((), 104)]
  model_role:   [('sub_agent', 104)]
  mode:         [('parallel_inference', 104)]
```

Every sub-agent that has ever run was granted an empty tool list, and used none. **The primary makes
every tool call and absorbs every raw result**; the sub-agent is a pure inference call over context
the primary hands it (`context=messages[-4:]`, `expansion_controller.py:412`).

**Arm 1 — target-identifier provenance (1a, raw instance).** A capture quoted verbatim from the
store the finding queries:

```
index: agent-captains-captures-subagents-2026-09
  trace_id: 94fda7de4fce7905273532e78ccda063
  task_id: 3ff9e010-e092-43f0-9ddc-a25dd965f026
  model_role: sub_agent      max_tokens: 4096
  tools_granted: []          tools_used: []
  full_output_chars: 0       digest_chars: 0    truncation_ratio: 0.0
  success: False             error: Timeout after 85.0s
```

The identifier under test is present and populated in the document — as an empty list, not as an
absent field. This is the load-bearing distinction: a wrong field name yields *absence*, and absence
is what would have been returned had the name been wrong. `[]`, returned 104 times out of 104, is an
exhaustive enumeration of a live field rather than a query that found nothing.

**Arm 2 — path liveness, same query, only the field varied.** On the identical scroll over the
identical index and window: `model_role` returns `sub_agent` ×104, `mode` returns
`parallel_inference` ×104, and `full_output_chars` is non-zero on 87 documents. The index is
reachable, the documents are populated, and the query shape is sound.

**Arm 3 — scope match.** Store: production Elasticsearch, index pattern
`agent-captains-captures-subagents-*`. Window: all 104 documents, the full lifetime of the index,
2026-06-07 → 2026-09-04. The verdict is stated at exactly that scope: *no sub-agent recorded in this
index has ever been granted or used a tool.* It claims nothing about sub-agent runs that predate the
index or were never captured.

**Why it is this way, and it is smaller than "the feature is missing."** The dispatch wiring is
complete: `expansion_controller.py:424` passes `tools=task.tools` into `SubAgentSpec`, and
`sub_agent.py:144` records it. The gap is one level up, in the planner's own output schema —
`_PLANNER_SYSTEM_PROMPT` (`expansion_controller.py:56`) specifies
`{"strategy": ..., "tasks": [{"name","goal","constraints","expected_output"}]}` and **the string
`tools` appears in it zero times**. The LLM planner is structurally incapable of requesting a tool;
the deterministic fallback planner never mentions tools either. `PlanTask`'s own docstring
(`expansion_types.py:47`) says it outright: *"tools: Tool names available to the sub-agent (currently
always empty)."*

**What this means for the hypothesis.** The context-isolation benefit the owner is reaching for —
keeping raw tool transcripts out of the primary — is not what today's sub-agent delivers, because
the sub-agent never goes near a tool. The primary's within-turn growth (FRE-1138) is untouched by
expansion for exactly this reason. But the fix is a planner-schema field and a governance decision
about which tools a sub-agent may hold, not a new subsystem.

---

### F21 — the "digest" is a raw prefix slice at 2,000 characters, not a summary

**Verdict: POSITIVE.**

FRE-1380's AC-4 asks for the digest-versus-full-output figure and notes that *"nobody has ever
measured"* it. Here it is, over every successful sub-agent on record.

**The query** — the same 104-document scroll, restricted to the 87 successful sub-agents with
non-zero output.

**Its actual output:**

```
CONTEXT ISOLATION (n=87 successful sub-agents)
  full_output_chars  total   330618  p50   2564  p90  11425  max  12987
  digest_chars       total   130990  p50   2000  p90   2000  max   2000
  characters KEPT OUT of the primary's context: 199,628  (60.4% of sub-agent output)
  truncation_ratio   p50 0.780  mean 0.690

  by month:
    2026-06  n= 28  full  163621  digest  41488  kept out  122133  ratio 0.254
    2026-07  n= 33  full   93497  digest  46444  kept out   47053  ratio 0.497
    2026-08  n= 17  full   49667  digest  27798  kept out   21869  ratio 0.560
    2026-09  n=  9  full   23833  digest  15260  kept out    8573  ratio 0.640
```

`digest_chars` has p50 = p90 = max = **2000**. That is not a distribution; it is a wall.

**The mechanism, and it is one line.** `sub_agent.py:39` declares `_SUMMARY_CAP_CHARS = 2000`, and
both the success path (`:323`) and the killed-worker path (`:202`) construct the digest as
`response_content[:_SUMMARY_CAP_CHARS]` — **a raw prefix slice.** No second model call, no
summarisation, not even a sentence boundary. The 199,628 characters "kept out of the primary" were
**cut mid-word at 2,000 characters and discarded.**

So context isolation is real in the narrow sense that `_build_synthesis_context` composes only from
`r.summary` and never from `r.full_output` — the primary genuinely does not see the full transcript.
But what it does see is the *first 2,000 characters*, and what the sub-agent concluded in its last
paragraph is gone. Against a `max_tokens` of 4096, a sub-agent is routinely asked to generate roughly
twice what can survive the clip.

**↯ relevant to the owner's plan.** "Manage context and thinking" via better-configured sub-agents
runs into this before it runs into model choice: raising a sub-agent's output quality raises the
amount thrown away, because the clip is positional. Together with F20 these are the two things
standing between the current mechanism and the one the hypothesis assumes — and both are small.

---

## Proposals

Nine, in the order they should be considered. Each names the findings it rests on. Several are
*sequencing* recommendations rather than changes — the measurements say the order matters more than
the individual items.

### P1 — Split `conversational`'s two effects; lift the iteration cap. Whether to lift the expansion denial is left open

**Rests on:** F13, F14. **No new ticket — belongs to FRE-1288**, where the measurements were posted
as a comment.

`conversational` does two things under one label, and only one of them this study can speak to.

**The 6-iteration cap** — recommended for lifting on the no-match case. It is a real, occasionally
binding constraint on a bucket that demonstrably does research: it binds on 13 of 504 turns, while
128 of 339 recent conversational turns used `web_search`. Small, low-risk, no expansion-cost
implications, and it does not depend on anything in the sub-agent path.

**The expansion denial** (`conversational_always_single`) — **left open.** An earlier version of this
document recommended keeping it, on the strength of F10. F10 is withdrawn (F18), so that
recommendation is withdrawn with it, and this study does not have a replacement. It cannot: FRE-1288's
own "It fails if" clause demands that anything removing the SINGLE forcing state what then bounds
expansion cost, and answering that honestly needs a measurement of the repaired path that nobody has
yet taken.

What this study *can* contribute to that decision is the size of the population it would move —
78–81% of all traffic — and the fact that the label is assigned by an absence of evidence rather than
a presence of it. Both argue that the denial is doing more work than its author intended. Neither
tells you whether lifting it produces better answers today.

### P2 — Make the per-turn expansion budget actually bind

**Rests on:** F6. **Filed as FRE-1382.** **Owner direction, 2026-09-04: "ceiling after"** — this is sequenced behind the other work, not ahead of it. Recorded here so the ticket is not read as a front-of-queue item.

`expansion_controller.py:45` caps fan-out on a constant keyed by strategy name. `governance.expansion_budget`
— computed by the brainstem from live CPU and memory, emitted on every turn, and read exactly once as
a zero-check — is discarded. Two turns on 2026-09-04 carried `budget=1` and spawned 4 sub-agents.

The change is to take the minimum of the two. It is small, it makes the load-shed signal real, it
caps FRE-1374's over-fan-out at its source, and — the reason it belongs in *this* document rather
than a performance ticket — **it is the missing piece of the owner's proposed shape.** The brief says
"gateway sets the budget, model spends it, delegation is a separate explicit choice… Two of the three
already exist." Measured: the budget exists as a *number* but not as a *ceiling*. Wiring it is what
makes the three-part design describable at all.

### P3 — Re-measure expansion on the repaired path; this study cannot say what it is worth

**Rests on:** F18, and the retraction of F10.

**This proposal replaces one that said the opposite.** It previously read "repair expansion before
widening the door to it", and called itself the most important recommendation in the document. That
rested entirely on F10, which measured a turn running inside a three-fix repair with two of the fixes
not yet shipped. Withdrawn.

All three fixes — FRE-1374 (per-worker clock), FRE-1379 (a killed worker reports partial progress),
FRE-1380 (serialize the fan-out) — are live as of the 11:57 rebuild. The sequencing argument is
therefore moot on its own terms: the repair already happened.

What is left is an absence. **There is no clean measurement of what expansion buys**, in either
direction, because every expansion turn in the ledger predates the repair. The commission asks what a
wrong strategy costs in "tokens, latency and answer quality", and on answer quality the honest answer
from this study is: *unmeasured, and not measurable from the existing corpus.*

The measurement worth running is small and well-specified, and two of its instruments now exist:
the same research brief through `SINGLE` and through `EXPAND` on the repaired path, with FRE-1379
making a killed worker legible and FRE-1380's AC-4 digest-vs-full-output figures showing what context
isolation actually bought — the figure its own ticket notes "nobody has ever measured".

### P4 — Visibility before control: show what the router resolved to, as its own change

**Rests on:** F9. **Filed as FRE-1384.**

The owner's proposal already contains this condition ("AUTO MUST SHOW WHAT IT RESOLVED TO"). The
measurement says it is not a detail of the selector but a prerequisite that does not exist: the
deployed `/chat` operation's 200 response is `{"type":"object","additionalProperties":{"type":"string"}}`,
and `task_type` and `strategy` appear zero times in the live OpenAPI document. There is no typed
envelope to add a field to and no transport event carrying it.

Building the display alone delivers the thing every instance in this family has lacked — *"only a log
reader can tell it took the wrong path"* — and it is strictly cheaper than the selector. It should
ship first and be allowed to stand on its own.

### P5 — Build the routing selector. The objection this study raised against it is withdrawn

**Rests on:** F19. **No ticket — this is the owner's own proposal and their call to scope.**

**This proposal previously argued the opposite** and it was wrong. It objected that user overrides
would be too few (417 real turns in two months) and too uniform (95% `SINGLE`) to be a useful dataset,
and recommended scoping the control down to an escape hatch.

F19 dissolves that. **A manual routing control already exists.** The owner steers the router by
prefixing a keyword — the same question runs as `conversational` four times and as `analysis` four
times, the only difference being the word "Research" at the front — and across the recent window,
20 of 21 turns opening with a steering imperative escape the `conversational` lane while 85.4% of
those without it do not.

So the selector does not introduce a control the user lacks. It replaces an undocumented incantation
with a visible one, and records a choice the owner is already making that nothing currently captures.
The "future dataset" is a log of that choice — and its value does not depend on reaching a size that
would support fitting a classifier, which is the reading this study incorrectly attacked.

Two findings still bear on the design, as support rather than objection. **F9**: nothing on the
deployed client surface carries the resolved route, so "Auto, resolved to research" has no field to
live in yet — P4 remains a prerequisite. **F1**: with 95% of turns resolving to `SINGLE` today, a
selector shipped before the fallback is fixed would mostly offer a choice between one real option and
several that rarely apply.

### P6 — Reject the model-in-the-hot-path design; and reject the length-gated arbiter this study itself proposed

**Rests on:** F16, F17, F5, and the turn-latency baseline. **The alternative it points to is filed as FRE-1386.**

The brief asks for the deterministic-first latency tradeoff to be carried honestly rather than
assumed. It now has been, and the honest answer is that *neither* end of the tradeoff is attractive
as posed.

**Against a model call on every turn.** Measured per-call classification latency is **7.93s median /
14.41s p90 on the local primary** and **2.26s median / 2.66s p90 on Claude Sonnet**, against a live
turn median of 18.0s (`conversational` median 13.5s, p10 3.8s). On the local primary that is 59% of a
median conversational turn added before any work starts, and it more than doubles the fastest ones.
Worse than the latency: **3 of 60 local calls returned `503 ServiceUnavailable`** — one after 324.8
seconds of retries — because the SLM server is a single shared GPU also serving live traffic. A
5% hard-failure rate on a call that must complete before the turn can begin is not the same risk as a
5% failure inside a turn. Master's caveat on the ticket holds, and it now has numbers.

Cloud arbitration at ~2.3s is genuinely affordable. What makes it unattractive is not cost:

**Against the arbiter being an authority.** The two models agree with each other on only 84.2% of real
messages (F16). A second opinion that disagrees with a third opinion one time in six is not ground
truth; it is another classifier with its own error profile, and adopting it swaps a legible failure
(a regex matched the wrong substring) for the illegible one the brief itself names ("a wrong
judgement, harder to observe and harder to correct").

**Against the gate this study drafted.** An earlier draft of this document proposed gating an arbiter
to "no rung matched AND ≥ 25 words", sized at 8.4% of turns with 27/35 tool-using. F17 tested it and
it does not work: in the long-message gate the models mostly agree with the cascade, and every
substantive disagreement in the sample sits in messages *under* 15 words — including both turns where
the models unanimously overruled the cascade, at nine words each. The gate selects the wrong
population. It is recorded here as tested and rejected rather than quietly dropped, because it is the
obvious idea and someone will otherwise propose it again.

**What F17 points at instead.** The messages that need a second look are short, keyword-free, and
identifiable only from what surrounds them. That is F5: stage 4 receives `user_message` and nothing
else, by construction. If this axis is worth pursuing, the cheap experiment is not a model call — it
is giving the existing deterministic classifier the previous turn or two, and re-running the replay in
this document to see whether agreement moves. That costs no inference and no latency, and it is
testable against the corpus already assembled here.

### P7 — Collapse `HYBRID` and `DECOMPOSE` into one `EXPAND` strategy carrying a numeric cap

**Rests on:** F8. **Filed as FRE-1383.**

Two enum members, one shared `elif` branch, one shared controller, one shared planner, differing
only by `_MAX_TASKS`. Over 649 turns the distinction has produced a different outcome once. The
brief's reading — "two real states and a magnitude knob" — is correct; this is the change that makes
the code say so. It also removes one of the two things a user-facing selector would otherwise have to
render in user vocabulary, which the brief rightly flags as a test of whether the taxonomy carries
the right distinctions.

Do this together with P2, since both touch the same cap.

### P8 — Decide `DELEGATION`'s status explicitly rather than leaving it gated-off

**Rests on:** F7, F4, F11. **Filed as FRE-1385.**

The commission asks whether a task type should exist for a capability that is not implemented. The
measured answer: over the ledger's entire history the DELEGATE route has run four times and delegated
zero times; `delegation_enabled` is `False` on the deployed process; of three adapters, two declare
themselves stubs in their own docstrings and nothing in `src/` constructs any of them. Meanwhile the
type has cost at least one real misroute with a 921-second turn attached.

FRE-1376 has made the type harmless by default, which is the right emergency measure and the wrong
resting state — a permanently-false flag guarding a permanently-dead branch is a thing future readers
must reason about forever. Either wire `claude_code.py` behind the existing flag and give the type a
purpose, or remove the type and let coding requests classify on their content like everything else.

### P9 — Record what was measured: correct FRE-1288's premise, and document the ladder's ordering as adjudicated

**Rests on:** F12, F2, F3. **No new ticket — a description amendment requested on FRE-1288, plus a docstring to fold into whichever ticket next touches `intent.py`.**

Two small record-keeping actions, both aimed at stopping the same work being redone.

FRE-1288 is Approved, Urgent and unstarted, and one of its three stated effects plus one of its four
decision questions concern `request_gateway/recall_controller.py`, deleted on `main` in `b0600ec3`.
Amend the **description** (not a comment — a contradicting comment does not supersede a description)
so the ticket asks the two questions that survive.

Master's comment asked for the ladder's ordering to be named as a finding. It is: ordering decides
3.8% of turns, agrees with position weighting on 79 of 81 collisions, and position weighting would
flip two messages in 2,125 — one of them the wrong way. A short docstring note in `intent.py`
recording that the order was measured on 2026-09-04 and found not to be load-bearing closes the
"undocumented" half without touching the mechanism, and stops the next reader re-opening it.

### P10 — Close the two gaps between today's sub-agent and the one the context-isolation argument assumes

**Rests on:** F20, F21. **Filed as FRE-1387.** Added at the owner's request, 2026-09-04.

This is the one proposal here that is not about the router. It earns its place because it conditions
P1 and P3: the value of routing a turn *to* expansion depends on what expansion does when it gets
there, and the owner's stated plan — *"use larger models, manage context and thinking"* — meets these
two before it meets model choice.

**Gap 1: the planner cannot ask for a tool.** The dispatch wiring is complete
(`expansion_controller.py:424` → `SubAgentSpec.tools` → `sub_agent.py:144`), but
`_PLANNER_SYSTEM_PROMPT`'s JSON schema has no `tools` field, so `PlanTask.tools` is always empty and
104 of 104 recorded sub-agents ran with none. The consequence: the primary still makes every tool call
and absorbs every raw result, which is exactly the growth the mechanism is supposed to contain.

**Gap 2: the digest is a prefix slice.** `summary = response_content[:2000]` — no summarisation, no
sentence boundary. Measured: `digest_chars` p50 = p90 = max = 2000 across 87 successful sub-agents,
60.4% of output discarded mid-word. Against `max_tokens` 4096 a sub-agent is routinely asked to
generate about twice what can survive.

**Sequencing note.** Gap 1 without gap 2 makes things worse, not better: a tool-using sub-agent
produces *more* output, and more output against a positional clip means more of the conclusion thrown
away. If only one ships, ship the digest fix.

**Deliberately not specified here.** Which tools a sub-agent may hold is a governance question
(`config/governance/tools.yaml`), not a plumbing one, and it is the owner's call — a sub-agent with
`bash` is a different risk object from one with `web_search`.

---

## Filed tickets

Every ticket this study created, `Backlog`, none promoted. This list is the instrument by which the
study's footprint is audited, so it is complete by obligation: any ticket traceable to this study and
absent here would itself be a violation.

| Ticket | Proposal | Title |
|---|---|---|
| **FRE-1382** | P2 | The per-turn expansion budget is computed, emitted, and ignored — two turns with budget=1 spawned four sub-agents |
| **FRE-1383** | P7 | HYBRID and DECOMPOSE differ only by a cap and have produced a different outcome once in 649 turns — collapse them into one EXPAND strategy |
| **FRE-1384** | P4 | The routing decision reaches no client surface — show what the router resolved to, before offering a control over it |
| **FRE-1385** | P8 | Decide DELEGATION's status — the route has run four times, delegated zero times, and now sits behind a permanently-false flag |
| **FRE-1386** | P6 | Stage 4 classifies from the user message alone — test whether giving it the previous turns moves agreement, at zero inference cost |
| **FRE-1387** | P10 | Sub-agents are granted no tools and their "digest" is a 2,000-character prefix slice — close both gaps before tuning models |

**No ticket was filed for P1, P3, P5 or P9**, deliberately.

- **P1** (split `conversational`'s two effects) belongs to **FRE-1288**, which is Approved, Urgent
  and unstarted and already asks exactly that question. A comment carrying the measurements was
  posted there rather than a competing ticket.
- **P3** is now a call for a measurement rather than a sequencing recommendation, and the repair it
  would have sequenced behind (FRE-1374, FRE-1379, FRE-1380) has already shipped. No ticket filed:
  the comparison it asks for is largely FRE-1380's own AC-4 and AC-5, which are still to be
  evidenced at that ticket's gate.
- **P5** (scope the routing selector) is a disposition on the owner's own proposal. It is the
  owner's call, not a work item this seat should manufacture.
- **P9** is one description amendment on FRE-1288 (requested in the comment; explore does not edit
  another ticket's description) and one docstring, small enough to fold into whichever ticket next
  touches `intent.py`.

Comments were also posted on **FRE-1288** (measurements + the stale `recall_controller.py` premise)
and **FRE-1337** (arm 2 run, results, the two forced deviations, and the fixture-vs-real-traffic gap).

---

## Method appendix

### M1 — what "the deployed revision" means here

Identified by container file hash, not by ticket state:

```
                                     container          main@16af9e28      pre-FRE-1376
request_gateway/intent.py            3cd8ceb0dbb06dae   3cd8ceb0dbb06dae   52f2786747c8f792
request_gateway/decomposition.py     fe6515b22babe7c8   fe6515b22babe7c8   7619136b9fb5322b
orchestrator/expansion_controller.py 3fcb9678d379da75   3fcb9678d379da75   3fcb9678d379da75
```

The running `cloud-sim-seshat-gateway` carries `main@16af9e28`, i.e. FRE-1376's fix is live. All
replays in this document run that revision.

### M2 — live config, read from the running process

Read via `docker exec cloud-sim-seshat-gateway /app/.venv/bin/python -c "from personal_agent.config import settings; ..."`.
Note the interpreter: the container has no system-level `personal_agent`, and `PYTHONPATH=/app/src`
with the system python fails on missing deps — the venv at `/app/.venv/bin/python` is the only way
to read the process's own configuration.

```
delegation_enabled        = False
expansion_budget_max      = 3
route_trace_store_preview = False
orchestrator_task_timeout_seconds = 900
slm_base_url              = http://caddy:8600
orchestrator_max_tool_iterations_by_task_type =
  {'conversational': 6, 'memory_recall': 8, 'analysis': 25, 'planning': 25,
   'tool_use': 25, 'delegation': 25, 'self_improve': 25}
```

### M3 — the two forced deviations in running FRE-1337's harness

Both are recorded on FRE-1337 as well, because the next person to run it will hit them.

1. **Model set.** The harness's `MODEL_KEYS` names `qwen3.6-35b-thinking`. `GET
   http://localhost:8600/v1/models` returns exactly one served model — `unsloth/qwen3.8-flash-next`,
   llamacpp backend, port 8502 — so the local arm was run with `qwen3.8-flash-next`. This is a
   consequence of single-model serving, not a harness defect.
2. **Endpoint.** `AGENT_SLM_BASE_URL` had to be set to `http://localhost:8600` (the host-side
   publication of the Caddy egress the deployed gateway itself uses). The harness's test-environment
   default resolves to nothing on the VPS and fails with `LLMConnectionError` after three litellm
   retries.

A third snag, not a deviation: `config/governance/budget.yaml` is gitignored, so a fresh worktree
aborts at `CostGate` construction with `BudgetConfigError` before any model call.

### M4 — the corpus and the join

`route_traces` stores no message text: `user_message_preview` is gated behind
`route_trace_store_preview`, live value `False`, and `count(user_message_preview) = 0` across all 727
rows. The Postgres `captains_log_captures` table is empty (0 rows). The stimulus lives only in
Elasticsearch.

Corpus: every document in `agent-captains-captures-*` (2,229 docs, scroll API), of which **2,125**
carry a non-empty `user_message`. Joined to `route_traces` on `trace_id` after normalising the ES
hex form to the Postgres UUID form; **563** of the 649 turn-level rows join.

Populations used, and why they are reported separately:

- **All captures (n=2,125)** — the broadest base. Dominated by `agent-captains-captures-2026-05`
  (1,541 docs) which is early, short-message traffic; reported for scale, never on its own.
- **Recent real (non-eval, `timestamp >= 2026-07`, n=417)** — the population that reflects how the
  system is used now. Every headline percentage is given for both.
- **Joined subset (n=563)** — used only for instrument validation against recorded `task_type`.

### M5 — instrument validation

The replay is not trusted on assertion. Over the 563 joinable turns it reproduces production's
recorded `task_type` on **561 (99.6%)**. Both disagreements are explained: one is the FRE-1376
incident (post-fix code replayed against a pre-fix trace — the fix landing, visible in the data), the
other a `_TOOL_INTENT_PATTERNS` addition postdating its trace. Everything in this document that rests
on replay rests on that number.

### M6 — two instrument errors made and caught, recorded because they are the standing traps

**`_cat` lied by a factor of 60.** `GET _cat/indices/*capture*?h=index,docs.count` reports
`agent-captains-captures-2026-08` at **15,776** docs. `GET agent-captains-captures-2026-08/_count`
returns **249**. `_cat`'s `docs.count` counts Lucene documents including nested ones. Every count in
this document comes from `_count`.

**A wrong field name produced a clean, plausible zero.** The first query for gateway telemetry used
`{"term":{"event.keyword":"gateway_output"}}` and returned `total: 0`. The field in `agent-logs-*` is
`event_type`, not `event`. Re-run correctly it returns 22. Had that zero been reported, it would have
read as "the gateway emits no output events" — a system property, invented by a typo. It is recorded
here rather than silently corrected, because it is exactly the failure the admissibility rule exists
to catch, and it happened on the first query of this study.

### M7 — what was measured against, and what was not

**Measured against:** production Postgres (`route_traces`, 727 rows / 649 turn-level, 2026-06-07 →
2026-09-04); production Elasticsearch (`agent-captains-captures-*` — 2,229 docs;
`agent-captains-captures-subagents-*` — 104 docs, the store behind F20/F21; `agent-logs-*` — 431,002
docs); the running `cloud-sim-seshat-gateway` container (config, file
hashes, live OpenAPI document); the SLM server's model list across the Caddy egress; and three live
model deployments via FRE-1337's probe.

**Not measured, and why:**

- **FRE-1337 arm 3 (behavioural, full live turns through the isolated eval gateway).** Requires
  standing up `seshat-gateway-control` plus four eval substrate containers — outside a read-only
  seat's scope. Its intent was covered instead by reading the behavioural signals (tool count,
  `web_search` presence, wall clock, token totals) back from the existing capture corpus for every
  turn in every population above, which is the same adjudication over a much larger n and with no
  new infrastructure. Arm 3 remains genuinely unrun.
- **No live gateway turn was fired.** Every behavioural number is read from turns the owner already
  ran.
- **Answer *quality*** — the commission asks what a wrong strategy costs in "tokens, latency and
  answer quality". Tokens and latency are measured throughout. Quality is proxied by
  `final_reply_chars` in F10, which is a length, not a judgement. The 160-vs-23,732 comparison is
  stark enough to carry weight, but it is two turns and a character count, and it is labelled as
  such rather than dressed as an evaluation.

### M8 — provisionality

Per the project's standing caveat (FRE-1051), Elasticsearch counts are **provisional**: ADR-0090 has
no delivery corner and ES has been measured losing up to 83% of emitted events on some days. This
affects the ES-derived populations here — the capture corpus and the `agent-logs` event counts — in
one direction only: they may under-report. Nothing in this document argues from an ES zero. The
Postgres `route_traces` figures are not subject to this; the ledger is a synchronous, bus-independent
write.

Sample sizes are small in two places: F17's gated subpopulation is n=5, labelled at the point of
use. F10 rested on two turns and drew a conclusion beyond what that n supported — it is retracted in
place, with F18 explaining why the n was not the only problem with it.

### M9 — reproducing this

Scripts were written to the session scratchpad rather than committed, since the explore contract
scopes this branch to one file. Each is a few dozen lines over the artifacts named above:

- `dump_captures.py` — scroll every `agent-captains-captures-*` doc to JSONL.
- `analyze.py` — replay all six rungs independently; pre/post-FRE-1376 diff; behavioural profile.
- `matrix.py` / `matrix2.py` — full stage-4 + stage-5 replay, by window.
- `position.py` — the position-weighting counterfactual.
- `corpus_probe.py` — FRE-1337's `classify_with_model()` over a seeded (1377) 60-turn sample, timed.
- `probe_report.py` — confusion matrices, latency percentiles, behavioural adjudication.
- (F19–F21, added later) an inline scroll over `agent-captains-captures-subagents-*` tallying
  `tools_granted` / `tools_used` / `full_output_chars` / `digest_chars`, and a regex census of
  router-steering imperatives over `recs.json`.

Every SQL statement and ES query is quoted verbatim in the finding that uses it.
