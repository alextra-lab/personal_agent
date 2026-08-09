# Mac-local OTel Collector

The Collector that runs on the Mac alongside `slm_server` (FRE-1224). It exists for two reasons,
and it is worth being precise about them because a third reason is often assumed and is wrong:

1. **Credential custody.** It holds the Cloudflare Access service token for the OTLP ingress, so
   `slm_server` does not. ADR-0132 D2 classes the Access pair as an *environment* credential — it
   exists only because this deployment's private surfaces sit behind Cloudflare Access — so it
   belongs to the environment layer, not inside an application process.
2. **Buffering.** A local queue and retry across a downstream outage, which the direct-to-
   Elasticsearch writer removed by FRE-1071 never had.

**Not** protocol conversion. `slm_server` already exports OTLP/HTTP (`src/slm_server/telemetry.py`
imports its exporter from `opentelemetry.exporter.otlp.proto.http`, `_OTLP_PROTOCOL` is
`http/protobuf`, default endpoint `http://localhost:4318`). There is no gRPC anywhere on this path,
and ADR-0136 keeps it that way — the Cloudflare zone gRPC toggle must stay off.

## Topology

```
slm_server ──OTLP/HTTP──▶ Mac Collector ──OTLP/HTTP + CF Access──▶ Caddy ──▶ VPS Collector ──▶ Tempo
           localhost:4318   (holds token,        across the tunnel   (path-      otel-collector:4318
                             buffers)                                allowlist)
```

## Host port 4318 belongs to this Collector

Not a free choice: it is `slm_server`'s compiled-in default, and keeping it is what makes "no
producer-side change" true.

Dev-stack Tempo used to publish host `4317:4317` and `4318:4318` (on `0.0.0.0`, which covers
loopback). FRE-1224 moved it to `127.0.0.1:4327` / `127.0.0.1:4328`. If you are looking for Tempo's
OTLP receiver from the host, that is where it is; `FRE1072_TEMPO_OTLP_URL` overrides it.

The collision mattered because one of its two orderings is silent:

- **Collector binds first** → `docker compose up` fails loudly with *"port is already allocated"*.
- **Dev stack binds first** → the Collector cannot bind, launchd respawns it forever, `slm_server`'s
  spans hit connection-refused, and telemetry goes dark with nothing announcing it.

`tests/scripts/test_mac_collector_port_is_unclaimed.py` guards against a compose service taking the
port back.

## Install

```bash
mkdir -p ~/.config/seshat
cp config/otel/mac-collector.env.example ~/.config/seshat/otlp-collector.env
chmod 600 ~/.config/seshat/otlp-collector.env
$EDITOR ~/.config/seshat/otlp-collector.env      # fill in the three values

scripts/mac/install_otelcol.sh
```

The installer fetches the pinned `otelcol` 0.158.0 core build — the same version and distribution as
the VPS Collector — verifies its SHA-256 **before** extracting, installs it to `~/.local/bin`,
**copies the wrapper and collector config into `~/.local/libexec/seshat`**, renders the launchd unit
from `config/otel/com.seshat.otelcol.plist.template` pointing at those copies, and bootstraps it.

### Re-run the installer to apply a config change — a checkout is not enough

The running agent uses the **frozen copies** under `~/.local/libexec/seshat`, not this working tree.
That is deliberate. The agent is persistent and holds a live Cloudflare Access token; if it executed
the repo's files directly, then checking out a branch — something reviewers reasonably treat as a
read-only act — would change both the code it runs and the endpoint that credential is sent to. A
branch checkout is now inert with respect to the running Collector.

The consequence to remember: **editing `config/otel/mac-collector-config.yaml` does nothing until you
re-run `scripts/mac/install_otelcol.sh`.**

The credential file lives outside the repo on purpose, and the launchd plist deliberately carries no
`EnvironmentVariables` — that would put a live credential in a plaintext file under `LaunchAgents`.

### The credential file's permissions are enforced, not assumed

The wrapper **sources** the env file, which means it *executes* it. A file another principal can
write would therefore be arbitrary code execution in a launchd-persistent context, re-run at every
login. So the wrapper refuses to start (exit 78) unless the file is owned by you and is neither
group- nor world-writable. `chmod 600` is not merely advice here.

### The three variables

| Variable | Notes |
|---|---|
| `SESHAT_OTLP_INGRESS_URL` | Base URL, **no path and no trailing slash**. The exporter appends `/v1/traces` itself, which is what matches FRE-1239's Caddy allowlist (`POST`, `^/v1/traces$`, empty query). |
| `SESHAT_OTLP_CF_ACCESS_CLIENT_ID` | Must be a **dedicated** token for the OTLP ingress application — explicitly not the ADR-0132 D1 egress pair. |
| `SESHAT_OTLP_CF_ACCESS_CLIENT_SECRET` | As above. |

`scripts/mac/otelcol_launch.sh` refuses to start (exit 78) if any is unset or empty, rather than
exporting spans with a blank Access header. It fails closed by design: an empty credential against
the edge is worse than not starting, because it looks like it is working.

## Operate

```bash
launchctl print "gui/$(id -u)/com.seshat.otelcol"   # state, last exit status, PID
tail -f ~/Library/Logs/seshat-otelcol.log            # stdout + stderr

launchctl kickstart -k "gui/$(id -u)/com.seshat.otelcol"   # restart
launchctl bootout "gui/$(id -u)/com.seshat.otelcol"        # stop
```

### Diagnosing a respawn loop

`KeepAlive` is on, so a config error would otherwise spin. `ThrottleInterval` is 30s, and the
wrapper exits **78** (`EX_CONFIG`) on a configuration problem specifically so it is distinguishable
from a crash. If `launchctl print` shows a repeating exit 78, read the log: the wrapper names the
missing variable (never its value — that stream is a persistent file).

### Uninstall / rollback

```bash
launchctl bootout "gui/$(id -u)/com.seshat.otelcol"
rm ~/Library/LaunchAgents/com.seshat.otelcol.plist
rm ~/.local/bin/otelcol
rm -rf ~/.local/libexec/seshat        # the frozen wrapper + config
```

Stopping the Collector does not break `slm_server` — its spans go to a closed port and are dropped
by its own exporter. That is the pre-FRE-1223 status quo, not a new failure mode. The credential
file is left in place; remove it separately if you are decommissioning.

## Verifying it

**Receipt (AC-1).** POST an OTLP/HTTP protobuf span to `http://localhost:4318/v1/traces`, expect
`200`, and confirm the `debug` exporter reports a non-zero span count in the log. `debug` runs at
`verbosity: basic` — counts only, deliberately: `normal`/`detailed` would write every span attribute
into a persistent local log, and it would grow fastest while the downstream is unreachable.

**Buffering, and its bound (AC-4).** With the downstream unreachable — today's real state, since the
ingress host does not exist until FRE-1223 — the producer still receives `200` and the log shows
export retries at a widening interval. `max_elapsed_time: 0` means retries never stop.

The Collector's own metrics are the direct way to watch this, on `127.0.0.1:8888`:

```bash
curl -s http://localhost:8888/metrics | grep -E '^otelcol_exporter_(queue_size|queue_capacity|enqueue_failed_spans)'
```

A span accepted while the downstream is unreachable shows up as a non-zero `queue_size` — that is
retention observed, not inferred. Note what `queue_capacity` reports:

```
otelcol_exporter_queue_capacity{data_type="traces",exporter="otlp_http/vps"} 5000
```

**5000 batches, not 5000 spans** — the default sizer counts export requests. Sizing the buffer for
an expected outage means reasoning in batches at your actual span rate.

Be precise about what none of this guarantees. The OTLP receiver returns `200` once `batch` accepts
the data, **before** export is attempted, so a later flush that cannot enqueue drops that batch after
the producer already saw success. To see that boundary rather than assume it, drive the queue past
its capacity and read a non-zero `otelcol_exporter_enqueue_failed_spans`. In-memory queue contents
are also lost if the Collector restarts — reboot-durable buffering would need the `file_storage`
extension from the contrib distribution, which is deliberately out of scope here.

## Related

- **FRE-1223** — the Cloudflare half: tunnel ingress rule and the Access application. When it lands,
  `OTLP_HOST` must be set and Caddy recreated — **and Filebeat recreated in the same pass**, or the
  new host's access lines never ship and this hop's evidence trail starts dark.
- **FRE-1239** — the Caddy site block (merged): path-allowlisted, `remote_ip`-restricted, 20MiB
  request cap, and it redacts the Access credential headers from its own access log.
- **FRE-1230** — the Mac-side acceptance procedure and the `slm_server` restart gate.
- **FRE-1242** — removing the now-dead CF Access credentials from `slm_server`'s `.env`, sequenced
  onto that restart.
