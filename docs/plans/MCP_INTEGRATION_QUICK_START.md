# MCP Gateway Integration - Quick Start Guide

**For**: Standard Cursor model implementation
**Date**: 2026-01-17
**Prerequisites**: All validation issues resolved, detailed specs available

---

## Document Overview

This integration adds Docker MCP Gateway support to the Personal Agent, enabling access to hundreds of containerized MCP tools while maintaining governance controls.

### Related Documents (Read These First)

1. **ADR-0011-mcp-gateway-integration.md** - Architecture decision (REVISED)
2. **MCP_GATEWAY_IMPLEMENTATION_PLAN_v2.md** - Step-by-step implementation (DETAILED)
3. **MCP_GOVERNANCE_DISCOVERY_SPEC.md** - Governance discovery specification

---

## Critical Changes from Original Plan

The implementation plan has been **completely validated and revised** to fix these issues:

### ✅ Fixed: Async/Sync Mismatch
- **Issue**: Tool execution was sync, MCP requires async
- **Solution**: Migrated `ToolExecutionLayer.execute_tool()` to async
- **Impact**: ALL tool executors now support async (sync still works via thread pool)

### ✅ Fixed: Subprocess Management
- **Issue**: Manual subprocess management redundant
- **Solution**: MCP SDK context manager handles subprocess lifecycle
- **Impact**: Removed `GatewayProcess` class, simpler architecture

### ✅ Fixed: Configuration Parsing
- **Issue**: JSON list parsing from env vars not handled
- **Solution**: Added `@field_validator` for `mcp_gateway_command`
- **Impact**: `MCP_GATEWAY_COMMAND='["docker", "mcp", "gateway", "run"]'` works correctly

### ✅ Added: Governance Discovery
- **Issue**: No auto-configuration for discovered tools
- **Solution**: `MCPGovernanceManager` auto-generates `tools.yaml` entries
- **Impact**: First-run friendly, user-customizable, version-control friendly

---

## Implementation Checklist

Use this as your roadmap - each task has detailed code in the implementation plan.

### Phase 1: Core Infrastructure (Week 1, Days 1-2)

- [ ] **Task 1.1**: Add `mcp>=1.0.0` to `pyproject.toml`
- [ ] **Task 1.2**: Create `src/personal_agent/mcp/` module structure
- [ ] **Task 1.3**: Implement `MCPClientWrapper` (context manager pattern)
- [ ] **Task 1.4**: Migrate tool execution to async (CRITICAL)
  - Change `ToolExecutionLayer.execute_tool()` signature to `async def`
  - Update orchestrator call to `await execute_tool()`
  - Support both async and sync executors
- [ ] **Task 1.5**: (Optional) Convert existing tool executors to async
- [ ] **Task 1.6**: Write unit tests (`tests/test_mcp/test_client.py`)

**Acceptance**: MCP client connects, tool execution is async

### Phase 2: Gateway Adapter (Week 1, Days 3-4)

- [ ] **Task 2.1**: Implement type conversions (`mcp/types.py`)
  - `mcp_tool_to_definition()` - MCP schema → `ToolDefinition`
  - `mcp_result_to_tool_result()` - MCP result → `ToolResult`
  - `_infer_risk_level()` - Risk inference from tool name
- [ ] **Task 2.2**: Implement `MCPGatewayAdapter` (`mcp/gateway.py`)
  - Initialize client as context manager
  - Discover tools via `client.list_tools()`
  - Register tools with `ToolRegistry`
  - Create async executors for each tool
- [ ] **Task 2.3**: Write integration tests (`tests/test_mcp/test_integration.py`)

**Acceptance**: Tools discovered and registered, execute through adapter

### Phase 3: Configuration & Governance (Week 1, Day 5 - Week 2, Day 1)

- [ ] **Task 3.1**: Extend `AppConfig` with MCP fields
  - Add `mcp_gateway_enabled`, `mcp_gateway_command`, etc.
  - Add `@field_validator` for command parsing (JSON + space-separated)
- [ ] **Task 3.2**: Implement `MCPGovernanceManager` (`mcp/governance.py`)
  - Check if tool exists in `tools.yaml`
  - Generate template with inferred risk level
  - Append to config file preserving formatting
  - Idempotency: don't overwrite existing entries
- [ ] **Task 3.3**: Update `config/governance/tools.yaml`
  - Add `mcp` category to `tool_categories`
- [ ] **Task 3.4**: Write config tests (`tests/test_config/test_mcp_config.py`)

**Acceptance**: Config loaded from env vars, governance entries auto-generated

### Phase 4: Integration & Documentation (Week 2, Days 2-3)

- [ ] **Task 4.1**: Wire up orchestrator initialization
  - Add `_initialize_mcp_gateway()` in `orchestrator/executor.py`
  - Call from `cli.py` at startup
  - Add `_shutdown_mcp_gateway()` for cleanup
- [ ] **Task 4.2**: Add telemetry events (`telemetry/events.py`)
  - `MCP_GATEWAY_STARTED`, `MCP_GATEWAY_STOPPED`, etc.
- [ ] **Task 4.3**: Create documentation
  - `src/personal_agent/mcp/AGENTS.md` - MCP integration guide
  - Update `src/personal_agent/tools/AGENTS.md` - MCP section
- [ ] **Task 4.4**: Write E2E tests (`tests/test_mcp/test_e2e.py`)

**Acceptance**: Full workflow works, documentation complete

---

## Code Patterns Reference

### Pattern 1: Async Context Manager (MCP Client)

```python
class MCPClientWrapper:
    async def __aenter__(self):
        # SDK handles subprocess creation
        server_params = StdioServerParameters(command=self.command[0], args=self.command[1:])
        self._client_context = stdio_client(server_params)
        self._read_stream, self._write_stream = await self._client_context.__aenter__()

        self.session = ClientSession(self._read_stream, self._write_stream)
        await self.session.__aenter__()
        await self.session.initialize()
        return self

    async def __aexit__(self, *args):
        if self.session:
            await self.session.__aexit__(*args)
        if self._client_context:
            await self._client_context.__aexit__(*args)
```

### Pattern 2: Async Tool Execution

```python
# In ToolExecutionLayer.execute_tool()
async def execute_tool(self, tool_name: str, arguments: dict, trace_ctx: TraceContext) -> ToolResult:
    # ... permission checks ...

    # Support both async and sync executors
    import inspect
    if inspect.iscoroutinefunction(executor):
        result = await executor(**arguments)
    else:
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(None, lambda: executor(**arguments))

    # ... return ToolResult ...
```

### Pattern 3: Governance Discovery

```python
# In MCPGovernanceManager.ensure_tool_configured()
def ensure_tool_configured(self, tool_name: str, tool_schema: dict, inferred_risk_level: str):
    # Load config
    with open(self.tools_config_path) as f:
        config = yaml.safe_load(f)

    # Check if exists (idempotency)
    if tool_name in config.get("tools", {}):
        return  # Already configured, preserve user changes

    # Generate template
    template = self._generate_template(tool_name, tool_schema, inferred_risk_level)

    # Append to file
    with open(self.tools_config_path, 'a') as f:
        f.write(f"\n  # Auto-discovered: {template['_auto_discovered']}\n")
        f.write(f"  {tool_name}:\n")
        f.write(f"    category: \"{template['category']}\"\n")
        # ... rest of template ...
```

### Pattern 4: Async Executor Factory

```python
# In MCPGatewayAdapter._create_executor()
def _create_executor(self, mcp_tool_name: str):
    async def executor(**kwargs):
        if not self.client:
            raise RuntimeError("MCP gateway not connected")

        start_time = time.time()
        result = await self.client.call_tool(mcp_tool_name, kwargs)
        latency_ms = (time.time() - start_time) * 1000

        return mcp_result_to_tool_result(
            tool_name=f"mcp_{mcp_tool_name}",
            mcp_result=result,
            latency_ms=latency_ms,
            error=None
        )

    return executor
```

---

## Common Pitfalls & Solutions

### Pitfall 1: Forgetting `await`
```python
# ❌ WRONG
result = tool_layer.execute_tool(...)

# ✅ CORRECT
result = await tool_layer.execute_tool(...)
```

### Pitfall 2: Using Client Outside Context Manager
```python
# ❌ WRONG
client = MCPClientWrapper([...])
await client.list_tools()  # RuntimeError!

# ✅ CORRECT
async with MCPClientWrapper([...]) as client:
    await client.list_tools()
```

### Pitfall 3: Overwriting User Config
```python
# ❌ WRONG - Always appends, even if exists
with open(config_path, 'a') as f:
    f.write(f"  {tool_name}:\n...")

# ✅ CORRECT - Check first
if tool_name not in existing_config["tools"]:
    with open(config_path, 'a') as f:
        f.write(f"  {tool_name}:\n...")
```

### Pitfall 4: Invalid JSON in Env Var
```python
# ❌ WRONG - No validation
mcp_gateway_command = os.getenv("MCP_GATEWAY_COMMAND")

# ✅ CORRECT - Use Pydantic validator
@field_validator("mcp_gateway_command", mode="before")
def parse_gateway_command(cls, v):
    if isinstance(v, str):
        try:
            return json.loads(v)
        except:
            return v.split()
    return v
```

---

## Testing Strategy

### Run Tests Incrementally

```bash
# Phase 1: Unit tests (no Docker needed)
pytest tests/test_mcp/test_client.py -v

# Phase 2: Integration tests (mock client)
pytest tests/test_mcp/test_integration.py -v

# Phase 3: Config tests
pytest tests/test_config/test_mcp_config.py -v
pytest tests/test_mcp/test_governance.py -v

# Phase 4: E2E tests (requires Docker)
export DOCKER_AVAILABLE=1
pytest tests/test_mcp/test_e2e.py -v

# All tests
pytest tests/test_mcp/ -v
```

### Test Markers

Use pytest markers to control test execution:

```python
@pytest.mark.asyncio  # Async test
@pytest.mark.integration  # Requires external services
@pytest.mark.skipif(not os.getenv("DOCKER_AVAILABLE"), reason="Requires Docker")
```

---

## Configuration Examples

### Minimal Configuration (.env)

```bash
MCP_GATEWAY_ENABLED=true
```

### Full Configuration (.env)

```bash
# Enable MCP Gateway
MCP_GATEWAY_ENABLED=true

# Gateway command (JSON array or space-separated)
MCP_GATEWAY_COMMAND='["docker", "mcp", "gateway", "run"]'

# Timeout for MCP operations (seconds)
MCP_GATEWAY_TIMEOUT_SECONDS=30

# Enabled servers (empty = all)
MCP_GATEWAY_ENABLED_SERVERS=github,duckduckgo
```

### Governance Config (tools.yaml)

After first discovery:

```yaml
tool_categories:
  mcp:
    description: "Tools from Docker MCP Gateway"
    risk_level: "medium"

tools:
  read_file:
    category: "read_only"
    # ... existing tools ...

  # Auto-discovered MCP tools

  # Auto-discovered: 2026-01-17T12:30:45
  mcp_github_search:
    category: "mcp"
    allowed_in_modes: ["NORMAL", "DEGRADED"]
    risk_level: "low"
    requires_approval: false
    # Customize as needed:
    # forbidden_paths: []
    # timeout_seconds: 30
```

---

## Verification Steps

After each phase, verify:

### Phase 1 Verification
```bash
# Check MCP SDK installed
python -c "import mcp; print(f'MCP SDK: {mcp.__version__}')"

# Check async tool execution
python -c "from personal_agent.tools.executor import ToolExecutionLayer; import inspect; print(f'Async: {inspect.iscoroutinefunction(ToolExecutionLayer.execute_tool)}')"
```

### Phase 2 Verification
```python
# In Python REPL
from personal_agent.mcp.types import mcp_tool_to_definition
tool_def = mcp_tool_to_definition({"name": "test", "description": "Test", "inputSchema": {}})
print(tool_def.name)  # Should be "mcp_test"
```

### Phase 3 Verification
```bash
# Check config loads
python -c "from personal_agent.config import settings; print(f'Enabled: {settings.mcp_gateway_enabled}')"

# Check governance manager
python -c "from personal_agent.mcp.governance import MCPGovernanceManager; mgr = MCPGovernanceManager(); print('OK')"
```

### Phase 4 Verification
```bash
# Full system test (requires Docker)
export MCP_GATEWAY_ENABLED=true
python -m personal_agent.ui.cli

# In CLI, check tools
> list tools
# Should show mcp_* tools if gateway available
```

---

## Rollback Plan

If implementation fails, rollback is easy:

1. **Set `MCP_GATEWAY_ENABLED=false`** in `.env`
2. System continues with built-in tools only
3. No breaking changes to existing functionality

The integration is **optional by design** - graceful degradation ensures system stability.

---

## Success Criteria

Implementation is complete when:

- [ ] All unit tests pass (`pytest tests/test_mcp/`)
- [ ] MCP Gateway can be enabled via environment variable
- [ ] Tools are discovered and registered at startup
- [ ] Governance entries auto-generated in `tools.yaml`
- [ ] User can customize governance entries
- [ ] Tool execution works through `ToolExecutionLayer`
- [ ] System works without gateway (graceful degradation)
- [ ] Documentation complete
- [ ] E2E test passes (with Docker available)

---

## Implementation Time Estimate

- **Phase 1**: 1-2 days (core infrastructure, async migration)
- **Phase 2**: 1 day (gateway adapter, type conversions)
- **Phase 3**: 1 day (configuration, governance discovery)
- **Phase 4**: 1 day (integration, documentation, tests)

**Total**: 4-5 days for complete implementation

---

## Support & Troubleshooting

### Issue: "mcp module not found"
```bash
uv sync
python -c "import mcp"
```

### Issue: "Docker not available"
```bash
docker ps  # Verify Docker running
docker mcp gateway run --help  # Verify MCP Toolkit installed
```

### Issue: "Async test fails"
```bash
pip install pytest-asyncio
pytest tests/test_mcp/ -v
```

### Issue: "Governance config not updated"
```bash
# Check file permissions
ls -la config/governance/tools.yaml

# Check log output
tail -f telemetry/logs/*.log | grep mcp_governance
```

---

## Next Steps After Implementation

Once implementation is complete:

1. **Enable in production**: Set `MCP_GATEWAY_ENABLED=true`
2. **Review governance entries**: Customize auto-discovered tools
3. **Enable desired servers**: Set `MCP_GATEWAY_ENABLED_SERVERS`
4. **Monitor telemetry**: Check for errors or performance issues
5. **Iterate**: Add more MCP servers as needed

---

## References

All detailed specifications available in:
- `./MCP_GATEWAY_IMPLEMENTATION_PLAN_v2.md` (step-by-step code)
- `./MCP_GOVERNANCE_DISCOVERY_SPEC.md` (governance workflow)
- `../architecture_decisions/ADR-0011-mcp-gateway-integration.md` (architecture)

**Start implementation with Phase 1, Task 1.1** ✨
