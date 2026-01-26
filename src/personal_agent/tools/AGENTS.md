# Tools

Tool execution layer with sandboxing and governance integration.

**Spec**: `../../docs/architecture/TOOL_EXECUTION_VALIDATION_SPEC_v0.1.md`

## Responsibilities

- Tool registry (register, discover, execute)
- Governance integration (permission checks)
- Tool execution with error handling
- Telemetry for all tool calls

## Structure

```
tools/
├── __init__.py          # Exports: ToolRegistry, execute_tool
├── executor.py          # ToolExecutionLayer
├── registry.py          # Tool registration
├── filesystem.py        # File operation tools
├── web.py               # Web search tools
└── system_health.py     # System health tools
```

## Tool Registration

```python
from personal_agent.tools import ToolRegistry

registry = ToolRegistry()

@registry.register("filesystem_read")
async def filesystem_read(path: str, ctx: TraceContext) -> dict[str, Any]:
    """Read file contents."""
    return {"success": True, "output": contents, "error": None}
```

## Tool Execution

```python
from personal_agent.tools import execute_tool
from personal_agent.governance import check_permission

async def safe_execute(name: str, args: dict[str, Any], ctx: TraceContext):
    mode = get_current_mode()
    if not check_permission(name, mode):
        raise PermissionDeniedError(f"Tool {name} denied in {mode}")
    return await execute_tool(name, args, ctx)
```

## Result Format

All tools return:

```python
{
    "success": bool,      # Did tool succeed?
    "output": Any,        # Tool-specific output
    "error": str | None,  # Error message if failed
}
```

## Telemetry

```python
log.info("tool_start", tool=name, args=args, trace_id=ctx.trace_id)
try:
    result = await registry.execute(name, args)
    log.info("tool_success", tool=name, trace_id=ctx.trace_id)
except Exception as e:
    log.error("tool_failed", tool=name, error=str(e), trace_id=ctx.trace_id)
    return {"success": False, "output": None, "error": str(e)}
```

## Dependencies

- `governance`: Permission checks
- `telemetry`: Logging
- `httpx`: Async HTTP (web tools)

## Search

```bash
rg -n "@registry.register|register_tool" src/personal_agent/tools/
rg -n "execute_tool" src/
rg -n "result\[\"success\"\]" src/
```

## Critical

- **Always check permissions** - never execute without governance check
- All tools return `{success, output, error}` dict
- Tools should be async for I/O
- **Never** direct file operations - use Path, handle errors gracefully
- Pass TraceContext through execution chain

## Testing

- Mock registry
- Test permission checks
- Test error handling (tool not found, execution failed)
- Test real file operations in temp directories

## Pre-PR

```bash
pytest tests/test_tools/ -v
mypy src/personal_agent/tools/
ruff check src/personal_agent/tools/
```
