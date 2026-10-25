# FRE-1698 step 1 — inventory of the parameters that change a turn

- **Ticket:** FRE-1698 (the owner approved it on 2026-10-25). This document is step 1, the inventory. Step 2, the menu build, waits for the owner's choice (AC-2).
- **Seat:** cc-explore. Master commissioned the study on 2026-10-25.
- **Revision measured:** `main` at `4f815377`. F1 shows that the live gateway runs the same files.
- **Rules followed:** no gateway turn, no llama.cpp call, no paid call. The document gives environment key names only, never values.

## Answer first

1. A turn reads its parameters from **six homes**:
   - two per-session database columns;
   - one per-user database table of saved choices;
   - the Neo4j person node, which holds location consent;
   - four YAML files that are built into the gateway image;
   - one git-ignored budget file, also built into the image;
   - 361 `AppConfig` settings, which take their values from code defaults and a `.env` file.
2. **The PWA edits four parameters.** They are the session's primary model, the session's effort level, the location consent and the saved choices. The saved choices can be written only through the "Remember this choice" box on a decision card. The PWA shows the role bindings on `/observe`, read-only. Every other parameter is absent from the PWA.
3. **No part of the PWA shows a saved choice, and no part of the PWA clears one.** No endpoint reads the saved choices for display. The Observe page computes a role's model without them. This is the gap that hid the stale `artifact_builder` row (F4).
4. **The config files are built into the image, not mounted** (F2). A UI edit of a YAML or `.env` parameter cannot reach the running gateway without a new store, or without a rebuild or restart. The ticket names this risk: such an edit is a change to the configuration model.
5. **Some parameters change nothing.** 22 settings have no reader in `src/` (F7). One of them, `cloud_weekly_budget_usd`, is set in the live `.env`, and a comment in the budget file says to keep it in sync. Ten governance sections also have no reader (F8). The selection endpoint accepts a model for `artifact_builder` and `sub_agent`, but the turn uses only the `primary` selection (F9). If a menu control edits one of these parameters, the control shows a change that the turn ignores. That is the same failure as the trigger.

The full inventory is in [§ Inventory](#inventory). The full list of settings is in [Appendix A](#appendix-a--every-appconfig-setting). The two recommendations that the commission asks for, the grouping and the build order, are in [§ Proposals](#proposals).

## Findings

Each finding gives its verdict, the query as run and the actual output. A negative finding also carries the three arms of the admissibility rule.

### F1 — The live gateway runs `main` for every file that this study reads

**Verdict: POSITIVE.**

**Query:** `docker exec <gateway> sha256sum /app/<path>` compared with `sha256sum <path>` at `4f815377`, run on 2026-10-25.

**Output (first 12 hex characters, container = main for each):**

| File | Hash |
|---|---|
| `config/models.yaml` | `e3b260ed0c36` |
| `config/model_roles.yaml` | `c3a115f79bfe` |
| `config/seshat_effort.yaml` | `78b9d3b7ba6b` |
| `config/governance/tools.yaml` | `d5eff8939aa2` |
| `config/governance/modes.yaml` · `models.yaml` · `safety.yaml` · `permissions.yaml` | `3ea7ebff400e` · `07f98fb56c2c` · `5a7ada442d2f` · `8deed39d0812` |
| `src/personal_agent/config/settings.py` | `4a8a0edd3fc3` |
| `src/personal_agent/config/seshat_effort.py` | `e8d1837dcac3` |
| `src/personal_agent/gateway/session_api.py` | `75a710970917` |
| `src/personal_agent/orchestrator/constraint_options.py` | `564bd1b5fe1f` |
| `src/personal_agent/orchestrator/executor.py` | `4fc0a6aaa003` |
| `src/personal_agent/service/app.py` | `23cad0df4f60` |
| `config/governance/budget.yaml` (git-ignored), container = host file | `324e251f5525` |

An earlier comparison on the same day ran against `1fe2835a`. It showed a different `models.yaml`, because a deploy at 17:23Z came between the two reads. The table above is the second read. `main` then moved to `5365a5ef` (FRE-1697), which changes no file under `config/` or `src/personal_agent/config/`. Line citations in this document are at `4f815377`.

### F2 — The config files are inside the image, not mounted

**Verdict: POSITIVE.**

**Query:** `docker inspect <gateway> --format '{{range .Mounts}}…{{end}}'`.

**Output:** eight named volumes under `/app/agent_workspace` and `/app/telemetry/…`, plus `/var/run/docker.sock`. No mount covers `/app/config`. The container holds `/app/config/governance/budget.yaml`, and its hash equals the host file's hash (F1). The environment comes from the compose `env_file`. The container has no `/app/.env`, and the loader logged `no_env_files_found`.

**What this means for the menu:** a YAML parameter changes only after a commit and a rebuild. The budget file changes only after an edit and a rebuild. A `.env` parameter changes only after an edit and a container recreate. Only the database rows and the Neo4j consent change at run time.

### F3 — The PWA edits four parameters and shows one more group read-only

**Verdict: POSITIVE.**

**Query:** in `seshat-pwa/src` at `4f815377`, a search for `fetch(` and `method:`, then a read of each call site that sends a write.

**Output — every write call in the PWA:**

| PWA call | Endpoint | Parameter written | UI element |
|---|---|---|---|
| `setSessionSelection(sessionId, 'primary', key)` (`StreamingChat.tsx:112`) | `PATCH /api/v1/sessions/{id}/selection` | primary model, per session | `ModelPicker` |
| `setSessionEffort` (`useSeshatEffort.ts:119`) | `PATCH /api/v1/sessions/{id}/effort` | Seshat effort level, per session | `EffortSelector` |
| `updateLocationPreference` (`useLocation.ts:112,134`) | `PATCH /api/v1/preferences/location` | location consent and coordinates | `LocationConsent` |
| `onDecide(actionId, remember)` (`DecisionCard.tsx:80`) | WebSocket decision message | a saved choice, only when the box is ticked | `DecisionCard` |
| `submitTurnRating` | `POST /api/v1/turns/{id}/rating` | a rating (feedback, not a parameter) | `TurnRating` |

**Read-only display:** `/observe` (`ObserveView.tsx`) shows each role as open or pinned, with its resolved model and its provenance, and the provider availability table. `ModelPicker` shows each candidate's placement, context window and input cost. `EffortSelector` shows the catalog mode that each level requests. The PWA also keeps a theme choice in browser storage. The theme does not change a turn.

### F4 — No endpoint shows a saved choice, and the PWA cannot clear one

**Verdict: NEGATIVE** (no read path for display, no clear path).

**Query:** a search of `src/personal_agent` and `seshat-pwa/src` at `4f815377` for `ConstraintPreferencesRepository`, `user_constraint_preferences` and `preferences/constraint`.

**Output:**

- `orchestrator/executor.py:414` (`_load_constraint_preference`): the turn reads the row and applies it silently (`:1016`, `if pref and pref != "always_pause"`).
- `orchestrator/executor.py:454` (`_save_constraint_preference`): the "Remember" path writes the row.
- `service/app.py:2011` (`PUT /api/v1/preferences/constraint`): an API upsert. The PWA never calls it (`grep -rn "preferences/constraint" seshat-pwa/src` returned no match).
- The gateway package (`src/personal_agent/gateway/`) had no match. `get_session_config` (`session_api.py:609`) and `get_config` (`:723`) resolve each role through `_resolve_role_binding` (`:373`) from the YAML binding and the session selection only.
- The route list has no `GET` and no `DELETE` for `/api/v1/preferences/constraint`.

**Arms:**

- *Arm 1, identifier provenance:* the identifiers exist in the searched tree. The repository class has a turn-time reader at `executor.py:442` and a writer at `app.py:2070`. The search found both, so the pattern matches real call sites.
- *Arm 2, path liveness:* the same search in `src/personal_agent/gateway/` for `SessionModelSelectionRepository` returns `session_api.py:870`. The gateway package is searchable, and it does read a sibling store.
- *Arm 3, scope:* the claim is about the code at `4f815377` in `src/personal_agent` and `seshat-pwa/src`. It is not about a rendered page. No live page was loaded.

**Consequence:** by this code, the Observe page shows the YAML default for `artifact_builder` while a saved row overrides it on every build. That matches the trigger in the ticket. AC-4 asks for exactly the missing view.

### F5 — The live database rows for each parameter table

**Verdict:** mixed. The tables with rows give POSITIVE values. The two empty tables give a NEGATIVE, with arms below.

**Query** (read-only, `cloud-sim-postgres`, database `personal_agent`, about 19:50Z on 2026-10-25):

```sql
select constraint_name, preferred_action, updated_at from user_constraint_preferences;
select time_window, role, provider, user_id is null as global, cap_usd, updated_at from budget_policies order by time_window, role;
select model_key, level, demoted_at, updated_at from grounding_enforcement_state;
select role, deployment_key, count(*) from session_model_selections group by 1,2 order by 3 desc;
select seshat_effort, execution_profile, mode, count(*) from sessions group by 1,2,3 order by 4 desc;
```

**Output:**

- `user_constraint_preferences`: **0 rows.** Master deleted the stale row at 13:29Z.
- `budget_policies`: **14 rows**, all global, all with `updated_at = 2026-10-25 17:24:20Z`. The values match the live budget file one for one: daily caps of 5.00 for `artifact_builder`, `captains_log`, `entity_extraction`, `span_extraction` and `study`, 10.00 for `main_inference`, 0.10 for `skill_routing`; weekly caps of 18.00 `artifact_builder`, 7.00 `entity_extraction`, 25.00 `main_inference`, 0.50 `skill_routing`, 7.00 `span_extraction`, 7.00 `study`, 30.00 `_total`. The gateway container started at 17:23:54Z. `app.py:962` mirrors the file into the table at start (FRE-1209). The table is therefore a copy, and the next start overwrites a direct edit.
- `grounding_enforcement_state`: **0 rows.**
- `session_model_selections`: **276 rows, all with `role = primary`**, over 14 deployment keys. The largest are `claude_sonnet` 110, `qwen3.8-flash-next` 97 and `qwen3.8-27b-ovh` 27.
- `sessions`: 1,516 rows. `seshat_effort`: `standard` 1,513, `deep` 2, `fast` 1. `execution_profile`: `local` 1,431, `cloud` 85. `mode`: `NORMAL` for all.

**Arms for the two empty tables:**

- *Arm 1:* each table exists in this database. The query returned its column header, not an error. The schema is `docker/postgres/init.sql:502` and `:371`.
- *Arm 2:* `budget_policies`, in the same database, in the same `psql` call, with the same no-predicate shape, returned 14 rows.
- *Arm 3:* the queries have no predicate. The verdict covers the whole table at the moment of the query, and no other time.

### F6 — Where the 361 settings take their live values

**Verdict: POSITIVE.**

**Query:**

1. An AST read of `AppConfig` in `settings.py`, which gives 361 annotated fields.
2. `docker exec <gateway> env | cut -d= -f1`, names only, which gives 98 `AGENT_`/`LLM_` keys.
3. In the container, for each non-secret overridden field, `settings.<field> == AppConfig.model_fields[<field>].get_default()`. This printed only `equals_default` or `differs`.

**Output:**

- 93 of the 98 live keys map to a settings field. The five that do not are `AGENT_HOST`, `AGENT_OWNER_EMAIL` (read through an alias), `AGENT_OWNER_NTFY_URL`, `AGENT_SESHAT_CHANNEL_SECRET` and `AGENT_SLM_TUNNEL_BASE_URL`.
- 62 non-secret overrides were compared: **33 differ** from the code default, **27 equal** it, and 2 have no field of that name.
- Of the 166 settings in the turn groups, 22 are set in `.env` (17 differ, 5 equal), and 144 run on the code default.
- The host `.env` and the container hold the same `AGENT_`/`LLM_` names. The container also has eight keys that compose sets: `AGENT_ARTIFACTS_EGRESS_BASE_URL`, `AGENT_BUILD_FINGERPRINT`, `AGENT_DATABASE_ADMIN_URL`, `AGENT_DEPLOYMENT_PROFILE`, `AGENT_ELASTICSEARCH_URL`, `AGENT_OTEL_EXPORTER_ENDPOINT`, `AGENT_SEARXNG_BASE_URL` and `AGENT_TEMPO_URL`.

### F7 — 22 settings have no reader in `src/`

**Verdict: NEGATIVE.**

**Query:** for each of the 361 field names, a whole-word search over every `.py` file under `src/personal_agent` except `config/settings.py`, at `4f815377`. Then a separate check of the dynamic reads: `getattr(settings, …)` with a literal name (the word search finds it), `getattr(settings, provider.auth_env)` (`constraint_options.py:196`, `provider_health.py:62`), and the `setting:<field>` references in `config/substrate.yaml`. The fields that these dynamic reads reach were removed from the result.

**Output — the fields with no reader:**

| Group | Fields |
|---|---|
| routing and planner | `orchestrator_max_concurrent_tasks`, `routing_policy`, `router_role`, `enable_reasoning_role`, `routing_heuristic_threshold`, `router_timeout_seconds`, `synthesis_timeout_seconds` |
| context and history | `context_budget_comfortable_tokens`, `context_budget_generation_reserve_tokens`, `context_compression_enabled`, `context_compression_threshold_ratio`, `within_session_compression_refire_after_messages`, `tool_result_digest_keep` |
| tools and governance | `orchestrator_max_repeated_tool_calls`, `url_guard_enabled` |
| budgets and cost | `cloud_weekly_budget_usd` — **set in the live `.env`, and it differs from its default** |
| memory and recall | `embedding_batch_size` |
| background jobs | `brainstem_sensor_poll_interval_seconds`, `request_monitoring_interval_seconds`, `request_monitoring_include_gpu`, `feedback_defer_revisit_days` |
| infrastructure | `use_service_mode` |

**Arms:**

- *Arm 1:* each name is a real field. Examples: `settings.py:366` `orchestrator_max_repeated_tool_calls`, `:388` `routing_policy`, `:1253`–`:1257` the two `context_compression_*` fields, `:1706` `cloud_weekly_budget_usd`, `:2859` `url_guard_enabled`.
- *Arm 2:* the same search finds real readers for other fields. `orchestrator_max_tool_iterations` has 3 reading files, `conversation_max_history_messages` has 2 and `expansion_enabled` has 2.
- *Arm 3:* the scope is `src/personal_agent` at `4f815377`, which is what the gateway runs (F1). Scripts and tests are outside the scope because a turn does not run them. A word search can count a local variable of the same name as a reader, so a non-zero count is an upper bound. A zero is exact for literal reads.

`cloud_weekly_budget_usd` needs care. The live budget file says "Keep in sync with AGENT_CLOUD_WEEKLY_BUDGET_USD (.env)" beside the 30.00 `_total` weekly cap. The cap that the cost gate enforces comes from `budget_policies` (F5). The setting changes nothing.

### F8 — Ten governance sections have no reader

**Verdict: NEGATIVE.**

**Query:** for each section and key name, a whole-word search over `src/personal_agent/**/*.py` at `4f815377`, without `governance/models.py` (the schema) and `config/governance_loader.py` (the loader).

**Output (reading files outside the schema and the loader):**

| Section or key | File | Readers |
|---|---|---|
| `mode_constraints` (per-mode `max_tokens`, `temperature`, `timeout_seconds`) | `governance/models.yaml` | 0 |
| `rate_limits` | `governance/safety.yaml` | 0 |
| `content_filtering` | `governance/safety.yaml` | 0 |
| `outbound_gateway` | `governance/safety.yaml` | 0 |
| `human_approval` | `governance/safety.yaml` | 0 |
| `max_concurrent_tasks` | `governance/modes.yaml` | 0 |
| `require_approval_for` | `governance/modes.yaml` | 0 |
| `allowed_tool_categories` | `governance/modes.yaml` | 0 |
| `mode_policies` | `governance/tools.yaml` | 0 |
| `tool_categories` | `governance/tools.yaml` | 0 |

**Arms:**

- *Arm 1:* each name is a key in the committed YAML and a field in `governance/models.py`. The loader reads all four files (`governance_loader.py:85`–`:94`).
- *Arm 2:* the same search returns `sub_agent_tools` 10, `transition_rules` 1, `allowed_in_modes` 7, `risk_level` 46 and `rate_limit_per_hour` 27.
- *Arm 3:* the scope is the deployed `src/personal_agent`. The loader validates these sections at start, so a broken value can still stop the gateway. The claim is only that no turn reads their values.

### F9 — The selection endpoint accepts three roles, and the turn uses one

**Verdict:** POSITIVE for the code path. NEGATIVE for live rows, with arms below.

**Query:** a read of `session_api.py` (`update_session_selection`, which calls `is_selectable_binding`) and of the `set_current_selection(` call sites. Then the F5 query on `session_model_selections`.

**Output:**

- `model_roles.yaml` marks three roles `open: true`: `primary`, `artifact_builder` and `sub_agent`. The PATCH accepts any open role.
- The turn launcher puts only `primary` into the per-turn selection: `app.py:411` `set_current_selection({"primary": primary_selection_key})` and `app.py:2473` `set_current_selection({"primary": resolved_selection})`. The builder model comes from the decision card, the saved choice or the YAML default (`constraint_options.py:435`–`:470`). The `sub_agent` role inherits the primary model.
- Live: 0 rows with `role <> 'primary'`. The PWA sends only `'primary'` (`StreamingChat.tsx:112`).

**Arms for the live zero:**

- *Arm 1:* the `role` column exists and holds `primary` in 276 rows.
- *Arm 2:* the same `group by` query returned 14 non-zero groups.
- *Arm 3:* the whole table, at the moment of the query.

**Consequence:** today nothing goes wrong. If a menu offered a model per open role, an `artifact_builder` or `sub_agent` selection would be stored and would show on `/observe` as `provenance: selection`. The turn would still not use it.

## Inventory

### How to read the table

- **Home** is where the value is stored.
- **Value source now** names the source that wins for the next turn, then the sources that it overrides, in order.
- **Who changes it today** uses these short forms:
  - *Owner (PWA)*: the owner, in the PWA.
  - *Owner (API)*: the owner, through an authenticated API call. No PWA control exists.
  - *Master (SQL)*: master, with a statement in the database.
  - *Master (.env)*: master edits `.env` and recreates the gateway container.
  - *PR + rebuild*: a build seat changes a committed file in a PR, then master merges, rebuilds and deploys.
  - *Master (file + rebuild)*: master edits the git-ignored budget file and rebuilds the image.
- **PWA**: *editable*, *shown* (read-only), or *absent*.
- The groups are the proposed menu groups (P1).

A "turn" here means one chat turn on the live gateway. Parameters that only steer background jobs or connections are listed in Appendix A, not here.

### Group 1 — This conversation

| Parameter | Home | Value source now (wins → overridden) | Who changes it today | PWA |
|---|---|---|---|---|
| Primary model | `session_model_selections` row (`role = 'primary'`) | stored row → `primary_selection` in the first request of a new session → `bindings.primary` in `config/model_roles.yaml` | Owner (PWA): `ModelPicker` → `PATCH …/selection` | **editable** |
| Seshat effort level (`fast` · `standard` · `deep`) | `sessions.seshat_effort` | stored column → `seshat_effort` in the first request of a new session → `default_level` in `config/seshat_effort.yaml` | Owner (PWA): `EffortSelector` → `PATCH …/effort` | **editable** |
| Model for `artifact_builder` or `sub_agent` in this session | `session_model_selections` (other roles) | the endpoint accepts it, and the turn ignores it (F9) | Owner (API) | absent |

### Group 2 — Saved choices

A saved choice skips the decision card and applies its action silently (`executor.py:1016`). The value `always_pause` keeps the card. Each card has a safe default, the last option, which applies on timeout or disconnect.

| Parameter (`constraint_name`) | Options (safe default last) | Home | Value source now | Who changes it today | PWA |
|---|---|---|---|---|---|
| `tool_iteration_limit` | `continue_10` · `finish_now` | `user_constraint_preferences` (per user) | saved row → card answer → timeout default. Live: no row (F5) | Owner (PWA) by "Remember" on a card, write-only · Owner (API) `PUT /api/v1/preferences/constraint` · clear: Master (SQL) only | write-only (not shown, no clear) |
| `context_compression` | `compress_continue` · `stop_here` | same | same | same | same |
| `attachment_cost` | `proceed_cloud` · `keep_local` | same | same | same | same |
| `sub_agent_fanout_incomplete` | `answer_from_partial` · `stop_and_show` | same | same | same | same |
| `spend_threshold` | `continue_turn` · `answer_now` | same | same | same | same |
| `artifact_builder` | any open `kind: llm` catalog key with an available provider (computed) | same | saved row → card answer → `bindings.artifact_builder` (`gpt-6.1-sol`) | same | same — **this row was the trigger** |
| Card wait time | `constraint_pause_timeout_seconds` | settings | code default `180.0` | Master (.env) | absent |
| Location consent | Neo4j person node | stored consent; the operator flag `location_enabled` gates it (live: override, differs from default) | Owner (PWA): `LocationConsent` → `PATCH /api/v1/preferences/location` | **editable** |
| Location precision | `location_precision` | code default `"precise"` | Master (.env) | absent |

### Group 3 — Models per role

Home: `bindings:` in `config/model_roles.yaml`, built into the image (F2). Live value = committed value (F1). Who changes it: PR + rebuild. PWA: **shown** on `/observe` (open or pinned, resolved model, provenance). Not editable.

| Role | Binding now | Open |
|---|---|---|
| `primary` | `qwen3.8-flash-next` | yes (Group 1 overrides it per session) |
| `artifact_builder` | `gpt-6.1-sol` | yes (a saved choice overrides it, Group 2) |
| `sub_agent` | `inherit` (the session's primary), with `mode: worker`, `max_tokens: 8192`, `default_timeout: 600`, `priority: ELEVATED` | yes |
| `entity_extraction` | `gpt-5.4-mini` | no |
| `captains_log` | `gpt-6.1-sol` | no |
| `session_summary` | `claude_sonnet` | no |
| `insights` | `claude_sonnet` | no |
| `compressor` | `gpt-5.4-mini` | no |
| `span_extraction` | `claude_sonnet` | no |
| `entailment` | `claude_sonnet` | no |
| `vision` | `claude_sonnet` | no |
| `embedding` | `embedding` | no |
| `reranker` | `reranker` | no |
| `reranker_fallback` | `reranker_fallback` | no |

### Group 4 — Effort presets and model modes

| Parameter | Home | Value now | Who changes it today | PWA |
|---|---|---|---|---|
| `default_level` | `config/seshat_effort.yaml` | `standard` | PR + rebuild | absent |
| `fast`: `expansion` · `sub_agent_rounds` · `planner_policy` · `primary_mode` | same | `false` · `null` · `null` · `fast` | PR + rebuild | `primary_mode` **shown** as the level's mode label; the rest absent |
| `standard`: same four keys | same | `true` · `12` · `p2` · `null` (the deployment's `default_mode`) | PR + rebuild | same |
| `deep`: same four keys | same | `true` · `20` · `baseline` · `deep` | PR + rebuild | same |
| Per deployment (18 deployments, 7 providers): `context_length`, `max_tokens`, `default_timeout`, `max_timeout`, `min_concurrency`, `max_concurrency`, `default_mode`, `tool_calling_strategy`, `supports_function_calling`, `supports_vision`, `supports_pdf_document`, `input_cost_per_token`, `output_cost_per_token`, `reasoning_effort_domain`, `dialect`, `endpoint` | `models:` in `config/models.yaml` | committed values | PR + rebuild | `context_length`, input cost and placement **shown** in `ModelPicker`; the rest absent |
| Per mode of each deployment (`default` 15, `worker` 15, `fast` 13, `deep` 2, `planner` 2): `temperature`, `top_p`, `top_k`, `min_p`, `presence_penalty`, `repeat_penalty`, `reasoning_effort`, `enable_thinking`, `effort` | `models.<key>.modes` in `config/models.yaml` | committed values. The level's `primary_mode` wins if the deployment declares that mode, otherwise `default_mode` | PR + rebuild | absent |
| `llm_no_think_suffix`, `llm_append_no_think_to_tool_prompts` | settings | code defaults | Master (.env) | absent |

### Group 5 — Budgets and cost

| Parameter | Home | Value source now | Who changes it today | PWA |
|---|---|---|---|---|
| 14 spend caps (7 daily, 7 weekly; values in F5) | `caps:` in `config/governance/budget.yaml` (git-ignored, built into the image) → copied into `budget_policies` at every start | the file. A direct table edit lasts only until the next start | Master (file + rebuild) | absent (a `BudgetDeniedCard` appears after a denial) |
| Per role (10 roles): `default_output_tokens`, `safety_factor`, `on_denial` | `roles:` in the same file | the file | Master (file + rebuild) | absent |
| `uncapped_roles` (`insights`, `promotion`, `freshness`) | same | the file | Master (file + rebuild) | absent |
| `attachment_cost_confirmation_threshold_usd` | settings | code default `0.50` | Master (.env) | absent |
| `orchestrator_spend_threshold` (tool iterations before the spend card) | settings | code default `6` | Master (.env) | absent |
| `brave_search_cost_per_call_usd`, `exa_search_cost_per_call_usd`, `eur_usd_rate` | settings | code defaults | Master (.env) | absent |
| `cloud_weekly_budget_usd` | settings | `.env` override, differs from default — **no reader** (F7) | Master (.env), with no effect | absent |

### Groups 6 to 9 — Settings that steer a turn

These parameters are `AppConfig` settings. For each one, the home is the environment key `AGENT_<NAME>`. The value source is the `.env` override when one exists, otherwise the code default. The change path is Master (.env) for an override, or PR + rebuild for a default. None of these parameters is in the PWA. Appendix A lists each one with its default, its live source and its reader count.

| Proposed group | Settings | Set in live `.env` | No reader (F7) |
|---|---|---|---|
| 6 — How a turn runs: routing, planner, workers | 19 (`expansion_*`, `delegation_enabled`, `planner_*`, `worker_queue_absorption_seconds`, `sub_agent_*`, `routing_*`/`router_*`) | 1 (`expansion_enabled`, equals default) | 7 |
| 6 — How a turn runs: context and history | 28 (`conversation_*`, `context_window_max_tokens`, `context_budget_*`, `within_session_*`, `tool_result_*`, `cache_*`) | 1 (`conversation_max_history_messages`, equals default) | 6 |
| 6 — How a turn runs: timeouts and limits | 10 (`llm_timeout_seconds`, `llm_max_retries`, `orchestrator_task_timeout_seconds`, `orchestrator_turn_lifetime_seconds`, `orchestrator_creditable_pause_limit`, `service_request_*`, `session_write_wait_timeout_seconds`, `route_trace_*`) | 2 (both differ) | 0 |
| 7 — Memory and recall | 46 (`reranker_*`, `broad_recall_*`, `entity_match_*`, `recall_*`, `structural_*`, `multipath_*`, `lexical_arm_enabled`, `multiquery_arm_enabled`, `proactive_memory_*`, `freshness_tier_*`, `freshness_enabled`, `freshness_relevance_weight`, `enable_memory_graph`, `embedding_*`, `dedup_similarity_threshold`) | 9 (8 differ, 1 equals) | 1 |
| 8 — Tools and governance | 32 (`orchestrator_max_tool_iterations`, `orchestrator_max_repeated_tool_calls`, `mcp_gateway_*`, `searxng_*`, `perplexity_timeout_seconds`, `primitive_tools_enabled`, `url_guard_*`, `approval_*`, `upload_*`, `artifact_*`, `attachment_*`, `document_*`, `linear_agent_rate_limit_per_day`) | 3 (2 differ, 1 equals) | 2 |
| 9 — Grounding and quality | 18 (`grounding_*`, `context_quality_governance_*`, `graph_quality_governance_enabled`) | 2 (1 differs, 1 equals) | 0 |
| — Identity | 2 (`owner_name`, `user_display_names_json`) | 2 (both differ) | 0 |

### Group 8, continued — Tool governance files

| Parameter | Home | Value source now | Who changes it today | PWA |
|---|---|---|---|---|
| Per tool (99 tools): `category`, `allowed_in_modes`, `forbidden_in_modes`, `risk_level`, `timeout_seconds`, `rate_limit_per_hour`, `allowed_paths`, `forbidden_paths`, `unattended_paths`, `loop_max_per_signature`, `loop_max_consecutive`, `loop_consecutive_terminal`, `loop_output_sensitive`, `requires_sandbox`, `max_file_size_mb` | `tools:` in `config/governance/tools.yaml` | committed values | PR + rebuild | absent |
| Worker tool sets | `sub_agent_tools:` in the same file | committed values | PR + rebuild | absent |
| Permission rules: `deny` 8, `ask` 8, `allow` 64, `curl_read` 7 | `config/governance/permissions.yaml` (the eval stack adds `permissions.eval.yaml`) | committed values. Order: deny, then ask, then allow. No match means ask | PR + rebuild | absent. An `ask` appears as an `ApprovalModal` when `approval_ui_enabled` is on |
| Mode transitions: `transition_rules` | `config/governance/modes.yaml` | committed values | PR + rebuild | absent |
| `mode_constraints`, `rate_limits`, `content_filtering`, `outbound_gateway`, `human_approval`, `max_concurrent_tasks`, `require_approval_for`, `allowed_tool_categories`, `mode_policies`, `tool_categories` | the four governance files | **no reader** (F8) | PR + rebuild, with no effect | absent |

### Group 9, continued — Grounding state

| Parameter | Home | Value source now | Who changes it today | PWA |
|---|---|---|---|---|
| Grounding enforcement level per model | `grounding_enforcement_state` | written by the compliance loop (`executor.py:2292`). Live: no row (F5) | the system. Master (SQL) by hand | absent |

### Not in the menu scope

- **Background jobs** (111 settings): consolidation, captain's log, insights, promotion, feedback, freshness review, session summaries, monitors and probes. They change memory and records that later turns read. They do not steer the current turn. Listed in Appendix A.
- **Infrastructure** (73 settings) and **secrets** (11 settings): connection strings, ports, origins, storage and keys. A menu can show a secret as set or not set, never its value. Listed in Appendix A without defaults.
- **Prompts and skills:** the system prompt text and the 11 skills under `skills/` change a turn, but they are content, not parameters. They change by PR + rebuild.
- **`config/substrate.yaml`, `config/deployment.yaml`, `config/gateway_access.yaml`, `config/calibration/*.json`:** deployment wiring, access tokens and calibration results.

## Proposals

The commission asks for one grouping and one build order. The owner chooses. P3 is a precondition that the inventory exposed.

### P1 — Grouping: nine menu groups and one read-only System page

Organize the menu in the groups of § Inventory, in this order:

1. **This conversation:** primary model, effort level.
2. **Saved choices:** the six decision-card choices, the card wait time, location consent and precision.
3. **Models per role:** the 14 role bindings.
4. **Effort presets and model modes:** the three levels and the per-deployment mode values.
5. **Budgets and cost:** the 14 caps, the per-role estimates and the cost thresholds.
6. **How a turn runs:** routing, planner and workers; context and history; timeouts and limits.
7. **Memory and recall.**
8. **Tools and governance:** per-tool rules, permission rules and the tool settings.
9. **Grounding and quality.**

Add a tenth page, **System**, read-only. It holds background jobs, infrastructure, and the secrets as set or not set.

Every row shows four things:
- the live value;
- the value it overrides, with that value's home;
- the source that wins;
- whether the row is editable.

The order follows how close each group is to what the owner does in a turn. Groups 1 and 2 hold every parameter that the owner sets today. Group 2 holds the row that caused FRE-1698.

### P2 — Build order: show everything first, then edit what lives in the database, then decide on the config files

1. **Stage 1, read-only menu (no ADR).**
   - Add one read endpoint that returns every parameter in P1 with its effective value and its source. A secret returns only "set" or "not set".
   - Add `GET` and `DELETE` for `/api/v1/preferences/constraint`.
   - Show each saved choice beside the default that it overrides, with a Clear button.

   This stage meets AC-1's need in the UI and AC-4 in full. It closes the failure that started the ticket. Every value is read from the running gateway, as F1 and F6 do.
2. **Stage 2, edits for parameters that already live in the database.** Saved choices get set and clear. Model and effort per session already have controls. These edits need no change to the configuration model.
3. **Stage 3, an ADR before any config-file edit.** The YAML files, the budget file and `.env` are built into the image or set at container start (F2). A UI edit can reach them only through one of two designs:
   - a database layer of overrides that the turn reads at launch, with the source shown;
   - a UI flow that commits the file and triggers a rebuild.

   The ADR chooses one. After it, add edits in this order:
   1. role bindings (Group 3);
   2. effort presets (Group 4);
   3. budget caps (Group 5), which already have a database copy;
   4. Groups 6 to 9.

FRE-1701 (P3) goes before Stage 3. It can run in parallel with Stages 1 and 2.

### P3 — Remove or wire the parameters whose change has no effect, before they get a control

22 settings (F7), 10 governance sections (F8) and the non-`primary` session selections (F9) can be changed, and the turn ignores the change. A menu control on any of them repeats the FRE-1698 failure. Each one needs one action: delete it, wire it, or refuse the write. Filed as **FRE-1701** (Backlog).

## Filed tickets

- **FRE-1701** — Remove or wire the parameters whose change has no effect, before the settings menu shows them (Backlog, related to FRE-1698).

## Appendix — method

**What was measured, and where:**

- **Code and committed config:** `main` at `4f815377`, read in a detached scratch worktree. The deployed files match by SHA-256 (F1).
- **Live gateway container:** `cloud-sim-seshat-gateway`. Measurements: mounts (F2), environment key names only (F6), file hashes (F1), and a comparison of each non-secret overridden setting with its code default. That comparison printed only `equals_default` or `differs`, never a value. It ran with the app's own interpreter (`/app/.venv/bin/python`) and with standard error discarded. A settings validation error prints a tail of setting values, and the discard stops that print.
- **Live Postgres:** `cloud-sim-postgres`, database `personal_agent`. Five read-only `SELECT`s, about 19:50Z on 2026-10-25 (F5).
- **Host `.env`:** key names only, compared with the container's key names. No value was printed or stored.

**Not measured:**
- No gateway turn and no PWA page load. The PWA statements (F3, F4) come from its source at `4f815377`. They describe what the code renders, not an observed screen.
- No llama.cpp call and no paid call.

**Identifier resolutions:**

- **`AGENT_<NAME>` → field.** Pydantic `env_prefix="AGENT_"` (`settings.py:53`), plus 26 explicit aliases. Examples of aliases: `owner_name` ← `AGENT_OWNER_NAME`, `agent_owner_email` ← `AGENT_OWNER_EMAIL`, `log_level` ← `APP_LOG_LEVEL`.
- **Reader counts.** A whole-word search per field name over `src/personal_agent/**/*.py`, without `settings.py`. The search does not see three kinds of dynamic read, so these fields were checked by hand and removed from the no-reader list:
  - `getattr(settings, provider.auth_env)`, which covers the provider key fields named in `models.yaml`, for example `scaleway_secret_key`;
  - the `setting:<field>` references in `config/substrate.yaml`, which cover the `managed_*` endpoints;
  - fields that `settings.py` reads in its own validators, for example `database_admin_url` and `owner_storage_allowlist`.
- **Grouping.** A prefix rule list in a scratch script assigns each field to a group, with two corrections by hand: `freshness_enabled` and `freshness_relevance_weight` moved from background jobs to memory and recall, because `memory/service.py` reads them at recall time.

**Rejected:**

- **Live values from `.env`.** The commission forbids them. The comparison with the default gives the source without the value.
- **A first read of the catalog against `1fe2835a`.** A deploy at 17:23Z moved the container to newer code. All hashes in F1 are from the second read, against `4f815377`.
- **A first count of "39 differ, 21 equal".** I gave that number in a progress note, and it was a misreading. The file holds 33 and 27, and F6 uses those numbers.

## Appendix A — every `AppConfig` setting

Generated from the AST of `settings.py` at `4f815377` and the live comparison in F6. The **Live value source** column shows where the value comes from:
- *code default*: no `.env` key;
- *.env override, differs from default* or *.env override, equals default*: the result of the boolean comparison;
- *.env override (value not compared)*: a secret or infrastructure key, which was not compared.

**Files that read it** counts the files under `src/personal_agent`, without `settings.py`. *dynamic* marks a field that is read by name at run time (see the method appendix). Infrastructure and secret rows show no default.

| Setting | Group | Environment key | Code default | Live value source | Files that read it |
|---|---|---|---|---|---|
| `llm_append_no_think_to_tool_prompts` | modes and effort | `AGENT_LLM_APPEND_NO_THINK_TO_TOOL_PROMPTS` | `False` | code default | 1 |
| `llm_no_think_suffix` | modes and effort | `AGENT_LLM_NO_THINK_SUFFIX` | `"/no_think"` | code default | 1 |
| `constraint_pause_timeout_seconds` | saved choices | `AGENT_CONSTRAINT_PAUSE_TIMEOUT_SECONDS` | `180.0` | code default | 1 |
| `location_enabled` | saved choices | `AGENT_LOCATION_ENABLED` | `False` | .env override, differs from default | 3 |
| `location_precision` | saved choices | `AGENT_LOCATION_PRECISION` | `"precise"` | code default | 2 |
| `attachment_cost_confirmation_threshold_usd` | budgets and cost | `AGENT_ATTACHMENT_COST_CONFIRMATION_THRESHOLD_USD` | `0.50` | code default | 1 |
| `brave_search_cost_per_call_usd` | budgets and cost | `AGENT_BRAVE_SEARCH_COST_PER_CALL_USD` | `0.005` | code default | 1 |
| `cloud_weekly_budget_usd` | budgets and cost | `AGENT_CLOUD_WEEKLY_BUDGET_USD` | `5.0` | .env override, differs from default | **none in `src/`** |
| `eur_usd_rate` | budgets and cost | `AGENT_EUR_USD_RATE` | `1.14` | code default | 2 |
| `exa_search_cost_per_call_usd` | budgets and cost | `AGENT_EXA_SEARCH_COST_PER_CALL_USD` | `0.007` | code default | 1 |
| `orchestrator_spend_threshold` | budgets and cost | `AGENT_ORCHESTRATOR_SPEND_THRESHOLD` | `6` | code default | 1 |
| `delegation_enabled` | routing and planner | `AGENT_DELEGATION_ENABLED` | `False` | code default | 2 |
| `enable_reasoning_role` | routing and planner | `AGENT_ENABLE_REASONING_ROLE` | `True` | code default | **none in `src/`** |
| `expansion_budget_max` | routing and planner | `AGENT_EXPANSION_BUDGET_MAX` | `3` | code default | 2 |
| `expansion_enabled` | routing and planner | `AGENT_EXPANSION_ENABLED` | `True` | .env override, equals default | 2 |
| `orchestrator_max_concurrent_tasks` | routing and planner | `AGENT_ORCHESTRATOR_MAX_CONCURRENT_TASKS` | `5` | code default | **none in `src/`** |
| `planner_history_max_chars` | routing and planner | `AGENT_PLANNER_HISTORY_MAX_CHARS` | `60000` | code default | 2 |
| `planner_input_max_chars` | routing and planner | `AGENT_PLANNER_INPUT_MAX_CHARS` | `64000` | code default | 2 |
| `planner_timeout_seconds` | routing and planner | `AGENT_PLANNER_TIMEOUT_SECONDS` | `600.0` | code default | 1 |
| `router_role` | routing and planner | `AGENT_ROUTER_ROLE` | `"ROUTER"` | code default | **none in `src/`** |
| `router_timeout_seconds` | routing and planner | `AGENT_ROUTER_TIMEOUT_SECONDS` | `6.0` | code default | **none in `src/`** |
| `routing_heuristic_threshold` | routing and planner | `AGENT_ROUTING_HEURISTIC_THRESHOLD` | `0.85` | code default | **none in `src/`** |
| `routing_policy` | routing and planner | `AGENT_ROUTING_POLICY` | `"heuristic_then_llm"` | code default | **none in `src/`** |
| `sub_agent_context_reserve_tokens` | routing and planner | `AGENT_SUB_AGENT_CONTEXT_RESERVE_TOKENS` | `16000` | code default | 2 |
| `sub_agent_landing_max_tokens` | routing and planner | `AGENT_SUB_AGENT_LANDING_MAX_TOKENS` | `4096` | code default | 1 |
| `sub_agent_max_tool_iterations` | routing and planner | `AGENT_SUB_AGENT_MAX_TOOL_ITERATIONS` | `20` | code default | 4 |
| `sub_agent_researcher_min_search_rounds` | routing and planner | `AGENT_SUB_AGENT_RESEARCHER_MIN_SEARCH_ROUNDS` | `0` | code default | 1 |
| `sub_agent_rounds_by_thoroughness` | routing and planner | `AGENT_SUB_AGENT_ROUNDS_BY_THOROUGHNESS` | `factory:dict` | code default | 2 |
| `synthesis_timeout_seconds` | routing and planner | `AGENT_SYNTHESIS_TIMEOUT_SECONDS` | `25.0` | code default | **none in `src/`** |
| `worker_queue_absorption_seconds` | routing and planner | `AGENT_WORKER_QUEUE_ABSORPTION_SECONDS` | `25.0` | code default | 2 |
| `cache_frozen_accum_max_ratio` | context and history | `AGENT_CACHE_FROZEN_ACCUM_MAX_RATIO` | `0.50` | code default | 1 |
| `cache_quality_token_weight` | context and history | `AGENT_CACHE_QUALITY_TOKEN_WEIGHT` | `4000.0` | code default | 1 |
| `cache_reset_min_run_turns_cloud` | context and history | `AGENT_CACHE_RESET_MIN_RUN_TURNS_CLOUD` | `4` | code default | 1 |
| `cache_reset_min_run_turns_local` | context and history | `AGENT_CACHE_RESET_MIN_RUN_TURNS_LOCAL` | `12` | code default | 1 |
| `context_budget_comfortable_tokens` | context and history | `AGENT_CONTEXT_BUDGET_COMFORTABLE_TOKENS` | `64000` | code default | **none in `src/`** |
| `context_budget_generation_reserve_tokens` | context and history | `AGENT_CONTEXT_BUDGET_GENERATION_RESERVE_TOKENS` | `32768` | code default | **none in `src/`** |
| `context_budget_max_tokens` | context and history | `AGENT_CONTEXT_BUDGET_MAX_TOKENS` | `98304` | code default | 1 |
| `context_compression_enabled` | context and history | `AGENT_CONTEXT_COMPRESSION_ENABLED` | `True` | code default | **none in `src/`** |
| `context_compression_threshold_ratio` | context and history | `AGENT_CONTEXT_COMPRESSION_THRESHOLD_RATIO` | `0.65` | code default | **none in `src/`** |
| `context_window_max_tokens` | context and history | `AGENT_CONTEXT_WINDOW_MAX_TOKENS` | `48000` | code default | 3 |
| `conversation_context_strategy` | context and history | `AGENT_CONVERSATION_CONTEXT_STRATEGY` | `"truncate"` | code default | 1 |
| `conversation_max_history_messages` | context and history | `AGENT_CONVERSATION_MAX_HISTORY_MESSAGES` | `20` | .env override, equals default | 2 |
| `tool_result_compression_enabled` | context and history | `AGENT_TOOL_RESULT_COMPRESSION_ENABLED` | `False` | code default | 3 |
| `tool_result_digest_exclude_tools` | context and history | `AGENT_TOOL_RESULT_DIGEST_EXCLUDE_TOOLS` | `factory:list` | code default | 1 |
| `tool_result_digest_head_lines` | context and history | `AGENT_TOOL_RESULT_DIGEST_HEAD_LINES` | `40` | code default | 1 |
| `tool_result_digest_keep` | context and history | `AGENT_TOOL_RESULT_DIGEST_KEEP` | `3` | code default | **none in `src/`** |
| `tool_result_digest_max_expand_tokens` | context and history | `AGENT_TOOL_RESULT_DIGEST_MAX_EXPAND_TOKENS` | `8000` | code default | 1 |
| `tool_result_digest_min_savings_tokens` | context and history | `AGENT_TOOL_RESULT_DIGEST_MIN_SAVINGS_TOKENS` | `500` | code default | 1 |
| `tool_result_digest_pin_ttl_turns` | context and history | `AGENT_TOOL_RESULT_DIGEST_PIN_TTL_TURNS` | `4` | code default | 2 |
| `tool_result_digest_put_timeout_ms` | context and history | `AGENT_TOOL_RESULT_DIGEST_PUT_TIMEOUT_MS` | `2000` | code default | 1 |
| `tool_result_digest_tail_lines` | context and history | `AGENT_TOOL_RESULT_DIGEST_TAIL_LINES` | `20` | code default | 1 |
| `tool_result_digest_threshold_tokens` | context and history | `AGENT_TOOL_RESULT_DIGEST_THRESHOLD_TOKENS` | `1500` | code default | 1 |
| `within_session_compression_enabled` | context and history | `AGENT_WITHIN_SESSION_COMPRESSION_ENABLED` | `True` | code default | 1 |
| `within_session_compression_refire_after_messages` | context and history | `AGENT_WITHIN_SESSION_COMPRESSION_REFIRE_AFTER_MESSAGES` | `4` | code default | **none in `src/`** |
| `within_session_hard_threshold_ratio` | context and history | `AGENT_WITHIN_SESSION_HARD_THRESHOLD_RATIO` | `0.85` | code default | 1 |
| `within_session_max_tail_ratio` | context and history | `AGENT_WITHIN_SESSION_MAX_TAIL_RATIO` | `0.35` | code default | 1 |
| `within_session_min_tail_ratio` | context and history | `AGENT_WITHIN_SESSION_MIN_TAIL_RATIO` | `0.25` | code default | 2 |
| `within_session_pre_pass_threshold_tokens` | context and history | `AGENT_WITHIN_SESSION_PRE_PASS_THRESHOLD_TOKENS` | `800` | code default | 1 |
| `broad_recall_relevance_bound` | memory and recall | `AGENT_BROAD_RECALL_RELEVANCE_BOUND` | `0.338891` | code default | 3 |
| `broad_recall_relevance_gate_enabled` | memory and recall | `AGENT_BROAD_RECALL_RELEVANCE_GATE_ENABLED` | `True` | code default | 1 |
| `dedup_similarity_threshold` | memory and recall | `AGENT_DEDUP_SIMILARITY_THRESHOLD` | `0.92` | code default | 1 |
| `embedding_backfill_enabled` | memory and recall | `AGENT_EMBEDDING_BACKFILL_ENABLED` | `True` | code default | 1 |
| `embedding_batch_size` | memory and recall | `AGENT_EMBEDDING_BATCH_SIZE` | `20` | code default | **none in `src/`** |
| `embedding_dimensions` | memory and recall | `AGENT_EMBEDDING_DIMENSIONS` | `1024` | code default | 4 |
| `enable_memory_graph` | memory and recall | `AGENT_ENABLE_MEMORY_GRAPH` | `False` | .env override, differs from default | 2 |
| `entity_match_relevance_bound` | memory and recall | `AGENT_ENTITY_MATCH_RELEVANCE_BOUND` | `0.338891` | code default | 3 |
| `entity_match_relevance_gate_enabled` | memory and recall | `AGENT_ENTITY_MATCH_RELEVANCE_GATE_ENABLED` | `True` | code default | 1 |
| `freshness_enabled` | memory and recall | `AGENT_FRESHNESS_ENABLED` | `False` | .env override, differs from default | 7 |
| `freshness_relevance_weight` | memory and recall | `AGENT_FRESHNESS_RELEVANCE_WEIGHT` | `0.15` | .env override, equals default | 1 |
| `freshness_tier_factors` | memory and recall | `AGENT_FRESHNESS_TIER_FACTORS` | `factory:lambda: { "warm": 1.0, "cooling": 0.85, …` (cut; see `settings.py:3157`) | code default | 2 |
| `freshness_tier_reranking_enabled` | memory and recall | `AGENT_FRESHNESS_TIER_RERANKING_ENABLED` | `True` | code default | 1 |
| `lexical_arm_enabled` | memory and recall | `AGENT_LEXICAL_ARM_ENABLED` | `False` | .env override, differs from default | 1 |
| `multipath_arm_top_k` | memory and recall | `AGENT_MULTIPATH_ARM_TOP_K` | `50` | code default | 1 |
| `multipath_paraphrase_count` | memory and recall | `AGENT_MULTIPATH_PARAPHRASE_COUNT` | `3` | code default | 1 |
| `multipath_recall_enabled` | memory and recall | `AGENT_MULTIPATH_RECALL_ENABLED` | `False` | .env override, differs from default | 2 |
| `multipath_rrf_k` | memory and recall | `AGENT_MULTIPATH_RRF_K` | `60` | code default | 1 |
| `multiquery_arm_enabled` | memory and recall | `AGENT_MULTIQUERY_ARM_ENABLED` | `False` | .env override, differs from default | 1 |
| `proactive_memory_diminishing_score_floor` | memory and recall | `AGENT_PROACTIVE_MEMORY_DIMINISHING_SCORE_FLOOR` | `0.35` | code default | 1 |
| `proactive_memory_diminishing_score_gap` | memory and recall | `AGENT_PROACTIVE_MEMORY_DIMINISHING_SCORE_GAP` | `0.15` | code default | 1 |
| `proactive_memory_enabled` | memory and recall | `AGENT_PROACTIVE_MEMORY_ENABLED` | `False` | .env override, differs from default | 1 |
| `proactive_memory_max_candidates` | memory and recall | `AGENT_PROACTIVE_MEMORY_MAX_CANDIDATES` | `10` | code default | 1 |
| `proactive_memory_max_injected_items` | memory and recall | `AGENT_PROACTIVE_MEMORY_MAX_INJECTED_ITEMS` | `5` | code default | 2 |
| `proactive_memory_max_tokens` | memory and recall | `AGENT_PROACTIVE_MEMORY_MAX_TOKENS` | `500` | code default | 1 |
| `proactive_memory_min_score` | memory and recall | `AGENT_PROACTIVE_MEMORY_MIN_SCORE` | `0.3` | code default | 1 |
| `proactive_memory_recency_half_life_days` | memory and recall | `AGENT_PROACTIVE_MEMORY_RECENCY_HALF_LIFE_DAYS` | `30.0` | code default | 1 |
| `proactive_memory_relevance_bound` | memory and recall | `AGENT_PROACTIVE_MEMORY_RELEVANCE_BOUND` | `None` | code default | 2 |
| `proactive_memory_relevance_gate_enabled` | memory and recall | `AGENT_PROACTIVE_MEMORY_RELEVANCE_GATE_ENABLED` | `True` | code default | 1 |
| `proactive_memory_rerank_relevance_bound` | memory and recall | `AGENT_PROACTIVE_MEMORY_RERANK_RELEVANCE_BOUND` | `0.338891` | code default | 2 |
| `proactive_memory_vector_top_k` | memory and recall | `AGENT_PROACTIVE_MEMORY_VECTOR_TOP_K` | `20` | code default | 1 |
| `proactive_memory_w_embedding` | memory and recall | `AGENT_PROACTIVE_MEMORY_W_EMBEDDING` | `0.45` | code default | 1 |
| `proactive_memory_w_entity` | memory and recall | `AGENT_PROACTIVE_MEMORY_W_ENTITY` | `0.25` | code default | 1 |
| `proactive_memory_w_recency` | memory and recall | `AGENT_PROACTIVE_MEMORY_W_RECENCY` | `0.20` | code default | 1 |
| `proactive_memory_w_topic` | memory and recall | `AGENT_PROACTIVE_MEMORY_W_TOPIC` | `0.10` | code default | 1 |
| `recall_candidate_cap` | memory and recall | `AGENT_RECALL_CANDIDATE_CAP` | `500` | code default | 2 |
| `recall_per_entity_turn_cap` | memory and recall | `AGENT_RECALL_PER_ENTITY_TURN_CAP` | `10` | code default | 1 |
| `recall_similarity_floor` | memory and recall | `AGENT_RECALL_SIMILARITY_FLOOR` | `0.0` | .env override, differs from default | 2 |
| `relevance_bounded_recall_enabled` | memory and recall | `AGENT_RELEVANCE_BOUNDED_RECALL_ENABLED` | `False` | .env override, differs from default | 1 |
| `reranker_enabled` | memory and recall | `AGENT_RERANKER_ENABLED` | `True` | code default | 2 |
| `reranker_input_cap` | memory and recall | `AGENT_RERANKER_INPUT_CAP` | `25` | code default | 2 |
| `reranker_top_k` | memory and recall | `AGENT_RERANKER_TOP_K` | `10` | code default | 3 |
| `structural_arm_enabled` | memory and recall | `AGENT_STRUCTURAL_ARM_ENABLED` | `False` | code default | 1 |
| `structural_arm_top_k` | memory and recall | `AGENT_STRUCTURAL_ARM_TOP_K` | `50` | code default | 1 |
| `structural_class_predicate_enabled` | memory and recall | `AGENT_STRUCTURAL_CLASS_PREDICATE_ENABLED` | `False` | code default | 1 |
| `structural_type_predicate_enabled` | memory and recall | `AGENT_STRUCTURAL_TYPE_PREDICATE_ENABLED` | `False` | code default | 1 |
| `approval_timeout_seconds` | tools and governance | `AGENT_APPROVAL_TIMEOUT_SECONDS` | `60.0` | code default | 1 |
| `approval_ui_enabled` | tools and governance | `AGENT_APPROVAL_UI_ENABLED` | `False` | .env override, differs from default | 1 |
| `artifact_draft_max_tokens` | tools and governance | `AGENT_ARTIFACT_DRAFT_MAX_TOKENS` | `32768` | code default | 3 |
| `artifact_envelope_probe_enabled` | tools and governance | `AGENT_ARTIFACT_ENVELOPE_PROBE_ENABLED` | `True` | code default | 1 |
| `artifact_envelope_probe_timeout_s` | tools and governance | `AGENT_ARTIFACT_ENVELOPE_PROBE_TIMEOUT_S` | `2.0` | code default | 1 |
| `attachment_image_max_bytes` | tools and governance | `AGENT_ATTACHMENT_IMAGE_MAX_BYTES` | `5_242_880` | code default | 1 |
| `attachment_image_max_pixels` | tools and governance | `AGENT_ATTACHMENT_IMAGE_MAX_PIXELS` | `1568` | code default | 1 |
| `attachment_max_images_per_turn` | tools and governance | `AGENT_ATTACHMENT_MAX_IMAGES_PER_TURN` | `4` | code default | 1 |
| `attachment_max_total_payload_bytes` | tools and governance | `AGENT_ATTACHMENT_MAX_TOTAL_PAYLOAD_BYTES` | `15_728_640` | code default | 1 |
| `document_max_extracted_text_chars` | tools and governance | `AGENT_DOCUMENT_MAX_EXTRACTED_TEXT_CHARS` | `200_000` | code default | 1 |
| `document_max_pages_per_turn` | tools and governance | `AGENT_DOCUMENT_MAX_PAGES_PER_TURN` | `40` | code default | 1 |
| `document_max_total_payload_bytes` | tools and governance | `AGENT_DOCUMENT_MAX_TOTAL_PAYLOAD_BYTES` | `15_728_640` | code default | 1 |
| `document_page_max_bytes` | tools and governance | `AGENT_DOCUMENT_PAGE_MAX_BYTES` | `5_242_880` | code default | 1 |
| `document_page_max_pixels` | tools and governance | `AGENT_DOCUMENT_PAGE_MAX_PIXELS` | `1568` | code default | 1 |
| `document_text_density_floor_per_page` | tools and governance | `AGENT_DOCUMENT_TEXT_DENSITY_FLOOR_PER_PAGE` | `100` | code default | 1 |
| `linear_agent_rate_limit_per_day` | tools and governance | `AGENT_LINEAR_AGENT_RATE_LIMIT_PER_DAY` | `10` | code default | 1 |
| `mcp_gateway_enabled` | tools and governance | `AGENT_MCP_GATEWAY_ENABLED` | `False` | .env override, equals default | 3 |
| `mcp_gateway_enabled_servers` | tools and governance | `AGENT_MCP_GATEWAY_ENABLED_SERVERS` | `factory:list` | code default | 2 |
| `mcp_gateway_timeout_seconds` | tools and governance | `AGENT_MCP_GATEWAY_TIMEOUT_SECONDS` | `60` | code default | 1 |
| `orchestrator_max_repeated_tool_calls` | tools and governance | `AGENT_ORCHESTRATOR_MAX_REPEATED_TOOL_CALLS` | `1` | code default | **none in `src/`** |
| `orchestrator_max_tool_iterations` | tools and governance | `AGENT_ORCHESTRATOR_MAX_TOOL_ITERATIONS` | `25` | code default | 3 |
| `perplexity_timeout_seconds` | tools and governance | `AGENT_PERPLEXITY_TIMEOUT_SECONDS` | `90` | code default | 1 |
| `primitive_tools_enabled` | tools and governance | `AGENT_PRIMITIVE_TOOLS_ENABLED` | `False` | .env override, differs from default | 1 |
| `searxng_default_categories` | tools and governance | `AGENT_SEARXNG_DEFAULT_CATEGORIES` | `"general"` | code default | 1 |
| `searxng_max_results` | tools and governance | `AGENT_SEARXNG_MAX_RESULTS` | `10` | code default | 1 |
| `searxng_timeout_seconds` | tools and governance | `AGENT_SEARXNG_TIMEOUT_SECONDS` | `12` | code default | 1 |
| `upload_max_size_bytes` | tools and governance | `AGENT_UPLOAD_MAX_SIZE_BYTES` | `52_428_800` | code default | 1 |
| `url_guard_allowlist` | tools and governance | `AGENT_URL_GUARD_ALLOWLIST` | `factory:list` | code default | 1 |
| `url_guard_cache_ttl_seconds` | tools and governance | `AGENT_URL_GUARD_CACHE_TTL_SECONDS` | `3600` | code default | 2 |
| `url_guard_enabled` | tools and governance | `AGENT_URL_GUARD_ENABLED` | `True` | code default | **none in `src/`** |
| `url_guard_mode` | tools and governance | `AGENT_URL_GUARD_MODE` | `"blocklist"` | code default | 1 |
| `url_guard_novelty_window_days` | tools and governance | `AGENT_URL_GUARD_NOVELTY_WINDOW_DAYS` | `14` | code default | 1 |
| `context_quality_governance_budget_reduction` | grounding and quality | `AGENT_CONTEXT_QUALITY_GOVERNANCE_BUDGET_REDUCTION` | `0.15` | code default | 1 |
| `context_quality_governance_enabled` | grounding and quality | `AGENT_CONTEXT_QUALITY_GOVERNANCE_ENABLED` | `False` | code default | 3 |
| `context_quality_governance_threshold` | grounding and quality | `AGENT_CONTEXT_QUALITY_GOVERNANCE_THRESHOLD` | `2` | code default | 2 |
| `graph_quality_governance_enabled` | grounding and quality | `AGENT_GRAPH_QUALITY_GOVERNANCE_ENABLED` | `False` | .env override, differs from default | 3 |
| `grounding_compliance_bar` | grounding and quality | `AGENT_GROUNDING_COMPLIANCE_BAR` | `0.95` | code default | 2 |
| `grounding_compliance_max_window_age_hours` | grounding and quality | `AGENT_GROUNDING_COMPLIANCE_MAX_WINDOW_AGE_HOURS` | `1440` | code default | 1 |
| `grounding_compliance_min_samples` | grounding and quality | `AGENT_GROUNDING_COMPLIANCE_MIN_SAMPLES` | `20` | code default | 1 |
| `grounding_compliance_window_size` | grounding and quality | `AGENT_GROUNDING_COMPLIANCE_WINDOW_SIZE` | `100` | code default | 1 |
| `grounding_enforcement_cooldown_hours` | grounding and quality | `AGENT_GROUNDING_ENFORCEMENT_COOLDOWN_HOURS` | `24` | code default | 1 |
| `grounding_enforcement_demote_below` | grounding and quality | `AGENT_GROUNDING_ENFORCEMENT_DEMOTE_BELOW` | `0.90` | code default | 1 |
| `grounding_enforcement_probation_rate` | grounding and quality | `AGENT_GROUNDING_ENFORCEMENT_PROBATION_RATE` | `0.35` | code default | 1 |
| `grounding_entailment_latency_budget_ms` | grounding and quality | `AGENT_GROUNDING_ENTAILMENT_LATENCY_BUDGET_MS` | `4000` | code default | 1 |
| `grounding_entailment_max_excerpt_chars` | grounding and quality | `AGENT_GROUNDING_ENTAILMENT_MAX_EXCERPT_CHARS` | `6000` | code default | 1 |
| `grounding_entailment_max_inline_checks` | grounding and quality | `AGENT_GROUNDING_ENTAILMENT_MAX_INLINE_CHECKS` | `8` | code default | 1 |
| `grounding_entailment_max_partial_miss_checks` | grounding and quality | `AGENT_GROUNDING_ENTAILMENT_MAX_PARTIAL_MISS_CHECKS` | `8` | code default | 1 |
| `grounding_entailment_sample_rate` | grounding and quality | `AGENT_GROUNDING_ENTAILMENT_SAMPLE_RATE` | `0.05` | code default | 1 |
| `grounding_max_generation_attempts` | grounding and quality | `AGENT_GROUNDING_MAX_GENERATION_ATTEMPTS` | `2` | code default | 1 |
| `grounding_verification_mode` | grounding and quality | `AGENT_GROUNDING_VERIFICATION_MODE` | `"off"` | .env override, equals default | 2 |
| `llm_max_retries` | timeouts and limits | `AGENT_LLM_MAX_RETRIES` | `3` | code default | 1 |
| `llm_timeout_seconds` | timeouts and limits | `AGENT_LLM_TIMEOUT_SECONDS` | `120` | code default | 2 |
| `orchestrator_creditable_pause_limit` | timeouts and limits | `AGENT_ORCHESTRATOR_CREDITABLE_PAUSE_LIMIT` | `3` | code default | 1 |
| `orchestrator_task_timeout_seconds` | timeouts and limits | `AGENT_ORCHESTRATOR_TASK_TIMEOUT_SECONDS` | `900` | .env override, differs from default | 1 |
| `orchestrator_turn_lifetime_seconds` | timeouts and limits | `AGENT_ORCHESTRATOR_TURN_LIFETIME_SECONDS` | `1800` | .env override, differs from default | 1 |
| `route_trace_preview_chars` | timeouts and limits | `AGENT_ROUTE_TRACE_PREVIEW_CHARS` | `280` | code default | 1 |
| `route_trace_store_preview` | timeouts and limits | `AGENT_ROUTE_TRACE_STORE_PREVIEW` | `False` | code default | 3 |
| `service_request_close_timeout_seconds` | timeouts and limits | `AGENT_SERVICE_REQUEST_CLOSE_TIMEOUT_SECONDS` | `10` | code default | 2 |
| `service_request_task_lifetime_seconds` | timeouts and limits | `AGENT_SERVICE_REQUEST_TASK_LIFETIME_SECONDS` | `factory:lambda data: data.get("orchestrator_turn_lifetime_seconds", 1800) + 300` | code default | 2 |
| `session_write_wait_timeout_seconds` | timeouts and limits | `AGENT_SESSION_WRITE_WAIT_TIMEOUT_SECONDS` | `10.0` | code default | 1 |
| `owner_name` | identity | `AGENT_OWNER_NAME` | `""` | .env override, differs from default | 2 |
| `user_display_names_json` | identity | `AGENT_USER_DISPLAY_NAMES_JSON` | `"{}"` | .env override, differs from default | dynamic |
| `brainstem_sensor_poll_interval_seconds` | background jobs | `AGENT_BRAINSTEM_SENSOR_POLL_INTERVAL_SECONDS` | `5.0` | code default | **none in `src/`** |
| `cache_erosion_probe_enabled` | background jobs | `AGENT_CACHE_EROSION_PROBE_ENABLED` | `True` | code default | 2 |
| `cache_erosion_probe_index_prefix` | background jobs | `AGENT_CACHE_EROSION_PROBE_INDEX_PREFIX` | `"agent-monitors-cache-erosion"` | code default | 2 |
| `cache_erosion_probe_interval_seconds` | background jobs | `AGENT_CACHE_EROSION_PROBE_INTERVAL_SECONDS` | `3600` | code default | 1 |
| `cache_erosion_probe_threshold` | background jobs | `AGENT_CACHE_EROSION_PROBE_THRESHOLD` | `0.9` | code default | 1 |
| `cache_erosion_probe_window_days` | background jobs | `AGENT_CACHE_EROSION_PROBE_WINDOW_DAYS` | `2` | code default | 1 |
| `captains_log_reflection_cadence_enabled` | background jobs | `AGENT_CAPTAINS_LOG_REFLECTION_CADENCE_ENABLED` | `True` | code default | 1 |
| `captains_log_reflection_enabled` | background jobs | `AGENT_CAPTAINS_LOG_REFLECTION_ENABLED` | `True` | .env override, differs from default | 1 |
| `captains_log_reflection_min_interval_seconds` | background jobs | `AGENT_CAPTAINS_LOG_REFLECTION_MIN_INTERVAL_SECONDS` | `1800.0` | .env override, equals default | 1 |
| `consolidator_max_extraction_attempts` | background jobs | `AGENT_CONSOLIDATOR_MAX_EXTRACTION_ATTEMPTS` | `5` | code default | 1 |
| `context_quality_stream_enabled` | background jobs | `AGENT_CONTEXT_QUALITY_STREAM_ENABLED` | `True` | code default | 1 |
| `data_lifecycle_enabled` | background jobs | `AGENT_DATA_LIFECYCLE_ENABLED` | `True` | code default | 2 |
| `decided_skip_days` | background jobs | `AGENT_DECIDED_SKIP_DAYS` | `90` | code default | 2 |
| `delivery_ratio_probe_enabled` | background jobs | `AGENT_DELIVERY_RATIO_PROBE_ENABLED` | `True` | code default | 2 |
| `delivery_ratio_probe_index_prefix` | background jobs | `AGENT_DELIVERY_RATIO_PROBE_INDEX_PREFIX` | `"agent-monitors-delivery-ratio"` | code default | 2 |
| `delivery_ratio_probe_interval_seconds` | background jobs | `AGENT_DELIVERY_RATIO_PROBE_INTERVAL_SECONDS` | `86400` | code default | 1 |
| `delivery_ratio_probe_min_ratio` | background jobs | `AGENT_DELIVERY_RATIO_PROBE_MIN_RATIO` | `0.99` | code default | 1 |
| `disk_usage_alert_percent` | background jobs | `AGENT_DISK_USAGE_ALERT_PERCENT` | `80.0` | code default | 1 |
| `enable_second_brain` | background jobs | `AGENT_ENABLE_SECOND_BRAIN` | `False` | .env override, differs from default | 2 |
| `entity_extraction_fewshot_exemplars_enabled` | background jobs | `AGENT_ENTITY_EXTRACTION_FEWSHOT_EXEMPLARS_ENABLED` | `False` | code default | 1 |
| `entity_extraction_timeout_seconds` | background jobs | `AGENT_ENTITY_EXTRACTION_TIMEOUT_SECONDS` | `90` | code default | 1 |
| `error_monitor_enabled` | background jobs | `AGENT_ERROR_MONITOR_ENABLED` | `True` | code default | 1 |
| `error_monitor_max_patterns_per_scan` | background jobs | `AGENT_ERROR_MONITOR_MAX_PATTERNS_PER_SCAN` | `50` | code default | 1 |
| `error_monitor_min_occurrences` | background jobs | `AGENT_ERROR_MONITOR_MIN_OCCURRENCES` | `5` | code default | 1 |
| `error_monitor_window_hours` | background jobs | `AGENT_ERROR_MONITOR_WINDOW_HOURS` | `24` | code default | 1 |
| `event_bus_ack_timeout_seconds` | background jobs | `AGENT_EVENT_BUS_ACK_TIMEOUT_SECONDS` | `300` | .env override, equals default | 1 |
| `event_bus_consumer_poll_interval_ms` | background jobs | `AGENT_EVENT_BUS_CONSUMER_POLL_INTERVAL_MS` | `100` | .env override, equals default | 1 |
| `event_bus_enabled` | background jobs | `AGENT_EVENT_BUS_ENABLED` | `False` | .env override, differs from default | 3 |
| `event_bus_max_retries` | background jobs | `AGENT_EVENT_BUS_MAX_RETRIES` | `3` | .env override, equals default | 1 |
| `failure_path_reflection_enabled` | background jobs | `AGENT_FAILURE_PATH_REFLECTION_ENABLED` | `True` | code default | 1 |
| `feedback_defer_revisit_days` | background jobs | `AGENT_FEEDBACK_DEFER_REVISIT_DAYS` | `90` | code default | **none in `src/`** |
| `feedback_max_reevaluations` | background jobs | `AGENT_FEEDBACK_MAX_REEVALUATIONS` | `2` | code default | 1 |
| `feedback_polling_enabled` | background jobs | `AGENT_FEEDBACK_POLLING_ENABLED` | `True` | .env override, differs from default | 2 |
| `feedback_polling_hour_utc` | background jobs | `AGENT_FEEDBACK_POLLING_HOUR_UTC` | `7` | code default | 1 |
| `feedback_suppression_days` | background jobs | `AGENT_FEEDBACK_SUPPRESSION_DAYS` | `30` | code default | 1 |
| `freshness_backfill_confirm` | background jobs | `AGENT_FRESHNESS_BACKFILL_CONFIRM` | `False` | .env override, equals default | 1 |
| `freshness_cold_threshold_days` | background jobs | `AGENT_FRESHNESS_COLD_THRESHOLD_DAYS` | `180.0` | .env override, equals default | 1 |
| `freshness_consumer_batch_max_events` | background jobs | `AGENT_FRESHNESS_CONSUMER_BATCH_MAX_EVENTS` | `50` | .env override, equals default | 1 |
| `freshness_consumer_batch_window_seconds` | background jobs | `AGENT_FRESHNESS_CONSUMER_BATCH_WINDOW_SECONDS` | `5.0` | .env override, equals default | 1 |
| `freshness_dormant_entity_proposal_threshold` | background jobs | `AGENT_FRESHNESS_DORMANT_ENTITY_PROPOSAL_THRESHOLD` | `10` | .env override, equals default | 3 |
| `freshness_dormant_relationship_proposal_threshold` | background jobs | `AGENT_FRESHNESS_DORMANT_RELATIONSHIP_PROPOSAL_THRESHOLD` | `10` | .env override, equals default | 1 |
| `freshness_frequency_boost_alpha` | background jobs | `AGENT_FRESHNESS_FREQUENCY_BOOST_ALPHA` | `0.1` | .env override, equals default | 1 |
| `freshness_frequency_boost_max` | background jobs | `AGENT_FRESHNESS_FREQUENCY_BOOST_MAX` | `1.5` | .env override, equals default | 1 |
| `freshness_half_life_days` | background jobs | `AGENT_FRESHNESS_HALF_LIFE_DAYS` | `30.0` | .env override, equals default | 3 |
| `freshness_never_accessed_noise_days` | background jobs | `AGENT_FRESHNESS_NEVER_ACCESSED_NOISE_DAYS` | `30.0` | .env override, equals default | 1 |
| `freshness_review_schedule_cron` | background jobs | `AGENT_FRESHNESS_REVIEW_SCHEDULE_CRON` | `"0 3 * * 0"` | .env override, equals default | 1 |
| `graph_quality_stream_enabled` | background jobs | `AGENT_GRAPH_QUALITY_STREAM_ENABLED` | `True` | code default | 3 |
| `insights_enabled` | background jobs | `AGENT_INSIGHTS_ENABLED` | `True` | .env override, differs from default | 2 |
| `insights_wiring_enabled` | background jobs | `AGENT_INSIGHTS_WIRING_ENABLED` | `True` | .env override, differs from default | 1 |
| `joinability_probe_enabled` | background jobs | `AGENT_JOINABILITY_PROBE_ENABLED` | `True` | code default | 2 |
| `joinability_probe_index_prefix` | background jobs | `AGENT_JOINABILITY_PROBE_INDEX_PREFIX` | `"agent-monitors-joinability"` | code default | 3 |
| `joinability_probe_interval_seconds` | background jobs | `AGENT_JOINABILITY_PROBE_INTERVAL_SECONDS` | `3600` | code default | 1 |
| `joinability_probe_window_hours` | background jobs | `AGENT_JOINABILITY_PROBE_WINDOW_HOURS` | `24` | code default | 1 |
| `kg_stats_projection_schedule_cron` | background jobs | `AGENT_KG_STATS_PROJECTION_SCHEDULE_CRON` | `"0 4 * * *"` | code default | 1 |
| `metrics_daemon_buffer_size` | background jobs | `AGENT_METRICS_DAEMON_BUFFER_SIZE` | `720` | code default | 2 |
| `metrics_daemon_es_emit_interval_seconds` | background jobs | `AGENT_METRICS_DAEMON_ES_EMIT_INTERVAL_SECONDS` | `30.0` | code default | 2 |
| `metrics_daemon_poll_interval_seconds` | background jobs | `AGENT_METRICS_DAEMON_POLL_INTERVAL_SECONDS` | `5.0` | code default | 2 |
| `metrics_sampled_stream_maxlen` | background jobs | `AGENT_METRICS_SAMPLED_STREAM_MAXLEN` | `720` | code default | 1 |
| `mode_calibration_anomaly_threshold` | background jobs | `AGENT_MODE_CALIBRATION_ANOMALY_THRESHOLD` | `3` | code default | 1 |
| `mode_controller_enabled` | background jobs | `AGENT_MODE_CONTROLLER_ENABLED` | `True` | code default | 3 |
| `mode_evaluation_interval_seconds` | background jobs | `AGENT_MODE_EVALUATION_INTERVAL_SECONDS` | `30.0` | code default | 1 |
| `mode_window_size` | background jobs | `AGENT_MODE_WINDOW_SIZE` | `12` | code default | 1 |
| `outcome_ingestion_enabled` | background jobs | `AGENT_OUTCOME_INGESTION_ENABLED` | `True` | code default | 2 |
| `outcome_ingestion_hour_utc` | background jobs | `AGENT_OUTCOME_INGESTION_HOUR_UTC` | `8` | code default | 1 |
| `promotion_initial_cap` | background jobs | `AGENT_PROMOTION_INITIAL_CAP` | `5` | code default | 1 |
| `promotion_min_seen_count` | background jobs | `AGENT_PROMOTION_MIN_SEEN_COUNT` | `3` | code default | 5 |
| `promotion_pipeline_enabled` | background jobs | `AGENT_PROMOTION_PIPELINE_ENABLED` | `True` | .env override, equals default | 2 |
| `prompt_debug_capture_enabled` | background jobs | `AGENT_PROMPT_DEBUG_CAPTURE_ENABLED` | `False` | .env override, differs from default | 1 |
| `quality_monitor_anomaly_window_days` | background jobs | `AGENT_QUALITY_MONITOR_ANOMALY_WINDOW_DAYS` | `7` | code default | 1 |
| `quality_monitor_daily_run_hour_utc` | background jobs | `AGENT_QUALITY_MONITOR_DAILY_RUN_HOUR_UTC` | `5` | code default | 1 |
| `quality_monitor_enabled` | background jobs | `AGENT_QUALITY_MONITOR_ENABLED` | `True` | code default | 2 |
| `request_monitoring_enabled` | background jobs | `AGENT_REQUEST_MONITORING_ENABLED` | `True` | code default | 1 |
| `request_monitoring_include_gpu` | background jobs | `AGENT_REQUEST_MONITORING_INCLUDE_GPU` | `True` | code default | **none in `src/`** |
| `request_monitoring_interval_seconds` | background jobs | `AGENT_REQUEST_MONITORING_INTERVAL_SECONDS` | `5.0` | code default | **none in `src/`** |
| `second_brain_cpu_threshold` | background jobs | `AGENT_SECOND_BRAIN_CPU_THRESHOLD` | `50.0` | .env override, equals default | 2 |
| `second_brain_idle_time_seconds` | background jobs | `AGENT_SECOND_BRAIN_IDLE_TIME_SECONDS` | `300.0` | .env override, differs from default | 2 |
| `second_brain_memory_threshold` | background jobs | `AGENT_SECOND_BRAIN_MEMORY_THRESHOLD` | `70.0` | .env override, equals default | 2 |
| `second_brain_min_interval_seconds` | background jobs | `AGENT_SECOND_BRAIN_MIN_INTERVAL_SECONDS` | `3600.0` | .env override, differs from default | 2 |
| `second_brain_resource_gating_enabled` | background jobs | `AGENT_SECOND_BRAIN_RESOURCE_GATING_ENABLED` | `True` | .env override, differs from default | 1 |
| `seshat_open_ticket_cap` | background jobs | `AGENT_SESHAT_OPEN_TICKET_CAP` | `10` | .env override, equals default | 4 |
| `session_digest_max_tokens` | background jobs | `AGENT_SESSION_DIGEST_MAX_TOKENS` | `400` | code default | 1 |
| `session_digest_structured_output` | background jobs | `AGENT_SESSION_DIGEST_STRUCTURED_OUTPUT` | `True` | code default | 1 |
| `session_digest_target_tokens` | background jobs | `AGENT_SESSION_DIGEST_TARGET_TOKENS` | `120` | code default | 1 |
| `session_retention_days` | background jobs | `AGENT_SESSION_RETENTION_DAYS` | `180` | code default | 1 |
| `session_retention_sweep_interval_seconds` | background jobs | `AGENT_SESSION_RETENTION_SWEEP_INTERVAL_SECONDS` | `86400` | code default | 1 |
| `session_summary_enabled` | background jobs | `AGENT_SESSION_SUMMARY_ENABLED` | `True` | .env override, equals default | 2 |
| `session_summary_idle_threshold_seconds` | background jobs | `AGENT_SESSION_SUMMARY_IDLE_THRESHOLD_SECONDS` | `900.0` | code default | 1 |
| `session_summary_max_attempts` | background jobs | `AGENT_SESSION_SUMMARY_MAX_ATTEMPTS` | `2` | code default | 1 |
| `session_summary_retry_backoff_base_seconds` | background jobs | `AGENT_SESSION_SUMMARY_RETRY_BACKOFF_BASE_SECONDS` | `900.0` | code default | 1 |
| `session_summary_retry_backoff_max_seconds` | background jobs | `AGENT_SESSION_SUMMARY_RETRY_BACKOFF_MAX_SECONDS` | `21600.0` | code default | 1 |
| `session_summary_sweep_interval_seconds` | background jobs | `AGENT_SESSION_SUMMARY_SWEEP_INTERVAL_SECONDS` | `300.0` | code default | 1 |
| `signal_priority_clamp` | background jobs | `AGENT_SIGNAL_PRIORITY_CLAMP` | `0.5` | code default | 1 |
| `signal_smoothing_prior` | background jobs | `AGENT_SIGNAL_SMOOTHING_PRIOR` | `2.0` | code default | 1 |
| `signal_suppression_cooldown_days` | background jobs | `AGENT_SIGNAL_SUPPRESSION_COOLDOWN_DAYS` | `30` | code default | 1 |
| `signal_suppression_min_n` | background jobs | `AGENT_SIGNAL_SUPPRESSION_MIN_N` | `5` | code default | 1 |
| `signal_suppression_threshold` | background jobs | `AGENT_SIGNAL_SUPPRESSION_THRESHOLD` | `-0.4` | code default | 1 |
| `signal_window_days` | background jobs | `AGENT_SIGNAL_WINDOW_DAYS` | `90` | code default | 1 |
| `slm_gpu_util_degraded_pct` | background jobs | `AGENT_SLM_GPU_UTIL_DEGRADED_PCT` | `95.0` | code default | 1 |
| `slm_health_cache_ttl_seconds` | background jobs | `AGENT_SLM_HEALTH_CACHE_TTL_SECONDS` | `45.0` | code default | 2 |
| `slm_health_generation_check_enabled` | background jobs | `AGENT_SLM_HEALTH_GENERATION_CHECK_ENABLED` | `False` | .env override, differs from default | 2 |
| `slm_health_index_prefix` | background jobs | `AGENT_SLM_HEALTH_INDEX_PREFIX` | `"agent-monitors-slm-health"` | code default | 2 |
| `slm_health_probe_down_interval_seconds` | background jobs | `AGENT_SLM_HEALTH_PROBE_DOWN_INTERVAL_SECONDS` | `60.0` | code default | 2 |
| `slm_health_probe_enabled` | background jobs | `AGENT_SLM_HEALTH_PROBE_ENABLED` | `True` | code default | 2 |
| `slm_health_probe_interval_seconds` | background jobs | `AGENT_SLM_HEALTH_PROBE_INTERVAL_SECONDS` | `300.0` | code default | 2 |
| `slm_health_url` | background jobs | `AGENT_SLM_HEALTH_URL` | `None` | code default | dynamic |
| `slm_queue_depth_degraded` | background jobs | `AGENT_SLM_QUEUE_DEPTH_DEGRADED` | `4` | code default | 1 |
| `slm_thinking_canary_enabled` | background jobs | `AGENT_SLM_THINKING_CANARY_ENABLED` | `False` | code default | 1 |
| `sysgraph_maintenance_enabled` | background jobs | `AGENT_SYSGRAPH_MAINTENANCE_ENABLED` | `True` | code default | 2 |
| `sysgraph_maintenance_hour_utc` | background jobs | `AGENT_SYSGRAPH_MAINTENANCE_HOUR_UTC` | `9` | code default | 1 |
| `turn_observed_stream_maxlen` | background jobs | `AGENT_TURN_OBSERVED_STREAM_MAXLEN` | `10000` | code default | 5 |
| `turn_projector_enabled` | background jobs | `AGENT_TURN_PROJECTOR_ENABLED` | `True` | code default | 1 |
| `agent_id` | infrastructure | `AGENT_AGENT_ID` | — | .env override (value not compared) | 3 |
| `agent_owner_email` | infrastructure | `AGENT_OWNER_EMAIL` | — | .env override (value not compared) | 5 |
| `allow_test_writes_to_prod_substrate` | infrastructure | `AGENT_ALLOW_TEST_WRITES_TO_PROD_SUBSTRATE` | — | code default | 1 |
| `allowed_ws_origins` | infrastructure | `AGENT_ALLOWED_WS_ORIGINS` | — | .env override (value not compared) | 1 |
| `artifacts_egress_base_url` | infrastructure | `AGENT_ARTIFACTS_EGRESS_BASE_URL` | — | .env override (value not compared) | 2 |
| `artifacts_public_base_url` | infrastructure | `AGENT_ARTIFACTS_PUBLIC_BASE_URL` | — | .env override (value not compared) | 6 |
| `build_fingerprint` | infrastructure | `AGENT_BUILD_FINGERPRINT` | — | .env override (value not compared) | 1 |
| `captains_log_index_prefix` | infrastructure | `AGENT_CAPTAINS_LOG_INDEX_PREFIX` | — | code default | 5 |
| `cf_access_aud` | infrastructure | `CF_ACCESS_AUD` | — | code default | 2 |
| `cf_access_main_aud` | infrastructure | `CF_ACCESS_MAIN_AUD` | — | code default | 2 |
| `cf_access_team_domain` | infrastructure | `CF_ACCESS_TEAM_DOMAIN` | — | code default | 3 |
| `cors_allowed_origins` | infrastructure | `AGENT_CORS_ALLOWED_ORIGINS` | — | .env override (value not compared) | 1 |
| `database_admin_url` | infrastructure | `AGENT_DATABASE_ADMIN_URL` | — | .env override (value not compared) | dynamic |
| `database_echo` | infrastructure | `AGENT_DATABASE_ECHO` | — | code default | 1 |
| `database_url` | infrastructure | `AGENT_DATABASE_URL` | — | .env override (value not compared) | 8 |
| `debug` | infrastructure | `APP_DEBUG` | — | code default | 67 |
| `deployment_profile` | infrastructure | `AGENT_DEPLOYMENT_PROFILE` | — | .env override, differs from default | 2 |
| `elasticsearch_index_prefix` | infrastructure | `AGENT_ELASTICSEARCH_INDEX_PREFIX` | — | code default | 7 |
| `elasticsearch_url` | infrastructure | `AGENT_ELASTICSEARCH_URL` | — | .env override (value not compared) | 6 |
| `environment` | infrastructure | `AGENT_ENVIRONMENT` | — | code default | 24 |
| `event_bus_dead_letter_stream` | infrastructure | `AGENT_EVENT_BUS_DEAD_LETTER_STREAM` | — | .env override, equals default | 1 |
| `event_bus_redis_url` | infrastructure | `AGENT_EVENT_BUS_REDIS_URL` | — | .env override (value not compared) | 2 |
| `experimental_openai_base_llm_http_handler` | infrastructure | `EXPERIMENTAL_OPENAI_BASE_LLM_HTTP_HANDLER` | — | code default | dynamic |
| `extra_permission_rules_path` | infrastructure | `AGENT_EXTRA_PERMISSION_RULES_PATH` | — | code default | 1 |
| `gateway_access_config` | infrastructure | `AGENT_GATEWAY_ACCESS_CONFIG` | — | code default | 1 |
| `gateway_auth_enabled` | infrastructure | `AGENT_GATEWAY_AUTH_ENABLED` | — | .env override, differs from default | 3 |
| `gateway_mount_local` | infrastructure | `AGENT_GATEWAY_MOUNT_LOCAL` | — | code default | 3 |
| `governance_config_path` | infrastructure | `AGENT_GOVERNANCE_CONFIG_PATH` | — | code default | 4 |
| `linear_personal_agent_label_id` | infrastructure | `AGENT_LINEAR_PERSONAL_AGENT_LABEL_ID` | — | code default | 2 |
| `linear_promotion_project` | infrastructure | `AGENT_LINEAR_PROMOTION_PROJECT` | — | code default | 2 |
| `linear_team_name` | infrastructure | `AGENT_LINEAR_TEAM_NAME` | — | code default | 5 |
| `local_fallback_embedding_endpoint` | infrastructure | `AGENT_LOCAL_FALLBACK_EMBEDDING_ENDPOINT` | — | code default | 1 |
| `local_fallback_embedding_model` | infrastructure | `AGENT_LOCAL_FALLBACK_EMBEDDING_MODEL` | — | .env override, differs from default | 2 |
| `log_dir` | infrastructure | `AGENT_LOG_DIR` | — | code default | 8 |
| `log_format` | infrastructure | `APP_LOG_FORMAT` | — | code default | 1 |
| `log_level` | infrastructure | `APP_LOG_LEVEL` | — | code default | 2 |
| `managed_database_url` | infrastructure | `AGENT_MANAGED_DATABASE_URL` | — | code default | dynamic |
| `managed_elasticsearch_url` | infrastructure | `AGENT_MANAGED_ELASTICSEARCH_URL` | — | code default | dynamic |
| `managed_embedding_endpoint` | infrastructure | `AGENT_MANAGED_EMBEDDING_ENDPOINT` | — | .env override (value not compared) | 1 |
| `managed_embedding_model` | infrastructure | `AGENT_MANAGED_EMBEDDING_MODEL` | — | .env override, equals default | 2 |
| `managed_neo4j_uri` | infrastructure | `AGENT_MANAGED_NEO4J_URI` | — | code default | dynamic |
| `managed_reranker_endpoint` | infrastructure | `AGENT_MANAGED_RERANKER_ENDPOINT` | — | code default | dynamic |
| `managed_slm_endpoint` | infrastructure | `AGENT_MANAGED_SLM_ENDPOINT` | — | code default | dynamic |
| `mcp_gateway_command` | infrastructure | `AGENT_MCP_GATEWAY_COMMAND` | — | .env override (value not compared) | 2 |
| `neo4j_uri` | infrastructure | `AGENT_NEO4J_URI` | — | .env override (value not compared) | 3 |
| `neo4j_user` | infrastructure | `AGENT_NEO4J_USER` | — | .env override (value not compared) | 2 |
| `otel_exporter_endpoint` | infrastructure | `AGENT_OTEL_EXPORTER_ENDPOINT` | — | .env override (value not compared) | 3 |
| `owner_storage_allowlist` | infrastructure | `AGENT_OWNER_STORAGE_ALLOWLIST` | — | code default | dynamic |
| `perplexity_base_url` | infrastructure | `AGENT_PERPLEXITY_BASE_URL` | — | code default | 1 |
| `project_name` | infrastructure | `AGENT_PROJECT_NAME` | — | code default | 3 |
| `pwa_public_origin` | infrastructure | `AGENT_PWA_PUBLIC_ORIGIN` | — | .env override, differs from default | 1 |
| `r2_bucket_name` | infrastructure | `AGENT_R2_BUCKET_NAME` | — | .env override (value not compared) | 2 |
| `r2_endpoint_url` | infrastructure | `AGENT_R2_ENDPOINT_URL` | — | .env override (value not compared) | 2 |
| `r2_region` | infrastructure | `AGENT_R2_REGION` | — | .env override (value not compared) | 1 |
| `sandbox_image` | infrastructure | `AGENT_SANDBOX_IMAGE` | — | code default | 1 |
| `sandbox_network` | infrastructure | `AGENT_SANDBOX_NETWORK` | — | code default | 1 |
| `sandbox_scratch_root` | infrastructure | `AGENT_SANDBOX_SCRATCH_ROOT` | — | code default | 1 |
| `searxng_base_url` | infrastructure | `AGENT_SEARXNG_BASE_URL` | — | .env override (value not compared) | 1 |
| `service_host` | infrastructure | `AGENT_SERVICE_HOST` | — | code default | 1 |
| `service_port` | infrastructure | `AGENT_SERVICE_PORT` | — | code default | 1 |
| `service_url` | infrastructure | `SERVICE_URL` | — | code default | 1 |
| `slm_base_url` | infrastructure | `AGENT_SLM_BASE_URL` | — | .env override (value not compared) | 4 |
| `substrate_profile` | infrastructure | `AGENT_SUBSTRATE_PROFILE` | — | .env override, differs from default | 1 |
| `sysgraph_database_url` | infrastructure | `AGENT_SYSGRAPH_DATABASE_URL` | — | .env override (value not compared) | 4 |
| `tempo_url` | infrastructure | `AGENT_TEMPO_URL` | — | .env override (value not compared) | 1 |
| `use_service_mode` | infrastructure | `AGENT_USE_SERVICE_MODE` | — | code default | **none in `src/`** |
| `version` | infrastructure | `AGENT_VERSION` | — | code default | 29 |
| `ws_event_queue_size` | infrastructure | `AGENT_WS_EVENT_QUEUE_SIZE` | — | code default | 1 |
| `ws_event_ttl_hours` | infrastructure | `AGENT_WS_EVENT_TTL_HOURS` | — | code default | 1 |
| `ws_max_message_size` | infrastructure | `AGENT_WS_MAX_MESSAGE_SIZE` | — | code default | 1 |
| `ws_ping_timeout_seconds` | infrastructure | `AGENT_WS_PING_TIMEOUT_SECONDS` | — | code default | 1 |
| `ws_rate_limit_per_second` | infrastructure | `AGENT_WS_RATE_LIMIT_PER_SECOND` | — | code default | 1 |
| `ws_ticket_ttl_seconds` | infrastructure | `AGENT_WS_TICKET_TTL_SECONDS` | — | code default | 2 |
| `anthropic_api_key` | secrets | `AGENT_ANTHROPIC_API_KEY` | — | .env override (value not compared) | 1 |
| `artifact_resolve_internal_token` | secrets | `AGENT_ARTIFACT_RESOLVE_INTERNAL_TOKEN` | — | .env override (value not compared) | 1 |
| `linear_api_key` | secrets | `AGENT_LINEAR_API_KEY` | — | .env override (value not compared) | 3 |
| `managed_embedding_token` | secrets | `AGENT_MANAGED_EMBEDDING_TOKEN` | — | .env override (value not compared) | 1 |
| `neo4j_password` | secrets | `AGENT_NEO4J_PASSWORD` | — | .env override (value not compared) | 2 |
| `openai_api_key` | secrets | `AGENT_OPENAI_API_KEY` | — | .env override (value not compared) | 1 |
| `perplexity_api_key` | secrets | `AGENT_PERPLEXITY_API_KEY` | — | .env override (value not compared) | 1 |
| `r2_access_key_id` | secrets | `AGENT_R2_ACCESS_KEY_ID` | — | .env override (value not compared) | 2 |
| `r2_secret_access_key` | secrets | `AGENT_R2_SECRET_ACCESS_KEY` | — | .env override (value not compared) | 2 |
| `scaleway_secret_key` | secrets | `AGENT_SCALEWAY_SECRET_KEY` | — | .env override (value not compared) | dynamic |
| `voyage_api_key` | secrets | `AGENT_VOYAGE_API_KEY` | — | .env override (value not compared) | 1 |
