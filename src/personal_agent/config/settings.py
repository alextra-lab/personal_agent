"""Application configuration settings.

This module provides the AppConfig class and settings singleton.
"""

from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
import structlog

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
        default="http://localhost:8000/v1", description="Base URL for LLM API (slm_server default)"
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
        default=3,
        ge=0,
        description="Maximum tool execution iterations per user request (prevents tool loops)",
    )
    orchestrator_max_repeated_tool_calls: int = Field(
        default=1,
        ge=0,
        description="Maximum times the same tool call signature can repeat per request",
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
        default_factory=list, description="List of MCP server names to enable (empty = all)"
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

    # Entity Extraction Configuration (Phase 2.2)
    entity_extraction_model: str = Field(
        default="qwen3-8b",
        description="Model for entity extraction: 'qwen3-8b' (reasoning), 'lfm2.5-1.2b' (fast), or 'claude' (cloud)",
    )

    # Claude API (Second Brain) - optional for production quality
    anthropic_api_key: str | None = Field(default=None, description="Anthropic API key for Claude")
    claude_model: str = Field(default="claude-sonnet-4-5-20250514", description="Claude model name")
    claude_max_tokens: int = Field(default=4096, description="Maximum tokens for Claude requests")
    claude_weekly_budget_usd: float = Field(
        default=5.0, description="Weekly budget for Claude API (USD)"
    )

    # Feature flags
    use_service_mode: bool = Field(default=True, description="Enable service mode")
    enable_second_brain: bool = Field(default=False, description="Enable second brain (Phase 2.2)")
    enable_memory_graph: bool = Field(default=False, description="Enable memory graph (Phase 2.2)")

    # Second Brain Scheduling (Phase 2.2)
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
