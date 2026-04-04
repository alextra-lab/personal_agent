"""Tool execution layer with governance, validation, and telemetry.

This module provides:
- Tool registry for tool discovery and registration
- Tool execution layer with permission checks and validation
- MVP tools (read_file, system_metrics_snapshot, self_telemetry_query)
- CLI-first native tools replacing MCP tools (ADR-0028)
"""

from personal_agent.tools.sysdiag import (
    run_sysdiag_executor,
    run_sysdiag_tool,
)
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
from personal_agent.tools.system_health import (
    system_metrics_snapshot_executor,
    system_metrics_snapshot_tool,
)
from personal_agent.tools.types import ToolDefinition, ToolParameter, ToolResult
from personal_agent.tools.web import (
    web_search_executor,
    web_search_tool,
)

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

    Registers the MVP tools plus CLI-first native tools that replace
    MCP tools per ADR-0028:
    - read_file: Read file contents
    - list_directory: List directory contents
    - system_metrics_snapshot: Get system health metrics
    - search_memory: Query memory graph (ADR-0026)
    - self_telemetry_query: Query agent execution history
    - web_search: Private web search via SearXNG (ADR-0034)
    - query_elasticsearch: ES|QL + index ops (ADR-0028 Phase 1)
    - perplexity_query: Perplexity AI synthesized answers (ADR-0028 Phase 2)
    - fetch_url: Fetch and extract webpage text (ADR-0028 Phase 3)
    - get_library_docs: Context7 library documentation (ADR-0028 Phase 3)
    - run_sysdiag: System diagnostic commands via subprocess allow-list (FRE-188)

    Args:
        registry: Tool registry to register tools with.
    """
    registry.register(read_file_tool, read_file_executor)
    registry.register(list_directory_tool, list_directory_executor)
    registry.register(system_metrics_snapshot_tool, system_metrics_snapshot_executor)
    registry.register(search_memory_tool, search_memory_executor)
    registry.register(self_telemetry_query_tool, self_telemetry_query_executor)
    registry.register(web_search_tool, web_search_executor)  # ADR-0034
    # ADR-0028 CLI-first native tools
    registry.register(query_elasticsearch_tool, query_elasticsearch_executor)
    registry.register(perplexity_query_tool, perplexity_query_executor)
    registry.register(fetch_url_tool, fetch_url_executor)
    registry.register(get_library_docs_tool, get_library_docs_executor)
    # FRE-188: system diagnostics
    registry.register(run_sysdiag_tool, run_sysdiag_executor)


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
