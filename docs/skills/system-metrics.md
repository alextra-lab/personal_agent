# system-metrics — CPU, memory, and disk utilisation snapshot

**Category:** `system_read` · **Risk:** none · **Approval:** auto-approved in all non-LOCKDOWN modes

## Agent-process RSS (NOT `free -m`)

`free -m` reports **host total memory**, not the agent process. For "how much memory is the agent using?":

```bash
# Preferred — pipe works (FRE-283)
bash ps -eo pid,rss,pmem,args --sort=-rss | grep -E 'uvicorn|personal_agent' | head

# No-pipe fallback
bash pgrep -fa uvicorn
bash ps -p <pid> -o pid,rss,pmem,args
```

RSS is in KB. Divide by 1024 for MB.

## Host-level metrics

```bash
bash top -bn1 | head -5   # load average + CPU
bash free -m               # host total memory (not agent)
bash df -h /               # root disk usage
bash df -h                 # all mounts
bash uptime                # load average shortcut
```

## Structured output via run_python

Use `run_python` only when structured keys are needed by downstream code.
Note: `/proc` inside the sandbox reflects cgroup namespace, not raw host. For
host-level metrics the `bash` recipes above are more accurate.

```python
import json, shutil
with open('/proc/loadavg') as f:
    la = f.read().split()
mem = {}
with open('/proc/meminfo') as f:
    for line in f:
        p = line.split()
        if p[0] in ('MemTotal:', 'MemAvailable:'):
            mem[p[0].rstrip(':')] = int(p[1])
print(json.dumps({
    "load_1m": float(la[0]), "load_5m": float(la[1]),
    "mem_total_mb": mem.get('MemTotal', 0) // 1024,
    "mem_available_mb": mem.get('MemAvailable', 0) // 1024,
    "disk_used_pct": round(shutil.disk_usage('/').used / shutil.disk_usage('/').total * 100, 1),
}, indent=2))
```

## Structured output via run_python

Use `run_python` when downstream code needs structured key/value data. The sandbox image now includes `psutil` (installed in `seshat-sandbox-python:0.1`) as well as `/proc` access. No `network=True` needed — reads local files only.

> **Scope note:** Inside the `seshat-sandbox-python:0.1` container, `/proc/meminfo`, `/proc/stat`, and `/proc/loadavg` reflect the **sandbox container's cgroup namespace**, not raw host metrics. Total memory may report the host total, but CPU stats are per-container. For host-level metrics, use the `bash top` / `bash free` / `bash df` recipes — those run in the seshat-gateway container which has direct `/proc` access.

```python
import json, shutil

# CPU load from /proc/loadavg
with open('/proc/loadavg') as f:
    loadavg = f.read().split()
    load_1m, load_5m, load_15m = loadavg[0], loadavg[1], loadavg[2]

# Memory from /proc/meminfo
mem = {}
with open('/proc/meminfo') as f:
    for line in f:
        parts = line.split()
        if parts[0] in ('MemTotal:', 'MemAvailable:', 'MemFree:'):
            mem[parts[0].rstrip(':')] = int(parts[1])

# Disk from Python stdlib
disk = shutil.disk_usage('/')

result = {
    "load_1m": float(load_1m),
    "load_5m": float(load_5m),
    "load_15m": float(load_15m),
    "mem_total_mb": mem.get('MemTotal', 0) // 1024,
    "mem_available_mb": mem.get('MemAvailable', 0) // 1024,
    "disk_used_pct": round(disk.used / disk.total * 100, 1),
}
print(json.dumps(result, indent=2))
```

## Gaps vs legacy `system_metrics_snapshot`

- **Structured keys** (`perf_system_*`): use `run_python` snippet above if needed.
- **GPU metrics**: unavailable. Legacy tool polled Apple Silicon sensors not present in the Linux container. If asked for GPU data, answer "GPU metrics unavailable in this environment".

## Governance

- `top`, `free`, `df` are auto-approved in all non-LOCKDOWN modes — no PWA prompt.
- Available in NORMAL, ALERT, and DEGRADED modes.
- `bash` is disabled in LOCKDOWN and RECOVERY — no workaround for those modes.
- `run_python` for `/proc`-based metrics: available in NORMAL/ALERT/DEGRADED; no `network=True` needed (reads local files only); requires sandbox image (`make sandbox-build`).
- See also: `bash.md` (output cap 50 KiB), `run-python.md` (sandbox details).
