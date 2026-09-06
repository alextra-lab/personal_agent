# Model management review — one model, two nouns, and a resolver rule that voids the fix on the table

**FRE-1421** · explore session · 2026-09-06 · read-only study against the live system

**Deployed revision under measurement:** `main@33ba59c4`. Confirmed by container file hash for
every source file cited below, not by board state — see [Method](#method-appendix), M1.

**Commission, verbatim (owner, 2026-09-06 05:00):** *"Full stop. I need an explore session to
analyze our model management. I think we have created a terribly difficult to manage system."*

> **Adversarial review, 2026-09-06 06:36 (Codex, `gpt-5.6-sol`, effort `xhigh`, read-only, at the
> owner's request).** Ten numbered questions. Seven findings held. Three failed and are corrected
> in place: **P1** did not carry per-role concurrency (one catalog key means one semaphore), **P6**
> described the reasoning guard backwards (it rejects `disable_thinking: false`), and **P2** did not
> state its scope (cloud dispatch ignores `default_timeout` and `temperature` from the definition,
> and three roles bypass the resolver). Six wording overstatements in F4, F6, F8, F9 and F14 are
> tightened, and one new gap is added to F15. Every correction was verified against the code before
> it was applied — see [Method](#method-appendix), M7.

---

## Verdict, up front

Yes. The system is difficult to manage. The reason is not the design. The reason is that the design
was never finished, and the unfinished part now carries load.

ADR-0121 decided in July that a role binding carries per-use policy and a catalog entry carries only
what the model is. The schema for that exists (`RoleBinding` has `disable_thinking`, `max_tokens`,
`temperature`, `default_timeout`). The catalog was never migrated onto it. Instead the catalog holds
**two entries for one served model**, one per role, and the picker offers both as models. That
second entry is the remainder of a workaround: before ADR-0141 the client did not send
`enable_thinking`, so the only way to get a no-thinking worker was a second deployment with the flag
baked in. ADR-0141 fixed the client on 2026-09-03. The workaround is still in the catalog.

Everything in the commission follows from that one un-migrated fact:

- The instruct entry needs its own budget, so role policy sits on a deployment (F5).
- Two entries drift, so `quantization` contradicts itself (F1).
- Two entries share a wire id, so no record can tell them apart (F12).
- Two entries need a pairing table, so `defaults_by_primary` exists — and nothing reads it (F6).

And one thing the commission did not know: **the fix on the table for FRE-1420 does nothing.** The
deployed resolver drops every binding override when the session's selected key differs from the
binding's own deployment (`model_loader.py:324`, by documented design). Moving `default_timeout` onto the primary binding
leaves the incident path unchanged. Measured in F4.

The owner's own model of the system is the right one, and it is simpler than the code's: **two
nouns.** A *model* is what you pick — a name, where it runs, what it can do, and its card's modes.
A *role* is who uses it and how — which model, which mode, what budget. "Deployment" becomes a
property of a model, not a word the owner meets. The proposals below are that model, plus the one
resolver rule that makes role policy hold.

The recommendation is one ADR (P1–P4), three small tickets (P5–P7, two already filed at Backlog),
and one design seed for a later ADR (P9). No general model-management framework.

---

## Findings

Every finding states its verdict, the query as run, and the output as returned. Negative findings
carry the admissibility arms on the finding itself.

### F1 — The server serves one model. The catalog names it twice, and the two descriptions disagree.

**Verdict:** POSITIVE.

**Query (live server, via the Caddy egress on the VPS host):**
`curl -s http://localhost:8600/v1/models`

**Output:**
```
{"object":"list","data":[{"id":"unsloth/qwen3.8-flash-next","backend":"llamacpp","port":8502,
"model_type":"multimodal","context_length":262144,"quantization":"UD-IQ4_XS",
"supports_function_calling":true}]}
```

**Query (catalog, loaded through the deployed loader):** group `config.models` by `id`.

**Output:** 11 entries, 9 distinct ids. Two ids are shared:
`unsloth/qwen3.6-35-A3B` ← `qwen3.6-35b-thinking`, `qwen3.6-35b-instruct`.
`unsloth/qwen3.8-flash-next` ← `qwen3.8-flash-next`, `qwen3.8-flash-next-instruct`.

The instruct entry declares `quantization: "4bit"`. The server reports `UD-IQ4_XS`. The primary
entry is right. The instruct entry is wrong. Master's 21-field audit in the commission holds: 12
fields identical, 9 different, and 8 of the 9 are role policy.

### F2 — The thinking toggle works on the server, the client sends it, and the default is thinking on.

**Verdict:** POSITIVE.

**Query (live server, three calls, same prompt "Reply with exactly the word PONG", `max_tokens` 200):**

| `chat_template_kwargs` | wall | completion tokens | `reasoning_content` |
|---|---|---|---|
| `{"enable_thinking": false}` | 3.1 s | **3** | absent |
| `{"enable_thinking": true}` | 1.4 s | 47 | 177 chars |
| omitted | 1.4 s | 48 | 202 chars |

**Client side:** `litellm_client.py:219–220` — `if model_def.disable_thinking:
extra_body["chat_template_kwargs"] = {"enable_thinking": False}`. Container hash of
`litellm_client.py` matches the repo (M1).

The owner's design — one served model, thinking toggled per call by the client — is live today. The
catalog comment at `models.yaml:203–211` says the same and dates it to FRE-1365, deployed 2026-09-03.

### F3 — `thinking_budget` is inert on the live server.

**Verdict:** NEGATIVE.

**Query (live server, two calls, prompt "In one sentence: why is the sky blue?", `max_tokens` 600):**

| `thinking_budget` | completion tokens | `reasoning_content` |
|---|---|---|
| 8 | 134 | 412 chars |
| 100000 | 141 | 453 chars |

**Arm 1 — provenance.** The key is sent: `litellm_client.py:221–222`,
`elif model_def.thinking_budget_tokens is not None: extra_body["thinking_budget"] = ...`. The
catalog's `qwen3.8-flash-next` declares `thinking_budget_tokens: 32768`. Container hash matches.
**Arm 2 — liveness.** Same endpoint, same session, `enable_thinking: false` cut the same prompt to
3 tokens (F2). The endpoint honours per-request chat-template arguments. It ignores this key.
**Arm 3 — scope.** This served build (llamacpp, port 8502) on 2026-09-06 06:05 UTC. Whether the
build exposes any per-request reasoning-budget parameter under another name is UNVERIFIED.

Consequence: the primary entry's declaration exists to satisfy `config_guard`'s FRE-1007 rule that a
local deployment must declare `disable_thinking` or `thinking_budget_tokens`. The guard is satisfied
by a value the server discards. ADR-0141 D8 (adaptive thinking budget, owner-designated Critical)
assumes this lever. Filed as **FRE-1423**.

### F4 — FRE-1420's proposed fix, and its one-line mitigation, are vacuous under the deployed resolver.

**Verdict:** POSITIVE.

**Query:** `resolve_role_target("primary", model_key=<selection>, config=cfg)` at the deployed
revision (`model_loader.py` hash matches), first on the live config, then with
`primary.default_timeout = 600` added to the binding.

**Output:**
```
selection=None                          -> key=qwen3.8-flash-next          timeout=600 max_tokens=None
selection='qwen3.8-flash-next-instruct' -> key=qwen3.8-flash-next-instruct timeout=90  max_tokens=2048
[primary.default_timeout=600] selection=None                          -> timeout=600 max_tokens=None
[primary.default_timeout=600] selection='qwen3.8-flash-next-instruct' -> timeout=90  max_tokens=2048
```

The rule, from `model_loader.py:324` docstring: *"Overrides apply only when the resolved key is the
binding's own deployment. When a per-turn selection or an explicit model_key redirects the role
elsewhere, that deployment's own values stand."* And the code: `if ... key != binding.deployment:
return key, definition`.

So FRE-1420 option 1 ("move `default_timeout`, `max_tokens`, `max_concurrency` onto the role
binding") does not touch the incident path. Neither does the "one-line mitigation". Any role-level
budget is dropped exactly when a user picks a model. The fix needs the rule changed (P2), not the
field moved.

The rule is narrower than "any selection": a selection equal to the binding's deployment keeps the
overrides. It is exactly the case where the user picks a *different* model that drops them — which
is the only case a selection exists for.

### F5 — The deployment's `default_timeout: 90` binds in exactly one situation: the incident.

**Verdict:** POSITIVE.

**Query (code at deployed hash):** `sub_agent.py:500` passes `timeout_s=spec.timeout_seconds`.
`expansion_controller.py:629` sets `timeout_seconds=settings.worker_timeout_seconds`.
`litellm_client.py:1494` — `effective_timeout_s = timeout_s if timeout_s is not None else
float(model_def.default_timeout)`.

**Query (live container):** `docker exec cloud-sim-seshat-gateway env | grep -E 'WORKER|PLANNER'`
→ no override. Settings defaults: `worker_timeout_seconds` 60, `worker_hard_deadline_seconds` 85,
`planner_timeout_seconds` 600.

A sub-agent call therefore carries a 60 s read timeout from settings. The instruct entry's 90 is
never read for a worker. Its only reader is a primary call that resolved to the instruct entry —
which is the FRE-1420 turn. The field's entire live effect is the defect.

The planner is the third case. `expansion_controller.py:440–460` wraps the planner call in
`asyncio.wait_for(..., timeout=600)` but passes no `timeout_s` into `respond`, so the planner call
itself runs on the deployment's `default_timeout`. With the instruct entry selected, the planner
call carries 90 s internally under a 600 s outer wrapper. In the incident it took 13 s and passed.

`max_tokens: 2048` is different: `sub_agent.py:498` passes `max_tokens=spec.max_tokens`, which is
`None` since FRE-1379, so the client falls to `self.max_tokens` = the deployment's 2048. ES confirms
every call in the incident trace, primary and sub-agent, logged `max_tokens: 2048` (F12 query).

### F6 — `defaults_by_primary` has zero runtime readers. Four tests pin it. The pairing layer was never switched on.

**Verdict:** NEGATIVE.

**Query:** `grep -rn 'defaults_by_primary' src/personal_agent --include=*.py | grep -v llm_client/models.py`

**Output:** 0 lines. The only two hits in `src/` are the schema field and its docstring
(`llm_client/models.py:142`, `:163`).

**Arm 1 — provenance.** The identifier is real: `config/model_roles.yaml:161–169` carries eight
rows under it, and `RoleBinding.defaults_by_primary` parses them (`models.py:163`). The docstring
at `:146` says *"Substrate only as of step 1 (FRE-965) — nothing resolves through it yet."*
**Arm 2 — liveness.** Same grep shape, identifier varied: `resolve_role_target(` → 9 call sites.
**Arm 3 — scope.** `src/personal_agent` at `33ba59c4`; `model_loader.py`, `factory.py`,
`expansion_controller.py` container hashes match. Outside `src/`,
`tests/personal_agent/config/test_sub_agent_defaults_by_primary.py` reads the map nine times: exact
equality, total key coverage, every value is `kind: llm`, and the Qwen pairing. Those tests enforce
a shape nothing runs. P4 must delete or replace them.

Linear confirms: FRE-965 (step 1, the map) is Done 2026-07-24. FRE-966 (the guard) and FRE-967
(the resolver) are Approved, unstarted, priority None, since 2026-07-24. Seven of the eight rows are
self-pairs. The one non-self pair (`qwen3.6-35b-thinking → qwen3.6-35b-instruct`) is the local
thinking/instruct split — which P1 removes. "Inherit" reproduces the whole map with zero rows.

### F7 — `route_traces.thinking_enabled` is not unpopulated. It is hard-coded `None`.

**Verdict:** NEGATIVE.

**Query (production Postgres):**
`select count(*), count(thinking_enabled), min(created_at), max(created_at) from route_traces`

**Output:** `805 | 0 | 2026-06-07 04:32:46 | 2026-09-06 04:45:19`

**Arm 1 — provenance (1b, a live producer).** The emit site at the deployed revision is
`observability/route_trace/assembler.py:320`: `thinking_enabled=None,` — unconditional. Container
hash of `assembler.py` matches. The enclosing path executes: the sibling fields on the same
constructor call are populated (arm 2).
**Arm 2 — liveness.** Same table, same window, identifier varied:
`count(model_role)` 803, `count(decomposition_strategy)` 688, `count(sub_agent_count)` 805.
**Arm 3 — scope.** Whole table, all rows, 2026-06-07 to 2026-09-06.

The Elasticsearch side is no better: `model_call_started` for the incident trace carries `role`,
`model`, `max_tokens` and nothing about thinking or timeout. The owner's question "was thinking
off?" has no evidence path anywhere. Filed as **FRE-1422**.

### F8 — The application never reads `/v1/models`. Catalog drift is found only by a turn failing.

**Verdict:** NEGATIVE.

**Query:** `grep -rn 'v1/models' src/personal_agent --include=*.py`

**Output:** 0 lines.

**Arm 1 — provenance.** The endpoint is real and reachable from the container's own egress (F1).
One script reads it — `tools/test_slm_server.sh:35–36`, a hand-run diagnostic — and no code under
`src/` does. The catalog comments transcribe it by hand: `models.yaml:274` — *"reported by /v1/models
2026-09-05 (was 8bit)"*.
**Arm 2 — liveness.** Same grep shape, identifier varied: `resolved_slm_health_url` → 4 hits.
`provider_health.py:58` calls a health URL only.
**Arm 3 — scope.** `src/personal_agent` at `33ba59c4`; `provider_health.py` container hash matches.

Every drift found so far — FRE-1317 (both local roles on an unserved id, every turn failing),
FRE-1363 (blocked on a stale id), F1's `4bit` — was found by failure or by a human reading the
endpoint. The data to catch it at boot is one GET away and already reachable.

### F9 — The schema for the owner's design shipped in July. The catalog was never migrated onto it.

**Verdict:** POSITIVE.

**Query:** `RoleBinding` at `llm_client/models.py:123–163`, container hash matches.

**Output:** fields `deployment`, `open`, `max_tokens`, `temperature`, `disable_thinking`,
`reasoning_effort`, `default_timeout`, `defaults_by_primary`. Its docstring, `:126–129`:

> *"Decoding parameters and effort live here, not on the deployment, because they are per-use. This
> is what dissolves the primary/sub_agent duplication: they stop being two 'models' and become two
> bindings of one model at different effort — which is what they always were."*

**Query (live bindings):** `cfg.roles["primary"]` → `{deployment: qwen3.8-flash-next, open: True}`.
`cfg.roles["sub_agent"]` → `{deployment: qwen3.8-flash-next-instruct, open: True}` plus the dead map.
Neither binding uses any per-use field. Per-use policy lives everywhere except the bindings: on the
deployment entries (F1, F5), in settings (`worker_timeout_seconds`, F5), and at call sites
(`artifact_tools.py:1576–1608`, `entailment.py:351–365`, `context_compressor.py:235–261` each pass
their own timeout and token cap).

The gap between F9 and F1 is the whole commission.

### F10 — What the two-entry shape has cost, from the record.

**Verdict:** POSITIVE.

**Query:** `git log --oneline --since=2026-08-01 -- config/models.yaml config/model_roles.yaml`.

**Output:** `models.yaml` 14 commits since 2026-08-01 (70 all time), `model_roles.yaml` 10. Seven
of those commits are a swap, a revert or a repoint. FRE-1317 was an outage ("every primary and
HYBRID turn failing"). FRE-1363 was blocked for a day on a stale id stranded by a revert. FRE-1411
found 26 tests pinned to a binding. The two sessions that selected the instruct entry as primary ran
4 turns, 1 errored (`route_traces` joined to `session_model_selections`, since 2026-09-03).

### F11 — The sub-agent never follows the primary. The flat binding always decides.

**Verdict:** POSITIVE.

**Query (Postgres → ES):** route traces with `sub_agent_count > 0` whose session's primary selection
is a cloud or OVH model, then `model_call_completed` rows for each trace grouped by `(role, model)`.

**Output:**

| trace | primary selection | sub-agent calls ran on |
|---|---|---|
| `9c0159…` 2026-08-31 | `claude_sonnet` | `unsloth/qwen3.6-35-A3B-subagent` ×4 |
| `883c5f…` 2026-09-02 | `qwen3.6-27b-ovh` | `unsloth/qwen3.6-35-A3B-subagent` ×5 |

A Sonnet primary got local Qwen workers. That is the flat `sub_agent` binding, and it is consistent
with F6. Whether that pairing is *wanted* is a design question (P8). That it is *silent* is not.

### F12 — Three records, and none can say which catalog entry ran a past turn.

**Verdict:** POSITIVE.

**Query (ES, `agent-logs-2026-09`, the incident trace):** `model_call_started` / `_completed` /
`_error` rows.

**Output (abridged):** 9 starts, 8 completions, 2 errors. Every row: `model =
unsloth/qwen3.8-flash-next`, `max_tokens = 2048`. Role `primary` at 04:41:03 (planner, 13 s) and
04:43:49 (synthesis, `LLMTimeout` at 04:45:19.437, 90.001 s). No field names the catalog key.

**Query (Postgres):** `session_model_selections` for trace `494d9612…` — turn at 2026-09-05
10:57:29, selection row `qwen3.6-27b-ovh` with `updated_at` 15:13:49. ES shows that turn's primary
ran on `unsloth/qwen3.8-flash-next`. The selection table overwrites in place, so a join gives the
later state, not the turn's.

**Query (Postgres):** `sessions.primary_model_at_creation` — `''` 1021, `unsloth/qwen3.6-35-A3B`
337, `unsloth/qwen3.8-flash-next` 32, `unsloth/Qwen3.6-27B` 3. Wire ids, ambiguous between the
paired keys.

Filed as **FRE-1424**.

### F13 — Thinking-off sub-agents do call tools on this model.

**Verdict:** POSITIVE.

**Query (ES, incident trace):** event counts.

**Output:** `tool_call_started` 4, `tool_call_completed` 4, `run_python_started` 4,
`run_python_finished` 4, `sub_agent_start` 3, `sub_agent_complete` 3 — all under the instruct entry
(`disable_thinking: true`). Master's FRE-1420 table places every execution inside its sub-agent's
lifetime.

This answers half of the owner's hypothesis: on `qwen3.8-flash-next`, a no-thinking worker still
uses tools. The other half — that thinking-off is a *performance* win for workers — is not measured
by this study. FRE-432 measured ~75 % thinking share on trivial turns, which is the prior.

### F14 — Layer census: what decides a turn's model, and what is dead.

**Verdict:** POSITIVE.

**Query:** each layer read live (container env, Postgres, loaded config) and traced to its readers
at the deployed hash.

| Layer | Live state | Readers | Verdict |
|---|---|---|---|
| `session_model_selections` | 153 rows, all `role = primary` | `factory.py:183`, `executor.py:1312/2569` | live, primary only |
| `model_roles.yaml` `bindings:` | primary, sub_agent + 12 pinned | `resolve_role_target` (9 sites) | live |
| `model_roles.yaml` `roles:` matrix | 12 entries incl. `primary` | `resolve_role_model_key` (8 background sites); `config_guard.py:417–439` validates every row | live for background roles; `primary` row validated, not dispatched |
| `defaults_by_primary` | 8 rows | none | **dead** (F6) |
| deployment entry values | budgets, sampler, thinking | local branch `litellm_client.py:1494–1541`; cloud branch `:898–966` sends `temperature` and `timeout` only when the caller passes them | live on local, and win over binding on redirect (F4); partly inert on cloud |
| `settings.*_timeout_seconds` | 60 / 85 / 600, no env override | `expansion_controller.py:629` (workers, as `timeout_s`); `:440` (planner, outer `wait_for` only) | live; wins over deployment for workers, not for the planner (F5) |
| `.env` | no model or budget keys | — | not a model layer today |
| `AGENT_DEPLOYMENT_PROFILE=cloud` | set in container | `required_secrets`, compose guard only | not a model layer |

Two role tables live in one file, read by different resolvers. `open: true` on `sub_agent` and
`artifact_builder` is read (`session_api.py:655–668` exposes candidates for every open role,
`constraint_options.py:213` gates artifact selection on it) but the UI writes selections for
`primary` only — 0 rows for any other role. The picker (`role_candidates`,
`model_loader.py:485`) offers every `kind: llm` entry whose provider is available, which is how a
role profile reaches the model list.

### F15 — Which of the nine "role policy" fields exist on which provider.

**Verdict:** POSITIVE.

**Query:** loaded catalog, all `kind: llm` entries.

| key | provider | timeout | max_tokens | conc | disable_thinking | budget | effort | temp | top_p | presence |
|---|---|---|---|---|---|---|---|---|---|---|
| qwen3.8-flash-next | slm_local | 600 | – | 1 | False | 32768 | – | 1.0 | 0.95 | 0.0 |
| qwen3.8-flash-next-instruct | slm_local | 90 | 2048 | 3 | True | – | – | 0.7 | 0.8 | 1.5 |
| qwen3.6-35b-thinking | slm_local | 600 | – | 1 | False | 32768 | – | 0.6 | 0.95 | 0.0 |
| qwen3.6-35b-instruct | slm_local | 90 | 2048 | 3 | True | – | – | 0.7 | 0.8 | 1.5 |
| qwen3.6-27b-ovh | ovhcloud | 300 | 32768 | 25 | False | – | – | 0.6 | – | – |
| claude_sonnet | anthropic | 180 | 128000 | 10 | False | – | high | – | – | – |
| claude_haiku | anthropic | 30 | 64000 | 20 | False | – | – | – | – | – |
| gpt-5.4-mini | openai | 60 | 8192 | 10 | False | – | none | 0.0 | – | – |

Three classes fall out. **Budget** (timeout, max_tokens): every entry, every provider.
**Concurrency** is not in that class — it is a provider fact and a model-footprint fact, and the
role's share of it is a priority, not a count (see P1).
**Thinking**: one intent, two vocabularies — local `disable_thinking`, cloud `reasoning_effort`.
`config_guard` already enforces the right one per placement and probes litellm for what an effort
becomes on each model. Anthropic cannot express "off" (`none` is dropped by litellm, `models.py:305`).
**Sampling**: the full six-field preset is local only, and it comes from the model card.

**A gap the review surfaced.** The FRE-1007 guard walks `bindings:` and checks only the deployment
each binding names (`config_guard.py:1143–1163`). The primary picker offers every available
`kind: llm` entry (`model_loader.py:504–512`). So a selectable model can carry no reasoning
declaration at all and pass: `qwen3.6-27b-ovh` declares neither local field (by comment, deliberately)
and `claude_haiku` declares no `reasoning_effort`. The guard enforces the property for two of the
eight models a user can pick.

---

## Proposals

At most ten. These are the single place recommendations appear.

### P1 — Two nouns: one catalog entry per served artifact, one role binding per role. Collapse the local pairs.

Delete `qwen3.8-flash-next-instruct` and `qwen3.6-35b-instruct`. Move their role policy onto the
`sub_agent` binding, which already has the fields (F9):

```yaml
sub_agent:
  deployment: inherit          # the primary's model, whatever the session picked
  mode: instruct               # P3 — the card's non-thinking preset
  disable_thinking: true
  max_tokens: 2048
  default_timeout: 90
```

`inherit` is one new value for `deployment`, resolved against the session's primary. It replaces the
eight-row map (F6). The picker then lists models only. `quantization` cannot disagree with itself.
The wire id becomes unambiguous for local models.

**Concurrency is not role policy. Priority is.** (Corrected after the owner's challenge: a fixed
count on a role cannot be right across providers — local serves 3 slots, cloud 50.) The controller
already separates the two physical facts from the one policy. The **provider ceiling** is acquired
first and is *"the binding constraint across every deployment it serves"* (`concurrency.py:166`).
The **per-model sub-limit** beneath it means something only where a model's footprint is smaller
than its provider's slots; with one served local model at 262K pooled KV the model *is* the
provider, and the number is 3. And **`InferencePriority`** (`concurrency.py:57`: `CRITICAL`,
`USER_FACING`, `ELEVATED`, `BACKGROUND`, `DEFERRED`) is what the semaphore wakes first. Today the
primary passes `USER_FACING` (`executor.py:6217`), background roles pass `BACKGROUND`, and
sub-agents pass nothing — so they default to `USER_FACING` and rank equal to the primary.

So under P1 `max_concurrency` stays on the provider and the model. The single local entry carries
`3`, the truth of the box. The role carries `priority`: `primary: USER_FACING`,
`sub_agent: ELEVATED`, background roles `BACKGROUND`. A rank is valid on every provider; a count is
not. What is lost is the old entry's "one primary at a time" — which only ever approximated "the
primary goes first", and priority says that directly. Worst case is a primary sharing the box with
two workers of another session (turns in different sessions run as independent tasks,
`service/app.py:2561–2573`), which is today's state too. FRE-1380 serialises workers *within* a
turn only.

**`inherit` is a resolver value, not a YAML trick.** It must resolve centrally in
`resolve_role_target` and `resolve_selected_deployment` (which today returns `binding.deployment`
literally, `model_loader.py:473–477`), the catalog validator must accept the sentinel
(`models.py:536–551` rejects any binding deployment absent from `models:`), and the factory must
pass the primary's session selection when resolving `sub_agent` (`factory.py:169–188` asks for
`get_current_selection("sub_agent")`, which is always `None`; `get_current_selection("primary")` is
available at that site). Three edits, all in one module and one factory.

**Why not just fix the entry:** because the class recurs. Every future local model needs its own
instruct twin under the current shape, and every twin re-creates F1, F5, F6 and F12.

### P2 — Role policy applies regardless of which model the session picked. Change the resolver rule.

`resolve_role_target` drops binding overrides when `key != binding.deployment` (F4). Replace with:
binding **budget** fields (`default_timeout`, `max_tokens`) always apply; binding **mode** fields
apply when the selected model declares that mode, else the model's default mode. Then FRE-1420's
AC-2 ("a deployment cannot lower a role's budget") is satisfiable, and it is not today.

FRE-1420 must be re-scoped onto this. Its current option 1 passes review and changes nothing.

**Scope, stated.** P2 governs the factory path — the primary's planner and synthesis calls, and any
open role resolved through `get_llm_client`. Three roles bypass it by design and set their own budget
at the call site: `artifact_builder` (`get_llm_client_for_key`, `artifact_tools.py:1501–1534`),
`vision` (rebuilt by raw key, `executor.py:5748–5767`) and `compressor`
(`context_compressor.py:235–261`). That is acceptable. It must be written down. And for P2 to hold
on a cloud primary, the cloud dispatch branch must read `default_timeout` and `temperature` from
the effective definition the way the local branch does — today it sends them only when the caller
passes them (`litellm_client.py:898–966`).

### P3 — Follow the model card: modes live on the model, the role names one.

A local model declares its card's presets as named modes. A cloud model declares none.

```yaml
qwen3.8-flash-next:
  modes:
    thinking: { enable_thinking: true,  temperature: 1.0, top_p: 0.95, presence_penalty: 0.0 }
    instruct: { enable_thinking: false, temperature: 0.7, top_p: 0.8,  presence_penalty: 1.5 }
```

This is where the six sampler fields go, and it is why `RoleBinding` does not need `top_p` or
`presence_penalty` added. The binding's `mode:` selects. For cloud, `disable_thinking` / effort on
the binding maps through the existing per-model probe (F15).

### P4 — Delete the dead layer and merge the two role tables.

Remove `defaults_by_primary` and its schema field. Cancel FRE-966 and FRE-967 as superseded by
`inherit`. Fold the `roles:` matrix into `bindings:` so one resolver reads one table (F14). This is
the ADR's cleanup clause, not a separate ticket.

**P1–P4 are one ADR.** They change a live resolution path and the config schema. The owner is the
architect and this is a boundary decision. "The design is sound, migrate onto it" is the honest
title.

### P5 — Reconcile the catalog against `/v1/models` at boot.

For every `slm_local` entry: fetch the endpoint once at startup, compare `id`, `context_length`,
`quantization`. A mismatch on `id` is fail-closed (it is FRE-1317). A mismatch on the other two is a
`config_guard` finding with the served value in the message. The GET already reaches the host (F8).
This turns "found by a turn failing" into "found before the first turn".

### P6 — Remove `thinking_budget_tokens` from the local reasoning vocabulary. Re-scope ADR-0141 D8.

The key is inert (F3). Removing it needs a guard change, because the guard today accepts only
`disable_thinking is True` or a non-`None` budget (`config_guard.py:968–978`): an explicit
`disable_thinking: false` is merged and then rejected as `reasoning_declaration_missing`. The change
is to test field *presence*, not truth, so that an explicit `false` is the thinking-on declaration.
Under P3 the guard must also read the selected `mode`. And it must walk every selectable
`kind: llm` entry, not only the ones a binding names (F15 gap). D8's adaptive budget has no lever on
this backend and must not spawn tickets until one is measured. **FRE-1423**, filed.

### P7 — Record the thinking mode and the resolved catalog key per turn.

`assembler.py:320` populates `thinking_enabled` from the resolved definition; a new column on
`route_traces` carries the resolved deployment key. Both read from the same `resolve_role_target`
result the client was built from. **FRE-1422** and **FRE-1424**, filed. Small, and they are the
instrument the owner's 04:41 test needed.

### P8 — Under a cloud primary, the sub-agent inherits at the lowest effort. One line overrides it.

`inherit` under Sonnet runs Sonnet at `low` (Anthropic has no `none`). That costs money. If the
owner wants a cheaper worker on a cloud primary, `sub_agent.deployment: claude_haiku` is one
explicit line, visible in the file, not a map. Today the choice is made silently and the answer is
"local Qwen" whatever the primary (F11).

### P9 — Where the delegation decision lives: a harness floor, a criterion at the decision point, a visible budget. Seed for a later ADR.

Discussed with the owner and recorded here so it is not lost. Not this ticket's deliverable.

Models under-delegate. Claude Code does not ask the model whether it is capable. It states a rule
keyed on an observable — *"answering would mean reading across several files — delegate it and you
keep the conclusion, not the file dumps"* — in the tool description, at the moment of decision. Three
levers for Seshat, in order of reliability: (1) a harness floor — a tool result above N chars, or a
task naming more than K sources, is delegated without model judgement, which is what the FRE-1380
ruling already says sub-agents are for; (2) the context-cost criterion in the tool description, not
in a planner prompt — FRE-1377 found the router is the wrong place to decide expansion; (3) the
remaining context budget stated to the model. Parallelism is a negative on the local box (four
concurrent requests wedge the server, concurrency 1→3 buys 15.8 % wall-clock) and a positive only
when the worker runs on a cloud provider — a fact the harness knows from `provider:` and can state
per session.

Whether a local primary *chooses* to delegate is unmeasured. The FRE-1416 nonce-probe shape tests
it: a task that cannot fit in one context, and a digest that proves the split.

---

## Filed tickets

All at `Backlog`, none promoted. This list is the complete census of tickets this study filed.

- **FRE-1422** — `route_traces.thinking_enabled` is hard-coded `None` (F7, P7).
- **FRE-1423** — `thinking_budget_tokens` is inert on the live llama-server (F3, P6).
- **FRE-1424** — A past turn's selected model cannot be reconstructed (F12, P7).

No ticket was filed for P1–P5, P8 or P9. P1–P4 are an ADR, which is the ADR seat's to write. P5 and
P8 are inside that ADR's scope. P9 is a seed.

---

## Method appendix

**M1 — Deployed revision.** Container `cloud-sim-seshat-gateway` (image created 2026-09-05
18:22 UTC). Config is baked into the image, not mounted (`docker inspect` shows no bind mount under
`/app/config`). `md5sum` of these files inside the container matches the worktree at `33ba59c4`:
`litellm_client.py`, `model_loader.py`, `settings.py`, `factory.py`, `expansion_controller.py`,
`sub_agent.py`, `session_api.py`, `config_guard.py`, `concurrency.py`, `provider_health.py`,
`llm_client/models.py`, `route_trace/assembler.py`. `models.yaml` and `model_roles.yaml` differ in
hash but not in any non-comment value (diff after stripping comments: empty). Every code citation
above is therefore a citation of running code.

**M2 — Stores and windows.** Live server: `http://localhost:8600` on the VPS host, the published
port of the Caddy egress that carries the Cloudflare Access credential (ADR-0132 D1); five
completions were sent to it, one at a time, sequentially, all under 4 s. Postgres:
`cloud-sim-postgres`, database `personal_agent`, tables `route_traces` (805 rows, 2026-06-07 to
2026-09-06), `session_model_selections` (153 rows), `sessions`. Elasticsearch: `localhost:9200`,
index `agent-logs-2026-09`, matched by full-text on the trace id because `trace_id` is not a
keyword field there; `slm-requests-*` holds 22 documents, all from 2026-08-07..09, and was not used.
ES counts are provisional (FRE-1051).

**M3 — Resolver simulation.** Run with `uv run python` in the explore worktree against the loaded
catalog, calling the deployed `resolve_role_target` directly. The "with `default_timeout=600`" arm
was produced by `model_copy` on the loaded `RoleBinding`, not by editing any file.

**M4 — Rejected instruments.** `python -m personal_agent.config.resolve` is not importable inside
the container (`ModuleNotFoundError`) and was not used. `/props` on the served model is not proxied
by Caddy (non-JSON response) and quantization was read from `/v1/models` instead. The
`route_traces` ⋈ `session_model_selections` join was used only for F10's count and is unreliable
for attribution (F12); F11 attributes by ES `model_call_completed.model` per trace, not by the join.

**M5 — What this study did not measure.** Whether thinking-off is a latency or quality win for
workers (F13 caveat). Whether a local primary chooses to delegate (P9). Whether the served
llama-server exposes any reasoning-budget parameter under a different key (F3, arm 3). Google
provider behaviour — not in the catalog.

**M6 — Adversarial review.** Codex `gpt-5.6-sol`, effort `xhigh`, read-only, 16 min, job
`task-mtpfa0t3-ggg81a`, ten numbered questions with file:line evidence demanded. Verdicts: 1, 2, 7,
8 HOLDS; 4, 6 PARTIAL; 3, 5, 9, 10 FAILS. Each FAILS and PARTIAL claim was re-checked against the
worktree before the document was edited: planner `wait_for` without `timeout_s`
(`expansion_controller.py:440–460`), guard scope (`config_guard.py:1143–1163`),
`constraint_options.py:213`, the nine test references, the cloud branch's conditional
`temperature`/`timeout`, `tools/test_slm_server.sh:35`, the transport test asserting
`body["thinking_budget"]` (`test_local_via_litellm.py:368–396`), `register_model(role=...)`, and
the two undeclared selectable models. All nine held. The review's transport-test evidence
strengthens F3's arm 1: the wire carries top-level `thinking_budget` under litellm 1.98.0.

**M7 — Owner discussion, folded in.** The two-noun model, "follow the model card", `inherit` as
the default, and the delegation-decision levers came from the owner's own account of the system and
from the Claude Code agent-definition rules quoted in P9. They are recorded as proposals, not
findings.
