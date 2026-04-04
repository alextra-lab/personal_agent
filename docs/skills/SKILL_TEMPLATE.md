# SKILL: <tool-name>

> **Tier:** 2 — CLI tool  
> **Binary:** `<cli-command>`  
> **Auth:** `<how auth is configured, e.g. AGENT_MYSERVICE_API_KEY env var>`  
> **ADR:** `docs/architecture_decisions/ADR-XXXX-<name>.md`

---

## What This Skill Does

One paragraph describing what the CLI tool does and when the agent should use it vs alternatives.

---

## When to Use

- Use case 1
- Use case 2
- **Prefer `<native-tool>` instead** when \<condition\>

---

## Commands

### Basic usage
```bash
<cli-command> <subcommand> [flags]
```

### Common patterns

**Pattern 1: <description>**
```bash
<cli-command> <example-command> --flag value
```
Output:
```
<example output>
```

**Pattern 2: <description>**
```bash
<cli-command> <another-example>
```

---

## Authentication

```bash
# Check current auth status
<cli-command> auth status

# Login (interactive — run with ! prefix in Claude Code)
<cli-command> auth login
```

---

## Error Handling

| Error | Cause | Fix |
|-------|-------|-----|
| `<error message>` | `<cause>` | `<resolution>` |

---

## Notes

- Any quirks, rate limits, or important constraints
- Version requirements if any
