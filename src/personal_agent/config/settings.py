"""Application configuration settings.

This module provides the AppConfig class and settings singleton.
"""

from pathlib import Path

import structlog
from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from personal_agent.config.env_loader import Environment, get_environment, load_env_files
from personal_agent.config.validators import (
    resolve_path,
    validate_log_format,
    validate_log_level,
)

log = structlog.get_logger(__name__)


class AppConfig(BaseSettings):
    """Unified application configuration.

    Loads configuration from environment variables, YAML files, and defaults.
    Validates all values using Pydantic.
    """

    model_config = SettingsConfigDict(
        # Don't use env_file here - we load .env files manually via env_loader
        # to support environment-specific files with priority order
        # But we still want to read from os.environ
        env_prefix="AGENT_",  # All env vars use AGENT_ prefix
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
        populate_by_name=True,  # Allow both field name and alias
        protected_namespaces=(),  # Allow model_* field names (we have model_config_path)
    )

    # Environment
    environment: Environment = Field(
        default_factory=get_environment, description="Current environment"
    )
    debug: bool = Field(default=False, alias="APP_DEBUG", description="Debug mode flag")

    # Application
    project_name: str = Field(default="Personal Local AI Collaborator", description="Project name")
    version: str = Field(default="0.1.0", description="Application version")
    cors_allowed_origins: list[str] = Field(
        default=["http://localhost:3000"],
        description=(
            "CORS allowed origins for the FastAPI service. "
            "In production Caddy proxies PWA and backend through the same origin so this is unused. "
            "Locally the Next.js dev server runs on :3000 while the backend is on :9000."
        ),
    )

    # Telemetry
    log_dir: Path = Field(default=Path("telemetry/logs"), description="Log directory path")
    log_level: str = Field(
        default="INFO",
        alias="APP_LOG_LEVEL",
        description="Logging level (DEBUG, INFO, WARNING, ERROR, CRITICAL)",
    )
    log_format: str = Field(
        default="json", alias="APP_LOG_FORMAT", description="Log format (json or console)"
    )

    @field_validator("log_level")
    @classmethod
    def validate_log_level(cls, v: str) -> str:
        """Validate log level."""
        return validate_log_level(v)

    @field_validator("log_format")
    @classmethod
    def validate_log_format(cls, v: str) -> str:
        """Validate log format."""
        return validate_log_format(v)

    @field_validator("log_dir", "governance_config_path", "model_config_path", mode="before")
    @classmethod
    def resolve_paths(cls, v: Path | str) -> Path:
        """Resolve relative paths to absolute."""
        return resolve_path(v)

    # LLM Client
    llm_base_url: str = Field(
        default="http://127.0.0.1:1234/v1", description="Base URL for LLM API (LM Studio default)"
    )
    llm_timeout_seconds: int = Field(default=120, ge=1, description="Request timeout")
    llm_max_retries: int = Field(default=3, ge=0, description="Maximum retry attempts")
    llm_no_think_suffix: str = Field(
        default="/no_think",
        description=(
            "Optional suffix token appended to prompts to discourage 'thinking' / long reasoning. "
            "Works for some models (e.g., Qwen3) when placed at the end of the prompt."
        ),
    )
    llm_append_no_think_to_tool_prompts: bool = Field(
        default=True,
        description=(
            "If true, append llm_no_think_suffix to tool-request prompts and post-tool synthesis prompts "
            "to reduce latency and reasoning verbosity."
        ),
    )

    # Orchestrator
    orchestrator_max_concurrent_tasks: int = Field(
        default=5, ge=0, description="Maximum concurrent tasks"
    )
    orchestrator_task_timeout_seconds: int = Field(default=300, ge=1, description="Task timeout")
    orchestrator_max_tool_iterations: int = Field(
        default=25,
        ge=0,
        description=(
            "Maximum tool execution iterations per user request (prevents tool loops). "
            "Raised to 25: compound telemetry/analysis tasks can need 15+ sequential calls. "
            "At max-3, a budget warning is injected. At max+1, a forced LLM synthesis pass "
            "runs (no tools) so gathered results are never silently discarded."
        ),
    )
    orchestrator_max_repeated_tool_calls: int = Field(
        default=1,
        ge=0,
        description="Maximum times the same tool call signature can repeat per request",
    )
    orchestrator_max_tool_iterations_by_task_type: dict[str, int] = Field(
        default_factory=lambda: {
            "conversational": 6,
            "memory_recall": 8,
            "analysis": 25,
            "planning": 25,
            "tool_use": 25,
            "delegation": 25,
            "self_improve": 25,
        },
        description=(
            "Per-TaskType cap on tool iterations. Intersected with "
            "orchestrator_max_tool_iterations (whichever is lower wins). "
            "TaskTypes not listed fall back to orchestrator_max_tool_iterations."
        ),
    )

    # Routing (router speed and single-model mode)
    routing_policy: str = Field(
        default="heuristic_then_llm",
        description="Routing policy: heuristic_then_llm (gate then LLM if below threshold), heuristic_only, llm_only",
    )
    router_role: str = Field(
        default="ROUTER",
        description="Role used for routing: ROUTER (dedicated small model) or STANDARD (single-model mode, e.g. qwen as router+standard)",
    )
    enable_reasoning_role: bool = Field(
        default=True,
        description="If False, map REASONING -> STANDARD (single-model mode).",
    )
    routing_heuristic_threshold: float = Field(
        default=0.85,
        ge=0.0,
        le=1.0,
        description="Only call LLM router when heuristic confidence is below this (heuristic_then_llm).",
    )
    router_timeout_seconds: float = Field(
        default=6.0,
        gt=0,
        le=60,
        description="Timeout for router LLM call; on timeout fall back to heuristic.",
    )

    # Brainstem
    brainstem_sensor_poll_interval_seconds: float = Field(
        default=5.0, gt=0, description="Sensor polling interval"
    )

    # Request Monitoring (ADR-0012)
    request_monitoring_enabled: bool = Field(
        default=True,
        description="Enable automatic request-scoped metrics monitoring for homeostasis control loops",
    )
    request_monitoring_interval_seconds: float = Field(
        default=5.0,
        gt=0,
        description="Request monitoring polling interval (default: 5.0s per CONTROL_LOOPS_SENSORS spec)",
    )
    request_monitoring_include_gpu: bool = Field(
        default=True,
        description="Include GPU metrics in request monitoring (Apple Silicon: powermetrics)",
    )
    metrics_daemon_poll_interval_seconds: float = Field(
        default=5.0,
        gt=0,
        description="Continuous metrics daemon polling interval in seconds",
    )
    metrics_daemon_es_emit_interval_seconds: float = Field(
        default=30.0,
        gt=0,
        description="How often the metrics daemon emits SENSOR_POLL telemetry to ES",
    )
    metrics_daemon_buffer_size: int = Field(
        default=720,
        ge=1,
        le=10000,
        description="Metrics daemon ring buffer size",
    )
    mode_controller_enabled: bool = Field(
        default=True,
        description="Enable ADR-0055 mode controller (cg:mode-controller consumer + "
        "dual-write of metrics.sampled and mode.transition events). "
        "Defaults True — the full ADR-0055 pipeline is active in production.",
    )

    # ADR-0056 — Error Pattern Monitoring (Wave 2)
    error_monitor_enabled: bool = Field(
        default=True,
        description="Enable ADR-0056 error pattern monitor (cg:error-monitor subscribes "
        "to stream:consolidation.completed, scans ES, dual-writes EP-*.json files and "
        "stream:errors.pattern_detected events). Flip False if ES load spikes.",
    )
    error_monitor_window_hours: int = Field(
        default=24,
        ge=1,
        description="Trailing window (hours) for error-pattern ES aggregation.",
    )
    error_monitor_min_occurrences: int = Field(
        default=5,
        ge=1,
        description="Minimum event count for a (component, event, error_type) cluster "
        "to be emitted as an ErrorPatternDetectedEvent.",
    )
    error_monitor_max_patterns_per_scan: int = Field(
        default=50,
        ge=1,
        description="Hard cap on ErrorPatternDetectedEvent emissions per scan run.",
    )
    failure_path_reflection_enabled: bool = Field(
        default=False,
        description="Enable ADR-0056 Phase 2 GEPA-inspired failure-path reflection. "
        "Extends GenerateReflection with failure_excerpt inputs and "
        "failure_path_fix_what/location outputs. Flip True after 1 week of Phase 1 "
        "observation validates signal quality.",
    )
    metrics_sampled_stream_maxlen: int = Field(
        default=720,
        ge=60,
        description="MAXLEN (approximate) on stream:metrics.sampled — matches MetricsDaemon "
        "ring-buffer depth (~1 h at 5 s).",
    )
    mode_evaluation_interval_seconds: float = Field(
        default=30.0,
        gt=0,
        description="How often cg:mode-controller aggregates its window and calls "
        "ModeManager.evaluate_transitions. Unit: seconds.",
    )
    mode_window_size: int = Field(
        default=12,
        ge=1,
        le=720,
        description="Number of recent MetricsSampledEvent samples retained by "
        "cg:mode-controller for aggregation (12 × 5 s = 60 s window).",
    )
    mode_calibration_anomaly_threshold: int = Field(
        default=3,
        ge=1,
        description="Number of (from_mode, to_mode) edge transitions within 10 min "
        "that triggers a Captain's Log calibration proposal.",
    )

    # MCP Gateway
    mcp_gateway_enabled: bool = Field(
        default=False, description="Enable Docker MCP Gateway integration"
    )
    mcp_gateway_command: list[str] = Field(
        default_factory=lambda: ["docker", "mcp", "gateway", "run"],
        description="Command to run Docker MCP Gateway",
    )
    mcp_gateway_timeout_seconds: int = Field(
        default=60, ge=1, le=300, description="Timeout for MCP operations (seconds)"
    )
    mcp_gateway_enabled_servers: list[str] = Field(
        default_factory=list,
        description=(
            "MCP server ids to expose (empty = all). Matching uses tool name substrings, "
            "Linear tool metadata (linear.app), and built-in aliases for tools whose names "
            "omit the server id (see mcp_server_allowlist)."
        ),
    )

    # SearXNG web search (ADR-0034)
    searxng_base_url: str = Field(
        default="http://localhost:8888",
        description="SearXNG instance base URL",
    )
    searxng_timeout_seconds: int = Field(
        default=12,
        ge=1,
        description="Timeout for SearXNG search requests",
    )
    searxng_default_categories: str = Field(
        default="general",
        description="Default SearXNG categories (comma-separated)",
    )
    searxng_max_results: int = Field(
        default=10,
        ge=1,
        le=50,
        description="Maximum results to return per search",
    )

    @field_validator("mcp_gateway_command", mode="before")
    @classmethod
    def parse_gateway_command(cls, v: str | list[str]) -> list[str]:
        """Parse gateway command from string or list.

        Handles:
        - JSON array: '["docker", "mcp", "gateway", "run"]'
        - Space-separated: "docker mcp gateway run"
        - Already a list: ["docker", "mcp", "gateway", "run"]
        """
        if isinstance(v, list):
            return v

        if isinstance(v, str):
            # Try JSON parsing first
            try:
                import json

                parsed = json.loads(v)
                if isinstance(parsed, list):
                    return parsed
            except json.JSONDecodeError:
                pass

            # Fallback: split by whitespace
            return v.split()

        raise ValueError(f"Invalid gateway command type: {type(v)}")

    # Request Gateway & Expansion Budget (Phase 2.4)
    context_budget_comfortable_tokens: int = Field(
        default=32000,
        ge=1000,
        description="Comfortable context budget threshold for token management",
    )
    context_budget_max_tokens: int = Field(
        default=65536,
        ge=2000,
        description="Maximum context budget limit",
    )
    context_budget_generation_reserve_tokens: int = Field(
        default=4096,
        ge=500,
        description="Reserve tokens for LLM generation in context budget calculations",
    )
    expansion_budget_max: int = Field(
        default=3,
        ge=1,
        le=10,
        description="Maximum request expansion budget (max decomposition depth)",
    )
    sub_agent_timeout_seconds: float = Field(
        default=120.0,
        gt=0,
        le=3600,
        description="Timeout for sub-agent task execution",
    )
    sub_agent_max_tokens: int = Field(
        default=4096,
        ge=500,
        description="Maximum tokens per sub-agent response",
    )

    # --- Expansion controller (ADR-0036) ---
    orchestration_mode: str = Field(
        default="enforced",
        description="Expansion enforcement mode: 'enforced' (gateway binding) or 'autonomous' (LLM decides)",
    )
    planner_timeout_seconds: float = Field(
        default=30.0,
        description="Max time for LLM planner phase in expansion controller",
    )
    worker_timeout_seconds: float = Field(
        default=60.0,
        description="Max time per sub-agent worker in expansion dispatch",
    )
    worker_global_timeout_seconds: float = Field(
        default=180.0,
        description="Max total time for all sub-agent workers combined (serial GPU)",
    )
    synthesis_timeout_seconds: float = Field(
        default=25.0,
        description="Max time for synthesis phase in expansion controller",
    )

    # --- Embedding & Reranker configuration (ADR-0035) ---
    # Model identity (id, endpoint) lives in config/models.yaml — ADR-0031.
    # Only runtime knobs belong here.
    embedding_dimensions: int = Field(
        default=1024,
        description="Embedding vector dimensions (1024 native for Qwen3-Embedding-0.6B)",
    )
    embedding_batch_size: int = Field(
        default=20,
        description="Max items per embedding API call",
    )
    dedup_similarity_threshold: float = Field(
        default=0.85,
        description="Cosine similarity threshold for entity deduplication",
    )
    reranker_enabled: bool = Field(
        default=True,
        description="Enable cross-attention reranker in memory query pipeline",
    )
    reranker_top_k: int = Field(
        default=10,
        description="Number of top candidates to re-score with reranker",
    )

    # Paths (for domain config loaders)
    governance_config_path: Path = Field(
        default=Path("config/governance"), description="Path to governance config directory"
    )
    model_config_path: Path = Field(
        default=Path("config/models.yaml"), description="Path to model config file"
    )

    # Service Configuration
    service_host: str = Field(default="0.0.0.0", description="Service host address")
    service_port: int = Field(
        default=9000,
        description="Service port number (9000 to avoid conflict with LLM server on 8000)",
    )
    service_url: str = Field(
        default="http://localhost:9000",
        alias="SERVICE_URL",
        description="Base URL for service-facing clients (CLI service client).",
    )

    # Conversation continuity (Phase 2.6)
    conversation_max_history_messages: int = Field(
        default=10,
        ge=1,
        description="Maximum number of historical session messages to hydrate into orchestrator memory.",
    )
    context_window_max_tokens: int = Field(
        default=2048,
        ge=500,
        description="Maximum context token budget for conversation messages before each LLM call.",
    )
    conversation_context_strategy: str = Field(
        default="truncate",
        description="Context window strategy. Supported: 'truncate'.",
    )

    # Context Compression (ADR-0038)
    context_compression_enabled: bool = Field(
        default=True,
        description="Enable async context compression of evicted turns.",
    )
    context_compression_threshold_ratio: float = Field(
        default=0.65,
        gt=0.0,
        le=1.0,
        description="Fire compression when estimated tokens exceed this fraction of context_window_max_tokens.",
    )

    # Database (Postgres)
    database_url: str = Field(
        default="postgresql+asyncpg://agent:agent_dev_password@localhost:5432/personal_agent",
        description="PostgreSQL database URL",
    )
    database_echo: bool = Field(default=False, description="Echo SQL queries (for debugging)")

    # Elasticsearch
    elasticsearch_url: str = Field(default="http://localhost:9200", description="Elasticsearch URL")
    elasticsearch_index_prefix: str = Field(
        default="agent-logs", description="Elasticsearch index prefix"
    )

    # Neo4j
    neo4j_uri: str = Field(default="bolt://localhost:7687", description="Neo4j connection URI")
    neo4j_user: str = Field(default="neo4j", description="Neo4j username")
    neo4j_password: str = Field(default="neo4j_dev_password", description="Neo4j password")

    # Cloud API secrets (model identity lives in config/models.yaml — ADR-0031)
    anthropic_api_key: str | None = Field(default=None, description="Anthropic API key for Claude")
    openai_api_key: str | None = Field(default=None, description="OpenAI API key")

    # Linear (native tool — FRE-224)
    linear_api_key: str | None = Field(
        default=None,
        description="Linear Personal Access Token for the native create_linear_issue tool (FRE-224)",
    )
    linear_agent_rate_limit_per_day: int = Field(
        default=10,
        ge=1,
        description="Max agent-filed Linear issues per 24h (across all projects combined)",
    )

    # Perplexity AI (native tool — ADR-0028 Phase 2)
    perplexity_api_key: str | None = Field(default=None, description="Perplexity API key")
    perplexity_base_url: str = Field(
        default="https://api.perplexity.ai",
        description="Perplexity API base URL",
    )
    perplexity_timeout_seconds: int = Field(
        default=90,
        ge=10,
        le=300,
        description="Timeout for Perplexity API requests (research mode can be slow)",
    )
    cloud_weekly_budget_usd: float = Field(
        default=5.0,
        description=(
            "Weekly spending cap across all cloud LLM providers (USD). "
            "Shared budget; per-provider breakdown available via CostTrackerService."
        ),
    )

    # Event Bus (ADR-0041 — Redis Streams)
    event_bus_enabled: bool = Field(
        default=False,
        description="Enable Redis Streams event bus. When False, a no-op bus is used and polling continues.",
    )
    event_bus_redis_url: str = Field(
        default="redis://localhost:6379/0",
        description="Redis connection URL for the event bus",
    )
    event_bus_consumer_poll_interval_ms: int = Field(
        default=100,
        ge=10,
        le=10000,
        description="Consumer XREADGROUP block timeout in milliseconds",
    )
    event_bus_max_retries: int = Field(
        default=3,
        ge=1,
        le=20,
        description="Maximum delivery attempts before routing to dead-letter stream",
    )
    event_bus_dead_letter_stream: str = Field(
        default="stream:dead_letter",
        description="Stream name for dead-letter events",
    )
    event_bus_ack_timeout_seconds: int = Field(
        default=300,
        ge=10,
        le=3600,
        description="Seconds before an unacknowledged message is considered stuck",
    )

    # Feature flags
    use_service_mode: bool = Field(default=True, description="Enable service mode")
    enable_second_brain: bool = Field(default=False, description="Enable second brain (Phase 2.2)")
    enable_memory_graph: bool = Field(default=False, description="Enable memory graph (Phase 2.2)")

    # Second Brain Scheduling (Phase 2.2)
    second_brain_resource_gating_enabled: bool = Field(
        default=True,
        description=(
            "Enable idle-time and CPU/memory gating before consolidation. "
            "Set False when entity extraction uses a remote model and local "
            "resource pressure is irrelevant."
        ),
    )
    second_brain_idle_time_seconds: float = Field(
        default=300.0, description="Idle time before consolidation (5 minutes)"
    )
    second_brain_cpu_threshold: float = Field(
        default=50.0, description="Maximum CPU usage for consolidation (50%)"
    )
    second_brain_memory_threshold: float = Field(
        default=70.0, description="Maximum memory usage for consolidation (70%)"
    )
    second_brain_check_interval_seconds: float = Field(
        default=60.0, description="How often to check consolidation conditions (1 minute)"
    )
    second_brain_min_interval_seconds: float = Field(
        default=3600.0, description="Minimum time between consolidations (1 hour)"
    )
    entity_extraction_timeout_seconds: int = Field(
        default=90,
        ge=10,
        le=600,
        description="Timeout for entity-extraction LLM call; on timeout return empty entities.",
    )

    # Data Lifecycle (Phase 2.3)
    disk_usage_alert_percent: float = Field(
        default=80.0,
        ge=0,
        le=100,
        description="Alert when disk usage exceeds this percent",
    )
    data_lifecycle_enabled: bool = Field(
        default=True,
        description="Enable automated retention, archive, and purge",
    )

    # Consolidation Quality Monitor (Phase 2.3, FRE-32)
    quality_monitor_enabled: bool = Field(
        default=True,
        description="Enable scheduled consolidation quality monitoring",
    )
    quality_monitor_daily_run_hour_utc: int = Field(
        default=5,
        ge=0,
        le=23,
        description="UTC hour for daily quality monitor pass",
    )
    quality_monitor_anomaly_window_days: int = Field(
        default=7,
        ge=1,
        le=30,
        description="Window in days used for quality anomaly detection",
    )

    # Insights Engine (Phase 2.3, FRE-24)
    insights_enabled: bool = Field(
        default=True,
        description="Enable proactive insights analysis and proposal generation",
    )
    insights_daily_run_hour_utc: int = Field(
        default=6,
        ge=0,
        le=23,
        description="UTC hour for daily insights analysis",
    )
    insights_weekly_day: int = Field(
        default=6,
        ge=0,
        le=6,
        description="Weekday for weekly proposals (0=Monday, 6=Sunday)",
    )
    insights_weekly_run_hour_utc: int = Field(
        default=9,
        ge=0,
        le=23,
        description="UTC hour for weekly Captain's Log insight proposals",
    )

    # ADR-0057 — dual-write wiring enable flag (independent of insights_enabled so the
    # engine can produce insights without CL/bus wiring if needed during rollout).
    insights_wiring_enabled: bool = Field(
        default=True,
        description=(
            "Enable ADR-0057 wiring: publish InsightsPatternDetectedEvent + "
            "InsightsCostAnomalyEvent on the bus and emit CaptainLogEntry "
            "proposals via CaptainLogManager on every consolidation. Flip "
            "False if a CL flood occurs post-rollout; the engine still "
            "analyses and indexes to agent-insights-*."
        ),
    )

    # Captain's Log promotion + Linear feedback loop (ADR-0040)
    promotion_pipeline_enabled: bool = Field(
        default=True,
        description="Enable weekly Captain's Log → Linear promotion pipeline",
    )
    feedback_polling_enabled: bool = Field(
        default=True,
        description="Enable daily Linear feedback polling in brainstem scheduler",
    )
    feedback_polling_hour_utc: int = Field(
        default=7,
        ge=0,
        le=23,
        description="UTC hour for daily Linear feedback polling",
    )
    feedback_suppression_days: int = Field(
        default=30,
        ge=1,
        description="Days to suppress re-promotion of fingerprint after Linear Rejected",
    )
    feedback_max_reevaluations: int = Field(
        default=2,
        ge=1,
        description="Max Deepen/Too Vague response rounds per issue",
    )
    feedback_defer_revisit_days: int = Field(
        default=90,
        ge=7,
        description="Days before revisiting a Deferred proposal (future archive hook)",
    )
    issue_budget_threshold: int = Field(
        default=200,
        ge=50,
        le=250,
        description="Pause promotion when non-archived Linear issues exceed this count",
    )
    promotion_initial_cap: int = Field(
        default=5,
        ge=1,
        description="Max Linear issues created per promotion pipeline run",
    )
    linear_team_name: str = Field(
        default="FrenchForest",
        description="Linear team name for promotion and feedback (ADR-0040)",
    )
    linear_promotion_project: str = Field(
        default="2.3 Homeostasis & Feedback",
        description="Linear project name for promoted improvement issues",
    )

    # Knowledge Graph Freshness (ADR-0042)
    freshness_enabled: bool = Field(
        default=False,
        description="Enable knowledge graph freshness tracking (ADR-0042). "
        "When False, memory.accessed publish paths are no-ops and the consumer skips writes.",
    )
    freshness_half_life_days: float = Field(
        default=30.0,
        gt=0,
        description="Freshness decay half-life in days. "
        "An entity loses half its freshness score every this many days without access.",
    )
    freshness_cold_threshold_days: float = Field(
        default=180.0,
        gt=0,
        description="Days since last access after which an entity is considered dormant "
        "and a Captain's Log archival proposal may be generated.",
    )
    freshness_frequency_boost_alpha: float = Field(
        default=0.1,
        gt=0,
        description="Alpha coefficient in the frequency boost formula: "
        "boost = min(1 + α × ln(1 + access_count), max_boost).",
    )
    freshness_frequency_boost_max: float = Field(
        default=1.5,
        gt=1.0,
        description="Maximum multiplier applied by the frequency boost.",
    )
    freshness_consumer_batch_window_seconds: float = Field(
        default=5.0,
        gt=0,
        description="cg:freshness consumer accumulates events for this many seconds "
        "before issuing a batch Neo4j update.",
    )
    freshness_consumer_batch_max_events: int = Field(
        default=50,
        ge=1,
        description="cg:freshness consumer flushes early when this many events accumulate "
        "within the batch window.",
    )
    freshness_review_schedule_cron: str = Field(
        default="0 3 * * 0",
        description="Cron expression for the weekly staleness-review brainstem job (default: Sunday 03:00 UTC).",
    )
    freshness_dormant_entity_proposal_threshold: int = Field(
        default=10,
        ge=1,
        description="Minimum dormant entity count before emitting a Captain's Log archival proposal.",
    )
    freshness_dormant_relationship_proposal_threshold: int = Field(
        default=10,
        ge=1,
        description="Minimum dormant relationship count before emitting a Captain's Log proposal.",
    )
    freshness_never_accessed_noise_days: float = Field(
        default=30.0,
        gt=0,
        description="Entities with zero accesses and first_seen older than this are counted as "
        "never_accessed_old_entity_count (extraction-noise signal).",
    )
    freshness_backfill_confirm: bool = Field(
        default=False,
        description="Must be True (e.g. AGENT_FRESHNESS_BACKFILL_CONFIRM=true) to run "
        "destructive-adjacent freshness backfill CLI.",
    )
    freshness_relevance_weight: float = Field(
        default=0.15,
        ge=0.0,
        le=1.0,
        description="Weight allocated to the freshness signal in _calculate_relevance_scores(). "
        "Only active when freshness_enabled=True and access data exists.",
    )

    # Proactive memory (ADR-0039, FRE-174–176)
    proactive_memory_enabled: bool = Field(
        default=False,
        description="Inject scored cross-session memory for non-MEMORY_RECALL intents.",
    )
    proactive_memory_w_embedding: float = Field(
        default=0.45,
        ge=0.0,
        le=1.0,
        description="Weight for embedding similarity in proactive scoring.",
    )
    proactive_memory_w_entity: float = Field(
        default=0.25,
        ge=0.0,
        le=1.0,
        description="Weight for session–candidate entity overlap in proactive scoring.",
    )
    proactive_memory_w_recency: float = Field(
        default=0.20,
        ge=0.0,
        le=1.0,
        description="Weight for recency decay in proactive scoring.",
    )
    proactive_memory_w_topic: float = Field(
        default=0.10,
        ge=0.0,
        le=1.0,
        description="Weight for topic coherence (MVP stub) in proactive scoring.",
    )
    proactive_memory_recency_half_life_days: float = Field(
        default=30.0,
        gt=0,
        description="Half-life in days for proactive recency sub-score.",
    )
    proactive_memory_min_score: float = Field(
        default=0.3,
        ge=0.0,
        le=1.0,
        description="Discard proactive candidates below this final score.",
    )
    proactive_memory_max_tokens: int = Field(
        default=500,
        ge=1,
        description="Max estimated tokens for injected proactive memory payloads.",
    )
    proactive_memory_max_candidates: int = Field(
        default=10,
        ge=1,
        description="Max candidates after scoring before diminishing-returns trim.",
    )
    proactive_memory_max_injected_items: int = Field(
        default=5,
        ge=1,
        description="Max proactive items injected into context.",
    )
    proactive_memory_diminishing_score_floor: float = Field(
        default=0.35,
        ge=0.0,
        le=1.0,
        description="Stop when next candidate score is below this value.",
    )
    proactive_memory_diminishing_score_gap: float = Field(
        default=0.15,
        ge=0.0,
        le=1.0,
        description="Stop when score drops more than this vs previous selected item.",
    )
    proactive_memory_vector_top_k: int = Field(
        default=20,
        ge=1,
        le=100,
        description="Neo4j vector query top_k before per-session filtering.",
    )

    # Execution Profiles (ADR-0044, FRE-207)
    default_profile: str = Field(
        default="local",
        description=(
            "Default execution profile name (e.g. 'local', 'cloud'). "
            "Used when no profile is explicitly specified for a conversation. "
            "Must match a file in profiles_dir."
        ),
    )
    profiles_dir: str = Field(
        default="config/profiles",
        description="Directory containing execution profile YAML files (ADR-0044).",
    )

    # Seshat API Gateway (FRE-206)
    gateway_mount_local: bool = Field(
        default=True,
        description=(
            "Mount the Seshat API Gateway routes on the execution service "
            "(port 9000). When False the gateway must run as a separate process."
        ),
    )
    gateway_auth_enabled: bool = Field(
        default=False,
        description=(
            "Require Bearer token authentication on gateway endpoints. "
            "Disabled by default for local dev — set True for production."
        ),
    )
    gateway_access_config: str = Field(
        default="config/gateway_access.yaml",
        description="Path to the YAML file declaring gateway bearer tokens and scopes.",
    )
    cf_access_client_id: str | None = Field(
        default=None,
        alias="CF_ACCESS_CLIENT_ID",
        description=(
            "Cloudflare Zero Trust service token client ID for Mac SLM tunnel. "
            "Injected as CF-Access-Client-Id header on requests to slm.frenchforet.com."
        ),
    )
    cf_access_client_secret: str | None = Field(
        default=None,
        alias="CF_ACCESS_CLIENT_SECRET",
        description=(
            "Cloudflare Zero Trust service token secret for Mac SLM tunnel. "
            "Injected as CF-Access-Client-Secret header on requests to slm.frenchforet.com."
        ),
    )
    cf_access_team_domain: str | None = Field(
        default=None,
        alias="CF_ACCESS_TEAM_DOMAIN",
        description=(
            "Cloudflare Access team domain (e.g. 'myteam.cloudflareaccess.com'). "
            "Used to fetch the JWKS for verifying Cf-Access-Jwt-Assertion on inbound requests. "
            "When set, JWT verification is enabled on all authenticated endpoints."
        ),
    )
    cf_access_aud: str | None = Field(
        default=None,
        alias="CF_ACCESS_AUD",
        description=(
            "Cloudflare Access application audience tag (AUD). "
            "Must match the aud claim in Cf-Access-Jwt-Assertion JWTs. "
            "Required when cf_access_team_domain is set."
        ),
    )
    agent_owner_email: str | None = Field(
        default=None,
        alias="AGENT_OWNER_EMAIL",
        description=(
            "Deployment owner's email address — must match the Cloudflare Access "
            "email for the owner so that CLI paths and CF Access paths resolve to "
            "the same user_id. Used as the dev-mode fallback identity when "
            "gateway_auth_enabled=False and no CF Access header is present."
        ),
    )

    # FRE-261 PIVOT-2: Primitive tools feature flags (ADR-0063 Phase 2)
    # All flags default OFF — enable after pentest gate clears.
    # Env vars are resolved via the AGENT_ prefix (SettingsConfigDict env_prefix).
    primitive_tools_enabled: bool = Field(
        default=False,
        description=(
            "Master gate for the four FRE-261 primitive tools: read, write, bash, "
            "run_python.  When False (default) none are registered in the tool "
            "registry.  Enable with AGENT_PRIMITIVE_TOOLS_ENABLED=true after the "
            "pentest gate clears (ADR-0063 Phase 2)."
        ),
    )
    prefer_primitives_enabled: bool = Field(
        default=False,
        alias="AGENT_PREFER_PRIMITIVES",
        description=(
            "Inject skill library docs into system prompt and nudge the model to prefer "
            "primitives over MCP tools. Enable AFTER pentest gate clears AND 6+ skill docs "
            "are in docs/skills/. Requires AGENT_PRIMITIVE_TOOLS_ENABLED=true to be meaningful. "
            "Env var: AGENT_PREFER_PRIMITIVES"
        ),
    )

    # FRE-263 PIVOT-4: Flag-gated deprecation of legacy tools (ADR-0063 Phase 4)
    legacy_tools_enabled: bool = Field(
        default=False,
        alias="AGENT_LEGACY_TOOLS_ENABLED",
        description=(
            "When False (default), the 8 curated tools superseded by primitives + skill docs "
            "are not registered in the tool registry (ADR-0063 PIVOT-4). Set True only to "
            "roll back the deprecation; a 'tool_deprecated' warning is emitted at startup. "
            "Code deletion is FRE-265 after >=2 weeks of production stability. "
            "Env var: AGENT_LEGACY_TOOLS_ENABLED"
        ),
    )

    # Docker sandbox (FRE-261 — Step 5: run_python primitive)
    sandbox_image: str = Field(
        default="seshat-sandbox-python:0.1",
        description=(
            "Docker image used by the run_python primitive tool. "
            "Build with: make sandbox-build. "
            "Env var: AGENT_SANDBOX_IMAGE"
        ),
    )
    sandbox_scratch_root: str = Field(
        default="/app/agent_workspace/sandbox",
        description=(
            "Host-side root directory for per-trace sandbox scratch dirs. "
            "Each invocation gets a sub-directory keyed by trace_id. "
            "In cloud deployments set to a path on the seshat_workspace_cloud volume. "
            "Env var: AGENT_SANDBOX_SCRATCH_ROOT"
        ),
    )

    # Tool approval UI (FRE-261 — Steps 1 & 6)
    approval_ui_enabled: bool = Field(
        default=False,
        description=(
            "Enable interactive tool-approval round-trips via the PWA (FRE-261). "
            "When True, tools with requires_approval=True pause and await a human "
            "decision via POST /agui/approval/{request_id} before executing. "
            "Env var: AGENT_APPROVAL_UI_ENABLED"
        ),
    )
    approval_timeout_seconds: float = Field(
        default=60.0,
        gt=0,
        description=(
            "Seconds to wait for a tool-approval decision from the PWA before "
            "auto-denying with decision='timeout' (FRE-261). "
            "Env var: AGENT_APPROVAL_TIMEOUT_SECONDS"
        ),
    )

    # ADR-0059 — Context Quality Stream (Wave 3 — FRE-249)
    context_quality_stream_enabled: bool = Field(
        default=True,
        description=(
            "Enable ADR-0059 context quality stream. When True, "
            "request_gateway.recall_controller dual-writes detected "
            "compaction-quality incidents to telemetry/context_quality/CQ-*.jsonl "
            "and publishes CompactionQualityIncidentEvent on "
            "stream:context.compaction_quality_poor for the Captain's Log "
            "consumer. Set False to disable Stream 7 emission while keeping "
            "the structlog warning (which feeds ADR-0056 cluster monitoring). "
            "Env var: AGENT_CONTEXT_QUALITY_STREAM_ENABLED"
        ),
    )
    context_quality_governance_enabled: bool = Field(
        default=False,
        description=(
            "Enable ADR-0059 §D6 Phase 2 per-session budget tightening. When "
            "True, Stage 7 reduces max_tokens by "
            "context_quality_governance_budget_reduction whenever a session has "
            "≥ context_quality_governance_threshold incidents in the trailing "
            "24 h. Default False — flip after 14 days of Phase 1 telemetry "
            "validates signal quality. "
            "Env var: AGENT_CONTEXT_QUALITY_GOVERNANCE_ENABLED"
        ),
    )
    context_quality_governance_threshold: int = Field(
        default=2,
        ge=1,
        description=(
            "Minimum compaction-quality incident count in the trailing 24 h "
            "for a session to trigger Phase 2 budget tightening (ADR-0059 §D6). "
            "Env var: AGENT_CONTEXT_QUALITY_GOVERNANCE_THRESHOLD"
        ),
    )
    context_quality_governance_budget_reduction: float = Field(
        default=0.15,
        ge=0.0,
        le=0.95,
        description=(
            "Fraction by which Stage 7 reduces max_tokens when the session "
            "incident threshold is exceeded (ADR-0059 §D6). 0.15 = 15 %% "
            "tightening for the next request in that session. "
            "Env var: AGENT_CONTEXT_QUALITY_GOVERNANCE_BUDGET_REDUCTION"
        ),
    )

    # ADR-0060 — Knowledge Graph Quality Stream (Wave 3 — FRE-250)
    graph_quality_stream_enabled: bool = Field(
        default=True,
        description=(
            "Enable ADR-0060 knowledge graph quality stream. When True, "
            "BrainstemScheduler._run_quality_monitoring() dual-writes detected "
            "anomalies to telemetry/graph_quality/GQ-*.jsonl and publishes "
            "GraphQualityAnomalyEvent on stream:graph.quality_anomaly; "
            "run_freshness_review() publishes MemoryStalenessReviewedEvent on "
            "stream:memory.staleness_reviewed. Both are consumed by cg:graph-monitor "
            "to write Captain's Log entries. "
            "Env var: AGENT_GRAPH_QUALITY_STREAM_ENABLED"
        ),
    )
    freshness_tier_reranking_enabled: bool = Field(
        default=True,
        description=(
            "Enable ADR-0060 §D5 StalenessTier multiplier in recall reranking. "
            "When True, _calculate_relevance_scores() applies a tier factor to "
            "the freshness score: DORMANT entities contribute 30%% of their raw "
            "freshness weight. Default True — additive refinement of an existing "
            "signal, no observable side effects other than improved recall quality. "
            "Env var: AGENT_FRESHNESS_TIER_RERANKING_ENABLED"
        ),
    )
    freshness_tier_factors: dict[str, float] = Field(
        default_factory=lambda: {
            "warm": 1.0,
            "cooling": 0.85,
            "cold": 0.60,
            "dormant": 0.30,
        },
        description=(
            "Per-tier multiplier applied to the freshness score during recall "
            "reranking (ADR-0060 §D5). Keys are StalenessTier.value strings. "
            "Defaults: warm=1.0, cooling=0.85, cold=0.60, dormant=0.30. "
            "Adjust dormant upward if DORMANT entities are over-suppressed after "
            "30+ days of Phase 1 data. No code change needed — set via env as "
            "JSON: AGENT_FRESHNESS_TIER_FACTORS='{\"warm\":1.0,...}'. "
            "Env var: AGENT_FRESHNESS_TIER_FACTORS"
        ),
    )
    graph_quality_governance_enabled: bool = Field(
        default=False,
        description=(
            "Enable ADR-0060 §D7 Phase 2 governance response for high-severity "
            "graph quality anomalies. When True, cg:graph-monitor publishes a "
            "ModeAdvisoryEvent(DEGRADED, 'consolidation') to stream:mode.transition "
            "for each high-severity GraphQualityAnomalyEvent. Default False — flip "
            "after 14 days of Phase 1 telemetry confirms false-positive rate < 20%%. "
            "Env var: AGENT_GRAPH_QUALITY_GOVERNANCE_ENABLED"
        ),
    )


_settings: AppConfig | None = None


def load_app_config() -> AppConfig:
    """Load and validate application configuration.

    This function:
    1. Loads .env files in priority order (via env_loader)
    2. Creates AppConfig instance (reads from environment variables)
    3. Validates all values using Pydantic
    4. Logs configuration loading using structlog

    Returns:
        Validated AppConfig instance.

    Raises:
        ValidationError: If configuration validation fails.
    """
    log.info("loading_app_config", environment=get_environment().value)

    # Load .env files in priority order before creating AppConfig
    load_env_files()

    try:
        config = AppConfig()
        log.info(
            "app_config_loaded",
            environment=config.environment.value,
            debug=config.debug,
            log_level=config.log_level,
            log_format=config.log_format,
        )
        return config
    except Exception as e:
        log.error("app_config_load_failed", error=str(e), error_type=type(e).__name__)
        raise


def get_settings() -> AppConfig:
    """Get the application settings singleton.

    Returns:
        AppConfig instance (singleton pattern).
    """
    global _settings
    if _settings is None:
        _settings = load_app_config()
    return _settings
