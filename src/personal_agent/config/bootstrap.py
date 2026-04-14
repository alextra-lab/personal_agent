"""Bootstrap configuration helpers (pre-settings) and composition root.

Two distinct concerns live here:

1. **Pre-settings helpers** (``get_bootstrap_log_level``): lightweight helpers
   needed before the full Pydantic settings singleton can be imported.
   Keep these dependency-light to avoid circular imports.

2. **Composition root** (``bootstrap``): the *only* place in the codebase that
   imports concrete implementations and wires them against protocol types
   (ADR-0049 Phase 1).  All other modules depend on protocols, not concretions.

Constraints:
- Pre-settings helpers must not import telemetry (circular import risk).
- ``bootstrap()`` may import any module — concrete imports are intentional here.
- Prefer validating values using existing config validators.
"""

from __future__ import annotations

import os
from typing import Any

from personal_agent.config.env_loader import load_env_files
from personal_agent.config.validators import validate_log_level

_env_loaded_for_log_level = False


def get_bootstrap_log_level(default: str = "INFO") -> str:
    """Get logging level from environment without importing settings.

    Ensures .env files are loaded before reading APP_LOG_LEVEL so that
    APP_LOG_LEVEL=WARNING (and similar) in .env is respected even when
    logging is configured before the full settings singleton is used.

    Args:
        default: Default log level if not set or invalid.

    Returns:
        Uppercased, validated log level string.
    """
    global _env_loaded_for_log_level
    if not _env_loaded_for_log_level:
        load_env_files()
        _env_loaded_for_log_level = True
    value = os.getenv("APP_LOG_LEVEL", default)
    try:
        return validate_log_level(value)
    except ValueError:
        return validate_log_level(default)


# ---------------------------------------------------------------------------
# Composition root — ADR-0049 Phase 1
# ---------------------------------------------------------------------------


class _WiredDependencies:
    """Container for concretions wired against protocol interfaces.

    Attributes:
        memory_service: Concrete MemoryService instance (or None if disabled).
        memory_adapter: MemoryServiceAdapter satisfying MemoryProtocol.
        tool_registry: Populated ToolRegistry.
        tool_executor: ToolExecutionLayer satisfying ToolExecutorProtocol.
        es_logger: ElasticsearchLogger satisfying TraceSinkProtocol.
    """

    def __init__(
        self,
        memory_service: Any,
        memory_adapter: Any,
        tool_registry: Any,
        tool_executor: Any,
        es_logger: Any,
    ) -> None:
        """Wire concrete implementations against protocol contracts."""
        self.memory_service = memory_service
        self.memory_adapter = memory_adapter
        self.tool_registry = tool_registry
        self.tool_executor = tool_executor
        self.es_logger = es_logger


def bootstrap(profile: str = "local") -> _WiredDependencies:
    """Wire concrete implementations from protocol definitions.

    This is the **composition root**: the only place that imports concrete
    implementations.  All other modules depend on protocols (ADR-0049).
    The ``profile`` parameter is reserved for future multi-environment support
    (e.g. ``"local"``, ``"ci"``, ``"staging"``).

    Concrete imports are deferred inside this function intentionally — they
    must not appear at module level to preserve the protocol boundary.

    Args:
        profile: Deployment profile hint (currently unused; reserved).

    Returns:
        _WiredDependencies container with all concretions ready for injection.

    Example:
        deps = bootstrap()
        # Inject into FastAPI lifespan or test fixtures:
        app.state.memory = deps.memory_adapter
        app.state.tool_executor = deps.tool_executor
    """
    # --- Memory (Neo4j) -------------------------------------------------
    # Import here so protocol consumers never need to see the concretion.
    from personal_agent.memory.protocol_adapter import MemoryServiceAdapter
    from personal_agent.memory.service import MemoryService

    memory_service = MemoryService()
    memory_adapter: Any = MemoryServiceAdapter(memory_service)

    # --- Tools ----------------------------------------------------------
    from personal_agent.tools.executor import ToolExecutionLayer
    from personal_agent.tools.registry import ToolRegistry

    tool_registry = ToolRegistry()
    tool_executor: Any = ToolExecutionLayer(registry=tool_registry)

    # --- Telemetry (Elasticsearch) --------------------------------------
    from personal_agent.config.settings import get_settings

    _settings = get_settings()

    from personal_agent.telemetry.es_logger import ElasticsearchLogger

    es_logger: Any = ElasticsearchLogger(
        es_url=_settings.elasticsearch_url,
    )

    return _WiredDependencies(
        memory_service=memory_service,
        memory_adapter=memory_adapter,
        tool_registry=tool_registry,
        tool_executor=tool_executor,
        es_logger=es_logger,
    )
