---
name: infrastructure-health
description: Probes whether Postgres, Elasticsearch, Neo4j and Redis answer, with a real connection or a
  health endpoint for each, and reports one line for each service. Use when the task is whether a backend
  service is up, reachable or healthy, before saying that a service is up or down. Not for Tempo - Tempo
  status and version belong to the query-tempo skill.
metadata:
  requires-tools: bash
---

# infrastructure-health — Is each backend service reachable?

## Rules

- Probe the live endpoint before you say that a service is healthy or down.
- Use a real connection for Postgres, not a TCP probe. Port 5432 is the wire protocol, not HTTP: `curl http://postgres:5432` always gives a meaningless reply.
- The names `postgres`, `neo4j`, `elasticsearch` and `redis` are Docker DNS names. They resolve only inside the `cloud-sim` network. From the host shell use `localhost:<port>`.

## Step 1 — One service, or all four

The script opens a real Postgres connection and runs `SELECT 1`, reads the Elasticsearch cluster
health, reads the Neo4j HTTP page, and sends a Redis `PING`. It prints one line for each service.

```bash
python3 docs/skills/infrastructure-health/scripts/probe_services.py all
```

```bash
python3 docs/skills/infrastructure-health/scripts/probe_services.py postgres
```

The argument is `postgres`, `elasticsearch`, `neo4j`, `redis` or `all`. Each line reads
`<service>: ok <detail>` or `<service>: FAIL <detail>`. The exit code is 0 when every probed
service answered, 1 when one did not, and 2 for bad usage. No password is printed.

## Step 2 — Without asking: the two HTTP endpoints

A plain `GET` of a listed health endpoint runs without asking:

```bash
curl -fsS http://elasticsearch:9200/_cluster/health
curl -fsS http://neo4j:7474/
```

`status` in the first reply is `green`, `yellow` or `red`. A single-node cluster is often
`yellow`: that is up. Only `red` is unhealthy.

## Success

Check the result before you answer:

- The answer has one line for each service that was asked about, with `ok` or the error text.
- "Down" rests on a failed connection or a bad reply, not on a missing tool or a wrong name.
- A `yellow` Elasticsearch status is reported as `yellow` and as up.
- The answer says whether the probe ran inside the gateway container or on the host.

## Recovery

| Failure | Cause | Next step |
|---|---|---|
| `postgres: FAIL AGENT_DATABASE_URL is not set in this environment` | The script runs where the variable is not set | Say that Postgres cannot be probed from here. Do not guess |
| `postgres: FAIL psql is not installed in this environment` | The host shell has no client | Run the probe through the `bash` tool in the gateway container |
| `FAIL cannot connect to ...` | The service is down, or the name does not resolve from here | Check where the command runs (the Rules above). If it runs in the gateway, the service is down: say so |
| `redis: ok ... asks for a password` | Redis needs authentication | The server is up. Say that PING was not tried with a password |
| `elasticsearch: FAIL cluster status red` | Shards are missing | Report `red`. Use the `query-elasticsearch` skill for `_cat/indices` if asked for the cause |
| The same failure twice | — | Stop. Tell the user the service, the error text and what was tried |

## Governance

- `curl` `GET` of a listed health endpoint runs without asking in NORMAL, ALERT and DEGRADED. Any other host, path or method asks. For Neo4j only `GET http://neo4j:7474/` runs without asking.
- `python3` (the probe script), `psql` and `redis-cli` ask the user, in every mode.
- LOCKDOWN and RECOVERY: `bash` is disabled.
