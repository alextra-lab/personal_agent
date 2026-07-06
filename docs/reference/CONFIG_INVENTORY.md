# Configuration Inventory — canonical parameter × location × default × reader × validation × divergence

> **Ticket:** [FRE-648](https://linear.app/frenchforest/issue/FRE-648) — ADR-0099 **stage 0**, the "first deliverable."
> **Backing ADR:** [ADR-0099 — Configuration Management & Validation](../architecture_decisions/ADR-0099-configuration-management-and-validation.md) (single-source role matrix · profile-divergence policy · cross-config validator).
> **Generated:** 2026-07-03 · **Status:** point-in-time audit of `origin/main` @ FRE-648.
> **Reproduce / verify:** `uv run python scripts/audit/config_inventory.py verify` (asserts this doc covers every `AppConfig` field + documented `AGENT_*` key). Regenerate the AppConfig section with `… generate`.

This is the canonical configuration inventory that seeds ADR-0099's role matrix (C1/C2) and the cross-config guard (C1/D4). It has one **machine-generated** section (§1 `AppConfig`, produced by `scripts/audit/config_inventory.py`) and hand-curated sections for the surfaces that require judgment (role matrix, model-definition drift, profiles, governance, compose, secrets, findings).

---

## §0 — Methodology, scope, and reconciliation with ADR-0099

**Method.** The `AppConfig` section (§1) is produced by introspecting `AppConfig.model_fields` at the class level (no instantiation → no env/secrets needed) and cross-referencing the `AGENT_*` keys documented in `.env.example`. The env-var binding rule (`AGENT_<FIELD>` always valid; a declared `alias` is an *additional* accepted spelling) was **empirically verified**, not assumed — see the finding in §10. Model-YAML, profile, governance, and compose sections were read directly from the files.

**Surfaces covered (current reality on disk, not the ADR's 2026-06-28 snapshot):**

| Surface | Files | §|
|---|---|---|
| `AppConfig` typed scalars | `src/personal_agent/config/settings.py` (277 fields) | §1 |
| Model-definition YAMLs **with** role headers | `models.yaml`, `models.cloud.yaml`, `models-baseline.yaml`, `models.benchmark-8b.yaml`, `models.benchmark-4b.yaml`, `models.benchmark-4b-f16.yaml` | §2–§3 |
| Model-definition YAML **without** role headers | `models.medium.yaml` | §2–§3 |
| Model **policy** YAML (different schema) | `config/governance/models.yaml` | §5 |
| Deployment profiles (ADR-0044) | `config/profiles/{local,cloud}.yaml` | §4 |
| Governance | `config/governance/{budget,modes,safety,tools}.yaml` | §5 |
| Env template | `.env.example`, `docker/mcp/mcp-secrets.env.example` | §1, §7 |
| Compose `environment:` | `docker-compose{,.cloud,.eval,.test}.yml` | §6 |
| Other `config/` | `gateway_access.yaml`, `cloud-sim/Caddyfile`, `kibana/*`, `artifact_lib_*.json` | §5 (noted) |

**⚠️ Reconciliation — ADR-0099's Context table is already stale (a finding in itself):**

- ADR-0099 enumerates **`models.eval.yaml`** as one of the four role-bearing YAMLs and cites it as the site of the headline **`gpt-5.4-nano` → `id: gpt-4o-mini`** definition-drift example. **`models.eval.yaml` no longer exists** on `main` (removed after the ADR was written), and **no file currently defines `gpt-5.4-nano` with `id: gpt-4o-mini`** — every current file gives `gpt-5.4-nano` the id `gpt-5.4-nano`. The ADR's specific headline example is **historical**.
- Three **`models.benchmark-*.yaml`** files (added 2026-06-30, after the ADR) are role-bearing YAMLs the ADR does not mention.
- ADR-0099 references **`mcp-secrets.env`**; the actual file is **`docker/mcp/mcp-secrets.env.example`**.
- ADR-0099's drift table claims **`embedding`/`reranker` are "consistent"** across profiles; the `reranker` model in fact **diverges local vs cloud** (0.6B vs 4B — see §3).

The audit therefore inventories **current reality** and records the *current* live drift (the `claude_sonnet` definition split, §3) as the concrete instance the D4 guard must catch, in place of the ADR's now-historical `gpt-4o-mini` example.

---

## §1 — `AppConfig` (typed scalar authority, ADR-0007)

_Machine-generated — regenerate with `uv run python scripts/audit/config_inventory.py generate` and paste between the AUTOGEN markers._

<!-- AUTOGEN:AppConfig START — regenerate via scripts/audit/config_inventory.py generate -->

**286 typed scalar/path parameters** live in `src/personal_agent/config/settings.py` (`AppConfig`, a pydantic `BaseSettings` with `env_prefix="AGENT_"`). Every field is read through the process-wide `from personal_agent.config import settings` singleton (`settings.<field>`), so the **reader** is uniformly that accessor; **validation** is pydantic type coercion at load (`AppConfig()` raises `ValidationError` on a bad value). The **Env var** column shows `AGENT_<FIELD>` (the prefix+name form, **always valid**); where a field also declares an `alias=`, that alias is shown after `·` as an **additional** accepted spelling — empirically both bind (e.g. `debug` accepts `AGENT_DEBUG` *and* `APP_DEBUG`). A field's default is overridable by either; the *profile-divergence* for scalars is the set of `docker-compose*.yml` `environment:` blocks that override it (see §8).

| # | Field (`settings.X`) | Env var | Type | Default | Secret | In `.env.example` |
|---|---|---|---|---|---|---|
| 1 | `agent_id` | `AGENT_AGENT_ID` | `str` | `'seshat-local'` |  | ✅ |
| 2 | `agent_owner_email` | `AGENT_AGENT_OWNER_EMAIL` · `AGENT_OWNER_EMAIL` | `str \| None` | `None` |  | ✅ |
| 3 | `allow_test_writes_to_prod_substrate` | `AGENT_ALLOW_TEST_WRITES_TO_PROD_SUBSTRATE` | `bool` | `False` |  | ✅ |
| 4 | `allowed_ws_origins` | `AGENT_ALLOWED_WS_ORIGINS` | `list` | `['https://<deployment-host>', 'https://<deployment-host>', 'http://localhost:3000']` |  | — |
| 5 | `anthropic_api_key` | `AGENT_ANTHROPIC_API_KEY` | `str \| None` | 🔒 redacted (secret — `.env` only) | 🔑 | ✅ |
| 6 | `approval_timeout_seconds` | `AGENT_APPROVAL_TIMEOUT_SECONDS` | `float` | `60.0` |  | ✅ |
| 7 | `approval_ui_enabled` | `AGENT_APPROVAL_UI_ENABLED` | `bool` | `False` |  | ✅ |
| 8 | `artifact_decomposition_enabled` | `AGENT_ARTIFACT_DECOMPOSITION_ENABLED` | `bool` | `False` |  | — |
| 9 | `artifact_draft_max_tokens` | `AGENT_ARTIFACT_DRAFT_MAX_TOKENS` | `int` | `32768` |  | ✅ |
| 10 | `artifact_envelope_probe_enabled` | `AGENT_ARTIFACT_ENVELOPE_PROBE_ENABLED` | `bool` | `True` |  | — |
| 11 | `artifact_envelope_probe_timeout_s` | `AGENT_ARTIFACT_ENVELOPE_PROBE_TIMEOUT_S` | `float` | `2.0` |  | — |
| 12 | `artifact_resolve_internal_token` | `AGENT_ARTIFACT_RESOLVE_INTERNAL_TOKEN` | `str \| None` | 🔒 redacted (secret — `.env` only) | 🔑 | ✅ |
| 13 | `artifacts_public_base_url` | `AGENT_ARTIFACTS_PUBLIC_BASE_URL` | `str \| None` | `None` |  | ✅ |
| 14 | `attachment_cost_confirmation_threshold_usd` | `AGENT_ATTACHMENT_COST_CONFIRMATION_THRESHOLD_USD` | `float` | `0.5` |  | — |
| 15 | `attachment_image_max_bytes` | `AGENT_ATTACHMENT_IMAGE_MAX_BYTES` | `int` | `5242880` |  | — |
| 16 | `attachment_image_max_pixels` | `AGENT_ATTACHMENT_IMAGE_MAX_PIXELS` | `int` | `1568` |  | — |
| 17 | `attachment_max_images_per_turn` | `AGENT_ATTACHMENT_MAX_IMAGES_PER_TURN` | `int` | `4` |  | — |
| 18 | `attachment_max_total_payload_bytes` | `AGENT_ATTACHMENT_MAX_TOTAL_PAYLOAD_BYTES` | `int` | `15728640` |  | — |
| 19 | `brainstem_sensor_poll_interval_seconds` | `AGENT_BRAINSTEM_SENSOR_POLL_INTERVAL_SECONDS` | `float` | `5.0` |  | ✅ |
| 20 | `cache_frozen_accum_max_ratio` | `AGENT_CACHE_FROZEN_ACCUM_MAX_RATIO` | `float` | `0.5` |  | — |
| 21 | `cache_frozen_layout_enabled` | `AGENT_CACHE_FROZEN_LAYOUT_ENABLED` | `bool` | `True` |  | ✅ |
| 22 | `cache_quality_token_weight` | `AGENT_CACHE_QUALITY_TOKEN_WEIGHT` | `float` | `4000.0` |  | — |
| 23 | `cache_reset_min_run_turns_cloud` | `AGENT_CACHE_RESET_MIN_RUN_TURNS_CLOUD` | `int` | `4` |  | — |
| 24 | `cache_reset_min_run_turns_local` | `AGENT_CACHE_RESET_MIN_RUN_TURNS_LOCAL` | `int` | `12` |  | — |
| 25 | `captains_log_index_prefix` | `AGENT_CAPTAINS_LOG_INDEX_PREFIX` | `str` | `'agent-captains'` |  | — |
| 26 | `cf_access_aud` | `AGENT_CF_ACCESS_AUD` · `CF_ACCESS_AUD` | `str \| None` | `None` |  | ✅ |
| 27 | `cf_access_client_id` | `AGENT_CF_ACCESS_CLIENT_ID` · `CF_ACCESS_CLIENT_ID` | `str \| None` | `None` |  | ✅ |
| 28 | `cf_access_client_secret` | `AGENT_CF_ACCESS_CLIENT_SECRET` · `CF_ACCESS_CLIENT_SECRET` | `str \| None` | 🔒 redacted (secret — `.env` only) | 🔑 | ✅ |
| 29 | `cf_access_team_domain` | `AGENT_CF_ACCESS_TEAM_DOMAIN` · `CF_ACCESS_TEAM_DOMAIN` | `str \| None` | `None` |  | ✅ |
| 30 | `cloud_weekly_budget_usd` | `AGENT_CLOUD_WEEKLY_BUDGET_USD` | `float` | `5.0` |  | ✅ |
| 31 | `consolidator_max_extraction_attempts` | `AGENT_CONSOLIDATOR_MAX_EXTRACTION_ATTEMPTS` | `int` | `5` |  | — |
| 32 | `context_budget_comfortable_tokens` | `AGENT_CONTEXT_BUDGET_COMFORTABLE_TOKENS` | `int` | `64000` |  | — |
| 33 | `context_budget_generation_reserve_tokens` | `AGENT_CONTEXT_BUDGET_GENERATION_RESERVE_TOKENS` | `int` | `32768` |  | — |
| 34 | `context_budget_max_tokens` | `AGENT_CONTEXT_BUDGET_MAX_TOKENS` | `int` | `120000` |  | — |
| 35 | `context_compression_enabled` | `AGENT_CONTEXT_COMPRESSION_ENABLED` | `bool` | `True` |  | ✅ |
| 36 | `context_compression_threshold_ratio` | `AGENT_CONTEXT_COMPRESSION_THRESHOLD_RATIO` | `float` | `0.65` |  | ✅ |
| 37 | `context_quality_governance_budget_reduction` | `AGENT_CONTEXT_QUALITY_GOVERNANCE_BUDGET_REDUCTION` | `float` | `0.15` |  | — |
| 38 | `context_quality_governance_enabled` | `AGENT_CONTEXT_QUALITY_GOVERNANCE_ENABLED` | `bool` | `False` |  | — |
| 39 | `context_quality_governance_threshold` | `AGENT_CONTEXT_QUALITY_GOVERNANCE_THRESHOLD` | `int` | `2` |  | — |
| 40 | `context_quality_stream_enabled` | `AGENT_CONTEXT_QUALITY_STREAM_ENABLED` | `bool` | `True` |  | — |
| 41 | `context_window_max_tokens` | `AGENT_CONTEXT_WINDOW_MAX_TOKENS` | `int` | `96000` |  | ✅ |
| 42 | `conversation_context_strategy` | `AGENT_CONVERSATION_CONTEXT_STRATEGY` | `str` | `'truncate'` |  | ✅ |
| 43 | `conversation_max_history_messages` | `AGENT_CONVERSATION_MAX_HISTORY_MESSAGES` | `int` | `10` |  | ✅ |
| 44 | `cors_allowed_origins` | `AGENT_CORS_ALLOWED_ORIGINS` | `list` | `['http://localhost:3000', 'https://<deployment-host>', 'https://<deployment-host>']` |  | — |
| 45 | `data_lifecycle_enabled` | `AGENT_DATA_LIFECYCLE_ENABLED` | `bool` | `True` |  | ✅ |
| 46 | `database_admin_url` | `AGENT_DATABASE_ADMIN_URL` | `str` | `'postgresql+asyncpg://<redacted>@localhost:5432/personal_agent'` |  | ✅ |
| 47 | `database_echo` | `AGENT_DATABASE_ECHO` | `bool` | `False` |  | ✅ |
| 48 | `database_url` | `AGENT_DATABASE_URL` | `str` | `'postgresql+asyncpg://<redacted>@localhost:5432/personal_agent'` |  | ✅ |
| 49 | `debug` | `AGENT_DEBUG` · `APP_DEBUG` | `bool` | `False` |  | ✅ |
| 50 | `dedup_similarity_threshold` | `AGENT_DEDUP_SIMILARITY_THRESHOLD` | `float` | `0.92` |  | ✅ |
| 51 | `default_profile` | `AGENT_DEFAULT_PROFILE` | `str` | `'local'` |  | — |
| 52 | `disk_usage_alert_percent` | `AGENT_DISK_USAGE_ALERT_PERCENT` | `float` | `80.0` |  | ✅ |
| 53 | `elasticsearch_index_prefix` | `AGENT_ELASTICSEARCH_INDEX_PREFIX` | `str` | `'agent-logs'` |  | ✅ |
| 54 | `elasticsearch_url` | `AGENT_ELASTICSEARCH_URL` | `str` | `'http://localhost:9200'` |  | ✅ |
| 55 | `embedding_backfill_enabled` | `AGENT_EMBEDDING_BACKFILL_ENABLED` | `bool` | `True` |  | — |
| 56 | `embedding_batch_size` | `AGENT_EMBEDDING_BATCH_SIZE` | `int` | `20` |  | ✅ |
| 57 | `embedding_dimensions` | `AGENT_EMBEDDING_DIMENSIONS` | `int` | `1024` |  | ✅ |
| 58 | `enable_memory_graph` | `AGENT_ENABLE_MEMORY_GRAPH` | `bool` | `False` |  | ✅ |
| 59 | `enable_reasoning_role` | `AGENT_ENABLE_REASONING_ROLE` | `bool` | `True` |  | ✅ |
| 60 | `enable_second_brain` | `AGENT_ENABLE_SECOND_BRAIN` | `bool` | `False` |  | ✅ |
| 61 | `entity_extraction_fewshot_exemplars_enabled` | `AGENT_ENTITY_EXTRACTION_FEWSHOT_EXEMPLARS_ENABLED` | `bool` | `False` |  | — |
| 62 | `entity_extraction_timeout_seconds` | `AGENT_ENTITY_EXTRACTION_TIMEOUT_SECONDS` | `int` | `90` |  | — |
| 63 | `environment` | `AGENT_ENVIRONMENT` | `Environment` | `<Environment.DEVELOPMENT: 'development'>` |  | — |
| 64 | `error_monitor_enabled` | `AGENT_ERROR_MONITOR_ENABLED` | `bool` | `True` |  | ✅ |
| 65 | `error_monitor_max_patterns_per_scan` | `AGENT_ERROR_MONITOR_MAX_PATTERNS_PER_SCAN` | `int` | `50` |  | ✅ |
| 66 | `error_monitor_min_occurrences` | `AGENT_ERROR_MONITOR_MIN_OCCURRENCES` | `int` | `5` |  | ✅ |
| 67 | `error_monitor_window_hours` | `AGENT_ERROR_MONITOR_WINDOW_HOURS` | `int` | `24` |  | ✅ |
| 68 | `event_bus_ack_timeout_seconds` | `AGENT_EVENT_BUS_ACK_TIMEOUT_SECONDS` | `int` | `300` |  | ✅ |
| 69 | `event_bus_consumer_poll_interval_ms` | `AGENT_EVENT_BUS_CONSUMER_POLL_INTERVAL_MS` | `int` | `100` |  | ✅ |
| 70 | `event_bus_dead_letter_stream` | `AGENT_EVENT_BUS_DEAD_LETTER_STREAM` | `str` | `'stream:dead_letter'` |  | ✅ |
| 71 | `event_bus_enabled` | `AGENT_EVENT_BUS_ENABLED` | `bool` | `False` |  | ✅ |
| 72 | `event_bus_max_retries` | `AGENT_EVENT_BUS_MAX_RETRIES` | `int` | `3` |  | ✅ |
| 73 | `event_bus_redis_url` | `AGENT_EVENT_BUS_REDIS_URL` | `str` | `'redis://localhost:6379/0'` |  | ✅ |
| 74 | `expansion_budget_max` | `AGENT_EXPANSION_BUDGET_MAX` | `int` | `3` |  | — |
| 75 | `failure_path_reflection_enabled` | `AGENT_FAILURE_PATH_REFLECTION_ENABLED` | `bool` | `False` |  | ✅ |
| 76 | `feedback_defer_revisit_days` | `AGENT_FEEDBACK_DEFER_REVISIT_DAYS` | `int` | `90` |  | ✅ |
| 77 | `feedback_max_reevaluations` | `AGENT_FEEDBACK_MAX_REEVALUATIONS` | `int` | `2` |  | ✅ |
| 78 | `feedback_polling_enabled` | `AGENT_FEEDBACK_POLLING_ENABLED` | `bool` | `True` |  | ✅ |
| 79 | `feedback_polling_hour_utc` | `AGENT_FEEDBACK_POLLING_HOUR_UTC` | `int` | `7` |  | ✅ |
| 80 | `feedback_suppression_days` | `AGENT_FEEDBACK_SUPPRESSION_DAYS` | `int` | `30` |  | ✅ |
| 81 | `freshness_backfill_confirm` | `AGENT_FRESHNESS_BACKFILL_CONFIRM` | `bool` | `False` |  | ✅ |
| 82 | `freshness_cold_threshold_days` | `AGENT_FRESHNESS_COLD_THRESHOLD_DAYS` | `float` | `180.0` |  | ✅ |
| 83 | `freshness_consumer_batch_max_events` | `AGENT_FRESHNESS_CONSUMER_BATCH_MAX_EVENTS` | `int` | `50` |  | ✅ |
| 84 | `freshness_consumer_batch_window_seconds` | `AGENT_FRESHNESS_CONSUMER_BATCH_WINDOW_SECONDS` | `float` | `5.0` |  | ✅ |
| 85 | `freshness_dormant_entity_proposal_threshold` | `AGENT_FRESHNESS_DORMANT_ENTITY_PROPOSAL_THRESHOLD` | `int` | `10` |  | ✅ |
| 86 | `freshness_dormant_relationship_proposal_threshold` | `AGENT_FRESHNESS_DORMANT_RELATIONSHIP_PROPOSAL_THRESHOLD` | `int` | `10` |  | ✅ |
| 87 | `freshness_enabled` | `AGENT_FRESHNESS_ENABLED` | `bool` | `False` |  | ✅ |
| 88 | `freshness_frequency_boost_alpha` | `AGENT_FRESHNESS_FREQUENCY_BOOST_ALPHA` | `float` | `0.1` |  | ✅ |
| 89 | `freshness_frequency_boost_max` | `AGENT_FRESHNESS_FREQUENCY_BOOST_MAX` | `float` | `1.5` |  | ✅ |
| 90 | `freshness_half_life_days` | `AGENT_FRESHNESS_HALF_LIFE_DAYS` | `float` | `30.0` |  | ✅ |
| 91 | `freshness_never_accessed_noise_days` | `AGENT_FRESHNESS_NEVER_ACCESSED_NOISE_DAYS` | `float` | `30.0` |  | ✅ |
| 92 | `freshness_relevance_weight` | `AGENT_FRESHNESS_RELEVANCE_WEIGHT` | `float` | `0.15` |  | ✅ |
| 93 | `freshness_review_schedule_cron` | `AGENT_FRESHNESS_REVIEW_SCHEDULE_CRON` | `str` | `'0 3 * * 0'` |  | ✅ |
| 94 | `freshness_tier_factors` | `AGENT_FRESHNESS_TIER_FACTORS` | `dict` | `{'warm': 1.0, 'cooling': 0.85, 'cold': 0.6, 'dormant': 0.3}` |  | — |
| 95 | `freshness_tier_reranking_enabled` | `AGENT_FRESHNESS_TIER_RERANKING_ENABLED` | `bool` | `True` |  | — |
| 96 | `gateway_access_config` | `AGENT_GATEWAY_ACCESS_CONFIG` | `str` | `'config/gateway_access.yaml'` |  | — |
| 97 | `gateway_auth_enabled` | `AGENT_GATEWAY_AUTH_ENABLED` | `bool` | `False` |  | ✅ |
| 98 | `gateway_mount_local` | `AGENT_GATEWAY_MOUNT_LOCAL` | `bool` | `True` |  | — |
| 99 | `governance_config_path` | `AGENT_GOVERNANCE_CONFIG_PATH` | `Path` | `PosixPath('config/governance')` |  | ✅ |
| 100 | `graph_quality_governance_enabled` | `AGENT_GRAPH_QUALITY_GOVERNANCE_ENABLED` | `bool` | `False` |  | — |
| 101 | `graph_quality_stream_enabled` | `AGENT_GRAPH_QUALITY_STREAM_ENABLED` | `bool` | `True` |  | — |
| 102 | `insights_daily_run_hour_utc` | `AGENT_INSIGHTS_DAILY_RUN_HOUR_UTC` | `int` | `6` |  | ✅ |
| 103 | `insights_enabled` | `AGENT_INSIGHTS_ENABLED` | `bool` | `True` |  | ✅ |
| 104 | `insights_weekly_day` | `AGENT_INSIGHTS_WEEKLY_DAY` | `int` | `6` |  | ✅ |
| 105 | `insights_weekly_run_hour_utc` | `AGENT_INSIGHTS_WEEKLY_RUN_HOUR_UTC` | `int` | `9` |  | ✅ |
| 106 | `insights_wiring_enabled` | `AGENT_INSIGHTS_WIRING_ENABLED` | `bool` | `True` |  | ✅ |
| 107 | `issue_budget_threshold` | `AGENT_ISSUE_BUDGET_THRESHOLD` | `int` | `200` |  | ✅ |
| 108 | `joinability_probe_enabled` | `AGENT_JOINABILITY_PROBE_ENABLED` | `bool` | `True` |  | — |
| 109 | `joinability_probe_index_prefix` | `AGENT_JOINABILITY_PROBE_INDEX_PREFIX` | `str` | `'agent-monitors-joinability'` |  | — |
| 110 | `joinability_probe_interval_seconds` | `AGENT_JOINABILITY_PROBE_INTERVAL_SECONDS` | `int` | `3600` |  | — |
| 111 | `joinability_probe_window_hours` | `AGENT_JOINABILITY_PROBE_WINDOW_HOURS` | `int` | `24` |  | — |
| 112 | `lexical_arm_enabled` | `AGENT_LEXICAL_ARM_ENABLED` | `bool` | `False` |  | — |
| 113 | `linear_agent_rate_limit_per_day` | `AGENT_LINEAR_AGENT_RATE_LIMIT_PER_DAY` | `int` | `10` |  | ✅ |
| 114 | `linear_api_key` | `AGENT_LINEAR_API_KEY` | `str \| None` | 🔒 redacted (secret — `.env` only) | 🔑 | ✅ |
| 115 | `linear_personal_agent_label_id` | `AGENT_LINEAR_PERSONAL_AGENT_LABEL_ID` | `str \| None` | `'25004aac-3b32-4fa4-bdc2-55ff348ea842'` |  | ✅ |
| 116 | `linear_promotion_project` | `AGENT_LINEAR_PROMOTION_PROJECT` | `str` | `'2.3 Homeostasis & Feedback'` |  | ✅ |
| 117 | `linear_team_name` | `AGENT_LINEAR_TEAM_NAME` | `str` | `'FrenchForest'` |  | ✅ |
| 118 | `llm_append_no_think_to_tool_prompts` | `AGENT_LLM_APPEND_NO_THINK_TO_TOOL_PROMPTS` | `bool` | `False` |  | ✅ |
| 119 | `llm_base_url` | `AGENT_LLM_BASE_URL` | `str` | `'http://127.0.0.1:1234/v1'` |  | ✅ |
| 120 | `llm_max_retries` | `AGENT_LLM_MAX_RETRIES` | `int` | `3` |  | ✅ |
| 121 | `llm_no_think_suffix` | `AGENT_LLM_NO_THINK_SUFFIX` | `str` | `'/no_think'` |  | ✅ |
| 122 | `llm_timeout_seconds` | `AGENT_LLM_TIMEOUT_SECONDS` | `int` | `120` |  | ✅ |
| 123 | `location_enabled` | `AGENT_LOCATION_ENABLED` | `bool` | `False` |  | ✅ |
| 124 | `location_precision` | `AGENT_LOCATION_PRECISION` | `str` | `'precise'` |  | — |
| 125 | `log_dir` | `AGENT_LOG_DIR` | `Path` | `PosixPath('telemetry/logs')` |  | ✅ |
| 126 | `log_format` | `AGENT_LOG_FORMAT` · `APP_LOG_FORMAT` | `str` | `'json'` |  | ✅ |
| 127 | `log_level` | `AGENT_LOG_LEVEL` · `APP_LOG_LEVEL` | `str` | `'INFO'` |  | ✅ |
| 128 | `managed_database_url` | `AGENT_MANAGED_DATABASE_URL` | `str \| None` | `None` |  | ✅ |
| 129 | `managed_elasticsearch_url` | `AGENT_MANAGED_ELASTICSEARCH_URL` | `str \| None` | `None` |  | ✅ |
| 130 | `managed_embedding_endpoint` | `AGENT_MANAGED_EMBEDDING_ENDPOINT` | `str \| None` | `None` |  | ✅ |
| 131 | `managed_neo4j_uri` | `AGENT_MANAGED_NEO4J_URI` | `str \| None` | `None` |  | ✅ |
| 132 | `managed_reranker_endpoint` | `AGENT_MANAGED_RERANKER_ENDPOINT` | `str \| None` | `None` |  | ✅ |
| 133 | `managed_slm_endpoint` | `AGENT_MANAGED_SLM_ENDPOINT` | `str \| None` | `None` |  | ✅ |
| 134 | `mcp_gateway_command` | `AGENT_MCP_GATEWAY_COMMAND` | `list` | `['docker', 'mcp', 'gateway', 'run']` |  | ✅ |
| 135 | `mcp_gateway_enabled` | `AGENT_MCP_GATEWAY_ENABLED` | `bool` | `False` |  | ✅ |
| 136 | `mcp_gateway_enabled_servers` | `AGENT_MCP_GATEWAY_ENABLED_SERVERS` | `list` | `[]` |  | ✅ |
| 137 | `mcp_gateway_timeout_seconds` | `AGENT_MCP_GATEWAY_TIMEOUT_SECONDS` | `int` | `60` |  | ✅ |
| 138 | `metrics_daemon_buffer_size` | `AGENT_METRICS_DAEMON_BUFFER_SIZE` | `int` | `720` |  | — |
| 139 | `metrics_daemon_es_emit_interval_seconds` | `AGENT_METRICS_DAEMON_ES_EMIT_INTERVAL_SECONDS` | `float` | `30.0` |  | — |
| 140 | `metrics_daemon_poll_interval_seconds` | `AGENT_METRICS_DAEMON_POLL_INTERVAL_SECONDS` | `float` | `5.0` |  | — |
| 141 | `metrics_sampled_stream_maxlen` | `AGENT_METRICS_SAMPLED_STREAM_MAXLEN` | `int` | `720` |  | — |
| 142 | `mode_calibration_anomaly_threshold` | `AGENT_MODE_CALIBRATION_ANOMALY_THRESHOLD` | `int` | `3` |  | — |
| 143 | `mode_controller_enabled` | `AGENT_MODE_CONTROLLER_ENABLED` | `bool` | `True` |  | — |
| 144 | `mode_evaluation_interval_seconds` | `AGENT_MODE_EVALUATION_INTERVAL_SECONDS` | `float` | `30.0` |  | — |
| 145 | `mode_window_size` | `AGENT_MODE_WINDOW_SIZE` | `int` | `12` |  | — |
| 146 | `model_config_path` | `AGENT_MODEL_CONFIG_PATH` | `Path` | `PosixPath('config/models.yaml')` |  | ✅ |
| 147 | `multipath_arm_top_k` | `AGENT_MULTIPATH_ARM_TOP_K` | `int` | `50` |  | — |
| 148 | `multipath_paraphrase_count` | `AGENT_MULTIPATH_PARAPHRASE_COUNT` | `int` | `3` |  | — |
| 149 | `multipath_recall_enabled` | `AGENT_MULTIPATH_RECALL_ENABLED` | `bool` | `False` |  | — |
| 150 | `multipath_rrf_k` | `AGENT_MULTIPATH_RRF_K` | `int` | `60` |  | — |
| 151 | `multiquery_arm_enabled` | `AGENT_MULTIQUERY_ARM_ENABLED` | `bool` | `False` |  | — |
| 152 | `neo4j_password` | `AGENT_NEO4J_PASSWORD` | `str` | 🔒 redacted (secret — `.env` only) | 🔑 | ✅ |
| 153 | `neo4j_uri` | `AGENT_NEO4J_URI` | `str` | `'bolt://localhost:7687'` |  | ✅ |
| 154 | `neo4j_user` | `AGENT_NEO4J_USER` | `str` | `'neo4j'` |  | ✅ |
| 155 | `openai_api_key` | `AGENT_OPENAI_API_KEY` | `str \| None` | 🔒 redacted (secret — `.env` only) | 🔑 | ✅ |
| 156 | `orchestration_mode` | `AGENT_ORCHESTRATION_MODE` | `str` | `'enforced'` |  | — |
| 157 | `orchestrator_max_concurrent_tasks` | `AGENT_ORCHESTRATOR_MAX_CONCURRENT_TASKS` | `int` | `5` |  | ✅ |
| 158 | `orchestrator_max_repeated_tool_calls` | `AGENT_ORCHESTRATOR_MAX_REPEATED_TOOL_CALLS` | `int` | `1` |  | ✅ |
| 159 | `orchestrator_max_tool_iterations` | `AGENT_ORCHESTRATOR_MAX_TOOL_ITERATIONS` | `int` | `25` |  | ✅ |
| 160 | `orchestrator_max_tool_iterations_by_task_type` | `AGENT_ORCHESTRATOR_MAX_TOOL_ITERATIONS_BY_TASK_TYPE` | `dict` | `{'conversational': 6, 'memory_recall': 8, 'analysis': 25, 'planning': 25, 'tool_use': 25, 'delegation': 25, 'self_improve': 25}` |  | — |
| 161 | `orchestrator_task_timeout_seconds` | `AGENT_ORCHESTRATOR_TASK_TIMEOUT_SECONDS` | `int` | `300` |  | ✅ |
| 162 | `owner_name` | `AGENT_OWNER_NAME` | `str` | `''` |  | ✅ |
| 163 | `perplexity_api_key` | `AGENT_PERPLEXITY_API_KEY` | `str \| None` | 🔒 redacted (secret — `.env` only) | 🔑 | ✅ |
| 164 | `perplexity_base_url` | `AGENT_PERPLEXITY_BASE_URL` | `str` | `'https://api.perplexity.ai'` |  | ✅ |
| 165 | `perplexity_timeout_seconds` | `AGENT_PERPLEXITY_TIMEOUT_SECONDS` | `int` | `90` |  | ✅ |
| 166 | `planner_timeout_seconds` | `AGENT_PLANNER_TIMEOUT_SECONDS` | `float` | `30.0` |  | — |
| 167 | `prefer_primitives_enabled` | `AGENT_PREFER_PRIMITIVES_ENABLED` · `AGENT_PREFER_PRIMITIVES` | `bool` | `True` |  | ✅ |
| 168 | `primitive_tools_enabled` | `AGENT_PRIMITIVE_TOOLS_ENABLED` | `bool` | `False` |  | ✅ |
| 169 | `proactive_memory_diminishing_score_floor` | `AGENT_PROACTIVE_MEMORY_DIMINISHING_SCORE_FLOOR` | `float` | `0.35` |  | ✅ |
| 170 | `proactive_memory_diminishing_score_gap` | `AGENT_PROACTIVE_MEMORY_DIMINISHING_SCORE_GAP` | `float` | `0.15` |  | ✅ |
| 171 | `proactive_memory_enabled` | `AGENT_PROACTIVE_MEMORY_ENABLED` | `bool` | `False` |  | ✅ |
| 172 | `proactive_memory_max_candidates` | `AGENT_PROACTIVE_MEMORY_MAX_CANDIDATES` | `int` | `10` |  | ✅ |
| 173 | `proactive_memory_max_injected_items` | `AGENT_PROACTIVE_MEMORY_MAX_INJECTED_ITEMS` | `int` | `5` |  | ✅ |
| 174 | `proactive_memory_max_tokens` | `AGENT_PROACTIVE_MEMORY_MAX_TOKENS` | `int` | `500` |  | ✅ |
| 175 | `proactive_memory_min_score` | `AGENT_PROACTIVE_MEMORY_MIN_SCORE` | `float` | `0.3` |  | ✅ |
| 176 | `proactive_memory_recency_half_life_days` | `AGENT_PROACTIVE_MEMORY_RECENCY_HALF_LIFE_DAYS` | `float` | `30.0` |  | ✅ |
| 177 | `proactive_memory_vector_top_k` | `AGENT_PROACTIVE_MEMORY_VECTOR_TOP_K` | `int` | `20` |  | ✅ |
| 178 | `proactive_memory_w_embedding` | `AGENT_PROACTIVE_MEMORY_W_EMBEDDING` | `float` | `0.45` |  | ✅ |
| 179 | `proactive_memory_w_entity` | `AGENT_PROACTIVE_MEMORY_W_ENTITY` | `float` | `0.25` |  | ✅ |
| 180 | `proactive_memory_w_recency` | `AGENT_PROACTIVE_MEMORY_W_RECENCY` | `float` | `0.2` |  | ✅ |
| 181 | `proactive_memory_w_topic` | `AGENT_PROACTIVE_MEMORY_W_TOPIC` | `float` | `0.1` |  | ✅ |
| 182 | `profiles_dir` | `AGENT_PROFILES_DIR` | `str` | `'config/profiles'` |  | — |
| 183 | `project_name` | `AGENT_PROJECT_NAME` | `str` | `'Personal Local AI Collaborator'` |  | ✅ |
| 184 | `promotion_initial_cap` | `AGENT_PROMOTION_INITIAL_CAP` | `int` | `5` |  | ✅ |
| 185 | `promotion_pipeline_enabled` | `AGENT_PROMOTION_PIPELINE_ENABLED` | `bool` | `True` |  | ✅ |
| 186 | `quality_monitor_anomaly_window_days` | `AGENT_QUALITY_MONITOR_ANOMALY_WINDOW_DAYS` | `int` | `7` |  | ✅ |
| 187 | `quality_monitor_daily_run_hour_utc` | `AGENT_QUALITY_MONITOR_DAILY_RUN_HOUR_UTC` | `int` | `5` |  | ✅ |
| 188 | `quality_monitor_enabled` | `AGENT_QUALITY_MONITOR_ENABLED` | `bool` | `True` |  | ✅ |
| 189 | `r2_access_key_id` | `AGENT_R2_ACCESS_KEY_ID` | `str \| None` | `None` |  | ✅ |
| 190 | `r2_bucket_name` | `AGENT_R2_BUCKET_NAME` | `str` | `'seshat-artifacts'` |  | ✅ |
| 191 | `r2_endpoint_url` | `AGENT_R2_ENDPOINT_URL` | `str \| None` | `None` |  | ✅ |
| 192 | `r2_region` | `AGENT_R2_REGION` | `str` | `'auto'` |  | ✅ |
| 193 | `r2_secret_access_key` | `AGENT_R2_SECRET_ACCESS_KEY` | `str \| None` | 🔒 redacted (secret — `.env` only) | 🔑 | ✅ |
| 194 | `recall_candidate_cap` | `AGENT_RECALL_CANDIDATE_CAP` | `int` | `500` |  | — |
| 195 | `recall_per_entity_turn_cap` | `AGENT_RECALL_PER_ENTITY_TURN_CAP` | `int` | `10` |  | — |
| 196 | `recall_similarity_floor` | `AGENT_RECALL_SIMILARITY_FLOOR` | `float` | `0.0` |  | — |
| 197 | `reflection_recall_enabled` | `AGENT_REFLECTION_RECALL_ENABLED` | `bool` | `True` |  | ✅ |
| 198 | `reflection_recall_max_results` | `AGENT_REFLECTION_RECALL_MAX_RESULTS` | `int` | `3` |  | ✅ |
| 199 | `reflection_recall_min_seen_count` | `AGENT_REFLECTION_RECALL_MIN_SEEN_COUNT` | `int` | `2` |  | ✅ |
| 200 | `reflection_recall_recency_days` | `AGENT_REFLECTION_RECALL_RECENCY_DAYS` | `int` | `14` |  | ✅ |
| 201 | `relevance_bounded_recall_enabled` | `AGENT_RELEVANCE_BOUNDED_RECALL_ENABLED` | `bool` | `False` |  | — |
| 202 | `request_monitoring_enabled` | `AGENT_REQUEST_MONITORING_ENABLED` | `bool` | `True` |  | ✅ |
| 203 | `request_monitoring_include_gpu` | `AGENT_REQUEST_MONITORING_INCLUDE_GPU` | `bool` | `True` |  | ✅ |
| 204 | `request_monitoring_interval_seconds` | `AGENT_REQUEST_MONITORING_INTERVAL_SECONDS` | `float` | `5.0` |  | ✅ |
| 205 | `reranker_enabled` | `AGENT_RERANKER_ENABLED` | `bool` | `True` |  | ✅ |
| 206 | `reranker_input_cap` | `AGENT_RERANKER_INPUT_CAP` | `int` | `25` |  | — |
| 207 | `reranker_top_k` | `AGENT_RERANKER_TOP_K` | `int` | `10` |  | ✅ |
| 208 | `route_trace_preview_chars` | `AGENT_ROUTE_TRACE_PREVIEW_CHARS` | `int` | `280` |  | ✅ |
| 209 | `route_trace_store_preview` | `AGENT_ROUTE_TRACE_STORE_PREVIEW` | `bool` | `False` |  | ✅ |
| 210 | `router_role` | `AGENT_ROUTER_ROLE` | `str` | `'ROUTER'` |  | ✅ |
| 211 | `router_timeout_seconds` | `AGENT_ROUTER_TIMEOUT_SECONDS` | `float` | `6.0` |  | ✅ |
| 212 | `routing_heuristic_threshold` | `AGENT_ROUTING_HEURISTIC_THRESHOLD` | `float` | `0.85` |  | ✅ |
| 213 | `routing_policy` | `AGENT_ROUTING_POLICY` | `str` | `'heuristic_then_llm'` |  | ✅ |
| 214 | `sandbox_image` | `AGENT_SANDBOX_IMAGE` | `str` | `'seshat-sandbox-python:0.1'` |  | ✅ |
| 215 | `sandbox_scratch_root` | `AGENT_SANDBOX_SCRATCH_ROOT` | `str` | `'/app/agent_workspace/sandbox'` |  | ✅ |
| 216 | `searxng_base_url` | `AGENT_SEARXNG_BASE_URL` | `str` | `'http://localhost:8888'` |  | ✅ |
| 217 | `searxng_default_categories` | `AGENT_SEARXNG_DEFAULT_CATEGORIES` | `str` | `'general'` |  | — |
| 218 | `searxng_max_results` | `AGENT_SEARXNG_MAX_RESULTS` | `int` | `10` |  | — |
| 219 | `searxng_timeout_seconds` | `AGENT_SEARXNG_TIMEOUT_SECONDS` | `int` | `12` |  | — |
| 220 | `second_brain_cpu_threshold` | `AGENT_SECOND_BRAIN_CPU_THRESHOLD` | `float` | `50.0` |  | ✅ |
| 221 | `second_brain_idle_time_seconds` | `AGENT_SECOND_BRAIN_IDLE_TIME_SECONDS` | `float` | `300.0` |  | ✅ |
| 222 | `second_brain_memory_threshold` | `AGENT_SECOND_BRAIN_MEMORY_THRESHOLD` | `float` | `70.0` |  | ✅ |
| 223 | `second_brain_min_interval_seconds` | `AGENT_SECOND_BRAIN_MIN_INTERVAL_SECONDS` | `float` | `3600.0` |  | ✅ |
| 224 | `second_brain_resource_gating_enabled` | `AGENT_SECOND_BRAIN_RESOURCE_GATING_ENABLED` | `bool` | `True` |  | ✅ |
| 225 | `service_host` | `AGENT_SERVICE_HOST` | `str` | `'0.0.0.0'` |  | ✅ |
| 226 | `service_port` | `AGENT_SERVICE_PORT` | `int` | `9000` |  | ✅ |
| 227 | `service_url` | `AGENT_SERVICE_URL` · `SERVICE_URL` | `str` | `'http://localhost:9000'` |  | ✅ |
| 228 | `session_summary_enabled` | `AGENT_SESSION_SUMMARY_ENABLED` | `bool` | `True` |  | ✅ |
| 229 | `session_write_wait_timeout_seconds` | `AGENT_SESSION_WRITE_WAIT_TIMEOUT_SECONDS` | `float` | `10.0` |  | — |
| 230 | `skill_index_max_tokens` | `AGENT_SKILL_INDEX_MAX_TOKENS` | `int` | `2048` |  | ✅ |
| 231 | `skill_index_p95_token_threshold` | `AGENT_SKILL_INDEX_P95_TOKEN_THRESHOLD` | `int` | `6000` |  | ✅ |
| 232 | `skill_nudge_enabled` | `AGENT_SKILL_NUDGE_ENABLED` | `bool` | `True` |  | ✅ |
| 233 | `skill_routing_mode` | `AGENT_SKILL_ROUTING_MODE` | `str` | `'hybrid'` |  | ✅ |
| 234 | `skill_routing_model_key` | `AGENT_SKILL_ROUTING_MODEL_KEY` | `str` | `'claude_haiku'` |  | ✅ |
| 235 | `skill_routing_threshold_monitor_enabled` | `AGENT_SKILL_ROUTING_THRESHOLD_MONITOR_ENABLED` | `bool` | `True` |  | ✅ |
| 236 | `skill_routing_threshold_monitor_hour_utc` | `AGENT_SKILL_ROUTING_THRESHOLD_MONITOR_HOUR_UTC` | `int` | `5` |  | ✅ |
| 237 | `slm_gpu_util_degraded_pct` | `AGENT_SLM_GPU_UTIL_DEGRADED_PCT` | `float` | `95.0` |  | — |
| 238 | `slm_health_cache_ttl_seconds` | `AGENT_SLM_HEALTH_CACHE_TTL_SECONDS` | `float` | `45.0` |  | — |
| 239 | `slm_health_index_prefix` | `AGENT_SLM_HEALTH_INDEX_PREFIX` | `str` | `'agent-monitors-slm-health'` |  | — |
| 240 | `slm_health_probe_enabled` | `AGENT_SLM_HEALTH_PROBE_ENABLED` | `bool` | `True` |  | — |
| 241 | `slm_health_probe_interval_seconds` | `AGENT_SLM_HEALTH_PROBE_INTERVAL_SECONDS` | `float` | `300.0` |  | — |
| 242 | `slm_health_url` | `AGENT_SLM_HEALTH_URL` | `str` | `'https://<deployment-host>/health'` |  | — |
| 243 | `slm_queue_depth_degraded` | `AGENT_SLM_QUEUE_DEPTH_DEGRADED` | `int` | `4` |  | — |
| 244 | `structural_arm_enabled` | `AGENT_STRUCTURAL_ARM_ENABLED` | `bool` | `False` |  | — |
| 245 | `structural_arm_top_k` | `AGENT_STRUCTURAL_ARM_TOP_K` | `int` | `50` |  | — |
| 246 | `structural_type_predicate_enabled` | `AGENT_STRUCTURAL_TYPE_PREDICATE_ENABLED` | `bool` | `False` |  | — |
| 247 | `sub_agent_max_tokens` | `AGENT_SUB_AGENT_MAX_TOKENS` | `int` | `4096` |  | — |
| 248 | `sub_agent_max_tool_iterations` | `AGENT_SUB_AGENT_MAX_TOOL_ITERATIONS` | `int` | `5` |  | — |
| 249 | `sub_agent_summary_max_chars` | `AGENT_SUB_AGENT_SUMMARY_MAX_CHARS` | `int` | `8000` |  | — |
| 250 | `sub_agent_timeout_seconds` | `AGENT_SUB_AGENT_TIMEOUT_SECONDS` | `float` | `120.0` |  | — |
| 251 | `substrate_profile` | `AGENT_SUBSTRATE_PROFILE` | `str` | `'private'` |  | ✅ |
| 252 | `synthesis_timeout_seconds` | `AGENT_SYNTHESIS_TIMEOUT_SECONDS` | `float` | `25.0` |  | — |
| 253 | `sysgraph_database_url` | `AGENT_SYSGRAPH_DATABASE_URL` | `str` | `'postgresql+asyncpg://<redacted>@localhost:5432/personal_agent'` |  | ✅ |
| 254 | `tool_result_compression_enabled` | `AGENT_TOOL_RESULT_COMPRESSION_ENABLED` | `bool` | `False` |  | — |
| 255 | `tool_result_digest_exclude_tools` | `AGENT_TOOL_RESULT_DIGEST_EXCLUDE_TOOLS` | `list` | `[]` |  | — |
| 256 | `tool_result_digest_head_lines` | `AGENT_TOOL_RESULT_DIGEST_HEAD_LINES` | `int` | `40` |  | — |
| 257 | `tool_result_digest_keep` | `AGENT_TOOL_RESULT_DIGEST_KEEP` | `int` | `3` |  | — |
| 258 | `tool_result_digest_max_expand_tokens` | `AGENT_TOOL_RESULT_DIGEST_MAX_EXPAND_TOKENS` | `int` | `8000` |  | — |
| 259 | `tool_result_digest_min_savings_tokens` | `AGENT_TOOL_RESULT_DIGEST_MIN_SAVINGS_TOKENS` | `int` | `500` |  | — |
| 260 | `tool_result_digest_pin_ttl_turns` | `AGENT_TOOL_RESULT_DIGEST_PIN_TTL_TURNS` | `int` | `4` |  | — |
| 261 | `tool_result_digest_put_timeout_ms` | `AGENT_TOOL_RESULT_DIGEST_PUT_TIMEOUT_MS` | `int` | `2000` |  | — |
| 262 | `tool_result_digest_tail_lines` | `AGENT_TOOL_RESULT_DIGEST_TAIL_LINES` | `int` | `20` |  | — |
| 263 | `tool_result_digest_threshold_tokens` | `AGENT_TOOL_RESULT_DIGEST_THRESHOLD_TOKENS` | `int` | `1500` |  | — |
| 264 | `turn_observed_stream_maxlen` | `AGENT_TURN_OBSERVED_STREAM_MAXLEN` | `int` | `10000` |  | — |
| 265 | `turn_projector_enabled` | `AGENT_TURN_PROJECTOR_ENABLED` | `bool` | `True` |  | — |
| 266 | `upload_max_size_bytes` | `AGENT_UPLOAD_MAX_SIZE_BYTES` | `int` | `52428800` |  | — |
| 267 | `url_guard_allowlist` | `AGENT_URL_GUARD_ALLOWLIST` | `list` | `[]` |  | — |
| 268 | `url_guard_cache_ttl_seconds` | `AGENT_URL_GUARD_CACHE_TTL_SECONDS` | `int` | `3600` |  | — |
| 269 | `url_guard_enabled` | `AGENT_URL_GUARD_ENABLED` | `bool` | `True` |  | — |
| 270 | `url_guard_mode` | `AGENT_URL_GUARD_MODE` | `str` | `'blocklist'` |  | — |
| 271 | `use_service_mode` | `AGENT_USE_SERVICE_MODE` | `bool` | `True` |  | — |
| 272 | `user_display_names_json` | `AGENT_USER_DISPLAY_NAMES_JSON` | `str` | `'{}'` |  | ✅ |
| 273 | `version` | `AGENT_VERSION` | `str` | `'0.1.0'` |  | ✅ |
| 274 | `within_session_compression_enabled` | `AGENT_WITHIN_SESSION_COMPRESSION_ENABLED` | `bool` | `True` |  | — |
| 275 | `within_session_compression_refire_after_messages` | `AGENT_WITHIN_SESSION_COMPRESSION_REFIRE_AFTER_MESSAGES` | `int` | `4` |  | — |
| 276 | `within_session_hard_threshold_ratio` | `AGENT_WITHIN_SESSION_HARD_THRESHOLD_RATIO` | `float` | `0.85` |  | — |
| 277 | `within_session_min_tail_ratio` | `AGENT_WITHIN_SESSION_MIN_TAIL_RATIO` | `float` | `0.25` |  | — |
| 278 | `within_session_pre_pass_threshold_tokens` | `AGENT_WITHIN_SESSION_PRE_PASS_THRESHOLD_TOKENS` | `int` | `800` |  | — |
| 279 | `worker_global_timeout_seconds` | `AGENT_WORKER_GLOBAL_TIMEOUT_SECONDS` | `float` | `180.0` |  | — |
| 280 | `worker_timeout_seconds` | `AGENT_WORKER_TIMEOUT_SECONDS` | `float` | `60.0` |  | — |
| 281 | `ws_event_queue_size` | `AGENT_WS_EVENT_QUEUE_SIZE` | `int` | `500` |  | — |
| 282 | `ws_event_ttl_hours` | `AGENT_WS_EVENT_TTL_HOURS` | `int` | `24` |  | — |
| 283 | `ws_max_message_size` | `AGENT_WS_MAX_MESSAGE_SIZE` | `int` | `8192` |  | — |
| 284 | `ws_ping_timeout_seconds` | `AGENT_WS_PING_TIMEOUT_SECONDS` | `int` | `60` |  | — |
| 285 | `ws_rate_limit_per_second` | `AGENT_WS_RATE_LIMIT_PER_SECOND` | `int` | `20` |  | — |
| 286 | `ws_ticket_ttl_seconds` | `AGENT_WS_TICKET_TTL_SECONDS` | `int` | `30` |  | — |

### Orphan `AGENT_*` keys in `.env.example` (0)

`AGENT_*` keys documented in `.env.example` that bind to **no `AppConfig` field** (neither `AGENT_<FIELD>` nor any alias) and are not in the curated consumed-elsewhere allow-list (6 entries: model-loader endpoints + infra scripts). A non-empty list here is a genuine surprise (dead doc or renamed field):

_None — every documented `AGENT_*` key either binds a field (`AGENT_<FIELD>` or alias) or is a known consumed-elsewhere key._

### AppConfig fields not documented in `.env.example` (113)

Fields with no matching env-var line in `.env.example` — the coverage gap ADR-0099 D4 flags as a *policy* finding (undocumented config surface):

<details><summary>113 undocumented fields</summary>

- `allowed_ws_origins`
- `artifact_decomposition_enabled`
- `artifact_envelope_probe_enabled`
- `artifact_envelope_probe_timeout_s`
- `attachment_cost_confirmation_threshold_usd`
- `attachment_image_max_bytes`
- `attachment_image_max_pixels`
- `attachment_max_images_per_turn`
- `attachment_max_total_payload_bytes`
- `cache_frozen_accum_max_ratio`
- `cache_quality_token_weight`
- `cache_reset_min_run_turns_cloud`
- `cache_reset_min_run_turns_local`
- `captains_log_index_prefix`
- `consolidator_max_extraction_attempts`
- `context_budget_comfortable_tokens`
- `context_budget_generation_reserve_tokens`
- `context_budget_max_tokens`
- `context_quality_governance_budget_reduction`
- `context_quality_governance_enabled`
- `context_quality_governance_threshold`
- `context_quality_stream_enabled`
- `cors_allowed_origins`
- `default_profile`
- `embedding_backfill_enabled`
- `entity_extraction_fewshot_exemplars_enabled`
- `entity_extraction_timeout_seconds`
- `environment`
- `expansion_budget_max`
- `freshness_tier_factors`
- `freshness_tier_reranking_enabled`
- `gateway_access_config`
- `gateway_mount_local`
- `graph_quality_governance_enabled`
- `graph_quality_stream_enabled`
- `joinability_probe_enabled`
- `joinability_probe_index_prefix`
- `joinability_probe_interval_seconds`
- `joinability_probe_window_hours`
- `lexical_arm_enabled`
- `location_precision`
- `metrics_daemon_buffer_size`
- `metrics_daemon_es_emit_interval_seconds`
- `metrics_daemon_poll_interval_seconds`
- `metrics_sampled_stream_maxlen`
- `mode_calibration_anomaly_threshold`
- `mode_controller_enabled`
- `mode_evaluation_interval_seconds`
- `mode_window_size`
- `multipath_arm_top_k`
- `multipath_paraphrase_count`
- `multipath_recall_enabled`
- `multipath_rrf_k`
- `multiquery_arm_enabled`
- `orchestration_mode`
- `orchestrator_max_tool_iterations_by_task_type`
- `planner_timeout_seconds`
- `profiles_dir`
- `recall_candidate_cap`
- `recall_per_entity_turn_cap`
- `recall_similarity_floor`
- `relevance_bounded_recall_enabled`
- `reranker_input_cap`
- `searxng_default_categories`
- `searxng_max_results`
- `searxng_timeout_seconds`
- `session_write_wait_timeout_seconds`
- `slm_gpu_util_degraded_pct`
- `slm_health_cache_ttl_seconds`
- `slm_health_index_prefix`
- `slm_health_probe_enabled`
- `slm_health_probe_interval_seconds`
- `slm_health_url`
- `slm_queue_depth_degraded`
- `structural_arm_enabled`
- `structural_arm_top_k`
- `structural_type_predicate_enabled`
- `sub_agent_max_tokens`
- `sub_agent_max_tool_iterations`
- `sub_agent_summary_max_chars`
- `sub_agent_timeout_seconds`
- `synthesis_timeout_seconds`
- `tool_result_compression_enabled`
- `tool_result_digest_exclude_tools`
- `tool_result_digest_head_lines`
- `tool_result_digest_keep`
- `tool_result_digest_max_expand_tokens`
- `tool_result_digest_min_savings_tokens`
- `tool_result_digest_pin_ttl_turns`
- `tool_result_digest_put_timeout_ms`
- `tool_result_digest_tail_lines`
- `tool_result_digest_threshold_tokens`
- `turn_observed_stream_maxlen`
- `turn_projector_enabled`
- `upload_max_size_bytes`
- `url_guard_allowlist`
- `url_guard_cache_ttl_seconds`
- `url_guard_enabled`
- `url_guard_mode`
- `use_service_mode`
- `within_session_compression_enabled`
- `within_session_compression_refire_after_messages`
- `within_session_hard_threshold_ratio`
- `within_session_min_tail_ratio`
- `within_session_pre_pass_threshold_tokens`
- `worker_global_timeout_seconds`
- `worker_timeout_seconds`
- `ws_event_queue_size`
- `ws_event_ttl_hours`
- `ws_max_message_size`
- `ws_ping_timeout_seconds`
- `ws_rate_limit_per_second`
- `ws_ticket_ttl_seconds`

</details>

### Secret fields (8)

8 `AppConfig` fields match the tightened secret heuristic (`*_api_key`, `*_password`, `*_secret`, `*secret_access_key`, plus the internal auth token; token-budget scalars like `*_max_tokens` are excluded). Their **values are never emitted** — the default column shows a redaction marker, and any credential embedded in a DSN default (Postgres/Neo4j) is stripped by the sanitizer. The field names are enumerated in **§8**; prod secrets live only in `.env` (ADR-0007).

<!-- AUTOGEN:AppConfig END -->

---

## §2 — Model role-assignment matrix (the recurring drift, ADR-0099 D1)

The `entity_extraction_role` / `captains_log_role` / `insights_role` headers are redeclared independently in the header of every role-bearing model YAML. **Reader:** these three are fields on the `ModelConfig` pydantic model (`src/personal_agent/llm_client/models.py:298-306`), consumed by `cost_gate/__init__.py`, `captains_log/{reflection,reflection_dspy,feedback}.py`, and the Second-Brain consolidator. **`models.medium.yaml` declares no headers**, so all three fall back to the `ModelConfig` field default **`"primary"`** — silently routing extraction/log/insights to the *primary* model.

| Role header | `models.yaml` (local default) | `models.cloud.yaml` (**prod**) | `models-baseline.yaml` | `benchmark-8b` | `benchmark-4b` | `benchmark-4b-f16` | `models.medium.yaml` |
|---|---|---|---|---|---|---|---|
| `entity_extraction_role` | **`gpt-5.4-nano`** | `gpt-5.4-mini` | `claude_sonnet` | `gpt-5.4-mini` | `gpt-5.4-mini` | `gpt-5.4-mini` | _(default)_ `primary` |
| `captains_log_role` | **`gpt-5.4-nano`** | `claude_sonnet` | `claude_sonnet` | `claude_sonnet` | `claude_sonnet` | `claude_sonnet` | _(default)_ `primary` |
| `insights_role` | **`gpt-5.4-nano`** | `claude_sonnet` | `claude_sonnet` | `claude_sonnet` | `claude_sonnet` | `claude_sonnet` | _(default)_ `primary` |

**Divergence (cognitive-pipeline roles — ADR-0099 says these should be `forbidden`/consistent):**
- `entity_extraction`: **local `nano` ≠ prod `mini`** — the drift ADR-0099 names. Local writes nano-quality entities into the same substrate prod writes mini-quality ones into.
- `captains_log` + `insights`: **local `nano` ≠ prod `sonnet`**.
- `models.medium.yaml`: all three resolve to **`primary`** (header-less fallback) — a *third* distinct assignment.

The two live profiles that write to the shared substrate are `models.yaml` (local dev, via `make dev`) and `models.cloud.yaml` (prod, pinned by `AGENT_MODEL_CONFIG_PATH` in `docker-compose.cloud.yml`). Their divergence is the operative one.

**Update (ADR-0099 stage 4, FRE-652):** `models-baseline.yaml` and `models.medium.yaml` — both columns above — are now **retired** (deleted from the repo). Neither had a live or test reader; the header-less-fallback risk (`models.medium.yaml`) is closed by deletion, not correction. The table above is left as the point-in-time audit record; treat those two columns as historical.

---

## §3 — Model-definition drift (same role-name → different resolved model)

A role name resolving to *different real model definitions* across files. ADR-0099's cited example (`gpt-5.4-nano` → `gpt-4o-mini` in `models.eval.yaml`) is **historical** (§0). The **current** live definition drift:

| Role / key | `models.yaml` + `models.cloud.yaml` (live) | `models-baseline.yaml` + all 3 `benchmark-*` | Divergent? |
|---|---|---|---|
| `claude_sonnet` (`id:`) | **`claude-sonnet-5`** | **`claude-sonnet-4-6`** | **YES — definition drift** |
| `claude_haiku` (`id:`) | `claude-haiku-4-5-20251001` | `claude-haiku-4-5-20251001` | no |
| `gpt-5.4-nano` (`id:`) | `gpt-5.4-nano` | `gpt-5.4-nano` | no (ADR example now historical) |
| `gpt-5.4-mini` (`id:`) | `gpt-5.4-mini` | `gpt-5.4-mini` | no |

**Consequence (as audited):** `captains_log_role`/`insights_role` = `claude_sonnet` in prod resolves to **`claude-sonnet-5`**, but the same role in `models-baseline.yaml` / benchmarks resolves to **`claude-sonnet-4-6`** — a `forbidden`-role definition drift, exactly the class ADR-0099 D4's guard must fail on (comparing the fully-resolved `ModelDefinition`, not the name-key). **Update (FRE-652):** `models-baseline.yaml` is now retired, closing its half of this drift. The identical `claude-sonnet-4-6` drift **persists, unresolved, in the three `benchmark-*.yaml` files** — those were added after the ADR and are out of FRE-652's scope (they are excluded from the guard's `active_profiles`, so the guard's `forbidden`-role check does not see them either).

**`reranker` diverges local vs cloud** (contradicting ADR-0099's "embedding/reranker: consistent"):

| Key | `models.yaml` (local) | `models.cloud.yaml` (prod) | Divergent? |
|---|---|---|---|
| `embedding` (`id:`) | `Qwen/Qwen3-Embedding-0.6B` | `Qwen/Qwen3-Embedding-0.6B` | no |
| `reranker` (`id:`) | **`ggml-org/Qwen3-Reranker-0.6B-Q8_0-GGUF`** | **`Qwen/Qwen3-Reranker-4B-mxfp8`** | **YES — 0.6B local vs 4B cloud** |

(The `benchmark-*` files intentionally vary embedding/reranker sizes — 4B/8B — as the point of the benchmark; those are by-design, not drift.)

---

## §4 — Deployment profiles (`config/profiles/*`, ADR-0044)

Profiles swap only the inference brain (`primary`/`sub_agent`) — ADR-0099's *legitimate* (`allowed`) divergence.

| Param | `local.yaml` | `cloud.yaml` | Reader |
|---|---|---|---|
| `primary_model` | `primary` (SLM) | `claude_sonnet` | profile loader / orchestrator |
| `sub_agent_model` | `sub_agent` (SLM) | `claude_haiku` | orchestrator sub-agent spawn |
| `provider_type` | `local` | `cloud` | provider dispatch (ADR-0044) |
| `cost_limit_per_session` | `null` (no limit) | `2.00` | cost gate |
| `delegation.allow_cloud_escalation` | `false` | `true` | delegation router |
| `delegation.escalation_model` | `claude_sonnet` | `claude_sonnet` | per-attachment "cloud" override (ADR-0101 §8a) |

---

## §5 — Governance & other `config/`

**`config/governance/` (5 role-bearing/policy files).** Not `AppConfig` params — YAML policy consumed by the governance evaluator and cost gate.

| File | Top-level keys | Reader |
|---|---|---|
| `budget.yaml` | `version`, `roles` (`main_inference`, `entity_extraction`, `captains_log`, `insights`, `promotion`, `freshness`, `skill_routing`, …), `caps` | `cost_gate/` |
| `modes.yaml` | `modes`, `transition_rules` | `brainstem` mode manager |
| `safety.yaml` | `content_filtering`, `outbound_gateway`, `rate_limits`, `human_approval` | governance evaluator |
| `tools.yaml` | `mode_policies`, `tool_categories`, `tools` | `ToolRegistry` governance |
| `models.yaml` (**policy**, ADR-0005) | `mode_constraints` | mode-aware model-role constraint check |

_Note: `config/governance/tools.yaml.backup` is an untracked-style backup file colocated with the live policy — candidate cleanup (not config)._

**Other `config/` (not parameter surfaces, listed for completeness):** `gateway_access.yaml` (gateway ACL), `cloud-sim/Caddyfile` (deploy-target reverse proxy), `kibana/dashboards/*.ndjson` (dashboard definitions), `artifact_lib_manifest.json` + `artifact_lib_substitution_map.json` (ADR-0089 curated `/lib/`).

---

## §6 — `docker-compose*.yml` `environment:` blocks (deployment provenance, ADR-0099 D2.2)

Which model YAML is live depends on which compose file was deployed. Only `.cloud.yml` and `.eval.yml` run the agent service (and both pin `models.cloud.yaml`); `.yml` (local dev) and `.test.yml` run **infra only** — the agent runs via `make dev` (uvicorn) reading the `AGENT_MODEL_CONFIG_PATH` default → `config/models.yaml`.

| File | Runs agent? | `AGENT_MODEL_CONFIG_PATH` | `APP_ENV` | Notable env |
|---|---|---|---|---|
| `docker-compose.yml` | no (infra only) | — → default `models.yaml` | — | Postgres/ES/Neo4j dev passwords (`:-` defaults) |
| `docker-compose.cloud.yml` | **yes** | `/app/config/models.cloud.yaml` | `production` | DB/Neo4j/ES/Redis/SearXNG URIs; `AGENT_GATEWAY_AUTH_ENABLED=true`; CF-Access client id/secret; `?`-required passwords |
| `docker-compose.eval.yml` | **yes** (2 services) | `/app/config/models.cloud.yaml` | `eval` | `AGENT_CLOUD_WEEKLY_BUDGET_USD=50.0`; `AGENT_GATEWAY_AUTH_ENABLED=false`; MCP/primitives toggles differ between the two services; `AGENT_ANTHROPIC/OPENAI_API_KEY` passthrough |
| `docker-compose.test.yml` | no (infra only) | — | — | dev-default passwords |

**Provenance chain:** `prod` → `docker-compose.cloud.yml` → `AGENT_MODEL_CONFIG_PATH=/app/config/models.cloud.yaml` → extraction `gpt-5.4-mini`, captains_log/insights `claude_sonnet` (`claude-sonnet-5`). This is the one committed place tying profile → compose → active YAML → resolved model, per ADR-0099 D2.2.

---

## §7 — `.env.example` / `mcp-secrets.env.example`

`.env.example` (35 KB) is a **documentation template**: 166 `AGENT_*` keys are documented, but all bar one (`AGENT_LOCATION_ENABLED=true`) are **commented examples** — real values live only in the gitignored `.env`. Non-`AGENT_` keys documented: `APP_ENV/APP_DEBUG/APP_LOG_LEVEL/APP_LOG_FORMAT` (bind aliased AppConfig fields), `POSTGRES_PASSWORD`/`NEO4J_PASSWORD` (compose), `CF_ACCESS_*` (aliased fields), `PERSONAL_AGENT_EVAL`/`PERSONAL_AGENT_INTEGRATION` (pytest markers), `NEXT_PUBLIC_SESHAT_URL` (PWA build).

`docker/mcp/mcp-secrets.env.example` — MCP-gateway secret template (consumed by `docker/mcp/run-gateway.sh` via `AGENT_MCP_SECRETS_FILE`), not an `AppConfig` surface.

**Orphan analysis (see §1):** after alias-aware binding, **zero** documented `AGENT_*` keys fail to bind a field, once the curated consumed-elsewhere allow-list (6 model-loader/infra keys: `AGENT_EMBEDDING_ENDPOINT`, `AGENT_RERANKER_ENDPOINT`, `AGENT_MCP_SECRETS_FILE`, `AGENT_GATEWAY_TOKEN_PWA`, `AGENT_GATEWAY_TOKEN_EXTERNAL_AGENT`, `AGENT_CLOUDFLARE_TUNNEL_TOKEN`) is excluded. **113** `AppConfig` fields have no `.env.example` line (coverage gap — §1).

---

## §8 — Secret inventory (ADR-0099 D2, derived)

8 `AppConfig` fields match the tightened secret heuristic: `anthropic_api_key`, `openai_api_key`, `perplexity_api_key`, `linear_api_key`, `neo4j_password`, `cf_access_client_secret`, `r2_secret_access_key`, `artifact_resolve_internal_token`.

**Committed-value check (corrected — FRE-648 CodeQL remediation).** The six API-key-style fields default `None`. But **two defaults did carry a clear-text credential**: `neo4j_password` defaulted to `'neo4j_dev_password'`, and `database_url`'s DSN embedded `agent:agent_dev_password@…`. These are **dev placeholders** (the same values docker-compose supplies via `${…:-dev_password}`) — **prod passwords use `${…:?required}` with no default**, so no production secret was ever a committed default. Even so, clear-text credentials must not be re-published: this inventory now **redacts every secret field's default** (🔒 marker) and **strips `user:pass@` credentials from all DSN defaults** via `scripts/audit/config_inventory.py::_sanitize_urls`. This resolved a CodeQL high-severity "clear-text logging of sensitive information" alert. **Follow-up for the owner:** consider moving `neo4j_password`/`database_url` dev-placeholder defaults out of `settings.py` (env-only) so the source itself carries no credential (an `src/` change beyond this docs ticket).

---

## §9 — Findings summary (feeds C1 guard + C2 matrix)

| # | Finding | Class (ADR-0099 D4) | Feeds |
|---|---|---|---|
| F1 | **Role-assignment drift** — `entity_extraction` local `nano` ≠ prod `mini`; `captains_log`/`insights` local `nano` ≠ prod `sonnet` (§2). | Policy (undeclared `forbidden` divergence) | C1/C2 |
| F2 | **Model-definition drift** — `claude_sonnet` = `claude-sonnet-5` (live) vs `claude-sonnet-4-6` (baseline + benchmarks) (§3). Replaces the ADR's historical `gpt-4o-mini` example. **Resolved for `models-baseline.yaml` (FRE-652, retired); persists, out of scope, in `models.benchmark-*.yaml`.** | Safety (`forbidden` role → mismatched `ModelDefinition`) | C1 guard |
| F3 | **`reranker` diverges local vs cloud** (0.6B vs 4B) — contradicts ADR-0099's "embedding/reranker consistent" (§3). | Policy | C1/C2 |
| F4 | **`models.medium.yaml` header-less fallback** routes extraction/log/insights to `primary` (§2). **Resolved (FRE-652) — file retired.** | Policy | C2 matrix |
| F5 | **Dual env-var spelling** — aliased fields bind BOTH `AGENT_<FIELD>` and the alias (e.g. `debug` ← `AGENT_DEBUG` *and* `APP_DEBUG`; `log_level` ← `AGENT_LOG_LEVEL` *and* `APP_LOG_LEVEL`). Empirically verified. A subtle footgun: two spellings, last-wins, easy to set one unaware of the other. | Policy (surface hygiene) | D4 design |
| F6 | **ADR-0099 Context table is stale** — `models.eval.yaml` gone, 3 `benchmark-*` YAMLs added, `mcp-secrets.env` path wrong (§0). | — (ADR maintenance) | ADR-0099 update |
| F7 | **113 `AppConfig` fields undocumented** in `.env.example` (§1, §7). | Policy (undocumented surface) | D4 coverage check |

**Structural point for D1:** the two live substrate-writing profiles (`models.yaml` local, `models.cloud.yaml` prod) diverge on 3 of the cognitive-pipeline roles (F1) and on `reranker` (F3), and a `forbidden` role suffers definition drift (F2) — precisely the three drift layers ADR-0099 D1/D4 removes (assignment) and catches (definition). The role matrix (C2) should encode prod's values as the `forbidden` targets; F5 argues the guard should also flag multi-spelling env aliases.
