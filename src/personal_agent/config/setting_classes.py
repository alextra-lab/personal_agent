"""When an edit of each settings menu parameter applies (FRE-1724; FRE-1698).

The class of a parameter comes from a measurement of its reads, not from its meaning. The table is
data, and a test fails when a menu row has no entry in it, so a new setting cannot be silently left
out. A second test fails when a ``next_turn`` field gains a read at import, in ``__init__`` or in
``lifespan``.

- ``next_turn``: every gateway read runs at call time. An edit reaches the next read. For a background
  job, that is its next run.
- ``next_restart``: at least one read runs once per process. The running gateway keeps one value, so
  an edit reaches every reader at the next start.
- ``read_only``: no override can reach the reads, or the value is a secret. The menu shows the reason.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

ApplyClass = Literal["next_turn", "next_restart", "read_only"]

_SECRET = "this is a secret or a credential, so its value stays hidden. Set it in .env"
_OVERRIDE_STORE = "the override store is in this database, so an override cannot reach it"
_IMPORT = "the gateway reads this while it imports, before it can read the override store"
_EARLY_START = (
    "the gateway reads this at its first start step, before it can read the override store"
)
_SETTINGS_LOAD = (
    "only the checks that run while the settings load read this, before an override exists"
)
_LOGGING = "logging reads its own environment variable before the settings load"
_PROFILE = (
    "the checks that run while the settings load use this, and later reads must agree with them"
)
_LOCKOUT = (
    "a wrong value locks the owner out or stops the gateway, and this menu then cannot undo it"
)
_COMMAND = "this is a process that the gateway starts, so the menu does not edit it"
_BUILD = "the image build writes this, and the freshness check compares it with the image"
_COMPOSE_PORT = "the compose command sets the port that the gateway listens on"
_CLI_ONLY = "only a command-line tool reads this, in its own process, which holds no override"

#: ``AppConfig`` fields whose every gateway read runs at call time.
NEXT_TURN_SETTINGS: frozenset[str] = frozenset(
    {
        "allowed_ws_origins",
        "approval_timeout_seconds",
        "approval_ui_enabled",
        "artifact_draft_max_tokens",
        "artifact_envelope_probe_enabled",
        "artifact_envelope_probe_timeout_s",
        "artifacts_egress_base_url",
        "attachment_cost_confirmation_threshold_usd",
        "attachment_image_max_bytes",
        "attachment_image_max_pixels",
        "attachment_max_images_per_turn",
        "attachment_max_total_payload_bytes",
        "brave_search_cost_per_call_usd",
        "broad_recall_relevance_bound",
        "broad_recall_relevance_gate_enabled",
        "cache_erosion_probe_index_prefix",
        "cache_erosion_probe_threshold",
        "cache_erosion_probe_window_days",
        "cache_frozen_accum_max_ratio",
        "cache_quality_token_weight",
        "cache_reset_min_run_turns_cloud",
        "cache_reset_min_run_turns_local",
        "captains_log_reflection_cadence_enabled",
        "captains_log_reflection_enabled",
        "consolidator_max_extraction_attempts",
        "constraint_pause_timeout_seconds",
        "context_budget_max_tokens",
        "context_quality_governance_budget_reduction",
        "context_window_max_tokens",
        "conversation_context_strategy",
        "conversation_max_history_messages",
        "debug",
        "decided_skip_days",
        "dedup_similarity_threshold",
        "delegation_enabled",
        "delivery_ratio_probe_index_prefix",
        "delivery_ratio_probe_min_ratio",
        "disk_usage_alert_percent",
        "document_max_extracted_text_chars",
        "document_max_pages_per_turn",
        "document_max_total_payload_bytes",
        "document_page_max_bytes",
        "document_page_max_pixels",
        "document_text_density_floor_per_page",
        "embedding_backfill_enabled",
        "entity_extraction_fewshot_exemplars_enabled",
        "entity_extraction_timeout_seconds",
        "entity_match_relevance_bound",
        "entity_match_relevance_gate_enabled",
        "eur_usd_rate",
        "event_bus_dead_letter_stream",
        "exa_search_cost_per_call_usd",
        "expansion_budget_max",
        "expansion_enabled",
        "failure_path_reflection_enabled",
        "feedback_max_reevaluations",
        "feedback_suppression_days",
        "freshness_cold_threshold_days",
        "freshness_dormant_entity_proposal_threshold",
        "freshness_dormant_relationship_proposal_threshold",
        "freshness_frequency_boost_alpha",
        "freshness_frequency_boost_max",
        "freshness_half_life_days",
        "freshness_never_accessed_noise_days",
        "freshness_relevance_weight",
        "freshness_review_schedule_cron",
        "freshness_tier_factors",
        "freshness_tier_reranking_enabled",
        "grounding_compliance_bar",
        "grounding_compliance_max_window_age_hours",
        "grounding_compliance_min_samples",
        "grounding_compliance_window_size",
        "grounding_enforcement_cooldown_hours",
        "grounding_enforcement_demote_below",
        "grounding_enforcement_probation_rate",
        "grounding_entailment_latency_budget_ms",
        "grounding_entailment_max_excerpt_chars",
        "grounding_entailment_max_inline_checks",
        "grounding_entailment_max_partial_miss_checks",
        "grounding_entailment_sample_rate",
        "grounding_max_generation_attempts",
        "grounding_verification_mode",
        "insights_wiring_enabled",
        "joinability_probe_index_prefix",
        "joinability_probe_window_hours",
        "kg_stats_projection_schedule_cron",
        "lexical_arm_enabled",
        "linear_agent_rate_limit_per_day",
        "linear_personal_agent_label_id",
        "linear_promotion_project",
        "linear_team_name",
        "llm_append_no_think_to_tool_prompts",
        "llm_max_retries",
        "llm_no_think_suffix",
        "llm_timeout_seconds",
        "local_fallback_embedding_endpoint",
        "local_fallback_embedding_model",
        "location_precision",
        "managed_embedding_endpoint",
        "managed_embedding_model",
        "metrics_sampled_stream_maxlen",
        "multipath_arm_top_k",
        "multipath_paraphrase_count",
        "multipath_recall_enabled",
        "multipath_rrf_k",
        "multiquery_arm_enabled",
        "orchestrator_creditable_pause_limit",
        "orchestrator_max_tool_iterations",
        "orchestrator_spend_threshold",
        "orchestrator_task_timeout_seconds",
        "orchestrator_turn_lifetime_seconds",
        "outcome_ingestion_enabled",
        "perplexity_base_url",
        "perplexity_timeout_seconds",
        "planner_history_max_chars",
        "planner_input_max_chars",
        "planner_timeout_seconds",
        "proactive_memory_diminishing_score_floor",
        "proactive_memory_diminishing_score_gap",
        "proactive_memory_enabled",
        "proactive_memory_max_candidates",
        "proactive_memory_max_injected_items",
        "proactive_memory_max_tokens",
        "proactive_memory_min_score",
        "proactive_memory_recency_half_life_days",
        "proactive_memory_relevance_bound",
        "proactive_memory_relevance_gate_enabled",
        "proactive_memory_rerank_relevance_bound",
        "proactive_memory_vector_top_k",
        "proactive_memory_w_embedding",
        "proactive_memory_w_entity",
        "proactive_memory_w_recency",
        "proactive_memory_w_topic",
        "project_name",
        "promotion_initial_cap",
        "promotion_min_seen_count",
        "prompt_debug_capture_enabled",
        "pwa_public_origin",
        "recall_candidate_cap",
        "recall_per_entity_turn_cap",
        "recall_similarity_floor",
        "relevance_bounded_recall_enabled",
        "request_monitoring_enabled",
        "reranker_enabled",
        "reranker_input_cap",
        "reranker_top_k",
        "route_trace_preview_chars",
        "route_trace_store_preview",
        "sandbox_image",
        "sandbox_network",
        "sandbox_scratch_root",
        "sandbox_scratch_volume",
        "searxng_base_url",
        "searxng_default_categories",
        "searxng_max_results",
        "searxng_timeout_seconds",
        "service_request_close_timeout_seconds",
        "service_request_task_lifetime_seconds",
        "seshat_open_ticket_cap",
        "session_digest_max_tokens",
        "session_digest_structured_output",
        "session_digest_target_tokens",
        "session_retention_days",
        "session_summary_enabled",
        "session_summary_idle_threshold_seconds",
        "session_summary_max_attempts",
        "session_summary_retry_backoff_base_seconds",
        "session_summary_retry_backoff_max_seconds",
        "session_summary_sweep_interval_seconds",
        "session_write_wait_timeout_seconds",
        "signal_priority_clamp",
        "signal_smoothing_prior",
        "signal_suppression_cooldown_days",
        "signal_suppression_min_n",
        "signal_suppression_threshold",
        "signal_window_days",
        "slm_gpu_util_degraded_pct",
        "slm_health_cache_ttl_seconds",
        "slm_health_generation_check_enabled",
        "slm_health_index_prefix",
        "slm_health_url",
        "slm_queue_depth_degraded",
        "slm_thinking_canary_enabled",
        "structural_arm_enabled",
        "structural_arm_top_k",
        "structural_class_predicate_enabled",
        "structural_type_predicate_enabled",
        "sub_agent_context_reserve_tokens",
        "sub_agent_landing_max_tokens",
        "sub_agent_max_tool_iterations",
        "sub_agent_researcher_min_search_rounds",
        "sub_agent_rounds_by_thoroughness",
        "sysgraph_maintenance_enabled",
        "tempo_url",
        "tool_result_digest_exclude_tools",
        "tool_result_digest_head_lines",
        "tool_result_digest_max_expand_tokens",
        "tool_result_digest_min_savings_tokens",
        "tool_result_digest_pin_ttl_turns",
        "tool_result_digest_put_timeout_ms",
        "tool_result_digest_tail_lines",
        "tool_result_digest_threshold_tokens",
        "turn_observed_stream_maxlen",
        "upload_max_size_bytes",
        "version",
        "web_search_monthly_budget_usd",
        "within_session_compression_enabled",
        "within_session_hard_threshold_ratio",
        "within_session_max_tail_ratio",
        "within_session_min_tail_ratio",
        "within_session_pre_pass_threshold_tokens",
        "worker_queue_absorption_seconds",
        "ws_event_ttl_hours",
        "ws_max_message_size",
        "ws_ping_timeout_seconds",
        "ws_ticket_ttl_seconds",
    }
)

#: ``AppConfig`` fields with a read that runs once per process, after the overrides load.
NEXT_RESTART_SETTINGS: frozenset[str] = frozenset(
    {
        "artifacts_public_base_url",
        "cache_erosion_probe_enabled",
        "cache_erosion_probe_interval_seconds",
        "captains_log_reflection_min_interval_seconds",
        "context_quality_governance_enabled",
        "context_quality_governance_threshold",
        "context_quality_stream_enabled",
        "data_lifecycle_enabled",
        "delivery_ratio_probe_enabled",
        "delivery_ratio_probe_interval_seconds",
        "dev_agent_github_api_url",
        "dev_agent_github_app_id",
        "dev_agent_github_installation_id",
        "dev_agent_github_org",
        "dev_agent_label_scope",
        "dev_agent_network",
        "dev_agent_supervisor_enabled",
        "dev_agent_toolkit_volume",
        "dev_broker_control_url",
        "elasticsearch_index_prefix",
        "elasticsearch_url",
        "embedding_dimensions",
        "enable_memory_graph",
        "enable_second_brain",
        "error_monitor_enabled",
        "error_monitor_max_patterns_per_scan",
        "error_monitor_min_occurrences",
        "error_monitor_window_hours",
        "event_bus_ack_timeout_seconds",
        "event_bus_consumer_poll_interval_ms",
        "event_bus_enabled",
        "event_bus_max_retries",
        "event_bus_redis_url",
        "feedback_polling_enabled",
        "feedback_polling_hour_utc",
        "freshness_consumer_batch_max_events",
        "freshness_consumer_batch_window_seconds",
        "freshness_enabled",
        "graph_quality_governance_enabled",
        "graph_quality_stream_enabled",
        "insights_enabled",
        "joinability_probe_enabled",
        "joinability_probe_interval_seconds",
        "location_enabled",
        "mcp_gateway_enabled",
        "mcp_gateway_enabled_servers",
        "mcp_gateway_timeout_seconds",
        "metrics_daemon_buffer_size",
        "metrics_daemon_es_emit_interval_seconds",
        "metrics_daemon_poll_interval_seconds",
        "mode_calibration_anomaly_threshold",
        "mode_controller_enabled",
        "mode_evaluation_interval_seconds",
        "mode_window_size",
        "neo4j_uri",
        "outcome_ingestion_hour_utc",
        "owner_name",
        "primitive_tools_enabled",
        "promotion_pipeline_enabled",
        "quality_monitor_anomaly_window_days",
        "quality_monitor_daily_run_hour_utc",
        "quality_monitor_enabled",
        "r2_bucket_name",
        "r2_endpoint_url",
        "r2_region",
        "second_brain_cpu_threshold",
        "second_brain_idle_time_seconds",
        "second_brain_memory_threshold",
        "second_brain_min_interval_seconds",
        "second_brain_resource_gating_enabled",
        "session_retention_sweep_interval_seconds",
        "slm_base_url",
        "slm_health_probe_down_interval_seconds",
        "slm_health_probe_enabled",
        "slm_health_probe_interval_seconds",
        "substrate_profile",
        "sysgraph_maintenance_hour_utc",
        "tool_result_compression_enabled",
        "turn_projector_enabled",
        "url_guard_allowlist",
        "url_guard_cache_ttl_seconds",
        "url_guard_mode",
        "url_guard_novelty_window_days",
        "user_display_names_json",
        "web_search_brief_brave_max_tokens",
        "web_search_brief_exa_max_characters",
        "web_search_brief_exa_num_results",
        "web_search_full_brave_max_tokens",
        "web_search_full_exa_max_characters",
        "web_search_full_exa_num_results",
        "web_search_timeout_seconds",
        "ws_event_queue_size",
        "ws_rate_limit_per_second",
    }
)

#: ``AppConfig`` fields that no edit can reach, with the reason that the menu shows.
READ_ONLY_SETTINGS: Mapping[str, str] = {
    "abuse_ch_auth_key": _SECRET,
    "anthropic_api_key": _SECRET,
    "artifact_resolve_internal_token": _SECRET,
    "brave_api_key": _SECRET,
    "database_admin_url": _SECRET,
    "dev_agent_github_app_private_key_b64": _SECRET,
    "dev_broker_control_secret": _SECRET,
    "exa_api_key": _SECRET,
    "linear_api_key": _SECRET,
    "local_fallback_embedding_token": _SECRET,
    "managed_database_url": _SECRET,
    "managed_embedding_token": _SECRET,
    "neo4j_password": _SECRET,
    "neo4j_user": _SECRET,
    "openai_api_key": _SECRET,
    "owner_ntfy_url": _SECRET,
    "perplexity_api_key": _SECRET,
    "r2_access_key_id": _SECRET,
    "r2_secret_access_key": _SECRET,
    "scaleway_secret_key": _SECRET,
    "sysgraph_database_url": _SECRET,
    "voyage_api_key": _SECRET,
    "database_url": _OVERRIDE_STORE,
    "captains_log_index_prefix": _IMPORT,
    "cors_allowed_origins": _IMPORT,
    "database_echo": _IMPORT,
    "gateway_mount_local": _IMPORT,
    "log_dir": _IMPORT,
    "agent_id": _EARLY_START,
    "otel_exporter_endpoint": _EARLY_START,
    "allow_test_writes_to_prod_substrate": _PROFILE,
    "deployment_profile": _PROFILE,
    "environment": _PROFILE,
    "experimental_openai_base_llm_http_handler": _SETTINGS_LOAD,
    "log_format": _LOGGING,
    "log_level": _LOGGING,
    "managed_elasticsearch_url": _SETTINGS_LOAD,
    "managed_neo4j_uri": _SETTINGS_LOAD,
    "managed_reranker_endpoint": _SETTINGS_LOAD,
    "managed_slm_endpoint": _SETTINGS_LOAD,
    "owner_storage_allowlist": _SETTINGS_LOAD,
    "agent_owner_email": _LOCKOUT,
    "cf_access_aud": _LOCKOUT,
    "cf_access_main_aud": _LOCKOUT,
    "cf_access_team_domain": _LOCKOUT,
    "extra_permission_rules_path": _LOCKOUT,
    "gateway_access_config": _LOCKOUT,
    "gateway_auth_enabled": _LOCKOUT,
    "governance_config_path": _LOCKOUT,
    "mcp_gateway_command": _COMMAND,
    "build_fingerprint": _BUILD,
    "service_port": _COMPOSE_PORT,
    "freshness_backfill_confirm": _CLI_ONLY,
    "service_url": _CLI_ONLY,
}


@dataclass(frozen=True)
class RowClass:
    """When an edit of one menu row applies.

    Attributes:
        applies: The class of the row.
        reason: Why the row is read-only, or ``None`` for an editable row.
    """

    applies: ApplyClass
    reason: str | None = None


_NEXT_TURN = RowClass("next_turn")
_ONE_OPTION = "the catalog has one option for this role, so there is nothing to choose"
_NEVER_APPLIED = "the turn never applies a saved choice here, because the card always asks"
_UNCAPPED = "only the role check at gateway start reads this list. It does not remove a cap"
_GROUNDING = "the turn selector writes this state on each turn, so an edit does not last"
_LOCATION_OFF = "the setting location_enabled is off"

#: Row name prefixes that are not ``AppConfig`` fields.
_NEXT_TURN_PREFIXES: tuple[str, ...] = (
    "role.",
    "effort.",
    "cap.",
    "saved_choice.",
    "tool.",
    "deployment.",
    "role_estimate.",
)
_NEXT_TURN_ROWS: frozenset[str] = frozenset(
    {
        "session.primary_model",
        "session.effort_level",
        "location.consent",
        "sub_agent_tools",
        "mode_transition_rules",
    }
)
ONE_OPTION_REASON = _ONE_OPTION
NEVER_APPLIED_REASON = _NEVER_APPLIED
LOCATION_OFF_REASON = _LOCATION_OFF
NO_CONVERSATION_REASON = "no conversation is open"
PINNED_REASON = "the catalog pins this role, so a conversation cannot choose its model"


def setting_class(field: str) -> RowClass:
    """Return the class of one ``AppConfig`` field.

    Args:
        field: An ``AppConfig`` field name.

    Returns:
        The class of the field.

    Raises:
        KeyError: When the table does not name the field.
    """
    if field in NEXT_TURN_SETTINGS:
        return _NEXT_TURN
    if field in NEXT_RESTART_SETTINGS:
        return RowClass("next_restart")
    return RowClass("read_only", READ_ONLY_SETTINGS[field])


def row_class(name: str) -> RowClass:
    """Return the class of one settings menu row.

    Args:
        name: The row name.

    Returns:
        The class of the row. A row with a dynamic exception, such as a role with one option,
        takes that exception in the menu builder.

    Raises:
        KeyError: When the table does not name the row.
    """
    if name.startswith("setting."):
        return setting_class(name.removeprefix("setting."))
    if name == "uncapped_roles":
        return RowClass("read_only", _UNCAPPED)
    if name.startswith("grounding_enforcement."):
        return RowClass("read_only", _GROUNDING)
    if name in _NEXT_TURN_ROWS or name.startswith(_NEXT_TURN_PREFIXES):
        return _NEXT_TURN
    raise KeyError(name)
