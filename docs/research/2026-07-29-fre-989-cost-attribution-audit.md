# Cost attribution audit — where LLM spend comes from, and which store can be trusted to say so

**Date:** 2026-07-29
**Ticket:** [FRE-989](https://linear.app/frenchforest/issue/FRE-989) · prompted by [FRE-987](https://linear.app/frenchforest/issue/FRE-987) (the ~100× daily-spend incident)
**Backing design:** ADR-0065 (Cost Check Gate — operative). ADR-0120 (supersedes 0065) is **Proposed**.
**Audited at:** `origin/main` @ `002e2de7`, 2026-07-28/29.

---

## The question this audit exists to answer

The FRE-987 incident exposed something worse than a cost spike: **spend could not be attributed to the
process that caused it.** A deliberately simple test — the owner asking the agent "do you see any
captains_log cost today" — produced four independent wrong answers before producing no answer. The
audit's job was to establish, mechanically, which invocation paths exist, which budget each bills, and
which store can be believed.

The headline finding is not any one of the eight below. It is that **there were three paid paths, and
only one of them was fully instrumented.**

---

## Answer first: what to read for a cost question

**Postgres `api_costs` is the authoritative store.** Append-only, one row per call, real timestamps,
and role-attributed through its `purpose` column.

Its completeness, stated exactly — because a ledger you trust more than it deserves is worse than one
you distrust:

| Path | Reserves against the gate? | Row in `api_costs`? |
|---|---|---|
| `LiteLLMClient` (all three acquisition doors) | Yes | Yes |
| Gateway streaming (`gateway/chat_api.py`) | Yes — but **settled $0** until FRE-989, see F9 | **Yes — as of FRE-989** (was: no) |
| Cloud DSPy (`get_dspy_lm`, Captain's Log reflection) | **Yes — as of FRE-989** (was: no) | **Yes — as of FRE-989** (was: no) |
| Embeddings / rerankers (`record_vendor_cost`) | **No** — deliberate, see F5 | Yes |
| `LiteLLMClient` calls with no `session_id` | Yes | **No** — ADR-0074 identity contract |
| Intermediate LiteLLM retry attempts | Once, for the job | One row, for the final response |
| Raw `litellm.acompletion` in three eval entry points | No | No |

**`budget_counters` is a per-window ledger, not a current-state gauge.** It keeps one row per role per
window and never supersedes prior windows. A read that does not constrain `window_start` to the current
window will happily return a *closed* window's total — and since `updated_at` moves with the last write,
yesterday's row can look fresher than today's. **A missing row for today means zero, never yesterday.**
This is the mechanism behind the incident's worked example: $5.02 reported as "today, 100.3% of cap,
still over" when today's real figure was $3.67 and the process had been stopped fourteen hours earlier.

**Elasticsearch is a mirror, and only from 2026-07-29 forward.** Before FRE-989, `api_cost_recorded`
carried no `purpose`/role field at all, which made a role-level cost question unanswerable from ES *by
construction* rather than by difficulty. It was also emitted at `debug`. Both are fixed. Two further
traps remain for any ES consumer: the time field is `@timestamp` (a range filter on `timestamp` matches
nothing and returns a clean zero, indistinguishable from an honest absence), and historical documents
predating this change carry no role.

---

## Findings

Numbering follows the ticket; **F6–F9 were found during the audit and are new** — F9 during the
adversarial self-review of the fixes for F1–F8, which is worth noting on its own: the audit's own
output needed auditing.

### F1 — Role resolution was partial, and `study` fell through it · **fixed**

`budget_role_for` mapped factory role names to budget lanes and **fell back to `main_inference`** for
anything it did not recognise. FRE-1037 added `session_summary`, `vision` and `skill_routing`, leaving
`study` — a role it had just newly threaded — resolving to the wrong lane.

Nothing mis-billed in practice: `study`'s only live call site constructs `LiteLLMClient` directly with
`budget_role="study"`. But FRE-1037 widened `ModelRole` from four members to fourteen, so the fallback's
blast radius had **tripled** while this sat approved.

The deeper problem is not the missing entry, it is that a silent default is *indistinguishable from a
correct mapping at every downstream layer*. The counters, the ledger and the telemetry all record the
wrong lane with full confidence.

**Fixed:** resolution is now total and fail-closed (`cost_gate/role_map.py`) — an unknown name raises
`UnknownBudgetRoleError`. Three mechanisms keep it unreachable for declared roles: a CI guard
(`config_guard.check_budget_role_coverage`), a **startup** validator (`validate_role_totality`, wired
into the lifespan because `budget.yaml` is a runtime file baked into the image, so CI validates the tree
and not the container that ships), and unit tests. All three delegate to one `role_totality_findings`,
so they cannot disagree about what "consistent" means.

### F2 — "Unlimited and unmeasurable" was half right · **corrected, and the measurable half fixed**

The ticket said `insights`, `promotion` and `freshness` "pass the gate unbounded". **They do not.**
`BudgetConfig.caps_for()` returns same-role caps *plus* the synthetic `_total` caps, and `_total` weekly
is $30 — so all three are bounded collectively. What is true: they get **no per-role counter row**, so
per-role counter queries and the FRE-547 cap-utilisation panel show nothing for them.

"Unmeasurable" was also only true of the read path. `LiteLLMClient` writes `purpose=self.budget_role`
into `api_costs` on every paid call *regardless of whether any cap applies*. Per-role spend was always
measurable in Postgres; it was ES that could not answer.

**Also found:** `promotion` and `freshness` have **zero call sites** anywhere in `src/` or `scripts/`.
They are dead budget lanes. `insights` is live.

**Fixed:** ES now carries `purpose` at `info` level. **Deliberately not fixed:** no new dollar caps —
see *What was decided not to do*.

### F3 — Three conflicting defaults · **fixed, by removing all three**

| Door | Old default | Daily cap it implied |
|---|---|---|
| `get_llm_client_for_key` | `skill_routing` | $0.10 |
| `LiteLLMClient.__init__` | `main_inference` | $10.00 |
| `budget_role_for` fallback | `main_inference` | $10.00 |

A call that omitted the lane landed in a different budget depending on which door it came through —
either the user-facing budget or a near-zero one, arbitrarily.

**Fixed:** reconciled to **zero** defaults. `budget_role` is required and keyword-only at both
constructors, and the resolver raises. Stronger than picking one winner: an omission is now a
`TypeError` at construction rather than a plausible wrong answer at billing time.

### F4 — Three acquisition paths, not two · **documented**

`get_llm_client(role_name=…)`, `get_llm_client_for_key(key, budget_role=…)`, and **direct
`LiteLLMClient(...)` construction**. Every production construction already passed `budget_role`
explicitly, so this was a latent trap rather than a live defect; F3's fix closes it by construction.

### F5 — Embeddings and rerankers bypass the gate entirely · **answered, deliberately not closed**

`record_vendor_cost` writes an `api_costs` row with `purpose="embedding"`/`"reranker"` and **never calls
`CostGate.reserve()`**. Their cost is *recorded* but never *reserved*; no cap applies at any window.

Left as-is and declared in `NON_GATED_ROLES` with the rationale in code. Gating them needs a token
estimator for the vendor API shape, and is an ADR-0120 decision rather than an audit fix. **Follow-up
ticket filed.**

### F6 — The role-name path cannot make a paid call for `skill_routing` or `study` · **new**

Neither role has a Layer-3 binding in `config/model_roles.yaml`. `resolve_role_target` therefore treats
the role name as a deployment key, finds no definition, and `_build_client` returns a **`LocalLLMClient`**.

This matters because the ticket's AC-4 is worded against that path — "a call made through the role-name
path for skill_routing or study is charged to that role's budget" — and that path does not exist. The
criterion is instead proved against the door each role *actually* uses: key-based for `skill_routing`
(the executor's skill router), direct construction for `study` (the study categorizer). A regression test
pins the local-client behaviour so nobody assumes otherwise later.

### F7 — Gateway streaming was a paid path with no ledger row · **fixed**

`gateway/chat_api.py` talks to the Anthropic SDK directly. It reserved and committed against the gate
correctly, and emitted `model_call_completed` — but **never called `record_api_call`**. A paid streaming
turn moved the `main_inference` counter while remaining invisible in the store this audit names
authoritative. It also proceeded **ungated** when the gate singleton was absent, logged at `warning`.

**Fixed:** the success path writes its `api_costs` row (`purpose="main_inference"`), best-effort so a
ledger failure cannot break the user's stream. The ungated-proceed case is now `log.error` — a paid call
running with no gate is an operational fault, not a note.

### F9 — Every gateway streaming turn committed **$0** · **fixed** (found in self-review)

`_commit_reservation_safe` priced the turn with
`litellm.model_cost.get(f"anthropic/{_CLOUD_MODEL}", {})`. Verified against the installed litellm:
**there are zero `anthropic/`-prefixed keys in that table** — Anthropic models are indexed by the bare
id (`claude-sonnet-5`), with the prefixed spellings being bedrock-style (`us.anthropic.…`). So the
lookup always returned `{}`, both prices were zero, and **every streamed chat turn settled at $0**.

The consequence is not cosmetic: the `main_inference` daily counter never moved for gateway chat, so
its $10 cap was **unreachable by that path**. The reservation was taken and then fully released on
commit.

`cost_estimator.py` had already solved exactly this, with a comment saying so — "indexes some models by
the prefixed form and others by the bare id. Try both rather than silently fall back to $0." The
gateway did not use it. That lookup is now a shared `lookup_model_pricing` helper, used by every path
that prices a call, plus a test with **no mock** asserting the shipped model is actually priceable —
so this cannot regress silently again.

This one is worth dwelling on: it is the same class as F1 (a lookup that misses returns a plausible
value rather than an error) and it survived in a *money* path for as long as the gateway has existed.

### F8 — The cloud DSPy channel neither reserved nor recorded · **fixed** (the largest hole)

`get_dspy_lm` builds a raw `dspy.LM` with the provider API key for any cloud-placed role. Every budget
control this project has — reservation, cap enforcement, the ledger row carrying the budget role — lives
in `LiteLLMClient`. DSPy does not go through it.

`reflection_dspy.py` uses that LM with `role=CAPTAINS_LOG`, whose ADR-0121 binding is `claude_sonnet` —
**cloud**. So Captain's Log reflection, *the exact role behind the FRE-987 incident*, was making paid
calls that reserved nothing and recorded nothing, for the entire period the incident was being
investigated.

**Fixed** at **job scope** (`llm_client/dspy_gate.py`). `dspy.LM.forward` is synchronous and the
reflection caller already runs it in a worker thread, so reserving inside `forward` would need an
`asyncio.run_coroutine_threadsafe` bridge back into the caller's loop — a real failure surface for no
extra fidelity, since a predictor call is one logical unit of work. ADR-0120 names this shape:
keep `reserve`/`commit`/`refund`, re-applied at *job* scope.

Actual cost comes from DSPy's own bookkeeping: `BaseLM._process_lm_response` appends
`{"cost": <litellm response_cost>, "usage": {...}}` to `lm.history` per call, and `configure_dspy_lm`
builds a fresh LM per job, so the history is exactly that job's calls. Three cases are kept distinct
rather than collapsed: a **metered** cost, a **locally-priced** fallback when the provider reported no
cost, and **unavailable** — which commits the original estimate rather than settling at zero, because
handing headroom back for spend that did occur is the failure this whole ticket is about. A fully-cached
job genuinely costs zero and settles there.

Only the **cloud** placement is gated: a local DSPy call is free and must not consume a paid lane's
headroom.

---

## What was decided, and not merely fixed

**Should an absent cap mean unlimited?** The behaviour made *forgetting* a cap indistinguishable from
*deciding* not to have one, with no signal either way. Resolved by making the decision explicit rather
than by changing the semantics: `budget.yaml` now carries an `uncapped_roles:` list, and both the CI
guard and the startup validator reject a declared role that is neither capped nor listed there.

**Should `insights`, `promotion` and `freshness` be capped, and at what?** **No — not now.** ADR-0120
supersedes ADR-0065 and its first decision is to remove hard, process-breaking dollar caps outright.
Inventing cap values today is work that ADR would delete. Per-role spend is measurable in
`api_costs.purpose`, so a number can be set from observation whenever ADR-0120 resolves. All three are
recorded in `uncapped_roles` with that reasoning inline.

**Should the acquisition paths be unified?** No — they serve genuinely different trust levels
(`selection_key` guardrail vs trusted-config key). Instead the *difference* was made impossible to use
by accident: no door has a default lane any more.

---

## Follow-ups filed

1. **Counter-read contract for cost consumers** — the ES cost skill and the agent read path must
   constrain to the current window or read `api_costs` directly. A confident wrong answer about cost is
   more expensive than no answer.
2. **Decide whether `record_vendor_cost` paths should reserve** (F5) — ADR-0120 decision.
3. **Retire the dead `promotion` / `freshness` lanes** — declared, zero call sites.
4. **Raw `litellm.acompletion` in the FRE-630 eval scripts** — outside gate and tracker. Two call
   sites, `scripts/eval/fre630_extraction_quality/relabel_v2_types.py:293` and
   `.../relabel_v2_rels.py:277`, but **three** runnable entry points: `.../adr0109_boundary_probe.py`
   imports `classify_all` from `relabel_v2_types`, and says so in its own docstring. Low severity
   (eval-only, hand-run), but it is unmetered spend.

---

## One framing point worth keeping

This was a deliberately simple test — one question, one role, one day — and it produced four independent
read-path defects and, on follow-up, three unmetered write paths. **The failures were not in the
complicated cases.** Any future audit here should start from the assumption that the simple path is
broken too, and check it rather than reason about it: every claim in this document was established by
reading the code at a cited line or by a test that fails when the claim stops holding.
