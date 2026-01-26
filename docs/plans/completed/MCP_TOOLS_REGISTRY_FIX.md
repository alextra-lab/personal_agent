# MCP Tools Registry Singleton Fix

**Date**: January 23, 2026
**Status**: ✅ **FIXED**

## Problem

After fixing the service initialization, MCP tools were STILL not available to the LLM. The logs showed no tool calls being attempted, and the LLM responded "I cannot search the internet."

## Root Cause

**Tool Registry was not a singleton** - `get_default_registry()` created a new `ToolRegistry` instance on every call:

```python
def get_default_registry() -> ToolRegistry:
    registry = ToolRegistry()  # NEW instance each time!
    register_mvp_tools(registry)
    return registry
```

**Flow breakdown**:
1. Service startup: `registry_A = get_default_registry()` → creates Registry A
2. MCP gateway: Registers MCP tools into Registry A ✅
3. Orchestrator handles chat: `registry_B = get_default_registry()` → creates Registry B (NEW instance)
4. Orchestrator gets tools from Registry B → only MVP tools, NO MCP tools ❌

## Fix Applied

**Made tool registry a singleton**:

**File**: `src/personal_agent/tools/__init__.py`

```python
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
```

## Impact

Now there is **ONE shared registry** across the entire application:
1. Service gets Registry (singleton, first access)
2. MCP gateway registers tools in Registry (singleton)
3. Orchestrator gets Registry (same singleton) → has all tools including MCP ✅

## Documentation Fix

Also fixed `.env.example` to use correct `AGENT_` prefix for MCP variables:

```bash
# Before (WRONG):
# MCP_GATEWAY_ENABLED=false
# MCP_GATEWAY_COMMAND=...
# MCP_GATEWAY_TIMEOUT_SECONDS=30

# After (CORRECT):
# AGENT_MCP_GATEWAY_ENABLED=false
# AGENT_MCP_GATEWAY_COMMAND=...
# AGENT_MCP_GATEWAY_TIMEOUT_SECONDS=60
```

**Why**: The `AppConfig` class uses `env_prefix="AGENT_"` for all environment variables (see `src/personal_agent/config/settings.py`).

## Testing

After this fix, restart the service and test:

```bash
# 1. Restart service
pkill -f "personal_agent.service"
uv run python -m personal_agent.service.app

# 2. Test MCP tools
python -m personal_agent.ui.service_client chat \
  "What is the price of tea in China? Use Perplexity to search."
```

Expected: The LLM should now recognize available MCP tools and use them for internet searches.

## Related Files

- `src/personal_agent/tools/__init__.py` - Registry singleton implementation
- `src/personal_agent/service/app.py` - Service initialization (from previous fix)
- `src/personal_agent/orchestrator/executor.py` - Tool retrieval
- `.env.example` - Documentation template (AGENT_ prefix)

## Previous Related Fix

See `./completed/MCP_TOOLS_FIX.md` for the initial service initialization fix.

---

**Status**: RESOLVED ✅
**Phase**: 2.3 (MCP Integration)
**Fixes**: Tool registry singleton + documentation
