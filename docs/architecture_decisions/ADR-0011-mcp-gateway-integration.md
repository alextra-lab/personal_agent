# ADR-0011: MCP Gateway Integration for Tool Expansion

**Status**: Accepted (Validated 2026-01-17)
**Date**: 2026-01-17
**Deciders**: Architecture Team
**Related**: ADR-0008 (Tool Calling Strategy), ADR-0007 (Configuration Management)

---

## 1. Context

The Personal Agent currently implements a custom tool execution layer (`ToolRegistry`, `ToolExecutionLayer`) that provides a limited set of built-in tools (filesystem operations, system health monitoring). While this approach provides full control and governance integration, it has significant limitations:

### Current Limitations

1. **Limited tool ecosystem**: Only a handful of built-in tools (read_file, list_directory, system_metrics_snapshot)
2. **High development overhead**: Each new tool requires custom implementation, testing, and governance configuration
3. **No community tool sharing**: Cannot leverage the growing ecosystem of MCP servers (GitHub, databases, web search, etc.)
4. **Maintenance burden**: All tools must be maintained within this codebase

### The Opportunity

Docker's MCP Gateway provides:
- **Access to MCP server ecosystem**: Hundreds of pre-built MCP servers available through Docker's catalog
- **Containerized isolation**: MCP servers run in isolated Docker containers with resource limits
- **Standardized protocol**: Model Context Protocol (MCP) is an open standard for tool/data integration
- **Low integration effort**: Gateway can be launched as a subprocess with stdio transport
- **Security benefits**: Servers run in containers with configurable restrictions (network blocking, resource limits)

### Requirements

1. **Must integrate seamlessly** with existing `ToolExecutionLayer` architecture
2. **Must maintain governance controls** (permission checks, mode-based restrictions)
3. **Must preserve telemetry** (all MCP tool calls logged with trace_id)
4. **Must handle failures gracefully** (gateway unavailable, tool not found, etc.)
5. **Must be optional/opt-in** (system works without MCP gateway)
6. **Must support configuration** (gateway command, enabled servers, transport settings)

---

## 2. Decision

We implement MCP Gateway integration as a **tool provider adapter** that bridges Docker's MCP Gateway to our existing tool execution layer.

### 2.1 Architecture Pattern

```
Orchestrator
    ↓
ToolExecutionLayer
    ↓
    ├──→ Built-in Tools (existing: filesystem, system_health)
    └──→ MCP Gateway Adapter (new)
            ↓
        Docker MCP Gateway (subprocess: docker mcp gateway run)
            ↓
        MCP Servers (containerized)
```

### 2.2 Integration Strategy

**Option A: Adapter Pattern (Selected)**

Create an `MCPGatewayAdapter` that:
- Runs Docker MCP Gateway as a subprocess (stdio transport)
- Implements MCP client protocol (handshake, list tools, call tools)
- Converts MCP tools to `ToolDefinition` format for registration
- Wraps MCP tool calls to match `ToolResult` format
- Integrates with `ToolRegistry` as a secondary tool source

**Benefits**:
- Minimal changes to existing tool execution layer
- Clear separation of concerns (MCP logic isolated)
- Graceful degradation (if gateway unavailable, built-in tools still work)
- Testable in isolation

**Implementation**:
- Use Python MCP SDK (`mcp` package) for client implementation
- MCP SDK manages subprocess lifecycle via `stdio_client()` context manager
- Register MCP tools dynamically at startup (if gateway available)
- Route tool execution: if tool in MCP registry → use adapter, else use built-in
- **Migrate `ToolExecutionLayer.execute_tool()` to async** (orchestrator already async)

### 2.3 Configuration

Add MCP Gateway settings to `AppConfig` (ADR-0007):

```python
# In config/settings.py
class AppConfig(BaseSettings):
    # ... existing fields ...

    # MCP Gateway
    mcp_gateway_enabled: bool = Field(default=False)
    mcp_gateway_command: list[str] = Field(default=["docker", "mcp", "gateway", "run"])
    mcp_gateway_timeout_seconds: int = Field(default=30)
    mcp_gateway_enabled_servers: list[str] = Field(default_factory=list)
```

Environment variables:
- `MCP_GATEWAY_ENABLED=true/false`
- `MCP_GATEWAY_TIMEOUT_SECONDS=30`
- `MCP_GATEWAY_ENABLED_SERVERS=github,duckduckgo` (comma-separated)

### 2.4 Governance Integration

MCP tools are subject to the same governance rules as built-in tools:

1. **Mode-based permissions**: MCP tools checked against `governance/config/tools.yaml`
2. **Category assignment**: All MCP tools assigned to category `"mcp"` by default
3. **Risk level**: Default to `"medium"` risk (configurable per tool name pattern)
4. **Path validation**: For file-related MCP tools, use existing path allowlist/denylist logic

**Governance Config Pattern**:
```yaml
# config/governance/tools.yaml
tool_categories:
  mcp:
    description: "Tools from Docker MCP Gateway"
    risk_level: "medium"
    examples: ["mcp_github_search", "mcp_duckduckgo_search"]

tools:
  # Individual MCP tool entries (auto-discovered, user-editable)
  mcp_github_create_pull_request:
    category: "mcp"
    risk_level: "high"
    requires_approval: true
    allowed_in_modes: ["NORMAL"]
```

### 2.5 Governance Discovery Workflow

**Critical Feature**: MCP tool discovery automatically maintains governance configuration:

1. **First Discovery**: When gateway discovers new MCP tools, check if they exist in `tools.yaml`
2. **Auto-Generate Entries**: If tool not found, append template entry with safe defaults
3. **Preserve User Customizations**: Existing tool entries are never overwritten
4. **Smart Risk Inference**: Tool names analyzed for risk indicators:
   - High risk: `write`, `delete`, `execute`, `send`, `create` → `requires_approval: true`
   - Low risk: `read`, `get`, `list`, `search` → `requires_approval: false`
5. **Audit Trail**: Auto-discovered entries include discovery timestamp

**Example Discovery Flow**:

```yaml
# Before discovery
tools:
  read_file:
    category: "read_only"
    allowed_in_modes: ["NORMAL", "ALERT"]

# After discovery (auto-appended)
  # Auto-discovered: 2026-01-17T12:30:45
  # Description: Search GitHub repositories
  mcp_github_search:
    category: "mcp"
    allowed_in_modes: ["NORMAL", "DEGRADED"]
    risk_level: "low"  # Inferred from name
    requires_approval: false
    # User can uncomment to customize:
    # forbidden_paths: []
    # allowed_paths: []

# User customizes (manually edits file)
  mcp_github_search:
    category: "mcp"
    allowed_in_modes: ["NORMAL"]  # User restricted
    risk_level: "medium"  # User elevated
    requires_approval: true  # User enabled

# New tool discovered later (user's customizations preserved)
  # Auto-discovered: 2026-01-18T09:15:22
  mcp_slack_send_message:
    category: "mcp"
    allowed_in_modes: ["NORMAL", "DEGRADED"]
    risk_level: "high"  # Inferred from "send" keyword
    requires_approval: true
```

**Benefits**:
- ✅ First-run friendly: Users get working defaults immediately
- ✅ Version control friendly: Config file can be committed, reviewed in PRs
- ✅ User control: Users can customize any discovered tool
- ✅ Incremental discovery: New tools append without disrupting existing config
- ✅ Audit trail: Discovery timestamps for security review

### 2.6 Error Handling

**Gateway Unavailable**:
- Log warning at startup
- Continue with built-in tools only
- Do not fail system initialization

**Tool Not Found in Gateway**:
- Return `ToolExecutionError` (same as built-in tool not found)
- Log with trace_id for debugging

**Gateway Process Death**:
- Detect via subprocess monitoring
- Log error, attempt restart (with backoff)
- Fail tool execution gracefully with user-visible error

**Timeout Handling**:
- Use `mcp_gateway_timeout_seconds` for all MCP tool calls
- Return timeout error in `ToolResult.error`

### 2.7 Telemetry

All MCP tool calls logged with same structure as built-in tools:
- `TOOL_CALL_STARTED` (tool_name, trace_id, is_mcp=true)
- `TOOL_CALL_COMPLETED` (tool_name, success, latency_ms, trace_id)
- `TOOL_CALL_FAILED` (tool_name, error, latency_ms, trace_id)

Additional MCP-specific telemetry:
- Gateway startup/shutdown events
- Tool discovery events (tools registered from gateway)
- Gateway health check events

---

## 3. Implementation Plan

### Phase 1: Core MCP Client Infrastructure (Week 1)

1. **Add dependencies**:
   - Add `mcp>=1.0.0` to `pyproject.toml`

2. **Create MCP module structure**:
   ```
   src/personal_agent/mcp/
   ├── __init__.py
   ├── gateway.py          # MCPGatewayAdapter class
   ├── client.py           # MCPClientWrapper (uses mcp SDK context manager)
   ├── types.py            # MCP-specific types/adapters
   └── governance.py       # MCPGovernanceManager (discovery integration)
   ```

3. **Implement MCP client wrapper**:
   - Use MCP SDK `stdio_client()` context manager (handles subprocess lifecycle)
   - Implement `__aenter__`/`__aexit__` for async context manager pattern
   - Handle handshake, tool discovery, tool invocation
   - Error handling and timeout management

4. **Migrate tool execution to async**:
   - Change `ToolExecutionLayer.execute_tool()` from sync to async
   - Update `step_tool_execution()` to await tool execution
   - Update all tool executors to be async functions

5. **Write unit tests**:
   - Test client wrapper (mock MCP SDK)
   - Test gateway adapter (mock MCP client)
   - Test error scenarios (gateway unavailable, tool not found)

### Phase 2: Tool Registry Integration (Week 1-2)

1. **Extend ToolRegistry**:
   - Add `register_mcp_tool()` method
   - Add `list_mcp_tools()` method
   - Tool resolution: check MCP registry before built-in

2. **Update ToolExecutionLayer**:
   - Detect MCP tools vs built-in tools
   - Route execution to appropriate handler
   - Unified error handling and telemetry

3. **Implement tool conversion**:
   - Convert MCP tool schema to `ToolDefinition`
   - Map MCP tool results to `ToolResult`
   - Handle type conversions (MCP JSON → Python types)

4. **Write integration tests**:
   - Test tool registration from gateway
   - Test tool execution through adapter
   - Test governance checks for MCP tools

### Phase 3: Configuration & Governance (Week 2)

1. **Add configuration** (ADR-0007 pattern):
   - Extend `AppConfig` with MCP Gateway settings
   - Environment variable support with validator for list parsing
   - Default values and validation

2. **Implement governance discovery**:
   - Create `MCPGovernanceManager` class
   - Auto-generate governance entries for discovered tools
   - Preserve user customizations on subsequent discoveries
   - Smart risk inference from tool names

3. **Update governance config**:
   - Add `mcp` category to `tool_categories` in `tools.yaml`
   - Document discovery workflow and customization patterns

4. **Write configuration tests**:
   - Test config loading
   - Test gateway enabled/disabled modes
   - Test governance discovery (new tool, existing tool, user customization)

### Phase 4: Startup Integration & Documentation (Week 2-3)

1. **Orchestrator integration**:
   - Initialize MCP Gateway adapter at startup (if enabled)
   - Register MCP tools with `ToolRegistry`
   - Graceful degradation if gateway unavailable

2. **Telemetry integration**:
   - Gateway lifecycle events
   - MCP tool call logging
   - Health check metrics

3. **Documentation**:
   - Update `tools/AGENTS.md` with MCP integration
   - Create `mcp/AGENTS.md` with MCP-specific patterns
   - Update architecture docs

4. **End-to-end tests**:
   - Test full flow: gateway startup → tool discovery → tool execution
   - Test error scenarios end-to-end
   - Test governance enforcement for MCP tools

**Acceptance Criteria**:
- ✅ MCP Gateway can be enabled via configuration
- ✅ MCP tools are discoverable and registerable
- ✅ MCP tools execute through existing ToolExecutionLayer
- ✅ Governance rules apply to MCP tools
- ✅ All MCP tool calls are logged with trace_id
- ✅ System works without gateway (graceful degradation)
- ✅ Gateway failures don't crash system

---

## 4. Consequences

### Positive

✅ **Expanded tool ecosystem**: Access to hundreds of MCP servers (GitHub, databases, web search, etc.)
✅ **Reduced development overhead**: No need to implement every tool from scratch
✅ **Community leverage**: Benefit from community-maintained MCP servers
✅ **Containerized security**: MCP servers run in isolated Docker containers
✅ **Standardized protocol**: MCP is an open standard, future-proof
✅ **Incremental adoption**: Can enable gateway selectively, built-in tools remain available
✅ **Governance preserved**: MCP tools subject to same governance rules

### Negative

⚠️ **Docker dependency**: Requires Docker to be installed and running
⚠️ **Additional complexity**: MCP client protocol, subprocess management, tool conversion
⚠️ **Latency overhead**: Subprocess communication + container startup adds latency
⚠️ **Debugging difficulty**: Tool failures may occur in Docker containers (harder to debug)
⚠️ **Gateway process management**: Need to monitor and restart gateway if it crashes
⚠️ **Configuration complexity**: More configuration options to manage

### Neutral

- **Performance**: MCP tool calls slower than built-in tools (subprocess + container overhead)
- **Maintenance**: Must keep Docker MCP Gateway updated (external dependency)
- **Testing**: Requires Docker for integration tests (more complex test setup)

### Mitigations

- **Docker dependency**: Make gateway optional (system works without it)
- **Complexity**: Isolate in adapter module, well-documented
- **Latency**: Acceptable trade-off for expanded tool ecosystem
- **Debugging**: Comprehensive logging, structured error messages
- **Process management**: Automatic restart with backoff, health checks
- **Configuration**: Clear defaults, environment variable support

---

## 5. Alternatives Considered

### 5.1 Direct MCP Server Integration (Rejected)

**Approach**: Connect directly to individual MCP servers (skip Docker Gateway)

**Rejected because**:
- Requires managing multiple server processes
- No centralized security/configuration management
- Gateway provides containerization, resource limits, security controls
- Gateway simplifies server lifecycle management

### 5.2 CLI Tool Wrapper (Rejected)

**Approach**: Wrap `docker mcp gateway` CLI commands as tools (no SDK integration)

**Rejected because**:
- CLI approach loses structured tool definitions
- No tool discovery (would need to parse CLI output)
- Harder to handle errors and timeouts
- MCP SDK provides proper protocol implementation

### 5.3 Replace Tool System with MCP (Rejected)

**Approach**: Migrate entire tool system to MCP-only

**Rejected because**:
- Loses governance integration (would need to rebuild)
- Loses telemetry integration (would need to rebuild)
- Built-in tools are fast, reliable, well-tested
- Hybrid approach provides best of both worlds

### 5.4 HTTP/SSE Transport (Deferred)

**Approach**: Use HTTP/SSE transport instead of stdio

**Deferred to future**:
- Stdio is simpler for subprocess integration (no port management)
- HTTP/SSE adds complexity (port binding, network configuration)
- Can add HTTP transport later if needed (e.g., remote gateway)

---

## 6. Related ADRs

- **ADR-0007**: Unified Configuration Management (config pattern)
- **ADR-0008**: Hybrid Tool Calling Strategy (tool execution architecture)
- **ADR-0005**: Governance Configuration (tool permissions)

---

## 7. Open Questions

1. **Tool naming conflicts**: What if MCP tool name conflicts with built-in tool name? (Decision: MCP tools **always** prefixed with `mcp_`, no conflicts possible)
2. **Gateway startup time**: How long to wait for gateway startup? (Decision: Configurable timeout, default 30s)
3. **Tool schema compatibility**: What if MCP tool schema doesn't map cleanly to `ToolDefinition`? (Decision: Best-effort conversion, log warnings, handle all MCP content types)
4. **Gateway persistence**: Should gateway run for entire session or per-tool-call? (Decision: Long-lived context manager, lifecycle managed by MCP SDK)
5. **Resource limits**: Should we configure Docker resource limits for MCP servers? (Decision: Use gateway defaults initially, add config later if needed)

---

## 8. References

- [Docker MCP Gateway Documentation](https://docs.docker.com/ai/mcp-catalog-and-toolkit/mcp-gateway/)
- [MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk)
- [Model Context Protocol Specification](https://spec.modelcontextprotocol.io/)
- [Research: MCP vs CLI Tools](../research/learning-self-improvement-patterns.md)

---

**Decision Log**:
- 2026-01-17: Initial proposal
- 2026-01-17: Revised after validation - Added async tool execution, governance discovery, fixed subprocess management
