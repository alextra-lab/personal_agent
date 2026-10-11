# Governance

Policy enforcement layer - controls what the agent can do.

**Spec**: `../../docs/architecture_decisions/ADR-0005-governance-config-and-modes.md`

## Responsibilities

- Load and validate governance configuration (YAML)
- Provide mode definitions and thresholds
- Check tool permissions based on mode
- Enforce safety policies

## Structure

```
governance/
├── __init__.py          # Exports: PolicyConfig, check_permission
# Config loader moved to config/governance_loader.py (ADR-0007)
├── models.py            # Pydantic schemas
└── permissions.py       # Permission logic
```

## Load Configuration

```python
from personal_agent.governance import load_governance_config

config = load_governance_config("config/governance/")
```

## Check Permissions

```python
from personal_agent.governance import check_permission
from personal_agent.brainstem import get_current_mode

mode = get_current_mode()
allowed = check_permission("filesystem_write", mode)

if not allowed:
    raise PermissionDeniedError(f"Tool denied in {mode}")
```

## Pydantic Models

```python
from pydantic import BaseModel, Field

class ModeConfig(BaseModel):
    mode: OperationalMode
    cpu_threshold: float = Field(ge=0.0, le=100.0)
    max_parallel_tools: int = Field(ge=1, le=10)
    require_approval: list[str] = Field(default_factory=list)
```

## YAML Format

```yaml
# config/governance/modes.yaml
modes:
  NORMAL:
    cpu_threshold: 85.0
    max_parallel_tools: 5
    require_approval: []
  ALERT:
    cpu_threshold: 75.0
    require_approval: ["filesystem_write"]

# config/governance/tools.yaml — availability per mode
tools:
  filesystem_write:
    allowed_in_modes: ["NORMAL", "ALERT"]

# config/governance/permissions.yaml — run, ask or refuse (ADR-0156 D8, FRE-1574)
ask:
  - rule: "filesystem_write"
    modes: [ALERT]
    reason: "Writes ask outside NORMAL mode."
allow:
  - rule: "filesystem_write"
# curl has no allow rule. A curl call runs without asking only when curl_request.py reduces it
# to one request that a curl_read entry lists (FRE-1583). A bash(curl … *) allow rule is refused.
# python3 asks, except a reviewed skill script (FRE-1585): the loader accepts
# bash(python3 docs/skills/<skill>/scripts/<file>.py *) only for one regular file there.
# In /app (the image), the matcher also accepts that path with a /app/ prefix (FRE-1589).
curl_read:
  - hosts: ["tempo:3200"]
    methods: [GET]
    paths: ["/ready", "/api/traces/*"]
```

## Dependencies

- **Pydantic**: Config validation
- **PyYAML**: YAML parsing
- **brainstem**: Current mode state

## Search

```bash
rg -n "check_permission" src/
rg -n "class.*ModeConfig" src/personal_agent/governance/
rg -n "load_governance_config" src/
```

## Critical

- Validate at load time - fail fast if config invalid
- **Default to safe** - if permission check fails, deny
- **No hardcoded policies** - all from YAML
- Always check current mode before permission check

## Testing

- Test config loading (valid and invalid YAML)
- Test permission logic
- Test Pydantic validation
- Test mode transitions affect permissions

## Pre-PR

```bash
pytest tests/test_governance/ -v
mypy src/personal_agent/governance/
ruff check src/personal_agent/governance/
```
