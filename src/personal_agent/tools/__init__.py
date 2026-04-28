"""Tool execution layer with governance, validation, and telemetry.

This module provides:
- Tool registry for tool discovery and registration
- Tool execution layer with permission checks and validation
- MVP tools (read_file, system_metrics_snapshot, self_telemetry_query)
- CLI-first native tools replacing MCP tools (ADR-0028)
"""

import structlog

from personal_agent.tools.context7 import (
    get_library_docs_executor,
    get_library_docs_tool,
)
from personal_agent.tools.elasticsearch import (
    query_elasticsearch_executor,
    query_elasticsearch_tool,
)
from personal_agent.tools.executor import ToolExecutionError, ToolExecutionLayer
from personal_agent.tools.fetch import (
    fetch_url_executor,
    fetch_url_tool,
)
from personal_agent.tools.filesystem import (
    list_directory_executor,
    list_directory_tool,
    read_file_executor,
    read_file_tool,
)
from personal_agent.tools.infra_health import (
    infra_health_executor,
    infra_health_tool,
)
from personal_agent.tools.linear import (
    create_linear_issue_executor,
    create_linear_issue_tool,
    create_linear_project_executor,
    create_linear_project_tool,
    find_linear_issues_executor,
    find_linear_issues_tool,
    list_linear_projects_executor,
    list_linear_projects_tool,
)
from personal_agent.tools.memory_search import (
    search_memory_executor,
    search_memory_tool,
)
from personal_agent.tools.perplexity import (
    perplexity_query_executor,
    perplexity_query_tool,
)
from personal_agent.tools.registry import ToolRegistry
from personal_agent.tools.self_telemetry import (
    self_telemetry_query_executor,
    self_telemetry_query_tool,
)
from personal_agent.tools.sysdiag import (
    run_sysdiag_executor,
    run_sysdiag_tool,
)
from personal_agent.tools.system_health import (
    system_metrics_snapshot_executor,
    system_metrics_snapshot_tool,
)
from personal_agent.tools.types import ToolDefinition, ToolParameter, ToolResult
from personal_agent.tools.web import (
    web_search_executor,
    web_search_tool,
)

log = structlog.get_logger(__name__)

__all__ = [
    # Core exports
    "ToolRegistry",
    "ToolExecutionLayer",
    "ToolExecutionError",
    "ToolDefinition",
    "ToolParameter",
    "ToolResult",
    # Tool registration function
    "register_mvp_tools",
    "get_default_registry",
]


def register_mvp_tools(registry: ToolRegistry) -> None:
    """Register all native tools with the registry.

    Always registered:
    - search_memory: Query memory graph (ADR-0026)
    - web_search: Private web search via SearXNG (ADR-0034)
    - perplexity_query: Perplexity AI synthesized answers (ADR-0028 Phase 2)
    - get_library_docs: Context7 library documentation (ADR-0028 Phase 3)
    - Linear tools: create/find/list (FRE-224, Tier-1)

    Registered only when ``settings.legacy_tools_enabled`` is True
    (rollback mode — default False per ADR-0063 PIVOT-4 / FRE-263):
    - read_file, list_directory, system_metrics_snapshot, self_telemetry_query
    - query_elasticsearch (ADR-0028 Phase 1)
    - fetch_url (ADR-0028 Phase 3)
    - run_sysdiag (FRE-188)
    - infra_health
    A ``tool_deprecated`` WARNING is emitted at startup when this flag is True.

    When ``settings.primitive_tools_enabled`` is True (opt-in, default False),
    the four FRE-261 primitive tools are also registered:
    - read: Low-level file reader
    - write: Low-level file writer
    - bash: Shell command executor via /bin/bash (FRE-283)
    - run_python: Python Docker-sandbox executor

    Args:
        registry: Tool registry to register tools with.
    """
    from personal_agent.config import settings  # noqa: PLC0415

    # --- Always-available tools ---
    registry.register(search_memory_tool, search_memory_executor)
    registry.register(web_search_tool, web_search_executor)  # ADR-0034
    registry.register(perplexity_query_tool, perplexity_query_executor)
    registry.register(get_library_docs_tool, get_library_docs_executor)
    # FRE-224: native Linear tool (Tier-1, no MCP gateway required)
    registry.register(create_linear_issue_tool, create_linear_issue_executor)
    registry.register(find_linear_issues_tool, find_linear_issues_executor)
    registry.register(list_linear_projects_tool, list_linear_projects_executor)
    registry.register(create_linear_project_tool, create_linear_project_executor)

    # --- 8 legacy curated tools deprecated by ADR-0063 PIVOT-4 (FRE-263) ---
    # Default off (AGENT_LEGACY_TOOLS_ENABLED=false). Rollback: set True.
    # Primitives + skill docs cover all 8 use cases (G3 eval 19/20, 1.39×).
    # Code deletion: FRE-265 after >=2 weeks production stability.
    if settings.legacy_tools_enabled:
        registry.register(read_file_tool, read_file_executor)
        registry.register(list_directory_tool, list_directory_executor)
        registry.register(system_metrics_snapshot_tool, system_metrics_snapshot_executor)
        registry.register(self_telemetry_query_tool, self_telemetry_query_executor)
        registry.register(query_elasticsearch_tool, query_elasticsearch_executor)
        registry.register(fetch_url_tool, fetch_url_executor)
        registry.register(run_sysdiag_tool, run_sysdiag_executor)
        registry.register(infra_health_tool, infra_health_executor)
        log.warning(
            "tool_deprecated",
            tools=[
                "read_file",
                "list_directory",
                "system_metrics_snapshot",
                "self_telemetry_query",
                "query_elasticsearch",
                "fetch_url",
                "run_sysdiag",
                "infra_health",
            ],
            replacement="bash / read / run_python primitives + skill docs",
            reason="ADR-0063 PIVOT-4 (FRE-263); AGENT_LEGACY_TOOLS_ENABLED=true is rollback-only",
        )

    # FRE-261 PIVOT-2 — primitive tools (ADR-0063 Phase 2).
    # Lazy imports inside the guard to avoid circular-import issues and to
    # ensure these modules are never loaded when the flag is off.
    if settings.primitive_tools_enabled:
        from personal_agent.tools.primitives.bash import bash_executor, bash_tool  # noqa: PLC0415, I001
        from personal_agent.tools.primitives.read import read_executor, read_tool  # noqa: PLC0415
        from personal_agent.tools.primitives.run_python import (  # noqa: PLC0415
            run_python_executor,
            run_python_tool,
        )
        from personal_agent.tools.primitives.write import write_executor, write_tool  # noqa: PLC0415

        registry.register(read_tool, read_executor)
        registry.register(write_tool, write_executor)
        registry.register(bash_tool, bash_executor)
        registry.register(run_python_tool, run_python_executor)
        log.info("primitive_tools_registered", count=4)


# Global singleton registry
_default_registry: ToolRegistry | None = None


def get_default_registry() -> ToolRegistry:
    """Get the singleton tool registry with MVP tools pre-registered.

    This ensures all parts of the application share the same registry,
    so MCP tools registered during service initialization are available
    to the orchestrator.

    Returns:
        ToolRegistry singleton with MVP tools (and any dynamically registered tools).
    """
    global _default_registry
    if _default_registry is None:
        _default_registry = ToolRegistry()
        register_mvp_tools(_default_registry)
    return _default_registry
