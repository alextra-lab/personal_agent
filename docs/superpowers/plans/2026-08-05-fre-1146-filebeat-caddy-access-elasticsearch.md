# FRE-1146 — Filebeat sidecar ships caddy-access-* to Elasticsearch with template + ILM

**Backing ADR:** ADR-0132 D3 (`docs/architecture_decisions/ADR-0132-outbound-authenticated-egress.md`).
Design intent only — the ADR's own AC-1..5 belong to the seam ticket FRE-1148, not this ticket.

**Revision note:** rewritten after a codex plan-review pass (2026-08-05) found the original
docker.sock-based design was root-equivalent, not the "safe read-only" mechanism it was framed
as. See "Design decisions" below for the replacement approach.

## This ticket's acceptance criteria (from the Linear issue)

* **AC-a**: A request driven through Caddy appears as a queryable document in `caddy-access-*`
  within 60s, with the site/logger identity distinguishable so egress-block traffic can be
  filtered from inbound.
* **AC-b**: `GET caddy-access-*/_settings` shows the intended ILM lifecycle name on the live
  index, and the index template exists with the expected mappings.
* **AC-c**: After `docker compose restart filebeat` (registry intact), a previously-ingested
  tagged request still has exactly one document — no re-ingest duplication.

Not this ticket's: cross-container (caddy + filebeat) recreation survival — that's the ADR's
AC-3, owned by the FRE-1148 seam ticket.

## Grounding (measured, not assumed)

- FRE-1036 convention: `<family>-YYYY-MM` dash-separated monthly indices, client-side bucketing,
  ILM is delete-only (no rollover). New families with no precedent default to **90d retention**
  (`docs/superpowers/plans/2026-07-31-fre-1036-es-ilm-monthly-rollover.md:198-201`).
- Single provisioning mechanism: `scripts/setup-elasticsearch.sh` PUTs
  `docker/elasticsearch/*.json` to ES; invoked by `make up` (`Makefile:118-121`) and manually.
  No Python-side template writer exists or should be added (ADR-0128 "single sanctioned mapping
  path").
- Caddy already logs JSON to stdout only, on every site block, via `log { output stdout; format
  json }` (`config/cloud-sim/Caddyfile`). No file output exists.
- Elastic stack is pinned to `8.19.0` in `docker-compose.cloud.yml:131,161` — Filebeat image
  must match.
- No Filebeat service/config exists anywhere in the repo today — greenfield.
- Docker's `json-file` driver (`max-size 10m`, `max-file 3`, host-level `/etc/docker/daemon.json`,
  not in-repo) writes to `/var/lib/docker/containers/<id>/<id>-json.log`; the container ID is not
  known statically ahead of a compose `up`/recreate. Each container's directory also holds
  `config.v2.json`, whose top-level `"Name"` field is the compose `container_name` prefixed with
  `/` — this is filesystem metadata, not a Docker API call.
- Compose build convention: root-level `Dockerfile.<service>` files with `context: .`
  (`Dockerfile.llmserver`, `Dockerfile.gateway`, `Dockerfile.pwa`; see
  `docker-compose.cloud.yml:218-220,333-335,450-452`).
- `tests/scripts/test_es_templates.py` statically validates ILM families (policy shape, template↔
  policy binding, setup-script registration) by parsing repo JSON + grepping the setup script —
  no live ES touched. `tests/scripts/test_gateway_depends_on.py` is the pattern for compose-service
  assertions: render via `docker compose config` (skipped if no `docker` CLI), parse the YAML.

## Design decisions (revised post-codex-review)

1. **No `/var/run/docker.sock` mount.** The original design used Filebeat's Docker autodiscover
   provider to resolve the caddy container's log path by name, which requires mounting the
   Docker socket. Codex review correctly flagged that a `:ro` bind mount on the socket only
   protects the socket *inode* — every request sent over that socket still reaches the full
   Docker Engine API (container creation with arbitrary host mounts, effectively host-root).
   That is a real privilege-escalation surface for a sidecar whose only job is reading one
   container's logs, and this repo (ADR-0132's whole point) is specifically trying to shrink
   credential/access surfaces, not add a new one.

   **Replacement**: a tiny root-owned entrypoint script, baked into a custom Filebeat image,
   resolves the caddy container ID by scanning the already-mounted, read-only
   `/var/lib/docker/containers/*/config.v2.json` files for `"Name":"/cloud-sim-caddy"` (compose's
   `container_name`), then `export`s the resolved ID and `exec`s Filebeat. Filebeat's config
   natively supports `${ENV_VAR}` interpolation, so `filebeat.yml` references
   `${CADDY_CONTAINER_ID}` directly in its `paths:` entry — no Docker autodiscover, no API access
   of any kind, only the same read-only filesystem mount the design already needed for the log
   files themselves. Re-resolved on every Filebeat start, so `docker compose restart filebeat`
   (AC-c) or a future caddy recreation (seam-ticket concern, not this one) both pick up the
   correct/current container ID.

   **Residual risk, accepted and stated (second codex round, 2026-08-05)**: the mount is the
   whole `/var/lib/docker/containers` directory, not scoped to caddy's subdirectory alone — a
   compromised Filebeat process can read every container's logs and `config.v2.json`, not just
   caddy's. There is no way to mount only caddy's subdirectory without already knowing its ID,
   which is exactly the value being resolved (the same chicken-and-egg problem docker.sock
   autodiscover was solving, just moved from API-level to filesystem-level access). This is a
   strictly smaller surface than docker.sock (read-only, no container creation/exec/host-mount
   capability), and is the trade this decision makes consciously, matching ADR-0132 D2's own
   "consciously accepted trade" pattern rather than claiming a false zero-risk result.

2. **Distinguishing egress vs inbound traffic (AC-a)**: name the `log` directive on the two
   ADR-0132 D1 egress blocks (`:8600`, `:8601`) — `log egress_slm { ... }` / `log egress_artifacts
   { ... }`. Caddy emits a distinct `logger` field (`http.log.access.egress_slm` /
   `.egress_artifacts`) for a named access logger, vs. an unnamed/auto-generated logger name on
   every inbound block — so the test/verification only ever asserts the two known egress values,
   never a specific literal for "the inbound default" (Caddy does not guarantee that value is a
   fixed string). This is the minimal Caddyfile change satisfying "site/logger identity
   distinguishable" without touching routing/timeout/streaming behavior (all load-bearing per
   ADR-0132 D1 — untouched).

3. **Decoding Caddy's JSON payload, defensively.** Filebeat's `container` parser (filestream)
   only extracts the raw log line into `message` — it does not parse nested JSON, and by default
   reads *both* stdout and stderr. Fixes from review:
   - Pin `parsers: - container: {stream: stdout}` (Caddy's access logs are stdout-only; excludes
     any stderr runtime/error output from entering `caddy-access-*` under the same schema).
   - Add `decode_json_fields` targeting `message`, namespaced under `caddy.*` (avoids colliding
     with Filebeat's own `log.*`/`container.*` fields).
   - Add a `drop_event` processor keeping only records whose decoded `caddy.logger` matches
     `^http\.log\.access(?:\.|$)` — a second, content-level filter in case a non-access-log JSON
     line ever reaches stdout.

4. **Mapping: explicit `flattened` for open-ended header maps, not blanket `dynamic: true`.**
   Caddy's `request.headers` and `resp_headers` are attacker-influenced (inbound request headers)
   open-ended key sets — under naive `dynamic: true`, each distinct header name becomes a new
   mapped field, an unbounded-cardinality mapping-explosion risk once any inbound traffic reaches
   the template. Fix: map `caddy.request.headers` and `caddy.resp_headers` explicitly as
   `flattened` (queryable, no per-key field creation). The template keeps `dynamic: true` only for
   genuinely-bounded, non-attacker-controlled top-level fields (matching the
   `slm-requests-index-template.json` rationale for *that* family's own generic fields — Caddy's
   is now scoped tighter given the header risk that family doesn't have).

5. **Registry persistence (AC-c)**: filestream's registry lives under `path.data`
   (`/usr/share/filebeat/data` by default) — mount a new named volume there. A `filebeat restart`
   (AC-c's scenario; caddy itself doesn't restart, so its container ID and log path are unchanged)
   resumes from the persisted offset, so no line is re-read. Stable input `id: caddy-access` on
   the filestream input (ADR's own requirement) — no autodiscover concurrency concern since there
   is exactly one static input definition now, not a per-container-instance template.

6. **Image ownership/permissions.** Filebeat defaults to `--strict.perms=true`, which requires
   its config file to be owned by the user Filebeat runs as with safe permissions. A bind-mounted
   host file (owned by the repo's UID) would fail that check. Fix: build a custom image
   (`Dockerfile.filebeat`, matching this repo's `Dockerfile.<service>` convention) that `COPY`s
   `filebeat.yml` and the resolver entrypoint in at build time with root ownership — sidesteps the
   bind-mount permission mismatch without weakening `--strict.perms`.

7. **Retention: 90d**, the established default for a new family with no existing precedent
   (FRE-1036 convention, see Grounding above). Flagged explicitly (not silently assumed as
   settled) — codex review noted FRE-1036's 90d approvals were owner-approved for named
   *operational* families, not access-log/security-evidence data, and that ILM `min_age` is
   measured from index creation, so late-month documents get less than a full 90 days before
   deletion (a property inherited from the FRE-1036 mechanism itself, not unique to this ticket).
   Proceeding with 90d as the concrete default — reversible via a later ILM policy update, and
   blocking implementation on a retention-value round-trip is disproportionate to the decision's
   stakes — but calling it out explicitly in the PR and the Linear handoff comment so master/owner
   can override at the gate if they weigh security-evidence retention differently.

8. **Healthcheck**: not `pgrep` (unverified whether it exists in the pinned Beats image) and not
   an HTTP probe (Filebeat exposes no HTTP endpoint by default). Use
   `grep -q filebeat /proc/1/cmdline` — `/proc` is always present in a Linux container and `grep`
   is near-universal, so this doesn't depend on unverified image contents.

9. **`restart: unless-stopped`**, matching the `caddy` service's own policy — a crashed Filebeat
   currently has no health-driven recovery, and the whole point of D3 is not losing evidence
   capture silently again (the ADR's own stated precedent: the 2026-05-10 token-emit outage went
   unnoticed for three months).

## Steps

1. **Caddyfile** (`config/cloud-sim/Caddyfile`): name the `log` block in the `:8600` block
   `egress_slm` and the `:8601` block `egress_artifacts`. No other change to those blocks (the
   load-bearing omissions stay untouched).
   - Verify: `docker run --rm -v $PWD/config/cloud-sim/Caddyfile:/etc/caddy/Caddyfile:ro
     caddy:2-alpine caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile`

2. **New file** `docker/elasticsearch/caddy-access-ilm-policy.json`: hot phase `min_age: 0ms` +
   `set_priority: 100`; warm phase `min_age: 32d` (forcemerge to 1 segment + lower priority);
   delete phase `min_age: 90d`; `_meta.retention_days: 90`,
   `_meta.managed_by: scripts/setup-elasticsearch.sh`, `_meta.description` naming FRE-1146 /
   ADR-0132 D3 and the index-creation-age caveat from decision 7. Mirrors
   `monitors-slm-health-ilm-policy.json` (the 90d-retention shape) — the no-warm shape used by
   `slm-requests-ilm-policy.json` only applies to retentions ≤32d, where an earlier warm min_age
   would violate ES's phase-monotonicity rule; 90d has room for the warm phase.

3. **New file** `docker/elasticsearch/caddy-access-index-template.json`: `index_patterns:
   ["caddy-access-*"]`, `priority: 100`, `template.settings` (`number_of_shards: 1`,
   `number_of_replicas: 0`, `index.codec: best_compression`, `index.refresh_interval: 5s`,
   `index.lifecycle.name: caddy-access-policy`), `template.mappings` (`dynamic: true` for
   unknown-but-bounded top-level fields, `dynamic_templates` for `*_id`→keyword,
   `*_ms|*_seconds|*duration*`→float, default-string→keyword ignore_above 1024, mirroring
   `slm-requests-index-template.json`), plus explicit `properties`:
   - `@timestamp` (date)
   - `caddy.logger` (keyword) — the AC-a distinguishing field
   - `caddy.status` (integer)
   - `caddy.duration` (float)
   - `caddy.request.host` (keyword)
   - `caddy.request.uri` (keyword)
   - `caddy.request.remote_ip` (keyword)
   - `caddy.request.method` (keyword)
   - `caddy.request.headers` (**flattened** — decision 4)
   - `caddy.resp_headers` (**flattened** — decision 4)

4. **Register in** `scripts/setup-elasticsearch.sh`: new numbered block (after the existing
   families) — `put_resource "ILM policy: caddy-access-policy" "/_ilm/policy/caddy-access-policy"
   ".../caddy-access-ilm-policy.json"` then `put_and_apply_template "Index template:
   caddy-access-template" "/_index_template/caddy-access-template"
   ".../caddy-access-index-template.json"`, with a comment block matching the existing style
   (ticket ref, retention rationale, ILM-before-template ordering).

5. **New file** `config/filebeat/filebeat.yml`:
   ```yaml
   filebeat.inputs:
     - type: filestream
       id: caddy-access
       paths:
         - /var/lib/docker/containers/${CADDY_CONTAINER_ID}/*.log
       parsers:
         - container:
             stream: stdout

   processors:
     - decode_json_fields:
         fields: ["message"]
         target: "caddy"
         overwrite_keys: true
         add_error_key: true
     - drop_event:
         when:
           not:
             regexp:
               caddy.logger: '^http\.log\.access(?:\.|$)'

   output.elasticsearch:
     hosts: ["http://elasticsearch:9200"]
     index: "caddy-access-%{+yyyy-MM}"

   setup.template.enabled: false
   setup.ilm.enabled: false
   ```
   Header comment: FRE-1146 / ADR-0132 D3, why `${CADDY_CONTAINER_ID}` is resolved by the
   entrypoint rather than Docker autodiscover (no docker.sock — decision 1), why template/ILM
   setup is disabled here (owned by `scripts/setup-elasticsearch.sh`, ADR-0128 single-writer
   rule).

6. **New file** `config/filebeat/resolve-caddy-container.sh`:
   ```sh
   #!/bin/sh
   set -eu
   CONTAINER_NAME="${CADDY_CONTAINER_NAME:-cloud-sim-caddy}"
   CONTAINERS_DIR="${DOCKER_CONTAINERS_DIR:-/var/lib/docker/containers}"
   for cfg in "$CONTAINERS_DIR"/*/config.v2.json; do
     if grep -q "\"Name\":\"/${CONTAINER_NAME}\"" "$cfg" 2>/dev/null; then
       export CADDY_CONTAINER_ID="$(basename "$(dirname "$cfg")")"
       exec filebeat -e -c /usr/share/filebeat/filebeat.yml
     fi
   done
   echo "resolve-caddy-container: no container named ${CONTAINER_NAME} found under ${CONTAINERS_DIR}" >&2
   exit 1
   ```
   `DOCKER_CONTAINERS_DIR` defaults to the real path but is overridable — this is what
   `test_resolve_caddy_container.py` (step 9) points at a fixture directory, and what makes the
   resolver testable without touching the host's real `/var/lib/docker/containers`.
   Comment header: FRE-1146, why this exists instead of docker.sock/autodiscover (decision 1).

7. **New file** `Dockerfile.filebeat` (repo root, matching the `Dockerfile.<service>` +
   `context: .` convention):
   ```dockerfile
   FROM docker.elastic.co/beats/filebeat:8.19.0
   USER root
   COPY config/filebeat/filebeat.yml /usr/share/filebeat/filebeat.yml
   COPY config/filebeat/resolve-caddy-container.sh /usr/local/bin/resolve-caddy-container.sh
   RUN chown root:root /usr/share/filebeat/filebeat.yml /usr/local/bin/resolve-caddy-container.sh \
       && chmod 0644 /usr/share/filebeat/filebeat.yml \
       && chmod 0755 /usr/local/bin/resolve-caddy-container.sh
   ENTRYPOINT ["/usr/local/bin/resolve-caddy-container.sh"]
   ```

8. **`docker-compose.cloud.yml`**: new `filebeat:` service near the `caddy:` block —
   - `build: {context: ., dockerfile: Dockerfile.filebeat}`
   - `container_name: cloud-sim-filebeat`
   - `restart: unless-stopped` (decision 9)
   - `volumes:` — `/var/lib/docker/containers:/var/lib/docker/containers:ro` (log files +
     `config.v2.json` for ID resolution — **no docker.sock**),
     `filebeat_registry_cloud:/usr/share/filebeat/data`
   - `depends_on:` `caddy: condition: service_healthy`, `elasticsearch: condition:
     service_healthy`
   - `healthcheck:` `test: ["CMD-SHELL", "grep -q filebeat /proc/1/cmdline"]` (decision 8)
   - `networks: cloud-sim` (no static IP needed — Filebeat only calls out to ES, nothing calls
     into it)
   New named volume `filebeat_registry_cloud: driver: local` in the bottom `volumes:` section,
   comment noting it's the filestream registry (AC-c).

9. **Tests — static, no live substrate** (FRE-375 compliant):
   - `tests/scripts/test_es_templates.py`: add
     `("caddy-access-ilm-policy.json", "caddy-access-policy", "caddy-access-index-template.json",
     90)` to `ILM_FAMILIES`. Gets the three existing parametrized tests (policy shape, template↔
     policy binding, setup-script registration) for free.
   - New `tests/scripts/test_caddy_access_mapping.py`: parse
     `caddy-access-index-template.json` directly, asserting `caddy.request.headers` and
     `caddy.resp_headers` are `{"type": "flattened"}` (guards decision 4's mapping-explosion fix
     from silently regressing to blanket dynamic mapping).
   - New `tests/scripts/test_filebeat_config.py`: parse `config/filebeat/filebeat.yml` as YAML,
     assert: filestream input `id: caddy-access`; `paths` references
     `${CADDY_CONTAINER_ID}`; `parsers` includes `container` with `stream: stdout`;
     `decode_json_fields` processor targets `message` with `target: caddy`; a `drop_event`
     processor filters on `caddy.logger`; `output.elasticsearch.index ==
     "caddy-access-%{+yyyy-MM}"` (FRE-1036 dash convention); `setup.ilm.enabled` and
     `setup.template.enabled` are both `false` (single-writer rule — mirrors the rationale behind
     `test_no_competing_agent_logs_template_writer` in `test_es_templates.py`).
   - New `tests/scripts/test_resolve_caddy_container.py`: exercise
     `config/filebeat/resolve-caddy-container.sh` directly against a temp directory faking
     `/var/lib/docker/containers/<id>/config.v2.json` (via `CADDY_CONTAINER_NAME` +ancestors on
     `PATH`, or by invoking the script with an overridden base dir if the script is parameterized
     for testability — see implementation note below), asserting: it resolves and exports the
     correct ID when the container name matches, and exits non-zero with no execve when no match
     is found. (Keeps the resolver logic itself under test rather than only asserting the YAML
     that invokes it.)
   - New `tests/scripts/test_filebeat_compose_service.py`, following
     `test_gateway_depends_on.py`'s `docker compose config` render pattern (`skipif` no docker
     CLI): assert the `filebeat` service exists, `depends_on` includes `caddy` and
     `elasticsearch`, `build.dockerfile == "Dockerfile.filebeat"`, `restart ==
     "unless-stopped"`, the containers-dir + registry volume mounts are present, **no**
     `/var/run/docker.sock` mount exists anywhere in the service definition, and
     `filebeat_registry_cloud` is declared as a top-level named volume.

   **Implementation note for testability**: make `CONTAINER_NAME` overridable via
   `CADDY_CONTAINER_NAME` (already in the script) and make the base search directory overridable
   via a second env var (e.g. `DOCKER_CONTAINERS_DIR`, default
   `/var/lib/docker/containers`) so the resolver test can point it at a temp fixture directory
   without touching the real path.

10. **Quality gates**: `make test-file FILE=tests/scripts/test_es_templates.py`,
    `make test-file FILE=tests/scripts/test_caddy_access_mapping.py`,
    `make test-file FILE=tests/scripts/test_filebeat_config.py`,
    `make test-file FILE=tests/scripts/test_resolve_caddy_container.py`,
    `make test-file FILE=tests/scripts/test_filebeat_compose_service.py`, then `make test`,
    `make mypy`, `make ruff-check`, `make ruff-format`, `pre-commit run --all-files`.

11. **Not covered by CI — live verification runbook for master/owner post-deploy** (AC-a/b/c need
    a real docker compose stack; this is the "post-deploy runbook" the build skill's Step 9
    requires, not something this session runs against prod). **Deliberate, not deferred by
    default**: this repo's cloud-sim stack (`cloud-sim-caddy`, `cloud-sim-elasticsearch`, etc.) is
    the live shared dev/prod environment on this VPS, not an isolated staging tier — a build
    session does not `docker compose up --build` a new sidecar into it (that's master's role at
    the merge/deploy gate, matching this project's build/master split: build proves correctness
    statically and hands off an exact runbook; master executes it against the shared stack once
    the PR is merged). Second codex review round flagged deferring all three ACs past merge as a
    residual concern on general engineering grounds; the response here is that this is this
    project's designed division of labor, not a gap being cut — the runbook below is the
    concrete, unambiguous artifact that makes that handoff safe.
    - `docker compose -f docker-compose.cloud.yml build filebeat && docker compose -f
      docker-compose.cloud.yml up -d filebeat` (after `caddy` + `elasticsearch` are healthy), then
      `bash scripts/setup-elasticsearch.sh`.
    - Sanity-check the built config before relying on it: `docker compose -f
      docker-compose.cloud.yml exec filebeat filebeat test config` and `... filebeat test output`.
    - AC-a: hit a **named egress route directly**, not an inbound path — e.g.
      `curl http://127.0.0.1:8600/health?marker=<unique>` (the SLM egress block; loopback-bound
      per the ADR-0132 status update) — wait ≤60s, `GET caddy-access-*/_search` filtered on the
      marker; confirm `caddy.logger == "http.log.access.egress_slm"` distinguishes it from an
      inbound request's logger value.
    - AC-b: `GET caddy-access-*/_settings` → `index.lifecycle.name == "caddy-access-policy"`;
      `GET _index_template/caddy-access-template` → exists with the expected mappings.
    - AC-c: send a uniquely-tagged request, confirm exactly one doc, `docker compose restart
      filebeat`, send a second uniquely-tagged request, re-query: each marker still exactly one
      document.

## Risk / review tier

**Standard** — touches `docker-compose.cloud.yml`, a new custom-built sidecar container,
Elasticsearch index template/ILM (schema-adjacent, with an explicit mapping-explosion mitigation
for attacker-influenced header data), and Caddyfile logging config. Already through one codex
plan-review round (2026-08-05); this revision addresses all 7 blocking findings from that round.
