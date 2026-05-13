# SKILL: <tool-name>

<!-- Frontmatter for auto-loaded skills (name required; all other fields optional):

---
name: <tool-name>
description: <one-line description shown in the compact index>
when_to_use: <when the agent should prefer this skill>
tools: [bash]           # primitive tools this skill uses
keywords: [...]         # keyword/phrase triggers for hybrid routing
nudge: |                # optional — only set when the behavior is custom and specific to this
  <strong directive text>    skill (e.g. "run a live query — never answer from priors").
  <second line if needed>    Generic skills (bash, list-directory) don't need nudge.
                             Nudge is reviewed with the same gate as other frontmatter.
---

-->

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
