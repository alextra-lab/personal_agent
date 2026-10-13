---
name: system-metrics
description: Takes a CPU, memory, disk and load snapshot of the agent process and of the host with ps, top,
  free and df. Use when the task is a utilisation number - CPU %, the memory of the agent process or the
  host in MB, disk space, or load average - or asks about a GPU. GPU metrics are not available here. Not
  for listing or ranking processes (for example the top 5 by memory), ports or open files, which the
  `system-diagnostics` skill does.
metadata:
  requires-tools: bash
---

# system-metrics — CPU, memory and disk utilisation snapshot

## Rules

- Run the command before you answer. Do not estimate or approximate a value.
- `free -m` reports the memory of the **host**, not of the agent process. For "how much memory does the agent use" read the resident set size of the process.
- To list or find processes and ports, use the `system-diagnostics` skill.

## The agent process

```bash
# Resident set size (RSS, in KB) of the agent processes
ps -eo pid,rss,pmem,args --sort=-rss | grep -E 'uvicorn|personal_agent' | head
```

```bash
# Without a pipe: find the process id, then read it
pgrep -fa uvicorn
ps -p <pid> -o pid,rss,pmem,args
```

RSS is in KB. Divide by 1024 for MB.

## The host

```bash
top -bn1 | head -5
free -m
df -h /
df -h
uptime
```

`top -bn1 | head -5` gives the load average and the CPU line. `free -m` is the host total.
`df -h /` is the root disk, and `df -h` lists every mount.

## Structured values from `run_python`

Use `run_python` only when downstream code needs keys and values. It needs no `network` setting
(it reads local files only) and needs the sandbox image (`make sandbox-build`). The sandbox sees
its own cgroup: `/proc/meminfo` may report the host total, but CPU figures are per container. For
host values use the commands above.

```python
import json, shutil

with open('/proc/loadavg') as f:
    load = f.read().split()
mem = {}
with open('/proc/meminfo') as f:
    for line in f:
        parts = line.split()
        if parts[0] in ('MemTotal:', 'MemAvailable:'):
            mem[parts[0].rstrip(':')] = int(parts[1])
disk = shutil.disk_usage('/')
print(json.dumps({
    "load_1m": float(load[0]), "load_5m": float(load[1]), "load_15m": float(load[2]),
    "mem_total_mb": mem.get('MemTotal', 0) // 1024,
    "mem_available_mb": mem.get('MemAvailable', 0) // 1024,
    "disk_used_pct": round(disk.used / disk.total * 100, 1),
}, indent=2))
```

GPU metrics are not available in this environment. If asked for GPU data, answer "GPU metrics
unavailable in this environment".

## Success

Check the result before you answer:

- Each number comes from a command that ran in this turn. The answer names the command.
- The unit is stated (KB or MB, percent, load over 1, 5 and 15 minutes).
- A memory answer says whether it is the agent process or the host.
- A load figure is read next to the CPU count if the answer calls it high or low.

## Recovery

| Failure | Cause | Next step |
|---|---|---|
| `grep` prints nothing for the process name | The process has another name | Run `ps -eo pid,rss,pmem,args --sort=-rss` and read the first rows |
| `command not found` | The binary is not in the image | Say which one. Use the `run_python` snippet for the values |
| The `run_python` call fails to start | Docker is not available to the gateway | Use the `bash` commands. Report that the structured form was not available |
| A GPU figure is asked for | The environment has no GPU sensor | Answer that GPU metrics are unavailable. Do not give a number |
| The same failure twice | — | Stop. Tell the user what failed and what was tried |

## Governance

- `top`, `free` and `df` run without asking in NORMAL, ALERT and DEGRADED.
- `bash` is disabled in LOCKDOWN and RECOVERY. There is no other route for those modes.
- `run_python` for `/proc` metrics runs without asking in NORMAL. It asks in ALERT and DEGRADED.
