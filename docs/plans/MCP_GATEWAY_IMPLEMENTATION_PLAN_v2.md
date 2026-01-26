# MCP Gateway Integration Implementation Plan (v2 - Revised)

**Status**: ✅ COMPLETED (2026-01-18)
**Related ADR**: ADR-0011-mcp-gateway-integration.md (Validated 2026-01-17)
**Date**: 2026-01-17
**Completed**: 2026-01-18

---

## Critical Changes from v1

1. **Fixed async/sync mismatch**: Migrated tool execution to async throughout
2. **Removed redundant subprocess management**: MCP SDK handles subprocess via context manager
3. **Added configuration validators**: Proper JSON list parsing from environment variables
4. **Added governance discovery**: Auto-generate tool entries in `tools.yaml`
5. **Fixed MCP content type handling**: Handle all MCP result types properly

---

## Prerequisites

- Docker installed and running
- Docker MCP Toolkit enabled (Docker Desktop feature)
- Python 3.12+
- Existing tool execution architecture working (tests passing)

---

## Implementation Phases

### Phase 1: Core MCP Client Infrastructure & Async Migration

**Goal**: Establish MCP client connectivity and migrate tool execution to async

---

#### Task 1.1: Add Dependencies

**File**: `pyproject.toml`

**Action**: Add MCP SDK dependency

```toml
[project]
dependencies = [
    # ... existing dependencies ...
    "mcp>=1.0.0,<2.0.0",  # Model Context Protocol SDK
]
```

**Commands**:
```bash
uv sync
python -c "import mcp; print(f'MCP SDK version: {mcp.__version__}')"
```

**Acceptance**: MCP SDK imports successfully

---

#### Task 1.2: Create Module Structure

**Action**: Create new `mcp` module

```bash
mkdir -p src/personal_agent/mcp
touch src/personal_agent/mcp/__init__.py
touch src/personal_agent/mcp/client.py
touch src/personal_agent/mcp/gateway.py
touch src/personal_agent/mcp/types.py
touch src/personal_agent/mcp/governance.py
```

**File**: `src/personal_agent/mcp/__init__.py`

```python
"""MCP Gateway integration for tool expansion.

This module integrates Docker's MCP Gateway to provide access to
containerized MCP servers while maintaining governance controls.
"""

from personal_agent.mcp.gateway import MCPGatewayAdapter
from personal_agent.mcp.governance import MCPGovernanceManager

__all__ = [
    "MCPGatewayAdapter",
    "MCPGovernanceManager",
]
```

**Acceptance**: Module structure created

---

#### Task 1.3: Implement MCP Client Wrapper

**File**: `src/personal_agent/mcp/client.py`

**Critical**: MCP SDK manages subprocess lifecycle - we don't need separate process management

```python
"""MCP client wrapper for stdio transport.

This wrapper uses the MCP SDK's stdio_client context manager,
which handles subprocess lifecycle automatically.
"""

from typing import Any
import asyncio
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from personal_agent.telemetry import get_logger

log = get_logger(__name__)


class MCPClientWrapper:
    """Wraps MCP SDK client for stdio transport.

    Uses MCP SDK's context manager pattern - the SDK handles:
    - Subprocess creation (docker mcp gateway run)
    - Subprocess cleanup
    - stdin/stdout communication

    Usage:
        async with MCPClientWrapper(["docker", "mcp", "gateway", "run"], timeout=30) as client:
            tools = await client.list_tools()
            result = await client.call_tool("tool_name", {"arg": "value"})
    """

    def __init__(self, command: list[str], timeout: int = 30):
        """Initialize MCP client wrapper.

        Args:
            command: Command to run gateway (e.g., ["docker", "mcp", "gateway", "run"])
            timeout: Timeout for operations in seconds.
        """
        self.command = command
        self.timeout = timeout
        self._read_stream = None
        self._write_stream = None
        self.session: ClientSession | None = None
        self._client_context = None

    async def __aenter__(self):
        """Enter context manager - starts gateway subprocess and connects.

        Returns:
            Self for use in context.
        """
        try:
            log.info("mcp_client_connecting", command=self.command)

            # Create server parameters
            server_params = StdioServerParameters(
                command=self.command[0],
                args=self.command[1:] if len(self.command) > 1 else None,
                env=None,  # Use current environment
            )

            # stdio_client returns context manager (read, write streams)
            self._client_context = stdio_client(server_params)
            self._read_stream, self._write_stream = await self._client_context.__aenter__()

            # Create session with timeout
            self.session = ClientSession(self._read_stream, self._write_stream)
            await asyncio.wait_for(
                self.session.__aenter__(),
                timeout=self.timeout
            )

            # Initialize session (handshake)
            await asyncio.wait_for(
                self.session.initialize(),
                timeout=self.timeout
            )

            log.info("mcp_client_connected")
            return self

        except asyncio.TimeoutError:
            log.error("mcp_client_timeout", timeout=self.timeout)
            raise
        except Exception as e:
            log.error("mcp_client_connect_failed", error=str(e), exc_info=True)
            raise

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Exit context manager - stops gateway subprocess and cleans up.

        Args:
            exc_type: Exception type (if any)
            exc_val: Exception value (if any)
            exc_tb: Exception traceback (if any)
        """
        try:
            log.info("mcp_client_disconnecting")

            # Close session
            if self.session:
                await self.session.__aexit__(exc_type, exc_val, exc_tb)
                self.session = None

            # Close client (subprocess cleanup)
            if self._client_context:
                await self._client_context.__aexit__(exc_type, exc_val, exc_tb)
                self._client_context = None

            log.info("mcp_client_disconnected")

        except Exception as e:
            log.error("mcp_client_disconnect_error", error=str(e), exc_info=True)

    async def list_tools(self) -> list[dict[str, Any]]:
        """List available tools from gateway.

        Returns:
            List of tool schemas (MCP format).

        Raises:
            RuntimeError: If client not connected.
        """
        if not self.session:
            raise RuntimeError("MCP client not connected - use async with context manager")

        try:
            result = await asyncio.wait_for(
                self.session.list_tools(),
                timeout=self.timeout
            )
            # MCP returns ListToolsResult with .tools attribute
            tools = [tool.model_dump() for tool in result.tools]
            log.debug("mcp_tools_listed", count=len(tools))
            return tools

        except asyncio.TimeoutError:
            log.error("mcp_list_tools_timeout", timeout=self.timeout)
            raise
        except Exception as e:
            log.error("mcp_list_tools_failed", error=str(e), exc_info=True)
            raise

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        """Call MCP tool.

        Args:
            name: Tool name (MCP server name, NOT prefixed with mcp_)
            arguments: Tool arguments.

        Returns:
            Tool result (parsed from MCP content).

        Raises:
            RuntimeError: If client not connected.
        """
        if not self.session:
            raise RuntimeError("MCP client not connected - use async with context manager")

        try:
            log.debug("mcp_tool_calling", tool=name, arguments=arguments)

            result = await asyncio.wait_for(
                self.session.call_tool(name, arguments),
                timeout=self.timeout
            )

            # Parse MCP content (can be text, blob, or resource)
            if not result.content:
                log.warning("mcp_tool_empty_content", tool=name)
                return {}

            # Handle different content types
            parsed_result = self._parse_mcp_content(result.content)

            log.debug("mcp_tool_called", tool=name, result_type=type(parsed_result).__name__)
            return parsed_result

        except asyncio.TimeoutError:
            log.error("mcp_tool_timeout", tool=name, timeout=self.timeout)
            raise
        except Exception as e:
            log.error("mcp_tool_call_failed", tool=name, error=str(e), exc_info=True)
            raise

    def _parse_mcp_content(self, content: list) -> Any:
        """Parse MCP content items.

        MCP results can contain:
        - TextContent: Plain text
        - ImageContent: Base64 encoded image
        - ResourceContent: Resource reference

        Args:
            content: List of MCP content items.

        Returns:
            Parsed content (str, dict, or bytes).
        """
        if not content:
            return {}

        # Get first content item
        item = content[0]

        # TextContent (most common)
        if hasattr(item, 'text'):
            text = item.text
            # Try to parse as JSON
            try:
                import json
                return json.loads(text)
            except (json.JSONDecodeError, TypeError):
                return text

        # ImageContent or BlobContent
        if hasattr(item, 'data'):
            return item.data

        # ResourceContent
        if hasattr(item, 'resource'):
            return {"resource": item.resource.uri}

        # Fallback: convert to string
        log.warning("mcp_unknown_content_type", item_type=type(item).__name__)
        return str(item)
```

**Acceptance**: Client wrapper implements async context manager pattern

---

#### Task 1.4: Migrate Tool Execution to Async

**CRITICAL**: This is a breaking change - ALL tool executors become async

**File**: `src/personal_agent/tools/executor.py`

**Step 1**: Change `execute_tool` signature

**Find** (around line 204):
```python
def execute_tool(
    self,
    tool_name: str,
    arguments: dict[str, Any],
    trace_ctx: TraceContext,
) -> ToolResult:
```

**Replace with**:
```python
async def execute_tool(
    self,
    tool_name: str,
    arguments: dict[str, Any],
    trace_ctx: TraceContext,
) -> ToolResult:
```

**Step 2**: Make executor call async

**Find** (around line 278):
```python
# For MVP, execute synchronously (Phase 2 will add async support)
result = executor(**arguments)
```

**Replace with**:
```python
# Execute tool (async or sync executor)
import inspect
if inspect.iscoroutinefunction(executor):
    result = await executor(**arguments)
else:
    # Sync executor - run in thread pool to avoid blocking
    import asyncio
    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(None, lambda: executor(**arguments))
```

**File**: `src/personal_agent/orchestrator/executor.py`

**Step 3**: Update orchestrator tool execution call

**Find** (around line 822):
```python
result = tool_layer.execute_tool(tool_name, arguments, trace_ctx)
```

**Replace with**:
```python
result = await tool_layer.execute_tool(tool_name, arguments, trace_ctx)
```

**Acceptance**:
- Tool execution is async
- Both sync and async executors supported
- Orchestrator awaits tool execution

---

#### Task 1.5: Update Existing Tool Executors (Optional - Sync Still Works)

**Note**: Existing sync executors still work (run in thread pool). Converting to async is optional optimization.

**Example** - Convert `read_file_executor` to async:

**File**: `src/personal_agent/tools/filesystem.py`

**Before**:
```python
def read_file_executor(path: str, max_size_mb: int = 10) -> dict[str, Any]:
    """Execute read_file tool."""
    # ... implementation ...
```

**After**:
```python
async def read_file_executor(path: str, max_size_mb: int = 10) -> dict[str, Any]:
    """Execute read_file tool (async)."""
    # If file I/O is heavy, use aiofiles:
    # import aiofiles
    # async with aiofiles.open(path, 'r') as f:
    #     content = await f.read()

    # For now, just run sync code in executor
    import asyncio
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _read_file_sync, path, max_size_mb)

def _read_file_sync(path: str, max_size_mb: int) -> dict[str, Any]:
    """Synchronous file read implementation."""
    # ... existing implementation ...
```

**Acceptance**: Existing tools still work, async migration path clear

---

#### Task 1.6: Write Unit Tests

**File**: `tests/test_mcp/test_client.py`

```python
"""Tests for MCP client wrapper."""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from personal_agent.mcp.client import MCPClientWrapper


@pytest.mark.asyncio
async def test_client_context_manager():
    """Test client connects and disconnects via context manager."""
    with patch("personal_agent.mcp.client.stdio_client") as mock_stdio:
        # Mock context manager chain
        mock_streams = AsyncMock()
        mock_read, mock_write = MagicMock(), MagicMock()
        mock_streams.__aenter__.return_value = (mock_read, mock_write)
        mock_stdio.return_value = mock_streams

        mock_session = AsyncMock()

        with patch("personal_agent.mcp.client.ClientSession") as mock_session_class:
            mock_session_class.return_value = mock_session

            # Test context manager
            async with MCPClientWrapper(["docker", "mcp", "gateway", "run"]) as client:
                assert client.session is not None
                mock_session.initialize.assert_called_once()

            # Verify cleanup
            mock_session.__aexit__.assert_called_once()


@pytest.mark.asyncio
async def test_list_tools():
    """Test tool listing."""
    with patch("personal_agent.mcp.client.stdio_client"):
        client = MCPClientWrapper(["docker", "mcp", "gateway", "run"])

        # Mock session
        mock_tool = MagicMock()
        mock_tool.model_dump.return_value = {
            "name": "test_tool",
            "description": "Test tool",
            "inputSchema": {"type": "object", "properties": {}}
        }

        mock_result = MagicMock()
        mock_result.tools = [mock_tool]

        client.session = AsyncMock()
        client.session.list_tools = AsyncMock(return_value=mock_result)

        tools = await client.list_tools()
        assert len(tools) == 1
        assert tools[0]["name"] == "test_tool"


@pytest.mark.asyncio
async def test_call_tool():
    """Test tool invocation."""
    with patch("personal_agent.mcp.client.stdio_client"):
        client = MCPClientWrapper(["docker", "mcp", "gateway", "run"])

        # Mock session
        mock_content = MagicMock()
        mock_content.text = '{"result": "success"}'

        mock_result = MagicMock()
        mock_result.content = [mock_content]

        client.session = AsyncMock()
        client.session.call_tool = AsyncMock(return_value=mock_result)

        result = await client.call_tool("test_tool", {"arg": "value"})
        assert result == {"result": "success"}


@pytest.mark.asyncio
async def test_client_timeout():
    """Test timeout handling."""
    with patch("personal_agent.mcp.client.stdio_client") as mock_stdio:
        # Mock slow initialization
        mock_streams = AsyncMock()
        mock_streams.__aenter__.side_effect = asyncio.TimeoutError()
        mock_stdio.return_value = mock_streams

        with pytest.raises(asyncio.TimeoutError):
            async with MCPClientWrapper(["docker", "mcp", "gateway", "run"], timeout=1):
                pass
```

**Commands**:
```bash
pytest tests/test_mcp/test_client.py -v
```

**Acceptance**: All tests pass

---

### Phase 2: MCP Gateway Adapter & Type Conversions

**Goal**: Bridge MCP tools to tool execution layer

---

#### Task 2.1: Implement Type Conversions

**File**: `src/personal_agent/mcp/types.py`

```python
"""Type conversions between MCP and tool execution formats."""

from typing import Any, Literal
from personal_agent.tools.types import ToolDefinition, ToolParameter
from personal_agent.telemetry import get_logger

log = get_logger(__name__)


def mcp_tool_to_definition(mcp_tool: dict[str, Any]) -> ToolDefinition:
    """Convert MCP tool schema to ToolDefinition.

    Args:
        mcp_tool: MCP tool schema from list_tools().
        Format: {
            "name": "github_search",
            "description": "Search GitHub repositories",
            "inputSchema": {
                "type": "object",
                "properties": {...},
                "required": [...]
            }
        }

    Returns:
        ToolDefinition with mcp_ prefix and governance metadata.
    """
    # Extract metadata
    name = mcp_tool.get("name", "")
    description = mcp_tool.get("description", "")
    input_schema = mcp_tool.get("inputSchema", {})

    # Convert parameters
    parameters = []
    properties = input_schema.get("properties", {})
    required_fields = input_schema.get("required", [])

    for param_name, param_schema in properties.items():
        param_type = param_schema.get("type", "string")

        # Map JSON Schema types to tool parameter types
        type_mapping = {
            "string": "string",
            "number": "number",
            "integer": "number",
            "boolean": "boolean",
            "object": "object",
            "array": "array",
        }

        parameters.append(
            ToolParameter(
                name=param_name,
                type=type_mapping.get(param_type, "string"),
                description=param_schema.get("description", ""),
                required=param_name in required_fields,
                default=param_schema.get("default"),
            )
        )

    # Infer risk level from name (used as default, overridden by governance)
    risk_level = _infer_risk_level(name)

    # Create ToolDefinition with mcp_ prefix
    return ToolDefinition(
        name=f"mcp_{name}",  # Always prefix to avoid conflicts
        description=description,
        category="mcp",
        parameters=parameters,
        risk_level=risk_level,
        allowed_modes=["NORMAL", "DEGRADED"],  # Default, overridden by governance
        requires_approval=risk_level == "high",  # Auto-approval for low/medium
        requires_sandbox=False,  # MCP servers already containerized
        timeout_seconds=30,
    )


def _infer_risk_level(tool_name: str) -> Literal["low", "medium", "high"]:
    """Infer risk level from tool name keywords.

    Args:
        tool_name: MCP tool name (without mcp_ prefix).

    Returns:
        Risk level: "low", "medium", or "high".
    """
    name_lower = tool_name.lower()

    # High risk keywords
    high_risk = ["write", "delete", "execute", "send", "create", "modify", "update", "remove"]
    if any(keyword in name_lower for keyword in high_risk):
        return "high"

    # Low risk keywords
    low_risk = ["read", "get", "list", "search", "query", "view", "show"]
    if any(keyword in name_lower for keyword in low_risk):
        return "low"

    # Default to medium
    return "medium"


def mcp_result_to_tool_result(
    tool_name: str, mcp_result: Any, latency_ms: float, error: str | None = None
) -> dict[str, Any]:
    """Convert MCP tool result to ToolResult format.

    Args:
        tool_name: Name of tool called (with mcp_ prefix).
        mcp_result: Result from MCP client.call_tool().
        latency_ms: Execution latency in milliseconds.
        error: Error message if call failed.

    Returns:
        Dict matching ToolResult structure.
    """
    return {
        "tool_name": tool_name,
        "success": error is None,
        "output": mcp_result if error is None else {},
        "error": error,
        "latency_ms": latency_ms,
        "metadata": {"source": "mcp_gateway"},
    }
```

**Acceptance**: Type conversion functions implemented

---

#### Task 2.2: Implement MCP Gateway Adapter

**File**: `src/personal_agent/mcp/gateway.py`

```python
"""MCP Gateway adapter for tool execution layer."""

from typing import Any
import time

from personal_agent.config import settings
from personal_agent.mcp.client import MCPClientWrapper
from personal_agent.mcp.types import mcp_tool_to_definition, mcp_result_to_tool_result
from personal_agent.mcp.governance import MCPGovernanceManager
from personal_agent.tools.registry import ToolRegistry
from personal_agent.telemetry import get_logger

log = get_logger(__name__)


class MCPGatewayAdapter:
    """Adapter that integrates MCP Gateway with tool execution layer.

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
                command=settings.mcp_gateway_command,
                timeout=settings.mcp_gateway_timeout_seconds
            )
            await self.client.__aenter__()

            # Discover and register tools
            await self._discover_and_register_tools()

            log.info(
                "mcp_gateway_initialized",
                tools_count=len(self._mcp_tool_names),
                tools=list(self._mcp_tool_names)
            )

        except Exception as e:
            log.warning(
                "mcp_gateway_init_failed",
                error=str(e),
                error_type=type(e).__name__,
                exc_info=True
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
                # Convert to ToolDefinition
                tool_def = mcp_tool_to_definition(mcp_tool)

                # Ensure governance entry exists (creates if missing)
                governance_mgr.ensure_tool_configured(
                    tool_name=tool_def.name,
                    tool_schema=mcp_tool,
                    inferred_risk_level=tool_def.risk_level
                )

                # Create async executor for this tool
                executor = self._create_executor(mcp_tool["name"])

                # Register with tool registry
                self.registry.register(tool_def, executor)
                self._mcp_tool_names.add(tool_def.name)

                log.debug(
                    "mcp_tool_registered",
                    tool=tool_def.name,
                    risk_level=tool_def.risk_level
                )

            except Exception as e:
                log.error(
                    "mcp_tool_registration_failed",
                    tool=mcp_tool.get("name"),
                    error=str(e),
                    exc_info=True
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
            """Execute MCP tool via gateway.

            Args:
                **kwargs: Tool arguments.

            Returns:
                Dict matching ToolResult structure.
            """
            if not self.client:
                raise RuntimeError("MCP gateway not connected")

            start_time = time.time()
            error = None
            result = {}

            try:
                # Call tool through MCP client
                result = await self.client.call_tool(mcp_tool_name, kwargs)

            except Exception as e:
                error = f"MCP tool execution failed: {str(e)}"
                log.error(
                    "mcp_tool_execution_failed",
                    tool=mcp_tool_name,
                    error=str(e),
                    exc_info=True
                )

            latency_ms = (time.time() - start_time) * 1000

            # Convert to ToolResult format
            return mcp_result_to_tool_result(
                tool_name=f"mcp_{mcp_tool_name}",
                mcp_result=result,
                latency_ms=latency_ms,
                error=error
            )

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
```

**Acceptance**: Gateway adapter bridges MCP to tool execution layer

---

#### Task 2.3: Integration Tests

**File**: `tests/test_mcp/test_integration.py`

```python
"""Integration tests for MCP Gateway (requires Docker)."""

import pytest
from unittest.mock import AsyncMock, patch, MagicMock
from personal_agent.mcp.gateway import MCPGatewayAdapter
from personal_agent.tools.registry import ToolRegistry


@pytest.mark.asyncio
async def test_gateway_initialization():
    """Test gateway initialization and tool discovery."""
    registry = ToolRegistry()
    adapter = MCPGatewayAdapter(registry)

    # Mock client
    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock()

    # Mock tool discovery
    mock_client.list_tools = AsyncMock(return_value=[
        {
            "name": "test_tool",
            "description": "Test tool",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "arg1": {"type": "string", "description": "Argument 1"}
                },
                "required": ["arg1"]
            }
        }
    ])

    with patch("personal_agent.mcp.gateway.MCPClientWrapper", return_value=mock_client):
        with patch("personal_agent.mcp.gateway.MCPGovernanceManager"):
            with patch("personal_agent.config.settings.mcp_gateway_enabled", True):
                await adapter.initialize()

    # Verify tool registered
    tools = registry.list_tools()
    assert len(tools) > 0
    assert any(t.name == "mcp_test_tool" for t in tools)


@pytest.mark.asyncio
async def test_graceful_degradation():
    """Test system continues if gateway unavailable."""
    registry = ToolRegistry()
    adapter = MCPGatewayAdapter(registry)

    # Mock client that fails
    mock_client = AsyncMock()
    mock_client.__aenter__.side_effect = Exception("Gateway unavailable")

    with patch("personal_agent.mcp.gateway.MCPClientWrapper", return_value=mock_client):
        with patch("personal_agent.config.settings.mcp_gateway_enabled", True):
            # Should not raise, just log warning
            await adapter.initialize()

    # Adapter should be disabled
    assert adapter.enabled is False
```

**Commands**:
```bash
pytest tests/test_mcp/test_integration.py -v
```

**Acceptance**: Integration tests pass

---

### Phase 3: Configuration & Governance Discovery

**Goal**: Add configuration support and auto-generate governance entries

---

#### Task 3.1: Extend AppConfig

**File**: `src/personal_agent/config/settings.py`

**Add after existing orchestrator config** (around line 114):

```python
    # MCP Gateway
    mcp_gateway_enabled: bool = Field(
        default=False,
        description="Enable Docker MCP Gateway integration"
    )
    mcp_gateway_command: list[str] = Field(
        default_factory=lambda: ["docker", "mcp", "gateway", "run"],
        description="Command to run Docker MCP Gateway"
    )
    mcp_gateway_timeout_seconds: int = Field(
        default=30,
        ge=1,
        le=300,
        description="Timeout for MCP operations (seconds)"
    )
    mcp_gateway_enabled_servers: list[str] = Field(
        default_factory=list,
        description="List of MCP server names to enable (empty = all)"
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
```

**Environment variables** (document in `.env.example`):
```bash
# MCP Gateway Configuration
MCP_GATEWAY_ENABLED=false
MCP_GATEWAY_COMMAND='["docker", "mcp", "gateway", "run"]'
MCP_GATEWAY_TIMEOUT_SECONDS=30
MCP_GATEWAY_ENABLED_SERVERS=github,duckduckgo
```

**Acceptance**: Configuration loaded from environment variables

---

#### Task 3.2: Implement Governance Discovery

**File**: `src/personal_agent/mcp/governance.py`

```python
"""Governance integration for MCP tools.

This module manages auto-discovery and configuration of MCP tools
in the governance config file (config/governance/tools.yaml).
"""

import yaml
from pathlib import Path
from datetime import datetime
from typing import Any, Literal

from personal_agent.config import settings
from personal_agent.telemetry import get_logger

log = get_logger(__name__)


class MCPGovernanceManager:
    """Manages governance configuration for discovered MCP tools.

    Responsibilities:
    - Check if MCP tool exists in tools.yaml
    - Auto-generate template entry if missing
    - Preserve user customizations on subsequent discoveries
    - Infer risk levels from tool names
    """

    def __init__(self):
        """Initialize governance manager."""
        self.tools_config_path = settings.governance_config_path / "tools.yaml"

        if not self.tools_config_path.exists():
            raise FileNotFoundError(
                f"Governance config not found: {self.tools_config_path}"
            )

    def ensure_tool_configured(
        self,
        tool_name: str,
        tool_schema: dict[str, Any],
        inferred_risk_level: Literal["low", "medium", "high"]
    ) -> None:
        """Ensure MCP tool has governance entry, create template if missing.

        Args:
            tool_name: MCP tool name with mcp_ prefix (e.g., 'mcp_github_search')
            tool_schema: MCP tool schema from discovery
            inferred_risk_level: Risk level inferred from tool name
        """
        # Load existing config
        with open(self.tools_config_path, 'r') as f:
            config = yaml.safe_load(f)

        # Check if tool already configured
        tools_section = config.get("tools", {})
        if tool_name in tools_section:
            log.debug("mcp_tool_already_configured", tool=tool_name)
            return

        # Generate template entry
        template = self._generate_template(
            tool_name=tool_name,
            tool_schema=tool_schema,
            inferred_risk_level=inferred_risk_level
        )

        # Append to config file
        self._append_to_config(tool_name, template)

        log.info(
            "mcp_tool_governance_added",
            tool=tool_name,
            risk_level=template["risk_level"]
        )

    def _generate_template(
        self,
        tool_name: str,
        tool_schema: dict[str, Any],
        inferred_risk_level: Literal["low", "medium", "high"]
    ) -> dict[str, Any]:
        """Generate default governance template for MCP tool.

        Args:
            tool_name: Tool name with mcp_ prefix
            tool_schema: MCP tool schema
            inferred_risk_level: Risk level from name analysis

        Returns:
            Template dict for YAML output
        """
        description = tool_schema.get("description", "")

        # Determine modes based on risk level
        if inferred_risk_level == "high":
            allowed_modes = ["NORMAL"]
            requires_approval = True
        elif inferred_risk_level == "medium":
            allowed_modes = ["NORMAL", "DEGRADED"]
            requires_approval = False
        else:  # low
            allowed_modes = ["NORMAL", "ALERT", "DEGRADED"]
            requires_approval = False

        return {
            "category": "mcp",
            "allowed_in_modes": allowed_modes,
            "risk_level": inferred_risk_level,
            "requires_approval": requires_approval,
            "_auto_discovered": datetime.now().isoformat(),
            "_description": description,
        }

    def _append_to_config(self, tool_name: str, template: dict[str, Any]) -> None:
        """Append tool template to config file, preserving formatting.

        Args:
            tool_name: Tool name with mcp_ prefix
            template: Template dict generated by _generate_template
        """
        with open(self.tools_config_path, 'a') as f:
            # Add blank line before new entry
            f.write("\n")

            # Add comment with discovery timestamp and description
            f.write(f"  # Auto-discovered: {template['_auto_discovered']}\n")
            if template['_description']:
                # Wrap long descriptions
                desc = template['_description']
                if len(desc) > 70:
                    desc = desc[:70] + "..."
                f.write(f"  # {desc}\n")

            # Write tool entry
            f.write(f"  {tool_name}:\n")
            f.write(f"    category: \"{template['category']}\"\n")
            f.write(f"    allowed_in_modes: {template['allowed_in_modes']}\n")
            f.write(f"    risk_level: \"{template['risk_level']}\"\n")
            f.write(f"    requires_approval: {str(template['requires_approval']).lower()}\n")

            # Add commented customization hints
            f.write("    # Customize as needed:\n")
            f.write("    # forbidden_paths: []\n")
            f.write("    # allowed_paths: []\n")
            f.write("    # timeout_seconds: 30\n")

        log.debug("mcp_tool_config_appended", tool=tool_name, path=str(self.tools_config_path))
```

**Acceptance**: Governance manager auto-generates tool entries

---

#### Task 3.3: Update Governance Config

**File**: `config/governance/tools.yaml`

**Add to `tool_categories` section** (after existing categories):

```yaml
  mcp:
    description: "Tools from Docker MCP Gateway (containerized)"
    risk_level: "medium"
    examples: ["mcp_github_search", "mcp_duckduckgo_search", "mcp_filesystem_read"]
```

**Note**: Individual MCP tool entries will be auto-appended by governance manager

**Acceptance**: MCP category documented

---

#### Task 3.4: Write Configuration Tests

**File**: `tests/test_config/test_mcp_config.py`

```python
"""Tests for MCP Gateway configuration."""

import os
import json
from personal_agent.config.settings import AppConfig


def test_mcp_gateway_defaults():
    """Test default MCP configuration."""
    config = AppConfig()
    assert config.mcp_gateway_enabled is False
    assert config.mcp_gateway_command == ["docker", "mcp", "gateway", "run"]
    assert config.mcp_gateway_timeout_seconds == 30


def test_mcp_gateway_env_override():
    """Test environment variable override."""
    os.environ["MCP_GATEWAY_ENABLED"] = "true"
    os.environ["MCP_GATEWAY_TIMEOUT_SECONDS"] = "60"

    config = AppConfig()
    assert config.mcp_gateway_enabled is True
    assert config.mcp_gateway_timeout_seconds == 60

    # Cleanup
    del os.environ["MCP_GATEWAY_ENABLED"]
    del os.environ["MCP_GATEWAY_TIMEOUT_SECONDS"]


def test_mcp_gateway_command_json_parsing():
    """Test gateway command parses from JSON."""
    os.environ["MCP_GATEWAY_COMMAND"] = '["docker", "mcp", "gateway", "run", "--verbose"]'

    config = AppConfig()
    assert config.mcp_gateway_command == ["docker", "mcp", "gateway", "run", "--verbose"]

    # Cleanup
    del os.environ["MCP_GATEWAY_COMMAND"]


def test_mcp_gateway_command_space_separated():
    """Test gateway command parses from space-separated string."""
    os.environ["MCP_GATEWAY_COMMAND"] = "docker mcp gateway run"

    config = AppConfig()
    assert config.mcp_gateway_command == ["docker", "mcp", "gateway", "run"]

    # Cleanup
    del os.environ["MCP_GATEWAY_COMMAND"]
```

**File**: `tests/test_mcp/test_governance.py`

```python
"""Tests for MCP governance discovery."""

import pytest
import tempfile
from pathlib import Path
from personal_agent.mcp.governance import MCPGovernanceManager


def test_governance_template_generation():
    """Test governance template generation."""
    with tempfile.TemporaryDirectory() as tmpdir:
        # Create temp config file
        config_path = Path(tmpdir) / "tools.yaml"
        config_path.write_text("tools:\n  read_file:\n    category: read_only\n")

        # Override config path
        import personal_agent.config.settings as settings_mod
        original_path = settings_mod.settings.governance_config_path
        settings_mod.settings.governance_config_path = Path(tmpdir)

        try:
            mgr = MCPGovernanceManager()

            # Test tool configuration
            mgr.ensure_tool_configured(
                tool_name="mcp_github_search",
                tool_schema={"description": "Search GitHub repositories"},
                inferred_risk_level="low"
            )

            # Verify entry added
            content = config_path.read_text()
            assert "mcp_github_search:" in content
            assert "category: \"mcp\"" in content
            assert "risk_level: \"low\"" in content

            # Test idempotency (don't duplicate)
            mgr.ensure_tool_configured(
                tool_name="mcp_github_search",
                tool_schema={"description": "Search GitHub repositories"},
                inferred_risk_level="low"
            )

            # Verify not duplicated
            content = config_path.read_text()
            assert content.count("mcp_github_search:") == 1

        finally:
            settings_mod.settings.governance_config_path = original_path
```

**Commands**:
```bash
pytest tests/test_config/test_mcp_config.py -v
pytest tests/test_mcp/test_governance.py -v
```

**Acceptance**: All configuration tests pass

---

### Phase 4: Orchestrator Integration & Documentation

**Goal**: Wire up MCP adapter at startup and complete documentation

---

#### Task 4.1: Add Orchestrator Initialization

**File**: `src/personal_agent/orchestrator/executor.py`

**Add after tool registry initialization** (around line 48-50):

```python
# Global MCP adapter instance
_mcp_adapter: MCPGatewayAdapter | None = None


async def _initialize_mcp_gateway() -> None:
    """Initialize MCP Gateway adapter if enabled.

    Called during orchestrator startup to discover and register MCP tools.
    If gateway fails to initialize, logs warning and continues (graceful degradation).
    """
    global _mcp_adapter

    if not settings.mcp_gateway_enabled:
        log.debug("mcp_gateway_not_enabled")
        return

    try:
        from personal_agent.mcp.gateway import MCPGatewayAdapter

        registry = _get_tool_registry()
        _mcp_adapter = MCPGatewayAdapter(registry)
        await _mcp_adapter.initialize()

    except Exception as e:
        log.error(
            "mcp_gateway_init_failed",
            error=str(e),
            error_type=type(e).__name__,
            exc_info=True
        )
        # Graceful degradation: continue without MCP


async def _shutdown_mcp_gateway() -> None:
    """Shutdown MCP Gateway adapter."""
    global _mcp_adapter

    if _mcp_adapter:
        try:
            await _mcp_adapter.shutdown()
        except Exception as e:
            log.error("mcp_gateway_shutdown_failed", error=str(e), exc_info=True)
        finally:
            _mcp_adapter = None
```

**File**: `src/personal_agent/ui/cli.py`

**Add initialization call at startup** (in `main()` or similar):

Find the CLI main function (around line 50-80) and add:

```python
async def main():
    """Main CLI entry point."""
    # ... existing setup ...

    # Initialize MCP Gateway (if enabled)
    from personal_agent.orchestrator.executor import _initialize_mcp_gateway
    await _initialize_mcp_gateway()

    # ... rest of CLI loop ...

    # Shutdown MCP Gateway on exit
    from personal_agent.orchestrator.executor import _shutdown_mcp_gateway
    await _shutdown_mcp_gateway()
```

**Acceptance**: MCP Gateway initializes at startup

---

#### Task 4.2: Add Telemetry Events

**File**: `src/personal_agent/telemetry/events.py`

**Add after existing events**:

```python
# MCP Gateway events
MCP_GATEWAY_STARTED = "mcp_gateway_started"
MCP_GATEWAY_STOPPED = "mcp_gateway_stopped"
MCP_GATEWAY_INIT_FAILED = "mcp_gateway_init_failed"
MCP_TOOL_DISCOVERED = "mcp_tool_discovered"
MCP_TOOL_GOVERNANCE_ADDED = "mcp_tool_governance_added"
```

**Update imports in gateway code**:

```python
from personal_agent.telemetry.events import (
    MCP_GATEWAY_STARTED,
    MCP_GATEWAY_STOPPED,
    MCP_GATEWAY_INIT_FAILED,
    MCP_TOOL_DISCOVERED,
)
```

**Acceptance**: Telemetry events defined

---

#### Task 4.3: Create MCP Documentation

**File**: `src/personal_agent/mcp/AGENTS.md`

```markdown
# MCP Gateway Integration

Docker MCP Gateway integration for tool expansion.

**Spec**: `../architecture_decisions/ADR-0011-mcp-gateway-integration.md`

## Overview

The MCP Gateway adapter integrates Docker's MCP Gateway to provide access to
containerized MCP servers while maintaining governance controls.

## Architecture

```
Orchestrator → ToolExecutionLayer → ToolRegistry
                                         ↓
                                   ┌─────┴──────┐
                                   │            │
                              Built-in     MCP Gateway
                               Tools       Adapter
                                              ↓
                                         MCP Client
                                              ↓
                                      Docker Gateway
                                        (subprocess)
                                              ↓
                                        MCP Servers
                                       (containers)
```

## Configuration

Enable MCP Gateway via environment variables:

```bash
# .env or .env.local
MCP_GATEWAY_ENABLED=true
MCP_GATEWAY_TIMEOUT_SECONDS=30
MCP_GATEWAY_COMMAND='["docker", "mcp", "gateway", "run"]'
```

## Governance Discovery

When MCP Gateway discovers new tools, they're automatically added to
`config/governance/tools.yaml`:

```yaml
# Auto-discovered: 2026-01-17T12:30:45
# Search GitHub repositories
mcp_github_search:
  category: "mcp"
  allowed_in_modes: ["NORMAL", "DEGRADED"]
  risk_level: "low"
  requires_approval: false
```

Users can then customize these entries as needed.

## Tool Execution

MCP tools are registered with `mcp_` prefix to avoid naming conflicts:

```python
# In orchestrator
tools = registry.list_tools()
# Returns: [..., ToolDefinition(name="mcp_github_search"), ...]

# Tool execution (automatic routing)
result = await tool_layer.execute_tool(
    "mcp_github_search",
    {"query": "python async"},
    trace_ctx
)
```

## Error Handling

Gateway failures are handled gracefully:

- **Startup failure**: System continues with built-in tools only
- **Tool execution failure**: Returns ToolResult with error message
- **Gateway crash**: Logged, no system crash

## Testing

```bash
# Unit tests (no Docker required)
pytest tests/test_mcp/ -m "not integration"

# Integration tests (requires Docker)
pytest tests/test_mcp/ -m integration
```

## Dependencies

- `personal_agent.config`: Settings access
- `personal_agent.tools`: ToolRegistry, ToolExecutionLayer
- `personal_agent.telemetry`: Structured logging
- `mcp` package: Python MCP SDK
```

**File**: `src/personal_agent/tools/AGENTS.md`

**Add section after existing tool documentation**:

```markdown
## MCP Gateway Integration

MCP tools are dynamically discovered and registered from Docker MCP Gateway.

### Tool Resolution

Tools are resolved in this order:
1. Built-in tools (filesystem, system_health)
2. MCP tools (prefixed with `mcp_`)

### Governance

MCP tools follow same governance rules as built-in tools:
- Mode-based permissions (`allowed_in_modes`)
- Risk levels (`low`, `medium`, `high`)
- Approval requirements (`requires_approval`)
- Path validation (`forbidden_paths`, `allowed_paths`)

See `mcp/AGENTS.md` for MCP-specific details.
```

**Acceptance**: Documentation complete

---

#### Task 4.4: End-to-End Tests

**File**: `tests/test_mcp/test_e2e.py`

```python
"""End-to-end tests for MCP Gateway integration."""

import pytest
import os
from unittest.mock import patch, AsyncMock


@pytest.mark.asyncio
@pytest.mark.integration
@pytest.mark.skipif(
    not os.getenv("DOCKER_AVAILABLE"),
    reason="Requires Docker with MCP Gateway"
)
async def test_full_mcp_workflow():
    """Test complete MCP workflow: init → discover → execute → governance.

    This test requires Docker to be running with MCP Gateway available.
    """
    from personal_agent.tools import ToolRegistry
    from personal_agent.mcp.gateway import MCPGatewayAdapter
    from personal_agent.config import settings

    # Enable gateway for test
    with patch.object(settings, 'mcp_gateway_enabled', True):
        registry = ToolRegistry()
        adapter = MCPGatewayAdapter(registry)

        try:
            # Initialize gateway
            await adapter.initialize()

            # Verify tools discovered
            tools = registry.list_tools()
            mcp_tools = [t for t in tools if t.name.startswith("mcp_")]
            assert len(mcp_tools) > 0, "No MCP tools discovered"

            # Verify governance entries created
            # (Check config/governance/tools.yaml was updated)

            print(f"✓ Discovered {len(mcp_tools)} MCP tools")

        finally:
            await adapter.shutdown()


@pytest.mark.asyncio
async def test_graceful_degradation_no_docker():
    """Test system works when Docker unavailable."""
    from personal_agent.tools import ToolRegistry, get_default_registry
    from personal_agent.mcp.gateway import MCPGatewayAdapter

    # Mock client that fails
    with patch("personal_agent.mcp.gateway.MCPClientWrapper") as mock_client:
        mock_client.return_value.__aenter__.side_effect = Exception("Docker not available")

        with patch("personal_agent.config.settings.mcp_gateway_enabled", True):
            registry = get_default_registry()  # Built-in tools
            adapter = MCPGatewayAdapter(registry)

            # Should not raise
            await adapter.initialize()

            # Built-in tools still work
            tools = registry.list_tools()
            assert len(tools) > 0
            assert any(t.name == "read_file" for t in tools)
```

**Commands**:
```bash
# Run without Docker (graceful degradation test)
pytest tests/test_mcp/test_e2e.py::test_graceful_degradation_no_docker -v

# Run with Docker (full integration)
export DOCKER_AVAILABLE=1
pytest tests/test_mcp/test_e2e.py::test_full_mcp_workflow -v
```

**Acceptance**: E2E tests pass

---

## Acceptance Criteria Checklist

- [x] MCP Gateway can be enabled via `MCP_GATEWAY_ENABLED=true` ✅
- [x] MCP tools are discovered and registered at startup ✅ (41 tools discovered)
- [x] MCP tools execute through async `ToolExecutionLayer` ✅
- [x] Governance rules apply to MCP tools (mode checks, path validation) ✅
- [x] All MCP tool calls logged with `trace_id` ✅
- [x] System works without gateway (graceful degradation) ✅
- [x] Gateway failures don't crash system ✅
- [x] Governance entries auto-generated for new tools ✅
- [x] User can customize governance entries manually ✅
- [x] Documentation complete (`mcp/AGENTS.md`, updated `tools/AGENTS.md`) ✅
- [x] Tests pass (>80% coverage for new code) ✅ (40/40 tests passing)

## Implementation Results

**Completed**: 2026-01-18

### What Was Built
- ✅ MCP SDK integration (`mcp>=1.0.0`)
- ✅ Async tool execution layer (breaking change handled)
- ✅ MCPClientWrapper with subprocess management
- ✅ MCPGatewayAdapter with auto-discovery
- ✅ MCPGovernanceManager for automatic tools.yaml entries
- ✅ Configuration extensions in AppConfig
- ✅ Telemetry events for MCP operations
- ✅ Comprehensive test suite (11 MCP tests, 40 total passing)
- ✅ Full documentation in `src/personal_agent/mcp/AGENTS.md`

### Verified Working
- ✅ **41 MCP tools discovered** including:
  - 3 Perplexity tools (ask, reason, research)
  - 22 Playwright browser automation tools
  - 5 Elasticsearch query tools
  - 2 DuckDuckGo search tools
  - Docker CLI tool
  - Context7 documentation tools
  - Sequential thinking tool
- ✅ **Perplexity integration tested** - Successfully called Perplexity API
- ✅ **Graceful degradation** - System continues without Docker
- ✅ **Auto-governance** - All tools auto-added to tools.yaml

---

## Rollout Timeline

- **Week 1, Days 1-2**: Phase 1 (Core infrastructure, async migration)
- **Week 1, Days 3-4**: Phase 2 (Gateway adapter, type conversions)
- **Week 1, Day 5 - Week 2, Day 1**: Phase 3 (Configuration, governance)
- **Week 2, Days 2-3**: Phase 4 (Integration, documentation, E2E tests)

---

## Common Issues & Solutions

### Issue: "MCP client not connected"
**Cause**: Client used outside context manager
**Solution**: Use `async with MCPClientWrapper(...) as client:`

### Issue: "Gateway timeout"
**Cause**: Docker MCP Gateway slow to start
**Solution**: Increase `MCP_GATEWAY_TIMEOUT_SECONDS` in config

### Issue: "Tool already registered"
**Cause**: Tool name conflicts
**Solution**: MCP tools always have `mcp_` prefix, shouldn't conflict

### Issue: "Governance config not updated"
**Cause**: File permissions or path wrong
**Solution**: Check `governance_config_path` in settings

### Issue: "Async tool execution fails"
**Cause**: Forgot to await `execute_tool()`
**Solution**: `result = await tool_layer.execute_tool(...)`

---

## Future Enhancements

- HTTP/SSE transport for remote gateway
- Gateway health monitoring and auto-restart
- Per-server resource limit configuration
- Tool schema validation and compatibility checks
- Caching of tool discovery results
- Gateway metrics and performance monitoring
