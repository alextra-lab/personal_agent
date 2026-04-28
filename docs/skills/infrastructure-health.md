# infrastructure-health — Probe all infrastructure services for reachability

**Category:** `system_read` · **Risk:** none · **Approval:** bash one-liners auto-approved in NORMAL; `run_python(network=True)` requires PWA approval in ALERT/DEGRADED

> **Container vs host:** Hostnames (`postgres`, `neo4j`, `elasticsearch`, `redis`) are Docker DNS names that only resolve from **inside** the `cloud-sim` network. From the VPS host shell, use `localhost:<port>` instead.

## Single-service checks — preferred path (bash)

For "is Postgres reachable?" use a real connection, not a TCP probe to port 5432:

```bash
# Postgres — AGENT_DATABASE_URL uses postgresql+asyncpg:// which psql cannot parse.
# Strip the driver specifier first:
bash psql "$(echo $AGENT_DATABASE_URL | sed 's|postgresql+asyncpg|postgresql|')" -c 'SELECT 1'
```

> **`pg_isready` is NOT installed** on the agent image. Do not use it.
> Do NOT probe Postgres with `curl http://postgres:5432` — port 5432 is the wire protocol, not HTTP; the response is always garbage and tells you nothing useful.

```bash
# Elasticsearch
bash curl -fsS http://elasticsearch:9200/_cluster/health | grep '"status"'

# Redis
bash redis-cli -h redis ping

# Neo4j HTTP
bash curl -fsS http://neo4j:7474/
```

## Multi-service check — run_python fallback

Use `run_python(network=True)` only when the prompt explicitly asks for all services at once. `run_python` depends on Docker-in-Docker socket availability and may fail when the Docker daemon is not mounted into the gateway container.

```python
import socket, json, urllib.request, urllib.error

def tcp(host, port, timeout=3):
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return {"reachable": True}
    except OSError as e:
        return {"reachable": False, "error": str(e)}

def http(url, timeout=5):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return {"reachable": True, "status": r.status}
    except urllib.error.HTTPError as e:
        return {"reachable": True, "status": e.code}
    except Exception as e:
        return {"reachable": False, "error": str(e)}

result = {
    "postgres":      tcp("postgres", 5432),
    "neo4j_http":    http("http://neo4j:7474"),
    "elasticsearch": http("http://elasticsearch:9200/_cluster/health"),
    "redis":         tcp("redis", 6379),
}
result["all_reachable"] = all(v.get("reachable") for v in result.values())
print(json.dumps(result, indent=2))
```

## Stop rules

- Do not infer "service unreachable" from a curl TCP probe to a non-HTTP port.
- If `run_python` fails to start (Docker unavailable), surface that clearly and fall back to single-service bash probes.
- `pg_isready` is not installed — do not attempt it.

## Governance

- `bash curl`, `bash redis-cli`: auto-approved NORMAL/ALERT/DEGRADED.
- `bash psql -c`: auto-approved NORMAL.
- `run_python(network=True)`: NORMAL auto-approved; ALERT/DEGRADED require PWA approval.
- LOCKDOWN: bash and run_python both disabled.
