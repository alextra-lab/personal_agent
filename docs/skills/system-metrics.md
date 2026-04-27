# system-metrics — CPU, memory, and disk utilisation snapshot

**Category:** `system_read` · **Risk:** none · **Approval:** auto-approved in all non-LOCKDOWN modes

## Quick snapshot (human-readable)

```bash
bash sh -c 'echo "=CPU="; top -bn1 | head -5; echo "=MEM="; free -m; echo "=DISK="; df -h /'
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

Use `run_python` when downstream code needs structured key/value data matching the legacy `perf_system_*` naming:

```python
import psutil, json
m = psutil.virtual_memory()
d = psutil.disk_usage("/")
print(json.dumps({
    "cpu_percent": psutil.cpu_percent(interval=1),
    "perf_system_cpu_count": psutil.cpu_count(),
    "perf_system_mem_used": m.used // 1048576,
    "perf_system_mem_total": m.total // 1048576,
    "perf_system_mem_available": m.available // 1048576,
    "disk_used_pct": d.percent,
}))
```

## Regression note

`bash top / free / df` output is **human-readable text** — it does not emit the structured `perf_system_*` keys the legacy `system_metrics_snapshot` tool returned. If callers expected those keys, use the `run_python` snippet above.

**GPU metrics are unavailable** via these commands. The legacy tool polled Apple Silicon sensors (`powermetrics`) which are not present in the Linux container. GPU data is not accessible.

## Governance

- `top`, `free`, `df` are auto-approved in all non-LOCKDOWN modes — no PWA prompt.
- Available in NORMAL, ALERT, and DEGRADED modes.
- `bash` is disabled in LOCKDOWN and RECOVERY — no workaround for those modes.
- `run_python` for `psutil`: available in NORMAL/ALERT/DEGRADED; requires the sandbox image (`make sandbox-build`).
- See also: `bash.md` (output cap 50 KiB), `run-python.md` (sandbox details).
