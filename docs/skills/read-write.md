# Skill: read / write (primitive filesystem I/O)

> FRE-261 Step 3 — supersedes legacy `read_file` and `write_file` tools.

## When to use `read` vs legacy `read_file`

| Situation | Use |
|-----------|-----|
| Reading any file in an allowed path | `read` (has explicit path governance) |
| Legacy code that already uses `read_file` | keep `read_file` until it is removed |
| Need byte-level size cap (e.g. cap at 64 KB) | `read` with `max_bytes=65536` |

`read` applies `allowed_paths` / `forbidden_paths` from `config/governance/tools.yaml`
at executor time, so bad paths are rejected before the filesystem is touched.

## When to use `write` vs legacy `write_file`

| Situation | Use |
|-----------|-----|
| Writing to a scratch area (`/tmp/`, sandbox) | `write` — proceeds unattended |
| Writing to a project file | `write` — advisory flag emitted in result; no automatic blocking (Planned: FRE-261 follow-up) |
| Appending log lines to an existing file | `write` with `mode="append"` |
| Legacy code using `write_file` | keep until `write_file` is removed |

## Scratch-dir convention (unattended)

Paths that don't require interactive approval:

- `/tmp/**` — OS temp directory
- `/app/agent_workspace/sandbox/<trace_id>/` — per-task scratch area

Write to scratch dirs when the output is ephemeral (analysis intermediates,
generated drafts before review). Always use a `trace_id`-scoped subdirectory
to avoid cross-task collisions.

## Path restrictions

Both tools check against `config/governance/tools.yaml`:

- **`allowed_paths`** — globs the path must match (if list is non-empty)
- **`forbidden_paths`** — globs the path must NOT match (checked first)
- **`unattended_paths`** (`write` only) — paths exempt from approval advisory

See `config/governance/tools.yaml` entries `read` and `write` for exact patterns.

## Usage examples

### read — load a config file

```json
{
  "tool": "read",
  "arguments": {
    "path": "/app/config/governance/tools.yaml"
  }
}
```

### read — read first 4 KB of a large file

```json
{
  "tool": "read",
  "arguments": {
    "path": "/app/logs/agent.log",
    "max_bytes": 4096
  }
}
```

### write — create a scratch analysis file

```json
{
  "tool": "write",
  "arguments": {
    "path": "/tmp/analysis-abc123/summary.txt",
    "content": "Key findings:\n- ...",
    "mode": "overwrite"
  }
}
```

### write — append to an existing notes file

```json
{
  "tool": "write",
  "arguments": {
    "path": "/tmp/notes.md",
    "content": "\n## New section\n...",
    "mode": "append"
  }
}
```
