"""MCP client wrapper for stdio transport.

This wrapper uses the MCP SDK's stdio_client context manager,
which handles subprocess lifecycle automatically.
"""

import asyncio
from typing import Any

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
        async with MCPClientWrapper(["docker", "mcp", "gateway", "run"], timeout=60) as client:
            tools = await client.list_tools()
            result = await client.call_tool("tool_name", {"arg": "value"})
    """

    def __init__(self, command: list[str], timeout: int = 60):
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
            await asyncio.wait_for(self.session.__aenter__(), timeout=self.timeout)

            # Initialize session (handshake)
            await asyncio.wait_for(self.session.initialize(), timeout=self.timeout)

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

            # Close session - use None for clean exit even if there was an exception
            # This avoids anyio cancel scope issues when exiting after timeout
            if self.session:
                try:
                    await self.session.__aexit__(None, None, None)
                except RuntimeError as e:
                    # Ignore anyio cancel scope errors during cleanup
                    if "cancel scope" in str(e):
                        log.debug("mcp_session_cleanup_cancel_scope_ignored", error=str(e))
                    else:
                        raise
                finally:
                    self.session = None

            # Close client (subprocess cleanup)
            if self._client_context:
                try:
                    await self._client_context.__aexit__(None, None, None)
                except RuntimeError as e:
                    # Ignore anyio cancel scope errors during cleanup
                    if "cancel scope" in str(e):
                        log.debug("mcp_client_cleanup_cancel_scope_ignored", error=str(e))
                    else:
                        raise
                finally:
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
            result = await asyncio.wait_for(self.session.list_tools(), timeout=self.timeout)
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
            RuntimeError: If client not connected or tool execution fails.
        """
        if not self.session:
            raise RuntimeError("MCP client not connected - use async with context manager")

        try:
            log.info("mcp_tool_calling", tool=name, arguments=arguments)

            # Call without explicit timeout - let the SDK/gateway handle it
            # asyncio.wait_for causes anyio cancel scope conflicts with the MCP SDK
            result = await self.session.call_tool(name, arguments)

            log.info("mcp_tool_response_received", tool=name)

            # Check if tool returned an error
            if result.isError:
                error_msg = self._extract_error_message(result)
                log.error("mcp_tool_returned_error", tool=name, error=error_msg)
                raise RuntimeError(f"MCP tool '{name}' returned error: {error_msg}")

            # Check structuredContent first (some tools use this)
            if result.structuredContent:
                log.debug("mcp_tool_structured_content", tool=name)
                return result.structuredContent

            # Parse MCP content (can be text, blob, or resource)
            if not result.content:
                log.warning("mcp_tool_empty_content", tool=name)
                return {}

            # Handle different content types
            parsed_result = self._parse_mcp_content(result.content)

            log.debug("mcp_tool_called", tool=name, result_type=type(parsed_result).__name__)
            return parsed_result

        except RuntimeError:
            # Re-raise RuntimeError (tool errors) as-is
            raise
        except Exception as e:
            log.error("mcp_tool_call_failed", tool=name, error=str(e), exc_info=True)
            raise

    def _extract_error_message(self, result) -> str:
        """Extract error message from CallToolResult.

        Args:
            result: CallToolResult with isError=True.

        Returns:
            Error message string.
        """
        # Try to get error from content
        if result.content:
            for item in result.content:
                if hasattr(item, "text"):
                    return item.text
        # Fallback
        return "Unknown tool error"

    def _parse_mcp_content(self, content: list) -> Any:
        """Parse MCP content items.

        MCP results can contain multiple content types:
        - TextContent: Plain text (may be JSON)
        - ImageContent: Base64 encoded image
        - AudioContent: Audio data
        - ResourceLink: Link to a resource
        - EmbeddedResource: Embedded resource data

        Args:
            content: List of MCP content items.

        Returns:
            Parsed content. If single text item, returns parsed JSON or string.
            If multiple items, returns list of parsed items.
        """
        import json

        if not content:
            return {}

        parsed_items = []
        for item in content:
            # TextContent (most common)
            if hasattr(item, "text"):
                text = item.text
                # Try to parse as JSON
                try:
                    parsed_items.append(json.loads(text))
                except (json.JSONDecodeError, TypeError):
                    parsed_items.append(text)

            # ImageContent or AudioContent (has data attribute)
            elif hasattr(item, "data"):
                parsed_items.append({"type": "binary", "data": item.data})

            # ResourceLink (has uri)
            elif hasattr(item, "uri"):
                parsed_items.append({"type": "resource_link", "uri": item.uri})

            # EmbeddedResource (has resource)
            elif hasattr(item, "resource"):
                resource = item.resource
                parsed_items.append(
                    {
                        "type": "embedded_resource",
                        "uri": getattr(resource, "uri", None),
                        "text": getattr(resource, "text", None),
                        "blob": getattr(resource, "blob", None),
                    }
                )

            else:
                log.warning("mcp_unknown_content_type", item_type=type(item).__name__)
                parsed_items.append(str(item))

        # Return single item directly, list if multiple
        if len(parsed_items) == 1:
            return parsed_items[0]
        return parsed_items
