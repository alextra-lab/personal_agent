# MCP Gateway Integration

Docker MCP Gateway integration for tool expansion.

**Spec**: `../../docs/architecture_decisions/ADR-0011-mcp-gateway-integration.md`

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

## Known Limitations

### Async/Sync Compatibility

**Important**: MCP integration requires async tool execution. The system automatically handles this:

- **MCP tools**: Always execute asynchronously (required by MCP SDK)
- **Built-in tools**: Can be sync or async - sync tools run in thread pool to avoid blocking
- **Tool execution layer**: Fully async (`await tool_layer.execute_tool(...)`)

**Impact**: All tool executors must support async execution. Existing sync executors continue to work but are wrapped in a thread pool for compatibility.

### Gateway Lifecycle

- **Subprocess management**: Handled by MCP SDK context manager
- **Cleanup**: Automatic on context exit
- **Known issue**: Some async cleanup errors may appear in logs (stdio transport bug in MCP SDK) but don't affect functionality

### Docker Dependency

MCP Gateway requires Docker Desktop with MCP Gateway feature enabled. The system gracefully degrades if Docker is unavailable:

- Gateway initialization failures are logged as warnings
- System continues with built-in tools only
- No system crash or error propagation

## Dependencies

- `personal_agent.config`: Settings access
- `personal_agent.tools`: ToolRegistry, ToolExecutionLayer
- `personal_agent.telemetry`: Structured logging
- `mcp` package: Python MCP SDK (>=1.0.0, <2.0.0)
