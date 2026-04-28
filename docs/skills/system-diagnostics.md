# system-diagnostics — Run system diagnostic commands (ps, ss, vmstat, lsof, …)

**Category:** `system_read` · **Risk:** low · **Approval:** all listed commands are auto-approved (NORMAL); no PWA prompt

All commands the legacy `run_sysdiag` tool allowed are on the bash auto-approve list. Write the full command directly — no `shlex.split` needed.

## Container ABI (empirically verified 2026-04-28)

Commands run inside `seshat-gateway` (Debian bookworm base). Installed versions:

| Tool | Version | Supported flags |
|------|---------|-----------------|
| `ps` (procps-ng) | 4.0.4 | `--sort=-%mem`, `--sort=-%cpu`, `-o pid,user,pcpu,pmem,cmd`, `-ef`, `aux` |
| `vmstat` (procps-ng) | 4.0.4 | `vmstat <interval> <count>` (bounded form required — see below) |
| `iostat` (sysstat) | 12.7.5 | `iostat <interval> <count>` (bounded form required) |
| `ss` (iproute2) | system | `-tunlp`, `-tnp` |
| `lsof` | system | `-i`, `-p <pid>` |
| `uptime` / `uname` | system | Standard flags |

## vmstat / iostat — ALWAYS use bounded form

**`vmstat` without a count limit runs until killed.** The bash tool times out at 30 s — an unbounded `vmstat` blocks the full 30 s and returns nothing useful.

```bash
# CORRECT — bounded: interval=1s, count=3 → terminates in 3s
bash vmstat 1 3
bash iostat 1 3

# WRONG — unbounded: runs until 30s timeout
bash vmstat 1        # ← DO NOT USE
bash iostat          # ← DO NOT USE
```

Rule: `interval × count < 30` seconds. Typical use: `vmstat 1 3` (3 seconds).

## Processes

```bash
# All processes, full listing
bash ps -ef

# Top memory consumers
bash ps aux --sort=-%mem | head -15

# Top CPU consumers
bash ps aux --sort=-%cpu | head -15

# Find a specific process by name
bash pgrep -fa uvicorn
```

## Network / ports

```bash
# Listening ports (TCP + UDP, numeric, with process)
bash ss -tunlp

# Established connections
bash ss -tnp state established

# All open network connections (verbose)
bash netstat -tunlp
```

## Load and I/O

```bash
# System load + memory (3 samples, 1 s apart) — BOUNDED
bash vmstat 1 3

# Disk I/O throughput (3 samples, 1 s apart) — BOUNDED
bash iostat 1 3

# Current uptime + load average
bash uptime
```

## Disk usage

```bash
# Summary per top-level directory
bash du -sh /app/*

# Largest directories under /app
bash du -sh /app/* | sort -rh | head -10
```

## File handles and open connections

```bash
# All open network connections
bash lsof -i

# Open files for a specific process (PID or name)
bash lsof -p <pid>
```

## System info

```bash
bash uname -a
bash uptime
```

## Output cap

For commands that may produce large output, pipe through `head -c`:

```bash
bash lsof -i | head -c 30000
bash ps -ef | head -c 30000
```

## Governance

- Auto-approved in NORMAL: `ps`, `pgrep`, `top`, `lsof`, `find`, `df`, `du`, `iostat`, `vmstat`, `free`, `ip`, `ifconfig`, `ss`, `netstat`, `uptime`, `sysctl`, `who`, `last`, `uname`.
- **ALERT/DEGRADED mode auto-approve subset** (no PWA prompt): `ps`, `top`, `free`, `df`. Commands like `ss`, `netstat`, `vmstat`, `iostat`, `lsof`, `uname`, `uptime` require PWA approval in ALERT/DEGRADED modes. During incidents, use `ps` and `top` first.
- LOCKDOWN / RECOVERY: `bash` disabled. No equivalent available in those modes.
- Combined stdout + stderr capped at 50 KiB by the bash executor. Use `head -c 30000` to stay within limits on verbose commands.
- See also: `bash.md` for hard-denied patterns and full auto-approve list.
