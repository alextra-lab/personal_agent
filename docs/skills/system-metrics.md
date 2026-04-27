# system-metrics — CPU, memory, and disk utilisation snapshot

**Category:** `system_read` · **Risk:** none · **Approval:** auto-approved in all non-LOCKDOWN modes

## Quick snapshot (human-readable)

Run each command separately to stay within the auto-approve list (`sh` is not auto-approved):

```bash
bash top -bn1 | head -5
bash free -m
bash df -h /
```

## Individual metrics

```bash
# CPU — load average + per-core breakdown
bash top -bn1 | head -5

# Memory — total / used / free / available in MB
bash free -m

# Disk — all mounts, human-readable
bash df -h

# Disk — root only
bash df -h /
```

Example `free -m` output:
```
              total        used        free      shared  buff/cache   available
Mem:          15982        4201        8342          62        3438       11346
Swap:          2047           0        2047
```

## Structured output via run_python

Use `run_python` when downstream code needs structured key/value data. The sandbox image does **not** include `psutil`; use `/proc` and `shutil` instead. No `network=True` needed — reads local files only.

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

## Regression note

`bash top / free / df` output is **human-readable text** — it does not emit the structured `perf_system_*` keys the legacy `system_metrics_snapshot` tool returned. If callers expected those keys, use the `run_python` snippet above.

**GPU metrics are unavailable** via these commands. The legacy tool polled Apple Silicon sensors (`powermetrics`) which are not present in the Linux container. GPU data is not accessible.

## Governance

- `top`, `free`, `df` are auto-approved in all non-LOCKDOWN modes — no PWA prompt.
- Available in NORMAL, ALERT, and DEGRADED modes.
- `bash` is disabled in LOCKDOWN and RECOVERY — no workaround for those modes.
- `run_python` for `/proc`-based metrics: available in NORMAL/ALERT/DEGRADED; no `network=True` needed (reads local files only); requires sandbox image (`make sandbox-build`).
- See also: `bash.md` (output cap 50 KiB), `run-python.md` (sandbox details).
