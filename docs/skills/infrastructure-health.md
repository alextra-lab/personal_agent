# infrastructure-health — Probe all infrastructure services for reachability

**Category:** `system_read` · **Risk:** none · **Approval:** `run_python` auto-approved (NORMAL/ALERT/DEGRADED); bash one-liners auto-approved

## Full health check (run_python — preferred)

Probes all 7 services: Postgres, Neo4j (Bolt + HTTP), Elasticsearch, Redis, Embeddings, Reranker.

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

Uses stdlib `socket` and `urllib` only — no network flag needed in the sandbox.

## Quick single-service checks (bash)

```bash
# Elasticsearch cluster status
bash curl -s http://elasticsearch:9200/_cluster/health | jq .status

# Postgres — query test
bash psql -c 'SELECT 1' postgresql://agent:agent_dev_password@postgres:5432/personal_agent

# Redis
bash redis-cli -h redis PING

# Neo4j HTTP
bash curl -s http://neo4j:7474 | head -c 100

# Embeddings service
bash curl -s http://embeddings:8503/health

# Reranker service
bash curl -s http://reranker:8504/health
```

## Interpreting results

| Field | Meaning |
|-------|---------|
| `reachable: true` | TCP connection succeeded (or HTTP responded, even with 4xx/5xx) |
| `reachable: false` | Connection refused or timeout — service is down or unreachable |
| `all_reachable` | `true` only when every service is reachable |
| `http_status` | HTTP response code; 200 = healthy, anything else warrants investigation |

## Governance

- `run_python`: auto-approved in NORMAL/ALERT/DEGRADED. Uses stdlib only (no subprocess). No `network=true` flag needed — socket/urllib bypass `--network=none` restriction when attaching to the Docker bridge.
- `bash curl`/`psql`/`redis-cli`: auto-approved in NORMAL, ALERT, DEGRADED.
- LOCKDOWN: `run_python` disabled; `bash` also disabled. No health check is available in LOCKDOWN mode.
- See also: `run-python.md` (sandbox details), `bash.md` (auto-approve list).
