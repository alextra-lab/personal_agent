"""Tool execution layer with governance, validation, and telemetry.

This module provides:
- Tool registry for tool discovery and registration
- Tool execution layer with permission checks and validation
- CLI-first native tools replacing MCP tools (ADR-0028)
- Primitive tools (read/write/bash/run_python) per ADR-0063 (FRE-261)
"""

import structlog

from personal_agent.tools.artifact_tools import (
    artifact_draft_executor,
    artifact_draft_tool,
    artifact_list_executor,
    artifact_list_tool,
    artifact_read_executor,
    artifact_read_tool,
    artifact_write_executor,
    artifact_write_tool,
)
from personal_agent.tools.context7 import (
    get_library_docs_executor,
    get_library_docs_tool,
)
from personal_agent.tools.executor import ToolExecutionError, ToolExecutionLayer
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
from personal_agent.tools.notes_tools import (
    notes_search_executor,
    notes_search_tool,
    notes_write_executor,
    notes_write_tool,
)
from personal_agent.tools.perplexity import (
    perplexity_query_executor,
    perplexity_query_tool,
)
from personal_agent.tools.personal_history import (
    recall_personal_history_executor,
    recall_personal_history_tool,
)
from personal_agent.tools.registry import ToolRegistry
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
    - recall_personal_history: Personal time-window retrieval scoped to ctx.user_id (FRE-343)
    - web_search: Private web search via SearXNG (ADR-0034)
    - perplexity_query: Perplexity AI synthesized answers (ADR-0028 Phase 2)
    - get_library_docs: Context7 library documentation (ADR-0028 Phase 3)
    - Linear tools: create/find/list (FRE-224, Tier-1)

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
    registry.register(recall_personal_history_tool, recall_personal_history_executor)
    registry.register(web_search_tool, web_search_executor)  # ADR-0034
    registry.register(perplexity_query_tool, perplexity_query_executor)
    registry.register(get_library_docs_tool, get_library_docs_executor)
    # FRE-224: native Linear tool (Tier-1, no MCP gateway required)
    registry.register(create_linear_issue_tool, create_linear_issue_executor)
    registry.register(find_linear_issues_tool, find_linear_issues_executor)
    registry.register(list_linear_projects_tool, list_linear_projects_executor)
    registry.register(create_linear_project_tool, create_linear_project_executor)

    # FRE-227 — notes tools, register only when R2 substrate is wired.
    # When unset (laptop-only dev, fresh installs before terraform applies)
    # the gateway logs "notes_tools_skipped_unconfigured" and the LLM never
    # sees these tool definitions, so it cannot try to use them.
    if settings.r2_endpoint_url and settings.r2_access_key_id and settings.r2_secret_access_key:
        registry.register(notes_write_tool, notes_write_executor)
        registry.register(notes_search_tool, notes_search_executor)
        log.info("notes_tools_registered", bucket=settings.r2_bucket_name)
        # FRE-368 — artifact tools share the same R2 substrate dependency.
        registry.register(artifact_write_tool, artifact_write_executor)
        registry.register(artifact_list_tool, artifact_list_executor)
        registry.register(artifact_read_tool, artifact_read_executor)
        # ADR-0077 — artifact_draft: plan/generate split via sub-agent.
        registry.register(artifact_draft_tool, artifact_draft_executor)
        log.info("artifact_tools_registered", bucket=settings.r2_bucket_name)
    else:
        log.warning("notes_tools_skipped_unconfigured")

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

        # Phase B skill routing: read_skill tool (always registered with primitives)
        from personal_agent.tools.read_skill import read_skill_executor, read_skill_tool  # noqa: PLC0415, I001

        registry.register(read_tool, read_executor)
        registry.register(write_tool, write_executor)
        registry.register(bash_tool, bash_executor)
        registry.register(run_python_tool, run_python_executor)
        registry.register(read_skill_tool, read_skill_executor)
        log.info("primitive_tools_registered", count=5)


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
