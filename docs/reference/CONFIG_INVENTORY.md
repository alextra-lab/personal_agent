# Configuration Inventory — canonical parameter × location × default × reader × validation × divergence

> **Ticket:** [FRE-648](https://linear.app/frenchforest/issue/FRE-648) — ADR-0099 **stage 0**, the "first deliverable."
> **Backing ADR:** [ADR-0099 — Configuration Management & Validation](../architecture_decisions/ADR-0099-configuration-management-and-validation.md) (single-source role matrix · profile-divergence policy · cross-config validator).
> **Generated:** 2026-07-03 · **Status:** point-in-time audit of `origin/main` @ FRE-648.
> **Reproduce / verify:** `uv run python scripts/audit/config_inventory.py verify` (asserts this doc covers every `AppConfig` field + documented `AGENT_*` key). Regenerate the AppConfig section with `… generate`.
> **Currency (FRE-1712, 2026-10-30):** Only §1 is current. The generator rebuilds it from `AppConfig`, and `verify` checks it. §0, §2 to §8 and §9 are the 2026-07-03 audit. They are not the present state. After 2026-07-03, the extra model YAML files and `config/profiles/*` were deleted, the embedder and reranker models changed, and `AppConfig` grew from 277 to 338 fields. The wrong statements are listed as rows D9 to D19 in [the FRE-1703 documentation audit](../research/2026-10-29-fre-1703-documentation-audit.md).

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
| Deployment profiles (ADR-0044) | `config/profiles/{local,cloud}.yaml` | §4 |
| Governance | `config/governance/{budget,modes,tools}.yaml` | §5 |
| Env template | `.env.example`, `docker/mcp/mcp-secrets.env.example` | §1, §7 |
| Compose `environment:` | `docker-compose{,.cloud,.eval,.test}.yml` | §6 |
| Other `config/` | `gateway_access.yaml`, `cloud-sim/Caddyfile`, `grafana/*`, `artifact_lib_*.json` | §5 (noted) |

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

**341 typed scalar/path parameters** live in `src/personal_agent/config/settings.py` (`AppConfig`, a pydantic `BaseSettings` with `env_prefix="AGENT_"`). Every field is read through the process-wide `from personal_agent.config import settings` singleton (`settings.<field>`), so the **reader** is uniformly that accessor; **validation** is pydantic type coercion at load (`AppConfig()` raises `ValidationError` on a bad value). The **Env var** column shows `AGENT_<FIELD>` (the prefix+name form, **always valid**); where a field also declares an `alias=`, that alias is shown after `·` as an **additional** accepted spelling — empirically both bind (e.g. `debug` accepts `AGENT_DEBUG` *and* `APP_DEBUG`). A field's default is overridable by either; the *profile-divergence* for scalars is the set of `docker-compose*.yml` `environment:` blocks that override it (see §8).

| # | Field (`settings.X`) | Env var | Type | Default | Secret | In `.env.example` |
|---|---|---|---|---|---|---|
| 1 | `abuse_ch_auth_key` | `AGENT_ABUSE_CH_AUTH_KEY` | `str \| None` | 🔒 redacted (secret — `.env` only) | 🔑 | ✅ |
| 2 | `agent_id` | `AGENT_AGENT_ID` | `str` | `'seshat-local'` |  | ✅ |
| 3 | `agent_owner_email` | `AGENT_AGENT_OWNER_EMAIL` · `AGENT_OWNER_EMAIL` | `str \| None` | `None` |  | ✅ |
| 4 | `allow_test_writes_to_prod_substrate` | `AGENT_ALLOW_TEST_WRITES_TO_PROD_SUBSTRATE` | `bool` | `False` |  | ✅ |
| 5 | `allowed_ws_origins` | `AGENT_ALLOWED_WS_ORIGINS` | `list` | `['https://<deployment-host>', 'https://<deployment-host>', 'http://localhost:3000']` |  | ✅ |
| 6 | `anthropic_api_key` | `AGENT_ANTHROPIC_API_KEY` | `str \| None` | 🔒 redacted (secret — `.env` only) | 🔑 | ✅ |
| 7 | `approval_timeout_seconds` | `AGENT_APPROVAL_TIMEOUT_SECONDS` | `float` | `60.0` |  | ✅ |
| 8 | `approval_ui_enabled` | `AGENT_APPROVAL_UI_ENABLED` | `bool` | `False` |  | ✅ |
| 9 | `artifact_draft_max_tokens` | `AGENT_ARTIFACT_DRAFT_MAX_TOKENS` | `int` | `32768` |  | ✅ |
| 10 | `artifact_envelope_probe_enabled` | `AGENT_ARTIFACT_ENVELOPE_PROBE_ENABLED` | `bool` | `True` |  | — |
| 11 | `artifact_envelope_probe_timeout_s` | `AGENT_ARTIFACT_ENVELOPE_PROBE_TIMEOUT_S` | `float` | `2.0` |  | — |
| 12 | `artifact_resolve_internal_token` | `AGENT_ARTIFACT_RESOLVE_INTERNAL_TOKEN` | `str \| None` | 🔒 redacted (secret — `.env` only) | 🔑 | ✅ |
| 13 | `artifacts_egress_base_url` | `AGENT_ARTIFACTS_EGRESS_BASE_URL` | `str \| None` | `None` |  | ✅ |
| 14 | `artifacts_public_base_url` | `AGENT_ARTIFACTS_PUBLIC_BASE_URL` | `str \| None` | `None` |  | ✅ |
| 15 | `attachment_cost_confirmation_threshold_usd` | `AGENT_ATTACHMENT_COST_CONFIRMATION_THRESHOLD_USD` | `float` | `0.5` |  | — |
| 16 | `attachment_image_max_bytes` | `AGENT_ATTACHMENT_IMAGE_MAX_BYTES` | `int` | `5242880` |  | — |
| 17 | `attachment_image_max_pixels` | `AGENT_ATTACHMENT_IMAGE_MAX_PIXELS` | `int` | `1568` |  | — |
| 18 | `attachment_max_images_per_turn` | `AGENT_ATTACHMENT_MAX_IMAGES_PER_TURN` | `int` | `4` |  | — |
| 19 | `attachment_max_total_payload_bytes` | `AGENT_ATTACHMENT_MAX_TOTAL_PAYLOAD_BYTES` | `int` | `15728640` |  | — |
| 20 | `brave_search_cost_per_call_usd` | `AGENT_BRAVE_SEARCH_COST_PER_CALL_USD` | `float` | `0.005` |  | — |
| 21 | `broad_recall_relevance_bound` | `AGENT_BROAD_RECALL_RELEVANCE_BOUND` | `float \| None` | `0.344176` |  | — |
| 22 | `broad_recall_relevance_gate_enabled` | `AGENT_BROAD_RECALL_RELEVANCE_GATE_ENABLED` | `bool` | `True` |  | — |
| 23 | `build_fingerprint` | `AGENT_BUILD_FINGERPRINT` | `str \| None` | `None` |  | — |
| 24 | `cache_erosion_probe_enabled` | `AGENT_CACHE_EROSION_PROBE_ENABLED` | `bool` | `True` |  | — |
| 25 | `cache_erosion_probe_index_prefix` | `AGENT_CACHE_EROSION_PROBE_INDEX_PREFIX` | `str` | `'agent-monitors-cache-erosion'` |  | — |
| 26 | `cache_erosion_probe_interval_seconds` | `AGENT_CACHE_EROSION_PROBE_INTERVAL_SECONDS` | `int` | `3600` |  | — |
| 27 | `cache_erosion_probe_threshold` | `AGENT_CACHE_EROSION_PROBE_THRESHOLD` | `float` | `0.9` |  | — |
| 28 | `cache_erosion_probe_window_days` | `AGENT_CACHE_EROSION_PROBE_WINDOW_DAYS` | `int` | `2` |  | — |
| 29 | `cache_frozen_accum_max_ratio` | `AGENT_CACHE_FROZEN_ACCUM_MAX_RATIO` | `float` | `0.5` |  | — |
| 30 | `cache_quality_token_weight` | `AGENT_CACHE_QUALITY_TOKEN_WEIGHT` | `float` | `4000.0` |  | — |
| 31 | `cache_reset_min_run_turns_cloud` | `AGENT_CACHE_RESET_MIN_RUN_TURNS_CLOUD` | `int` | `4` |  | — |
| 32 | `cache_reset_min_run_turns_local` | `AGENT_CACHE_RESET_MIN_RUN_TURNS_LOCAL` | `int` | `12` |  | — |
| 33 | `captains_log_index_prefix` | `AGENT_CAPTAINS_LOG_INDEX_PREFIX` | `str` | `'agent-captains'` |  | — |
| 34 | `captains_log_reflection_cadence_enabled` | `AGENT_CAPTAINS_LOG_REFLECTION_CADENCE_ENABLED` | `bool` | `True` |  | — |
| 35 | `captains_log_reflection_enabled` | `AGENT_CAPTAINS_LOG_REFLECTION_ENABLED` | `bool` | `True` |  | ✅ |
| 36 | `captains_log_reflection_min_interval_seconds` | `AGENT_CAPTAINS_LOG_REFLECTION_MIN_INTERVAL_SECONDS` | `float` | `1800.0` |  | — |
| 37 | `cf_access_aud` | `AGENT_CF_ACCESS_AUD` · `CF_ACCESS_AUD` | `str \| None` | `None` |  | ✅ |
| 38 | `cf_access_main_aud` | `AGENT_CF_ACCESS_MAIN_AUD` · `CF_ACCESS_MAIN_AUD` | `str \| None` | `None` |  | ✅ |
| 39 | `cf_access_team_domain` | `AGENT_CF_ACCESS_TEAM_DOMAIN` · `CF_ACCESS_TEAM_DOMAIN` | `str \| None` | `None` |  | ✅ |
| 40 | `consolidator_max_extraction_attempts` | `AGENT_CONSOLIDATOR_MAX_EXTRACTION_ATTEMPTS` | `int` | `5` |  | — |
| 41 | `constraint_pause_timeout_seconds` | `AGENT_CONSTRAINT_PAUSE_TIMEOUT_SECONDS` | `float` | `180.0` |  | — |
| 42 | `context_budget_max_tokens` | `AGENT_CONTEXT_BUDGET_MAX_TOKENS` | `int` | `98304` |  | — |
| 43 | `context_quality_governance_budget_reduction` | `AGENT_CONTEXT_QUALITY_GOVERNANCE_BUDGET_REDUCTION` | `float` | `0.15` |  | — |
| 44 | `context_quality_governance_enabled` | `AGENT_CONTEXT_QUALITY_GOVERNANCE_ENABLED` | `bool` | `False` |  | — |
| 45 | `context_quality_governance_threshold` | `AGENT_CONTEXT_QUALITY_GOVERNANCE_THRESHOLD` | `int` | `2` |  | — |
| 46 | `context_quality_stream_enabled` | `AGENT_CONTEXT_QUALITY_STREAM_ENABLED` | `bool` | `True` |  | — |
| 47 | `context_window_max_tokens` | `AGENT_CONTEXT_WINDOW_MAX_TOKENS` | `int` | `48000` |  | ✅ |
| 48 | `conversation_context_strategy` | `AGENT_CONVERSATION_CONTEXT_STRATEGY` | `str` | `'truncate'` |  | ✅ |
| 49 | `conversation_max_history_messages` | `AGENT_CONVERSATION_MAX_HISTORY_MESSAGES` | `int` | `20` |  | ✅ |
| 50 | `cors_allowed_origins` | `AGENT_CORS_ALLOWED_ORIGINS` | `list` | `['http://localhost:3000', 'https://<deployment-host>', 'https://<deployment-host>']` |  | ✅ |
| 51 | `data_lifecycle_enabled` | `AGENT_DATA_LIFECYCLE_ENABLED` | `bool` | `True` |  | ✅ |
| 52 | `database_admin_url` | `AGENT_DATABASE_ADMIN_URL` | `str` | `'postgresql+asyncpg://<redacted>@localhost:5432/personal_agent'` |  | ✅ |
| 53 | `database_echo` | `AGENT_DATABASE_ECHO` | `bool` | `False` |  | ✅ |
| 54 | `database_url` | `AGENT_DATABASE_URL` | `str` | `'postgresql+asyncpg://<redacted>@localhost:5432/personal_agent'` |  | ✅ |
| 55 | `debug` | `AGENT_DEBUG` · `APP_DEBUG` | `bool` | `False` |  | ✅ |
| 56 | `decided_skip_days` | `AGENT_DECIDED_SKIP_DAYS` | `int` | `90` |  | ✅ |
| 57 | `dedup_similarity_threshold` | `AGENT_DEDUP_SIMILARITY_THRESHOLD` | `float` | `0.92` |  | ✅ |
| 58 | `delegation_enabled` | `AGENT_DELEGATION_ENABLED` | `bool` | `False` |  | — |
| 59 | `delivery_ratio_probe_enabled` | `AGENT_DELIVERY_RATIO_PROBE_ENABLED` | `bool` | `True` |  | — |
| 60 | `delivery_ratio_probe_index_prefix` | `AGENT_DELIVERY_RATIO_PROBE_INDEX_PREFIX` | `str` | `'agent-monitors-delivery-ratio'` |  | — |
| 61 | `delivery_ratio_probe_interval_seconds` | `AGENT_DELIVERY_RATIO_PROBE_INTERVAL_SECONDS` | `int` | `86400` |  | — |
| 62 | `delivery_ratio_probe_min_ratio` | `AGENT_DELIVERY_RATIO_PROBE_MIN_RATIO` | `float` | `0.99` |  | — |
| 63 | `deployment_profile` | `AGENT_DEPLOYMENT_PROFILE` | `Literal` | `'local'` |  | ✅ |
| 64 | `dev_broker_control_secret` | `AGENT_DEV_BROKER_CONTROL_SECRET` | `str \| None` | 🔒 redacted (secret — `.env` only) | 🔑 | ✅ |
| 65 | `disk_usage_alert_percent` | `AGENT_DISK_USAGE_ALERT_PERCENT` | `float` | `80.0` |  | ✅ |
| 66 | `document_max_extracted_text_chars` | `AGENT_DOCUMENT_MAX_EXTRACTED_TEXT_CHARS` | `int` | `200000` |  | — |
| 67 | `document_max_pages_per_turn` | `AGENT_DOCUMENT_MAX_PAGES_PER_TURN` | `int` | `40` |  | — |
| 68 | `document_max_total_payload_bytes` | `AGENT_DOCUMENT_MAX_TOTAL_PAYLOAD_BYTES` | `int` | `15728640` |  | — |
| 69 | `document_page_max_bytes` | `AGENT_DOCUMENT_PAGE_MAX_BYTES` | `int` | `5242880` |  | — |
| 70 | `document_page_max_pixels` | `AGENT_DOCUMENT_PAGE_MAX_PIXELS` | `int` | `1568` |  | — |
| 71 | `document_text_density_floor_per_page` | `AGENT_DOCUMENT_TEXT_DENSITY_FLOOR_PER_PAGE` | `int` | `100` |  | — |
| 72 | `elasticsearch_index_prefix` | `AGENT_ELASTICSEARCH_INDEX_PREFIX` | `str` | `'agent-logs'` |  | ✅ |
| 73 | `elasticsearch_url` | `AGENT_ELASTICSEARCH_URL` | `str` | `'http://localhost:9200'` |  | ✅ |
| 74 | `embedding_backfill_enabled` | `AGENT_EMBEDDING_BACKFILL_ENABLED` | `bool` | `True` |  | — |
| 75 | `embedding_dimensions` | `AGENT_EMBEDDING_DIMENSIONS` | `int` | `1024` |  | ✅ |
| 76 | `enable_memory_graph` | `AGENT_ENABLE_MEMORY_GRAPH` | `bool` | `False` |  | ✅ |
| 77 | `enable_second_brain` | `AGENT_ENABLE_SECOND_BRAIN` | `bool` | `False` |  | ✅ |
| 78 | `entity_extraction_fewshot_exemplars_enabled` | `AGENT_ENTITY_EXTRACTION_FEWSHOT_EXEMPLARS_ENABLED` | `bool` | `False` |  | — |
| 79 | `entity_extraction_timeout_seconds` | `AGENT_ENTITY_EXTRACTION_TIMEOUT_SECONDS` | `int` | `90` |  | — |
| 80 | `entity_match_relevance_bound` | `AGENT_ENTITY_MATCH_RELEVANCE_BOUND` | `float \| None` | `0.344176` |  | — |
| 81 | `entity_match_relevance_gate_enabled` | `AGENT_ENTITY_MATCH_RELEVANCE_GATE_ENABLED` | `bool` | `True` |  | — |
| 82 | `environment` | `AGENT_ENVIRONMENT` | `Environment` | `<Environment.PRODUCTION: 'production'>` |  | — |
| 83 | `error_monitor_enabled` | `AGENT_ERROR_MONITOR_ENABLED` | `bool` | `True` |  | ✅ |
| 84 | `error_monitor_max_patterns_per_scan` | `AGENT_ERROR_MONITOR_MAX_PATTERNS_PER_SCAN` | `int` | `50` |  | ✅ |
| 85 | `error_monitor_min_occurrences` | `AGENT_ERROR_MONITOR_MIN_OCCURRENCES` | `int` | `5` |  | ✅ |
| 86 | `error_monitor_window_hours` | `AGENT_ERROR_MONITOR_WINDOW_HOURS` | `int` | `24` |  | ✅ |
| 87 | `eur_usd_rate` | `AGENT_EUR_USD_RATE` | `float` | `1.14` |  | — |
| 88 | `event_bus_ack_timeout_seconds` | `AGENT_EVENT_BUS_ACK_TIMEOUT_SECONDS` | `int` | `300` |  | ✅ |
| 89 | `event_bus_consumer_poll_interval_ms` | `AGENT_EVENT_BUS_CONSUMER_POLL_INTERVAL_MS` | `int` | `100` |  | ✅ |
| 90 | `event_bus_dead_letter_stream` | `AGENT_EVENT_BUS_DEAD_LETTER_STREAM` | `str` | `'stream:dead_letter'` |  | ✅ |
| 91 | `event_bus_enabled` | `AGENT_EVENT_BUS_ENABLED` | `bool` | `False` |  | ✅ |
| 92 | `event_bus_max_retries` | `AGENT_EVENT_BUS_MAX_RETRIES` | `int` | `3` |  | ✅ |
| 93 | `event_bus_redis_url` | `AGENT_EVENT_BUS_REDIS_URL` | `str` | `'redis://localhost:6379/0'` |  | ✅ |
| 94 | `exa_search_cost_per_call_usd` | `AGENT_EXA_SEARCH_COST_PER_CALL_USD` | `float` | `0.007` |  | — |
| 95 | `expansion_budget_max` | `AGENT_EXPANSION_BUDGET_MAX` | `int` | `3` |  | — |
| 96 | `expansion_enabled` | `AGENT_EXPANSION_ENABLED` | `bool` | `True` |  | ✅ |
| 97 | `experimental_openai_base_llm_http_handler` | `AGENT_EXPERIMENTAL_OPENAI_BASE_LLM_HTTP_HANDLER` · `EXPERIMENTAL_OPENAI_BASE_LLM_HTTP_HANDLER` | `bool` | `False` |  | — |
| 98 | `extra_permission_rules_path` | `AGENT_EXTRA_PERMISSION_RULES_PATH` | `pathlib.Path \| None` | `None` |  | ✅ |
| 99 | `failure_path_reflection_enabled` | `AGENT_FAILURE_PATH_REFLECTION_ENABLED` | `bool` | `True` |  | ✅ |
| 100 | `feedback_max_reevaluations` | `AGENT_FEEDBACK_MAX_REEVALUATIONS` | `int` | `2` |  | ✅ |
| 101 | `feedback_polling_enabled` | `AGENT_FEEDBACK_POLLING_ENABLED` | `bool` | `True` |  | ✅ |
| 102 | `feedback_polling_hour_utc` | `AGENT_FEEDBACK_POLLING_HOUR_UTC` | `int` | `7` |  | ✅ |
| 103 | `feedback_suppression_days` | `AGENT_FEEDBACK_SUPPRESSION_DAYS` | `int` | `30` |  | ✅ |
| 104 | `freshness_backfill_confirm` | `AGENT_FRESHNESS_BACKFILL_CONFIRM` | `bool` | `False` |  | ✅ |
| 105 | `freshness_cold_threshold_days` | `AGENT_FRESHNESS_COLD_THRESHOLD_DAYS` | `float` | `180.0` |  | ✅ |
| 106 | `freshness_consumer_batch_max_events` | `AGENT_FRESHNESS_CONSUMER_BATCH_MAX_EVENTS` | `int` | `50` |  | ✅ |
| 107 | `freshness_consumer_batch_window_seconds` | `AGENT_FRESHNESS_CONSUMER_BATCH_WINDOW_SECONDS` | `float` | `5.0` |  | ✅ |
| 108 | `freshness_dormant_entity_proposal_threshold` | `AGENT_FRESHNESS_DORMANT_ENTITY_PROPOSAL_THRESHOLD` | `int` | `10` |  | ✅ |
| 109 | `freshness_dormant_relationship_proposal_threshold` | `AGENT_FRESHNESS_DORMANT_RELATIONSHIP_PROPOSAL_THRESHOLD` | `int` | `10` |  | ✅ |
| 110 | `freshness_enabled` | `AGENT_FRESHNESS_ENABLED` | `bool` | `False` |  | ✅ |
| 111 | `freshness_frequency_boost_alpha` | `AGENT_FRESHNESS_FREQUENCY_BOOST_ALPHA` | `float` | `0.1` |  | ✅ |
| 112 | `freshness_frequency_boost_max` | `AGENT_FRESHNESS_FREQUENCY_BOOST_MAX` | `float` | `1.5` |  | ✅ |
| 113 | `freshness_half_life_days` | `AGENT_FRESHNESS_HALF_LIFE_DAYS` | `float` | `30.0` |  | ✅ |
| 114 | `freshness_never_accessed_noise_days` | `AGENT_FRESHNESS_NEVER_ACCESSED_NOISE_DAYS` | `float` | `30.0` |  | ✅ |
| 115 | `freshness_relevance_weight` | `AGENT_FRESHNESS_RELEVANCE_WEIGHT` | `float` | `0.15` |  | ✅ |
| 116 | `freshness_review_schedule_cron` | `AGENT_FRESHNESS_REVIEW_SCHEDULE_CRON` | `str` | `'0 3 * * 0'` |  | ✅ |
| 117 | `freshness_tier_factors` | `AGENT_FRESHNESS_TIER_FACTORS` | `dict` | `{'warm': 1.0, 'cooling': 0.85, 'cold': 0.6, 'dormant': 0.3}` |  | — |
| 118 | `freshness_tier_reranking_enabled` | `AGENT_FRESHNESS_TIER_RERANKING_ENABLED` | `bool` | `True` |  | — |
| 119 | `gateway_access_config` | `AGENT_GATEWAY_ACCESS_CONFIG` | `str` | `'config/gateway_access.yaml'` |  | — |
| 120 | `gateway_auth_enabled` | `AGENT_GATEWAY_AUTH_ENABLED` | `bool` | `False` |  | ✅ |
| 121 | `gateway_mount_local` | `AGENT_GATEWAY_MOUNT_LOCAL` | `bool` | `True` |  | — |
| 122 | `governance_config_path` | `AGENT_GOVERNANCE_CONFIG_PATH` | `Path` | `PosixPath('config/governance')` |  | ✅ |
| 123 | `graph_quality_governance_enabled` | `AGENT_GRAPH_QUALITY_GOVERNANCE_ENABLED` | `bool` | `False` |  | — |
| 124 | `graph_quality_stream_enabled` | `AGENT_GRAPH_QUALITY_STREAM_ENABLED` | `bool` | `True` |  | — |
| 125 | `grounding_compliance_bar` | `AGENT_GROUNDING_COMPLIANCE_BAR` | `float` | `0.95` |  | — |
| 126 | `grounding_compliance_max_window_age_hours` | `AGENT_GROUNDING_COMPLIANCE_MAX_WINDOW_AGE_HOURS` | `int` | `1440` |  | — |
| 127 | `grounding_compliance_min_samples` | `AGENT_GROUNDING_COMPLIANCE_MIN_SAMPLES` | `int` | `20` |  | — |
| 128 | `grounding_compliance_window_size` | `AGENT_GROUNDING_COMPLIANCE_WINDOW_SIZE` | `int` | `100` |  | — |
| 129 | `grounding_enforcement_cooldown_hours` | `AGENT_GROUNDING_ENFORCEMENT_COOLDOWN_HOURS` | `int` | `24` |  | — |
| 130 | `grounding_enforcement_demote_below` | `AGENT_GROUNDING_ENFORCEMENT_DEMOTE_BELOW` | `float` | `0.9` |  | — |
| 131 | `grounding_enforcement_probation_rate` | `AGENT_GROUNDING_ENFORCEMENT_PROBATION_RATE` | `float` | `0.35` |  | — |
| 132 | `grounding_entailment_latency_budget_ms` | `AGENT_GROUNDING_ENTAILMENT_LATENCY_BUDGET_MS` | `int` | `4000` |  | — |
| 133 | `grounding_entailment_max_excerpt_chars` | `AGENT_GROUNDING_ENTAILMENT_MAX_EXCERPT_CHARS` | `int` | `6000` |  | — |
| 134 | `grounding_entailment_max_inline_checks` | `AGENT_GROUNDING_ENTAILMENT_MAX_INLINE_CHECKS` | `int` | `8` |  | — |
| 135 | `grounding_entailment_max_partial_miss_checks` | `AGENT_GROUNDING_ENTAILMENT_MAX_PARTIAL_MISS_CHECKS` | `int` | `8` |  | — |
| 136 | `grounding_entailment_sample_rate` | `AGENT_GROUNDING_ENTAILMENT_SAMPLE_RATE` | `float` | `0.05` |  | — |
| 137 | `grounding_max_generation_attempts` | `AGENT_GROUNDING_MAX_GENERATION_ATTEMPTS` | `int` | `2` |  | — |
| 138 | `grounding_verification_mode` | `AGENT_GROUNDING_VERIFICATION_MODE` | `Literal` | `'off'` |  | — |
| 139 | `insights_enabled` | `AGENT_INSIGHTS_ENABLED` | `bool` | `True` |  | ✅ |
| 140 | `insights_wiring_enabled` | `AGENT_INSIGHTS_WIRING_ENABLED` | `bool` | `True` |  | ✅ |
| 141 | `joinability_probe_enabled` | `AGENT_JOINABILITY_PROBE_ENABLED` | `bool` | `True` |  | — |
| 142 | `joinability_probe_index_prefix` | `AGENT_JOINABILITY_PROBE_INDEX_PREFIX` | `str` | `'agent-monitors-joinability'` |  | — |
| 143 | `joinability_probe_interval_seconds` | `AGENT_JOINABILITY_PROBE_INTERVAL_SECONDS` | `int` | `3600` |  | — |
| 144 | `joinability_probe_window_hours` | `AGENT_JOINABILITY_PROBE_WINDOW_HOURS` | `int` | `24` |  | — |
| 145 | `kg_stats_projection_schedule_cron` | `AGENT_KG_STATS_PROJECTION_SCHEDULE_CRON` | `str` | `'0 4 * * *'` |  | — |
| 146 | `lexical_arm_enabled` | `AGENT_LEXICAL_ARM_ENABLED` | `bool` | `False` |  | — |
| 147 | `linear_agent_rate_limit_per_day` | `AGENT_LINEAR_AGENT_RATE_LIMIT_PER_DAY` | `int` | `10` |  | ✅ |
| 148 | `linear_api_key` | `AGENT_LINEAR_API_KEY` | `str \| None` | 🔒 redacted (secret — `.env` only) | 🔑 | ✅ |
| 149 | `linear_personal_agent_label_id` | `AGENT_LINEAR_PERSONAL_AGENT_LABEL_ID` | `str \| None` | `'25004aac-3b32-4fa4-bdc2-55ff348ea842'` |  | ✅ |
| 150 | `linear_promotion_project` | `AGENT_LINEAR_PROMOTION_PROJECT` | `str` | `'Linear Async Feedback Channel'` |  | ✅ |
| 151 | `linear_team_name` | `AGENT_LINEAR_TEAM_NAME` | `str` | `'FrenchForest'` |  | ✅ |
| 152 | `llm_append_no_think_to_tool_prompts` | `AGENT_LLM_APPEND_NO_THINK_TO_TOOL_PROMPTS` | `bool` | `False` |  | ✅ |
| 153 | `llm_max_retries` | `AGENT_LLM_MAX_RETRIES` | `int` | `3` |  | ✅ |
| 154 | `llm_no_think_suffix` | `AGENT_LLM_NO_THINK_SUFFIX` | `str` | `'/no_think'` |  | ✅ |
| 155 | `llm_timeout_seconds` | `AGENT_LLM_TIMEOUT_SECONDS` | `int` | `120` |  | ✅ |
| 156 | `local_fallback_embedding_endpoint` | `AGENT_LOCAL_FALLBACK_EMBEDDING_ENDPOINT` | `str \| None` | `None` |  | ✅ |
| 157 | `local_fallback_embedding_model` | `AGENT_LOCAL_FALLBACK_EMBEDDING_MODEL` | `str` | `'Qwen/Qwen3-Embedding-8B'` |  | ✅ |
| 158 | `location_enabled` | `AGENT_LOCATION_ENABLED` | `bool` | `False` |  | ✅ |
| 159 | `location_precision` | `AGENT_LOCATION_PRECISION` | `str` | `'precise'` |  | — |
| 160 | `log_dir` | `AGENT_LOG_DIR` | `Path` | `PosixPath('telemetry/logs')` |  | ✅ |
| 161 | `log_format` | `AGENT_LOG_FORMAT` · `APP_LOG_FORMAT` | `str` | `'json'` |  | ✅ |
| 162 | `log_level` | `AGENT_LOG_LEVEL` · `APP_LOG_LEVEL` | `str` | `'INFO'` |  | ✅ |
| 163 | `managed_database_url` | `AGENT_MANAGED_DATABASE_URL` | `str \| None` | 🔒 redacted (secret — `.env` only) | 🔑 | ✅ |
| 164 | `managed_elasticsearch_url` | `AGENT_MANAGED_ELASTICSEARCH_URL` | `str \| None` | 🔒 redacted (secret — `.env` only) | 🔑 | ✅ |
| 165 | `managed_embedding_endpoint` | `AGENT_MANAGED_EMBEDDING_ENDPOINT` | `str \| None` | 🔒 redacted (secret — `.env` only) | 🔑 | ✅ |
| 166 | `managed_embedding_model` | `AGENT_MANAGED_EMBEDDING_MODEL` | `str` | `'Qwen3-Embedding-8B'` |  | ✅ |
| 167 | `managed_embedding_token` | `AGENT_MANAGED_EMBEDDING_TOKEN` | `str \| None` | 🔒 redacted (secret — `.env` only) | 🔑 | ✅ |
| 168 | `managed_neo4j_uri` | `AGENT_MANAGED_NEO4J_URI` | `str \| None` | 🔒 redacted (secret — `.env` only) | 🔑 | ✅ |
| 169 | `managed_reranker_endpoint` | `AGENT_MANAGED_RERANKER_ENDPOINT` | `str \| None` | 🔒 redacted (secret — `.env` only) | 🔑 | ✅ |
| 170 | `managed_slm_endpoint` | `AGENT_MANAGED_SLM_ENDPOINT` | `str \| None` | 🔒 redacted (secret — `.env` only) | 🔑 | ✅ |
| 171 | `mcp_gateway_command` | `AGENT_MCP_GATEWAY_COMMAND` | `list` | `['docker', 'mcp', 'gateway', 'run']` |  | ✅ |
| 172 | `mcp_gateway_enabled` | `AGENT_MCP_GATEWAY_ENABLED` | `bool` | `False` |  | ✅ |
| 173 | `mcp_gateway_enabled_servers` | `AGENT_MCP_GATEWAY_ENABLED_SERVERS` | `list` | `[]` |  | ✅ |
| 174 | `mcp_gateway_timeout_seconds` | `AGENT_MCP_GATEWAY_TIMEOUT_SECONDS` | `int` | `60` |  | ✅ |
| 175 | `metrics_daemon_buffer_size` | `AGENT_METRICS_DAEMON_BUFFER_SIZE` | `int` | `720` |  | — |
| 176 | `metrics_daemon_es_emit_interval_seconds` | `AGENT_METRICS_DAEMON_ES_EMIT_INTERVAL_SECONDS` | `float` | `30.0` |  | — |
| 177 | `metrics_daemon_poll_interval_seconds` | `AGENT_METRICS_DAEMON_POLL_INTERVAL_SECONDS` | `float` | `5.0` |  | — |
| 178 | `metrics_sampled_stream_maxlen` | `AGENT_METRICS_SAMPLED_STREAM_MAXLEN` | `int` | `720` |  | — |
| 179 | `mode_calibration_anomaly_threshold` | `AGENT_MODE_CALIBRATION_ANOMALY_THRESHOLD` | `int` | `3` |  | — |
| 180 | `mode_controller_enabled` | `AGENT_MODE_CONTROLLER_ENABLED` | `bool` | `True` |  | — |
| 181 | `mode_evaluation_interval_seconds` | `AGENT_MODE_EVALUATION_INTERVAL_SECONDS` | `float` | `30.0` |  | — |
| 182 | `mode_window_size` | `AGENT_MODE_WINDOW_SIZE` | `int` | `12` |  | — |
| 183 | `multipath_arm_top_k` | `AGENT_MULTIPATH_ARM_TOP_K` | `int` | `50` |  | — |
| 184 | `multipath_paraphrase_count` | `AGENT_MULTIPATH_PARAPHRASE_COUNT` | `int` | `3` |  | — |
| 185 | `multipath_recall_enabled` | `AGENT_MULTIPATH_RECALL_ENABLED` | `bool` | `False` |  | — |
| 186 | `multipath_rrf_k` | `AGENT_MULTIPATH_RRF_K` | `int` | `60` |  | — |
| 187 | `multiquery_arm_enabled` | `AGENT_MULTIQUERY_ARM_ENABLED` | `bool` | `False` |  | — |
| 188 | `neo4j_password` | `AGENT_NEO4J_PASSWORD` | `str` | 🔒 redacted (secret — `.env` only) | 🔑 | ✅ |
| 189 | `neo4j_uri` | `AGENT_NEO4J_URI` | `str` | `'bolt://localhost:7687'` |  | ✅ |
| 190 | `neo4j_user` | `AGENT_NEO4J_USER` | `str` | `'neo4j'` |  | ✅ |
| 191 | `openai_api_key` | `AGENT_OPENAI_API_KEY` | `str \| None` | 🔒 redacted (secret — `.env` only) | 🔑 | ✅ |
| 192 | `orchestrator_creditable_pause_limit` | `AGENT_ORCHESTRATOR_CREDITABLE_PAUSE_LIMIT` | `int` | `3` |  | — |
| 193 | `orchestrator_max_tool_iterations` | `AGENT_ORCHESTRATOR_MAX_TOOL_ITERATIONS` | `int` | `25` |  | ✅ |
| 194 | `orchestrator_spend_threshold` | `AGENT_ORCHESTRATOR_SPEND_THRESHOLD` | `int` | `6` |  | — |
| 195 | `orchestrator_task_timeout_seconds` | `AGENT_ORCHESTRATOR_TASK_TIMEOUT_SECONDS` | `int` | `900` |  | ✅ |
| 196 | `orchestrator_turn_lifetime_seconds` | `AGENT_ORCHESTRATOR_TURN_LIFETIME_SECONDS` | `int` | `1800` |  | — |
| 197 | `otel_exporter_endpoint` | `AGENT_OTEL_EXPORTER_ENDPOINT` | `str` | `'localhost:4319'` |  | ✅ |
| 198 | `outcome_ingestion_enabled` | `AGENT_OUTCOME_INGESTION_ENABLED` | `bool` | `True` |  | ✅ |
| 199 | `outcome_ingestion_hour_utc` | `AGENT_OUTCOME_INGESTION_HOUR_UTC` | `int` | `8` |  | ✅ |
| 200 | `owner_name` | `AGENT_OWNER_NAME` | `str` | `''` |  | ✅ |
| 201 | `owner_storage_allowlist` | `AGENT_OWNER_STORAGE_ALLOWLIST` | `list` | `['postgres', 'neo4j', 'elasticsearch']` |  | ✅ |
| 202 | `perplexity_api_key` | `AGENT_PERPLEXITY_API_KEY` | `str \| None` | 🔒 redacted (secret — `.env` only) | 🔑 | ✅ |
| 203 | `perplexity_base_url` | `AGENT_PERPLEXITY_BASE_URL` | `str` | `'https://api.perplexity.ai'` |  | ✅ |
| 204 | `perplexity_timeout_seconds` | `AGENT_PERPLEXITY_TIMEOUT_SECONDS` | `int` | `90` |  | ✅ |
| 205 | `planner_history_max_chars` | `AGENT_PLANNER_HISTORY_MAX_CHARS` | `int` | `60000` |  | ✅ |
| 206 | `planner_input_max_chars` | `AGENT_PLANNER_INPUT_MAX_CHARS` | `int` | `64000` |  | ✅ |
| 207 | `planner_timeout_seconds` | `AGENT_PLANNER_TIMEOUT_SECONDS` | `float` | `600.0` |  | — |
| 208 | `primitive_tools_enabled` | `AGENT_PRIMITIVE_TOOLS_ENABLED` | `bool` | `False` |  | ✅ |
| 209 | `proactive_memory_diminishing_score_floor` | `AGENT_PROACTIVE_MEMORY_DIMINISHING_SCORE_FLOOR` | `float` | `0.35` |  | ✅ |
| 210 | `proactive_memory_diminishing_score_gap` | `AGENT_PROACTIVE_MEMORY_DIMINISHING_SCORE_GAP` | `float` | `0.15` |  | ✅ |
| 211 | `proactive_memory_enabled` | `AGENT_PROACTIVE_MEMORY_ENABLED` | `bool` | `False` |  | ✅ |
| 212 | `proactive_memory_max_candidates` | `AGENT_PROACTIVE_MEMORY_MAX_CANDIDATES` | `int` | `10` |  | ✅ |
| 213 | `proactive_memory_max_injected_items` | `AGENT_PROACTIVE_MEMORY_MAX_INJECTED_ITEMS` | `int` | `5` |  | ✅ |
| 214 | `proactive_memory_max_tokens` | `AGENT_PROACTIVE_MEMORY_MAX_TOKENS` | `int` | `500` |  | ✅ |
| 215 | `proactive_memory_min_score` | `AGENT_PROACTIVE_MEMORY_MIN_SCORE` | `float` | `0.3` |  | ✅ |
| 216 | `proactive_memory_recency_half_life_days` | `AGENT_PROACTIVE_MEMORY_RECENCY_HALF_LIFE_DAYS` | `float` | `30.0` |  | ✅ |
| 217 | `proactive_memory_relevance_bound` | `AGENT_PROACTIVE_MEMORY_RELEVANCE_BOUND` | `float \| None` | `None` |  | — |
| 218 | `proactive_memory_relevance_gate_enabled` | `AGENT_PROACTIVE_MEMORY_RELEVANCE_GATE_ENABLED` | `bool` | `True` |  | — |
| 219 | `proactive_memory_rerank_relevance_bound` | `AGENT_PROACTIVE_MEMORY_RERANK_RELEVANCE_BOUND` | `float \| None` | `0.344176` |  | — |
| 220 | `proactive_memory_vector_top_k` | `AGENT_PROACTIVE_MEMORY_VECTOR_TOP_K` | `int` | `20` |  | ✅ |
| 221 | `proactive_memory_w_embedding` | `AGENT_PROACTIVE_MEMORY_W_EMBEDDING` | `float` | `0.45` |  | ✅ |
| 222 | `proactive_memory_w_entity` | `AGENT_PROACTIVE_MEMORY_W_ENTITY` | `float` | `0.25` |  | ✅ |
| 223 | `proactive_memory_w_recency` | `AGENT_PROACTIVE_MEMORY_W_RECENCY` | `float` | `0.2` |  | ✅ |
| 224 | `proactive_memory_w_topic` | `AGENT_PROACTIVE_MEMORY_W_TOPIC` | `float` | `0.1` |  | ✅ |
| 225 | `project_name` | `AGENT_PROJECT_NAME` | `str` | `'Personal Local AI Collaborator'` |  | ✅ |
| 226 | `promotion_initial_cap` | `AGENT_PROMOTION_INITIAL_CAP` | `int` | `5` |  | ✅ |
| 227 | `promotion_min_seen_count` | `AGENT_PROMOTION_MIN_SEEN_COUNT` | `int` | `3` |  | ✅ |
| 228 | `promotion_pipeline_enabled` | `AGENT_PROMOTION_PIPELINE_ENABLED` | `bool` | `True` |  | ✅ |
| 229 | `prompt_debug_capture_enabled` | `AGENT_PROMPT_DEBUG_CAPTURE_ENABLED` | `bool` | `False` |  | ✅ |
| 230 | `pwa_public_origin` | `AGENT_PWA_PUBLIC_ORIGIN` | `str` | `'https://<deployment-host>'` |  | ✅ |
| 231 | `quality_monitor_anomaly_window_days` | `AGENT_QUALITY_MONITOR_ANOMALY_WINDOW_DAYS` | `int` | `7` |  | ✅ |
| 232 | `quality_monitor_daily_run_hour_utc` | `AGENT_QUALITY_MONITOR_DAILY_RUN_HOUR_UTC` | `int` | `5` |  | ✅ |
| 233 | `quality_monitor_enabled` | `AGENT_QUALITY_MONITOR_ENABLED` | `bool` | `True` |  | ✅ |
| 234 | `r2_access_key_id` | `AGENT_R2_ACCESS_KEY_ID` | `str \| None` | `None` |  | ✅ |
| 235 | `r2_bucket_name` | `AGENT_R2_BUCKET_NAME` | `str` | `'seshat-artifacts'` |  | ✅ |
| 236 | `r2_endpoint_url` | `AGENT_R2_ENDPOINT_URL` | `str \| None` | `None` |  | ✅ |
| 237 | `r2_region` | `AGENT_R2_REGION` | `str` | `'auto'` |  | ✅ |
| 238 | `r2_secret_access_key` | `AGENT_R2_SECRET_ACCESS_KEY` | `str \| None` | 🔒 redacted (secret — `.env` only) | 🔑 | ✅ |
| 239 | `recall_candidate_cap` | `AGENT_RECALL_CANDIDATE_CAP` | `int` | `500` |  | — |
| 240 | `recall_per_entity_turn_cap` | `AGENT_RECALL_PER_ENTITY_TURN_CAP` | `int` | `10` |  | — |
| 241 | `recall_similarity_floor` | `AGENT_RECALL_SIMILARITY_FLOOR` | `float` | `0.0` |  | — |
| 242 | `relevance_bounded_recall_enabled` | `AGENT_RELEVANCE_BOUNDED_RECALL_ENABLED` | `bool` | `False` |  | — |
| 243 | `request_monitoring_enabled` | `AGENT_REQUEST_MONITORING_ENABLED` | `bool` | `True` |  | ✅ |
| 244 | `reranker_enabled` | `AGENT_RERANKER_ENABLED` | `bool` | `True` |  | ✅ |
| 245 | `reranker_input_cap` | `AGENT_RERANKER_INPUT_CAP` | `int` | `25` |  | — |
| 246 | `reranker_top_k` | `AGENT_RERANKER_TOP_K` | `int` | `10` |  | ✅ |
| 247 | `route_trace_preview_chars` | `AGENT_ROUTE_TRACE_PREVIEW_CHARS` | `int` | `280` |  | ✅ |
| 248 | `route_trace_store_preview` | `AGENT_ROUTE_TRACE_STORE_PREVIEW` | `bool` | `False` |  | ✅ |
| 249 | `sandbox_image` | `AGENT_SANDBOX_IMAGE` | `str` | `'seshat-sandbox-python:0.1'` |  | ✅ |
| 250 | `sandbox_network` | `AGENT_SANDBOX_NETWORK` | `str` | `'seshat_cloud-sim'` |  | — |
| 251 | `sandbox_scratch_root` | `AGENT_SANDBOX_SCRATCH_ROOT` | `str` | `'/app/agent_workspace/sandbox'` |  | ✅ |
| 252 | `sandbox_scratch_volume` | `AGENT_SANDBOX_SCRATCH_VOLUME` | `str` | `''` |  | ✅ |
| 253 | `scaleway_secret_key` | `AGENT_SCALEWAY_SECRET_KEY` | `str \| None` | 🔒 redacted (secret — `.env` only) | 🔑 | ✅ |
| 254 | `searxng_base_url` | `AGENT_SEARXNG_BASE_URL` | `str` | `'http://localhost:8888'` |  | ✅ |
| 255 | `searxng_default_categories` | `AGENT_SEARXNG_DEFAULT_CATEGORIES` | `str` | `'general'` |  | — |
| 256 | `searxng_max_results` | `AGENT_SEARXNG_MAX_RESULTS` | `int` | `10` |  | — |
| 257 | `searxng_timeout_seconds` | `AGENT_SEARXNG_TIMEOUT_SECONDS` | `int` | `12` |  | — |
| 258 | `second_brain_cpu_threshold` | `AGENT_SECOND_BRAIN_CPU_THRESHOLD` | `float` | `50.0` |  | ✅ |
| 259 | `second_brain_idle_time_seconds` | `AGENT_SECOND_BRAIN_IDLE_TIME_SECONDS` | `float` | `300.0` |  | ✅ |
| 260 | `second_brain_memory_threshold` | `AGENT_SECOND_BRAIN_MEMORY_THRESHOLD` | `float` | `70.0` |  | ✅ |
| 261 | `second_brain_min_interval_seconds` | `AGENT_SECOND_BRAIN_MIN_INTERVAL_SECONDS` | `float` | `3600.0` |  | ✅ |
| 262 | `second_brain_resource_gating_enabled` | `AGENT_SECOND_BRAIN_RESOURCE_GATING_ENABLED` | `bool` | `True` |  | ✅ |
| 263 | `service_port` | `AGENT_SERVICE_PORT` | `int` | `9000` |  | ✅ |
| 264 | `service_request_close_timeout_seconds` | `AGENT_SERVICE_REQUEST_CLOSE_TIMEOUT_SECONDS` | `int` | `10` |  | — |
| 265 | `service_request_task_lifetime_seconds` | `AGENT_SERVICE_REQUEST_TASK_LIFETIME_SECONDS` | `int` | `<factory>` |  | — |
| 266 | `service_url` | `AGENT_SERVICE_URL` · `SERVICE_URL` | `str` | `'http://localhost:9000'` |  | ✅ |
| 267 | `seshat_open_ticket_cap` | `AGENT_SESHAT_OPEN_TICKET_CAP` | `int` | `10` |  | ✅ |
| 268 | `session_digest_max_tokens` | `AGENT_SESSION_DIGEST_MAX_TOKENS` | `int` | `400` |  | ✅ |
| 269 | `session_digest_structured_output` | `AGENT_SESSION_DIGEST_STRUCTURED_OUTPUT` | `bool` | `True` |  | ✅ |
| 270 | `session_digest_target_tokens` | `AGENT_SESSION_DIGEST_TARGET_TOKENS` | `int` | `120` |  | ✅ |
| 271 | `session_retention_days` | `AGENT_SESSION_RETENTION_DAYS` | `int` | `180` |  | ✅ |
| 272 | `session_retention_sweep_interval_seconds` | `AGENT_SESSION_RETENTION_SWEEP_INTERVAL_SECONDS` | `int` | `86400` |  | ✅ |
| 273 | `session_summary_enabled` | `AGENT_SESSION_SUMMARY_ENABLED` | `bool` | `True` |  | ✅ |
| 274 | `session_summary_idle_threshold_seconds` | `AGENT_SESSION_SUMMARY_IDLE_THRESHOLD_SECONDS` | `float` | `900.0` |  | ✅ |
| 275 | `session_summary_max_attempts` | `AGENT_SESSION_SUMMARY_MAX_ATTEMPTS` | `int` | `2` |  | ✅ |
| 276 | `session_summary_retry_backoff_base_seconds` | `AGENT_SESSION_SUMMARY_RETRY_BACKOFF_BASE_SECONDS` | `float` | `900.0` |  | ✅ |
| 277 | `session_summary_retry_backoff_max_seconds` | `AGENT_SESSION_SUMMARY_RETRY_BACKOFF_MAX_SECONDS` | `float` | `21600.0` |  | ✅ |
| 278 | `session_summary_sweep_interval_seconds` | `AGENT_SESSION_SUMMARY_SWEEP_INTERVAL_SECONDS` | `float` | `300.0` |  | ✅ |
| 279 | `session_write_wait_timeout_seconds` | `AGENT_SESSION_WRITE_WAIT_TIMEOUT_SECONDS` | `float` | `10.0` |  | — |
| 280 | `signal_priority_clamp` | `AGENT_SIGNAL_PRIORITY_CLAMP` | `float` | `0.5` |  | ✅ |
| 281 | `signal_smoothing_prior` | `AGENT_SIGNAL_SMOOTHING_PRIOR` | `float` | `2.0` |  | ✅ |
| 282 | `signal_suppression_cooldown_days` | `AGENT_SIGNAL_SUPPRESSION_COOLDOWN_DAYS` | `int` | `30` |  | ✅ |
| 283 | `signal_suppression_min_n` | `AGENT_SIGNAL_SUPPRESSION_MIN_N` | `int` | `5` |  | ✅ |
| 284 | `signal_suppression_threshold` | `AGENT_SIGNAL_SUPPRESSION_THRESHOLD` | `float` | `-0.4` |  | ✅ |
| 285 | `signal_window_days` | `AGENT_SIGNAL_WINDOW_DAYS` | `int` | `90` |  | ✅ |
| 286 | `slm_base_url` | `AGENT_SLM_BASE_URL` | `str \| None` | `None` |  | ✅ |
| 287 | `slm_gpu_util_degraded_pct` | `AGENT_SLM_GPU_UTIL_DEGRADED_PCT` | `float` | `95.0` |  | — |
| 288 | `slm_health_cache_ttl_seconds` | `AGENT_SLM_HEALTH_CACHE_TTL_SECONDS` | `float` | `45.0` |  | — |
| 289 | `slm_health_generation_check_enabled` | `AGENT_SLM_HEALTH_GENERATION_CHECK_ENABLED` | `bool` | `False` |  | — |
| 290 | `slm_health_index_prefix` | `AGENT_SLM_HEALTH_INDEX_PREFIX` | `str` | `'agent-monitors-slm-health'` |  | — |
| 291 | `slm_health_probe_down_interval_seconds` | `AGENT_SLM_HEALTH_PROBE_DOWN_INTERVAL_SECONDS` | `float` | `60.0` |  | — |
| 292 | `slm_health_probe_enabled` | `AGENT_SLM_HEALTH_PROBE_ENABLED` | `bool` | `True` |  | — |
| 293 | `slm_health_probe_interval_seconds` | `AGENT_SLM_HEALTH_PROBE_INTERVAL_SECONDS` | `float` | `300.0` |  | — |
| 294 | `slm_health_url` | `AGENT_SLM_HEALTH_URL` | `str \| None` | `None` |  | — |
| 295 | `slm_queue_depth_degraded` | `AGENT_SLM_QUEUE_DEPTH_DEGRADED` | `int` | `4` |  | — |
| 296 | `slm_thinking_canary_enabled` | `AGENT_SLM_THINKING_CANARY_ENABLED` | `bool` | `False` |  | — |
| 297 | `structural_arm_enabled` | `AGENT_STRUCTURAL_ARM_ENABLED` | `bool` | `False` |  | — |
| 298 | `structural_arm_top_k` | `AGENT_STRUCTURAL_ARM_TOP_K` | `int` | `50` |  | — |
| 299 | `structural_class_predicate_enabled` | `AGENT_STRUCTURAL_CLASS_PREDICATE_ENABLED` | `bool` | `False` |  | — |
| 300 | `structural_type_predicate_enabled` | `AGENT_STRUCTURAL_TYPE_PREDICATE_ENABLED` | `bool` | `False` |  | — |
| 301 | `sub_agent_context_reserve_tokens` | `AGENT_SUB_AGENT_CONTEXT_RESERVE_TOKENS` | `int` | `16000` |  | — |
| 302 | `sub_agent_landing_max_tokens` | `AGENT_SUB_AGENT_LANDING_MAX_TOKENS` | `int` | `4096` |  | — |
| 303 | `sub_agent_max_tool_iterations` | `AGENT_SUB_AGENT_MAX_TOOL_ITERATIONS` | `int` | `20` |  | ✅ |
| 304 | `sub_agent_researcher_min_search_rounds` | `AGENT_SUB_AGENT_RESEARCHER_MIN_SEARCH_ROUNDS` | `int` | `0` |  | ✅ |
| 305 | `sub_agent_rounds_by_thoroughness` | `AGENT_SUB_AGENT_ROUNDS_BY_THOROUGHNESS` | `dict` | `{}` |  | ✅ |
| 306 | `substrate_profile` | `AGENT_SUBSTRATE_PROFILE` | `str` | `'private'` |  | ✅ |
| 307 | `sysgraph_database_url` | `AGENT_SYSGRAPH_DATABASE_URL` | `str` | `'postgresql+asyncpg://<redacted>@localhost:5432/personal_agent'` |  | ✅ |
| 308 | `sysgraph_maintenance_enabled` | `AGENT_SYSGRAPH_MAINTENANCE_ENABLED` | `bool` | `True` |  | — |
| 309 | `sysgraph_maintenance_hour_utc` | `AGENT_SYSGRAPH_MAINTENANCE_HOUR_UTC` | `int` | `9` |  | — |
| 310 | `tempo_url` | `AGENT_TEMPO_URL` | `str` | `'http://localhost:3200'` |  | ✅ |
| 311 | `tool_result_compression_enabled` | `AGENT_TOOL_RESULT_COMPRESSION_ENABLED` | `bool` | `False` |  | — |
| 312 | `tool_result_digest_exclude_tools` | `AGENT_TOOL_RESULT_DIGEST_EXCLUDE_TOOLS` | `list` | `[]` |  | — |
| 313 | `tool_result_digest_head_lines` | `AGENT_TOOL_RESULT_DIGEST_HEAD_LINES` | `int` | `40` |  | — |
| 314 | `tool_result_digest_max_expand_tokens` | `AGENT_TOOL_RESULT_DIGEST_MAX_EXPAND_TOKENS` | `int` | `8000` |  | — |
| 315 | `tool_result_digest_min_savings_tokens` | `AGENT_TOOL_RESULT_DIGEST_MIN_SAVINGS_TOKENS` | `int` | `500` |  | — |
| 316 | `tool_result_digest_pin_ttl_turns` | `AGENT_TOOL_RESULT_DIGEST_PIN_TTL_TURNS` | `int` | `4` |  | — |
| 317 | `tool_result_digest_put_timeout_ms` | `AGENT_TOOL_RESULT_DIGEST_PUT_TIMEOUT_MS` | `int` | `2000` |  | — |
| 318 | `tool_result_digest_tail_lines` | `AGENT_TOOL_RESULT_DIGEST_TAIL_LINES` | `int` | `20` |  | — |
| 319 | `tool_result_digest_threshold_tokens` | `AGENT_TOOL_RESULT_DIGEST_THRESHOLD_TOKENS` | `int` | `1500` |  | — |
| 320 | `turn_observed_stream_maxlen` | `AGENT_TURN_OBSERVED_STREAM_MAXLEN` | `int` | `10000` |  | — |
| 321 | `turn_projector_enabled` | `AGENT_TURN_PROJECTOR_ENABLED` | `bool` | `True` |  | — |
| 322 | `upload_max_size_bytes` | `AGENT_UPLOAD_MAX_SIZE_BYTES` | `int` | `52428800` |  | — |
| 323 | `url_guard_allowlist` | `AGENT_URL_GUARD_ALLOWLIST` | `list` | `[]` |  | — |
| 324 | `url_guard_cache_ttl_seconds` | `AGENT_URL_GUARD_CACHE_TTL_SECONDS` | `int` | `3600` |  | — |
| 325 | `url_guard_mode` | `AGENT_URL_GUARD_MODE` | `str` | `'blocklist'` |  | — |
| 326 | `url_guard_novelty_window_days` | `AGENT_URL_GUARD_NOVELTY_WINDOW_DAYS` | `int` | `14` |  | — |
| 327 | `user_display_names_json` | `AGENT_USER_DISPLAY_NAMES_JSON` | `str` | `'{}'` |  | ✅ |
| 328 | `version` | `AGENT_VERSION` | `str` | `'0.1.0'` |  | ✅ |
| 329 | `voyage_api_key` | `AGENT_VOYAGE_API_KEY` | `str \| None` | 🔒 redacted (secret — `.env` only) | 🔑 | ✅ |
| 330 | `within_session_compression_enabled` | `AGENT_WITHIN_SESSION_COMPRESSION_ENABLED` | `bool` | `True` |  | — |
| 331 | `within_session_hard_threshold_ratio` | `AGENT_WITHIN_SESSION_HARD_THRESHOLD_RATIO` | `float` | `0.85` |  | — |
| 332 | `within_session_max_tail_ratio` | `AGENT_WITHIN_SESSION_MAX_TAIL_RATIO` | `float` | `0.35` |  | — |
| 333 | `within_session_min_tail_ratio` | `AGENT_WITHIN_SESSION_MIN_TAIL_RATIO` | `float` | `0.25` |  | — |
| 334 | `within_session_pre_pass_threshold_tokens` | `AGENT_WITHIN_SESSION_PRE_PASS_THRESHOLD_TOKENS` | `int` | `800` |  | — |
| 335 | `worker_queue_absorption_seconds` | `AGENT_WORKER_QUEUE_ABSORPTION_SECONDS` | `float` | `25.0` |  | — |
| 336 | `ws_event_queue_size` | `AGENT_WS_EVENT_QUEUE_SIZE` | `int` | `500` |  | — |
| 337 | `ws_event_ttl_hours` | `AGENT_WS_EVENT_TTL_HOURS` | `int` | `24` |  | — |
| 338 | `ws_max_message_size` | `AGENT_WS_MAX_MESSAGE_SIZE` | `int` | `8192` |  | — |
| 339 | `ws_ping_timeout_seconds` | `AGENT_WS_PING_TIMEOUT_SECONDS` | `int` | `60` |  | — |
| 340 | `ws_rate_limit_per_second` | `AGENT_WS_RATE_LIMIT_PER_SECOND` | `int` | `20` |  | — |
| 341 | `ws_ticket_ttl_seconds` | `AGENT_WS_TICKET_TTL_SECONDS` | `int` | `30` |  | — |

### Orphan `AGENT_*` keys in `.env.example` (1)

`AGENT_*` keys documented in `.env.example` that bind to **no `AppConfig` field** (neither `AGENT_<FIELD>` nor any alias) and are not in the curated consumed-elsewhere allow-list (6 entries: model-loader endpoints + infra scripts). A non-empty list here is a genuine surprise (dead doc or renamed field):

- `AGENT_HOST`

### AppConfig fields not documented in `.env.example` (156)

Fields with no matching env-var line in `.env.example` — the coverage gap ADR-0099 D4 flags as a *policy* finding (undocumented config surface):

<details><summary>156 undocumented fields</summary>

- `artifact_envelope_probe_enabled`
- `artifact_envelope_probe_timeout_s`
- `attachment_cost_confirmation_threshold_usd`
- `attachment_image_max_bytes`
- `attachment_image_max_pixels`
- `attachment_max_images_per_turn`
- `attachment_max_total_payload_bytes`
- `brave_search_cost_per_call_usd`
- `broad_recall_relevance_bound`
- `broad_recall_relevance_gate_enabled`
- `build_fingerprint`
- `cache_erosion_probe_enabled`
- `cache_erosion_probe_index_prefix`
- `cache_erosion_probe_interval_seconds`
- `cache_erosion_probe_threshold`
- `cache_erosion_probe_window_days`
- `cache_frozen_accum_max_ratio`
- `cache_quality_token_weight`
- `cache_reset_min_run_turns_cloud`
- `cache_reset_min_run_turns_local`
- `captains_log_index_prefix`
- `captains_log_reflection_cadence_enabled`
- `captains_log_reflection_min_interval_seconds`
- `consolidator_max_extraction_attempts`
- `constraint_pause_timeout_seconds`
- `context_budget_max_tokens`
- `context_quality_governance_budget_reduction`
- `context_quality_governance_enabled`
- `context_quality_governance_threshold`
- `context_quality_stream_enabled`
- `delegation_enabled`
- `delivery_ratio_probe_enabled`
- `delivery_ratio_probe_index_prefix`
- `delivery_ratio_probe_interval_seconds`
- `delivery_ratio_probe_min_ratio`
- `document_max_extracted_text_chars`
- `document_max_pages_per_turn`
- `document_max_total_payload_bytes`
- `document_page_max_bytes`
- `document_page_max_pixels`
- `document_text_density_floor_per_page`
- `embedding_backfill_enabled`
- `entity_extraction_fewshot_exemplars_enabled`
- `entity_extraction_timeout_seconds`
- `entity_match_relevance_bound`
- `entity_match_relevance_gate_enabled`
- `environment`
- `eur_usd_rate`
- `exa_search_cost_per_call_usd`
- `expansion_budget_max`
- `experimental_openai_base_llm_http_handler`
- `freshness_tier_factors`
- `freshness_tier_reranking_enabled`
- `gateway_access_config`
- `gateway_mount_local`
- `graph_quality_governance_enabled`
- `graph_quality_stream_enabled`
- `grounding_compliance_bar`
- `grounding_compliance_max_window_age_hours`
- `grounding_compliance_min_samples`
- `grounding_compliance_window_size`
- `grounding_enforcement_cooldown_hours`
- `grounding_enforcement_demote_below`
- `grounding_enforcement_probation_rate`
- `grounding_entailment_latency_budget_ms`
- `grounding_entailment_max_excerpt_chars`
- `grounding_entailment_max_inline_checks`
- `grounding_entailment_max_partial_miss_checks`
- `grounding_entailment_sample_rate`
- `grounding_max_generation_attempts`
- `grounding_verification_mode`
- `joinability_probe_enabled`
- `joinability_probe_index_prefix`
- `joinability_probe_interval_seconds`
- `joinability_probe_window_hours`
- `kg_stats_projection_schedule_cron`
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
- `orchestrator_creditable_pause_limit`
- `orchestrator_spend_threshold`
- `orchestrator_turn_lifetime_seconds`
- `planner_timeout_seconds`
- `proactive_memory_relevance_bound`
- `proactive_memory_relevance_gate_enabled`
- `proactive_memory_rerank_relevance_bound`
- `recall_candidate_cap`
- `recall_per_entity_turn_cap`
- `recall_similarity_floor`
- `relevance_bounded_recall_enabled`
- `reranker_input_cap`
- `sandbox_network`
- `searxng_default_categories`
- `searxng_max_results`
- `searxng_timeout_seconds`
- `service_request_close_timeout_seconds`
- `service_request_task_lifetime_seconds`
- `session_write_wait_timeout_seconds`
- `slm_gpu_util_degraded_pct`
- `slm_health_cache_ttl_seconds`
- `slm_health_generation_check_enabled`
- `slm_health_index_prefix`
- `slm_health_probe_down_interval_seconds`
- `slm_health_probe_enabled`
- `slm_health_probe_interval_seconds`
- `slm_health_url`
- `slm_queue_depth_degraded`
- `slm_thinking_canary_enabled`
- `structural_arm_enabled`
- `structural_arm_top_k`
- `structural_class_predicate_enabled`
- `structural_type_predicate_enabled`
- `sub_agent_context_reserve_tokens`
- `sub_agent_landing_max_tokens`
- `sysgraph_maintenance_enabled`
- `sysgraph_maintenance_hour_utc`
- `tool_result_compression_enabled`
- `tool_result_digest_exclude_tools`
- `tool_result_digest_head_lines`
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
- `url_guard_mode`
- `url_guard_novelty_window_days`
- `within_session_compression_enabled`
- `within_session_hard_threshold_ratio`
- `within_session_max_tail_ratio`
- `within_session_min_tail_ratio`
- `within_session_pre_pass_threshold_tokens`
- `worker_queue_absorption_seconds`
- `ws_event_queue_size`
- `ws_event_ttl_hours`
- `ws_max_message_size`
- `ws_ping_timeout_seconds`
- `ws_rate_limit_per_second`
- `ws_ticket_ttl_seconds`

</details>

### Secret fields (18)

18 `AppConfig` fields match the tightened secret heuristic (`*_api_key`, `*_password`, `*_secret`, `*secret_access_key`, plus the internal auth token; token-budget scalars like `*_max_tokens` are excluded). Their **values are never emitted** — the default column shows a redaction marker, and any credential embedded in a DSN default (Postgres/Neo4j) is stripped by the sanitizer. The field names are enumerated in **§8**; prod secrets live only in `.env` (ADR-0007).

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

The two live profiles that write to the shared substrate were `models.yaml` (local dev, via `make dev`) and `models.cloud.yaml` (prod, pinned by `AGENT_MODEL_CONFIG_PATH` in `docker-compose.cloud.yml`). Their divergence was the operative one.

**Update (ADR-0121, FRE-916 phase 2):** this entire drift class is now **structurally closed**. `models.cloud.yaml` is deleted and `AGENT_MODEL_CONFIG_PATH` is removed — there is exactly one catalog (`config/models.yaml`), so a role cannot resolve to different models in different deployments. The two files were proven comment-only-different before the deletion, and the role-resolution snapshot (`tests/personal_agent/config/test_catalog_snapshot.py`) asserts it byte-for-byte. The sections below are retained as the point-in-time audit record.

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

**`config/governance/` (3 role-bearing/policy files).** Not `AppConfig` params — YAML policy consumed by the governance evaluator and cost gate.

| File | Top-level keys | Reader |
|---|---|---|
| `budget.yaml` | `version`, `roles` (`main_inference`, `entity_extraction`, `captains_log`, `insights`, `promotion`, `freshness`, `skill_routing`, …), `caps` | `cost_gate/` |
| `modes.yaml` | `modes`, `transition_rules` | `brainstem` mode manager |
| `tools.yaml` | `sub_agent_tools`, `tools` | `ToolRegistry` governance |

FRE-1701 deleted `safety.yaml`, the policy `models.yaml` (`mode_constraints`), and the `tools.yaml` `mode_policies` and `tool_categories` sections. No code read them.

_Note: `config/governance/tools.yaml.backup` is an untracked-style backup file colocated with the live policy — candidate cleanup (not config)._

**Other `config/` (not parameter surfaces, listed for completeness):** `gateway_access.yaml` (gateway ACL), `cloud-sim/Caddyfile` (deploy-target reverse proxy), `grafana/dashboards/*.json` (dashboard definitions), `artifact_lib_manifest.json` + `artifact_lib_substitution_map.json` (ADR-0089 curated `/lib/`).

---

## §6 — `docker-compose*.yml` `environment:` blocks (deployment provenance, ADR-0099 D2.2)

Every deployment reads the same catalog (`config/models.yaml`) since FRE-916 phase 2. What still varies per compose file is **which deployment profile** it declares, because that keys the required-secret set. Only `.cloud.yml` and `.eval.yml` run the agent service; `.yml` (local dev) and `.test.yml` run **infra only** — the agent runs via `make dev` (uvicorn) at the `local` default.

| File | Runs agent? | `AGENT_DEPLOYMENT_PROFILE` | `APP_ENV` | Notable env |
|---|---|---|---|---|
| `docker-compose.yml` | no (infra only) | — → default `local` | — | Postgres/ES/Neo4j dev passwords (`:-` defaults) |
| `docker-compose.cloud.yml` | **yes** | `cloud` | `production` | DB/Neo4j/ES/Redis/SearXNG URIs; `AGENT_GATEWAY_AUTH_ENABLED=true`; CF-Access client id/secret; `?`-required passwords |
| `docker-compose.eval.yml` | **yes** (2 services) | `eval` | `eval` | `AGENT_GATEWAY_AUTH_ENABLED=false`; MCP/primitives toggles differ between the two services; `AGENT_ANTHROPIC/OPENAI_API_KEY` passthrough |
| `docker-compose.test.yml` | no (infra only) | — | — | dev-default passwords |

**Provenance chain:** `prod` → `docker-compose.cloud.yml` → `AGENT_DEPLOYMENT_PROFILE=cloud` → required secrets `anthropic_api_key` + `openai_api_key`, and roles resolved from `config/models.yaml` → extraction `gpt-5.4-mini`, captains_log/insights `claude_sonnet` (`claude-sonnet-5`). `config_guard.check_deployment_manifest_matches_compose` fails if a compose file's declared profile disagrees with `config/deployment.yaml`, per ADR-0099 D2.2.

**Note:** `APP_ENV=eval` maps to `Environment.DEVELOPMENT` (`env_loader.get_environment` recognizes only production/staging/test). That is precisely why required-secrets is keyed on `AGENT_DEPLOYMENT_PROFILE` rather than `settings.environment` — the latter would silently drop the eval stack's cloud-secret enforcement.

---

## §7 — `.env.example` / `mcp-secrets.env.example`

`.env.example` (35 KB) is a **documentation template**: 166 `AGENT_*` keys are documented, but all bar one (`AGENT_LOCATION_ENABLED=true`) are **commented examples** — real values live only in the gitignored `.env`. Non-`AGENT_` keys documented: `APP_ENV/APP_DEBUG/APP_LOG_LEVEL/APP_LOG_FORMAT` (bind aliased AppConfig fields), `POSTGRES_PASSWORD`/`NEO4J_PASSWORD` (compose), `CF_ACCESS_*` (aliased fields), `PERSONAL_AGENT_EVAL`/`PERSONAL_AGENT_INTEGRATION` (pytest markers), `NEXT_PUBLIC_SESHAT_URL` (PWA build).

`docker/mcp/mcp-secrets.env.example` — MCP-gateway secret template (consumed by `docker/mcp/run-gateway.sh` via `AGENT_MCP_SECRETS_FILE`), not an `AppConfig` surface.

**Orphan analysis (see §1):** after alias-aware binding, **zero** documented `AGENT_*` keys fail to bind a field, once the curated consumed-elsewhere allow-list (6 model-loader/infra keys: `AGENT_EMBEDDING_ENDPOINT`, `AGENT_RERANKER_ENDPOINT`, `AGENT_MCP_SECRETS_FILE`, `AGENT_GATEWAY_TOKEN_PWA`, `AGENT_GATEWAY_TOKEN_EXTERNAL_AGENT`, `AGENT_CLOUDFLARE_TUNNEL_TOKEN`) is excluded. **113** `AppConfig` fields have no `.env.example` line (coverage gap — §1).

---

## §8 — Secret inventory (ADR-0099 D2, derived)

7 `AppConfig` fields match the tightened secret heuristic: `anthropic_api_key`, `openai_api_key`, `perplexity_api_key`, `linear_api_key`, `neo4j_password`, `r2_secret_access_key`, `artifact_resolve_internal_token`. (`cf_access_client_secret` was deleted by ADR-0132 D1 — Caddy holds the outbound CF credential now.)

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

---

## §10 — Parameter usage audit (FRE-893)

A read-evidence + override-evidence categorization of every `AppConfig` field into load-bearing / read-but-never-overridden (hardcode candidate) / never-read (dead candidate) / writer-pinned-guardrail (secrets + `owner_storage_allowlist`, never a removal candidate). Full per-field evidence, methodology, and ranked candidate lists live in the dated report — this section is a summary only, extending this doc rather than duplicating that one.

**Report:** [2026-07-16-fre-893-config-parameter-usage-audit.md](../research/2026-07-16-fre-893-config-parameter-usage-audit.md) · **308 fields categorized**

| Category | Count |
|---|---|
| `load-bearing` | 68 |
| `never-read` | 24 |
| `read-but-never-overridden` | 199 |
| `writer-pinned-guardrail` | 17 |
