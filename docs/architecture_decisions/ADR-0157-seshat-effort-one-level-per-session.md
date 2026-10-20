# ADR-0157: Seshat Effort — One Level per Session Sets the Harness and the Primary's Reasoning, and D7 Binds the Default Level

**Status:** Proposed — 2026-10-20
**Date:** 2026-10-20
**Deciders:** Owner (the selector, its name, the three levels, per-session scope, presets only, Standard as the default, P2 on Standard, Fast thinking off, D7 on the default level only, 2026-10-19 and 2026-10-20), `master` (the starting table of levels, 2026-10-19), `adr` seat at Opus 5.5 (author, FRE-1620)
**Tags:** routing, expansion, planner, pwa, model-selection, reasoning, config

**Amends:** [ADR-0154](ADR-0154-what-the-planner-sees-and-what-it-decides.md) D2 (a new forced-SINGLE reason), D3 item 3 (the round budget that binds), D7 (requalification binds the default level) and AC-10. **Extends:** [ADR-0121](ADR-0121-model-catalog-and-selection-layer.md) §4 with a second choice per session, under the same eleven invariants. **Replaces:** the FRE-1618 draft of ADR-0154 Amendment 3 (a P2 switch and a 50/50 trial), which was never merged. **Umbrella:** FRE-1620.

---

## Context

### The owner's request

On 2026-10-19 the owner asked, in the master session: "Add a selector to the interface, next to the model selector allowing me to choose the harness level i want applied. We can group helpers on/off, round limits, and other key parameters". Then: "It would represent "Seshat effort" - model effort/reasoning could be packaged in to the different level settings."

### The trigger

On 2026-10-19 at 05:27Z the owner set the helper round cap to 12 (FRE-1619): `AGENT_SUB_AGENT_MAX_TOOL_ITERATIONS=12` in `.env`. The planner system prompt renders the round budget that binds (ADR-0154 D3, `expansion_controller.py:198`). So the cap also changed the live planner prompt and its hash. The D7-qualified hash (`ffe08113…`, FRE-1581 and FRE-1515) is the 20-round prompt. Under D7, a new hash is a routing change that needs a passing probe run before it ships. The owner accepted that the live prompt is not qualified until this selector ships (FRE-1620, 2026-10-19).

### The levers are global today

Each harness lever is a process-wide setting:

| Lever | Setting | Where it binds |
|---|---|---|
| Helpers on or off | `expansion_enabled` | `request_gateway/decomposition.py:101`, reason `expansion_disabled` |
| Helper round budget | `sub_agent_max_tool_iterations`, `sub_agent_rounds_by_thoroughness` | `settings.sub_agent_rounds_for`, read at `sub_agent.py:978` (task message), `:1932` (the loop's enforced cap), `:2505` (`sub_agent_start.round_budget`) and `expansion_controller.py:198` (planner prompt). `sub_agent_max_tool_iterations` also sizes the helper's outer deadline (`_effective_hard_deadline`, `sub_agent.py:459`). |
| The planner's decomposition policy (P2) | none on `main` | — |
| The primary's reasoning | the catalog mode `default` of each deployment | `executor.py:6937` builds the primary client with no mode, and `factory.py:133` resolves `default_mode` through `resolve_mode()` (`llm_client/models.py:741`). An attachment turn that escalates to the vision deployment builds its client through `get_llm_client_for_key` (`executor.py:6944`), which takes no mode. |

The settings object loads once per process (`config/settings.py:3778`). `run_sub_agent` takes no override (`sub_agent.py:2395`). So a change needs an `.env` edit and a gateway restart, and it applies to every session at once.

The model choice is already per session. ADR-0121 §4 stores it server-side, and ADR-0079's eleven invariants govern it.

### What reasoning control each model offers

The picker offers nine deployments. Each one has `default_mode: default`. The dialect (ADR-0145) decides which reasoning field a mode can carry:

| Deployment | Dialect | Accepted reasoning values | `default` mode | Room above `default` |
|---|---|---|---|---|
| `qwen3.8-flash-next` (the default primary), `qwen3.6-35b-thinking`, `qwen3.8-27b-mtplx`, `qwen3.8-flash-next-mtplx`, `gemma-4-26b-a4b` | `llamacpp_qwen` | `enable_thinking` true or false | thinking on | none |
| `qwen3.8-27b-ovh` | `ovh_qwen` | `reasoning_effort` in {none, low, medium} | `medium` | none |
| `claude_sonnet` | `anthropic_adaptive` | `effort` in {low, medium, high} | `high` | none |
| `claude_haiku` | `anthropic_budget` | `budget_tokens` | thinking off | yes |
| `gpt-5.4-mini` | `openai_gpt5` | `reasoning_effort` in {none, low, medium, high, xhigh} | `none` | yes |

Sources: `config/models.yaml`, `DIALECT_FIELDS` and `DIALECT_VALUE_DOMAINS` (`llm_client/models.py:136-181`). On seven of the nine, `default` is already the top of the range. So "more reasoning" has no effect on most models. The real reasoning lever is downward, in a faster level. On 2026-10-20 the owner stated that the GPT and Haiku entries are outdated and their pricing is wrong. FRE-1621 analyses the replacements.

### P2, the "prefer SINGLE" policy

FRE-1615 Block 1 (`docs/research/2026-10-18-fre-1615-block1-planner-arms.md`, one draw, eval stack, production llama.cpp) measured the owner's "prefer SINGLE" text, named P2, against the production planner (PL). On the 6 tasks that PL expanded, P2 took 1,892 s against 2,554 s (−26%), with 76 model calls against 103. Both arms scored 15 of 24 judge points. A blind comparison of the 6 pairs preferred PL on 4, P2 on 0, with 2 ties. On 2026-10-19 the owner chose to put P2 on a level instead of a 50/50 trial.

### A selector cannot measure P2's quality

In a trial, chance chooses the arm, so both arms see the same kind of question. With a selector, the owner chooses the level, and probably chooses Deep for harder questions. A difference between levels can then come from the questions and not from the level. So P2 ships on Standard with no measure of its effect on answer quality. The owner accepted this on 2026-10-19 when the trial was dropped.

### What this ADR does not decide

- The new GPT and Haiku catalog entries, their prices and their `deep` modes. FRE-1621 delivers them.
- The primary's own tool-loop limit (`orchestrator_max_tool_iterations`). It stays global.
- `sub_agent_researcher_min_search_rounds`. It stays global.

---

## Decision

### D1 — Three levels, presets only

Seshat effort has three levels. The owner picks a level. A level sets all of its values together. No value can be set alone (owner, 2026-10-20: "Presets only").

| Value | Fast | Standard (the default) | Deep |
|---|---|---|---|
| `expansion` — helpers | `false`: no planner call and no helper | `true`: the planner decides (ADR-0154 D2) | `true`: the planner decides |
| `sub_agent_rounds` — helper round budget | not used | 12 | 20 |
| `planner_policy` | not used | `p2` (D6), subject to the ship rule of D7 | `baseline` |
| `primary_mode` — the catalog mode of the primary | `fast` | `null`: the deployment's `default_mode` | `deep` |

The values live in one committed file, `config/seshat_effort.yaml`, with `default_level: standard`. The gateway loads it once at start into a frozen model and refuses to start if it is invalid (D3). A new level, or a new value, is a PR to that file.

The planner and the helpers keep their own modes at every level. On a D2 turn, the planner uses the deployment's qualified `planner` mode (ADR-0154 D4: thinking off). Outside D2, a deployment without a `planner` mode answers the planner call with its `default` mode, as today (`with_requested_mode`, `config/model_loader.py:393`). The helpers use the `sub_agent` binding's `worker` mode (thinking off on the local models, `effort: low` on `claude_sonnet`). No level changes either mode. The planner's D7 qualification and ADR-0154 AC-7 rest on thinking off.

The sentence of FRE-1619 ("The budget is a limit, not a target…") is static text in every helper prompt, at every level.

### D2 — The level is per session, and the server holds it

The level is a choice per session, as the model is. It inherits the eleven invariants of ADR-0121 §4 unchanged, including invariant 2 (an explicit stored value, never a silent fallback) and invariant 3 with its post-deploy correction (the client keeps sending, and the server decides). The design:

1. **Stored on the session, always explicit.** A new column `sessions.seshat_effort`, `NOT NULL`, with a check on the values `fast`, `standard` and `deep`. The migration writes `standard` into every existing row. No row holds a null.
2. **The client keeps sending.** The PWA sends the level that its selector shows with every chat request. On a new session, the selector shows Standard (owner, 2026-10-20: "standard"), and the server stores the sent level. If a request for a new session carries no level, the server stores `default_level` explicitly. For an existing session, the stored value wins, and a sent value is ignored.
3. **The change is a write.** `PATCH /api/v1/sessions/{session_id}/effort`, with `sessions:write`, scoped to the resolved user, with 404 on a mismatch (invariant 4).
4. **Hydration and notice.** The session GET and `GET /{session_id}/config` return `seshat_effort` and `effort_provenance` (`server-hydrated` or `default`). The WS `STATE_DELTA` on reconnect carries the level, and a change notifies the single active socket (invariants 5, 6 and 10).
5. **A failed write reverts.** If the `PATCH` fails, the selector returns to the stored level and shows the error (invariant 9).
6. **An in-flight turn keeps its level.** A change applies from the next turn (invariant 7).
7. **Both chat entry paths.** The rules above apply to `POST /chat/stream` (the PWA) and to `POST /chat` (the CLI and the evaluation harness, `service/app.py:2142`). A request on either path resolves the level in the same way.

The selector sits next to the model picker. For each level, it shows the catalog mode that the session's model will run, or "model default" when the model declares no mode for that level (D5).

### D3 — One frozen spec per turn, under global ceilings

At turn launch, the gateway resolves the session's level into a frozen `EffortSpec`: the level name and the four values of D1. The spec travels with the turn in the same way as the model selection (`set_current_selection`). These four readers take the spec, not the global setting:

1. the decomposition step (`expansion`, D4);
2. the planner prompt render (`sub_agent_rounds` and `planner_policy`, D6);
3. the helper's round budget, in all three places: the loop's enforced cap, the task message and `sub_agent_start.round_budget`;
4. the primary's mode on both client paths: the ordinary path and the vision escalation (`primary_mode`, D5).

One reader keeps the global value on purpose: `_effective_hard_deadline` sizes the helper's outer deadline from `sub_agent_max_tool_iterations`. It is a safety net, not the round budget, and the ceiling below bounds every level's budget.

The global settings stay, as ceilings and kill switches:

- **`expansion_enabled = false` overrides every level.** The turn records `expansion_disabled`, as today.
- **`sub_agent_max_tool_iterations` is the upper bound.** The gateway refuses to start if any level's `sub_agent_rounds` is above it. At this date `.env` sets it to 12, so the deploy must remove that line (the code default is 20), or the gateway refuses to start.
- **`sub_agent_rounds_by_thoroughness` must be empty.** A level sets one budget for all three thoroughness levels, so the gateway refuses to start if the map is set. It is empty at this date. A later amendment can give a level a map per thoroughness.

This amends ADR-0154 D3 item 3: the round budget that binds, and that the planner prompt renders, is the level's `sub_agent_rounds`. This ADR's implementation chain changes round budgets on purpose, so it is outside ADR-0154 AC-9, as a change under FRE-1487 is.

### D4 — Fast routes every turn to SINGLE

When the spec's `expansion` is `false`, the decomposition step returns `SINGLE` with the reason `effort_fast`, for every task type. The check runs after the resource forcings and after `expansion_disabled`, and before the matrix. The planner does not run, the fallback planner does not run, and no helper starts.

This amends ADR-0154 D2: `effort_fast` joins `expansion_denied`, `zero_budget` and `expansion_disabled` as a reason that forces `SINGLE` before the matrix. ADR-0154 AC-1 counts only turns "with expansion permitted", so it excludes Fast turns.

### D5 — The level selects the primary's catalog mode

A level names a mode: `fast`, none, or `deep`. When the deployment that answers the turn declares that mode, its client requests it. When it does not, the client requests `default_mode`. This holds on both client paths: the session's primary through `get_llm_client`, and the vision escalation through `get_llm_client_for_key`, which gains a mode argument. The client never invents a field: every mode passes the existing catalog validation against its dialect (`DIALECT_FIELDS`, `DIALECT_VALUE_DOMAINS`) at load. So a level cannot send a field that a deployment refuses.

Fast lowers the primary's reasoning to the bottom of each dialect's range. That is thinking off on the local models and on OVH, and low effort on `claude_sonnet`. The `anthropic_adaptive` dialect has no off value in its `effort` field, so Sonnet's Fast still reasons, at low effort. The build adds these `fast` modes. No `deep` mode is added at this date:

| Deployment | `fast` mode | Reason |
|---|---|---|
| The five `llamacpp_qwen` deployments | `enable_thinking: false`, with that entry's own `worker` sampling values, copied | Each entry's `worker` sampling is its model card's non-thinking preset: the Qwen card for the four Qwen entries, the Gemma 4 card for `gemma-4-26b-a4b`. A copy, so that a later `worker` change does not move Fast. |
| `qwen3.8-27b-ovh` | `reasoning_effort: none` | The bottom of the dialect's range. |
| `claude_sonnet` | `effort: low` | The bottom of the dialect's range. Not thinking off. |
| `claude_haiku`, `gpt-5.4-mini` | none | Their `default` is already thinking off or `none`. |

So at this date, Deep uses `default` on every model, and the selector shows "model default" for Deep. A model gets a `deep` mode only in a change that adds it to its catalog entry. FRE-1621's follow-up adds `fast` and `deep` to the new GPT and Haiku entries.

**Fast is unmeasured.** No run measured the primary with thinking off, with its tools and its full prompt. The catalog notes that Gemma with thinking off answered an arithmetic probe wrongly (`config/models.yaml:382`). Fast trades answer quality for speed, and the owner judges the result.

### D6 — P2 on Standard

The P2 block is the owner's text, word for word, as master copied it into FRE-1615 (comment of 2026-10-18 12:42Z, "Arm P2"). It equals the `_P2_DECOMPOSITION_POLICY` constant of `scripts/eval/fre1615/arms/P2.patch`, which FRE-1615 measured: 913 characters, 23 lines, each line ending in a newline.

```text
DECOMPOSITION POLICY
Prefer SINGLE.
Delegate only when independent execution is likely
to materially improve evidence quality, coverage,
or total completion time.
Account for:
- Additional model calls and context overhead
- Shared inference-server contention
- Information lost through worker summaries
- Work the primary agent can perform directly
Do not create a worker solely to summarize,
compare, or reformat information another worker
is already gathering, unless independent analysis
has a specific benefit.
HYBRID delegates bounded specialized work while
the primary handles remaining reasoning or synthesis.
DECOMPOSE requires at least two genuinely independent
subtasks whose results can be merged without depending
on one another.
Prefer fewer, more complete tasks when they share
the same tools, sources, and information.
Every task needs a specific deliverable and a
measurable completion condition.
```

1. **The place.** In `_build_planner_system_prompt` (`orchestrator/expansion_controller.py`), directly before the first `Rules:\n` (`_PLANNER_RULES_HEADING`), followed by one blank line. The FRE-1498 decline rule stays the first rule.
2. **The condition.** Only when the decline rule is in the prompt (`admit_single`, set from `planner_may_decline`, so `planner_asked` turns only), and only when the spec's `planner_policy` is `p2`. The fallback planner never gets the block.
3. **No other change.** With `planner_policy: baseline`, the prompt is byte-identical to the prompt that the same round budget renders today.
4. **The size.** The block and its blank line add 914 characters, about 200 tokens, on `p2` turns only. The text is static, so it stays in the cached prefix. ADR-0154 D1's bound on a `p2` turn becomes 2,439 + 914 + 64,000 characters, plus the Amendment 2 skill line when that line ships.
5. **The order with ADR-0154 Amendment 2.** The block goes before the `Rules:` heading. The Amendment 2 skill line goes inside the list, after the decline rule. The two additions never touch.

### D7 — D7 binds the default level

This amends ADR-0154 D7 ("Requalification") and AC-10 (owner, 2026-10-19: "the D7 test binds the default level only. The owner chooses the other levels on purpose").

1. **The configuration of a level.** ADR-0154 D7's fingerprint (engine and build, model and quant, the `planner` mode, the rendered planner prompt hash) is taken per level. The prompt that D7 qualifies is the D2 prompt: the prompt of a `planner_asked` turn, with `admit_single` true, as the committed probe renders it (`scripts/eval/fre1537/`). It depends on the level's `sub_agent_rounds` and `planner_policy`, and on the worker tools that governance grants. A planner prompt with `admit_single` false runs only on a turn outside D2, for example a `DELEGATION` turn that the matrix expands. That prompt never carries P2 (D6). ADR-0154 D7 does not qualify it, and this ADR does not change that.
2. **Only the default level requalifies.** A change to the default level's fingerprint is a routing change. It needs a passing D7 run, with Amendment 1's paired rows, before it ships. A change of which level is the default is a routing change too.
3. **The other levels need no probe run.** Their prompt hashes are recorded with their level (D8), and AC-10 accepts them.
4. **The deployment gate does not change.** A deployment reaches ADR-0154 D2 only with a `planner` mode, and it gets one only on a passing run of the default level's configuration.
5. **The ship rule for Standard.** The build runs D7 on Standard with `p2` (12 rounds) on the default local binding.
   - Every row passes: Standard ships with `p2`.
   - Any row fails: Standard ships with `baseline` (12 rounds), after a D7 run of that prompt passes. The per-fixture result of the `p2` run goes to the owner. P2 is then on no level until an owner-approved amendment.
   - The row at risk is expand-correct: production passed at 44 of 48 against a threshold of 43, and P2 asks the planner to decline more. This ADR does not soften that row.
6. **Deep.** With 20 rounds and `baseline`, Deep's prompt is expected to be byte-identical to the qualified 20-round prompt (`ffe08113…`), because FRE-1619 changes only the helper prompt. The build records the hash. A difference changes nothing under rule 3.
7. **ADR-0154 Amendment 2 (FRE-1617).** Its baseline arm is the default level's prompt at the date of its run.

### D8 — Every turn records its level

| Record | New field | Values |
|---|---|---|
| `route_traces` and its ES projection | `seshat_effort` | `fast`, `standard`, `deep` |
| `route_traces` and its ES projection | `primary_mode` | the catalog mode that the answering deployment requested: the session's primary, or the vision deployment on an escalated turn |
| `planner_outcome` | `seshat_effort` | as above |
| `planner_outcome` | `planner_policy` | `baseline` or `p2` when `admit_single` is true, else null |
| `model_call_completed` (`extra`) | `mode` | the catalog mode of the call |
| `chat_stream.launched` (`POST /chat/stream`), and a new `chat.launched` (`POST /chat`) | `seshat_effort` | the level that the turn resolved at launch. Each event fires once per accepted request, after the duplicate and validation checks and before the turn runs. |

- The two `route_traces` columns need an idempotent migration under `docker/postgres/migrations/` and the same columns in `docker/postgres/init.sql`, as does `sessions.seshat_effort` (D2). The migration runs as the `agent` role through `AGENT_DATABASE_ADMIN_URL`.
- `planner_policy` must agree with `planner_system_prompt_sha256` on every event.
- `sub_agent_start.round_budget` already exists. It reads the spec (D3).

### D9 — What this replaces

- **The FRE-1618 Amendment 3 draft** (branch `fre-1618-adr-0154-amendment-3`, no PR). Its setting `planner_decomposition_policy` (`off`, `on`, `ab`), its 50/50 trial, its quality stop and its review are not adopted. Its policy text and the block's place in the prompt carry into D6.
- **The global `.env` cap of 12.** Standard sets 12 per session. The `.env` line goes at the deploy (D3).

---

## Alternatives Considered

### Option 1: Keep the global settings

**Description:** the owner asks master for each change, and master edits `.env` and restarts the gateway.
**Pros:** no new code.
**Cons:** each change needs a restart, applies to every session, and is a D7 routing change. That is how the cap of 12 left the live prompt unqualified.
**Why Rejected:** the owner asked for a choice per session in the interface.

### Option 2: Single values as well as presets

**Description:** the selector also lets the owner change one value, for example Standard with 16 rounds.
**Pros:** more control.
**Cons:** each combination is a planner prompt that no probe tested, and the level name no longer says what ran.
**Why Rejected:** the owner chose presets only (2026-10-20).

### Option 3: A choice per turn

**Description:** the owner picks the level with each message.
**Pros:** a hard question can get Deep without a switch back.
**Cons:** it departs from the model picker's per-session form and from ADR-0121 §4.
**Why Rejected:** the owner chose per session (2026-10-19).

### Option 4: A D7 run for every level

**Description:** each level's planner prompt qualifies as the default does.
**Pros:** every prompt that runs has passed the probe.
**Cons:** three measurement windows for each change. The owner selects a non-default level on purpose, and the levels' purpose is to trade quality for time.
**Why Rejected:** the owner bound D7 to the default level (2026-10-19).

### Option 5: Keep the 50/50 trial inside Standard

**Description:** Standard runs P2 at random on half of its `planner_asked` turns, as the FRE-1618 draft designed.
**Pros:** a measure of P2's quality effect that the owner's choice of level does not bias.
**Cons:** about one `planner_asked` turn per day, so about 7 turns per arm in 14 days. The trial could show only a large difference.
**Why Rejected:** the owner dropped the trial (2026-10-19). The limit is stated in Context.

### Option 6: The planner and the helpers follow the level's reasoning

**Description:** Deep also turns on thinking for the planner and the helpers.
**Pros:** possibly better plans and helper reports.
**Cons:** ADR-0154 D4 and AC-7 require thinking off for the planner. With thinking on, declines took a median 165 completion tokens against 13 (ADR-0154 AC-7).
**Why Rejected:** the qualification depends on thinking off (owner, 2026-10-19, question 8).

### Option 7: One reasoning value per level for all models

**Description:** for example, Deep sets `reasoning_effort: high` on every call.
**Pros:** one value to read.
**Cons:** the dialects take different fields (ADR-0145). `llamacpp_qwen` takes no `reasoning_effort`, and `ovh_qwen` refuses `high`.
**Why Rejected:** a named mode per deployment fits every dialect, and the catalog validation already checks it.

---

## Consequences

### Positive Consequences

- The owner sets the harness per session, in one click, with no restart and no master action.
- The default level gets a D7-qualified prompt again, at 12 rounds.
- Every turn records its level and the primary's mode, so the turns of each level can be compared.
- The P2 work of FRE-1615 and FRE-1618 ships on a level, with the D7 guard intact.

### Negative Consequences

- Four readers take a per-turn spec in place of a global setting. That is more code on the turn path.
- Deep's reasoning equals Standard's on every model at this date (D5). Deep differs only by 20 rounds and `baseline`.
- P2's effect on quality stays unmeasured (Context).
- Fast's quality is unmeasured (D5).

### Risks and Mitigations

| Risk | Severity | Mitigation |
|---|---|---|
| A reader still takes the global setting, so a level has no effect | High | AC-1 tests each reader with a spec that differs from the global value. |
| The gateway refuses to start after the deploy, because `.env` still sets 12 | Medium | D3 states it, and the build ticket's deploy steps remove the line. The refusal is loud, not silent. |
| Standard's `p2` run fails expand-correct | Medium | D7 rule 5: Standard ships with `baseline`, and the owner gets the result. |
| A session stays at Fast without the owner being aware of it | Low | Every new session starts at Standard (D2), and the selector shows the level. |
| A mode sends a field that a deployment refuses | Low | Catalog validation at load (D5), and AC-4. |

---

## Implementation Notes

**Files (indicative):** `config/seshat_effort.yaml` (new); `config/models.yaml` (the `fast` modes); `src/personal_agent/config/` (the frozen loader and the start-up checks of D3); `request_gateway/decomposition.py` (D4); `orchestrator/expansion_controller.py` (D6, rounds from the spec); `orchestrator/sub_agent.py` (rounds from the spec); `orchestrator/executor.py` and `llm_client/factory.py` (the mode on both client paths, D5); `gateway/session_api.py` and `service/app.py` (D2); `observability/route_trace/` and the ES mapping (D8); `docker/postgres/migrations/` and `init.sql`; `scripts/eval/fre1537/render.py` (renders each level's prompt); `seshat-pwa/` (the selector).

**Sequence.** Two build tickets:
1. **Backend.** Everything except the PWA. Before its PR is ready, the build runs D7 on Standard, on the eval stack outside the production compose project (ADR-0154 Implementation Notes), and sets Standard's `planner_policy` by D7 rule 5. The run needs a llama.cpp measurement window, so every seat holds its tests and evals during it. Deploy class: `seshat-gateway` rebuild plus a Postgres migration (do-and-report).
2. **The PWA selector.** Blocked by ticket 1, because it needs the endpoint of D2. Deploy class: PWA-only rebuild.

**Testing.** Unit tests for each reader with a spec, for the start-up checks, for the asymmetry of D2, and byte-identity pins for each level's planner prompt.

---

## Verification / Acceptance Criteria

These criteria belong to this ADR. They are adjudicated on FRE-1620 after both build tickets deploy, not at the merge of this ADR. The live checks use the owner's own turns. No seat fires a live gateway turn for them.

- **AC-1 — Each level runs its own values, and every reader of D3 takes the spec.** · **Check:** unit tests with the global settings at `expansion_enabled=true` and `sub_agent_max_tool_iterations=30`, so that the global value differs from every level's value, and a spec for each level:
  - *Fast:* the decomposition returns `SINGLE` with `effort_fast` for every task type, and no planner call is made.
  - *Standard:* a helper driven by a stub model that requests a tool on every call stops after exactly 12 tool rounds. Its task message states "budget of 12 tool round(s)", its `round_budget` is 12, and the planner prompt contains "(12 tool round(s) each)".
  - *Deep:* the same, with 20.

  Live, over 14 days after both deploys: `route_traces` grouped by `seshat_effort`, joined to `sub_agent_start` on `trace_id`. · *Fails if* any test case differs, any live Fast row has a non-null `planner_decision` or `sub_agent_count > 0`, or any live `round_budget` differs from its turn's level.

- **AC-2 — The level follows ADR-0121 §4.** · **Check:** tests, one per case:
  1. A new session with a sent level stores it. A new session with no level stores `standard` explicitly. Cases 1, 2 and 4 run on both `POST /chat/stream` and `POST /chat`.
  2. An existing session ignores a sent level.
  3. A `PATCH` from another user returns 404 and writes nothing.
  4. A level change during a turn leaves that turn's spec unchanged.
  5. A WS reconnect's `STATE_DELTA` carries the stored level. The session GET and `GET /{session_id}/config` each return it with `effort_provenance`.
  6. After a successful `PATCH`, the session's active socket receives an event that carries the new level.
  7. In the PWA, a failed `PATCH` returns the selector to the stored level and shows the error.
  8. A migration test on the test Postgres: after the migration, an insert or update of `sessions.seshat_effort` to null, or to a value outside `fast`, `standard` and `deep`, is refused by the database.

  After the production migration, a query counts the `sessions` rows with no `seshat_effort`. Live: the owner changes the level in a session, and the next turn's `route_traces` row carries the new level. · *Fails if* any test fails, the count is above 0, or the live row after a change carries the old level.

- **AC-3 — The default level is qualified, and its live prompt is the qualified one.** · **Check, three parts:**
  1. The backend ticket holds the passing D7 scorer output, with Amendment 1's paired rows and its fingerprint, for Standard's shipped configuration. If Standard ships with `baseline`, the ticket also holds the failing `p2` output.
  2. A pin test renders Standard's D2 prompt from the committed `config/seshat_effort.yaml` and asserts that its SHA-256 equals the posted hash. A later change to Standard's values or to the planner text then fails CI until a new run is posted.
  3. Live: `planner_outcome` events with `seshat_effort = standard` and a non-null `planner_policy` (D2 turns). The live part needs at least 5 such events. If 30 days after the deploy hold fewer, master reports the count to the owner as "not decided", not as a pass.

  · *Fails if* the output is missing, a posted row fails while Standard ships with `p2`, the posted fingerprint differs from the deployed configuration, the pin test's hash differs from the posted hash, or any live event of part 3 carries another `planner_system_prompt_sha256`.

- **AC-4 — The answering model runs the level's mode, or `default`, and never a refused field.** · **Check:** tests for each of the nine deployments and each level, on both client paths: the ordinary primary path and the vision escalation through `get_llm_client_for_key`. Each asserts the requested mode: the level's mode when the deployment declares it, else `default_mode`. The catalog load refuses a `fast` or `deep` mode with a field outside its dialect. Live: `route_traces.primary_mode` against the answering deployment and the level. · *Fails if* any test case differs, the load accepts an invalid mode, or any live row records a mode other than the expected one.

- **AC-5 — Fast has no reasoning where its mode turns reasoning off.** · **Check, two parts:**
  1. *Every `fast` mode, by test.* For each deployment with a `fast` mode, a test builds the request through the dialect and asserts the reasoning field on the wire: `chat_template_kwargs.enable_thinking: false` on the five `llamacpp_qwen` deployments, `reasoning_effort: "none"` on `qwen3.8-27b-ovh`, and `effort: "low"` on `claude_sonnet`.
  2. *The response, live, on the default local binding.* `model_call_completed` events with `role = primary` and `mode = fast` on `qwen3.8-flash-next`: `reasoning_content_chars`. This part needs at least 5 calls from the owner's Fast turns. If 30 days after the deploy hold fewer, master reports the count to the owner as "not decided".

  *Stated limit.* The response proves thinking off only where the owner runs Fast. Part 1 decides the other deployments at the request. Their live calls are reported when they exist: `reasoning_content_chars` on a local deployment, and `reasoning_tokens` on OVH where the provider sends the field. Sonnet's Fast mode reasons at low effort by design (D5). · *Fails if* any part 1 test differs, any live Fast call on a `llamacpp_qwen` deployment records `reasoning_content_chars` above 0, any live Fast call on OVH records `reasoning_tokens` above 0, or part 2 is reported as a pass with fewer than 5 calls.

- **AC-6 — P2 appears only where D6 puts it, and the record names it.** · **Check:** unit tests against Standard's shipped `planner_policy`:
  - *Shipped `p2`:* Standard's D2 prompt equals its `baseline` prompt with the 913-character block and one `\n` inserted directly before the first `Rules:\n`.
  - *Shipped `baseline`:* Standard's D2 prompt is byte-identical to the prompt that the pre-change renderer gives at 12 rounds.
  - *Every level:* the block is absent without `admit_single`. Deep's D2 prompt is byte-identical to the prompt that the pre-change renderer gives at 20 rounds.

  Live: every `planner_outcome` event. · *Fails if* a character differs, the block appears in another place or on another level, or any live event has a `planner_policy` that disagrees with its prompt hash.

- **AC-7 — The ceilings hold.** · **Check:** tests. The gateway refuses to start when a level's `sub_agent_rounds` is above `sub_agent_max_tool_iterations`, or when `sub_agent_rounds_by_thoroughness` is set. With `expansion_enabled=false`, expansion permitted by governance and a positive expansion budget, every level records `expansion_disabled`, makes no planner call and starts no helper. · *Fails if* the gateway starts in either refused case, or a turn with `expansion_enabled=false` calls the planner or starts a helper.

- **AC-8 — Every turn carries its level.** · **Check:** over 14 days after the backend deploy, three queries:
  1. *The population.* Every `chat_stream.launched` and `chat.launched` event in the window. Together they cover each accepted request on both chat entry paths, written before the turn runs. Each event carries `seshat_effort` (D8).
  2. *The turn rows.* Each event of query 1, left-joined to `route_traces` on `trace_id`: the row's `seshat_effort`, and its `primary_mode` when its `model_role` is not null.
  3. *The planner events.* Every `planner_outcome` event in the window, left-joined to `route_traces` on `trace_id`.

  · *Fails if* an event of query 1 lacks `seshat_effort` or has no `route_traces` row, a joined row lacks a field of query 2 or carries a level other than its launch event's, or an event of query 3 has no row or a `seshat_effort` that differs from its row's.

---

## References

- FRE-1620 — this ADR's umbrella, with the owner's decisions of 2026-10-19 and the FRE-1618 handoff.
- FRE-1618 — the canceled ADR-0154 Amendment 3 draft, branch `fre-1618-adr-0154-amendment-3`.
- FRE-1619 — the helper round cap of 12 and the "as few rounds as possible" sentence.
- FRE-1621 — the analysis of the new GPT and Haiku models, their reasoning ranges and their prices.
- FRE-1615 — the P2 measurement: `docs/research/2026-10-18-fre-1615-block1-planner-arms.md`.
- [ADR-0154](ADR-0154-what-the-planner-sees-and-what-it-decides.md) — the planner, D7, AC-10 (Accepted, Amendments 1 and 2).
- [ADR-0121](ADR-0121-model-catalog-and-selection-layer.md) — the selection layer and §4's invariants (Implemented).
- [ADR-0079](ADR-0079-session-execution-profile-ownership.md) — the source of the eleven invariants (Implemented, subject superseded by ADR-0121).
- [ADR-0145](ADR-0145-two-nouns-and-the-dialect-between-them.md) — deployments, modes and dialects (Accepted).
- [ADR-0150](ADR-0150-the-worker-returns-data-not-prose.md) — D3, thoroughness levels and `sub_agent_rounds_by_thoroughness` (Accepted).

---

## Status Updates

### 2026-10-20 - Proposed
**Changed By:** `adr` seat at Opus 5.5 (FRE-1620)
**Reason:** Drafted on the owner's "Ok. Write it up" after the ADR discussion of 2026-10-20. The owner answered the five open points: P2 on Standard behind a D7 gate ("1 - yes"), Fast lowers the primary's reasoning to the bottom of each model's range, which is thinking off on the local models and low effort on Sonnet ("2- yes"), presets only ("Presets only"), and Standard for every new session ("5 - standard"). For point 3, the owner asked for a model refresh, filed by master as FRE-1621, and this ADR does not wait for it. Codex round 1 (11 blocking): an explicit stored level under ADR-0121 invariants 2 and 3, the D7 scope as the D2 prompt, Sonnet's Fast as low effort and not off, the vision escalation path, and criteria that a broken build cannot pass. Round 2 (5 blocking): a global cap unlike every level in AC-1, the schema constraints and the live notice in AC-2, Fast reasoning decided per mode in AC-5, and an independent turn population in AC-8. Round 3 (1 blocking): the level and the population cover both chat entry paths. The round budget is used at three.
