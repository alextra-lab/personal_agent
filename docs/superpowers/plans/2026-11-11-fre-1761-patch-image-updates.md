# FRE-1761 — Patch-level updates of service images

Date: 2026-11-11. Ticket: FRE-1761. Class: config-only diff (compose pins, tests, comments). The deploy is a
stricter class (`seshat-gateway`-adjacent data stores restart), so master deploys it, one service at a time.

## 1. Scope and result

Each image moves to a later patch of the same release line. No major or minor change.

| Service | Was (running) | Now (pinned) | Notes |
| -- | -- | -- | -- |
| Elasticsearch (production, eval, test, harness fixture) | 8.19.0 | 8.19.25 | one version in all stacks |
| Neo4j (production, eval, test, study) | 5.26.24 (floating tag `5.26-community`) | 5.26.33-community | floating tag is now a pin |
| Redis (production, eval, dev) | 7.4.8 (floating tag `7-alpine`) | 7.4.12-alpine | floating tag is now a pin |
| Grafana (production, dev) | 13.1.3 | 13.1.10 | |
| Tempo (production, dev) | 2.10.7 | 2.10.10 | |
| OTel collector (production, dev) | 0.158.0 | 0.165.0 | |

The newest patch of each line was read from the registry on 2026-11-11 (a probe for the next patch returned
404 for every service). The ticket text said "at least 5.26.28" for Neo4j. The newest tag is 5.26.33.

Out of scope, with the reason:

- **Postgres** (`pgvector/pgvector:pg17`, running 17.9). No change is made. No backup is needed.
- **Caddy** (`caddy:2-alpine`, running 2.11.2). A tag 2.11.3-alpine exists. No reason to move was found, and a
  single-service Caddy recreate caused two outages on 2026-08-09/10.
- **cloudflared** (`:latest`, running 2026.3.0). A newer tag (2026.10.7) exists. It is a separate decision.
- **Filebeat** (`Dockerfile.filebeat`, 8.19.0). Elastic's rule: the Beats version must be lower than or equal
  to the Elasticsearch version. 8.19.0 against 8.19.25 satisfies the rule. Bumping it needs an image rebuild.
- **Mac collector** (owner's binary, 0.158.0, installed by `scripts/mac/install_otelcol.sh`, which pins
  `OTELCOL_VERSION="0.158.0"`). `config/otel/mac-collector-config.yaml` validates on 0.158.0 and on 0.165.0.
  The owner decides when to update the binary and the installer pin. Its header comment no longer says "same pin
  as the VPS".
- **SearXNG**. It goes with FRE-1760.

## 2. AC-1 — release notes read and effect on this deployment

Result: no breaking change and no required action was found for any service.
No service stops for the owner.

Method: three agents read the notes. Every entry below that could touch our configuration was then checked
against our files. The checks in section 3 do not rely on the agents' reading.

### Elasticsearch 8.19.1 to 8.19.25

Read: the release notes of every patch 8.19.1 to 8.19.25 on elastic.co, and the upgrade pages.

- Our use: monthly indices, no rollover. ILM has only `set_priority`, `forcemerge` and `delete`. The Caddy
  template uses `flattened`. No `allocate`, downsample, TSDB, `semantic_text` or inference.
- 8.19.20 "Breaking": ILM `allocate` with `number_of_replicas` removes `auto_expand_replicas`. We have no
  `allocate` action. No effect.
- 8.19.19 "Breaking": the Inference API `secret_parameters`. We do not use the Inference API.
- 8.19.13, 8.19.15, 8.19.19, 8.19.21: new circuit breakers (regexp, wildcard, automaton, fetch phase,
  cardinality) and `search.aggs.max_nested_depth`. With a 1 GB heap, a heavy Grafana query may now return a
  breaker error. This is a risk to watch, not a change to make.
- 8.19.15 and 8.19.22: bundled JDK changes. Our heap options are explicit.
- Security advisories read (ESA-2026-235, -227, -210, -209, -201): fixed by the upgrade. No action beyond it.
- No on-disk format change is documented in any patch.
- Not read: the "Breaking changes in 8.19" page returned 404. The 8.19.25 notes are headed "Coming in 8.19.25".
  The advisories for 8.19.3, .5, .19 and .22 were not opened.

### Neo4j 5.26.25 to 5.26.33

Read: the nine release-note pages and the operations manual pages for dump, load, Docker and plugins. The fetch
tool summarised the pages, so quoted lines are its extractions.

- Fixes in the record layer (5.26.25), the planner (5.26.26, 5.26.32) and index queries (5.26.33). None names
  vector indexes, fulltext indexes, Bolt or Cypher 5 syntax.
- 5.26.33 adds native truncation of transaction log files on rotation.
- Security-only dependency updates in 5.26.26 to 5.26.33.
- No store format change or migration is mentioned. The manual says Neo4j does not support downgrades.
- APOC core is bundled in the image. This was observed: see section 3.

### Redis 7.4.9 to 7.4.12

Read: GitHub release pages for 7.4.9 to 7.4.12. 7.4.10 loaded in part. 7.4.11 and 7.4.12 gave only the security
sections.

- 7.4.9: three security fixes (use-after-free in the unblock flow, `RESTORE`, Lua) and crash fixes.
- 7.4.10: a crafted stream `RESTORE` payload (Streams). 7.4.11: ACL bypass for `XREAD` (we use no ACLs),
  blocked-client use-after-free, a malicious RDB slot id.
- No AOF-specific change and no behaviour change is listed.

### Grafana 13.1.4 to 13.1.10

Read: `CHANGELOG.md` for 13.1.4 to 13.1.10. The upgrade guide for v13 returned 404.

- The changelog has no entry for `GF_AUTH_JWT_*`, basic auth, router logging, or file provisioning.
- Many "Security: Fix CVE-..." lines give no component. They cannot be mapped to our setup from the text.
- 13.1.6 and 13.1.10 "Provisioning" entries look like the Git-sync feature, not our file provisioning. This is
  an inference from the entry text.
- The Grafana docs give no downgrade procedure. They advise a database backup before an upgrade.

### Tempo 2.10.8 to 2.10.10

Read: `CHANGELOG.md` and the three GitHub release pages.

- All three patches are dependency and Go security updates. No config key, default or block-format change.

### OTel collector 0.159.0 to 0.165.0

Read: `CHANGELOG.md`. Components we use: receiver `otlp` (gRPC and HTTP), processors `batch` and
`attributes/redaction`, exporters `otlp_grpc/tempo` and `debug`, one traces pipeline.

- 0.165.0 "Breaking": removal of deprecated `confighttp` fields (`IdleConnTimeout`, `MaxIdleConns` and others).
  We set none. No effect.
- 0.162.0: keepalive is on by default for `confighttp`. No action found.
- 0.162.0 "Breaking": `queuebatch` renamed to `queue_batch`. We use `batch`.
- 0.165.0: a start-up warning when `batch` is combined with exporter batching. Our exporter sets no batching.

## 3. Checks done before the deploy (observed)

All checks used scratch containers and copies. Nothing in production was stopped, changed or written.

| Check | Observed |
| -- | -- |
| OTel `validate` of `config/otel/collector-config.yaml` | exit 0 on 0.165.0 and on 0.158.0 |
| OTel `validate` of `mac-collector-config.yaml` | exit 0 on 0.165.0 and on 0.158.0 |
| Tempo 2.10.10 with `docker/tempo/tempo.yaml` on empty storage | log line "Tempo started" and two warnings (worker address, WAL replay). 2.10.7 on the same config prints the same two warnings |
| Grafana 13.1.10 with the real `provisioning/` and `dashboards/` | `/api/health` ok, version 13.1.10, 17 dashboards, 10 datasources, Elasticsearch plugin starts |
| Grafana 13.1.3 with the same files | prints the same `provisioning/plugins` error line, so it is not new |
| Redis 7.4.12 on a copy of the production AOF volume | loads the base and incremental AOF, 21 keys (production: 21), `PONG` |
| Neo4j 5.26.33 on an empty volume | `5.26.33` kernel, `apoc.version()` is `5.26.33`, vector index created |
| Neo4j dump then load into a fresh volume (section 4) | 50 nodes and the vector index came back |
| Elasticsearch 8.19.25 on a copy of the production data volume | green, 192 of 192 shards, 155 indices (production: 155) |
| `pytest` on compose and config tests | 64 passed (includes a real Grafana 13.1.10 container for the JWT test) |

Two caveats. First, a Tempo `-config.verify` flag does not exist, so the Tempo check is a real start. Second,
the Grafana probe could not reach Elasticsearch, so its "dial" errors on the Elasticsearch datasource are
expected there.

## 4. AC-3 — backup step and rollback image

Total backup size is about 2.2 GB (ES 892 MB, Neo4j 962 MB, Grafana 117 MB, Redis 115 MB, Tempo 99 MB). The
volume `/var/lib/docker` has 31 GB free. Write backups outside every Docker volume:
`BK=$HOME/seshat-backups/fre1761-$(date -u +%Y%m%dT%H%M%SZ)`.

Neither Elasticsearch nor Neo4j supports a downgrade of a data directory. The rollback for both is a restore of
the pre-update backup, then the old image. Do not run `docker image prune` before the deploy is verified: it
removes the old images.

| Service | Backup, taken with the service stopped | Rollback image | Rollback steps |
| -- | -- | -- | -- |
| Elasticsearch | `docker stop cloud-sim-elasticsearch` then `docker run --rm -v seshat_es_data_cloud:/from:ro -v $BK:/to alpine tar czf /to/es_data.tgz -C /from .` | `docker.elastic.co/elasticsearch/elasticsearch:8.19.0` | Restore the tarball into an emptied volume, revert the pin, `up -d --no-deps elasticsearch`. |
| Neo4j | `docker stop cloud-sim-neo4j`. Make the target owned by the image user: `docker run --rm -v $BK:/b alpine chown 7474:7474 /b`. Then `docker run --rm -v seshat_neo4j_data_cloud:/data -v $BK:/backups neo4j:5.26.33-community neo4j-admin database dump neo4j --to-path=/backups`. Repeat with `system` in place of `neo4j`. | `neo4j:5.26.24-community` | `neo4j-admin database load neo4j --from-path=/backups --overwrite-destination=true` (and `system`) on the stopped volume, revert the pin, `up -d --no-deps neo4j`. |
| Grafana | `docker stop cloud-sim-grafana`, then tar `seshat_grafana_data_cloud` as for Elasticsearch | `grafana/grafana:13.1.3` | Restore the tarball, revert the pin. A patch may migrate the Grafana database, so revert the image only with the restored volume. |
| Redis | `docker stop cloud-sim-redis`, then tar `seshat_redis_data_cloud` | `redis:7.4.8-alpine` | Revert the pin. Restore the tarball if the AOF does not load. |
| Tempo | none needed (no format change in 2.10.8 to 2.10.10). A tar of `seshat_tempo_data_cloud` is cheap if master wants it. | `grafana/tempo:2.10.7` | Revert the pin. |
| OTel collector | none (no state) | `otel/opentelemetry-collector:0.158.0` | Revert the pin. |
| Postgres | not in scope | — | — |

The Neo4j procedure was rehearsed on scratch volumes with the 5.26.33 image. One failure was found and the
procedure above includes its fix: a dump into a root-owned directory fails with `AccessDeniedException` because
the image runs as the `neo4j` user. The `system` database dump and the Elasticsearch, Grafana and Redis tar
commands were not rehearsed. The tar form is standard, and the Redis and Elasticsearch volumes were copied with
`cp -a` in the checks of section 3.

The Neo4j, Redis and Elasticsearch images that production runs now stay in the local image cache until a prune.
The old Neo4j and Redis images are under the floating tags. Use the explicit rollback tags in the table.

## 5. Deploy order and checks for master (AC-4, AC-5)

Use one command per service and never a bare `up -d <service>`. Compose recreates a dependency whose config
changed, and Grafana depends on Tempo, Elasticsearch and Postgres. The command is
`docker compose -f docker-compose.cloud.yml up -d --no-deps <service>`. Do not touch Caddy.

Record the baseline before the first change:

- `curl -s localhost:9001/health`. Baseline on 2026-11-11: `database`, `elasticsearch` and `neo4j` are
  `connected`, `second_brain` is `running`, and `mcp_gateway` is `disconnected`. The last one is expected:
  `AGENT_MCP_GATEWAY_ENABLED` is `false`. "Every component connected" means every enabled component.
- `docker exec cloud-sim-redis redis-cli dbsize`. Baseline: 21 keys (the value changes with live use).

Order, with the lowest risk first. Check each service before the next one:

1. Redis. Check `redis-server --version` is 7.4.12, `redis-cli ping` is `PONG`, `dbsize` is close to the baseline,
   healthcheck `healthy`.
2. OTel collector. Check the container is running and the log has no error.
3. Tempo. Check the container is running and the log line "Tempo started" appears.
4. Grafana. Check `curl -s localhost:3003/api/health` shows version 13.1.10, and the dashboard search
   (`/api/search?type=dash-db`, basic auth) returns 17.
5. Elasticsearch (after the backup). Check `curl -s localhost:9200` shows 8.19.25, `/_cluster/health` is green,
   and the index count is 155 or more.
6. Neo4j (after the backup). Check `docker exec cloud-sim-neo4j neo4j --version` is 5.26.33 and the healthcheck is
   `healthy`.

After each of steps 1, 5 and 6, run `/health` again. The gateway reconnect behaviour after a restart was not
tested here. If a component stays `disconnected`, record it and decide on a gateway restart.

AC-5, trace check. Tempo receives a trace every few minutes without a prompt (3 traces were seen about 14 minutes
before the last check). After steps 2 and 3, run
`docker exec cloud-sim-grafana wget -qO- "http://tempo:3200/api/search?limit=3"` and check that the newest
`startTimeUnixNano` is later than the time of step 3. If no trace arrives, ask the owner for a live turn. Do not
fire one without an explicit OK.

Eval and test stacks: recreate `*-eval` containers with the eval compose file, and the test stack with
`make test-infra-down` then `make test-infra-up`. They open the existing volumes with the new patch. If a test
volume does not open, `make test-infra-reset` wipes it.

## 6. Risks that remain

- The Elasticsearch circuit breakers added in 8.19.13 to 8.19.21 may reject a heavy Grafana query on the 1 GB heap.
  Watch the Elasticsearch dashboards after step 5.
- Neo4j and Elasticsearch are down during their backup and restart. Writers see an outage. Master picks the time.
