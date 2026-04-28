# system-diagnostics — Run system diagnostic commands (ps, ss, vmstat, lsof, …)

**Category:** `system_read` · **Risk:** low · **Approval:** listed commands auto-approved in NORMAL; subset in ALERT/DEGRADED

Pipes and composition work (`bash` now runs via `/bin/bash`). FRE-283.

## Processes — preferred recipes

```bash
# Top memory consumers (preferred — pipe works)
bash ps aux --sort=-%mem | head -15

# No-pipe fallback (always works, shows full list)
bash ps -eo pid,user,pcpu,pmem,rss,comm,args --sort=-rss

# Find a specific process
bash pgrep -fa uvicorn
bash ps -ef | grep uvicorn
```

**Stop rule:** If a piped command fails, do NOT infer the binary is unsupported.
Retry the same command without the pipe, or run `<binary> --version` to verify.

## Network / ports

```bash
bash ss -tunlp          # listening ports (TCP+UDP, with process)
bash ss -tnp state established
bash netstat -tunlp
```

## Load and I/O — ALWAYS use bounded vmstat/iostat

`vmstat` without a count runs until the 30 s timeout returns nothing useful.

```bash
bash vmstat 1 3         # 3 samples, 1 s apart (terminates in 3 s)
bash iostat 1 3
bash uptime             # current load average
```

Rule: `interval × count < 30` seconds.

## Disk usage

```bash
bash du -sh /app/*
bash du -sh /app/* | sort -rh | head -10
```

## File handles

```bash
bash lsof -i | head -c 30000
bash lsof -p <pid>
```

## Governance

- **NORMAL auto-approve:** `ps`, `pgrep`, `ss`, `netstat`, `lsof`, `vmstat`, `iostat`, `uname`, `uptime`, `du`, `df`, `free`, `top`, `find`.
- **ALERT/DEGRADED auto-approve subset:** `ps`, `top`, `free`, `df`, `ss`, `netstat`, `uname`, `pgrep`.
- LOCKDOWN / RECOVERY: `bash` disabled.
- Output capped at 50 KiB. Use `| head -c 30000` on verbose commands.
