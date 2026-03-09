# MCP Governance Discovery Specification

**Status**: Accepted
**Related ADR**: ADR-0011-mcp-gateway-integration.md
**Date**: 2026-01-17
**Owner**: Architecture Team

---

## Overview

This document specifies how MCP tool discovery integrates with the governance system to automatically generate and maintain governance configuration for discovered tools while preserving user customizations.

---

## Goals

1. **First-run friendly**: Provide working defaults immediately
2. **User control**: Allow customization of any tool's permissions
3. **Version control friendly**: Config file can be committed and reviewed
4. **Incremental discovery**: New tools append without disrupting existing config
5. **Audit trail**: Track when and how tools were discovered
6. **Safe defaults**: Infer appropriate risk levels automatically

---

## Architecture

### Components

1. **MCPGatewayAdapter**: Discovers tools from Docker MCP Gateway
2. **MCPGovernanceManager**: Manages governance config file updates
3. **tools.yaml**: User-editable governance configuration file

### Data Flow

```
┌─────────────────────────────────────────┐
│  Docker MCP Gateway                      │
│  - Discovers: github_search,             │
│    duckduckgo_search, slack_send, etc.  │
└──────────────┬──────────────────────────┘
               │ list_tools()
               ↓
┌─────────────────────────────────────────┐
│  MCPGatewayAdapter                       │
│  - Converts tool schemas                 │
│  - Infers risk levels                    │
└──────────────┬──────────────────────────┘
               │ ensure_tool_configured()
               ↓
┌─────────────────────────────────────────┐
│  MCPGovernanceManager                    │
│  - Check if tool exists in config        │
│  - Generate template if missing          │
│  - Append to tools.yaml                  │
│  - Preserve user customizations          │
└──────────────┬──────────────────────────┘
               │
               ↓
┌─────────────────────────────────────────┐
│  config/governance/tools.yaml            │
│  - Auto-generated entries                │
│  - User customizations                   │
│  - Version controlled                    │
└─────────────────────────────────────────┘
```

---

## Discovery Workflow

### Step 1: Tool Discovery

When MCP Gateway initializes:

```python
# In MCPGatewayAdapter.initialize()
mcp_tools = await self.client.list_tools()
# Returns: [
#   {"name": "github_search", "description": "...", "inputSchema": {...}},
#   {"name": "duckduckgo_search", "description": "...", "inputSchema": {...}},
# ]
```

### Step 2: Check Existing Configuration

For each discovered tool:

```python
tool_name = f"mcp_{mcp_tool['name']}"  # e.g., "mcp_github_search"

# Check if already configured
with open("config/governance/tools.yaml") as f:
    config = yaml.safe_load(f)

if tool_name in config.get("tools", {}):
    # Tool already configured - SKIP (preserve user customizations)
    continue
```

### Step 3: Generate Template

If tool not found, generate template with safe defaults:

```python
template = {
    "category": "mcp",
    "allowed_in_modes": [...],  # Based on risk level
    "risk_level": "...",  # Inferred from name
    "requires_approval": ...,  # Based on risk level
}
```

### Step 4: Append to Configuration

Append template to `tools.yaml` preserving formatting:

```yaml
  # Auto-discovered: 2026-01-17T12:30:45
  # Search GitHub repositories for code and projects
  mcp_github_search:
    category: "mcp"
    allowed_in_modes: ["NORMAL", "DEGRADED"]
    risk_level: "low"
    requires_approval: false
    # Customize as needed:
    # forbidden_paths: []
    # allowed_paths: []
    # timeout_seconds: 30
```

---

## Risk Level Inference

### Algorithm

Risk level is inferred from tool name using keyword matching:

```python
def _infer_risk_level(tool_name: str) -> Literal["low", "medium", "high"]:
    """Infer risk level from tool name.

    High risk: Actions that modify state, send data, or execute code
    Low risk: Read-only, search, query operations
    Medium risk: Everything else (default)
    """
    name_lower = tool_name.lower()

    # High risk keywords
    if any(keyword in name_lower for keyword in [
        "write", "delete", "execute", "send", "create",
        "modify", "update", "remove", "destroy", "drop"
    ]):
        return "high"

    # Low risk keywords
    if any(keyword in name_lower for keyword in [
        "read", "get", "list", "search", "query",
        "view", "show", "fetch", "retrieve"
    ]):
        return "low"

    # Default
    return "medium"
```

### Examples

| Tool Name | Inferred Risk | Reasoning |
|-----------|---------------|-----------|
| `github_search` | low | Contains "search" |
| `duckduckgo_search` | low | Contains "search" |
| `slack_send_message` | high | Contains "send" |
| `filesystem_write` | high | Contains "write" |
| `github_create_pull_request` | high | Contains "create" |
| `database_query` | low | Contains "query" |
| `system_info` | medium | No high/low keywords |

---

## Template Generation Rules

### Risk Level → Mode Permissions

| Risk Level | Allowed Modes | Requires Approval |
|------------|---------------|-------------------|
| low | NORMAL, ALERT, DEGRADED | false |
| medium | NORMAL, DEGRADED | false |
| high | NORMAL | true |

### Template Fields

```yaml
mcp_tool_name:
  # Required fields
  category: "mcp"  # Always "mcp" for discovered tools
  allowed_in_modes: [...]  # Based on risk level
  risk_level: "low|medium|high"  # Inferred from name
  requires_approval: true|false  # Based on risk level

  # Optional fields (commented out by default)
  # forbidden_paths: []  # User can uncomment to restrict
  # allowed_paths: []    # User can uncomment to restrict
  # timeout_seconds: 30  # User can uncomment to override
```

### Discovery Metadata

Auto-discovery includes metadata for audit trail:

```yaml
  # Auto-discovered: 2026-01-17T12:30:45  ← Timestamp
  # <tool description from MCP schema>    ← Description (truncated if >70 chars)
  mcp_tool_name:
    # ... config fields ...
```

---

## User Customization Workflow

### Scenario 1: Accept Defaults

User does nothing - auto-generated config works immediately:

```yaml
# Auto-discovered entry (no changes needed)
mcp_github_search:
  category: "mcp"
  allowed_in_modes: ["NORMAL", "DEGRADED"]
  risk_level: "low"
  requires_approval: false
```

Tool is usable immediately after discovery.

### Scenario 2: Restrict Permissions

User edits config to restrict tool:

```yaml
mcp_github_search:
  category: "mcp"
  allowed_in_modes: ["NORMAL"]  # Removed DEGRADED
  risk_level: "medium"  # Elevated from low
  requires_approval: true  # Require approval now
  timeout_seconds: 15  # Faster timeout
```

Changes take effect on next restart.

### Scenario 3: Add Path Restrictions

User adds path validation for file-related tools:

```yaml
mcp_filesystem_read:
  category: "mcp"
  allowed_in_modes: ["NORMAL", "DEGRADED"]
  risk_level: "low"
  requires_approval: false
  # User added these:
  allowed_paths:
    - "$HOME/Documents/**"
    - "$HOME/Dev/personal_agent/**"
  forbidden_paths:
    - "**/.git/**"
    - "**/.env*"
    - "$HOME/.ssh/**"
```

Path validation enforced by `ToolExecutionLayer`.

### Scenario 4: New Tool Discovered

Existing tools preserved, new tool appended:

```yaml
# Existing (user customized) - NOT TOUCHED
mcp_github_search:
  category: "mcp"
  allowed_in_modes: ["NORMAL"]  # User's custom value
  risk_level: "medium"
  requires_approval: true

# New tool discovered - AUTO-APPENDED
  # Auto-discovered: 2026-01-18T09:15:22
  mcp_slack_send_message:
    category: "mcp"
    allowed_in_modes: ["NORMAL"]
    risk_level: "high"  # Inferred from "send"
    requires_approval: true
```

User's customizations to `mcp_github_search` are preserved.

---

## File Format Specification

### YAML Structure

```yaml
tool_categories:
  mcp:
    description: "Tools from Docker MCP Gateway (containerized)"
    risk_level: "medium"
    examples: ["mcp_github_search", "mcp_duckduckgo_search"]

tools:
  # Built-in tools (manually defined)
  read_file:
    category: "read_only"
    allowed_in_modes: ["NORMAL", "ALERT", "DEGRADED"]

  # MCP tools (auto-discovered, user-editable)

  # Auto-discovered: 2026-01-17T12:30:45
  # Search GitHub repositories
  mcp_github_search:
    category: "mcp"
    allowed_in_modes: ["NORMAL", "DEGRADED"]
    risk_level: "low"
    requires_approval: false
    # Customize as needed:
    # forbidden_paths: []
    # allowed_paths: []
    # timeout_seconds: 30
```

### Append Location

New MCP tools are appended to the END of the `tools:` section to:
- Keep all MCP tools together
- Avoid disrupting manually-ordered built-in tools
- Make diffs easy to review in version control

### Comment Conventions

```yaml
  # Auto-discovered: YYYY-MM-DDTHH:MM:SS  ← ISO 8601 timestamp
  # <description>                         ← Tool description (max 70 chars)
  mcp_tool_name:                          ← Tool entry
    category: "mcp"                       ← Required fields
    # Customize as needed:                ← User customization hints
    # field: value                        ← Commented optional fields
```

---

## Idempotency & Conflict Resolution

### Idempotency Guarantee

Calling `ensure_tool_configured()` multiple times for same tool is safe:

```python
# First call: Appends entry
mgr.ensure_tool_configured("mcp_github_search", schema, "low")
# → Entry added to tools.yaml

# Second call: No-op
mgr.ensure_tool_configured("mcp_github_search", schema, "low")
# → Entry already exists, nothing happens

# User edits entry...

# Third call: Still no-op
mgr.ensure_tool_configured("mcp_github_search", schema, "low")
# → Entry exists (even with user changes), nothing happens
```

User customizations are NEVER overwritten.

### Name Conflicts

MCP tools always have `mcp_` prefix:
- MCP tool "github_search" → `mcp_github_search`
- Built-in tool "read_file" → `read_file`

No conflicts possible between built-in and MCP tools.

If two MCP servers have same tool name:
- First one discovered wins
- Second one skipped (already exists check)
- Logged as warning

---

## Testing Strategy

### Unit Tests

Test governance manager in isolation:

```python
def test_template_generation():
    """Test risk level inference and template generation."""
    mgr = MCPGovernanceManager()

    # High risk tool
    template = mgr._generate_template(
        "mcp_filesystem_write",
        {"description": "Write files"},
        "high"
    )
    assert template["risk_level"] == "high"
    assert template["requires_approval"] is True

    # Low risk tool
    template = mgr._generate_template(
        "mcp_github_search",
        {"description": "Search GitHub"},
        "low"
    )
    assert template["risk_level"] == "low"
    assert template["requires_approval"] is False
```

### Integration Tests

Test with temp config file:

```python
def test_config_append():
    """Test config file append and idempotency."""
    with tempfile.TemporaryDirectory() as tmpdir:
        config_path = Path(tmpdir) / "tools.yaml"
        config_path.write_text("tools:\n  read_file:\n    category: read_only\n")

        mgr = MCPGovernanceManager()

        # First append
        mgr.ensure_tool_configured("mcp_test", {...}, "low")
        content = config_path.read_text()
        assert "mcp_test:" in content

        # Second append (should be no-op)
        mgr.ensure_tool_configured("mcp_test", {...}, "low")
        content2 = config_path.read_text()
        assert content == content2  # Unchanged
```

### E2E Tests

Test full discovery workflow:

```python
@pytest.mark.integration
async def test_discovery_updates_config():
    """Test discovered tools are added to config."""
    # Enable gateway, discover tools
    adapter = MCPGatewayAdapter(registry)
    await adapter.initialize()

    # Check config file updated
    with open("config/governance/tools.yaml") as f:
        config = yaml.safe_load(f)

    # Verify MCP tools present
    tools = config["tools"]
    mcp_tools = {k: v for k, v in tools.items() if k.startswith("mcp_")}
    assert len(mcp_tools) > 0
```

---

## Error Handling

### File Not Found

```python
# Governance config must exist
if not tools_config_path.exists():
    raise FileNotFoundError(f"Governance config not found: {tools_config_path}")
```

User must create `config/governance/tools.yaml` before discovery.

### File Permissions

```python
try:
    with open(tools_config_path, 'a') as f:
        f.write(content)
except PermissionError as e:
    log.error("governance_config_write_failed", path=str(tools_config_path), error=str(e))
    # Don't crash - tool still registered, just not persisted
```

Discovery continues even if config file can't be updated.

### Invalid YAML

```python
try:
    with open(tools_config_path) as f:
        config = yaml.safe_load(f)
except yaml.YAMLError as e:
    log.error("governance_config_parse_failed", error=str(e))
    # Skip governance update for this tool
    return
```

Malformed config doesn't prevent tool registration.

---

## Migration & Backward Compatibility

### Existing Installations

For installations without MCP category in `tools.yaml`:

```python
# Auto-add MCP category if missing
if "mcp" not in config.get("tool_categories", {}):
    log.info("mcp_category_missing", action="will_append")
    # Append category definition to file
```

### Config File Versions

No explicit versioning needed - format is backward compatible:
- New fields ignored by old code
- Old fields still work with new code
- Comments don't affect parsing

---

## Security Considerations

### Audit Trail

Every auto-discovered tool includes timestamp:

```yaml
  # Auto-discovered: 2026-01-17T12:30:45
```

Enables security review:
- When was tool added?
- Was it manually added or auto-discovered?
- Has it been customized since discovery?

### Safe Defaults

Risk level inference errs on side of caution:
- Unknown patterns → `medium` risk
- `send`, `execute`, `write` → `high` risk (requires approval)
- Default modes are restrictive (high risk = NORMAL only)

### User Override

User can always make tools MORE restrictive:
- Elevate risk level
- Require approval
- Restrict to fewer modes
- Add path restrictions

User customizations override auto-generated defaults.

---

## Monitoring & Observability

### Log Events

```python
# Discovery events
log.info("mcp_tools_discovered", count=len(mcp_tools))
log.info("mcp_tool_governance_added", tool=tool_name, risk_level=risk_level)
log.debug("mcp_tool_already_configured", tool=tool_name)

# Error events
log.error("governance_config_write_failed", path=str(path), error=str(e))
log.warning("mcp_tool_registration_failed", tool=name, error=str(e))
```

### Metrics

Track discovery statistics:
- Total tools discovered
- Tools added to config
- Tools already configured
- Config write failures

---

## Future Enhancements

### Pattern-Based Rules

Allow users to define risk inference rules:

```yaml
mcp_discovery_rules:
  high_risk_patterns:
    - "*_delete_*"
    - "*_execute_*"
    - "*github*create_pull_request"
  low_risk_patterns:
    - "*_read_*"
    - "*_get_*"
    - "*_list_*"
```

### Config Validation

Validate user-edited config on load:

```python
def validate_config(config: dict) -> list[str]:
    """Validate governance config, return warnings."""
    warnings = []

    for tool_name, tool_config in config["tools"].items():
        # Check for overly permissive high-risk tools
        if tool_config["risk_level"] == "high":
            if not tool_config.get("requires_approval"):
                warnings.append(f"{tool_name}: High risk without approval")

    return warnings
```

### Discovery History

Separate log file tracking all discoveries:

```yaml
# config/governance/mcp_discovery_history.yaml
discoveries:
  - timestamp: 2026-01-17T12:30:45
    tool: mcp_github_search
    action: added
    risk_level: low
  - timestamp: 2026-01-18T09:15:22
    tool: mcp_slack_send_message
    action: added
    risk_level: high
```

---

## References

- ADR-0011: MCP Gateway Integration
- ADR-0005: Governance Configuration & Operational Modes
- MCP Implementation Plan v2
- `src/personal_agent/mcp/governance.py` (implementation)

---

**Document History**:
- 2026-01-17: Initial specification
