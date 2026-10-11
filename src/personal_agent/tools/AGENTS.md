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
    return {"content": contents}  # the success payload; a failure raises its typed error
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

## Result Format (ADR-0156 D6 — the one tool contract)

An executor returns its success payload and raises its typed `ToolError` subclass on a
failure. The model reads `{"ok": true, ...payload}` or
`{"ok": false, "error": {"code", "message", "retryable", "next_action"}}`, built only by
`tools/contract.py:render_content`. See `docs/reference/TOOL_INTEGRATION_GUIDE.md`
("The tool contract").

## Telemetry

`ToolExecutionLayer.execute_tool` logs `tool_call_started`, then exactly one
`tool_call_completed` with `success` equal to `ok` (and `error_code` on a failure). A tool's
own events (for example `bash_completed`) add detail; the outcome is the layer's event.

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
- Executors return the success payload and raise a typed `ToolError` on failure (never a
  returned error dict)
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
