# Config-parameter usage audit — FRE-893 (ADR-0099 hygiene)

> **Ticket:** [FRE-893](https://linear.app/frenchforest/issue/FRE-893) · **Backing ADR:** [ADR-0099](../architecture_decisions/ADR-0099-configuration-management-and-validation.md) · **Extends:** [CONFIG_INVENTORY.md](../reference/CONFIG_INVENTORY.md) §10 · **Generated:** 2026-07-16 · **Scope guard:** analysis and report only — zero configuration removed or changed.

## Methodology

Every one of the 311 typed `AppConfig` fields (`src/personal_agent/config/settings.py`) is categorized from evidence gathered three ways for reads and two ways for overrides:

- **Reads** — (1) `git grep` for `settings.<field>` or `getattr(settings, "<field>")` across `src/`, `scripts/`, `tests/` (excluding `settings.py` itself), tagged by root; (2) whether `settings.py` consults the field via `self.<field>` inside one of its cross-field validators; (3) whether `config/substrate.yaml` names the field via `source: "setting:<field>"` — the one dynamic `getattr(settings, field)` resolution path in the codebase (`src/personal_agent/config/substrate.py::_resolve_setting`), which no literal-string grep can trace without reading the manifest directly. Only `src`-root hits, the self-read check, or the manifest check count as **production** read evidence — a field touched only under `tests/`/`scripts/` is not treated as production load-bearing.
- **Overrides** — (1) the 5 `docker-compose*.yml` files, parsed with `pyyaml` (not raw-text regex) and checked for the field's env var in any service's `environment:` block; (2) `tests/conftest.py`'s `os.environ.setdefault("AGENT_...")` test-substrate defaults (FRE-375), tagged `test-substrate` separately from a real `compose` deployment override.

**Limitation (measure-don't-assert): the real deployed `.env` is gitignored and not in this repo.** It is the one place a field could be overridden that this audit structurally cannot see. **A field with zero in-repo override evidence is not proof it is never overridden in production — it is proof there is no repo-visible override.** Secrets in particular are expected to be overridden *only* via the real `.env`; they are placed in `writer-pinned-guardrail` specifically so this gap never reads as a false hardcode-candidate.

**Discovered finding (not fixed here — out of scope for an audit-only ticket): `config_inventory.py`'s regex-based `_is_secret()` heuristic misses 7 fields that carry the authoritative `json_schema_extra={"secret": True}` marker** (`managed_database_url`, `managed_neo4j_uri`, `managed_elasticsearch_url`, `managed_embedding_endpoint`, `managed_embedding_token`, `managed_reranker_endpoint`, `managed_slm_endpoint` — the regex's `_api_key$|_password$|_secret$|secret_access_key$` suffixes don't match `_url`/`_endpoint`/`_token`). This audit's guardrail check ORs in the schema flag so it is not affected, but `config_inventory.py`'s own secret redaction relies on the regex alone — a candidate follow-up ticket.

## Category counts

| Category | Count |
|---|---|
| `load-bearing` | 21 |
| `never-read` | 43 |
| `read-but-never-overridden` | 230 |
| `writer-pinned-guardrail` | 17 |

## Full categorized table

| Field | Category | Read evidence | Override evidence |
|---|---|---|---|
| `agent_id` | `read-but-never-overridden` | src:1 | none in-repo |
| `agent_owner_email` | `load-bearing` | src:7, scripts:5, tests:7 | docker-compose.eval.yml (compose) |
| `allow_test_writes_to_prod_substrate` | `read-but-never-overridden` | src:1, tests:1, self-read | none in-repo |
| `allowed_ws_origins` | `read-but-never-overridden` | src:1 | none in-repo |
| `anthropic_api_key` | `writer-pinned-guardrail` | src:4, scripts:2, tests:7 | docker-compose.eval.yml (compose) |
| `approval_timeout_seconds` | `read-but-never-overridden` | src:1, tests:3 | none in-repo |
| `approval_ui_enabled` | `load-bearing` | src:2, tests:5 | docker-compose.eval.yml (compose) |
| `artifact_draft_max_tokens` | `read-but-never-overridden` | src:2, tests:2 | none in-repo |
| `artifact_envelope_probe_enabled` | `read-but-never-overridden` | src:1 | none in-repo |
| `artifact_envelope_probe_timeout_s` | `read-but-never-overridden` | src:1 | none in-repo |
| `artifact_resolve_internal_token` | `writer-pinned-guardrail` | src:2 | none in-repo |
| `artifacts_public_base_url` | `read-but-never-overridden` | src:4 | none in-repo |
| `attachment_cost_confirmation_threshold_usd` | `read-but-never-overridden` | src:1 | none in-repo |
| `attachment_default_processing_target` | `read-but-never-overridden` | src:1 | none in-repo |
| `attachment_image_max_bytes` | `read-but-never-overridden` | src:2, tests:2 | none in-repo |
| `attachment_image_max_pixels` | `read-but-never-overridden` | src:2, tests:1 | none in-repo |
| `attachment_max_images_per_turn` | `read-but-never-overridden` | src:2, tests:3 | none in-repo |
| `attachment_max_total_payload_bytes` | `read-but-never-overridden` | src:1, tests:2 | none in-repo |
| `brainstem_sensor_poll_interval_seconds` | `read-but-never-overridden` | src:3 | none in-repo |
| `cache_frozen_accum_max_ratio` | `read-but-never-overridden` | src:1, tests:1 | none in-repo |
| `cache_frozen_layout_enabled` | `read-but-never-overridden` | src:5 | none in-repo |
| `cache_quality_token_weight` | `read-but-never-overridden` | src:1 | none in-repo |
| `cache_reset_min_run_turns_cloud` | `read-but-never-overridden` | src:1, tests:1 | none in-repo |
| `cache_reset_min_run_turns_local` | `read-but-never-overridden` | src:1, tests:2 | none in-repo |
| `captains_log_index_prefix` | `load-bearing` | src:5, scripts:1, tests:4 | tests/conftest.py (test-substrate) |
| `captains_log_reflection_cadence_enabled` | `read-but-never-overridden` | src:1 | none in-repo |
| `captains_log_reflection_min_interval_seconds` | `read-but-never-overridden` | src:2 | none in-repo |
| `cf_access_aud` | `read-but-never-overridden` | src:1 | none in-repo |
| `cf_access_client_id` | `load-bearing` | src:8, tests:8 | docker-compose.cloud.yml (compose) |
| `cf_access_client_secret` | `writer-pinned-guardrail` | src:8, tests:8 | docker-compose.cloud.yml (compose) |
| `cf_access_team_domain` | `read-but-never-overridden` | src:1 | none in-repo |
| `cloud_weekly_budget_usd` | `never-read` | none | docker-compose.eval.yml (compose) |
| `consolidator_max_extraction_attempts` | `never-read` | tests:1 | none in-repo |
| `context_budget_comfortable_tokens` | `never-read` | scripts:1 | none in-repo |
| `context_budget_generation_reserve_tokens` | `never-read` | none | none in-repo |
| `context_budget_max_tokens` | `read-but-never-overridden` | src:1, scripts:1 | none in-repo |
| `context_compression_enabled` | `read-but-never-overridden` | src:1 | none in-repo |
| `context_compression_threshold_ratio` | `read-but-never-overridden` | src:1, scripts:1 | none in-repo |
| `context_quality_governance_budget_reduction` | `read-but-never-overridden` | src:1 | none in-repo |
| `context_quality_governance_enabled` | `read-but-never-overridden` | src:2 | none in-repo |
| `context_quality_governance_threshold` | `read-but-never-overridden` | src:3 | none in-repo |
| `context_quality_stream_enabled` | `read-but-never-overridden` | src:2 | none in-repo |
| `context_window_max_tokens` | `read-but-never-overridden` | src:12, scripts:2, tests:2, self-read | none in-repo |
| `conversation_context_strategy` | `read-but-never-overridden` | src:1, tests:1 | none in-repo |
| `conversation_max_history_messages` | `read-but-never-overridden` | src:2 | none in-repo |
| `cors_allowed_origins` | `read-but-never-overridden` | src:1 | none in-repo |
| `data_lifecycle_enabled` | `read-but-never-overridden` | src:2, tests:4 | none in-repo |
| `database_admin_url` | `load-bearing` | tests:5, self-read | docker-compose.cloud.yml (compose), docker-compose.eval.yml (compose), tests/conftest.py (test-substrate) |
| `database_echo` | `read-but-never-overridden` | src:1 | none in-repo |
| `database_url` | `load-bearing` | src:8, scripts:13, tests:20, self-read, manifest | docker-compose.cloud.yml (compose), docker-compose.eval.yml (compose), tests/conftest.py (test-substrate) |
| `debug` | `read-but-never-overridden` | src:1 | none in-repo |
| `dedup_similarity_threshold` | `read-but-never-overridden` | src:1, scripts:1, tests:1 | none in-repo |
| `default_profile` | `read-but-never-overridden` | src:1 | none in-repo |
| `disk_usage_alert_percent` | `read-but-never-overridden` | src:2 | none in-repo |
| `document_max_extracted_text_chars` | `read-but-never-overridden` | src:1, tests:1 | none in-repo |
| `document_max_pages_per_turn` | `read-but-never-overridden` | src:1, tests:10 | none in-repo |
| `document_max_total_payload_bytes` | `read-but-never-overridden` | src:2, tests:2 | none in-repo |
| `document_page_max_bytes` | `read-but-never-overridden` | src:1, tests:1 | none in-repo |
| `document_page_max_pixels` | `read-but-never-overridden` | src:2, tests:1 | none in-repo |
| `document_text_density_floor_per_page` | `read-but-never-overridden` | src:1 | none in-repo |
| `elasticsearch_index_prefix` | `load-bearing` | src:8, scripts:11, tests:1 | tests/conftest.py (test-substrate) |
| `elasticsearch_url` | `load-bearing` | src:6, scripts:10, tests:6, self-read, manifest | docker-compose.cloud.yml (compose), docker-compose.eval.yml (compose), tests/conftest.py (test-substrate) |
| `embedding_backfill_enabled` | `read-but-never-overridden` | src:1 | none in-repo |
| `embedding_batch_size` | `never-read` | none | none in-repo |
| `embedding_dimensions` | `read-but-never-overridden` | src:8, scripts:6, tests:6 | none in-repo |
| `enable_memory_graph` | `read-but-never-overridden` | src:3, tests:2 | none in-repo |
| `enable_reasoning_role` | `never-read` | none | none in-repo |
| `enable_second_brain` | `read-but-never-overridden` | src:2, tests:2 | none in-repo |
| `entity_extraction_fewshot_exemplars_enabled` | `read-but-never-overridden` | src:1, scripts:1 | none in-repo |
| `entity_extraction_timeout_seconds` | `read-but-never-overridden` | src:2 | none in-repo |
| `environment` | `read-but-never-overridden` | src:4, scripts:10, self-read | none in-repo |
| `error_monitor_enabled` | `read-but-never-overridden` | src:1 | none in-repo |
| `error_monitor_max_patterns_per_scan` | `read-but-never-overridden` | src:1 | none in-repo |
| `error_monitor_min_occurrences` | `read-but-never-overridden` | src:2 | none in-repo |
| `error_monitor_window_hours` | `read-but-never-overridden` | src:2 | none in-repo |
| `event_bus_ack_timeout_seconds` | `never-read` | none | none in-repo |
| `event_bus_consumer_poll_interval_ms` | `read-but-never-overridden` | src:1 | none in-repo |
| `event_bus_dead_letter_stream` | `read-but-never-overridden` | src:1 | none in-repo |
| `event_bus_enabled` | `read-but-never-overridden` | src:2, scripts:1 | none in-repo |
| `event_bus_max_retries` | `read-but-never-overridden` | src:1 | none in-repo |
| `event_bus_redis_url` | `load-bearing` | src:2, scripts:1 | docker-compose.cloud.yml (compose), docker-compose.eval.yml (compose) |
| `expansion_budget_max` | `read-but-never-overridden` | src:4 | none in-repo |
| `failure_path_reflection_enabled` | `read-but-never-overridden` | src:2 | none in-repo |
| `feedback_defer_revisit_days` | `never-read` | none | none in-repo |
| `feedback_max_reevaluations` | `read-but-never-overridden` | src:2 | none in-repo |
| `feedback_polling_enabled` | `read-but-never-overridden` | src:2 | none in-repo |
| `feedback_polling_hour_utc` | `read-but-never-overridden` | src:1 | none in-repo |
| `feedback_suppression_days` | `read-but-never-overridden` | src:1 | none in-repo |
| `freshness_backfill_confirm` | `read-but-never-overridden` | src:1 | none in-repo |
| `freshness_cold_threshold_days` | `read-but-never-overridden` | src:1 | none in-repo |
| `freshness_consumer_batch_max_events` | `read-but-never-overridden` | src:1 | none in-repo |
| `freshness_consumer_batch_window_seconds` | `read-but-never-overridden` | src:1 | none in-repo |
| `freshness_dormant_entity_proposal_threshold` | `read-but-never-overridden` | src:2 | none in-repo |
| `freshness_dormant_relationship_proposal_threshold` | `never-read` | none | none in-repo |
| `freshness_enabled` | `read-but-never-overridden` | src:8, tests:9 | none in-repo |
| `freshness_frequency_boost_alpha` | `read-but-never-overridden` | src:1 | none in-repo |
| `freshness_frequency_boost_max` | `read-but-never-overridden` | src:1 | none in-repo |
| `freshness_half_life_days` | `read-but-never-overridden` | src:2 | none in-repo |
| `freshness_never_accessed_noise_days` | `never-read` | none | none in-repo |
| `freshness_relevance_weight` | `read-but-never-overridden` | src:1 | none in-repo |
| `freshness_review_schedule_cron` | `read-but-never-overridden` | src:1 | none in-repo |
| `freshness_tier_factors` | `read-but-never-overridden` | src:1 | none in-repo |
| `freshness_tier_reranking_enabled` | `read-but-never-overridden` | src:1 | none in-repo |
| `gateway_access_config` | `read-but-never-overridden` | src:1 | none in-repo |
| `gateway_auth_enabled` | `load-bearing` | src:7, tests:3 | docker-compose.cloud.yml (compose), docker-compose.eval.yml (compose) |
| `gateway_mount_local` | `read-but-never-overridden` | src:4 | none in-repo |
| `governance_config_path` | `read-but-never-overridden` | src:7, tests:3 | none in-repo |
| `graph_quality_governance_enabled` | `read-but-never-overridden` | src:1 | none in-repo |
| `graph_quality_stream_enabled` | `read-but-never-overridden` | src:2 | none in-repo |
| `insights_daily_run_hour_utc` | `never-read` | none | none in-repo |
| `insights_enabled` | `read-but-never-overridden` | src:1, tests:4 | none in-repo |
| `insights_weekly_day` | `never-read` | none | none in-repo |
| `insights_weekly_run_hour_utc` | `never-read` | none | none in-repo |
| `insights_wiring_enabled` | `never-read` | none | none in-repo |
| `issue_budget_threshold` | `read-but-never-overridden` | src:7 | none in-repo |
| `joinability_probe_enabled` | `read-but-never-overridden` | src:2 | none in-repo |
| `joinability_probe_index_prefix` | `read-but-never-overridden` | src:4, scripts:2 | none in-repo |
| `joinability_probe_interval_seconds` | `never-read` | none | none in-repo |
| `joinability_probe_window_hours` | `read-but-never-overridden` | src:1 | none in-repo |
| `lexical_arm_enabled` | `read-but-never-overridden` | src:2, tests:2 | none in-repo |
| `linear_agent_rate_limit_per_day` | `read-but-never-overridden` | src:1 | none in-repo |
| `linear_api_key` | `writer-pinned-guardrail` | src:7, tests:1 | none in-repo |
| `linear_personal_agent_label_id` | `read-but-never-overridden` | src:2 | none in-repo |
| `linear_promotion_project` | `read-but-never-overridden` | src:1 | none in-repo |
| `linear_team_name` | `read-but-never-overridden` | src:13 | none in-repo |
| `llm_append_no_think_to_tool_prompts` | `read-but-never-overridden` | src:2, tests:2 | none in-repo |
| `llm_base_url` | `read-but-never-overridden` | src:10, scripts:1, tests:2, manifest | none in-repo |
| `llm_max_retries` | `read-but-never-overridden` | src:3 | none in-repo |
| `llm_no_think_suffix` | `read-but-never-overridden` | src:2, tests:1 | none in-repo |
| `llm_timeout_seconds` | `read-but-never-overridden` | src:10 | none in-repo |
| `local_fallback_embedding_endpoint` | `read-but-never-overridden` | src:3 | none in-repo |
| `local_fallback_embedding_model` | `read-but-never-overridden` | src:2 | none in-repo |
| `location_enabled` | `read-but-never-overridden` | src:4 | none in-repo |
| `location_precision` | `read-but-never-overridden` | src:2 | none in-repo |
| `log_dir` | `read-but-never-overridden` | src:2 | none in-repo |
| `log_format` | `never-read` | none | none in-repo |
| `log_level` | `load-bearing` | src:6 | docker-compose.eval.yml (compose) |
| `managed_database_url` | `writer-pinned-guardrail` | manifest | none in-repo |
| `managed_elasticsearch_url` | `writer-pinned-guardrail` | manifest | none in-repo |
| `managed_embedding_endpoint` | `writer-pinned-guardrail` | src:1, manifest | none in-repo |
| `managed_embedding_model` | `read-but-never-overridden` | src:2 | none in-repo |
| `managed_embedding_token` | `writer-pinned-guardrail` | src:1 | none in-repo |
| `managed_neo4j_uri` | `writer-pinned-guardrail` | manifest | none in-repo |
| `managed_reranker_endpoint` | `writer-pinned-guardrail` | manifest | none in-repo |
| `managed_slm_endpoint` | `writer-pinned-guardrail` | manifest | none in-repo |
| `mcp_gateway_command` | `load-bearing` | src:3 | docker-compose.cloud.yml (compose) |
| `mcp_gateway_enabled` | `load-bearing` | src:3, tests:1 | docker-compose.eval.yml (compose) |
| `mcp_gateway_enabled_servers` | `read-but-never-overridden` | src:2 | none in-repo |
| `mcp_gateway_timeout_seconds` | `read-but-never-overridden` | src:1 | none in-repo |
| `metrics_daemon_buffer_size` | `read-but-never-overridden` | src:2 | none in-repo |
| `metrics_daemon_es_emit_interval_seconds` | `read-but-never-overridden` | src:2 | none in-repo |
| `metrics_daemon_poll_interval_seconds` | `read-but-never-overridden` | src:2 | none in-repo |
| `metrics_sampled_stream_maxlen` | `read-but-never-overridden` | src:1 | none in-repo |
| `mode_calibration_anomaly_threshold` | `read-but-never-overridden` | src:2 | none in-repo |
| `mode_controller_enabled` | `read-but-never-overridden` | src:3 | none in-repo |
| `mode_evaluation_interval_seconds` | `read-but-never-overridden` | src:2 | none in-repo |
| `mode_window_size` | `read-but-never-overridden` | src:2 | none in-repo |
| `model_config_path` | `load-bearing` | src:12, scripts:4, tests:4 | docker-compose.cloud.yml (compose), docker-compose.eval.yml (compose) |
| `multipath_arm_top_k` | `read-but-never-overridden` | src:3 | none in-repo |
| `multipath_paraphrase_count` | `read-but-never-overridden` | src:1 | none in-repo |
| `multipath_recall_enabled` | `read-but-never-overridden` | src:1, tests:2 | none in-repo |
| `multipath_rrf_k` | `read-but-never-overridden` | src:2 | none in-repo |
| `multiquery_arm_enabled` | `read-but-never-overridden` | src:2, tests:2 | none in-repo |
| `neo4j_password` | `writer-pinned-guardrail` | src:2, scripts:21, tests:15 | docker-compose.cloud.yml (compose), docker-compose.eval.yml (compose) |
| `neo4j_uri` | `load-bearing` | src:5, scripts:35, tests:15, self-read, manifest | docker-compose.cloud.yml (compose), docker-compose.eval.yml (compose), tests/conftest.py (test-substrate) |
| `neo4j_user` | `load-bearing` | src:2, scripts:20, tests:12 | docker-compose.eval.yml (compose) |
| `openai_api_key` | `writer-pinned-guardrail` | src:2, scripts:2, tests:1 | docker-compose.eval.yml (compose) |
| `orchestration_mode` | `read-but-never-overridden` | src:3 | none in-repo |
| `orchestrator_max_concurrent_tasks` | `read-but-never-overridden` | src:1 | none in-repo |
| `orchestrator_max_repeated_tool_calls` | `never-read` | tests:1 | none in-repo |
| `orchestrator_max_tool_iterations` | `read-but-never-overridden` | src:1, tests:2 | none in-repo |
| `orchestrator_max_tool_iterations_by_task_type` | `read-but-never-overridden` | src:1 | none in-repo |
| `orchestrator_task_timeout_seconds` | `never-read` | none | none in-repo |
| `outcome_ingestion_enabled` | `read-but-never-overridden` | src:1 | none in-repo |
| `outcome_ingestion_hour_utc` | `read-but-never-overridden` | src:1 | none in-repo |
| `owner_name` | `read-but-never-overridden` | src:2 | none in-repo |
| `owner_storage_allowlist` | `writer-pinned-guardrail` | self-read | none in-repo |
| `perplexity_api_key` | `writer-pinned-guardrail` | src:1, tests:10 | none in-repo |
| `perplexity_base_url` | `read-but-never-overridden` | src:2, tests:9 | none in-repo |
| `perplexity_timeout_seconds` | `read-but-never-overridden` | src:2, tests:9 | none in-repo |
| `planner_timeout_seconds` | `read-but-never-overridden` | src:1, tests:1 | none in-repo |
| `prefer_primitives_enabled` | `load-bearing` | src:4, tests:2 | docker-compose.eval.yml (compose) |
| `primitive_tools_enabled` | `load-bearing` | src:2, tests:1 | docker-compose.eval.yml (compose) |
| `proactive_memory_diminishing_score_floor` | `never-read` | none | none in-repo |
| `proactive_memory_diminishing_score_gap` | `never-read` | none | none in-repo |
| `proactive_memory_enabled` | `read-but-never-overridden` | src:1 | none in-repo |
| `proactive_memory_max_candidates` | `never-read` | none | none in-repo |
| `proactive_memory_max_injected_items` | `never-read` | none | none in-repo |
| `proactive_memory_max_tokens` | `never-read` | none | none in-repo |
| `proactive_memory_min_score` | `never-read` | none | none in-repo |
| `proactive_memory_recency_half_life_days` | `never-read` | none | none in-repo |
| `proactive_memory_vector_top_k` | `read-but-never-overridden` | src:2, scripts:3 | none in-repo |
| `proactive_memory_w_embedding` | `never-read` | none | none in-repo |
| `proactive_memory_w_entity` | `never-read` | none | none in-repo |
| `proactive_memory_w_recency` | `never-read` | none | none in-repo |
| `proactive_memory_w_topic` | `never-read` | none | none in-repo |
| `profiles_dir` | `never-read` | none | none in-repo |
| `project_name` | `read-but-never-overridden` | src:1 | none in-repo |
| `promotion_initial_cap` | `read-but-never-overridden` | src:1 | none in-repo |
| `promotion_pipeline_enabled` | `read-but-never-overridden` | src:1 | none in-repo |
| `quality_monitor_anomaly_window_days` | `never-read` | none | none in-repo |
| `quality_monitor_daily_run_hour_utc` | `never-read` | none | none in-repo |
| `quality_monitor_enabled` | `read-but-never-overridden` | src:2 | none in-repo |
| `r2_access_key_id` | `read-but-never-overridden` | src:3 | none in-repo |
| `r2_bucket_name` | `read-but-never-overridden` | src:4 | none in-repo |
| `r2_endpoint_url` | `read-but-never-overridden` | src:3 | none in-repo |
| `r2_region` | `read-but-never-overridden` | src:1 | none in-repo |
| `r2_secret_access_key` | `writer-pinned-guardrail` | src:3 | none in-repo |
| `recall_candidate_cap` | `read-but-never-overridden` | src:1 | none in-repo |
| `recall_per_entity_turn_cap` | `read-but-never-overridden` | src:1 | none in-repo |
| `recall_similarity_floor` | `read-but-never-overridden` | src:3, scripts:3, tests:2 | none in-repo |
| `reflection_recall_enabled` | `read-but-never-overridden` | src:2 | none in-repo |
| `reflection_recall_max_results` | `read-but-never-overridden` | src:1 | none in-repo |
| `reflection_recall_min_seen_count` | `read-but-never-overridden` | src:1 | none in-repo |
| `reflection_recall_recency_days` | `read-but-never-overridden` | src:1 | none in-repo |
| `relevance_bounded_recall_enabled` | `read-but-never-overridden` | src:2, scripts:1, tests:2 | none in-repo |
| `request_monitoring_enabled` | `read-but-never-overridden` | src:3, tests:1 | none in-repo |
| `request_monitoring_include_gpu` | `read-but-never-overridden` | src:2 | none in-repo |
| `request_monitoring_interval_seconds` | `read-but-never-overridden` | src:2 | none in-repo |
| `reranker_enabled` | `read-but-never-overridden` | src:4, tests:4 | none in-repo |
| `reranker_input_cap` | `read-but-never-overridden` | src:3 | none in-repo |
| `reranker_top_k` | `read-but-never-overridden` | src:3 | none in-repo |
| `route_trace_preview_chars` | `read-but-never-overridden` | src:1 | none in-repo |
| `route_trace_store_preview` | `read-but-never-overridden` | src:1 | none in-repo |
| `router_role` | `never-read` | none | none in-repo |
| `router_timeout_seconds` | `never-read` | none | none in-repo |
| `routing_heuristic_threshold` | `never-read` | none | none in-repo |
| `routing_policy` | `never-read` | none | none in-repo |
| `sandbox_image` | `read-but-never-overridden` | src:2, tests:1 | none in-repo |
| `sandbox_scratch_root` | `read-but-never-overridden` | src:2, tests:1 | none in-repo |
| `searxng_base_url` | `load-bearing` | src:2 | docker-compose.cloud.yml (compose), docker-compose.eval.yml (compose) |
| `searxng_default_categories` | `read-but-never-overridden` | src:1 | none in-repo |
| `searxng_max_results` | `read-but-never-overridden` | src:1 | none in-repo |
| `searxng_timeout_seconds` | `read-but-never-overridden` | src:2 | none in-repo |
| `second_brain_cpu_threshold` | `read-but-never-overridden` | src:3 | none in-repo |
| `second_brain_idle_time_seconds` | `read-but-never-overridden` | src:1 | none in-repo |
| `second_brain_memory_threshold` | `read-but-never-overridden` | src:3 | none in-repo |
| `second_brain_min_interval_seconds` | `read-but-never-overridden` | src:1 | none in-repo |
| `second_brain_resource_gating_enabled` | `never-read` | none | none in-repo |
| `service_host` | `never-read` | none | none in-repo |
| `service_port` | `read-but-never-overridden` | src:1 | none in-repo |
| `service_url` | `read-but-never-overridden` | src:3 | none in-repo |
| `session_retention_days` | `read-but-never-overridden` | src:3 | none in-repo |
| `session_retention_sweep_interval_seconds` | `read-but-never-overridden` | src:1 | none in-repo |
| `session_summary_enabled` | `read-but-never-overridden` | src:1 | none in-repo |
| `session_write_wait_timeout_seconds` | `read-but-never-overridden` | src:2 | none in-repo |
| `signal_priority_clamp` | `read-but-never-overridden` | src:1 | none in-repo |
| `signal_smoothing_prior` | `read-but-never-overridden` | src:1 | none in-repo |
| `signal_suppression_cooldown_days` | `read-but-never-overridden` | src:1 | none in-repo |
| `signal_suppression_min_n` | `read-but-never-overridden` | src:1 | none in-repo |
| `signal_suppression_threshold` | `read-but-never-overridden` | src:1 | none in-repo |
| `signal_window_days` | `read-but-never-overridden` | src:1 | none in-repo |
| `skill_index_max_tokens` | `read-but-never-overridden` | src:4 | none in-repo |
| `skill_index_p95_token_threshold` | `read-but-never-overridden` | src:1 | none in-repo |
| `skill_nudge_enabled` | `read-but-never-overridden` | src:2 | none in-repo |
| `skill_routing_mode` | `read-but-never-overridden` | src:3 | none in-repo |
| `skill_routing_model_key` | `read-but-never-overridden` | src:5, tests:1 | none in-repo |
| `skill_routing_threshold_monitor_enabled` | `read-but-never-overridden` | src:1 | none in-repo |
| `skill_routing_threshold_monitor_hour_utc` | `read-but-never-overridden` | src:1 | none in-repo |
| `slm_gpu_util_degraded_pct` | `read-but-never-overridden` | src:2, tests:5 | none in-repo |
| `slm_health_cache_ttl_seconds` | `read-but-never-overridden` | src:1 | none in-repo |
| `slm_health_index_prefix` | `read-but-never-overridden` | src:2 | none in-repo |
| `slm_health_probe_enabled` | `read-but-never-overridden` | src:2 | none in-repo |
| `slm_health_probe_interval_seconds` | `never-read` | none | none in-repo |
| `slm_health_url` | `read-but-never-overridden` | src:2, tests:5 | none in-repo |
| `slm_queue_depth_degraded` | `read-but-never-overridden` | src:2, tests:5 | none in-repo |
| `structural_arm_enabled` | `read-but-never-overridden` | src:2 | none in-repo |
| `structural_arm_top_k` | `read-but-never-overridden` | src:1 | none in-repo |
| `structural_class_predicate_enabled` | `read-but-never-overridden` | src:2 | none in-repo |
| `structural_type_predicate_enabled` | `read-but-never-overridden` | src:2 | none in-repo |
| `sub_agent_max_tokens` | `read-but-never-overridden` | src:2, tests:1 | none in-repo |
| `sub_agent_timeout_seconds` | `read-but-never-overridden` | src:1 | none in-repo |
| `substrate_profile` | `load-bearing` | src:1, self-read | tests/conftest.py (test-substrate) |
| `synthesis_timeout_seconds` | `never-read` | none | none in-repo |
| `sysgraph_database_url` | `load-bearing` | src:4, scripts:1, tests:5, self-read | tests/conftest.py (test-substrate) |
| `sysgraph_maintenance_enabled` | `read-but-never-overridden` | src:1, tests:3 | none in-repo |
| `sysgraph_maintenance_hour_utc` | `read-but-never-overridden` | src:1 | none in-repo |
| `tool_result_compression_enabled` | `read-but-never-overridden` | src:2 | none in-repo |
| `tool_result_digest_exclude_tools` | `read-but-never-overridden` | src:1 | none in-repo |
| `tool_result_digest_head_lines` | `read-but-never-overridden` | src:1 | none in-repo |
| `tool_result_digest_keep` | `never-read` | none | none in-repo |
| `tool_result_digest_max_expand_tokens` | `read-but-never-overridden` | src:1 | none in-repo |
| `tool_result_digest_min_savings_tokens` | `read-but-never-overridden` | src:1 | none in-repo |
| `tool_result_digest_pin_ttl_turns` | `read-but-never-overridden` | src:1 | none in-repo |
| `tool_result_digest_put_timeout_ms` | `read-but-never-overridden` | src:1 | none in-repo |
| `tool_result_digest_tail_lines` | `read-but-never-overridden` | src:1 | none in-repo |
| `tool_result_digest_threshold_tokens` | `read-but-never-overridden` | src:1, tests:1 | none in-repo |
| `turn_observed_stream_maxlen` | `read-but-never-overridden` | src:5, tests:1 | none in-repo |
| `turn_projector_enabled` | `read-but-never-overridden` | src:1 | none in-repo |
| `upload_max_size_bytes` | `read-but-never-overridden` | src:2 | none in-repo |
| `url_guard_allowlist` | `read-but-never-overridden` | src:1, tests:1 | none in-repo |
| `url_guard_cache_ttl_seconds` | `read-but-never-overridden` | src:1 | none in-repo |
| `url_guard_enabled` | `never-read` | none | none in-repo |
| `url_guard_mode` | `read-but-never-overridden` | src:1 | none in-repo |
| `use_service_mode` | `never-read` | none | none in-repo |
| `user_display_names_json` | `read-but-never-overridden` | self-read | none in-repo |
| `version` | `read-but-never-overridden` | src:1 | none in-repo |
| `voyage_api_key` | `writer-pinned-guardrail` | src:2 | none in-repo |
| `within_session_compression_enabled` | `read-but-never-overridden` | src:2, scripts:1 | none in-repo |
| `within_session_compression_refire_after_messages` | `read-but-never-overridden` | src:2, scripts:1 | none in-repo |
| `within_session_hard_threshold_ratio` | `read-but-never-overridden` | src:1, scripts:1 | none in-repo |
| `within_session_min_tail_ratio` | `read-but-never-overridden` | src:4, self-read | none in-repo |
| `within_session_pre_pass_threshold_tokens` | `read-but-never-overridden` | src:3 | none in-repo |
| `worker_global_timeout_seconds` | `read-but-never-overridden` | src:1, tests:1 | none in-repo |
| `worker_timeout_seconds` | `read-but-never-overridden` | src:1, tests:1 | none in-repo |
| `ws_event_queue_size` | `read-but-never-overridden` | src:1 | none in-repo |
| `ws_event_ttl_hours` | `read-but-never-overridden` | src:1 | none in-repo |
| `ws_max_message_size` | `read-but-never-overridden` | src:1 | none in-repo |
| `ws_ping_timeout_seconds` | `read-but-never-overridden` | src:1 | none in-repo |
| `ws_rate_limit_per_second` | `read-but-never-overridden` | src:1 | none in-repo |
| `ws_ticket_ttl_seconds` | `read-but-never-overridden` | src:2, tests:1 | none in-repo |

## Dead-config candidates — never-read (43)

Zero read evidence anywhere (`src/`, self-referential validator, or the substrate manifest). Candidates for removal — a separate, owner-gated follow-up ticket, not this one.

- `cloud_weekly_budget_usd`
- `consolidator_max_extraction_attempts`
- `context_budget_comfortable_tokens`
- `context_budget_generation_reserve_tokens`
- `embedding_batch_size`
- `enable_reasoning_role`
- `event_bus_ack_timeout_seconds`
- `feedback_defer_revisit_days`
- `freshness_dormant_relationship_proposal_threshold`
- `freshness_never_accessed_noise_days`
- `insights_daily_run_hour_utc`
- `insights_weekly_day`
- `insights_weekly_run_hour_utc`
- `insights_wiring_enabled`
- `joinability_probe_interval_seconds`
- `log_format`
- `orchestrator_max_repeated_tool_calls`
- `orchestrator_task_timeout_seconds`
- `proactive_memory_diminishing_score_floor`
- `proactive_memory_diminishing_score_gap`
- `proactive_memory_max_candidates`
- `proactive_memory_max_injected_items`
- `proactive_memory_max_tokens`
- `proactive_memory_min_score`
- `proactive_memory_recency_half_life_days`
- `proactive_memory_w_embedding`
- `proactive_memory_w_entity`
- `proactive_memory_w_recency`
- `proactive_memory_w_topic`
- `profiles_dir`
- `quality_monitor_anomaly_window_days`
- `quality_monitor_daily_run_hour_utc`
- `router_role`
- `router_timeout_seconds`
- `routing_heuristic_threshold`
- `routing_policy`
- `second_brain_resource_gating_enabled`
- `service_host`
- `slm_health_probe_interval_seconds`
- `synthesis_timeout_seconds`
- `tool_result_digest_keep`
- `url_guard_enabled`
- `use_service_mode`

## Hardcode candidates — read-but-never-overridden (230)

Read in production code, but with no repo-visible override anywhere — candidates to hardcode and remove from the configurable surface. Per the limitation above, this is not proof the real `.env` never overrides them; each is a candidate for owner review, not an automatic removal.

- `agent_id`
- `allow_test_writes_to_prod_substrate`
- `allowed_ws_origins`
- `approval_timeout_seconds`
- `artifact_draft_max_tokens`
- `artifact_envelope_probe_enabled`
- `artifact_envelope_probe_timeout_s`
- `artifacts_public_base_url`
- `attachment_cost_confirmation_threshold_usd`
- `attachment_default_processing_target`
- `attachment_image_max_bytes`
- `attachment_image_max_pixels`
- `attachment_max_images_per_turn`
- `attachment_max_total_payload_bytes`
- `brainstem_sensor_poll_interval_seconds`
- `cache_frozen_accum_max_ratio`
- `cache_frozen_layout_enabled`
- `cache_quality_token_weight`
- `cache_reset_min_run_turns_cloud`
- `cache_reset_min_run_turns_local`
- `captains_log_reflection_cadence_enabled`
- `captains_log_reflection_min_interval_seconds`
- `cf_access_aud`
- `cf_access_team_domain`
- `context_budget_max_tokens`
- `context_compression_enabled`
- `context_compression_threshold_ratio`
- `context_quality_governance_budget_reduction`
- `context_quality_governance_enabled`
- `context_quality_governance_threshold`
- `context_quality_stream_enabled`
- `context_window_max_tokens`
- `conversation_context_strategy`
- `conversation_max_history_messages`
- `cors_allowed_origins`
- `data_lifecycle_enabled`
- `database_echo`
- `debug`
- `dedup_similarity_threshold`
- `default_profile`
- `disk_usage_alert_percent`
- `document_max_extracted_text_chars`
- `document_max_pages_per_turn`
- `document_max_total_payload_bytes`
- `document_page_max_bytes`
- `document_page_max_pixels`
- `document_text_density_floor_per_page`
- `embedding_backfill_enabled`
- `embedding_dimensions`
- `enable_memory_graph`
- `enable_second_brain`
- `entity_extraction_fewshot_exemplars_enabled`
- `entity_extraction_timeout_seconds`
- `environment`
- `error_monitor_enabled`
- `error_monitor_max_patterns_per_scan`
- `error_monitor_min_occurrences`
- `error_monitor_window_hours`
- `event_bus_consumer_poll_interval_ms`
- `event_bus_dead_letter_stream`
- `event_bus_enabled`
- `event_bus_max_retries`
- `expansion_budget_max`
- `failure_path_reflection_enabled`
- `feedback_max_reevaluations`
- `feedback_polling_enabled`
- `feedback_polling_hour_utc`
- `feedback_suppression_days`
- `freshness_backfill_confirm`
- `freshness_cold_threshold_days`
- `freshness_consumer_batch_max_events`
- `freshness_consumer_batch_window_seconds`
- `freshness_dormant_entity_proposal_threshold`
- `freshness_enabled`
- `freshness_frequency_boost_alpha`
- `freshness_frequency_boost_max`
- `freshness_half_life_days`
- `freshness_relevance_weight`
- `freshness_review_schedule_cron`
- `freshness_tier_factors`
- `freshness_tier_reranking_enabled`
- `gateway_access_config`
- `gateway_mount_local`
- `governance_config_path`
- `graph_quality_governance_enabled`
- `graph_quality_stream_enabled`
- `insights_enabled`
- `issue_budget_threshold`
- `joinability_probe_enabled`
- `joinability_probe_index_prefix`
- `joinability_probe_window_hours`
- `lexical_arm_enabled`
- `linear_agent_rate_limit_per_day`
- `linear_personal_agent_label_id`
- `linear_promotion_project`
- `linear_team_name`
- `llm_append_no_think_to_tool_prompts`
- `llm_base_url`
- `llm_max_retries`
- `llm_no_think_suffix`
- `llm_timeout_seconds`
- `local_fallback_embedding_endpoint`
- `local_fallback_embedding_model`
- `location_enabled`
- `location_precision`
- `log_dir`
- `managed_embedding_model`
- `mcp_gateway_enabled_servers`
- `mcp_gateway_timeout_seconds`
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
- `orchestrator_max_concurrent_tasks`
- `orchestrator_max_tool_iterations`
- `orchestrator_max_tool_iterations_by_task_type`
- `outcome_ingestion_enabled`
- `outcome_ingestion_hour_utc`
- `owner_name`
- `perplexity_base_url`
- `perplexity_timeout_seconds`
- `planner_timeout_seconds`
- `proactive_memory_enabled`
- `proactive_memory_vector_top_k`
- `project_name`
- `promotion_initial_cap`
- `promotion_pipeline_enabled`
- `quality_monitor_enabled`
- `r2_access_key_id`
- `r2_bucket_name`
- `r2_endpoint_url`
- `r2_region`
- `recall_candidate_cap`
- `recall_per_entity_turn_cap`
- `recall_similarity_floor`
- `reflection_recall_enabled`
- `reflection_recall_max_results`
- `reflection_recall_min_seen_count`
- `reflection_recall_recency_days`
- `relevance_bounded_recall_enabled`
- `request_monitoring_enabled`
- `request_monitoring_include_gpu`
- `request_monitoring_interval_seconds`
- `reranker_enabled`
- `reranker_input_cap`
- `reranker_top_k`
- `route_trace_preview_chars`
- `route_trace_store_preview`
- `sandbox_image`
- `sandbox_scratch_root`
- `searxng_default_categories`
- `searxng_max_results`
- `searxng_timeout_seconds`
- `second_brain_cpu_threshold`
- `second_brain_idle_time_seconds`
- `second_brain_memory_threshold`
- `second_brain_min_interval_seconds`
- `service_port`
- `service_url`
- `session_retention_days`
- `session_retention_sweep_interval_seconds`
- `session_summary_enabled`
- `session_write_wait_timeout_seconds`
- `signal_priority_clamp`
- `signal_smoothing_prior`
- `signal_suppression_cooldown_days`
- `signal_suppression_min_n`
- `signal_suppression_threshold`
- `signal_window_days`
- `skill_index_max_tokens`
- `skill_index_p95_token_threshold`
- `skill_nudge_enabled`
- `skill_routing_mode`
- `skill_routing_model_key`
- `skill_routing_threshold_monitor_enabled`
- `skill_routing_threshold_monitor_hour_utc`
- `slm_gpu_util_degraded_pct`
- `slm_health_cache_ttl_seconds`
- `slm_health_index_prefix`
- `slm_health_probe_enabled`
- `slm_health_url`
- `slm_queue_depth_degraded`
- `structural_arm_enabled`
- `structural_arm_top_k`
- `structural_class_predicate_enabled`
- `structural_type_predicate_enabled`
- `sub_agent_max_tokens`
- `sub_agent_timeout_seconds`
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
- `user_display_names_json`
- `version`
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
