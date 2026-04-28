# bash — Shell Command Executor

**Category:** `system_dangerous` · **Risk:** high · **Approval:** required (auto-approve list exempts safe commands)

## Purpose

The `bash` tool gives the agent direct shell access inside the seshat-gateway container. It is the escape hatch for operations not covered by specialised native tools: inspecting logs, querying local services, running one-off diagnostics, or piping commands together.

Use it for:
- `curl` to call local services (Elasticsearch, Postgres, Redis, Neo4j)
- `grep` / `find` to search source files and application paths
- `ps` / `top` / `free` / `vmstat` for process and memory diagnostics
- `ss` / `lsof` for network and file-handle diagnostics
- `psql -c "..."` for ad-hoc SQL queries
- `redis-cli` for cache inspection
- `docker ps` / `docker logs` — only in the development container (Docker socket not mounted in cloud eval containers)

## Container environment

The bash tool runs commands **inside the seshat-gateway container** (`python:3.12-slim` base, plus diagnostic tools). Key characteristics:

| Tool | Available | Notes |
|------|-----------|-------|
| `curl`, `jq`, `grep`, `awk`, `sed`, `wc`, `find`, `ls`, `cat`, `df` | ✅ | Standard tools |
| `ps`, `top`, `free`, `vmstat`, `uptime` | ✅ | procps-ng 4.0.4 — `--sort`, `-o` flags supported |
| `iostat` | ✅ | sysstat 12.7.5 |
| `ss`, `netstat`, `lsof` | ✅ | Network/file handles |
| `psql` | ✅ | Use `postgresql://` URL (strip `+asyncpg`) |
| `redis-cli` | ✅ | |
| `git` | ❌ | Not installed. No `.git` directory in image |
| `docker ps` / `docker logs` | ❌ in cloud | Docker socket not mounted in cloud/eval containers |
| `rg` (ripgrep) | ❌ | Not installed; use `grep -rn` |

## Pipes

**Pipes work in a single bash call.** You can chain auto-approved commands without escaping:

```bash
bash command="find /app/src -name '*.py' | wc -l"
bash command="ps aux --sort=-%mem | head -10"
bash command="curl -s http://elasticsearch:9200/_cluster/health | jq .status"
```

## Shell semantics (FRE-283)

`bash` runs commands via `/bin/bash -o pipefail -c <command>`. Pipes (`|`), logical operators (`&&`, `||`), separators (`;`), redirects (`>`, `>>`), globs, and env substitution all work — the command is passed as a single argument to the shell.

The auto-approve check splits the command on top-level operators and verifies the first word of every segment. A command like `curl … | grep foo | wc -l` is auto-approved if all three first words (`curl`, `grep`, `wc`) are on the allowlist.

## Auto-approve list (no PWA prompt required)

**NORMAL mode** — auto-approved first words:

`curl`, `grep`, `ls`, `cat`, `find`, `jq`, `docker ps`, `docker logs`, `git log`, `git status`, `git diff`, `psql -c`, `redis-cli`, `ps`, `top`, `free`, `df`, `uptime`, `wc`, `rg`, `awk`, `sed`, `ss`, `vmstat`, `iostat`, `lsof`, `netstat`, `uname`, `pgrep`, `pg_isready`, `tr`, `sort`, `uniq`, `head`, `tail`, `echo`, `date`, `du`, `id`, `env`, `which`, `python3`

**ALERT/DEGRADED** — smaller subset: `curl`, `grep`, `ls`, `cat`, `ps`, `top`, `free`, `df`, `ss`, `netstat`, `uname`, `pgrep`, `echo`, `date`

Commands whose first word is not in the list (or a piped segment whose first word is not) pause for user approval via the PWA before executing.

## Hard-denied patterns (immediate block — no subprocess spawned)

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
bash command="grep -rn 'def bash_executor' /app/src/"

# Query Elasticsearch cluster health
bash command="curl -s http://elasticsearch:9200/_cluster/health | jq ."

# Top memory consumers
bash command="ps aux --sort=-%mem | head -10"

# Count Python files in source tree
bash command="find /app/src -name '*.py' | wc -l"

# Ad-hoc Postgres query (note: URL uses postgresql:// not postgresql+asyncpg://)
bash command="psql -c 'SELECT count(*) FROM sessions;' postgresql://agent:<password>@postgres:5432/personal_agent"

# Redis cache check
bash command="redis-cli -h redis PING"

# Disk usage
bash command="df -h | head -5"
```

## Forbidden modes

`bash` is **not available** in LOCKDOWN or RECOVERY mode.
