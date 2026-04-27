# bash — Shell Command Executor

**Category:** `system_dangerous` · **Risk:** high · **Approval:** required (auto-approve list exempts safe commands)

## Purpose

The `bash` tool gives the agent direct shell access inside the container. It is the escape hatch for operations not covered by specialised native tools: inspecting logs, querying local services, running one-off diagnostics, or piping commands together.

Use it for:
- `curl` to call local services (Elasticsearch, Postgres, Redis)
- `grep` / `rg` to search source files
- `docker ps` / `docker logs` to inspect container state
- `git log` / `git diff` for quick repository queries
- `psql -c "..."` for ad-hoc SQL queries
- `redis-cli` for cache inspection

## Auto-approve list (no PWA prompt required)

The following command prefixes are auto-approved in NORMAL mode and run immediately without a PWA approval round-trip:

`curl`, `grep`, `ls`, `cat`, `find`, `jq`, `docker ps`, `docker logs`, `git log`, `git status`, `git diff`, `psql -c`, `redis-cli`, `ps`, `top`, `free`, `df`, `uptime`, `wc`, `rg`, `awk`, `sed`

In ALERT and DEGRADED modes the list shrinks to: `curl`, `grep`, `ls`, `cat`, `ps`, `top`, `free`, `df`.

Commands not in the list pause for user approval via the PWA before executing.

## Hard-denied patterns (immediate block — no subprocess spawned)

These patterns match regardless of mode and are refused before the subprocess is even created:

| Pattern | Reason |
|---------|--------|
| `rm\s+-rf` | Recursive deletion |
| `dd\s+if=` | Raw disk write |
| `mkfs` | Filesystem format |
| `sudo` | Privilege escalation |
| `wget` | Arbitrary file download |
| `ssh` | Remote shell access |
| `nc\s+-l` | Netcat listener |
| `:\(\)\s*\{.*\};:` | Fork bomb |

## Output cap

Combined stdout + stderr is capped at **50 KiB**. If output exceeds this limit:
- Both streams are truncated to 25 KiB each in memory.
- The full output is written to a scratch file under `/tmp/agent_scratch/<trace_id>/bash_output_N.txt`.
- The response includes a `truncated_path` key with the path to the overflow file.

## Examples

```bash
# Search Python source for a function definition
bash command="grep -rn 'def bash_executor' src/"

# Query Elasticsearch cluster health
bash command="curl -s http://localhost:9200/_cluster/health | jq ."

# Show running containers
bash command="docker ps --format 'table {{.Names}}\t{{.Status}}'"

# Quick git history (last 5 commits)
bash command="git log --oneline -5"

# Ad-hoc Postgres query
bash command="psql -c 'SELECT count(*) FROM sessions;' postgresql://agent:agent_dev_password@localhost:5432/personal_agent"
```

## Forbidden modes

`bash` is **not available** in LOCKDOWN or RECOVERY mode.
