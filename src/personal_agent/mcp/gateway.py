"""MCP Gateway adapter for tool execution layer."""

from typing import Any

from personal_agent.config import settings
from personal_agent.mcp.client import MCPClientWrapper
from personal_agent.mcp.governance import MCPGovernanceManager
from personal_agent.mcp.types import mcp_tool_to_definition
from personal_agent.telemetry import get_logger
from personal_agent.tools.registry import ToolRegistry

log = get_logger(__name__)


class MCPGatewayAdapter:
    """Adapter that integrates MCP Gateway with tool execution layer.

    Uses a persistent MCP session for efficiency. Gateway discovery happens
    once at startup (~10-15s), then tool calls are fast.

    Responsibilities:
    - Launch MCP Gateway via client wrapper
    - Discover tools from gateway
    - Register tools with ToolRegistry
    - Integrate with governance (auto-generate config entries)
    - Route tool execution through MCP client

    Usage:
        registry = ToolRegistry()
        adapter = MCPGatewayAdapter(registry)
        await adapter.initialize()  # Discovers and registers tools
        # ... tool execution happens through registry ...
        await adapter.shutdown()
    """

    def __init__(self, registry: ToolRegistry):
        """Initialize adapter.

        Args:
            registry: Tool registry to register MCP tools with.
        """
        self.registry = registry
        self.client: MCPClientWrapper | None = None
        self.enabled = settings.mcp_gateway_enabled
        self._mcp_tool_names: set[str] = set()  # Track registered MCP tools

    async def initialize(self) -> None:
        """Initialize gateway, discover tools, and register them.

        This is called at agent startup if MCP gateway is enabled.
        If gateway fails to start, logs warning and continues (graceful degradation).
        """
        if not self.enabled:
            log.info("mcp_gateway_disabled")
            return

        try:
            log.info("mcp_gateway_initializing", command=settings.mcp_gateway_command)

            # Create and connect client (context manager handles subprocess)
            self.client = MCPClientWrapper(
                command=settings.mcp_gateway_command, timeout=settings.mcp_gateway_timeout_seconds
            )
            await self.client.__aenter__()

            # Discover and register tools
            await self._discover_and_register_tools()

            log.info(
                "mcp_gateway_initialized",
                tools_count=len(self._mcp_tool_names),
                tools=list(self._mcp_tool_names),
            )

        except Exception as e:
            log.warning(
                "mcp_gateway_init_failed", error=str(e), error_type=type(e).__name__, exc_info=True
            )
            # Graceful degradation: continue without MCP tools
            self.client = None
            self.enabled = False

    async def _discover_and_register_tools(self) -> None:
        """Discover tools from gateway and register with tool registry."""
        if not self.client:
            return

        # List tools from gateway
        mcp_tools = await self.client.list_tools()
        log.info("mcp_tools_discovered", count=len(mcp_tools))

        # Initialize governance manager
        governance_mgr = MCPGovernanceManager()

        # Register each tool
        for mcp_tool in mcp_tools:
            try:
                # Get tool name with mcp_ prefix for governance lookup
                mcp_tool_name = f"mcp_{mcp_tool.get('name', '')}"

                # Check for description override in governance config
                description_override = governance_mgr.get_description_override(mcp_tool_name)
                if description_override:
                    log.debug(
                        "mcp_tool_description_override",
                        tool=mcp_tool_name,
                        override_length=len(description_override),
                    )

                # Convert to ToolDefinition with optional description override
                tool_def = mcp_tool_to_definition(
                    mcp_tool, description_override=description_override
                )

                # Ensure governance entry exists (creates if missing)
                governance_mgr.ensure_tool_configured(
                    tool_name=tool_def.name,
                    tool_schema=mcp_tool,
                    inferred_risk_level=tool_def.risk_level,
                )

                # Create async executor for this tool
                executor = self._create_executor(mcp_tool["name"])

                # Register with tool registry
                self.registry.register(tool_def, executor)
                self._mcp_tool_names.add(tool_def.name)

                log.debug("mcp_tool_registered", tool=tool_def.name, risk_level=tool_def.risk_level)

            except Exception as e:
                log.error(
                    "mcp_tool_registration_failed",
                    tool=mcp_tool.get("name"),
                    error=str(e),
                    exc_info=True,
                )
                # Continue with other tools

    def _create_executor(self, mcp_tool_name: str):
        """Create async executor function for MCP tool.

        Args:
            mcp_tool_name: Original MCP tool name (without mcp_ prefix).

        Returns:
            Async executor function.
        """

        async def executor(**kwargs: Any) -> dict[str, Any]:
            """Execute MCP tool via persistent gateway session.

            Args:
                **kwargs: Tool arguments.

            Returns:
                Dict with tool output on success.

            Raises:
                RuntimeError: If gateway not connected or tool execution fails.
            """
            if not self.client:
                raise RuntimeError("MCP gateway not connected")

            try:
                result = await self.client.call_tool(mcp_tool_name, kwargs)
                return result if result else {}

            except Exception as e:
                log.error(
                    "mcp_tool_execution_failed", tool=mcp_tool_name, error=str(e), exc_info=True
                )
                raise RuntimeError(f"MCP tool '{mcp_tool_name}' failed: {e}") from e

        return executor

    async def shutdown(self) -> None:
        """Shutdown gateway and cleanup resources."""
        if self.client:
            try:
                log.info("mcp_gateway_shutting_down")
                await self.client.__aexit__(None, None, None)
                log.info("mcp_gateway_shutdown_complete")
            except Exception as e:
                log.error("mcp_gateway_shutdown_error", error=str(e), exc_info=True)
            finally:
                self.client = None
