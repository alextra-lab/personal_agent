# infrastructure-health — Probe all infrastructure services for reachability

**Category:** `system_read` · **Risk:** none · **Approval:** `run_python` auto-approved (NORMAL/ALERT/DEGRADED); bash one-liners auto-approved

> **Container vs host:** All hostnames (`postgres`, `neo4j`, `elasticsearch`, `redis`, `embeddings`, `reranker`) are Docker DNS names that **only resolve from inside the `cloud-sim` Docker network**. Running these checks from the VPS host shell will fail with DNS errors. From the host, use `localhost:<port>` instead (e.g. `curl http://localhost:9200/_cluster/health`).

## Full health check (run_python — preferred)

Probes all 7 services: Postgres, Neo4j (Bolt + HTTP), Elasticsearch, Redis, Embeddings, Reranker.

**Required invocation form** — `network=True` is mandatory; the sandbox runs with `--network=none` by default and stdlib socket/urllib cannot bypass kernel-level network namespace isolation:

```
run_python(script=<snippet below>, network=True)
```

> **ALERT/DEGRADED modes:** `network=True` requires PWA approval in ALERT and DEGRADED. In those modes prefer the individual bash quick-checks below, which use the bash executor (already has network access) and are auto-approved.

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
            return {"reachable": True, "http_status": r.status, "body": r.read(300).decode("utf-8", "replace")}
    except urllib.error.HTTPError as e:
        return {"reachable": True, "http_status": e.code, "body": ""}
    except Exception as e:
        return {"reachable": False, "error": str(e)}

result = {
    "postgres":      tcp("postgres", 5432),
    "neo4j_bolt":    tcp("neo4j", 7687),
    "neo4j_http":    http("http://neo4j:7474"),
    "elasticsearch": http("http://elasticsearch:9200/_cluster/health"),
    "redis":         tcp("redis", 6379),
    "embeddings":    http("http://embeddings:8503/health"),
    "reranker":      http("http://reranker:8504/health"),
}
result["all_reachable"] = all(v.get("reachable") for v in result.values())
print(json.dumps(result, indent=2))
```

## Quick single-service checks (bash — inside container only)

These use Docker DNS and **only work from inside the seshat-gateway container**:

```bash
# Elasticsearch cluster status
bash curl -s http://elasticsearch:9200/_cluster/health | jq .status

# Postgres — query test (note: postgresql:// not postgresql+asyncpg://)
bash psql -c 'SELECT 1' postgresql://agent:<password>@postgres:5432/personal_agent

# Redis
bash redis-cli -h redis PING

# Neo4j HTTP
bash curl -s http://neo4j:7474 | head -c 100

# Embeddings service
bash curl -s http://embeddings:8503/health

# Reranker service
bash curl -s http://reranker:8504/health
```

> **psql URL note:** `AGENT_DATABASE_URL` uses `postgresql+asyncpg://` which psql cannot parse. Strip `+asyncpg` before passing to psql: `echo $AGENT_DATABASE_URL | sed 's/postgresql+asyncpg/postgresql/'`

## Interpreting results

| Field | Meaning |
|-------|---------|
| `reachable: true` | TCP connection succeeded (or HTTP responded, even with 4xx/5xx) |
| `reachable: false` | Connection refused or timeout — service is down or unreachable |
| `all_reachable` | `true` only when every service is reachable |
| `http_status` | HTTP response code; 200 = healthy, anything else warrants investigation |

## Governance

- `run_python` with `network=True`: auto-approved in NORMAL. Requires PWA approval in ALERT/DEGRADED. Uses stdlib only (no subprocess); `network=True` is required — the sandbox runs with `--network=none` by default.
- `bash curl`: auto-approved in NORMAL, ALERT, DEGRADED.
- `bash psql -c` and `bash redis-cli`: auto-approved in NORMAL only — require PWA approval in ALERT/DEGRADED.
- LOCKDOWN: `run_python` disabled; `bash` also disabled. No health check is available in LOCKDOWN mode.
- See also: `run-python.md` (sandbox details), `bash.md` (auto-approve list).
