# ADR-0145: Two Nouns and the Dialect Between Them — Finish ADR-0121

**Status:** Proposed
**Date:** 2026-09-06
**Deciders:** Owner (architect) · adr session
**Tags:** configuration, model-management, llm-client, resolver, concurrency

---

## Context

**What is the issue we are addressing?**

The owner stopped work on 2026-09-06 with one sentence: *"Full stop. I need an explore session to
analyze our model management. I think we have created a terribly difficult to manage system."*

Two studies answered that commission. They agree, and together they decide this ADR.

### The cause: ADR-0121 shipped a schema and never migrated the catalog onto it

ADR-0121 decided in July 2026 that a role binding carries per-use policy, and a catalog entry carries
only what the model is. `RoleBinding` gained `max_tokens`, `temperature`, `disable_thinking`,
`reasoning_effort` and `default_timeout` (`llm_client/models.py:123–166`). Its own docstring states
the intent:

> *"Decoding parameters and effort live here, not on the deployment, because they are per-use. This
> is what dissolves the primary/sub_agent duplication: they stop being two 'models' and become two
> bindings of one model at different effort — which is what they always were."*

The catalog was never migrated. **No binding uses any per-use field today.** Instead the catalog
holds two entries for one served model, one per role, and per-use policy lives on the deployment
entries, in settings, and at call sites.

The second entry is the remainder of a workaround. Before ADR-0141 the client could not send
`enable_thinking`, so a second deployment with the flag baked in was the only way to get a
no-thinking worker. ADR-0141 fixed the client on 2026-09-03. The workaround is still in the catalog.

### What that one un-migrated fact costs, measured

| Symptom | Evidence |
|---|---|
| One served model, two catalog entries, descriptions disagree | Server reports `quantization: UD-IQ4_XS`; `qwen3.8-flash-next-instruct` declares `4bit` (FRE-1421 F1) |
| Role policy sits on a deployment, so a user's model choice imports a worker's budget | The FRE-1420 incident: primary resolved to the instruct entry and inherited `default_timeout: 90` and `max_tokens: 2048`; the synthesis call raised `LLMTimeout` at 90.001 s (FRE-1421 F5, F12) |
| Any role-level budget is dropped exactly when a user picks a different model | `resolve_role_target` returns early when `key != binding.deployment` (`model_loader.py:361`). Simulated at the deployed revision: adding `primary.default_timeout = 600` left the instruct selection at timeout 90, `max_tokens` 2048 — unchanged (FRE-1421 F4) |
| The pairing table has no readers | Live turn 2026-09-06: owner selected `qwen3.8-27b-ovh` as primary; all three sub-agents ran on `unsloth/qwen3.8-flash-next`, the static binding default. `api_costs` records no OVH call outside the two primary-role ones (FRE-1421 F6, and master's live-turn comment) |
| No record can say which catalog entry ran a past turn | Two entries share one wire id; every ES `model_call_*` row for the incident reads `unsloth/qwen3.8-flash-next` (FRE-1421 F12) |
| The picker offers a role profile as a selectable model | `role_candidates` offers every available `kind: llm` entry (`model_loader.py:485`), so `-instruct` appears in the PWA model list |

### The second study: placement does not predict the wire

FRE-1430 probed every declared model with raw HTTP, 948 calls for USD 0.20. **Five declared chat
models speak five dialects, and no two agree.** The counterexample that separates placement from
dialect is OVH: a cloud-placed, open-weights Qwen that accepts OpenAI-standard `reasoning_effort`
and refuses the Qwen-native `chat_template_kwargs`. Two Anthropic models from one vendor disagree
with each other — Sonnet 5 rejects `temperature` as deprecated, Haiku 4.5 accepts it but refuses it
next to `top_p` (FRE-1430 F1, F6).

Three further facts from that study bind this ADR:

- **On llama-server, rejection tells you nothing.** An unknown field returns 200. Six declared or
  client-sent levers are inert there: `repetition_penalty` (the wire name is `repeat_penalty`), the
  top-level `enable_thinking` and `preserve_thinking` forms, `thinking_budget`, and
  `response_format: json_object` (F2).
- **`reasoning_effort` is accepted and inert on the local build** at n=2 across five prompt shapes;
  `enable_thinking` is the only local thinking lever that does anything (F16).
- **litellm's capability map is wrong about OVH at the provider level.**
  `get_supported_openai_params("ovhcloud")` returns `OpenAIGPTConfig`'s base list with no
  `reasoning_effort`, for every model. The FRE-1007 guard therefore returns `None` today and would
  return `False` the day OVH's cost-map record appears. Both outcomes omit the value. The oracle
  cannot become right for this provider by any change in litellm's data (F8).

### What needs to be decided

The owner's own model of the system is simpler than the code's, and it is the right one. **Two
nouns.** A *model* is what you pick: a name, where it runs, what it can do, and its card's modes. A
*role* is who uses it and how: which model, which mode, what budget, what priority. "Deployment"
stops being a word the owner meets.

A dialect is **not** a third noun. It is machinery: declared once per provider, overridable on a
model that disagrees with its provider, and never seen in ordinary use. It exists so that "which
mode" can mean the same thing on five wires that spell it five ways.

---

## Decision

Seven decisions. D1, D2 and D4 finish ADR-0121. D3 supplies the vocabulary the collapse needs. D5,
D6 and D7 close the gaps the collapse opens.

### D1 — Collapse the local pairs. `inherit` replaces the pairing table.

Delete `qwen3.8-flash-next-instruct` and `qwen3.6-35b-instruct` from the catalog. Their role policy
moves onto the `sub_agent` binding, which has carried the fields since July:

```yaml
sub_agent:
  deployment: inherit          # the primary's model, whatever the session picked
  mode: worker                 # the dialect's own cheap mode — D6
  max_tokens: 2048
  default_timeout: 90
  priority: ELEVATED           # D7
  open: true
```

`inherit` is a **resolver value, not a YAML convenience**. It resolves to the session's primary
deployment, and it must resolve in **every** path that reads a binding's deployment. There are six,
not the three the FRE-1421 study named:

| Path | Today | Under D1 |
|---|---|---|
| `resolve_role_target` (`model_loader.py:356–361`) | `binding.deployment` literally | resolves the sentinel against the primary's resolved key |
| `resolve_selected_deployment` (`model_loader.py:474`) | returns `binding.deployment` literally | same resolution |
| Catalog validator (`models.py:536`) | rejects any binding deployment absent from `models:` | accepts the sentinel, and validates that the role it names is resolvable |
| Factory (`factory.py:169`) | asks `get_current_selection("sub_agent")`, always `None` | passes the primary's selection when the binding says `inherit` |
| Reasoning guard (`config_guard.py:1147–1151`) | reads the raw binding deployment; a key absent from `models:` hits `continue` | must resolve the sentinel, **or it silently skips the sub-agent binding as if it were dangling** — the guard would stop covering the one role this ADR moves |
| Artifact constraint options (`constraint_options.py:269`) | returns `binding.deployment` | same resolution. Hard-wired to `artifact_builder` today, so it is not a live break — it is proof that "the resolver is the only reader" is false, and the next role added to it would break silently |

After D4 folds the `roles:` matrix into `bindings:`, `resolve_role_model_key`
(`model_loader.py:291–322`) becomes a seventh, and `config/resolve.py:59–62` inherits the risk
through it.

**The factory edit is the one that fixes the owner's actual complaint.**
`get_current_selection("primary")` is already available at that call site. Today the primary
selection reaches nothing: choosing `qwen3.8-27b-ovh`, or `claude_sonnet`, or any other primary,
still produces local Qwen workers.

**One correction to the binding above, which the FRE-1421 study also recorded and which this ADR
must act on rather than repeat.** `default_timeout: 90` on the `sub_agent` binding **cannot bind
today**, and would stay decorative under D1 alone. `expansion_controller.py:629` sets
`timeout_seconds=settings.worker_timeout_seconds` (default 60) on every `SubAgentSpec`,
`sub_agent.py:500` passes it as `timeout_s`, and an explicit `timeout_s` wins over the definition's
`default_timeout` at `litellm_client.py:1494`. So the worker's real timeout is a setting, not the
role.

D2 says the role owns its budget. That is only true if the role is the **single** home for it, so
this ADR removes `timeout_seconds=settings.worker_timeout_seconds` from the sub-agent dispatch path
and lets the binding's `default_timeout` bind. `worker_hard_deadline_seconds` is a different,
outer bound and stays.

`inherit` reproduces `defaults_by_primary`'s eight rows with zero rows. Seven of those eight were
self-pairs, and the one non-self pair was the local thinking/instruct split this decision deletes.

**Why not simply correct the instruct entry:** the class recurs. Every future local model needs its
own instruct twin under the current shape, and every twin re-creates all six symptoms in the table
above.

### D2 — Three field classes. Role budget survives a model selection.

`resolve_role_target` drops every binding override when the resolved key differs from the binding's
own deployment. That rule is documented and deliberate, and it is wrong: it drops role policy
exactly when a user picks a different model, which is the only case a selection exists for.

Replace it with a rule that sorts a field by **what kind of fact it is**, not by whether the key
matched:

| Class | Fields | Rule | Why |
|---|---|---|---|
| **Budget** | `default_timeout`, `max_tokens` | **Always** applies. The role owns it. | A worker's 90 s and 2048 tokens describe the *job*, not the model. They are valid on any model. |
| **Mode** | the thinking lever and everything else the mode names | The binding names a mode. The model's dialect defines it. If the selected model declares no mode of that name, its `default_mode` applies. | "Non-thinking worker" is a role intent; each dialect spells it differently (D3). |
| **Sampler** | `temperature`, `top_p`, `top_k`, `min_p`, `presence_penalty`, `frequency_penalty`, `repeat_penalty`, `seed` | Lives **only** inside a mode on the model. Never on a binding. Never carried across a redirect. | A temperature tuned for Qwen is not valid on Sonnet 5, which returns 400 for any `temperature` at all (FRE-1430 F1). The accepted sampler set differs per dialect, so no binding field can be valid across models. |

`RoleBinding` therefore **loses** `temperature`, `disable_thinking` and `reasoning_effort`, and
**gains** `mode` and `priority`. Its final shape is six fields expressing three ideas:

```python
deployment: str        # a catalog key, or "inherit"
open: bool             # user-selectable
mode: str | None       # names a mode on whichever model resolves
max_tokens: int | None     # budget
default_timeout: int | None # budget
priority: InferencePriority # D7
```

Removing three fields is safe today because no binding sets any of them.

**Scope, written down rather than assumed.** D2 governs the factory path — the primary's planner and
synthesis calls, and any open role resolved through `get_llm_client`. Roles that set their own budget
at the call site bypass it: `artifact_builder` (`artifact_tools.py:1576–1608`), `compressor`
(`context_compressor.py:235–261`), and the sub-agent path corrected under D1. `vision`
(`executor.py:5745–5761`) sets **no** budget at all and takes the definition's. That is acceptable,
and it is now recorded rather than assumed.

**The cloud branch must read the effective definition, and the read must be gated by dialect.**
Today the cloud branch sends `temperature` and `timeout` only when the caller passes them
(`litellm_client.py:929`, `:965`), against the local branch's `model_def` fallback at `:1494`.

The FRE-1430 study recorded three temperature callers and concluded that none reaches Sonnet 5, so
Sonnet's rejection of every sampler had never been hit. **That conclusion is wrong, and the fourth
caller is a live latent defect.** `captains_log/reflection.py:543` passes `temperature=0.3` through
`get_llm_client_for_key(_captains_log_role, ...)`, and `captains_log` binds `claude_sonnet`
(`model_roles.yaml:54`), whose id is `claude-sonnet-5`. FRE-1430 F5 measured that litellm **raises**
for `anthropic/claude-sonnet-5` with any temperature, and keeps raising with
`allowed_openai_params`. That path is the manual fallback beneath reflection's DSPy path, which is
why it has not been noticed.

Two obligations follow, and the second is the one that makes D2 safe:

1. The reflection fallback is repaired as part of this chain, not left as a trap that fires the
   first time DSPy's path errors.
2. **The dialect gate applies to call-site overrides, not only to the definition read.** A caller
   passing a sampler the dialect does not accept is dropped with a log at the client, exactly as an
   undeliverable thinking value is. Gating only the definition read would fix the path D2 opens and
   leave the four existing call sites (`reflection.py:543`, `entity_extraction.py:1170`,
   `context_compressor.py:259`, `skills.py:478`) able to send a rejected field.

### D3 — A dialect is declared, not inferred. It is the guard's oracle too.

#### D3a — The shape

A **dialect** is a named wire vocabulary with two parts: the thinking lever, and the accepted
sampling set. It is declared on the provider and overridable on a model that disagrees with its
provider. Five dialects cover every declared model today:

| Dialect | Models | Thinking lever | Accepted samplers | Notes from measurement |
|---|---|---|---|---|
| `llamacpp_qwen` | `qwen3.8-flash-next`, `qwen3.6-35b-thinking` | `chat_template_kwargs.enable_thinking: bool` | temperature, top_p, top_k, min_p, presence_penalty, frequency_penalty, `repeat_penalty`, seed | `reasoning_effort` validates and does nothing (F16). `repetition_penalty` is inert — the wire name is `repeat_penalty` (F2). `thinking_budget` is inert (F2, FRE-1423) |
| `ovh_qwen` | `qwen3.8-27b-ovh` | `reasoning_effort` ∈ `{none, low, medium}` | temperature, top_p, presence_penalty, frequency_penalty, seed | `high` returns 400 and `xhigh` returns 422; `xhigh` is the provider default and is unrequestable. Requires `allowed_openai_params` through litellm (F3, F9) |
| `openai_gpt5` | `gpt-5.4-mini` | `reasoning_effort` ∈ `{none, low, medium, high, xhigh}` | temperature and top_p **only at effort `none`**, frequency_penalty, seed | `max_tokens` rejected — litellm maps it to `max_completion_tokens`. `stop` rejected. `presence_penalty` accepted and inert (F1, F5) |
| `anthropic_adaptive` | `claude_sonnet` | `thinking: {type: adaptive}` + `output_config.effort` | **none** | `temperature`, `top_p` and `top_k` all rejected as deprecated (F1, F6) |
| `anthropic_budget` | `claude_haiku` | `thinking: {type: enabled, budget_tokens}` | temperature XOR top_p, top_k | No adaptive thinking and no effort parameter. `temperature` and `top_p` are mutually exclusive (F6) |

A model declares its modes in its own dialect's vocabulary, and the loader validates each mode
against it:

```yaml
qwen3.8-flash-next:
  provider: slm_local
  # dialect inherited from the provider: llamacpp_qwen
  default_mode: thinking
  modes:
    thinking: { enable_thinking: true,  temperature: 1.0, top_p: 0.95, presence_penalty: 0.0 }
    worker:   { enable_thinking: false, temperature: 0.7, top_p: 0.8,  presence_penalty: 1.5 }

qwen3.8-27b-ovh:
  provider: ovhcloud
  default_mode: thinking
  modes:
    thinking: { reasoning_effort: medium }
    worker:   { reasoning_effort: none }

claude_sonnet:
  provider: anthropic
  default_mode: thinking
  modes:
    thinking: { effort: high }
    worker:   { effort: low }
```

**Modes are the only home for samplers and thinking.** The top-level sampler and thinking fields
leave `ModelDefinition`. Every `kind: llm` entry declares `modes:` with at least one mode and a
`default_mode` naming it. Eight entries migrate; the migration is mechanical, because each entry's
current top-level values become its default mode's body.

This is what makes D1 safe. The instruct entry's preset — `temperature 0.7`, `top_p 0.8`,
`presence_penalty 1.5` — has somewhere to live. Without D3, deleting that entry would silently move
the sub-agent onto the thinking preset's `1.0 / 0.95 / 0.0` with thinking switched off: an unmeasured
behaviour change on the exact path D1 exists to fix. `RoleBinding` does not gain `top_p` or
`presence_penalty`, because the accepted sampler set is a dialect fact and a binding cannot hold one
that is valid across models.

**The client builds its parameter block from the dialect, not from placement.**
`_local_extra_body` becomes `_dialect_params`. The two dispatch branches keep their transport
differences — streaming, timeouts, egress — and lose their parameter differences.

**The migration surface is wider than the catalog.** Removing the top-level sampler and thinking
fields breaks every reader of them, and four sit outside the resolver and the client:

| Reader | Reads | Must become |
|---|---|---|
| `llm_client/factory.py:107`, `:122` | `model_def.reasoning_effort` | the resolved mode's thinking value |
| `llm_client/dspy_adapter.py:138`, `:166`, `:174` | `reasoning_effort`, forwarded independently | the resolved mode, through the same helper as the client |
| `second_brain/entity_extraction.py:1100–1104`, `:1170` | `reasoning_effort` and `temperature`, passed explicitly | the resolved mode; the explicit sampler override falls under the D2 dialect gate |
| `captains_log/reflection.py:543` | passes `temperature` explicitly | see D2 — repaired, then gated |

A reader missed here is a silent behaviour change, not a load error, because the field simply
becomes absent.

#### D3b — The declared dialect is the reasoning oracle. litellm's map is consulted only where litellm is the wire.

`config_guard.check_reasoning_declaration` asks `provider_reasoning_support`, which asks litellm.
For OVH that oracle is wrong at the provider level and cannot become right (F8). Replace the rule:

- **Pass-through providers** (OVH, and any OpenAI-compatible endpoint): the declared dialect is the
  authority. Forward `reasoning_effort` with `allowed_openai_params=["reasoning_effort"]` when the
  dialect declares the value. A bad value is rejected loudly by the provider, which is the behaviour
  we want and already get (F3).
- **SDK-mapped providers** (Anthropic): litellm's transformation *is* the wire, so its map is the
  source of the mapping. Keep consulting it there.
- **Fallback:** a dialect that declares the value and a litellm record that is missing → forward. No
  dialect → today's omit-and-log.

`drop_params` stays off. That decision was correct: it is why the OVH refusal surfaced instead of
vanishing.

Two consequences fall out. The `Literal["none","low","medium","high","xhigh"]` at `models.py:297`
becomes per-dialect, because it currently admits two values OVH rejects. And the FRE-1007 guard,
which today checks only the deployment each binding names, must walk **every selectable** `kind: llm`
entry — the picker offers eight models and the guard covers two of them (FRE-1421 F15).

### D4 — Delete the dead layer. Merge the two role tables.

- Remove `defaults_by_primary` and its schema field. Zero runtime readers, confirmed by grep and
  then by a live turn. Four tests pin the shape
  (`tests/personal_agent/config/test_sub_agent_defaults_by_primary.py`); delete them, because they
  enforce a shape nothing runs.
- Fold the `roles:` matrix into `bindings:`, so one resolver reads one table. Two role tables live
  in one file today, read by different resolvers (`resolve_role_model_key` for eight background
  sites, `resolve_role_target` for nine), and some roles resolve through both.
- Cancel **FRE-966** (fail-closed guard: primary must define its default sub) and **FRE-967**
  (resolver: `sub_agent = f(selected primary)`). `inherit` supersedes both. Re-scope **FRE-968**
  (Config UI over the per-primary sub map): the map it edits stops existing.

### D5 — Reconcile the catalog against the served model list at boot. Report, never hard-fail.

**The premise the FRE-1421 study recorded is no longer true, and the correction narrows this
decision.** That study grepped for the literal string `v1/models` and found nothing. The URL is
composed, not literal: `provider_health.py:127` builds `f"{base_url.rstrip('/')}/models"` where
`base_url` already carries `/v1`. `fetch_served_model_ids` reads that endpoint today, and
`check_local_served_ids` fans it across local providers. That reader shipped under FRE-1415 at
08:26 on 2026-09-06, roughly three hours after the study was written — the premise was true when
written and false by the time this ADR was.

So D5 is not "add a reader". It is two smaller changes:

1. **Call the existing probe at startup.** It runs today only from the two picker endpoints
   (`gateway/session_api.py:657`, `:731`), so drift is found when a user opens the model list, or
   when a turn fails.
2. **Widen the comparison beyond `id`.** Compare `context_length` and `quantization` too. Both
   have already drifted: `quantization` `4bit` against a served `UD-IQ4_XS` (FRE-1421 F1), and
   `context_length` 262144 against a served 131072 (FRE-1430 F17, corrected by FRE-1427).

**A mismatch is a `config_guard` finding with the served value in the message. It never fails the
boot.** The SLM server runs on the owner's Mac and is not always awake. Booting the VPS gateway must
not depend on a laptop being on. The availability gate that *does* need to fail closed already
exists and works: `role_candidates` fails a local deployment closed when its id is not in the served
set (FRE-1415).

### D6 — A worker runs at its dialect's own cheap mode. "Inherit at low effort" is wrong on three of five dialects.

The study measured the effort-versus-cost curve on every provider. `low` is not a cheap setting; on
most dialects it is a thinking setting.

| Dialect | Worker mode | Measured |
|---|---|---|
| `llamacpp_qwen` | `enable_thinking: false` | Summary prompt 420 → 112 tokens, 11.3 s → 5.1 s (F16) |
| `ovh_qwen` | `reasoning_effort: none` | 823 tokens / USD 0.0035 against `low` 3242 / USD 0.0116 — a quarter of the cost (F13) |
| `openai_gpt5` | `reasoning_effort: none` | 452 tokens / USD 0.0028, 0 reasoning tokens; `low` 870 (F13) |
| `anthropic_adaptive` | `effort: low` | USD 0.0108 — the **cheapest** Sonnet setting measured, below thinking-off (0.0137), 10/10 correct. Adaptive thinking at `low` emits no thinking block and answers tersely (F13) |
| `anthropic_budget` | thinking disabled | Never litellm's `low`, which is a 1024-token budget costing 4.4x thinking-off (F5, F13) |

Sonnet 5 is the reason this decision cannot be a single rule: `low` is right there and wrong
everywhere else. A worker mode must name the dialect's own cheap setting.

**One quality signal, stated at its scope.** On OVH, `none` missed the ordering puzzle once in five
(22/25 overall against 25/25 at every other level). That is one prompt shape at n=5. A worker whose
task needs reasoning is the planner's decision, not the default's.

**What this saves.** Under D1 without D6, the owner's three sub-agents would have moved from free
local calls to OVH calls at the provider default — `xhigh`, billed as completion at USD 3.19 per
Mtoken, 2.2x `medium` in the measured totals.

### D7 — Concurrency stays on the provider and the model. The role carries a priority.

`max_concurrency` does **not** move to the role. The controller keys one semaphore per catalog key
(`concurrency.py:210–246`), so one catalog entry means one limit, and a fixed count on a role cannot
be right across providers — the local box serves 3 slots, a cloud provider 25 or 50. A rank is valid
everywhere; a count is not.

The collapsed local entry carries `max_concurrency: 3`, the truth of the box. The role carries
`InferencePriority`: `primary` `USER_FACING`, `sub_agent` `ELEVATED`, background roles `BACKGROUND`.
Sub-agents pass no priority today, so they default to `USER_FACING` and rank equal to the primary.

**The priority is read from the binding and threaded to the slot, not hard-coded at the dispatch
site.** A constant written into `sub_agent.py` would satisfy the sentence above while leaving
`RoleBinding.priority` decorative — a second home for a fact the ADR says the role owns, which is
the disease this document treats. The resolver returns it alongside the key and the definition, and
`request_slot` receives it.

**The prediction, stated plainly rather than reassured away.** Today the thinking entry allows 1
in flight and the instruct entry 3, on separate semaphores. Two primary turns in two different
sessions therefore **serialize** today. After the collapse they do not: one entry at 3 makes two or
three concurrent primaries reachable, against a **131072**-token pooled KV window (FRE-1427,
deployed 2026-09-06 11:41; the server and the catalog now agree at that value). Turns in different
sessions run as independent tasks (`service/app.py:2561–2573`), and FRE-1380 serialises workers
*within* a turn only.

`InferencePriority` orders the wait queue. It does not cap. So this is a real widening, and the
FRE-1421 study understated it: the worst case it named — one primary plus two workers — is indeed
today's state, but two or three concurrent primaries is not. AC-5 measures it. No ceiling is stated
in advance, because a number chosen without the measurement would be the same reasoning-instead-of-
running this ADR exists to end.

---

## Alternatives Considered

### Option 1: Correct the instruct entry in place

**Description:** Keep two catalog entries. Fix `quantization`, move `default_timeout` and
`max_tokens` onto the `sub_agent` binding, leave the resolver alone.

**Pros:**
- Smallest possible diff; no resolver change, no schema change.
- No migration of eight catalog entries.

**Cons:**
- Measured to change nothing. Moving `default_timeout` onto the binding leaves the incident path
  identical, because the resolver drops binding overrides on a redirect (FRE-1421 F4).
- The class recurs: every future local model needs its own instruct twin.
- Every twin re-creates the drift, the wire-id ambiguity, the pairing table and the picker leak.

**Why Rejected:** This is FRE-1420's option 1, and it was simulated at the deployed revision. It
passes review and does not touch the defect. Fixing the instance leaves the mechanism.

### Option 2: Move `max_concurrency` onto the role

**Description:** Treat concurrency as role policy alongside budget — `primary: 1`, `sub_agent: 3`.

**Pros:**
- Preserves today's "one primary at a time" exactly.
- Reads naturally next to the other per-use fields.

**Cons:**
- The controller keys one semaphore per catalog key, so one entry cannot hold two limits.
- A fixed count cannot be right across providers: 3 slots locally, 25 on OVH, 10 on Anthropic. The
  same role binding would be correct on one model and wrong on the next.

**Why Rejected:** Caught by adversarial review of the study's own first draft. Collapsing the pair
under a role-count would either serialise workers behind the primary across sessions, or let
primaries run three-wide, depending on which number was chosen. A role's share of a provider's
capacity is a priority, and `InferencePriority` already expresses it (D7).

### Option 3: Derive the dialect from `placement`

**Description:** Keep one axis. Local models speak `chat_template_kwargs`; cloud models speak
`reasoning_effort`.

**Pros:**
- No new declaration; the field already exists and is already correct for four of five models.

**Cons:**
- Wrong for OVH, which is cloud-placed and refuses `chat_template_kwargs` while accepting
  `reasoning_effort` (FRE-1430 F1).
- Wrong within one vendor: Sonnet 5 needs `thinking.adaptive` + `output_config.effort` and rejects
  `temperature`; Haiku 4.5 needs `thinking.enabled` + `budget_tokens` and accepts `temperature`
  (F6).
- Placement also fails to predict the *sampling* set, which differs on every dialect.

**Why Rejected:** Placement decides where a model runs. That is a different fact from which
vocabulary it speaks, and OVH is the counterexample that separates them. Deriving one from the other
is how the current shape produces silent 400s.

### Option 4: Keep litellm's capability map as the reasoning oracle

**Description:** Leave `check_reasoning_declaration` asking `provider_reasoning_support`, and wait
for litellm to add OVH's record.

**Pros:**
- No change; one oracle for every provider.
- litellm is right for the SDK-mapped providers, where its transformation is the wire.

**Cons:**
- `get_supported_openai_params("ovhcloud")` returns `OpenAIGPTConfig`'s base list for every model,
  and that list has no `reasoning_effort`. The subclass adds nothing.
- The guard returns `None` today because no cost-map record exists, and would return `False` the day
  one appears. Both outcomes omit the value.
- The failure branch has never fired in production: 0 occurrences of
  `reasoning_declaration_undeliverable` over thirty days and all time, against 457
  `model_call_started` from the same function (F15).

**Why Rejected:** The oracle cannot become right for this provider by any change in litellm's data.
Waiting for the record makes the answer worse, not better. Where litellm is not the wire, the
declaration is the only honest authority.

### Option 5: Add `top_p` and `presence_penalty` to `RoleBinding`

**Description:** Skip modes. Give the binding the two sampler fields it lacks, so the instruct
preset can move onto `sub_agent` directly.

**Pros:**
- Much smaller than D3: two fields, no dialect, no catalog migration.
- Solves the immediate stranding problem D1 creates.

**Cons:**
- Re-creates D2's bug pointing the other way. A binding sampler would either be dropped on a
  redirect — the behaviour D2 removes — or carried onto a model that rejects it.
- The accepted sampler set is per-dialect: Sonnet 5 rejects `top_p` outright, OpenAI rejects it above
  effort `none`, Haiku refuses it next to `temperature`. No single binding value is valid across
  models.
- Six sampler fields would eventually follow the first two, and the binding becomes the deployment
  entry it replaced.

**Why Rejected:** A sampler is a property of a model at a setting. The model card already groups
them into presets, and the role only ever needs to name one. Naming is the smaller interface.

### Option 6: Defer D3 and D6 to the FRE-1430 study

**Description:** Ship D1, D2, D4 and D5 now. Write D3 and D6 as open decisions naming the study as
the input.

**Pros:**
- Was the standing instruction until 2026-09-06 11:58, and honest at the time it was given: the
  cloud dialect was genuinely unsettled on one sample per level.

**Cons:**
- D1 cannot ship without D3. Deleting the instruct entry with no `modes:` strands its sampler preset
  and silently moves the sub-agent onto the thinking preset with thinking off.
- The study reported at 11:57 on 2026-09-06 (merge `c3676ee1`), with 948 provider calls behind it.
  Both questions are answered.

**Why Rejected:** Withdrawn by the owner after the stranding defect was identified. An ADR that
records "not yet decided, here is what would decide it" is honest only while the answer is genuinely
absent. It had arrived.

---

## Consequences

### Positive Consequences

- **The owner's model and the code's model become the same.** Two nouns, one table, one resolver.
  "Deployment" and "profile" stop appearing in the picker.
- **A model selection reaches the whole turn.** Choosing a cloud primary gives cloud sub-agents;
  choosing a local primary gives local ones. Today the primary selection reaches nothing.
- **Role policy holds.** A worker's 90 s and 2048 tokens travel with the *role*, on any model.
- **Drift becomes structurally impossible in three places at once.** One entry per served artifact
  means `quantization` cannot contradict itself, the wire id becomes unambiguous for local models,
  and no pairing table can go stale.
- **The wire stops being guessed.** Five dialects, each measured against the provider, replace one
  inference from `placement` and one wrong capability map.
- **Two inert declarations leave the vocabulary.** `thinking_budget_tokens` and the local
  `repetition_penalty` spelling both read as configured today and do nothing.
- **The FRE-1007 guard covers all eight selectable models** instead of the two a binding names.

### Negative Consequences

- **The catalog migration touches eight entries at once,** and a mode body written in the wrong
  dialect is a new class of config error. The validator is what keeps it a load-time failure rather
  than a call-time one.
- **`ModelDefinition` loses its top-level sampler and thinking fields.** Every reader of those
  fields moves to a mode lookup, and the golden config snapshot rebaselines.
- **Concurrent primaries become reachable** where they serialize today (D7). This is the one change
  that can degrade a live turn rather than improve it.
- **`allowed_openai_params` is a per-call kwarg,** not a map entry, so the pass-through forwarding
  rule lives in the client and must be kept in step with the dialect declarations.
- **Four tests are deleted rather than migrated.** They pin a shape that never ran.
- **The chain is long.** Ten tickets, and the catalog collapse cannot land before the schema and the
  resolver.

### Risks and Mitigations

| Risk | Severity | Mitigation |
|---|---|---|
| Concurrent primaries wedge the local server. Four concurrent requests are known to wedge it; three at a 131072 pooled window is untested | **High** | AC-5 measures observed in-flight counts across the collapse before the chain closes. `InferencePriority` orders the queue so the primary wakes first. If measurement shows contention, the model entry's own `max_concurrency` is one number to lower — no schema change |
| The cloud branch starts reading `model_def` samplers and sends `temperature` to Sonnet 5, which returns 400 | **High** | D2's cloud clause is gated by the dialect's accepted sampler set, not by placement. AC-9 asserts a Sonnet primary turn completes, which it cannot if any sampler reaches it |
| A mode body is written in the wrong dialect and fails at call time | Medium | The loader validates every mode against its model's dialect at config load, the same way `kind` compatibility is validated today. AC-11 seeds two invalid bodies, because a validator never shown to reject anything is not a validator |
| A reader of a removed top-level field is missed, and the parameter silently stops being sent | Medium | Four are named in D3a. Removing the field from the model rather than defaulting it makes each remaining reader a type error at `mypy` time, not a runtime absence |
| The `captains_log` reflection fallback fires and raises, because it sends a temperature Sonnet 5 rejects | Medium | Repaired in this chain rather than recorded. AC-9 asserts that path specifically, since the ordinary primary path completes today and hides it |
| `inherit` is honoured in one path and not another | Medium | AC-4 exercises all four paths. A sentinel honoured inconsistently is worse than no sentinel |
| The worker's mode is missing on a newly added model, and the sub-agent silently runs at the model's default (thinking on, billed) | Medium | D2's fallback is explicit and logged: no mode of that name → `default_mode`. The FRE-1007 guard extension covers every selectable entry, so an undeclared model is a load-time finding |
| The boot reconciliation is noisy when the Mac is asleep, and the finding is ignored | Low | The probe already returns an empty set on any failure without raising. An unreachable host is not a drift finding — only a *served* value that disagrees is |
| OVH's `none` costs a quality point on reasoning-heavy worker tasks (puzzle 4/5) | Low | One shape at n=5, recorded at that scope. A worker needing reasoning is the planner's decision. Revisit if worker quality regresses |

---

## Implementation Notes

**Files affected**

| Area | Files |
|---|---|
| Schema | `llm_client/models.py` — `ProviderDefinition` (dialect), `ModelDefinition` (modes, default_mode; drop top-level samplers/thinking), `RoleBinding` (drop 3 fields, add `mode`/`priority`), the binding validator, the per-dialect effort `Literal` |
| Resolver | `config/model_loader.py` — `resolve_role_target` (three classes), `resolve_selected_deployment` (`inherit`), `resolve_role_model_key` (folded into bindings), `role_candidates` |
| Factory | `llm_client/factory.py:169–188` — pass the primary's selection for an `inherit` binding |
| Client | `llm_client/litellm_client.py` — `_local_extra_body` → `_dialect_params`; cloud branch reads the effective definition, gated by dialect; the same gate applied to call-site sampler overrides; `allowed_openai_params` for pass-through providers |
| Other readers of the removed top-level fields | `llm_client/factory.py:107`, `:122` · `llm_client/dspy_adapter.py:138`, `:166`, `:174` · `second_brain/entity_extraction.py:1100–1104`, `:1170` · `captains_log/reflection.py:543` (also repaired — it sends a temperature Sonnet 5 rejects) |
| Guard | `config/config_guard.py` — reasoning oracle by dialect; resolve `inherit` at `:1147`; walk every selectable `kind: llm` entry; boot reconciliation |
| Health | `llm_client/provider_health.py:135–155` — return `context_length` and `quantization`, which the probe currently discards, and widen the comparison beyond `id` |
| Concurrency | `orchestrator/sub_agent.py`, `orchestrator/expansion_controller.py:629` — thread the binding's priority to `request_slot`; drop `timeout_seconds=settings.worker_timeout_seconds` so the binding's `default_timeout` binds |
| Other binding readers | `orchestrator/constraint_options.py:269` · `config/resolve.py:59–62` (transitive, after D4) |
| Catalog | `config/models.yaml` (8 entries migrate, 2 delete), `config/model_roles.yaml` (two tables merge) |
| Tests | delete `test_sub_agent_defaults_by_primary.py`; rebaseline the golden config snapshot |

**Migration order.** Schema and dialect declarations first, then the resolver and the client, then
the catalog collapse. The collapse is the only step that changes live behaviour, and it must land
after every path that reads a binding can resolve `inherit`.

**Testing strategy.** Unit tests for the resolver's three classes and the `inherit` sentinel in all
four paths. A call-site assertion for D2 — the value passed to the client, never the value in
config. A live-turn measurement for AC-1, AC-5, AC-8, AC-9 and AC-10, because five of these criteria
are about what a provider does, and no test against config can answer that.

---

## Verification / Acceptance Criteria

**How will we know this decision actually delivered — not just merged?**

- **AC-1 — The FRE-1420 incident turn completes.** Select the non-thinking mode as primary and run
  the incident's HYBRID query. · **Check:** the turn returns an answer to the user; the trace carries
  **no** `model_call_error` with `LLMTimeout` (a timed-out call emits `model_call_error`, not
  `model_call_completed` — `litellm_client.py:1618`); and the synthesis call's observed completion
  tokens exceed 2048, or the answer is complete on inspection. · *Fails if* it ends in `LLMTimeout`,
  **or** if the answer truncates at 2048. A fix addressing only the timeout passes half of this and
  is not a pass.

- **AC-2 — A selected model cannot lower a role's budget, demonstrated at the call site, for both
  budget-carrying roles.** · **Check:** assert the `timeout` and `max_tokens` values actually passed
  into the client — for `primary` with a session selection that **differs** from the binding's
  deployment, and for `sub_agent` under an `inherit` binding. · *Fails if* the assertion reads
  configuration. Config proves a path exists; it never proves the path runs. *Also fails if* only
  `primary` is covered: the sub-agent's timeout comes from a setting today (D1's correction), so a
  `primary`-only assertion would pass while the worker's budget still ignores its role.

- **AC-3 — Seeded negative: the role still binds when its budget is deliberately low, on a redirect.**
  · **Check:** set `primary.max_tokens` and `primary.default_timeout` to small values, select a
  model **other than** the binding's deployment, run a turn, and observe both bind. · *Fails if* the
  selected model equals the binding's deployment — the current early-return satisfies that case and
  proves nothing. *Also fails if* only `max_tokens` is asserted; the incident was a timeout.

- **AC-4 — `inherit` resolves in every path, not one.** · **Check:** exercise all six named in D1 —
  `resolve_role_target`, `resolve_selected_deployment`, the catalog validator, the factory,
  `config_guard.check_reasoning_declaration`, and `constraint_options` — each with an `inherit`
  binding and a non-default primary selection. The guard arm asserts the sub-agent binding is
  **still covered**, not merely that boot succeeds. · *Fails if* any path returns the literal string,
  falls back to a static default, or skips the binding. A guard that silently stops checking a role
  passes a naive test by staying quiet.

- **AC-5 — Concurrency across the collapse is measured, not reasoned about.** · **Check:**
  `ConcurrencyController.get_status()` sampled during two deliberately overlapped sessions, reading
  `active` per semaphore (`concurrency.py:369–380`); before and after the collapse. Not
  `inference_slot_acquired`, which fires only after a 100 ms wait and carries no active count
  (`concurrency.py:313–315`). · *Fails if* adjudicated from the configured `limit` rather than
  observed `active`. The before-figure must show the local model semaphore reaching `active: 1` and
  no higher, or the instrument is not measuring what D7 predicts.

- **AC-6 — The picker offers one candidate per served artifact.** · **Check:** for `primary`, no two
  candidates in the model-list response resolve to the same wire `id`. · *Fails if* two entries
  share an id under any naming. There is no runtime "role profile" predicate to test —
  `role_candidates` offers every kind-compatible model — so "no profile appears" is unfalsifiable
  and a renamed duplicate would pass it. Shared-id collision is the property that actually broke
  (FRE-1421 F12), and it is decidable.

- **AC-7 — Boot reconciliation reports every seeded drift dimension, and never blocks a boot.**
  Three arms, all required. · **Check (a):** seed a `quantization` that disagrees with the served
  value; the `config_guard` finding names the **served** value. **(b):** seed a `context_length`
  that disagrees; same. **(c):** with the SLM host unreachable, the service starts and serves a
  cloud-primary turn. · *Fails if* only one dimension is seeded — D5 requires both, and the probe
  today discards every field except `id` (`provider_health.py:135–155`), so a one-dimension test can
  pass with the widening half-built. *Also fails if* an unreachable host prevents startup.

- **AC-8 — The declared effort reaches the wire on a pass-through provider.** Two arms. ·
  **Check (a):** capture the keyword arguments dispatched for an OVH worker call and assert
  `reasoning_effort` is present with the declared value and `allowed_openai_params` carries it.
  **(b):** the same call's billed `completion_tokens` and absent `message.reasoning` match the
  measured `none` row, not the provider default. · *Fails if* only (b) is checked: billed tokens are
  stochastic and an outcome can be reproduced for other reasons. *Also fails if* only (a) is
  checked: the client can log a value and omit it (`litellm_client.py:940–953`), which is the
  present defect.

- **AC-9 — No sampler reaches a dialect that rejects it, on any path.** · **Check:** assert the
  parameter block built for a `claude_sonnet` call contains no `temperature`, `top_p` or `top_k` —
  for the primary factory path **and** for the `captains_log` reflection fallback, which passes
  `temperature=0.3` explicitly today (`reflection.py:543`). · *Fails if* adjudicated from a completed
  Sonnet turn. The ordinary primary path sends no temperature at all
  (`executor.py:6232`), so that turn completes today and would keep completing while the reflection
  path still leaks — the criterion would pass over the live defect it exists to catch.

- **AC-10 — A worker runs at its dialect's cheap mode, on all five dialects.** · **Check:** for one
  primary per dialect, compare the sub-agent call's thinking against the same call at the model's
  `default_mode` — billed reasoning tokens on OVH and OpenAI, thinking-block presence on the two
  Anthropic dialects, `reasoning_content` length locally. · *Fails if* only a local and a cloud
  primary are exercised: that covers two of five and can pass while three are wrong. *Also fails if*
  one effort value is applied across dialects — `low` is correct on Sonnet 5 and wrong on the other
  four.

- **AC-11 — A mode written in the wrong dialect fails at load, not at call time.** · **Check:** seed
  a mode body carrying `enable_thinking` on an `ovh_qwen` model, and a second carrying
  `temperature` on `anthropic_adaptive`; both must be load-time failures naming the field and the
  dialect. Then confirm a model declaring `modes` without a matching `default_mode` also fails. ·
  *Fails if* the invalid body loads and surfaces as a provider 400 on the first turn. A validator
  that only accepts valid input has not been shown to reject anything.

- **AC-12 — Priority is read from the binding and orders the queue under real contention.** ·
  **Check:** with the local model semaphore saturated, a `primary` request queued behind
  `sub_agent` requests acquires first; and changing `sub_agent.priority` in the binding changes that
  outcome. · *Fails if* the priority is hard-coded at the dispatch site — the binding field would
  then be decorative and the second arm cannot move the result. *Also fails if* adjudicated from the
  enum value passed, rather than from acquisition order.

**Where these are adjudicated.** On FRE-1426, this ADR's umbrella ticket, once the implementation
chain has landed and deployed. Not at merge of the ADR, and not by any single implementation ticket,
each of which carries criteria for its own work only.

---

## References

- `docs/research/2026-09-06-fre-1421-model-management-review.md` — the cause; findings F1, F4, F5,
  F6, F9, F12, F14, F15; proposals P1–P5, P8
- `docs/research/2026-09-06-fre-1430-provider-dialect-matrix.md` — the dialect matrix; findings F1,
  F2, F3, F5, F6, F8, F13, F15, F16, F17; proposals P1–P4, P8, P9
- [ADR-0121](ADR-0121-model-catalog-and-selection-layer.md) — the decision this finishes;
  `RoleBinding` and the Layer-3 model
- [ADR-0141](ADR-0141-one-llm-dispatch-path.md) — fixed the client so it can send `enable_thinking`,
  retiring the workaround this ADR removes from the catalog
- [ADR-0142](ADR-0142-capability-is-not-a-property-of-register.md) — capability is not a property of
  register
- [ADR-0099](ADR-0099-configuration-management-and-validation.md) — the `roles:` matrix, folded by D4
- [ADR-0132](ADR-0132-outbound-authenticated-egress.md) — the Caddy egress the probes reach
- FRE-1426 (this ADR's umbrella) · FRE-1421 (the model-management study) · FRE-1430 (the dialect
  study) · FRE-1420 (the incident, re-scoped onto D2) · FRE-1427 (the 131072 window)
- Superseded by this ADR: FRE-966, FRE-967. Re-scoped: FRE-968, FRE-1420. Absorbed: FRE-1423
  (`thinking_budget_tokens` leaves the local dialect under D3a)
- Related findings filed at Backlog: FRE-1422 (`route_traces.thinking_enabled` hard-coded `None`),
  FRE-1424 (a past turn's selected model cannot be reconstructed), FRE-1438 (`repetition_penalty` is
  inert on the local wire)

---

## Status Updates

### 2026-09-06 — Proposed
**Changed By:** adr session (FRE-1426)
**Reason:** Authored from the FRE-1421 and FRE-1430 studies, and from the owner's rulings of
2026-09-06 10:40 and 11:58. The second ruling withdrew the instruction to defer D3 and D6, after the
adr seat identified that deleting the instruct entry with D3 deferred would strand its sampler
preset — an unmeasured behaviour change on the exact path D1 exists to fix.
