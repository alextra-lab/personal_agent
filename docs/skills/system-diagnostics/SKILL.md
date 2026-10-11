---
name: system-diagnostics
description: Lists and inspects what runs on the host or in the container with ps, ss, vmstat, iostat and
  lsof - which processes run, which ports listen, which process holds a file, and what disk and network
  activity looks like. Use when the task is to find or list processes, listening ports, open files, or
  I/O and network activity.
metadata:
  requires-tools: bash
---

# system-diagnostics — ps, ss, vmstat, iostat, lsof

## Rules

- Run the command and read its output. Do not answer from memory of what is usually running.
- Bound every sampling command with a count: `interval × count` is under 30 seconds. `vmstat` and `iostat` without a count run until the 30 second timeout and return nothing.
- If a piped command fails, do not infer that the binary is not supported. Retry without the pipe, or run `<binary> --version`.
- For a utilisation number (how much CPU, memory or disk) use the `system-metrics` skill.

## Processes

```bash
# The processes that use the most memory
ps aux --sort=-%mem | head -15
```

```bash
# The full list, without a pipe (always works)
ps -eo pid,user,pcpu,pmem,rss,comm,args --sort=-rss
```

```bash
# One process by name
pgrep -fa uvicorn
```

## Network and ports

```bash
# Listening ports, TCP and UDP, with the process
ss -tunlp
```

```bash
# Established connections
ss -tnp state established
```

## Load and I/O

```bash
vmstat 1 3
iostat 1 3
uptime
```

`vmstat 1 3` takes 3 samples, 1 second apart, and ends in 3 seconds.

## Disk usage and open files

```bash
# ask: the shell expands the glob /app/* here, so this is not a plain command and asks for approval.
du -sh /app/*
```

```bash
lsof -i | head -c 30000
```

```bash
# The process that holds one file open (the path below is an example)
lsof /app/data/cache.db
```

## Success

Check the result before you answer:

- The answer quotes the lines of output that it rests on (the process, the port, the address).
- A sampling answer states the interval and the count.
- "Nothing is listening" rests on an `ss` that ran and printed no matching line, not on a failed command.

## Recovery

| Failure | Cause | Next step |
|---|---|---|
| `command not found` | The binary is not in the image | Say which binary is missing. Use another command in this file for the same fact |
| A piped command prints an error | A pipe or an option failed | Run the same command without the pipe |
| `vmstat` or `iostat` runs until the timeout | No count was given | Add `1 3` |
| `Operation not permitted` from `lsof` or `ss -p` | The process runs as another user | Report the part that is visible. Do not try `sudo` |
| The output is very long | A verbose command | Add `| head -c 30000` (the tool cap is 50 KiB) |
| The same failure twice | — | Stop. Tell the user what failed and what was tried |

## Governance

- NORMAL runs without asking: `ps`, `pgrep`, `ss`, `netstat`, `lsof`, `vmstat`, `iostat`, `uname`, `uptime`, `du`, `df`, `free`, `top`, `find`.
- ALERT and DEGRADED run without asking: `ps`, `top`, `free`, `df`, `ss`, `netstat`, `uname`, `pgrep`.
- A glob such as `/app/*` asks (the shell expands it).
- LOCKDOWN and RECOVERY: `bash` is disabled.
