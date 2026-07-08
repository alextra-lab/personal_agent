"""Application configuration settings.

This module provides the AppConfig class and settings singleton.
"""

from pathlib import Path

import structlog
from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from personal_agent.config._owner_host_allowlist import is_owner_controlled_host
from personal_agent.config._substrate_fingerprint import (
    is_prod_elasticsearch_url,
    is_prod_neo4j_uri,
    is_prod_postgres_url,
)
from personal_agent.config.config_guard import (
    check_orphan_env_keys,
    load_matrix,
    repo_root,
    resolve_active_profile,
)
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
        default=[
            "http://localhost:3000",
            "https://seshat.frenchforet.com",
            "https://agent.frenchforet.com",
        ],
        description=(
            "CORS allowed origins for the FastAPI service. "
            "Must include the production PWA origin for WebSocket upgrades — "
            "Starlette CORSMiddleware silently drops WS connections with non-matching Origin."
        ),
    )

    # WebSocket transport (ADR-0075 / FRE-388)
    allowed_ws_origins: list[str] = Field(
        default=[
            "https://seshat.frenchforet.com",
            "https://agent.frenchforet.com",
            "http://localhost:3000",
        ],
        description="Allowed Origin headers for WebSocket upgrade requests (RFC 6455 §10.2).",
    )
    ws_ping_timeout_seconds: int = Field(
        default=60,
        description="Close socket if no inbound message (including PING) within this window.",
    )
    ws_max_message_size: int = Field(
        default=8192,
        description="Maximum inbound WebSocket message size in bytes.",
    )
    ws_rate_limit_per_second: int = Field(
        default=20,
        description="Inbound message rate cap per connection; exceeding closes with 1008.",
    )
    ws_event_queue_size: int = Field(
        default=500,
        description="Bounded asyncio.Queue size per session; overflow events are Postgres-only.",
    )
    ws_event_ttl_hours: int = Field(
        default=24,
        description="session_events rows older than this are purged by the cleanup task.",
    )
    ws_ticket_ttl_seconds: int = Field(
        default=30,
        description="Lifetime of a single-use WebSocket ticket before it expires.",
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
        default=False,
        description=(
            "If true, append llm_no_think_suffix to tool-request prompts and post-tool synthesis prompts "
            "to reduce latency and reasoning verbosity. Default False (FRE-434): the primary now runs "
            "with reasoning enabled and the sub-agent is an instruct variant, so /no_think is unnecessary; "
            "it is also a byte-identity hazard for the ADR-0081 §D2 frozen layout (it rewrites the current "
            "last user message each turn). Re-enable per deployment only if a no-think model is reintroduced."
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

    # Route-trace ledger (FRE-452 / ADR-0088). The preview is a PII gate: when False
    # (default) the ledger stores only a SHA-256 pointer + counts, never raw stimulus text.
    route_trace_store_preview: bool = Field(
        default=False,
        description=(
            "PII gate for the route-trace ledger. When True, a bounded user-message "
            "preview is persisted to Postgres (widening the PII exposure surface); when "
            "False, only a SHA-256 pointer and character/message counts are stored."
        ),
    )
    route_trace_preview_chars: int = Field(
        default=280,
        ge=0,
        description="Max stimulus-preview length when route_trace_store_preview is enabled",
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
    turn_observed_stream_maxlen: int = Field(
        default=10000,
        ge=100,
        description="MAXLEN (approximate) on stream:turn.observed — the ADR-0088 live "
        "observability stream (FRE-513). Best-effort; durability is the direct ledger write.",
    )
    turn_projector_enabled: bool = Field(
        default=True,
        description="Register the ADR-0088 live turn-observation projector (FRE-513). When "
        "off, the durable route-trace + api_costs writes still happen; only the live "
        "turn_status surface is absent (ADR-0088 D8).",
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
        default=64000,
        ge=1000,
        description=(
            "Comfortable context budget threshold for token management. "
            "Aligned to ~50% of the Qwen3.6-35B-A3B 131K thinking-mode window."
        ),
    )
    context_budget_max_tokens: int = Field(
        default=120000,
        ge=2000,
        description=(
            "Maximum context budget limit. Aligned to the SLM context_length "
            "(131K for Qwen3.6-35B-A3B) minus generation reserve, leaving ~11K "
            "for system + tool definitions + thinking output."
        ),
    )
    context_budget_generation_reserve_tokens: int = Field(
        default=32768,
        ge=500,
        description=(
            "Reserve tokens for LLM generation in context budget calculations. "
            "Matches the Qwen3.6-35B-A3B card's 'standard' thinking-mode output budget."
        ),
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
    sub_agent_max_tool_iterations: int = Field(
        default=5,
        ge=1,
        le=15,
        description=(
            "ADR-0086 D3. Max tool-use rounds a TOOLED_SEQUENTIAL discovery "
            "sub-agent may run before a forced final synthesis pass. Bounds "
            "worst-case sub-agent runtime against worker_timeout_seconds."
        ),
    )
    sub_agent_summary_max_chars: int = Field(
        default=8000,
        ge=500,
        description=(
            "ADR-0086 D4. Upper bound on the SubAgentResult.summary that enters "
            "the parent synthesis context. Deliberately generous in round 1 — we "
            "observe real discovery output before tightening the digest (FRE-481 "
            "A/B). full_output is always preserved uncapped for observability."
        ),
    )

    # --- Expansion controller (ADR-0036) ---
    orchestration_mode: str = Field(
        default="enforced",
        description="Expansion enforcement mode: 'enforced' (gateway binding) or 'autonomous' (LLM decides)",
    )

    # --- Artifact-build decomposition rollout (ADR-0086) ---
    artifact_decomposition_enabled: bool = Field(
        default=False,
        description=(
            "ADR-0086 rollout flag. When False (default) TOOL_USE turns route to "
            "SINGLE (legacy 'tool_use_single'). When True, MODERATE/COMPLEX "
            "artifact builds route to HYBRID for tool-using discovery "
            "decomposition. Off until FRE-480 (sub-agent tool loop) + FRE-481 "
            "(telemetry/A/B) land. Rollback = set False."
        ),
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
        description=(
            "Embedding vector dimensions: 1024 native for Qwen3-Embedding-0.6B "
            "(local/private profile) and also the measured MRL sweet-spot for the "
            "managed Qwen3-Embedding-8B profile (nDCG@5 peaks at 1024, beating "
            "native 4096 -- FRE-826)"
        ),
    )
    embedding_batch_size: int = Field(
        default=20,
        description="Max items per embedding API call",
    )
    dedup_similarity_threshold: float = Field(
        default=0.92,
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
    reranker_input_cap: int = Field(
        default=25,
        ge=1,
        description=(
            "FRE-672: max candidates passed *into* the cross-attention reranker per "
            "recall. The reranker cross-attends over every document it receives, so "
            "its latency scales with this cap, not with recall_candidate_cap. Only "
            "the top-N candidates by vector score are reranked; the rest pass through "
            "on their vector+recency score. Small by design (positives sit in the "
            "high-vector-score head); calibrated against recall@5 in FRE-655's A/B. "
            "FRE-696 lowered 50->25: the FRE-696 latency curve showed the 4B-mxfp8 "
            "reranker costs ~0.11s/candidate, so 25 bounds per-recall rerank to ~2.8s "
            "now that recall returns real volume (the 50 cap was set while recall was "
            "empty under the FRE-673 auth bug and never bit)."
        ),
    )

    # --- Relevance-bounded recall (ADR-0100 / FRE-653) ---
    relevance_bounded_recall_enabled: bool = Field(
        default=False,
        description=(
            "ADR-0100: build the query_memory candidate set by relevance "
            "(vector top-k over entity_embedding unioned with the entity-name "
            "match, recency cutoff removed, recency demoted to a ranking weight) "
            "instead of by recency. Default off reproduces legacy recency-gated "
            "recall exactly."
        ),
    )
    recall_similarity_floor: float = Field(
        default=0.0,
        ge=0.0,
        le=1.0,
        description=(
            "ADR-0100: minimum cosine similarity for a vector-expanded recall "
            "candidate entity. Config-driven and embedder-calibrated — never "
            "hardcoded. Entities below the floor are dropped before turn "
            "expansion. 0.0 = no floor (legacy-equivalent); calibrated in FRE-655."
        ),
    )
    embedding_backfill_enabled: bool = Field(
        default=True,
        description=(
            "FRE-659: periodically re-embed entities whose embedding is missing or "
            "zero-vectored (baked during an embedder outage). Idempotent and outage-safe "
            "(persists only a non-zero vector, under a guard that never clobbers a fresher "
            "concurrent write). Default on; off-switch for the recall substrate."
        ),
    )
    recall_per_entity_turn_cap: int = Field(
        default=10,
        ge=1,
        le=100,
        description=(
            "ADR-0100: max turns expanded per candidate entity (most-recent), "
            "before relevance ranking. Bounds the candidate set so distractors "
            "under other entities cannot crowd out the relevant turn. "
            "Config-driven so FRE-655 can calibrate without a code change."
        ),
    )
    recall_candidate_cap: int = Field(
        default=500,
        ge=1,
        le=5000,
        description=(
            "ADR-0100: hard backstop on total relevance-bounded recall candidates "
            "after per-entity expansion. The candidate set is ordered by entity "
            "relevance (then recency) before this cap, so the cap keeps the most "
            "relevant turns. Config-driven so FRE-655 can calibrate it."
        ),
    )

    # --- Structural / closed-axis retrieval arm (ADR-0104 AC-4 / FRE-707) ---
    structural_arm_enabled: bool = Field(
        default=False,
        description=(
            "ADR-0104 / FRE-707: master gate for the closed-axis structural recall "
            "arm (entity type + recency-as-predicate + relationship hops). Ships "
            "flag-dark: default off means the arm is never invoked and contributes "
            "no candidates. Enabled only once the multi-path fusion core (FRE-722/724) "
            "wires it in, under the FRE-433 flag->verified->rollout discipline."
        ),
    )
    structural_type_predicate_enabled: bool = Field(
        default=False,
        description=(
            "ADR-0104 AC-4 / ADR-0103 §4: gates the entity-type sub-predicate of the "
            "structural arm. Off until the type axis is closed by contract (FRE-637). "
            "When on, the type predicate is SAFE by construction — it narrows to the "
            "requested types but never drops rows whose entity_type is ''/'Unknown', "
            "so an unenforced-type entity is never silently lost."
        ),
    )
    structural_arm_top_k: int = Field(
        default=50,
        ge=1,
        le=500,
        description=(
            "FRE-707 / design spec §3.3: per-arm retrieval depth for the structural "
            "arm's ranked list, matching the multi-path arm depth default (50). "
            "Config-driven per ADR-0031."
        ),
    )

    # --- Multi-path recall seam (ADR-0104 AC-1/AC-3/AC-5/AC-6 / FRE-724) ---
    multipath_recall_enabled: bool = Field(
        default=False,
        description=(
            "ADR-0104 / FRE-724: master gate that routes the recall paths "
            "(query_memory_broad, query_memory, suggest_relevant) through the shared "
            "multi-path fused+reranked core instead of their single-path retrieval. "
            "Ships flag-dark: default off reproduces single-path recall exactly. "
            "Enabled only after master's FRE-489/670 live probe confirms the p50 "
            "latency ceiling (<=17s) and the noise-guard floor invariant hold "
            "(FRE-433 flag->verified->rollout discipline)."
        ),
    )

    # --- Lexical + multi-query retrieval arms (ADR-0104 AC-1/AC-3 / FRE-723) ---
    lexical_arm_enabled: bool = Field(
        default=False,
        description=(
            "ADR-0104 / FRE-723: master gate for the lexical full-text recall arm "
            "(Turn.user_message + Entity.name). Ships flag-dark: default off means "
            "the arm is never invoked. Enabled only once the multi-path fusion core "
            "(FRE-724) wires it in, under the FRE-433 flag->verified->rollout "
            "discipline."
        ),
    )
    multiquery_arm_enabled: bool = Field(
        default=False,
        description=(
            "ADR-0104 / FRE-723: master gate for the multi-query paraphrase recall "
            "arm. Ships flag-dark: default off means the arm is never invoked. "
            "Enabled only once the multi-path fusion core (FRE-724) wires it in."
        ),
    )
    multipath_arm_top_k: int = Field(
        default=50,
        ge=1,
        le=500,
        description=(
            "ADR-0104 / design spec §3.3: per-arm retrieval depth, shared across "
            "the lexical and multi-query arms (and the multi-query arm's "
            "per-variant dense sub-search depth) per the spec's single symmetric "
            "knob. The already-shipped structural arm (FRE-707) predates this and "
            "keeps its own structural_arm_top_k, not unified here."
        ),
    )
    multipath_paraphrase_count: int = Field(
        default=3,
        ge=1,
        le=10,
        description=(
            "ADR-0104 / design spec §3.3: number of query variants (the original "
            "plus paraphrases) the multi-query arm fans through the dense arm. "
            "Paraphrases are generated by the local SUB_AGENT model, never the "
            "primary. Set to 1 to use only the original query (paraphrase "
            "generation skipped)."
        ),
    )
    multipath_rrf_k: int = Field(
        default=60,
        ge=0,
        description=(
            "ADR-0104 / design spec §3.2: the RRF constant k, config-driven per "
            "ADR-0031 rather than relying on memory.fusion.DEFAULT_RRF_K. Tunable "
            "only via the FRE-489/670 probe as a regression instrument, never an "
            "optimization target (ADR-0103 §7)."
        ),
    )

    # --- Artifact substrate (ADR-0069 / FRE-227) ---
    # R2 (Cloudflare) holds the bytes; Postgres holds the metadata canon.
    # The agent talks to R2 via aiobotocore. The cloud-side Worker resolves
    # /{artifact_id} by calling back to /internal/artifacts/{id} on the
    # gateway with `X-Internal-Token: <artifact_resolve_internal_token>`.
    # All values are populated by the laptop-side terraform half (see the
    # sibling Linear ticket for FRE-227); when unset the notes_* tools are
    # not registered, see tools/__init__.py.
    r2_endpoint_url: str | None = Field(
        default=None,
        description="R2 S3-compatible endpoint, e.g. https://<account>.r2.cloudflarestorage.com",
    )
    r2_bucket_name: str = Field(
        default="seshat-artifacts",
        description="R2 bucket name for the artifact substrate",
    )
    r2_access_key_id: str | None = Field(
        default=None, description="R2 access key id (S3 SDK credential)"
    )
    r2_secret_access_key: str | None = Field(
        default=None,
        description="R2 secret access key (S3 SDK credential)",
        json_schema_extra={"secret": True},
    )
    r2_region: str = Field(
        default="auto",
        description="R2 region. R2 ignores the value but the S3 SDK requires one ('auto').",
    )
    artifacts_public_base_url: str | None = Field(
        default=None,
        description="Public Worker URL prefix, e.g. https://artifacts.frenchforet.com",
    )
    artifact_resolve_internal_token: str | None = Field(
        default=None,
        description=(
            "Shared secret the Worker presents to /internal/artifacts/{id}. "
            "Constant-time compared via secrets.compare_digest on the gateway side."
        ),
        json_schema_extra={"secret": True},
    )
    upload_max_size_bytes: int = Field(
        default=52_428_800,  # 50 MiB
        description="Maximum allowed user-upload file size in bytes (FRE-369).",
    )
    artifact_envelope_probe_enabled: bool = Field(
        default=True,
        description=(
            "Probe the served artifact URL after every commit and emit the "
            "artifact_envelope_integrity event (FRE-512, ADR-0089 D5). The probe "
            "is never load-bearing — failures log, the commit succeeds."
        ),
    )
    artifact_envelope_probe_timeout_s: float = Field(
        default=2.0,
        gt=0.0,
        le=30.0,
        description=(
            "Worst-case latency tax the envelope probe may add to an artifact "
            "commit. Kept tight: the commit already spends seconds in R2 + "
            "embedding; on timeout the event reports probe_status=probe_failed."
        ),
    )
    artifact_draft_max_tokens: int = Field(
        default=32768,
        ge=1024,
        le=65536,
        description=(
            "Max output tokens for the artifact-draft HTML sub-agent (ADR-0077). "
            "Raised from 16384 (FRE-478): observed artifacts reached ~31k output "
            "tokens (trace a0a07227), forcing an unintended cap-hit + continuation "
            "call. 32768 fits a typical artifact in one generation and matches the "
            "cloud claude-sonnet-4-6 model-def ceiling; the local SLM server is "
            "loaded with a ~65k context window, so 32768 + ~5k input stays well "
            "within it. Reservation is unaffected (cost_estimator uses "
            "min(max_tokens, default_output_tokens))."
        ),
    )
    attachment_image_max_pixels: int = Field(
        default=1568,
        gt=0,
        description=(
            "Per-image long-edge pixel cap before encoding (ADR-0101 §6, FRE-666). "
            "An over-limit image is downscaled (aspect-preserving) below this "
            "before the byte-size check. Matches Anthropic's own vision downscale "
            "threshold."
        ),
    )
    attachment_image_max_bytes: int = Field(
        default=5_242_880,  # 5 MiB
        gt=0,
        description=(
            "Per-image byte-size cap after downscale/base64-encode (ADR-0101 §6, "
            "FRE-666). An image still over this cap after downscale is rejected "
            "with a user-visible AttachmentUnsupportedError."
        ),
    )
    attachment_max_images_per_turn: int = Field(
        default=4,
        gt=0,
        description=(
            "Max raster images resolved per turn (ADR-0101 §6, FRE-666). Excess "
            "images (beyond the first N in submitted order) are dropped with "
            "disclosure."
        ),
    )
    attachment_max_total_payload_bytes: int = Field(
        default=15_728_640,  # 15 MiB
        gt=0,
        description=(
            "Total per-turn resolved-image payload cap across all images, "
            "measured post-base64-encode (ADR-0101 §6, FRE-666), independent of "
            "the per-image byte cap. Trailing images that would exceed it are "
            "dropped with disclosure."
        ),
    )
    attachment_cost_confirmation_threshold_usd: float = Field(
        default=0.50,
        ge=0.0,
        description=(
            "Pre-flight cloud-attachment cost above which the agent asks the user to "
            "confirm before spending, rather than proceeding silently (ADR-0101 §8b / "
            "FRE-691). A single image (~1600 tokens ≈ $0.005) is far below this, so the "
            "gate mainly bounds multi-image turns and the pricier ADR-0102 PDF path. "
            "Owner-tunable via AGENT_ATTACHMENT_COST_CONFIRMATION_THRESHOLD_USD."
        ),
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
        default=96000,
        ge=500,
        description=(
            "Maximum context token budget for conversation messages before each LLM call. "
            "Default leaves ~35k tokens of headroom against the primary Qwen3.6-35B-A3B's "
            "131k thinking-mode window for system prompt, tool definitions, skill blocks, "
            "memory slab, and response generation. Calibrate per model profile via env override."
        ),
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

    # Within-Session Progressive Context Compression (ADR-0061)
    within_session_compression_enabled: bool = Field(
        default=True,
        description=(
            "Enable head-middle-tail within-session compression (ADR-0061). "
            "Master kill switch — when False, hard-trigger is a no-op and "
            "soft-trigger reverts to pre-FRE-251 behaviour."
        ),
    )
    within_session_hard_threshold_ratio: float = Field(
        default=0.85,
        gt=0.0,
        le=1.0,
        description=(
            "Fire synchronous mid-orchestration compression when estimated "
            "tokens reach this fraction of context_window_max_tokens "
            "(ADR-0061 §D1)."
        ),
    )
    within_session_min_tail_ratio: float = Field(
        default=0.25,
        ge=0.0,
        lt=1.0,
        description=(
            "Fraction of context_window_max_tokens reserved as the preserved "
            "tail in head-middle-tail compression (ADR-0061 §D3). The absolute "
            "tail floor is computed at runtime as int(ratio * "
            "context_window_max_tokens). Replaces the prior fixed "
            "within_session_min_tail_tokens=2000 which left almost no room "
            "for head and middle when context_window_max_tokens was 2048."
        ),
    )
    within_session_pre_pass_threshold_tokens: int = Field(
        default=800,
        ge=0,
        description=(
            "Per-tool-message token threshold for the deterministic pre-pass "
            "replacement (ADR-0061 §D4)."
        ),
    )
    within_session_compression_refire_after_messages: int = Field(
        default=4,
        ge=1,
        description=(
            "Minimum number of new messages between consecutive soft "
            "compressions for the same session (ADR-0061 §D1)."
        ),
    )

    # Intra-turn tool-result compression (ADR-0085, FRE-475). Knobs modeled on
    # Anthropic's context-editing vocabulary. Master flag defaults OFF — the
    # feature rolls out only after the before/after A/B clears the gate.
    tool_result_compression_enabled: bool = Field(
        default=False,
        description=(
            "Master kill switch for intra-turn tool-result digestion (ADR-0085 §D7). "
            "When False, tool results enter the transcript verbatim (pre-FRE-475 behaviour). "
            "PARKED 2026-06-05 (FRE-486): ADR-0085 is parked dormant — do NOT enable on "
            "file-read-heavy workloads. The bash digest head/tail-truncates file content the "
            "model reads via cat/grep/sed, corrupting source (see ADR-0085 Decision Outcome). "
            "Safe only for large non-file outputs; revisit needs the allowlist redesign."
        ),
    )
    tool_result_digest_threshold_tokens: int = Field(
        default=1500,
        ge=0,
        description=(
            "Compress tool results whose estimated token count is at or above this "
            "(ADR-0085 §D7; open decision §1 — conservative 1.5–2k start)."
        ),
    )
    tool_result_digest_keep: int = Field(
        default=3,
        ge=0,
        description=(
            "Most-recent tool results kept verbatim regardless of size "
            "(Anthropic `keep` analogue; ADR-0085 §D4). Enforced in PR-B wiring."
        ),
    )
    tool_result_digest_min_savings_tokens: int = Field(
        default=500,
        ge=0,
        description=(
            "Skip digestion that would not save at least this many tokens "
            "(Anthropic `clear_at_least` analogue; ADR-0085 §D1/§D7)."
        ),
    )
    tool_result_digest_pin_ttl_turns: int = Field(
        default=4,
        ge=1,
        description=(
            "Rounds a read→edit dependency pin survives before the read becomes "
            "eligible for digestion (abandonment bound; ADR-0085 §D4). PR-B."
        ),
    )
    tool_result_digest_put_timeout_ms: int = Field(
        default=2000,
        ge=1,
        description=(
            "Insertion-path ceiling for the R2 put before digest substitution; on "
            "timeout the result is left verbatim (ADR-0085 §D1). PR-B."
        ),
    )
    tool_result_digest_exclude_tools: list[str] = Field(
        default_factory=list,
        description="Tool names exempt from digestion (per-tool opt-out; ADR-0085 §D7).",
    )
    tool_result_digest_head_lines: int = Field(
        default=40,
        ge=0,
        description="Lines of stream head retained in a head/tail digest (ADR-0085 §D2).",
    )
    tool_result_digest_tail_lines: int = Field(
        default=20,
        ge=0,
        description="Lines of stream tail retained in a head/tail digest (ADR-0085 §D2).",
    )
    tool_result_digest_max_expand_tokens: int = Field(
        default=8000,
        ge=1,
        description=(
            "Cap on tokens returned by expand_tool_result re-expansion "
            "(anti-spike; ADR-0085 §D5). PR-B."
        ),
    )

    # Cache-aware frozen append-only layout (ADR-0081 §D2/D3, FRE-434)
    cache_frozen_layout_enabled: bool = Field(
        default=True,
        description=(
            "Enable the frozen append-only prompt layout (ADR-0081 §D2/D3). When "
            "True, per-turn volatile content (recalled memory + selected skill "
            "bodies + salient highlights) rides the current user turn instead of "
            "the system head and is persisted byte-identically, so prior turns "
            "replay as a strict forward extension and the local SLM reuses its KV "
            "cache cross-turn. No-op when False (byte-for-byte the D1/D4 layout). "
            "Default True since 2026-06-02 — this is the deployed production behavior "
            "(FRE-434 shipped + verified + rolled out), pinned here so the rollout no "
            "longer depends on a single line in the gitignored .env (FRE-440). Set "
            "AGENT_CACHE_FROZEN_LAYOUT_ENABLED=false to revert to the head layout."
        ),
    )
    cache_reset_min_run_turns_local: int = Field(
        default=12,
        ge=1,
        description=(
            "Anti-thrash floor for the cache-aware compaction scheduler on the local "
            "backend (ADR-0081 §D3): never fire a reset before this many turns. Local "
            "resets are expensive (full re-prefill) so runs are allowed to grow longer."
        ),
    )
    cache_reset_min_run_turns_cloud: int = Field(
        default=4,
        ge=1,
        description=(
            "Anti-thrash floor for the scheduler on the cloud backend (ADR-0081 §D3). "
            "Cloud resets are cheap (only the rewritten span re-creates) so it may "
            "compact tighter/sooner."
        ),
    )
    cache_frozen_accum_max_ratio: float = Field(
        default=0.50,
        gt=0.0,
        le=1.0,
        description=(
            "Hard token ceiling for accumulated frozen context as a fraction of "
            "context_window_max_tokens (ADR-0081 §D3 Decision 2). Hitting it forces a "
            "compaction reset regardless of the cost optimum, reserving headroom for "
            "the system prefix, the current volatile, and generation."
        ),
    )
    cache_quality_token_weight: float = Field(
        default=4000.0,
        ge=0.0,
        description=(
            "w_q (ADR-0081 §D3): token-equivalent of one FRE-407 quality point, used "
            "in the scheduler's marginal hold cost c = Δ_turn + w_q·Q_slope. Tuned "
            "post-deploy against the A/B harness. "
            "Currently inert — quality_slope is hardwired to 0.0 pending the "
            "per-compaction quality signal from FRE-554/570/572 (FRE-576 F3)."
        ),
    )

    # Database (Postgres)
    # Connects as `seshat_app`, a non-superuser role scoped to the public-schema
    # DML the app needs (ADR-0105 T1 / FRE-808). It is granted nothing on schema
    # sysgraph, so the app connection cannot reach the isolated System graph even
    # under a future bug. Admin/migration DDL uses `database_admin_url` below.
    database_url: str = Field(
        default="postgresql+asyncpg://seshat_app:seshat_app_dev_password@localhost:5432/personal_agent",
        description="PostgreSQL database URL — the restricted seshat_app app role (FRE-808)",
    )
    # Superuser credential for schema bootstrap + migrations (CREATE ROLE / CREATE
    # SCHEMA / SET ROLE), which the restricted app role cannot run. Never used by
    # request-path code — only by migration tooling and admin/DDL test fixtures.
    database_admin_url: str = Field(
        default="postgresql+asyncpg://agent:agent_dev_password@localhost:5432/personal_agent",
        description="PostgreSQL admin URL — the `agent` superuser for migrations/DDL (FRE-808)",
    )
    database_echo: bool = Field(default=False, description="Echo SQL queries (for debugging)")

    # sysgraph — isolated System-graph schema (ADR-0105 D2/FRE-714). A distinct
    # role/connection so the recall/user-facing role is never granted access
    # (physical isolation proven at the DB permission layer, AC-2).
    sysgraph_database_url: str = Field(
        default="postgresql+asyncpg://sysgraph_role:sysgraph_dev_password@localhost:5432/personal_agent",
        description="PostgreSQL URL for the isolated sysgraph schema (ADR-0105 D2). "
        "Connects as the dedicated sysgraph_role, never the app's main role.",
    )

    # Elasticsearch
    elasticsearch_url: str = Field(default="http://localhost:9200", description="Elasticsearch URL")
    elasticsearch_index_prefix: str = Field(
        default="agent-logs", description="Elasticsearch index prefix"
    )
    captains_log_index_prefix: str = Field(
        default="agent-captains",
        description=(
            "Captain's Log Elasticsearch index prefix. "
            "Captures are stored as f'{prefix}-captures-YYYY.MM.DD'. "
            "Reflections are stored as f'{prefix}-reflections-YYYY.MM.DD'."
        ),
    )

    # Neo4j
    neo4j_uri: str = Field(default="bolt://localhost:7687", description="Neo4j connection URI")
    neo4j_user: str = Field(default="neo4j", description="Neo4j username")
    neo4j_password: str = Field(
        default="neo4j_dev_password",
        description="Neo4j password",
        json_schema_extra={"secret": True},
    )

    # Substrate isolation (FRE-375)
    allow_test_writes_to_prod_substrate: bool = Field(
        default=False,
        description=(
            "Emergency escape hatch: allow TEST environment to connect to prod-fingerprint URIs. "
            "Set AGENT_ALLOW_TEST_WRITES_TO_PROD_SUBSTRATE=1 only when intentionally running "
            "tests against a prod-equivalent stack (e.g. acceptance tests on the VPS). "
            "Never set this in CI."
        ),
    )

    # ── Configurable substrate backends (ADR-0112 D3 / AC-2, FRE-816) ─────────
    # The backend-selection seam: config/substrate.yaml declares, per profile,
    # which backend every substrate component (postgres/neo4j/elasticsearch,
    # embedder/reranker/slm, vector_index) resolves to; src/personal_agent/
    # config/substrate.py resolves it. `substrate_profile` selects the active
    # profile; the AGENT_MANAGED_* fields carry the `managed` profile's targets
    # (optional — the owner's `private` default never needs them). All resolve
    # through AppConfig so the resolver never reads os.environ directly.
    substrate_profile: str = Field(
        default="private",
        description=(
            "Active substrate-backend profile (ADR-0112 D3): one of the profiles "
            "declared in config/substrate.yaml — 'private' (default, owner-controlled), "
            "'managed' (all components managed), 'managed_embedder' (storage stays local; "
            "only the embedder is managed — ADR-0112 AC-5/AC-6, FRE-821), 'dev', 'test'. "
            "Selects each component's backend with no code change."
        ),
    )
    managed_database_url: str | None = Field(
        default=None,
        description=(
            "PostgreSQL URL for the `managed` substrate profile (ADR-0112 D3). "
            "Unset by default — set only when running the managed profile."
        ),
        json_schema_extra={"secret": True},
    )
    managed_neo4j_uri: str | None = Field(
        default=None,
        description="Neo4j URI for the `managed` substrate profile (ADR-0112 D3). Unset by default.",
        json_schema_extra={"secret": True},
    )
    managed_elasticsearch_url: str | None = Field(
        default=None,
        description=(
            "Elasticsearch URL for the `managed` substrate profile (ADR-0112 D3). Unset by default."
        ),
        json_schema_extra={"secret": True},
    )
    managed_embedding_endpoint: str | None = Field(
        default=None,
        description=(
            "Embedder endpoint for the `managed` substrate profile (ADR-0112 D3 — e.g. the "
            "OVH AI Endpoints Qwen3-Embedding-8B base URL). Unset by default."
        ),
        json_schema_extra={"secret": True},
    )
    managed_embedding_token: str | None = Field(
        default=None,
        description=(
            "Bearer token for the managed embedder endpoint (ADR-0112 AC-5/AC-6, FRE-821 — "
            "e.g. the OVH AI Endpoints API token). Unset by default."
        ),
        json_schema_extra={"secret": True},
    )
    managed_embedding_model: str = Field(
        default="Qwen3-Embedding-8B",
        description=(
            "Model id sent to the managed embedder endpoint (ADR-0112 AC-5/AC-6, FRE-821). "
            "Must name the same weights revision as local_fallback_embedding_model — the "
            "config_guard identity check enforces this."
        ),
    )
    local_fallback_embedding_endpoint: str | None = Field(
        default=None,
        description=(
            "Same-model local fallback embedder endpoint (ADR-0112 D4/AC-6, FRE-821) — used "
            "only when the `managed_embedder` substrate profile's managed call fails. Unset "
            "by default (no fallback attempted until an operator provisions one)."
        ),
    )
    local_fallback_embedding_model: str = Field(
        default="Qwen/Qwen3-Embedding-8B",
        description=(
            "Model id requested from the local fallback embedder endpoint (ADR-0112 AC-6, "
            "FRE-821). Must name the same weights revision as managed_embedding_model — the "
            "config_guard identity check enforces this."
        ),
    )
    managed_reranker_endpoint: str | None = Field(
        default=None,
        description="Reranker endpoint for the `managed` substrate profile (ADR-0112 D3). Unset by default.",
        json_schema_extra={"secret": True},
    )
    managed_slm_endpoint: str | None = Field(
        default=None,
        description="Harness-SLM endpoint for the `managed` substrate profile (ADR-0112 D3). Unset by default.",
        json_schema_extra={"secret": True},
    )
    owner_storage_allowlist: list[str] = Field(
        default=["postgres", "neo4j", "elasticsearch"],
        description=(
            "Owner-controlled storage hosts (ADR-0112 AC-1), checked in addition to "
            "loopback (always allowed). Each entry is an exact hostname or a CIDR "
            "range (e.g. '10.0.0.0/8'). Defaults to the Docker Compose service names "
            "the private profile's stores resolve to on the owner's VPS "
            "(docker-compose.cloud.yml). Enforced only when substrate_profile == "
            "'private'."
        ),
    )

    # Cloud API secrets (model identity lives in config/models.yaml — ADR-0031)
    anthropic_api_key: str | None = Field(
        default=None,
        description="Anthropic API key for Claude",
        json_schema_extra={"secret": True},
    )
    openai_api_key: str | None = Field(
        default=None,
        description="OpenAI API key",
        json_schema_extra={"secret": True},
    )

    # Linear (native tool — FRE-224)
    linear_api_key: str | None = Field(
        default=None,
        description="Linear Personal Access Token for the native create_linear_issue tool (FRE-224)",
        json_schema_extra={"secret": True},
    )
    linear_agent_rate_limit_per_day: int = Field(
        default=10,
        ge=1,
        description="Max agent-filed Linear issues per 24h (across all projects combined)",
    )
    linear_personal_agent_label_id: str | None = Field(
        default="25004aac-3b32-4fa4-bdc2-55ff348ea842",
        description=(
            "Linear label ID for the 'PersonalAgent' label (FRE-309). "
            "Using the ID directly bypasses fragile runtime name lookup. "
            "Defaults to the known production UUID."
        ),
    )

    # Perplexity AI (native tool — ADR-0028 Phase 2)
    perplexity_api_key: str | None = Field(
        default=None,
        description="Perplexity API key",
        json_schema_extra={"secret": True},
    )
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
    session_write_wait_timeout_seconds: float = Field(
        default=10.0,
        ge=0.1,
        le=120.0,
        description=(
            "Max seconds a /chat turn waits for the prior turn's session append "
            "before proceeding without it (FRE-520 safety valve)"
        ),
    )

    # Feature flags
    use_service_mode: bool = Field(default=True, description="Enable service mode")
    enable_second_brain: bool = Field(default=False, description="Enable second brain (Phase 2.2)")
    enable_memory_graph: bool = Field(default=False, description="Enable memory graph (Phase 2.2)")
    location_enabled: bool = Field(
        default=False,
        description="Master opt-in for location features (FRE-230). Default OFF.",
    )
    location_precision: str = Field(
        default="precise",
        description=(
            "'precise' stores verbatim device-coordinate fidelity; 'coarse' is an "
            "optional operator override that rounds latitude/longitude to 2 decimals."
        ),
    )

    @field_validator("location_precision")
    @classmethod
    def validate_location_precision(cls, v: str) -> str:
        """Reject unknown precision values at startup (FRE-230).

        Without this guard an unconstrained string means any typo (e.g.
        ``"course"``) falls through to the precise branch and silently stores
        raw coordinates, defeating the operator coarse override.

        Args:
            v: Candidate precision value.

        Returns:
            The validated value.

        Raises:
            ValueError: When the value is not ``"precise"`` or ``"coarse"``.
        """
        if v not in ("precise", "coarse"):
            raise ValueError(
                f"location_precision must be 'precise' or 'coarse', got {v!r}. "
                "Set AGENT_LOCATION_PRECISION=precise or =coarse."
            )
        return v

    # Second Brain Scheduling (Phase 2.2)
    # The four fields below are the host-resource gate, used only when the
    # agent and LLM co-reside on the same host (local-inference deployment).
    # Under remote-inference deployments (current VPS setup) the gate is
    # disabled and these values are inert. See ADR-0041 §Update 2026-05-14
    # (FRE-326) and BrainstemScheduler._should_consolidate.
    second_brain_resource_gating_enabled: bool = Field(
        default=True,
        description=(
            "Enable host-resource gating (idle-time + CPU/memory) before "
            "consolidation. True for local-inference deployments where the "
            "agent and LLM share a host; False for remote-inference "
            "deployments where host metrics don't reflect inference load."
        ),
    )
    second_brain_idle_time_seconds: float = Field(
        default=300.0,
        description="Idle time before consolidation (5 min) — local-inference only",
    )
    second_brain_cpu_threshold: float = Field(
        default=50.0,
        description="Max CPU usage for consolidation (50%) — local-inference only",
    )
    second_brain_memory_threshold: float = Field(
        default=70.0,
        description="Max memory usage for consolidation (70%) — local-inference only",
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
    entity_extraction_fewshot_exemplars_enabled: bool = Field(
        default=False,
        description=(
            "FRE-759: when True, splice the type-disambiguation + claim-emphasis "
            "few-shot exemplar block into the entity-extraction prompt. Ships "
            "flag-dark (default False); flipped only after the FRE-630 A/B proves "
            "entity_type_accuracy >=0.95 and claim_emission_recall >=0.8 with no "
            "regression on the near-ideal metrics."
        ),
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

    # Consolidator retry cap (FRE-380, Stage 1 of Turn/extraction decoupling)
    consolidator_max_extraction_attempts: int = Field(
        default=5,
        ge=1,
        le=20,
        description=(
            "Max entity-extraction attempts per capture before the consolidator "
            "writes a stub Turn (joinable but entity-less) and stops retrying. "
            "Closes the captures-without-Turn orphan accumulation when extraction "
            "is broken for extended periods."
        ),
    )

    # Joinability probe (ADR-0074 Phase 5 / FRE-376)
    joinability_probe_enabled: bool = Field(
        default=True,
        description="Enable scheduled joinability probe runs (ADR-0074 Phase 5)",
    )
    joinability_probe_interval_seconds: int = Field(
        default=3600,
        ge=60,
        description="Seconds between joinability probe runs in the brainstem scheduler",
    )
    joinability_probe_window_hours: int = Field(
        default=24,
        ge=1,
        le=168,
        description="Session sampling window for the joinability probe (hours)",
    )
    joinability_probe_index_prefix: str = Field(
        default="agent-monitors-joinability",
        description="Elasticsearch index prefix for joinability probe result docs",
    )

    # SLM health monitor (FRE-399 Layer 3 / ADR-0083)
    slm_health_url: str = Field(
        default="https://slm.frenchforet.com/health",
        description=(
            "URL of the Mac SLM server health endpoint, polled by the SLM-health monitor "
            "and the /api/inference/status endpoint. The liveness-only response (today) "
            "degrades gracefully; a richer response (from the Mac-side child ticket) "
            "fills in GPU util, VRAM, queue depth, and model-loaded status."
        ),
    )
    slm_health_probe_enabled: bool = Field(
        default=True,
        description="Enable the scheduled SLM-health probe (FRE-399 Layer 3 / ADR-0083)",
    )
    slm_health_probe_interval_seconds: float = Field(
        default=300.0,
        ge=30.0,
        description="Seconds between SLM-health probe runs in the brainstem scheduler",
    )
    slm_health_index_prefix: str = Field(
        default="agent-monitors-slm-health",
        description="Elasticsearch index prefix for SLM-health snapshot docs",
    )
    slm_health_cache_ttl_seconds: float = Field(
        default=45.0,
        ge=5.0,
        description=(
            "Seconds a cached SLM-health snapshot stays fresh. Used by "
            "/api/inference/status and the executor error-reason hint to read "
            "the last known state without making a new network call."
        ),
    )
    slm_gpu_util_degraded_pct: float = Field(
        default=95.0,
        ge=0.0,
        le=100.0,
        description="GPU utilisation %% at or above which the SLM is considered degraded",
    )
    slm_queue_depth_degraded: int = Field(
        default=4,
        ge=1,
        description="Pending request queue depth at or above which the SLM is considered degraded",
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

    # Captain's Log reflection cadence (FRE-710)
    captains_log_reflection_cadence_enabled: bool = Field(
        default=True,
        description="FRE-710: gate Captain's Log reflection to a per-session cadence instead of "
        "every turn. False reverts to unconditional per-turn reflection (pre-FRE-710 behavior).",
    )
    captains_log_reflection_min_interval_seconds: float = Field(
        default=1800.0,
        ge=0,
        description="FRE-710: minimum seconds between reflected turns for the same session_id, "
        "approximating 'once per session' (no durable session-end signal exists to trigger on "
        "literally). A turn that hits the iteration limit always bypasses this interval.",
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
        description=(
            "Pause promotion when open (non-terminal) Linear issues exceed this count. "
            "FRE-598: counts open work only — Done/Canceled issues stay non-archived "
            "while their project is open, so a raw non-archived count would wedge the gate."
        ),
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

    # Outcome ingestion + realized-value signal (ADR-0105 D7 / FRE-717)
    outcome_ingestion_enabled: bool = Field(
        default=True,
        description="Enable daily ticket-outcome ingestion into the sysgraph realized-value signal",
    )
    outcome_ingestion_hour_utc: int = Field(
        default=8,
        ge=0,
        le=23,
        description="UTC hour for daily outcome-ingestion sweep (distinct from feedback polling's 7)",
    )
    signal_window_days: int = Field(
        default=90,
        ge=1,
        description="Trailing window (days) over which realized-value v is computed (ADR-0105 D7)",
    )
    signal_smoothing_prior: float = Field(
        default=2.0,
        ge=0,
        description="Additive-smoothing prior in v = Sum(weights) / (n + prior); pulls cold-start "
        "keys toward 0 so one early verdict cannot swing a source",
    )
    signal_priority_clamp: float = Field(
        default=0.5,
        ge=0,
        le=1.0,
        description="Bound on v's promotion-ranking modulation: priority x (1 + clamp(v, -bound, bound))",
    )
    signal_suppression_threshold: float = Field(
        default=-0.4,
        description="v at or below this value triggers suppression (with signal_suppression_min_n)",
    )
    signal_suppression_min_n: int = Field(
        default=5,
        ge=1,
        description="Minimum in-window outcome count before suppression can trigger",
    )
    signal_suppression_cooldown_days: int = Field(
        default=30,
        ge=1,
        description="Suppression cooldown duration once triggered, parallel to the fingerprint "
        "suppression in captains_log/suppression.py",
    )

    # Sysgraph operational maintenance (ADR-0105 D8 / FRE-718)
    sysgraph_maintenance_enabled: bool = Field(
        default=True,
        description="Enable daily VACUUM (ANALYZE) of sysgraph tables (ADR-0105 D8/AC-7)",
    )
    sysgraph_maintenance_hour_utc: int = Field(
        default=9,
        ge=0,
        le=23,
        description="UTC hour for the daily sysgraph maintenance sweep (distinct from feedback "
        "polling's 7, outcome ingestion's 8)",
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
        json_schema_extra={"secret": True},
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

    # ── Operator Identity (FRE-213 / ADR-0052) ───────────────────────────────
    owner_name: str = Field(
        default="",
        alias="AGENT_OWNER_NAME",
        description=(
            "Display name of the deployment owner (e.g. 'Alex'). When set, a "
            "':Person {is_owner: true}' node is bootstrapped in Neo4j on startup "
            "and injected into the system prompt as '## Operator'. "
            "No-op when empty. Never logged in plain text."
        ),
    )
    agent_id: str = Field(
        default="seshat-local",
        alias="AGENT_AGENT_ID",
        description=(
            "Stable identifier for this agent deployment. Used to anchor the "
            "':Agent' node in Neo4j and bind it to the owner ':Person' via "
            "':OPERATED_BY'. Should be unique per deployment."
        ),
    )
    user_display_names_json: str = Field(
        default="{}",
        alias="AGENT_USER_DISPLAY_NAMES_JSON",
        description=(
            "JSON map of email → display_name for non-owner CF Access users. "
            "Applied idempotently at startup after owner bootstrap. "
            "Only overwrites a :Person.name that still equals the email local-part. "
            'Example: \'{"alice@x.com":"Alice","bob@x.com":"Bob"}\' '
            "Env var: AGENT_USER_DISPLAY_NAMES_JSON"
        ),
    )

    @property
    def user_display_names(self) -> dict[str, str]:
        """Parse user_display_names_json into a dict, returning {} on error."""
        import json
        from typing import cast

        try:
            parsed = json.loads(self.user_display_names_json)
            if isinstance(parsed, dict):
                return cast(dict[str, str], parsed)
            return {}
        except (json.JSONDecodeError, ValueError):
            return {}

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
        default=True,
        alias="AGENT_PREFER_PRIMITIVES",
        description=(
            "Inject skill library docs into system prompt and nudge the model to prefer "
            "primitives over MCP tools. Default True since 2026-06-01 — this is the deployed "
            "production behavior (pentest gate cleared, 6+ skill docs present), pinned here so "
            "it no longer depends on a single line in the gitignored .env. "
            "Requires AGENT_PRIMITIVE_TOOLS_ENABLED=true to be meaningful. "
            "Env var: AGENT_PREFER_PRIMITIVES"
        ),
    )

    # Phase B skill routing (FRE-skill-routing)
    skill_routing_mode: str = Field(
        default="hybrid",
        alias="AGENT_SKILL_ROUTING_MODE",
        description=(
            "Skill routing strategy when prefer_primitives_enabled is True. "
            "'keyword' — inject keyword-matched skill bodies only (Phase A legacy). "
            "'model_decided' — inject compact skill index; model calls read_skill on demand. "
            "'hybrid' — inject both index and keyword-matched bodies; suppress keyword body "
            "for any skill already read_skill'd this conversation. "
            "Env var: AGENT_SKILL_ROUTING_MODE"
        ),
    )
    skill_index_max_tokens: int = Field(
        default=2048,
        alias="AGENT_SKILL_INDEX_MAX_TOKENS",
        description=(
            "Token cap for the compact skill index injected in model_decided and hybrid modes. "
            "Index is truncated to this many tokens if it exceeds the limit. "
            "Env var: AGENT_SKILL_INDEX_MAX_TOKENS"
        ),
    )
    skill_routing_model_key: str = Field(
        default="claude_haiku",
        alias="AGENT_SKILL_ROUTING_MODEL_KEY",
        description=(
            "Model key from models.yaml used for skill routing decisions when "
            "skill_routing_mode=model_decided. Independent of the primary agent's "
            "model path (local vs cloud). The local SLM server is currently "
            "single-threaded and cannot run routing concurrently with the primary "
            "agent, so the default is a remote model (claude_haiku). When local "
            "concurrency improves OR for a fully-cloud deployment, point this at "
            "any models.yaml entry. Empty string disables the separate routing "
            "call (primary agent does its own routing via read_skill). "
            "Env var: AGENT_SKILL_ROUTING_MODEL_KEY"
        ),
    )

    # FRE-337: Skill nudge injection
    skill_nudge_enabled: bool = Field(
        default=True,
        alias="AGENT_SKILL_NUDGE_ENABLED",
        description=(
            "Inject deterministic skill directive blocks (<skill_index_directive> and "
            "<skill_usage_directives>) into the system prompt after skill content. "
            "Set to False for eval baseline cells. "
            "Env var: AGENT_SKILL_NUDGE_ENABLED"
        ),
    )

    # FRE-225: Egress URL guard (domain blocklist)
    url_guard_enabled: bool = Field(
        default=True,
        alias="AGENT_URL_GUARD_ENABLED",
        description=(
            "Enable egress URL guard. When True, fetch_url calls are checked against "
            "a domain blocklist (URLhaus feed + bundled fallback). "
            "Env var: AGENT_URL_GUARD_ENABLED"
        ),
    )
    url_guard_mode: str = Field(
        default="blocklist",
        alias="AGENT_URL_GUARD_MODE",
        description=(
            "URL guard mode: 'off' (no checks), 'blocklist' (block known-malicious domains), "
            "or 'allowlist' (block all except explicitly listed domains). "
            "Env var: AGENT_URL_GUARD_MODE"
        ),
    )
    url_guard_cache_ttl_seconds: int = Field(
        default=3600,
        ge=60,
        alias="AGENT_URL_GUARD_CACHE_TTL_SECONDS",
        description=(
            "Seconds before the domain blocklist cache is refreshed from URLhaus. "
            "Env var: AGENT_URL_GUARD_CACHE_TTL_SECONDS"
        ),
    )
    url_guard_allowlist: list[str] = Field(
        default_factory=list,
        alias="AGENT_URL_GUARD_ALLOWLIST",
        description=(
            "Comma-separated domain allowlist used when url_guard_mode=allowlist. "
            "Env var: AGENT_URL_GUARD_ALLOWLIST"
        ),
    )

    # FRE-335 / ADR-0066 D2: skill routing threshold monitor
    skill_index_p95_token_threshold: int = Field(
        default=6000,
        alias="AGENT_SKILL_INDEX_P95_TOKEN_THRESHOLD",
        ge=100,
        description=(
            "Token threshold for skill index p95 injection size. When the rolling "
            "7-day p95 of injected_chars (divided by 4) exceeds this value for two "
            "consecutive days, the threshold monitor files a Linear issue recommending "
            "AGENT_SKILL_ROUTING_MODE=model_decided. "
            "Env var: AGENT_SKILL_INDEX_P95_TOKEN_THRESHOLD"
        ),
    )
    skill_routing_threshold_monitor_enabled: bool = Field(
        default=True,
        alias="AGENT_SKILL_ROUTING_THRESHOLD_MONITOR_ENABLED",
        description=(
            "Enable the ADR-0066 D2 daily job that monitors skill index injection size "
            "and files a Linear ticket when the threshold is exceeded. "
            "Env var: AGENT_SKILL_ROUTING_THRESHOLD_MONITOR_ENABLED"
        ),
    )
    skill_routing_threshold_monitor_hour_utc: int = Field(
        default=5,
        alias="AGENT_SKILL_ROUTING_THRESHOLD_MONITOR_HOUR_UTC",
        ge=0,
        le=23,
        description=(
            "UTC hour at which the skill routing threshold monitor runs daily. "
            "Env var: AGENT_SKILL_ROUTING_THRESHOLD_MONITOR_HOUR_UTC"
        ),
    )

    # FRE-347 / FRE-346 G1: session-level narrative summariser
    session_summary_enabled: bool = Field(
        default=True,
        alias="AGENT_SESSION_SUMMARY_ENABLED",
        description=(
            "Generate prose summaries for sessions during consolidation so cross-session "
            "recall surfaces narrative context, not just entity facts. Uses the "
            "captains_log model role and the captains_log budget cap. "
            "Env var: AGENT_SESSION_SUMMARY_ENABLED"
        ),
    )

    # FRE-348 / FRE-346 G2 / ADR-0067: surface past Captain's Log reflections in context
    reflection_recall_enabled: bool = Field(
        default=True,
        alias="AGENT_REFLECTION_RECALL_ENABLED",
        description=(
            "Surface up to N past Captain's Log reflections in the assembled context "
            "for the next turn so cross-session use cases (resumable refactor state, "
            "abstract idea recovery, evolving hypothesis) have a retrieval path. "
            "Env var: AGENT_REFLECTION_RECALL_ENABLED"
        ),
    )
    reflection_recall_recency_days: int = Field(
        default=14,
        alias="AGENT_REFLECTION_RECALL_RECENCY_DAYS",
        ge=1,
        le=365,
        description=(
            "Look-back window for reflection recall. Env var: AGENT_REFLECTION_RECALL_RECENCY_DAYS"
        ),
    )
    reflection_recall_max_results: int = Field(
        default=3,
        alias="AGENT_REFLECTION_RECALL_MAX_RESULTS",
        ge=1,
        le=10,
        description=(
            "Maximum reflections surfaced per turn. Hard-capped by the prompt budget "
            "anyway; raising this above ~5 risks crowding out memory_context. "
            "Env var: AGENT_REFLECTION_RECALL_MAX_RESULTS"
        ),
    )
    reflection_recall_min_seen_count: int = Field(
        default=2,
        alias="AGENT_REFLECTION_RECALL_MIN_SEEN_COUNT",
        ge=1,
        le=20,
        description=(
            "Minimum seen_count for a proposal-shaped reflection to qualify. Recurring "
            "patterns are signal; one-offs are noise. Failure-path-fix-only entries are "
            "exempt from this filter. "
            "Env var: AGENT_REFLECTION_RECALL_MIN_SEEN_COUNT"
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

    @model_validator(mode="after")
    def _validate_compression_geometry(self) -> "AppConfig":
        """Reject configurations that leave too little budget for head + middle.

        Recovery plan 2026-05-05 (Wave 0.2): the prior defaults of
        context_window_max_tokens=2048 and within_session_min_tail_tokens=2000
        left ~48 tokens for system prompt, skills, memory, and middle. Catch
        any future drift back into that pathology at config load time.
        """
        absolute_tail = int(self.context_window_max_tokens * self.within_session_min_tail_ratio)
        head_middle_budget = self.context_window_max_tokens - absolute_tail
        if head_middle_budget < 1024:
            raise ValueError(
                "Compression geometry leaves less than 1024 tokens for head+middle: "
                f"context_window_max_tokens={self.context_window_max_tokens}, "
                f"within_session_min_tail_ratio={self.within_session_min_tail_ratio}, "
                f"absolute_tail={absolute_tail}, head_middle_budget={head_middle_budget}. "
                "Lower the ratio, raise the window, or both."
            )
        return self

    @model_validator(mode="after")
    def _validate_substrate_isolation(self) -> "AppConfig":
        """Refuse to start in TEST environment when substrate URIs point to prod defaults.

        This guard prevents test runs from accidentally writing to the production
        Neo4j graph, Elasticsearch indices, or PostgreSQL database (main app or
        sysgraph).  It fires when ALL three conditions hold:

        1. ``environment == Environment.TEST``
        2. At least one of the four substrate URIs matches the default prod
           fingerprint (localhost on the canonical port).
        3. ``allow_test_writes_to_prod_substrate`` is not set.

        Raises:
            ValueError: When the conditions above are all met, with an actionable
                message naming the offending URIs and the env vars to fix them.
        """
        if self.environment != Environment.TEST:
            return self
        if self.allow_test_writes_to_prod_substrate:
            return self

        offenders: list[str] = []
        if is_prod_neo4j_uri(self.neo4j_uri):
            offenders.append(f"neo4j_uri={self.neo4j_uri!r}")
        if is_prod_elasticsearch_url(self.elasticsearch_url):
            offenders.append(f"elasticsearch_url={self.elasticsearch_url!r}")
        if is_prod_postgres_url(self.database_url):
            offenders.append(f"database_url={self.database_url!r}")
        if is_prod_postgres_url(self.database_admin_url):
            offenders.append(f"database_admin_url={self.database_admin_url!r}")
        if is_prod_postgres_url(self.sysgraph_database_url):
            offenders.append(f"sysgraph_database_url={self.sysgraph_database_url!r}")

        if not offenders:
            return self

        raise ValueError(
            f"Running in TEST environment (APP_ENV=test) but substrate URIs point to "
            f"prod/dev defaults. Offending: {', '.join(offenders)}. "
            "Set AGENT_NEO4J_URI=bolt://localhost:7688 (test stack), "
            "AGENT_ELASTICSEARCH_URL=http://localhost:9201, "
            "AGENT_DATABASE_URL=<test-db-url>, "
            "AGENT_DATABASE_ADMIN_URL=<test-db-url>, "
            "AGENT_SYSGRAPH_DATABASE_URL=<test-db-url>, "
            "or set AGENT_ALLOW_TEST_WRITES_TO_PROD_SUBSTRATE=1 to bypass (use with care)."
        )

    @model_validator(mode="after")
    def _validate_owner_storage_allowlist(self) -> "AppConfig":
        """Refuse to boot when the private profile's stores resolve off the owner allowlist.

        ADR-0112 AC-1: in the `private` (default) substrate profile, every resolved
        Postgres/Neo4j/Elasticsearch target must be owner-controlled — loopback, or a
        host declared in `owner_storage_allowlist`. A provider/managed hostname can
        never be passed off as owned by default.

        Fires whenever `substrate_profile == "private"` — independent of
        `environment` (this is a custody guard, not a test-isolation guard; the
        automated test suite instead declares `AGENT_SUBSTRATE_PROFILE=test` via
        `tests/conftest.py` to stay out of scope).

        Checks the same five Postgres/Neo4j/Elasticsearch fields the FRE-375 guard
        above checks, since `config/substrate.yaml`'s `private` profile maps its
        `postgres`/`neo4j`/`elasticsearch` components to exactly `database_url`/
        `neo4j_uri`/`elasticsearch_url` (see
        tests/personal_agent/config/test_substrate_manifest_drift.py, which fails
        loudly if that mapping ever changes).

        Raises:
            ValueError: When a store resolves to a host outside the allowlist.
        """
        if self.substrate_profile != "private":
            return self

        offenders: list[str] = []
        if not is_owner_controlled_host(self.database_url, self.owner_storage_allowlist):
            offenders.append(f"database_url={self.database_url!r}")
        if not is_owner_controlled_host(self.database_admin_url, self.owner_storage_allowlist):
            offenders.append(f"database_admin_url={self.database_admin_url!r}")
        if not is_owner_controlled_host(self.sysgraph_database_url, self.owner_storage_allowlist):
            offenders.append(f"sysgraph_database_url={self.sysgraph_database_url!r}")
        if not is_owner_controlled_host(self.neo4j_uri, self.owner_storage_allowlist):
            offenders.append(f"neo4j_uri={self.neo4j_uri!r}")
        if not is_owner_controlled_host(self.elasticsearch_url, self.owner_storage_allowlist):
            offenders.append(f"elasticsearch_url={self.elasticsearch_url!r}")

        if not offenders:
            return self

        raise ValueError(
            f"substrate_profile='private' but the following stores resolve off the "
            f"owner allowlist (ADR-0112 AC-1): {', '.join(offenders)}. Add the host "
            "to AGENT_OWNER_STORAGE_ALLOWLIST, point it at a loopback/owned host, or "
            "set AGENT_SUBSTRATE_PROFILE=managed if this is an intentional managed backend."
        )

    @model_validator(mode="after")
    def _validate_config_guard_policy(self) -> "AppConfig":
        """Orphan-``.env`` policy check (ADR-0099 D4, FRE-649).

        Reads ``config/model_roles.yaml`` (if present) and warn-loud (never
        raise) on any orphan ``.env.example`` key — a policy finding;
        CI/pre-commit (``scripts/check_config.py``) is the hard gate for
        these, not boot. A missing or unreadable matrix file is skipped
        silently, so it can never make an otherwise-valid ``AppConfig()``
        construction fail.

        The required-secret-per-profile *safety* check (ADR-0099 D4's other
        half of this hook) deliberately does **not** live here — see
        ``enforce_required_secrets`` and its call site in
        ``load_app_config()`` for why.
        """
        root = repo_root()
        matrix = load_matrix(root)
        if not matrix:
            return self

        orphans = check_orphan_env_keys(root)
        if orphans:
            log.warning(
                "config_guard_orphan_env_keys",
                findings=[o.message for o in orphans],
            )
        return self


def enforce_required_secrets(config: "AppConfig", *, root: Path | None = None) -> None:
    """Hard-fail if a secret required by the ACTIVE profile is unset (ADR-0099 D4, FRE-649).

    Deliberately a plain function called from ``load_app_config()`` — the
    real application-boot entry point — rather than an ``AppConfig``
    ``model_validator``. Ad-hoc ``AppConfig()`` / ``.model_validate()``
    construction is pervasive across the test suite (tests deliberately
    bypass env-file loading for isolation), and several legitimate
    eval-harness test files (FRE-435/FRE-630) pin
    ``AGENT_MODEL_CONFIG_PATH=config/models.cloud.yaml`` via an import-time
    ``os.environ.setdefault`` that outlives their own test module for the
    rest of the pytest session. A pydantic validator would fire on every one
    of those incidental constructions; a plain post-construction check called
    only from the real boot path fires only when the application is actually
    starting.

    Args:
        config: The constructed ``AppConfig`` to check.
        root: Repo root override (test seam); defaults to ``repo_root()``.

    Raises:
        ValueError: When the active profile requires a secret that is unset.
    """
    resolved_root = root if root is not None else repo_root()
    matrix = load_matrix(resolved_root)
    if not matrix:
        return

    active_profile = resolve_active_profile(config.model_config_path, matrix, resolved_root)
    required_secrets_by_profile = matrix.get("required_secrets", {})
    required_secrets: list[str] = (
        required_secrets_by_profile.get(active_profile, [])
        if isinstance(required_secrets_by_profile, dict)
        else []
    )
    missing = [name for name in required_secrets if getattr(config, name, None) is None]
    if missing:
        raise ValueError(
            f"Active profile {active_profile!r} (resolved from model_config_path="
            f"{config.model_config_path!r}) requires secrets {missing} but they are unset. "
            "Set the corresponding AGENT_* env var(s) before starting."
        )


def _log_active_substrate_profile(config: "AppConfig") -> None:
    """Log the declared substrate backends for the active profile (ADR-0112 / FRE-816).

    Boot observability for the backend-selection seam: emits, at startup, which
    profile is active and — per D3 component — its declared custody ``kind`` and
    ``source``. Reads the manifest declaration only (via
    ``config_guard.load_substrate_manifest``); it deliberately does NOT resolve
    targets, so it stays free of the model-loader import chain that runs during
    the settings-module partial-import window. Never logs a target *value* (a
    ``managed_*`` URL may embed a credential). Non-fatal: any error warns, never
    wedges boot.
    """
    try:
        from personal_agent.config.config_guard import (  # noqa: PLC0415
            load_substrate_manifest,
            repo_root,
        )

        manifest = load_substrate_manifest(repo_root())
        profiles = manifest.get("profiles")
        rows = profiles.get(config.substrate_profile) if isinstance(profiles, dict) else None
        if not isinstance(rows, dict):
            log.warning(
                "substrate_profile_undeclared",
                substrate_profile=config.substrate_profile,
            )
            return
        log.info(
            "substrate_profile_active",
            substrate_profile=config.substrate_profile,
            backends={
                component: {
                    "kind": row.get("kind"),
                    "source": row.get("source"),
                }
                for component, row in rows.items()
                if isinstance(row, dict)
            },
        )
    except Exception as exc:  # noqa: BLE001 — boot observability must never down the service
        log.warning(
            "substrate_profile_log_failed",
            substrate_profile=config.substrate_profile,
            error=str(exc),
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
        ValueError: If a secret required by the active profile is unset
            (ADR-0099 D4, FRE-649 — see ``enforce_required_secrets``).
    """
    log.info("loading_app_config", environment=get_environment().value)

    # Load .env files in priority order before creating AppConfig
    load_env_files()

    try:
        config = AppConfig()
        enforce_required_secrets(config)
        _log_active_substrate_profile(config)
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
