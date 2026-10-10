---
name: bash
description: Shell command executor for diagnostics, log inspection, and local service queries inside the agent container.
when_to_use: When you need to run shell commands, inspect container state, query local services via curl/psql/redis-cli, or perform operations not covered by specialized tools.
tools: [bash]
---

# bash — Shell Command Executor

**Status:** Primary path (FRE-263, 2026-04-28). Replaces 8 deprecated curated tools via `AGENT_LEGACY_TOOLS_ENABLED=false`. Use `bash`, `read`, `run_python` primitives for all shell operations.

**Category:** `system_dangerous` · **Risk:** high · **Approval:** plain commands that an allow rule covers run; any other command asks the user (ADR-0156 D8)

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

**Pipes work in a single bash call.** A pipeline runs without asking when every command in it is allowed:

```bash
bash command="find /app/src -name '*.py' | wc -l"
bash command="ps aux --sort=-%mem | head -10"
bash command="curl -s http://elasticsearch:9200/_cluster/health | jq .status"
```

## Shell semantics (FRE-283)

`bash` runs commands via `/bin/bash -o pipefail -c <command>`. Pipes (`|`), logical operators (`&&`, `||`), separators (`;`), redirects (`>`, `>>`), globs, and env substitution all work — the command is passed as a single argument to the shell.

The permission check parses the command and judges every subcommand on its own (ADR-0156 D8.2). `curl … | grep foo | wc -l` runs without asking because each of `curl`, `grep` and `wc` is a plain command that an allow rule covers. A plain command has literal words only: no `$VAR` or `$(…)`, no glob such as `*.py` (quote it: `'*.py'`), no `~`, no `VAR=x` in front, no here-doc, and no redirection except `> /dev/null`, `2> /dev/null` and `2>&1`.

### SIGPIPE / exit 141 (FRE-470)

When a downstream pipeline consumer closes the pipe early — e.g. `find … | head` or `… | grep -q` — the upstream producer is killed by SIGPIPE (signal 13), and under `pipefail` the pipeline's exit code becomes `141` (128 + 13). This is normal early-exit behavior, not a failure. The executor therefore reports `success: true` for exit `141` **when the command is a pipeline**, and adds an explanatory `note` field. A standalone `exit 141` (no pipe) still reports `success: false`, so genuine errors are never masked.

## Allow, ask and deny (`config/governance/permissions.yaml`)

**Allowed in NORMAL mode** (as plain commands): `curl`, `grep`, `ls`, `cat`, `find`, `jq`, `docker ps`, `docker logs`, `git log`, `git status`, `git diff`, `ps`, `top`, `free`, `df`, `uptime`, `wc`, `rg`, `ss`, `vmstat`, `iostat`, `lsof`, `netstat`, `uname`, `pgrep`, `pg_isready`, `tr`, `sort`, `uniq`, `head`, `tail`, `echo`, `date`, `du`, `id`, `which`, `mmdc`.

**ALERT** — `curl`, `grep`, `ls`, `cat`, `ps`, `top`, `free`, `df`, `uname`, `echo`, `ss`, `netstat`, `pgrep`, `date`. **DEGRADED** — `curl`, `grep`, `ls`, `cat`, `ps`, `top`, `free`, `df`, `uname`, `echo`.

**Ask:** every other command, and every command that is not plain. `python3`, `awk`, `sed`, `env`, `psql` and `redis-cli` always ask. `curl` asks when an option sends or writes a local file (`-d @file`, `-F f=@file`, `-T`, `-K`, `-o` outside `/tmp`). The user answers on a card; with no card available (a CLI or eval turn) the call is refused.

**Deny** (refused even with approval): `rm -rf`, `rm -fr`, `dd`, `mkfs`, `sudo`, `wget`, `ssh`, `nc -l`.

A refusal names the subcommand and the rule. Do not retry the same command: use one that the allow list covers, or tell the user what you need to run.

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
# ask: no allow rule covers psql (FRE-1572: plain arguments can change the store), so this asks for approval.
bash command="psql -c 'SELECT count(*) FROM sessions;' postgresql://agent:<password>@postgres:5432/personal_agent"

# Redis cache check
# ask: no allow rule covers redis-cli (FRE-1572), so this asks for approval.
bash command="redis-cli -h redis PING"

# Disk usage
bash command="df -h | head -5"
```

## Forbidden modes

`bash` is **not available** in LOCKDOWN or RECOVERY mode.
