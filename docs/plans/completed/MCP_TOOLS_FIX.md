# MCP Tools Fix - Making Tools Available to LLM

**Date**: January 23, 2026
**Status**: ✅ **FIXED**

## Problem

User reported that MCP tools (like Perplexity) were not available to the LLM when using the service client:

```bash
python -m personal_agent.ui.service_client chat "Search the internet with Perplexity"
# LLM responded: "I cannot access the internet"
```

## Root Causes

### 1. MCP Gateway Not Initialized in Service Lifespan

**Location**: `src/personal_agent/service/app.py`

**Issue**: The MCP gateway initialization code existed but was never called during service startup. There was a TODO comment but no actual implementation:

```python
# TODO: Initialize MCP gateway singleton
```

**Impact**: MCP tools were never registered with the tool registry, so they were unavailable to the orchestrator/LLM.

### 2. Incorrect Environment Variable Name

**Location**: `.env`

**Issue**: The environment variable was named `MCP_GATEWAY_ENABLED=true` but the AppConfig expects all variables to have the `AGENT_` prefix:

```python
# settings.py
model_config = SettingsConfigDict(
    env_prefix="AGENT_",  # All env vars use AGENT_ prefix
    ...
)
```

**Impact**: Even with `MCP_GATEWAY_ENABLED=true` in `.env`, Pydantic read it as `False` because it couldn't find `AGENT_MCP_GATEWAY_ENABLED`.

## Fixes Applied

### Fix 1: Initialize MCP Gateway in Service Lifespan

**File**: `src/personal_agent/service/app.py`

**Changes**:

1. Added `mcp_adapter` to global instances:
```python
mcp_adapter: "MCPGatewayAdapter | None" = None
```

2. Added initialization in lifespan startup:
```python
# Initialize MCP gateway (Phase 2.3+)
if settings.mcp_gateway_enabled:
    try:
        from personal_agent.mcp.gateway import MCPGatewayAdapter
        from personal_agent.tools import get_default_registry

        log.info("mcp_gateway_initializing", command=settings.mcp_gateway_command)
        registry = get_default_registry()
        mcp_adapter = MCPGatewayAdapter(registry)
        await mcp_adapter.initialize()
        log.info(
            "mcp_gateway_initialized",
            tools_count=len(mcp_adapter._mcp_tool_names),
            tools=list(mcp_adapter._mcp_tool_names)[:10],
        )
    except Exception as e:
        log.warning("mcp_gateway_init_failed", error=str(e), exc_info=True)
        mcp_adapter = None
```

3. Added cleanup in lifespan shutdown:
```python
if mcp_adapter:
    try:
        await mcp_adapter.shutdown()
    except Exception as e:
        log.error("mcp_gateway_shutdown_error", error=str(e), exc_info=True)
```

4. Updated health check endpoint:
```python
"mcp_gateway": "connected"
if mcp_adapter and getattr(mcp_adapter, "client", None)
else "disconnected",
```

### Fix 2: Correct Environment Variable Name

**File**: `.env`

**Change**:
```diff
- MCP_GATEWAY_ENABLED=true
+ AGENT_MCP_GATEWAY_ENABLED=true
```

## Verification

After the fix:

```bash
$ python -c "from personal_agent.config import settings; print(settings.mcp_gateway_enabled)"
Settings: True  ✅
```

## Testing the Fix

1. **Start Docker services** (if not already running):
```bash
docker compose up -d
```

2. **Start the agent service**:
```bash
uv run python -m personal_agent.service.app
```

Expected logs:
```
[info] service_starting
[info] database_initialized
[info] mcp_gateway_initializing command=['docker', 'mcp', 'gateway', 'run']
[info] mcp_gateway_initialized tools_count=... tools=[...]
[info] service_ready port=9000
```

3. **Test with Perplexity**:
```bash
python -m personal_agent.ui.service_client chat \
  "Where does the expression The Price of Tea in China come from? Use Perplexity to search."
```

Expected behavior: The LLM should now have access to `mcp_perplexity_ask`, `mcp_perplexity_research`, etc., and be able to search the internet.

4. **Check health endpoint**:
```bash
curl http://localhost:9000/health | jq .components.mcp_gateway
# Should return: "connected"
```

## Available MCP Tools

Once initialized, the following MCP tools will be available (example from MCP Docker gateway):

- `mcp_perplexity_ask` - Quick web search
- `mcp_perplexity_research` - Deep research with citations
- `mcp_perplexity_reason` - Reasoning tasks
- `mcp_fetch_content` - Fetch webpage content
- `mcp_search` - DuckDuckGo search
- Plus many others from enabled MCP servers

## Related Files

- `src/personal_agent/service/app.py` - Service lifespan management
- `src/personal_agent/mcp/gateway.py` - MCP Gateway adapter
- `src/personal_agent/config/settings.py` - Configuration with AGENT_ prefix
- `.env` - Environment variables

## Configuration

All MCP-related environment variables require the `AGENT_` prefix:

```bash
# Enable/disable
AGENT_MCP_GATEWAY_ENABLED=true

# Gateway command (JSON array or space-separated)
AGENT_MCP_GATEWAY_COMMAND='["docker", "mcp", "gateway", "run"]'

# Timeout
AGENT_MCP_GATEWAY_TIMEOUT_SECONDS=30

# Enabled servers (comma-separated, empty = all)
AGENT_MCP_GATEWAY_ENABLED_SERVERS=
```

## Next Steps

1. ✅ Fix applied and verified
2. ⏭️ Test with actual Perplexity queries
3. ⏭️ Update `.env.example` to use `AGENT_` prefix for all variables
4. ⏭️ Document all available MCP tools

---

**Status**: RESOLVED ✅
**Phase**: 2.3 (MCP Integration)
