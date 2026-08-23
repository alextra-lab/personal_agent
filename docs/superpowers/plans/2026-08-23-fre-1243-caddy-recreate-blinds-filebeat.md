# FRE-1243 — Recreating Caddy silently blinds the access-log pipeline until Filebeat is restarted

**Backing ADR:** ADR-0132 D3 (`docs/architecture_decisions/ADR-0132-outbound-authenticated-egress.md`)
— "Caddy access logs are captured into Elasticsearch." D3's stated guarantee: *"once ingested,
evidence survives recreation of both containers, without duplication."* That sentence is the
design intent this ticket must keep satisfying (and, with the chosen remedy, exceed — see Design
decision below).

## Ticket acceptance criteria (from Linear FRE-1243, verbatim conditions summarized)

| # | Criterion |
|---|---|
| AC-1 | Recreating Caddy alone no longer stops the access-log flow — no manual Filebeat action |
| AC-2 | The bug is shown to reproduce before the fix, on the same recipe |
| AC-3 | The evidence trail is unchanged in shape (same `caddy.*` fields/types), not merely present |
| AC-4 | Whichever remedy is chosen, the choice is recorded with its reasoning |
| AC-5 | No new silent-failure mode is introduced in place of the old one |

## Grounding (measured this session)

- `config/filebeat/filebeat.yml` resolves `${CADDY_CONTAINER_ID}` once via
  `config/filebeat/resolve-caddy-container.sh`, exec'd as `Dockerfile.filebeat`'s `ENTRYPOINT`
  (runs as root — needed today only to read root-owned `config.v2.json` under the read-only
  `/var/lib/docker/containers` mount). Recreating Caddy changes its container ID; Filebeat keeps
  tailing the stale path.
- `docker-compose.cloud.yml`: `caddy` (443/80, `depends_on: seshat-gateway, seshat-pwa`),
  `filebeat` (`depends_on: caddy, elasticsearch`, mounts `/var/lib/docker/containers:ro` +
  `filebeat_registry_cloud`). No `depends_on`-driven recreate-cascade exists in Compose — vanilla
  Compose never recreates a dependent just because its dependency was recreated.
  `docker compose restart caddy` (the documented `CLOUD_DEPLOYMENT.md` git-pull procedure) does
  **not** change the container ID (`restart` ≠ recreate) — the bug requires an actual recreate
  (image/env/volume/compose-file change), which is exactly what FRE-1244's own deploy did to
  every service, caddy included.
- `config/cloud-sim/Caddyfile` has 7 `log`/named-`log` **directives** (`routing` snippet used by
  `localhost`+`{$AGENT_HOST}`, plus `{$GRAPH_HOST}`, `{$ES_HOST}`, `{$OTLP_HOST}`, `{$API_HOST}`,
  `egress_slm` on `:8600`, `egress_artifacts` on `:8601`), every one `output stdout` / `format
  json` (or `format filter {...} wrap json` for OTLP's header redaction) — but `caddy adapt`
  (measured) compiles to **8 distinct loggers**, since the `routing` snippet's one `log{}`
  directive is imported into two separate site blocks (`localhost` and `{$AGENT_HOST}`) and each
  site compiles its own logger instance.
- ES contract (`docker/elasticsearch/caddy-access-index-template.json`): only `@timestamp` and
  `caddy.{logger,status,duration,resp_headers,request.{host,uri,remote_ip,method,headers}}` are
  typed/governed. Nothing outside `caddy.*` is a consumer contract (confirmed no other doc/ADR
  references filebeat's own `log.*`/`agent.*`/`input.*` envelope fields for this index).
- `caddy:2-alpine` runs as **root** by default (measured: `docker run --rm caddy:2-alpine id`).
  `docker.elastic.co/beats/filebeat:8.19.0`'s own default entrypoint is
  `tini -- docker-entrypoint`, default user **uid 1000** (measured via `docker inspect`) — but
  today's custom `ENTRYPOINT` overrides both, running Filebeat as root so it can read the
  root-owned containers directory. `filebeat_registry_cloud` is an existing **persistent**
  production volume already root-owned from that history — do not flip Filebeat's runtime user in
  this change; that's an unrelated hardening step with its own blast radius (existing-volume
  ownership) and isn't required once the containers-dir mount is gone.
- No local (non-`.cloud.yml`) compose stack has Caddy or Filebeat (`docker-compose.yml` has no
  such services) — there is no way to exercise this pairing against the FRE-375 test-ES stack;
  reproduction needs its own throwaway 3-container harness, isolated from the live
  `cloud-sim-*` containers (this worktree runs on the same VPS host as production).
- Test conventions: `tests/scripts/test_filebeat_config.py` (static YAML parse) and
  `tests/scripts/test_filebeat_compose_service.py` (`docker compose config` render, no live
  containers) cover the current mechanism and will need updates. `tests/integration/test_fre1070_*`
  is the established pattern for a live-stack acceptance test: module-level
  `pytestmark = pytest.mark.integration`, a reachability skip helper, docstring naming the exact
  bring-up command — excluded from `make test` (`-m "not integration"`), run manually with
  `PERSONAL_AGENT_INTEGRATION=1`.

## Design decision (AC-4)

**Chosen: Option 3 — drop the container-ID indirection. Caddy writes its access log to a fixed
path in a Docker named volume; Filebeat tails that fixed path.** No container-ID resolution, no
`/var/lib/docker/containers` mount, no `resolve-caddy-container.sh`.

**Why not the others:**
- **Option 1 (`depends_on`/restart coupling) — empirically tested, not just reasoned about.**
  Compose 2.17+ (this VPS runs Compose v5.1.4, confirmed via `docker compose version`) does have a
  native `depends_on.<service>.restart: true` — codex's plan-review round correctly flagged that I
  had missed this. But its actual trigger condition matters, and the official docs are ambiguous
  on it, so it was verified directly with a throwaway two-service compose stack (`dep` /
  `dependent`, `dependent` declaring `depends_on: dep: {restart: true}`) in
  `/tmp/.../scratchpad/compose-restart-test` (not committed):
  - `docker compose up -d --force-recreate dep` (naming **only** `dep`) → `dep` gets a new
    container ID; `dependent`'s ID and start time are **unchanged**.
  - `docker compose up -d dep` after changing `dep`'s own config (a real recreate-on-diff, not a
    forced one), naming **only** `dep` → same result: `dependent` untouched.
  - `docker compose up -d` naming **both** (or neither, i.e. the whole stack) → `dependent` *does*
    restart alongside `dep`'s recreate.

  So `restart: true` only cascades when the dependent is in the same invocation's scope — it does
  **not** fire on a single-service recreate. **Codex round 2 (minor, area 1) correctly narrowed
  this claim**: this project's deploy tooling is not *exclusively* single-service —
  `infrastructure/scripts/deploy.sh`'s unscoped `up -d` (`make deploy`, `make build-full`) targets
  the whole stack and *would* cascade a `restart: true` since both services are in scope together.
  The precise claim is narrower but still decisive: this project **also** supports (and per the
  ticket's own history, has twice actually used) single-service targeting of Caddy specifically —
  that's exactly how FRE-1239 and FRE-1244 triggered this bug, caddy alone, recreated alone — and
  `restart: true` provides no protection on that path. Adopting it would mean the safety of every
  future Caddy-only operation depends on nobody ever reaching for the single-service form again,
  which is the identical "someone has to remember, forever" fragility the ticket already documents
  failing twice, just relocated from a custom cascade script to a discipline about which command
  form to use. Option 3 needs no such discipline on any path, scoped or unscoped.
- **Option 2 (read-time resolution via symlink/glob):** still needs the root-owned
  `/var/lib/docker/containers` mount this design is trying to drop, and `filestream` does not
  reliably re-detect an inode swap behind a symlink without an added refresher process — more
  moving parts for a weaker guarantee than Option 3.
- **Option 4 (liveness alert):** detection, not prevention — the gap is still live for the check's
  interval, and AC-5 would then require its own seeded-negative proof. Complementary to any
  preventive fix, not a substitute; not needed once Option 3 removes the failure mode structurally
  (AC-5 is satisfied by construction — see below).

**AC-5 (no new silent-failure mode):** Option 3 is purely preventive — it removes the
coordination requirement rather than adding a new detector. There is no new "has it fired"
question to prove on a seeded negative, because there is no new failure mode: the log's existence
no longer depends on any fact about the currently-running Caddy container's identity. Filebeat
keeps running as root (unchanged — see the grounding note on `filebeat_registry_cloud`'s existing
ownership); Option 3 removes the *reason* Filebeat needed root (reading a root-owned host
directory to resolve a container ID) without itself flipping the runtime user, since that's an
unrelated hardening step with its own blast radius against a persistent production volume.

## Implementation

### 1. `config/cloud-sim/Caddyfile`
Every `log { output stdout ... }` / `log egress_slm { output stdout ... }` / `log
egress_artifacts { output stdout ... }` block (8 loggers total — the `routing` snippet is imported by both `localhost` and `{$AGENT_HOST}`, each compiling to its own logger) → identical sub-block content across all
seven, so there is one consistent writer config for the shared target file (verified this doesn't
error: `caddy adapt` accepts it, and it's structurally the same thing already proven safe today —
7 independent loggers already concurrently write interleaved JSON lines into one shared sink,
`stdout`; moving the sink from stdout to a file doesn't change that concurrency shape):

```
log {
    output file /var/log/caddy/access.log {
        mode 644
        roll_uncompressed
    }
    format json
}
```

(and the same `output file ... { mode 644; roll_uncompressed }` sub-block for the `egress_slm` /
`egress_artifacts` named loggers, and for the OTLP block whose `format` stays `filter {...} wrap
json`). `roll_uncompressed` matters, not cosmetic: **measured** (throwaway `caddy:2-alpine`
container, `roll_size 1KiB` — which Caddy's Caddyfile adapter rounds to its minimum 1 MB
granularity, `"roll_size_mb":1` in the adapted JSON — driven past that threshold with ~3,000
requests) that Caddy's default rolled-backup naming is `access-<RFC3339-ish-timestamp>-size.log`
(confirmed exactly, e.g. `access-2026-08-23T13-20-16.994-size.log`) and rolled backups are
**gzip-compressed by default** — `filestream` cannot read `.gz` content as lines, so without
`roll_uncompressed` a rotation that happens while Filebeat is down would permanently strand that
segment's evidence. `mode 644` matters because Caddy's default file mode is **`0600`**
(also measured) — root-owned and unreadable by anything but root or the owning process; harmless
today since Filebeat keeps running as root, but wrong to leave undocumented/implicit for a file
whose only sensitivity is already accepted (D3: "avoids storing the injected CF secret" — the
incoming-request log was never meant to be root-only).

### 2. `docker-compose.cloud.yml`
- New top-level volume `caddy_logs_cloud` (comment: FRE-1243, shared fixed-path log volume,
  read-write for caddy, read-only for filebeat, removes the container-ID indirection).
- `caddy` service `volumes:` gains `- caddy_logs_cloud:/var/log/caddy`.
- `filebeat` service: remove `- /var/lib/docker/containers:/var/lib/docker/containers:ro`; add
  `- caddy_logs_cloud:/var/log/caddy:ro`.
- Update the sidecar's explanatory comment block (currently describing container-ID resolution)
  to describe the fixed-path mechanism.
- Leave `depends_on`/`restart`/healthcheck/`filebeat_registry_cloud` untouched — still correct.

### 3. `config/filebeat/filebeat.yml`
- `paths: - /var/lib/docker/containers/${CADDY_CONTAINER_ID}/*.log` → `paths: -
  /var/log/caddy/access.log - /var/log/caddy/access-*.log`. The second glob matches
  `roll_uncompressed` rotated backups (measured naming above) — normally `filestream` keeps
  tailing the pre-rotation file via its already-open handle regardless of the glob, but the glob
  is what lets Filebeat pick up a segment it missed entirely (e.g. it was down across the
  rotation) rather than silently losing it — closing exactly the gap codex's plan-review flagged.
- Remove the `parsers: - container: {stream: stdout}` key entirely (no Docker json-file envelope
  to unwrap — the file already contains Caddy's raw JSON lines, so `message` already holds what
  `decode_json_fields` expects).
- **New, required — `@timestamp` processor (codex round 2 blocking finding, area 3).** The
  `container` parser being removed was doing double duty: besides unwrapping Docker's envelope, it
  was also the thing setting `@timestamp` from Docker's own per-line `time` field (close to true
  event time). A plain `filestream` input has no such behavior — its default `@timestamp` is
  Filebeat's own harvest/processing time. Under normal live tailing that's sub-second-close to the
  real event time and unnoticeable, but a backlog (Filebeat down, then catching up on buffered
  lines) would stamp every backlogged event with ~now instead of when Caddy actually logged it —
  and since `output.elasticsearch.index` is `caddy-access-%{+yyyy-MM}`, a backlog spanning a month
  boundary would silently misfile into the wrong monthly index. Caddy's JSON access log already
  carries its own Unix-epoch timestamp at the top level (`ts`, decoded to `caddy.ts` by the
  existing `decode_json_fields` step). Add, right after `decode_json_fields`:
  ```
  - timestamp:
      field: caddy.ts
      layouts:
        - UNIX
  ```
  so `@timestamp` is sourced from Caddy's own recorded event time, exactly matching what the
  removed `container` parser was effectively providing.
- `drop_event` / `output.elasticsearch` / `setup.template.enabled` / `setup.ilm.enabled`
  unchanged.
- Update the header comment (currently describes `resolve-caddy-container.sh` and
  `${CADDY_CONTAINER_ID}`) to describe the fixed-path mechanism.

### 4. `Dockerfile.filebeat`
Remove the `resolve-caddy-container.sh` `COPY`/`chown`/`chmod` lines and the custom-script
`ENTRYPOINT`. Keep `USER root` + the `filebeat.yml` `COPY`/`chown root:root`/`chmod 0644` (still
required for Filebeat's `--strict.perms` check on the config file — unrelated to container-ID
resolution). New `ENTRYPOINT ["filebeat", "-e", "-c", "/usr/share/filebeat/filebeat.yml"]` —
direct exec, PID 1 is `filebeat`, so the existing compose healthcheck (`grep -q filebeat
/proc/1/cmdline`) keeps working unchanged. Runtime user stays root (unchanged), for the
`filebeat_registry_cloud` ownership reason grounded above.

### 5. Delete `config/filebeat/resolve-caddy-container.sh`
Dead code once step 4 lands — created dead by this change, so it's mine to remove (not
pre-existing dead code).

### 6. `docs/architecture_decisions/ADR-0132-outbound-authenticated-egress.md`
Three passages describe the now-superseded mechanism (codex's plan-review round caught that my
first draft only planned to touch the first):
- **D3's mechanism paragraph** (~line 245): replace "a `filestream` input with the `container`
  parser over the Docker json-file logs... (the legacy `container` input type is deprecated)"
  with the fixed-path mechanism, and strengthen the stated guarantee — it now survives recreation
  of Caddy **alone**, not only "both containers together" as originally written.
- **The risk table** (~line 444, "Filebeat dies silently and evidence goes dark again"):
  **codex round 2 (minor, area 5) caught that my first revision's proposed wording didn't actually
  mitigate the stated risk** — "Caddy recreation no longer needs Filebeat cooperation" answers a
  *different* failure (Caddy recreating) than the row's own subject (*Filebeat* dying). Correct
  mitigation text: the fixed-path volume now buffers unread content (active file + uncompressed
  rotated backups) across a Filebeat outage, bounded by Caddy's rotation retention (see the
  accepted-limit note in Tests/D above) rather than being erased outright by a Caddy recreation as
  before; Filebeat's existing compose healthcheck still detects the dead-process case. Leave the
  row's separate "alertable in Kibana" clause untouched even though it's independently stale
  (Kibana retired, FRE-1214) — that staleness predates this ticket and isn't something this
  change makes newly inaccurate; touching it here would be scope creep past what CLAUDE.md's
  surgical-changes rule allows.
- **AC-3's own procedure** (~line 529-534, the seam ticket FRE-1148's criterion, not this
  ticket's to rewrite outright): the described check (`--force-recreate caddy filebeat`) gains a
  parenthetical noting the mechanism now makes a Caddy-only recreate sufficient too — FRE-1148
  owns re-verifying its own AC-3 against the new mechanism. Also fix the specific phrase codex
  flagged as now false: it currently says AC-3 tests something the old mechanism explicitly could
  **not** provide ("lossless... which the mechanism cannot provide" territory) — replace with "not
  lossless across an outage exceeding Caddy's configured rotation retention, but a Caddy-only
  recreate no longer erases anything at all" so the ADR's prose matches what actually ships.

Add a dated amendment marker in the ADR's status/changelog area per this repo's convention (e.g.
ADR-0129's "D6 amended 2026-08-07" style), citing FRE-1243.

### 7. `docs/guides/CLOUD_DEPLOYMENT.md`
Add a short note under "6. Caddy Reverse Proxy" (or its own subsection) stating access logs now
ship via a fixed-path volume tailed by Filebeat, recreating Caddy needs no companion Filebeat
action, and — for anyone debugging — `docker exec cloud-sim-caddy tail -f
/var/log/caddy/access.log` replaces `docker logs cloud-sim-caddy` for access-log visibility
specifically (application/runtime logs stay on stdout, only the access log moved).

### 8. Required one-time migration step on first deploy (codex round 2 blocking finding, area 1)
**Grounded in `infrastructure/scripts/deploy.sh`, measured this session:** plain `make deploy`
(no flags) runs `docker compose -f $COMPOSE_FILE up -d` — unscoped (names no service), no image
rebuild. Because this PR changes `filebeat`'s compose-level config (its volume list), an unscoped
`up -d` **would** recreate the `filebeat` container on the very next plain deploy — but using
whatever `cloud-sim-filebeat` image is already built on the VPS, i.e. the **stale pre-fix image**,
which still runs `resolve-caddy-container.sh` as its entrypoint. That script scans
`/var/lib/docker/containers` for a match — a mount this PR removes — finds nothing, and `exit 1`s;
Filebeat would crash-loop until rebuilt, an outage of exactly the evidence trail this ticket
exists to protect, self-inflicted by the fix's own rollout. `make build-full` (rebuilds every
image before `up -d`) is unaffected; only the flag-less fast path is the hazard.

**This is not a code fix — it's an explicit instruction for whoever deploys this PR** (master, per
this project's merge/deploy ownership): the deploy that lands this change must rebuild `filebeat`
before or as part of recreating it —
`docker compose -f docker-compose.cloud.yml build filebeat && docker compose -f
docker-compose.cloud.yml up -d --force-recreate caddy filebeat` (or `make build-full`) — **not** a
plain `make deploy`. State this explicitly, prominently, in the PR body and the Linear handoff's
post-deploy runbook (skill Step 8) as the first instruction, not buried among the routine
verification steps.

## Tests

### A. `tests/scripts/test_filebeat_config.py` (static — update)
- Replace `test_input_path_uses_resolved_container_id_placeholder` with
  `test_input_path_is_fixed_caddy_log_path`: asserts `paths == ["/var/log/caddy/access.log",
  "/var/log/caddy/access-*.log"]` (both the active file and the rotated-backup glob — codex round
  2 caught that my prior draft of this test asserted only the first element, contradicting the
  implementation section above; fixed here to match).
- Replace `test_container_parser_scoped_to_stdout` with `test_no_container_parser_present`:
  asserts `"parsers" not in config["filebeat.inputs"][0]`.
- New `test_timestamp_processor_sources_caddy_ts`: asserts a `timestamp` processor is present in
  `config["processors"]` with `field == "caddy.ts"` and `"UNIX"` in `layouts`, ordered after the
  `decode_json_fields` processor (index comparison) — the `@timestamp`-fidelity regression guard.
- Other three tests unchanged (drop_event, index convention, template/ILM disabled).

### B. `tests/scripts/test_filebeat_compose_service.py` (render-only — update)
- Replace `test_containers_dir_mounted_read_only` with
  `test_caddy_log_volume_shared_fixed_path`: renders compose, asserts `caddy_logs_cloud` is
  mounted at `/var/log/caddy` on **both** `caddy` (no `read_only`/writable) and `filebeat`
  (`read_only: True`), and is declared under top-level `volumes`.
- `test_no_docker_socket_mount_anywhere` unchanged — still a valid regression guard.
- New `test_containers_dir_no_longer_mounted`: asserts no volume entry anywhere in the rendered
  `filebeat` service names `/var/lib/docker/containers` — the explicit negative, so a future
  change can't silently reintroduce the old coupling.

### C. `tests/scripts/test_caddyfile_logs_to_fixed_path.py` (new, static — revised per codex finding)
Regex/text parsing of a Caddyfile is exactly the fragility codex's plan-review round flagged
(the OTLP block's nested `format filter {...} wrap json` is easy to mis-match). This repo already
has the right tool wired into CI (`.github/workflows/ci.yml`'s `caddy-validate` job runs `docker
run --rm caddy:2-alpine caddy validate --config /etc/caddy/Caddyfile` on every Caddyfile-touching
PR — syntax only, no semantic assertion). This test reuses the identical `caddy:2-alpine` image
but calls `caddy adapt` instead of `validate`, parses the resulting JSON with Caddy's **own**
parser (not a regex), and asserts semantically: every entry in `logging.logs` other than the
literal `"default"` key (Caddy's global fallback logger, which never carries `output stdout` here)
has `writer.output == "file"`, `writer.filename == "/var/log/caddy/access.log"`, `writer.mode ==
"0644"` (or however `caddy adapt` renders the mode field — confirm the exact key/format from a
manual `caddy adapt` run before writing the assertion), and `writer.roll_gzip == False`.
**Codex round 2 (minor, area 4) caught that Caddy pools file writers by filename** — all 8 loggers
targeting the identical path share ONE underlying writer, so if any single block's sub-options
were fat-fingered differently, whichever config's writer opens first silently wins for everyone
else, with no error. Asserting every non-`default` entry's writer object is **identical** (not
just present) is what actually catches that authoring mistake — assert `len(writers) == 8` first
(codex round 3: the uniqueness check alone would still pass on 6 identical writers if one block
were accidentally dropped) **and then** `len({json.dumps(w, sort_keys=True) for w in writers}) ==
1` in addition to the per-field checks. `skipif` no `docker`
CLI, matching `test_filebeat_compose_service.py`'s convention. TDD red/green: fails against the
pre-fix Caddyfile (`writer.output == "stdout"`), passes after step 1.

### D. `tests/integration/test_fre1243_caddy_recreate_reproduction.py` (new, live, `integration`-marked)
Follows the `test_fre1070_otel_collector_acceptance.py` pattern: module-level `pytestmark =
pytest.mark.integration`, docstring naming the bring-up command, a reachability skip helper.
Requires a **throwaway, isolated 3-container harness** — never the live `cloud-sim-*` stack.

**Codex's plan-review round caught a real flaw in the first draft here**: a single committed
harness whose Caddyfile already writes to a file cannot also stand in for the *pre-fix* mechanism
(stdout + container-ID resolution) — running "the same test" against it wouldn't reproduce the
actual bug, it would just fail for an unrelated reason (the pre-fix resolver would need a
`/var/lib/docker/containers` mount and a matching `CADDY_CONTAINER_NAME` that the file-based
harness never provides). Splitting into two harnesses — one per mechanism — is the correct fix,
and only one of them is a permanent, committed artifact:

**Permanent, committed — proves AC-1/AC-3 forever (post-fix mechanism):**
- `tests/scripts/fixtures/fre1243_harness/docker-compose.harness.yml` — `caddy` (image
  `caddy:2-alpine`, bind-mounts `Caddyfile.harness`, named volume for `/var/log/caddy`, port
  `18080:80`), `filebeat` (`build: {context: ../../../.., dockerfile: Dockerfile.filebeat}` — the
  **real**, post-fix, production Dockerfile/config, unmodified, so the test proves the shipped
  artifact, not a reimplementation), `elasticsearch` (single-node, `discovery.type=single-node`,
  `xpack.security.enabled=false`, small heap, ephemeral, service name **must** be `elasticsearch`
  since that's hardcoded in `filebeat.yml`'s `output.elasticsearch.hosts`). No named `cloud-sim`
  network override — Compose's project-scoped default network keeps this fully isolated from the
  live stack; brought up with an explicit distinct project name (`-p fre1243-harness`) so
  container names (`fre1243-harness-caddy` etc., set explicitly in the harness file) can never
  collide with `cloud-sim-*`.
- `tests/scripts/fixtures/fre1243_harness/Caddyfile.harness` — trivial `:80 { respond "ok" 200 ;
  log { output file /var/log/caddy/access.log { mode 644 ; roll_uncompressed } ; format json } }`
  — the identical `log` directive shape production uses post-fix.
- `tests/scripts/fixtures/fre1243_harness/pre_fix_caddy_field_baseline.json` — a **committed,
  one-time-captured** snapshot of one pre-fix ingested document's `caddy.*` **leaf field names
  only** (captured in the throwaway pre-fix run below) — not types, since types are checked
  separately, deterministically, against the real index template (see the AC-3 split below). This
  is the AC-3 field-presence baseline — evidence, not code, checked in the way a fixture is.
- **Both harnesses install the real `docker/elasticsearch/caddy-access-index-template.json`**
  before first ingestion (a single `PUT _index_template/caddy-access-template` using the repo's
  own committed file — no ILM policy needed for a short-lived throwaway index, ILM only governs
  rollover/deletion timing, irrelevant here). **Codex round 2 (blocking, area 3) caught that
  leaving both harnesses on dynamic mapping doesn't test the real contract** — dynamic mapping
  could plausibly infer `status`/`duration` as `long` vs `double` depending on the exact JSON
  literal, independent of any real regression, and never proves the *governed* types (`status:
  integer`, `duration: float`, headers `flattened`) that ADR-0132 D3's consumers (Grafana panels
  reading `caddy-access-*`) actually depend on. Installing the real template makes both harnesses'
  ES behave exactly like production's index for typing purposes, and turns the AC-3 type check
  into something deterministic rather than baseline-dependent.
- AC-3 now splits into two independently-checked halves, per codex's correction that a single
  "exactly match" assertion conflated two different things:
  - **Types** (deterministic, no baseline needed): `GET caddy-access-*/_mapping` on the **post-fix**
    harness; assert `caddy.status`/`caddy.duration`/`caddy.request.host` etc. resolve to the same
    types the index template declares (`docker/elasticsearch/caddy-access-index-template.json`).
    This is checked directly against the committed contract file, not against the pre-fix capture.
  - **Field presence/shape** (needs the pre/post comparison — this is what genuinely differs run
    to run): the **set of `caddy.*` leaf field names** present in a captured post-fix document's
    `_source` must equal the set captured in the pre-fix baseline fixture (below) — not
    "superset-or-equal", which codex correctly flagged as permissive enough to pass even if the
    fix silently dropped or renamed a field.
- The test: (1) skip unless `PERSONAL_AGENT_INTEGRATION=1` and the harness is reachable at
  `localhost:18080` and `localhost:19200` (mapped ES port) — docstring gives the exact `docker
  compose -p fre1243-harness -f tests/scripts/fixtures/fre1243_harness/docker-compose.harness.yml
  up -d --build` command, followed by the template PUT; (2) `curl` the harness once (with a fixed
  set of request headers, matching what the pre-fix capture below uses, so the compared field sets
  aren't confounded by different requests carrying different headers), poll ES for the newest
  `caddy-access-*` doc, record its `@timestamp`; (3) `docker compose ... up -d --force-recreate
  caddy` (**only** caddy — matches AC-1's exact recipe); (4) `curl` again (same headers); (5) poll
  ES and assert a document newer than step (2)'s timestamp appears within a bounded timeout —
  **this is AC-1**; (6) the two AC-3 checks above.

**Throwaway, uncommitted — produces the AC-2 evidence and the AC-3 field-set baseline, run once,
by hand, before any implementation edit lands:**
- A second, scratch compose file (e.g. under the session scratchpad, never `git add`ed) mirroring
  today's actual pre-fix mechanism: `caddy` with a Caddyfile using `output stdout`; `filebeat`
  built from the **current, unmodified** `Dockerfile.filebeat` (still has the resolver
  entrypoint), with `/var/lib/docker/containers:/var/lib/docker/containers:ro` mounted and
  `CADDY_CONTAINER_NAME` set to match this scratch harness's actual caddy `container_name` (the
  resolver's own env var — grounded above in `resolve-caddy-container.sh`); same throwaway
  `elasticsearch` service, same real index template installed (for symmetry — a secondary sanity
  check that today's mechanism is also template-compatible, not load-bearing for AC-2 itself).
- Procedure: bring it up, `curl` once with the same fixed headers the permanent harness will use,
  capture the newest doc's `@timestamp` **and its full `caddy.*` leaf field-name set** (→ becomes
  `pre_fix_caddy_field_baseline.json`, committed as part of this PR — field *names* only, not
  types, since types are now checked against the template directly), `--force-recreate` **only**
  caddy, `curl` again, poll ES — **observe no new document appears**, while `docker compose ...
  logs <scratch-caddy>` shows the request present in stdout. This negative result, captured
  verbatim, is AC-2's evidence, recorded in the PR/handoff exactly as the ticket's own "Measured"
  section already models. Tear the scratch harness down (`down -v`) once both artifacts (the
  timestamp evidence and the field-name baseline JSON) are captured — it never becomes a permanent
  test, because after the fix lands there is nothing left to keep proving about a mechanism no
  longer shipped.
- **Retention bound, accepted (codex round 2 minor, area 2):** this evidence chain is bounded by
  Caddy's rotation defaults (`roll_keep 10` × `roll_keep_for` ~90 days) — a sufficiently long or
  high-volume Filebeat outage can still exhaust that window before catching up. ADR-0132 D3
  already accepts an analogous bounded-loss window ("harvest lag, typically sub-second...
  accepted"); this is the same shape at a much larger, deliberately generous bound, and is
  recorded as an accepted limit rather than a gap AC-5 needs to close — the ticket's own AC-5 is
  about *not introducing a new silent-failure mode*, and a bounded, monitored (compose healthcheck
  already on `filebeat`), order-of-magnitude-more-generous retention window than today's implicit
  ~30 MB Docker json-file cap is not that.

## Verification plan

1. Write test C (Caddyfile static check) — confirm it fails against the unmodified Caddyfile.
2. Build and run the **throwaway pre-fix scratch harness** (Test D's second, uncommitted
   compose file) against today's actual, unmodified `Dockerfile.filebeat`/`filebeat.yml`/a
   stdout-only Caddyfile — confirm the recreate-only-caddy step produces no new document, capture
   that failure output (AC-2 evidence) and one document's `caddy.*` field set (AC-3 baseline →
   `pre_fix_caddy_field_baseline.json`). Tear the scratch harness down.
3. Write test D (the permanent harness + the reproduction test itself) — confirm it fails to
   even build/run meaningfully yet (fixtures don't exist) or, once the fixtures are written but
   before step 4's fix, confirm it fails at the AC-1 assertion for the *right* reason.
4. Implement steps 1–5 (Caddyfile, compose, filebeat.yml, Dockerfile.filebeat, delete resolver).
5. Re-run tests A/B/C — green.
6. Rebuild the permanent harness (`--build`) and re-run test D — green (AC-1, AC-3 against the
   committed pre-fix baseline).
7. Update docs (ADR-0132 D3 + risk table + AC-3 note, CLOUD_DEPLOYMENT.md, the required-migration
   note).
8. `make test` / `make mypy` / `make ruff-check` / `make ruff-format` / `pre-commit run
   --all-files`.
9. Tear down the permanent harness (`docker compose -p fre1243-harness ... down -v`) — never
   leaves a throwaway ES/Caddy running on the shared VPS host.
10. Self-review (`feature-dev:code-reviewer` on `git diff origin/main...HEAD`); `security-review`
    given this touches Docker volume mounts and a Dockerfile entrypoint.
11. PR body and Linear handoff both lead their post-deploy runbook with the required one-time
    `build filebeat && up -d --force-recreate caddy filebeat` (or `make build-full`) instruction —
    not buried, not optional, since a plain `make deploy` on this PR would crash-loop Filebeat
    (grounded above).

## Diff class

Touches infrastructure (Caddyfile, Dockerfile, compose) but not a production **write path**,
not destructive, not a schema change, not cost/governance code. Self-serve at the Step 6 gate
(no escalation to `/code-review ultra`), per the skill's diff-class test.
