# run_python — Python Docker Sandbox

Execute Python scripts in an isolated, hardened Docker container.

## When to use

Use `run_python` for:
- Computation (maths, stats, data analysis)
- Data transformation (CSV/JSON parsing, reshaping with pandas)
- Inspection tasks (parsing a file, decoding a format)
- Any task needing libraries not available in the agent runtime

Do **not** use for file I/O that modifies the host filesystem outside `/sandbox` — use `write` for that.

## Pre-installed libraries

`requests`, `httpx`, `pandas`, `numpy`, `pyyaml`, `psutil`

System tools also available in the sandbox: `ps`, `top`, `free`, `vmstat`, `iostat`, `ss`, `lsof`, `curl`, `jq`, `redis-cli`, `psql` (same set as the gateway container).

## Scratch directory

The container's `/sandbox` is a host bind-mount scoped to the current trace. Files written there persist in `settings.sandbox_scratch_root/<trace_id>/` and are returned in `scratch_files`.

## Network

Network access is **disabled by default** (`--network=none`). Pass `network=true` to attach to the `cloud-sim` Docker network. Network access requires approval in ALERT and DEGRADED modes.

## Timeout

Default: 60 s. Maximum: 300 s. Minimum: 5 s. Pass `timeout_seconds` to override.

## Output cap

Combined stdout + stderr is capped at 50 KiB. Excess is truncated; `truncated: true` is set in the response.

## Availability

Requires the `docker` binary on `PATH` and the image `seshat-sandbox-python:0.1` to be built (`make sandbox-build`). Not available in LOCKDOWN or RECOVERY modes.

## Examples

```python
# Simple computation
script = "print(2 ** 10)"
# → stdout: "1024\n"

# pandas data manipulation
script = """
import pandas as pd, json
data = [{"x": i, "y": i**2} for i in range(5)]
df = pd.DataFrame(data)
print(df.to_json(orient="records"))
"""

# Write output to scratch
script = """
import json
result = {"answer": 42}
with open("/sandbox/result.json", "w") as f:
    json.dump(result, f)
print("written")
"""
# → scratch_files: ["/tmp/agent_sandbox/<trace_id>/result.json"]
```
