# Orchestrator

Deterministic state machine for task execution.

**Spec**: `../../docs/architecture/ORCHESTRATOR_CORE_SPEC_v0.1.md`
**ADR**: `../../docs/architecture_decisions/ADR-0006-orchestrator-runtime-structure.md`

## Responsibilities

- Execute task graph (channels → state transitions)
- Invoke cognitive modules (planner, critic, executor)
- Maintain session state and history
- Coordinate with governance for permissions
- Emit telemetry for all transitions

## Structure

```
orchestrator/
├── __init__.py      # Exports: Orchestrator, TaskGraph, State
├── executor.py      # Main execution loop
├── channels.py      # Channel definitions
├── session.py       # Session management
└── types.py         # Pydantic models
```

## Constraints

- **Deterministic**: Same inputs → same state transitions
- **Observable**: Every transition logged with `trace_id`
- **Mode-aware**: Check mode before high-risk operations
- **Stateless nodes**: Nodes read state, return updates, don't mutate

## Node Pattern

```python
async def planner_node(state: State, config: Config) -> StateUpdate:
    """Generate plan from current state."""
    plan = await llm_client.generate_plan(state.task_description)
    return StateUpdate(plan=plan, next_channel="critic")
```

**Never** mutate state in-place. Return StateUpdate.

## Graph Definition

```python
GRAPH = TaskGraph(
    channels=["planner", "critic", "executor", "END"],
    edges=[
        ("planner", "critic"),
        ("critic", "executor"),
        ("executor", "END"),
    ],
)
```

## Telemetry

Log all transitions:

```python
log.info(
    "orchestrator_step_start",
    step=state.current_step,
    channel=state.current_channel,
    trace_id=ctx.trace_id,
)
```

## Error Handling

Catch, log, transition to error channel:

```python
try:
    result = await node_function(state, config)
except OrchestratorError as e:
    log.error("node_failed", node=node_name, error=str(e), trace_id=ctx.trace_id)
    return StateUpdate(next_channel="error_handler", error=str(e))
```

## Mode Checks

```python
from personal_agent.brainstem import get_current_mode
from personal_agent.governance import check_permission

current_mode = get_current_mode()
if not check_permission(state.tool_name, current_mode):
    log.warning("tool_denied", tool=state.tool_name, mode=current_mode)
    return StateUpdate(next_channel="approval_required")
```

## Dependencies

- `governance`: Mode checks, tool permissions
- `telemetry`: Logging and tracing
- `llm_client`: LLM calls in cognitive nodes
- `tools`: Tool execution

## Search

```bash
rg -n "async def \w+_node\(" src/personal_agent/orchestrator/  # Node functions
rg -n "next_channel" src/personal_agent/orchestrator/         # State transitions
rg -n "OrchestratorError" src/personal_agent/orchestrator/    # Error handling
```

## Critical

- Nodes must return StateUpdate, **never** mutate State
- Always pass TraceContext through call chain
- **Never** let exceptions crash the graph - transition to error channel
- Check mode **before** executing tools, not after

## Testing

- Unit: Mock LLM responses, test state transitions independently
- Integration: Full graph with recorded LLM responses
- Property: State machine invariants (no invalid transitions)

## Before Implementing

1. Read `../../docs/architecture/ORCHESTRATOR_CORE_SPEC_v0.1.md` in full
2. Understand state machine design (Section 3)
3. Review node signature pattern (Section 4.2)
4. Check mode integration (Section 7)

## Pre-PR

```bash
pytest tests/test_orchestrator/ -v
mypy src/personal_agent/orchestrator/
ruff check src/personal_agent/orchestrator/
```
