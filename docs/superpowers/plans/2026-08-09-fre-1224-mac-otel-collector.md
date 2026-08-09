# FRE-1224 — Mac-local OTel Collector as credential custodian and buffer

**Ticket:** FRE-1224 (Approved → In Progress) · **Project:** Observability Foundation
**Backing design:** ADR-0132 D2 (environment-credential custody), ADR-0136 (HTTP at the edge, never
gRPC), ADR-0129 D5 (vanilla upstream Collector; a same-host loopback Collector is permitted, the
terminus is the VPS Collector).
**Research:** `docs/research/2026-08-08-fre-1220-otlp-ingress-security-and-cloudflare-capability.md`
(Proposal 3).

---

## Correction to the ticket body, carried from the owner's dispatch

The ticket body asserts two things that are **void**:

1. *"slm_server exports OTLP/gRPC to loopback"* and *"the Mac Collector re-emits OTLP/HTTP, so the
   edge never carries gRPC"* — framed as protocol conversion being the Collector's job.

   Measured in `slm_server` @ `ea2b0b8` (PR #14, merged):
   `src/slm_server/telemetry.py:34` imports `OTLPSpanExporter` from
   `opentelemetry.exporter.otlp.proto.**http**.trace_exporter`; `:51` `_DEFAULT_OTLP_ENDPOINT =
   "http://localhost:4318"`; `:52` `_OTLP_PROTOCOL = "http/protobuf"`; `:113` posts to
   `{endpoint}/v1/traces`. **slm_server already speaks OTLP/HTTP.** There is no protocol conversion
   to perform.

2. Consequently the "protocol problem dissolves" bullet is not a justification for this ticket.

**The surviving justification, and this plan's actual objective:** *credential custody* (the Cloudflare
Access service token moves out of the application process into an environment-layer component) and
*buffering* (a local queue + retry that the removed direct-to-Elasticsearch writer never had).

Because slm_server's default endpoint is already `http://localhost:4318`, **no producer-side change is
required** — provided the Mac Collector can actually bind 4318. See Step 2.

## Scope decisions settled with the owner before coding

| Decision | Choice | Why |
|---|---|---|
| Runtime | **Native binary under launchd** | Survives sleep/wake with no Linux VM in the path; starts at login. Homebrew has no collector formula, so the pinned upstream release tarball is fetched and checksum-verified. |
| Distribution | **Vanilla core `otelcol` 0.158.0**, in-memory `sending_queue` + `retry_on_failure` | Matches the VPS image pin exactly; no contrib, so no ADR-0129 D5 caveat. macOS sleep *suspends* the process rather than killing it, so an in-memory queue satisfies the ticket's stated "buffering across Mac sleep". Reboot-durable buffering (`file_storage`) is deliberately **not** built — it is a stronger claim than the ticket makes. |
| Port conflict | **Move dev-stack Tempo's host ports to 4327/4328**; the Mac Collector takes 4318 | Preserves "no producer change needed". See Step 2. |
| Acceptance criteria | **AC-1…AC-5 below**, adopted with the owner (ticket body names none) | Each decidable on the Mac today, without the Cloudflare half existing. |

## Acceptance criteria (adopted — the ticket body states none)

- **AC-1 — accepts what slm_server already sends, unchanged.** An OTLP/HTTP protobuf span POSTed to
  `http://localhost:4318/v1/traces` returns 200 and is visible in the Collector's `debug` exporter
  output. *Fails if* `SLM_OTLP_ENDPOINT` would have to move off its default.
- **AC-2 — the Collector is the custodian, and this change adds no credential to slm_server.** The
  `CF-Access-Client-Id` / `CF-Access-Client-Secret` header names appear only in the Mac Collector's
  config and environment; a grep of the tracked `slm_server` tree returns no hit, and nothing in
  this diff writes a credential into slm_server.
  *Deliberately NOT claimed:* that slm_server holds no CF credential at all. Measured 2026-08-09:
  its gitignored `.env` still carries `SLM_CF_ACCESS_CLIENT_ID` / `_SECRET`, and `start.sh:15-20`
  (`set -a; source .env; set +a`) exports them into the running process. They are **dead** — PR #14
  removed the Elasticsearch writer, their only consumer, and `grep` finds zero references in
  `src/`, `tests/` or `scripts/`. That pre-existing exposure is filed separately and actioned by the
  owner at FRE-1230's restart gate; **this ticket does not touch slm_server** (owner decision,
  2026-08-09). Codex plan-review raised it as Blocking; the finding is accepted, its remedy relocated.
- **AC-3 — no secret is committed.** The committed config references the token through `${env:…}`
  only; the committed example env file holds placeholders; `check-no-deployment-identifier` and
  `check-no-personal-paths` pass.
- **AC-4 — spans survive an unreachable downstream, up to a stated bound.** With the forward target
  unreachable (today's real state — the ingress host does not exist yet): the producer still
  receives 200, the Collector logs export retries, and spans are retained in the exporter queue.
  **Beyond the queue bound they are dropped, and the drop is observable** — proven by driving the
  queue to overflow and reading a non-zero `otelcol_exporter_enqueue_failed_spans`.
  *Deliberately NOT claimed:* end-to-end delivery guarantee. The OTLP receiver returns 200 once
  `batch` accepts the data, before export is attempted, so a later flush that cannot enqueue drops
  that batch after the producer already saw success. In-memory queue contents are also lost on
  Collector restart. Both are inherent to core + in-memory buffering — the configuration the owner
  chose — and are recorded here rather than papered over. (Codex plan-review, Blocking: the original
  wording of this criterion overclaimed retention. Accepted and reworded.)
- **AC-5 — HTTP at the edge, never gRPC.** The committed config declares no gRPC receiver and no
  gRPC exporter; the egress exporter is `otlp_http` against an `https://…` base that resolves to the
  `/v1/traces` path. No `4317` anywhere in the Mac config. (ADR-0136.)
  Note: `otlp_http`, **not** the `otlphttp` alias — verified against the v0.158.0 exporter README,
  which states the alias is deprecated and will be removed. (Codex plan-review, Should-Fix; accepted.)

## Open remedies

None.

---

## Step 1 — failing tests first (TDD)

**New file:** `tests/scripts/test_mac_otel_collector_config.py`

Mirrors the two-layer style of `tests/scripts/test_otel_collector_compose_service.py` (source-only
parse class, always runs, no docker/live dependency). Assertions, mapped to criteria:

- receiver `otlp.protocols` has an `http` key bound to `127.0.0.1:4318` and **no `grpc` key** (AC-1, AC-5)
- the egress exporter is `otlp_http/*` — and specifically **not** the deprecated `otlphttp` alias —
  with its `endpoint` exactly an `${env:…}` reference (AC-3, AC-5)
- both CF Access headers are `${env:…}` references — no literal value (AC-2, AC-3)
- `sending_queue.enabled` is true and `retry_on_failure.enabled` is true, with
  `max_elapsed_time: 0` (AC-4)
- `debug` verbosity is `basic` — never `normal`/`detailed`, which would dump span attributes into a
  durable local log
- the string `4317` appears nowhere in the rendered config text (AC-5)
- the traces pipeline wires `[otlp] → [batch] → [otlp_http/*, debug]`

**New file:** `tests/scripts/test_mac_otelcol_launch_contract.py` — asserts the launch wrapper
exports rather than merely sourcing (`set -a` present, or every assignment `export`ed) and that it
refuses to `exec` when any of the three required variables is absent. This is the regression guard
for the fail-open-with-empty-Access-headers path Codex identified.

**AC-4 needs a live probe, not only a static parse.** A shell probe documented in the guide (and run
by hand at acceptance, since it needs the real binary): POST spans with the downstream unreachable,
confirm 200 to the producer, then drive the queue past `queue_size` and read a non-zero
`otelcol_exporter_enqueue_failed_spans` off the Collector's own metrics endpoint. A single
unreachable-downstream POST proves only the happy half of the criterion.

**New file:** `tests/scripts/test_mac_collector_port_is_unclaimed.py`

The generalization of `test_compose_port_collisions.py` to the case it structurally cannot see — a
host-native process vs. a compose-published port. Asserts no service in `docker-compose.yml` publishes
host port 4318. This is the regression guard for the conflict found in Step 2.

**Verify they fail:**
```bash
make test-file FILE=tests/scripts/test_mac_otel_collector_config.py
make test-file FILE=tests/scripts/test_mac_collector_port_is_unclaimed.py
```
Expected before implementation: collection/assertion failures (config file absent; 4318 claimed by tempo).

## Step 2 — resolve the port conflict (fold-in, per build skill Step 5)

**Found while planning, not speculative.** `docker-compose.yml` binds Tempo `"4317:4317"` and
`"4318:4318"` — no interface prefix, therefore `0.0.0.0`, which covers loopback. That is the dev stack,
which runs on this same Mac via `make infra-up`. `tests/scripts/test_otel_collector_compose_service.py:97`
states it outright: *"Tempo owns 4317/4318 for FRE-1072's direct-inject tests."*

Failure modes if left alone — the second is silent, which is why this is not deferrable:
- Collector binds first (launchd `RunAtLoad`) → `docker compose up` fails *"port is already
  allocated"*. Same shape as the FRE-1072 Verify Failed incident.
- Dev stack binds first → Collector cannot bind, launchd `KeepAlive` respawns forever, slm_server's
  spans get connection-refused → **telemetry silently dark**, the exact outcome FRE-1230's restart
  gate exists to prevent.

**Edits:**
- `docker-compose.yml` — tempo `ports:` → `"127.0.0.1:4327:4317"`, `"127.0.0.1:4328:4318"`.
  (Also tightens them to loopback, which they should have been.)
- `tests/integration/test_fre1072_tempo_grafana_acceptance.py:47` — default
  `FRE1072_TEMPO_OTLP_URL` → `http://localhost:4328`.
- `tests/scripts/test_otel_collector_compose_service.py:92-97` — the disjointness assertion and its
  docstring now name 4327/4328.
- **Comment/doc sweep** (Codex, Should-Fix — the code edits alone leave the prose lying): grep for
  `4317/4318`, `localhost:4318` and `FRE1072_TEMPO_OTLP_URL` across `docker-compose.yml` comments,
  `docs/`, and ADR-0129 references; update every statement that Tempo owns 4317/4318 on the host.
  A stale comment here misleads exactly the operator debugging a dark-telemetry incident.

## Step 3 — the Collector config

**New file:** `config/otel/mac-collector-config.yaml`

```yaml
receivers:
  otlp:
    protocols:
      http:
        endpoint: 127.0.0.1:4318      # loopback only — nothing off-Mac can inject

processors:
  batch:
    # FRE-1239's merged Caddy block caps a single request at `request_body { max_size 20MiB }`.
    # Default `batch` has no byte ceiling and no hard span cap, so a backlog flush after an
    # outage — precisely when the queue is deepest — is exactly when an export would exceed the
    # cap and be refused at the edge. A hard span cap bounds it.
    send_batch_size: 512
    send_batch_max_size: 2048

exporters:
  otlp_http/vps:                      # NOT the deprecated `otlphttp` alias
    endpoint: ${env:SESHAT_OTLP_INGRESS_URL}   # base; the exporter appends /v1/traces
    headers:
      CF-Access-Client-Id: ${env:SESHAT_OTLP_CF_ACCESS_CLIENT_ID}
      CF-Access-Client-Secret: ${env:SESHAT_OTLP_CF_ACCESS_CLIENT_SECRET}
    sending_queue:
      enabled: true
      # NOTE: the default sizer counts export REQUESTS (batches), not spans. 5000 is
      # 5000 queued batches. Overflow drops — see AC-4's stated bound.
      queue_size: 5000
    retry_on_failure:
      enabled: true
      initial_interval: 5s
      max_interval: 300s
      max_elapsed_time: 0             # 0 = retries never stop (verified, v0.158.0)
  debug:
    verbosity: basic                  # counts only — never dump span attributes to a durable log

service:
  pipelines:
    traces:
      receivers: [otlp]
      processors: [batch]
      exporters: [otlp_http/vps, debug]
```

No gRPC receiver: slm_server is HTTP-only, so declaring one would be unused surface. The exporter
appending `/v1/traces` to the base is what keeps the request inside FRE-1239's Caddy allowlist.

`debug` is at `verbosity: basic`, not `normal`. Codex plan-review flagged (Should-Fix) that a
`normal`/`detailed` debug exporter combined with launchd's persistent stdout redirection writes every
span attribute into an ever-growing local log — and it grows fastest precisely while the downstream is
intentionally unreachable. `basic` reports counts, which is all AC-1 needs to prove receipt.

**Alignment with the merged FRE-1239 block** (PR #893, `531add2a`), read off `origin/main` rather
than assumed. The Caddy allowlist is `method POST` + `path_regexp ^/v1/traces$` + `query ""` +
`remote_ip 172.25.0.0/16`. The `otlp_http` exporter with a base `endpoint` posts to `/v1/traces`
with no query string, so it matches the allowlist by construction — this is why AC-5 insists on the
base-endpoint form rather than a hand-written `traces_endpoint`. The block also redacts
`Cf-Access-Client-Id` / `Cf-Access-Jwt-Assertion` from its access log, so this hop's evidence trail
cannot carry the credential.

**Declined (Codex, Nice-to-Have): `memory_limiter`.** The queue is already bounded and this is a
low-volume single-producer agent, so it would be speculative complexity — `.claude/CLAUDE.md` §2.
Recorded as a deliberate decline, not an oversight.

## Step 4 — install, launch wrapper, launchd unit

**New:** `scripts/mac/install_otelcol.sh` — downloads
`otelcol_0.158.0_darwin_arm64.tar.gz` from `open-telemetry/opentelemetry-collector-releases`,
verifies `sha256 = a4fc106889faa4ffcd43c062cc8fd14c1eff6d2f53bef64930b3199bd8303095`, extracts
`otelcol` to `${SESHAT_OTELCOL_PREFIX:-$HOME/.local/bin}`. Refuses to proceed on checksum mismatch.

**New:** `scripts/mac/otelcol_launch.sh` — loads
`${SESHAT_OTLP_ENV_FILE:-$HOME/.config/seshat/otlp-collector.env}` (mode-0600, outside the repo,
never committed), then `exec`s `otelcol --config=<repo>/config/otel/mac-collector-config.yaml`.
A wrapper rather than launchd `EnvironmentVariables` so the token never lands in a plist.

**The load must export, and must fail closed:**
```sh
set -a; . "$ENV_FILE"; set +a          # `. file` alone sets shell vars, NOT child env
for v in SESHAT_OTLP_INGRESS_URL SESHAT_OTLP_CF_ACCESS_CLIENT_ID SESHAT_OTLP_CF_ACCESS_CLIENT_SECRET; do
  eval "[ -n \"\${$v:-}\" ]" || { echo "missing $v in $ENV_FILE" >&2; exit 78; }   # EX_CONFIG
done
```
Codex plan-review raised this as **Blocking** and was right: a bare `source` would leave the three
variables unexported, so `${env:…}` would resolve empty and the Collector would ship **spans with
empty Access headers** — failing open against the edge rather than refusing to start. The explicit
presence check makes it fail closed. Verified before `exec`; nothing is echoed but variable *names*.

**New:** `config/otel/com.seshat.otelcol.plist.template` — `RunAtLoad`, `KeepAlive`,
**`ThrottleInterval: 30`**, stdout/stderr to `$HOME/Library/Logs/seshat-otelcol.log`. Paths are
`@@PLACEHOLDER@@` tokens substituted at install time — **no literal path may be committed**
(`check-no-personal-paths` rejects a macOS home-directory prefix, and a home-relative `Dev/` layout,
in any tracked file — including, as this plan discovered by tripping it, prose *describing* the rule).

`ThrottleInterval` is the pairing for the fail-closed exit above: `KeepAlive` plus an exit-78 config
error is a tight respawn loop that would flood the log (Codex, Should-Fix). 30s bounds it.

**New:** `config/otel/mac-collector.env.example` — placeholder values only, example domain
(`https://otlp.example.com`), never a real hostname (`check-no-deployment-identifier`).

## Step 5 — documentation

**New:** `docs/guides/MAC_OTEL_COLLECTOR.md` — install, start/stop, where the env file lives, how to
confirm it is buffering, and the explicit note that host port 4318 on this Mac belongs to the
Collector (with the Tempo rebinding from Step 2 cross-referenced).

Must also cover, per Codex (Should-Fix — a native LaunchAgent with no documented exit is a support
burden): **uninstall and rollback** (`launchctl bootout`, remove the plist, remove the binary, and
what reverts if the Collector is simply stopped — slm_server's spans go to a closed port, which is
the pre-FRE-1223 status quo, not a new failure); **diagnosing a respawn loop** (`launchctl print`,
the exit-78 config-error signature, log location); and the **AC-4 overflow probe** from Step 1.

## Step 6 — quality gates

```bash
make test                       # module first, then full
make mypy
make ruff-check && make ruff-format
pre-commit run --all-files
```

Then commit, then the two reviewers scoped to `git diff origin/main...HEAD`:
`feature-dev:code-reviewer` and `security-review` (this diff handles a credential — security-review
is mandatory, not optional).

**Diff class: self-serve.** It touches no production write path, deletes nothing, changes no schema,
and modifies no cost/governance code. It is Mac-local dev/edge tooling plus config and tests. The
credential it handles is *newly custodied*, not an existing production write.

## Codex plan-review — findings and dispositions

Run 2026-08-09 against v0.158.0 upstream sources (exporterhelper README, otlphttp exporter README,
batchprocessor source). Risk tier: **Standard/Complex** — the diff handles a credential, so review
was required, not optional.

| Sev | Finding | Disposition |
|---|---|---|
| Blocking | slm_server still inherits CF Access creds from its `.env` (`set -a` export) | **Verified and accepted.** Confirmed live; also found they are *dead* post-PR #14. AC-2 reworded to a provable claim; remedy filed as **FRE-1242**, actioned by the owner at FRE-1230's restart gate. This ticket does not touch slm_server. |
| Blocking | Wrapper `source` leaves vars unexported → empty Access headers, failing **open** | **Accepted.** `set -a` + explicit presence check + exit 78 before `exec`. New contract test guards it. |
| Blocking | AC-4 overclaimed retention after HTTP 200 | **Accepted.** Reworded to a bounded claim with the drop made observable via `otelcol_exporter_enqueue_failed_spans`; overflow probe added. |
| Should-fix | Token still lives in the process environment | **Accepted as a stated limitation**, documented in the guide. Keychain retrieval declined — the Collector still receives it via runtime config, so it moves the leak rather than closing it. |
| Should-fix | `debug` + persistent stdout = durable, unbounded span log | **Accepted.** `verbosity: basic`. |
| Should-fix | `queue_size` counts requests, not spans | **Accepted.** Comment corrected; AC-4 states the bound. |
| Should-fix | `otlphttp` is a deprecated alias | **Verified against the v0.158.0 README and accepted.** Using `otlp_http`. |
| Should-fix | Tempo port move leaves stale comments/docs | **Accepted.** Doc sweep added to Step 2. |
| Should-fix | No uninstall/rollback; `KeepAlive` respawn loop | **Accepted.** `ThrottleInterval: 30` + uninstall/diagnosis section in the guide. |
| Nice-to-have | No `memory_limiter` | **Declined** — bounded queue, low-volume single-producer agent; speculative complexity (`.claude/CLAUDE.md` §2). |
| Nice-to-have | New Tempo ports not themselves guarded | **Partially accepted** — covered by the doc sweep; the existing in-file collision guard already covers the compose side. |

## security-review — findings and dispositions

Run 2026-08-09 against the committed diff. Mandatory here: the diff handles a credential.

| Sev | Finding | Disposition |
|---|---|---|
| Medium | The launchd agent executed `${REPO_ROOT}/scripts/...` and read the repo's config, so a **branch checkout** would change what a persistent, credential-holding daemon runs and where it sends the token — and reviewing a branch is normally read-only. | **Accepted and fixed.** The installer now freezes the wrapper and config into `~/.local/libexec/seshat` and the plist pins both paths; the working tree is inert with respect to the running agent. Guide states that re-running the installer is now the only way to apply a config change. Guarded by `test_installed_unit_runs_frozen_copies_not_the_working_tree`. |
| Low | The env file is **sourced** (executed), and the wrapper checked only existence — never mode or ownership — while `chmod 600` is a manual, unenforced install step. A group-writable file is persistent code execution at every login. | **Accepted and fixed.** The wrapper refuses (exit 78) unless the file is owned by the invoking uid with no group/other write bit. Guarded by `test_wrapper_refuses_a_group_or_world_writable_env_file` across four modes, plus a positive test that 0600 is still accepted. |
| — (aside) | `install_otelcol.sh` committed mode 644 while the guide documents invoking it directly — would fail with permission denied. | **Accepted and fixed.** Mode 755, guarded by `test_installer_is_executable`. |

**Explicitly cleared by the review**, recorded so master need not re-derive them: no secret or real
hostname committed (both repo guards pass); the fail-open path is genuinely closed and proven
*behaviourally* rather than by text-matching; checksum verification precedes extraction, so a
mismatched tarball is never unpacked; the credential never appears on argv, so it is absent from
`ps`; error paths name variables, never values; `debug` at `basic` keeps span attributes out of the
persistent log; the receiver is loopback-only with no gRPC listener.

The reviewer could not fetch upstream's checksum list from its sandbox and asked for manual
confirmation of the pin. **Confirmed:** `a4fc106889faa4ffcd43c062cc8fd14c1eff6d2f53bef64930b3199bd8303095`
was read from the release's own `otelcol_0.158.0_darwin_arm64.tar.gz.sha256` asset via
`gh release download`, not transcribed from a third party.

## Explicitly out of scope

- The Cloudflare tunnel ingress rule, the Access application, and its service token — **FRE-1223** (owner-side).
- The Caddy site block — **FRE-1239** (dispatched to build1).
- Restarting slm_server — gated by **FRE-1230**. This plan does not restart it and does not change it.
- Reboot-durable (`file_storage`) buffering — a stronger claim than the ticket makes; a clean
  follow-on if wanted.
- End-to-end verification through the tunnel — impossible until FRE-1223 lands; that is FRE-1230's
  procedure, not this ticket's.
