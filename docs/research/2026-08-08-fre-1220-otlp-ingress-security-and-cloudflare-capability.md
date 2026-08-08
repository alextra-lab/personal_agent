# FRE-1220 — OTLP ingress: what Cloudflare can actually carry, and what each mechanism costs

**Study date:** 2026-08-08
**Commissioning ticket:** [FRE-1220](https://linear.app/frenchforest/issue/FRE-1220) (owner-requested, relayed by master)
**Session:** `cc-explore` — read-only on everything operational
**Related:** ADR-0129 (D5–D7), ADR-0132 (D1, D2), ADR-0045, ADR-0064, ADR-0111, FRE-1070, FRE-1071, FRE-1073, FRE-411

> **Domain placeholders.** No literal deployment domain appears here (FRE-895; the
> `check-no-deployment-identifier` pre-commit hook enforces it). Hostnames are written
> `<es-host>`, `<slm-host>`, `<otlp-host>` and so on.

---

## Summary — what the study found, including where it contradicts its own commission

The commission asked whether OTLP/gRPC on 4317 is *carryable* through this tunnel, treating it as a
plumbing-difficulty question against OTLP/HTTP on 4318. **The documented answer is not "harder" — it is
that the combination the commission implicitly assumed is unavailable, and its nearest working form is a
silent security failure.** Cloudflare's own documentation states that Access *ignores* gRPC traffic, and
that tunnel gRPC is unsupported on public hostnames altogether (F6, F7). An Access policy placed on a
gRPC OTLP hostname would read as configured in the dashboard and enforce nothing.

The second correction is more useful. The commission frames the ingress as something that must be
*invented*, and lists three candidate mechanisms as if none existed yet. **Measured, one of them is
already running in production for this exact producer.** The Mac's `slm_server` writes telemetry to the
VPS today, through a Cloudflare Tunnel public hostname, path-scoped at Caddy, gated by Access — 586
documents all-time, most recently 2026-08-08T08:53:36Z, with Caddy's own access log showing the requests
arriving (F3, F4). The question is therefore not *can this be built* but *should the proven pattern be
re-used or re-minted* — and FRE-1071 deletes the consumer of the existing one.

Third, the commission records master's inference that ingress rules are dashboard-only because the tunnel
is token-based with no local config file. **That inference does not follow** (F10): token-based operation
implies *remotely-managed* config, and remotely-managed config is exactly what this project's own
Terraform design specifies owning via `cloudflare_zero_trust_tunnel_cloudflared_config`. The repository
contains two contradictory claims on this point and the live system cannot distinguish them.

**Recommendation (detail in `## Proposals`):** a public hostname `<otlp-host>` → Caddy → the Collector's
**OTLP/HTTP** receiver on 4318, path-scoped to `^/v1/traces$`, behind an Access application with a
**Service Auth** policy and a **dedicated** service token; on the Mac, a local OTel Collector holds that
token so `slm_server` holds no Cloudflare credential. Explicitly **not** OTLP/gRPC at the edge, and
explicitly **do not** enable the zone-level gRPC toggle.

---

## Findings

### F1 — The Collector publishes no host port; nothing off-box can reach it directly

**Verdict:** NEGATIVE (nothing off-box reaches the Collector via a published host port)

**The query, as run:**
```
docker inspect -f '{{.HostConfig.PortBindings}} {{.Config.ExposedPorts}}' cloud-sim-otel-collector
```

**Its actual output:**
```
PortBindings=map[] ExposedPorts=map[4317/tcp:{} 4318/tcp:{} 55679/tcp:{}]
```

The binding map is read in full and is empty; 4317/4318 are exposed to the compose network only.

**Arm 1 — target-identifier provenance (1a, raw instance in the queried store).** The same store
(Docker's host-config for containers on this VPS) exhibits populated `PortBindings` structures, so the
field is real and populated when a binding exists:
```
cloud-sim-seshat-gateway PortBindings=map[9001/tcp:[{127.0.0.1 9001}]]
cloud-sim-caddy          PortBindings=map[80/tcp:[{invalid IP 80}] 443/tcp:[{invalid IP 443}] 8600/tcp:[{127.0.0.1 8600}] 8601/tcp:[{127.0.0.1 8601}]]
cloud-sim-grafana        PortBindings=map[3000/tcp:[{127.0.0.1 3003}]]
```

**Arm 2 — path liveness (identical query, only the identifier varied).** The same `docker inspect -f`
invocation against three other containers returns non-empty, quoted immediately above. The command form
and the field selector are sound.

**Arm 3 — scope match.** Scope is **host-side port publication of `cloud-sim-otel-collector` on this
VPS, read 2026-08-08**. The verdict is stated at that scope only. It makes **no** claim about the
Cloudflare Tunnel path, which does not use host ports and is measured separately in F2.

---

### F2 — No tunnel ingress rule routes to the Collector; the live rule set has six hostnames and none is it

**Verdict:** NEGATIVE (no ingress rule in the live tunnel configuration targets the Collector)

**The query, as run** — the cloudflared container logs each remotely-fetched configuration verbatim with
a version counter, which is a direct read of the live ingress rule set:
```
docker logs cloud-sim-cloudflared 2>&1 | grep 'Updated to new configuration' | tail -1
```

**Its actual output** (domain scrubbed; structure verbatim):
```
2026-08-08T05:01:37Z INF Updated to new configuration config="{\"ingress\":[
  {\"hostname\":\"agent.<domain>\",\"service\":\"http://caddy:80\"},
  {\"hostname\":\"monitoring.<domain>\",\"service\":\"http://kibana:5601\"},
  {\"hostname\":\"observe.<domain>\",\"service\":\"http://grafana:3000\"},
  {\"hostname\":\"graph.<domain>\",\"service\":\"http://caddy:80\"},
  {\"hostname\":\"api.<domain>\",\"service\":\"http://caddy:80\"},
  {\"hostname\":\"es.<domain>\",\"service\":\"http://caddy:80\"},
  {\"service\":\"http_status:404\"}],
  \"warp-routing\":{\"enabled\":false}}" version=7
```

**Arm 1 — target-identifier provenance (1a, raw instance in the queried store).** The queried store is
the live tunnel configuration itself, and it exhibits the exact identifier shape sought — a
`{"hostname": ..., "service": "http://<compose-service>:<port>"}` mapping pointing straight at a
container. Two such rules are present verbatim above (`http://kibana:5601`, `http://grafana:3000`).
A Collector rule would be that same form with `http://otel-collector:4318`; the form is real in this
store, and no rule of that form names the Collector.

**Arm 2 — path liveness (identical query, only the identifier varied).** Grepping the same captured
configuration string for each service name:
```
service 'otel-collector' present in live tunnel config v7 : 0
service 'grafana'        present in live tunnel config v7 : 1
service 'kibana'         present in live tunnel config v7 : 1
service 'caddy'          present in live tunnel config v7 : 1
service 'tempo'          present in live tunnel config v7 : 0
```

**Arm 3 — scope match.** Scope is **the VPS tunnel (`f3f16069-…`), configuration version 7, as fetched
2026-08-08T05:01:37Z**. It claims nothing about the separate Mac tunnel serving `<slm-host>`, and nothing
about any Cloudflare object not represented in the tunnel's ingress document (Access applications, DNS
records and WAF rules are not visible in this store).

**Two collateral facts, read positively off the same output:** `warp-routing` is `enabled: false`, so the
private-network path is not available today without enabling it; and the six-hostname list is the
complete public surface of this tunnel.

---

### F3 — An off-box producer ingress already exists, is Access-gated, and is carrying `slm_server` telemetry now

**Verdict:** POSITIVE

**The query, as run** — Caddy's own JSON access log, filtered to the ES ingress site block:
```
docker logs cloud-sim-caddy 2>&1 | grep '_bulk\|slm-requests' | tail -3
```

**Its actual output** (domain scrubbed, fields extracted):
```
host=es.<domain> uri=/slm-requests-2026.08.08/_doc method=POST status=201 proto=HTTP/1.1
host=es.<domain> uri=/slm-requests-2026.08.08/_doc method=POST status=201 proto=HTTP/1.1
host=es.<domain> uri=/slm-requests-2026.08.08/_doc method=POST status=201 proto=HTTP/1.1
```

This is the Mac's `slm_server` writing into the VPS through the Cloudflare Tunnel, over the
`<es-host>` public hostname, terminating at Caddy's path-allowlisted block
(`^/(slm-requests-[^/]+|_bulk)(/.*)?$`), and being accepted (HTTP 201). **Plain HTTP/1.1** — the
protocol actually in use across this tunnel for a machine producer.

**The gate is real, measured from the outside:**
```
curl -X POST "https://<es-host>/slm-requests-2026.08.08/_doc" -d '{}'   # no credentials
  -> HTTP 403
```
Unauthenticated writes are refused at the edge; the authenticated writes above reach Caddy and succeed.
The 403/201 pair is the positive control for each other — the same hostname and path both refuses without
credentials and succeeds with them.

---

### F4 — The ES telemetry corpus that FRE-1071 removes: 586 documents, still arriving today

**Verdict:** POSITIVE

**The query, as run:**
```
POST http://localhost:9200/slm-requests-*/_count
  {"query":{"range":{"ts":{"gte":"now-<W>"}}}}
POST http://localhost:9200/slm-requests-*/_search?size=0
  {"aggs":{"m":{"max":{"field":"ts"}}}}
```

**Its actual output:**
```
ts>=now-24h : 3
ts>=now-48h : 4
ts>=now-7d  : 195
ts>=now-30d : 586
max ts / total docs : 2026-08-08T08:53:36.750Z / 586
```

A raw document from `slm-requests-2026.08.08`, quoted to fix the producer's identity:
```json
{"trace_id":"a555499d-…","span_id":"2b1e2f18-…","session_id":"35d379be-…",
 "model_id":"unsloth/qwen3.6-35-A3B","backend":"llamacpp","port":8502,
 "prompt_tokens":8600,"completion_tokens":821,"total_ms":11806.7,"ttfb_ms":908.4,
 "status":200,"ts":"2026-08-08T05:58:42.201454+00:00"}
```
`backend: llamacpp`, `port: 8502` identifies the Mac SLM server (`reference_slm_server_backends`), not a
VPS process.

**Instrument note, recorded because it nearly produced a false negative.** The first count run used
`@timestamp` and returned **0** against indices that visibly held 2026-08-08 documents. The timestamp
field in this family is `ts`. The control confirms the trap rather than the system:
```
identical query, field varied to @timestamp, 30d : 0
identical query, field ts,                  30d : 586
```
This is the same field-name trap recorded for the Grafana panel work (FRE-1072). Any acceptance criterion
written against this index family must name `ts`.

---

### F5 — The gateway exports OTLP over gRPC, read from the running process

**Verdict:** POSITIVE

**The query, as run:**
```
curl -s http://127.0.0.1:9001/telemetry/effective-config
docker exec cloud-sim-seshat-gateway env | grep OTEL
```

**Its actual output:**
```json
{"service_name":"seshat-vps","otlp_endpoint":"otel-collector:4317","otlp_protocol":"grpc","insecure":true}
```
```
AGENT_OTEL_EXPORTER_ENDPOINT=otel-collector:4317
```

The in-repo producer is gRPC, plaintext, to a compose service name. This is the symmetry `slm_server`'s
configuration would otherwise be expected to mirror — and F6/F7 are why it must not, at the edge.

---

### F6 — Cloudflare Access does not support gRPC: an Access policy on a gRPC endpoint is a silent no-op

**Verdict:** POSITIVE (the documented capability limit is stated and quoted)

**Source, quoted verbatim** — [gRPC connections, Cloudflare Network settings docs](https://developers.cloudflare.com/network/grpc-connections/) (page last updated 2026-04-23):

> "Cloudflare Access does not support gRPC traffic sent through Cloudflare's reverse proxy."

> "gRPC traffic will be ignored by Access if gRPC is enabled in Cloudflare."

> "We recommend disabling gRPC for any sensitive origin servers protected by Access or enabling another
> means of authenticating gRPC traffic to your origin servers."

**And the enabling requirements, same page, verbatim:**

> - Your gRPC endpoint must listen on port 443
> - Your gRPC endpoint must support TLS and HTTP/2
> - HTTP/2 must be advertised over ALPN
> - Use `application/grpc` or `application/grpc+<message type>` for the Content-Type header
> - The hostname must be set to proxied
> - The hostname must use at least Full SSL/TLS encryption mode

> "When gRPC is not enabled on a zone, Cloudflare will respond to gRPC requests with a `403 Forbidden`
> response."

**Why this is the decisive finding rather than a footnote.** gRPC enablement is a **zone-level** setting,
not a per-hostname one. Turning it on to carry OTLP would apply to the whole zone — which today includes
`agent`, `api`, `es`, `graph`, `monitoring` and `observe` (F2). Combined with the second quotation, the
consequence is that **every Access-protected hostname in the zone would stop enforcing Access for
requests carrying a gRPC content type.** The failure is silent: the policies remain visible and correct
in the dashboard. This converts "gRPC is more involved to tunnel" into "gRPC is a zone-wide weakening of
the only authentication layer this deployment has at the edge."

---

### F7 — Tunnel gRPC is unsupported on public hostnames; the supported path requires WARP enrolment

**Verdict:** POSITIVE (documented capability limit)

**Source, quoted verbatim** — [gRPC, Cloudflare One / Cloudflare Tunnel use cases](https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/use-cases/grpc/):

> "Cloudflare Tunnel supports gRPC traffic via private subnet routing."

> "Public hostname deployments are not currently supported."

Its prerequisites, per the same page: deploy the Cloudflare One (WARP) client on the device, configure
Split Tunnels to route the private network's IP/CIDR through the client, and enrol devices in the Zero
Trust organization.

**Against the live system:** the tunnel's `warp-routing` is `enabled: false` (F2). Taking the documented
gRPC path would require enabling WARP routing on the tunnel *and* enrolling the Mac as a managed device
with a Split Tunnel policy — which changes the Mac's whole network posture to solve a
one-endpoint telemetry problem. `--protocol http2` in the cloudflared command is unrelated to any of
this: it selects the **cloudflared↔edge** transport (HTTP/2 instead of QUIC) and says nothing about which
application protocols the tunnel can carry. The commission's framing conflated the two.

---

### F8 — OTLP/HTTP on 4318 works and returns 200; both receivers are listening

**Verdict:** POSITIVE

**The query, as run** — from inside the compose network, so this measures the *receiver*, not any path:
```
docker run --rm --network seshat_cloud-sim --entrypoint curl curlimages/curl:latest \
  -X POST http://otel-collector:4318/v1/traces -H 'Content-Type: application/json' -d '{"resourceSpans":[]}'
docker run --rm --network seshat_cloud-sim --entrypoint sh curlimages/curl:latest -c 'nc -z -w3 otel-collector 4317'
ss -ltnp   # in the collector's network namespace
```

**Its actual output:**
```
POST otel-collector:4318/v1/traces -> HTTP 200
tcp/4317 open
LISTEN 0 4096 *:4317 *:*
LISTEN 0 4096 *:4318 *:*
```

Both OTLP receivers are up. The HTTP receiver accepts an ordinary JSON POST and answers 200 — the shape
Caddy can path-scope (`^/v1/traces$`) and Access can gate, exactly as `<es-host>` does today for
`^/(slm-requests-[^/]+|_bulk)(/.*)?$`.

**Scope note:** this is a receiver measurement taken on the compose network. It is deliberately *not*
offered as evidence of reachability from the Mac — see F11.

---

### F9 — Access for a machine producer needs a Service Auth policy, and the ADR-0132 D1 custody pattern is the right principle on the wrong box

**Verdict:** POSITIVE

**Source, verbatim** — [Service tokens, Cloudflare One docs](https://developers.cloudflare.com/cloudflare-one/access-controls/service-credentials/service-tokens/):

> "To authenticate to an Access application using your service token, add the `CF-Access-Client-Id` and
> `CF-Access-Client-Secret` headers to any HTTP request."

> "You must set the policy action to **Service Auth**; otherwise, Access will prompt for an identity
> provider login."

> "Service tokens expire according to the token duration you selected when you created the token."

A headless producer cannot complete an interactive IdP challenge, so **Service Auth is not a preference
here, it is the only policy action that can work.** The token also carries an expiry, which makes silent
expiry a real failure mode for an unattended telemetry path — the symptom would be spans stopping, which
is precisely the condition ADR-0134 exists to alert on.

**How this interacts with ADR-0132 D1 — the custody pattern applies in principle and not in mechanism.**
D1 puts the service-token pair in the **Caddy container on the VPS** so the gateway process holds no
Cloudflare credential, and the move is verified:

```
grep -E '^CF_ACCESS_CLIENT_ID=' /opt/seshat/.env   ->  (absent)
```
measured, consistent with D1's own note that leaving the pair in `.env` would make the move cosmetic.

But that mechanism is **egress from the VPS** — Caddy is the last hop out. For OTLP the Mac is the
*client*, so a VPS-side proxy is on the wrong box and the wrong side of the connection; the D1 mechanism
does not transfer. What transfers is **D2's principle**: the CF pair is an *environment* credential
("this deployment's private surfaces sit behind Cloudflare Access; a deployment without Cloudflare has
none of those barriers"), so it belongs to the environment layer rather than inside an application
process. On the Mac, the environment-layer component that should hold it is **not `slm_server`** — which
is what makes the Mac-local Collector the custody-correct implementation rather than a third alternative.

**One shape to avoid explicitly:** re-using the *same* service-token pair that D1 places in Caddy for
egress. That pair authenticates the VPS to `<slm-host>` and the artifact origins; binding a third,
unrelated ingress surface to it would couple the blast radii of all three and place an egress credential
on a third machine. A separate Access application with its own token is the correct shape.

---

### F10 — Whether tunnel ingress is Terraform-managed cannot be determined from this repository, and the repository contradicts itself

**Verdict:** UNVERIFIABLE (from this repository) — with several sub-claims that *are* determinable

**What is determinable, and measured:**

```
find . -name '*.tf' -not -path './.git/*'   ->  (no output; zero tracked Terraform files)
ls infrastructure/                          ->  scripts/  systemd/
```
No Terraform lives in this repository, so **no tunnel ingress rule is Terraform-managed from here.** That
is consistent with ADR-0045's migration table: *"Phase 1 — Provision VPS via Terraform … ✅ Done.
Terraform lives in private repo `personal_agent_secrets`."*

**That Access applications are Terraform-managed with laptop-held state is evidenced in-repo**, in
ADR-0064's correction of 2026-05-17: *"Laptop terraform state showed `session_duration = "720h"` and
`auto_redirect_to_identity = true` correctly set; `terraform plan` returned no changes."* That is an
Access-application claim, as of that date.

**The contradiction, stated plainly.** Two in-repo sources disagree about tunnel *ingress*:

| Source | Claim about ingress rules |
|---|---|
| `docs/superpowers/specs/2026-04-16-cloudflare-tunnel-terraform-design.md` §Resources; `docs/superpowers/plans/2026-04-17-mac-slm-tunnel.md` | Terraform-owned, via `cloudflare_zero_trust_tunnel_cloudflared_config` with `config_src = "cloudflare"` — *"When using `config_src = "cloudflare"`, cloudflared fetches ingress routing from Cloudflare's API at runtime — no local config file needed."* |
| ADR-0129 D6 (Accepted 2026-08-08) | *"The ingress mapping lives in the Cloudflare dashboard rather than in this repository, so there was never a diff here to make"*; AC-10 adds that the tunnel ingress rule *"requires owner action — … outside this repository and outside CI."* |

**Why master's stated inference does not decide it.** The premise — token-based cloudflared, no local
config file — is exactly what `config_src = "cloudflare"` produces. It establishes that the config is
*remotely managed*, which is a precondition of the Terraform resource, not evidence against it. Remote
management is compatible with Terraform ownership **and** with dashboard ownership; the premise
discriminates between neither.

**The live system cannot discriminate either.** The version counter (F2, `version=7`) increments on every
change regardless of who made it, and one such change landed 2026-08-08T05:01:37Z adding the `observe`
hostname. Terraform `apply` and a dashboard edit produce identical evidence here.

**What the owner can run to settle it** (private repo, one command, read-only):
```
terraform state list | grep -i tunnel_cloudflared_config
terraform plan          # clean plan + resource in state  => Terraform owns ingress
```

**One collateral fact that is determinable and useful:** the cloudflared container has
`RestartCount=0` and has been running since `2026-07-08T19:16:24Z`, yet fetched a new configuration on
`2026-08-08T05:01:37Z`. **Adding an ingress rule therefore requires no VPS restart, no compose change and
no redeploy** — it takes effect on the running connector. Whatever mechanism provisions the new hostname,
it does not disturb the live stack.

---

### F11 — Reachability from the Mac cannot be measured from this seat, and no VPS-side measurement substitutes for it

**Verdict:** UNVERIFIABLE (from this seat)

The commission names this as the constraint that decides correctness, and it is correct to: the VPS
reaches the Collector over the compose network (F8), which is exactly why the gap stayed invisible. This
session runs on the VPS and cannot execute anything on the Mac.

**What was searched.** No store reachable from here records a Mac-side connection attempt to a Collector
endpoint: no such hostname exists in the tunnel configuration (F2), so no Caddy access-log line, no
cloudflared ingress-rule error, and no `caddy-access-*` document can exist for it yet. The nearest
*positive* evidence that the general Mac↔VPS Cloudflare path is healthy right now is F3 (ES ingress
serving 201s) and this egress probe in the opposite direction:
```
curl http://127.0.0.1:8600/health   # Caddy egress block -> <slm-host> through CF Access
  -> HTTP 200 in 0.172s
```
which proves the Mac's cloudflared daemon and `slm_server` are live and that the Access service-token
handshake works — but proves nothing about a *new* ingress hostname.

**Consequence for whoever implements this.** The acceptance criterion must be executed **on the Mac** and
must be unfakeable from the VPS. The shape that satisfies both:

1. On the Mac, POST a span carrying a **nonce** attribute to `https://<otlp-host>/v1/traces` with the
   service-token headers → expect HTTP 200.
2. On the Mac, repeat **without** the headers to a *different* nonce path → expect an Access refusal
   (HTTP 403, matching F3's measured behaviour on `<es-host>`), not a timeout and not a 404.
3. On the VPS, query Tempo for the nonce from step 1 and find it; query for step 2's nonce and do not.

Step 2's distinct nonce is what stops a cached or replayed step-1 result from satisfying it — the same
two-nonce construction ADR-0129 AC-10(c) uses for Grafana.

---

### F12 — Filebeat is shipping Caddy access logs, so an ingress block gets an evidence trail for free

**Verdict:** POSITIVE

**The query, as run:**
```
curl -s 'http://localhost:9200/_cat/indices/caddy-access-*?h=index,docs.count'
```

**Its actual output:**
```
caddy-access-2026-08 1563
```

ADR-0132 D3's log capture is live. Any new Caddy site block inherits it: a `log` directive in the block
ships to `caddy-access-*` with no additional work, giving the ingress path the "one place to look when
connectivity is troubled" property that D3 exists to guarantee. A tunnel hostname pointed **directly** at
the Collector container — the Grafana/Kibana bypass topology — would have **no** such trail, because
nothing but the container would see the request. This is the concrete reason the Caddy hop earns itself
here, and it is a different reason from the routing-work argument in ADR-0129 D6.

---

## The mechanism comparison

Blast radius is stated as *what the credential buys an attacker who holds it*, which is the question the
commission asked.

| | **A. CF hostname → Caddy → Collector (OTLP/HTTP), Access Service Auth** | **B. SSH tunnel from the Mac** | **C. Mac-local Collector forwarding to the VPS** |
|---|---|---|---|
| **What becomes reachable** | One hostname on the public internet, refused at the CF edge without credentials (F3 measures this behaviour on the existing analogue). Behind it: **one path**, `^/v1/traces$`, on one container | Nothing new is published. The Collector stays compose-internal | Nothing new on the Mac; it still needs A or B underneath for the hop |
| **Authenticated how** | CF Access service token (`CF-Access-Client-Id`/`-Secret`), Service Auth policy, enforced at the edge before the VPS sees the request (F9) | An SSH key authorising a login on the VPS | Whatever A or B uses — but held by the Collector, not by `slm_server` |
| **Blast radius if the credential leaks** | Attacker can POST spans. **Write-only, one path, one signal.** Consequences: forged traces in Tempo, and unbounded write volume against a 512 MB / 0.5 CPU Collector. **No read access to anything** | Attacker gets **a shell on the VPS** — Neo4j, Postgres, Elasticsearch, every application credential, the `.env.caddy` service token, the lot. Categorically worse | Same as the underlying hop, plus: the Mac-side blast radius shrinks, because a compromise of `slm_server` no longer yields a Cloudflare credential |
| **Custody** | Token must live somewhere on the Mac | SSH key on the Mac | **Environment layer on the Mac**, per ADR-0132 D2's principle — `slm_server` holds nothing |
| **Availability / failure mode** | Rides the connector already running; Mac sleep = spans dropped at source | Session dies on sleep/network change; needs `autossh` supervision built from scratch | Local queue + retry across Mac sleep — the one thing today's direct-to-ES writer cannot do |
| **Evidence trail** | Caddy JSON access log → `caddy-access-*`, already shipping (F12) | None | Collector's own telemetry, plus A's trail |
| **Precedent on this project** | **Running in production for this exact producer** (F3, F4) | None | None |

**Why B is rejected outright.** It is the only option that trades a write-only, single-path telemetry
credential for one that grants interactive control of the box. For a signal whose worst-case compromise
is "fake spans in Tempo", that is a severe and unnecessary posture inversion. It is also the least
available option on a laptop that sleeps.

**Why A and C are not competitors.** C is a client-side custody and buffering decision; it still needs a
network hop, and A is the hop. The pairing also dissolves the protocol question: `slm_server` exports
**OTLP/gRPC to `localhost`** — preserving symmetry with the in-repo gateway (F5) — and the Mac Collector
re-emits **OTLP/HTTP** across the tunnel, so the edge never sees gRPC and F6's silent-bypass hazard never
arises. Chained collectors (agent → gateway) are ordinary OpenTelemetry topology, not a workaround.

**The one adjudication this forces.** ADR-0129 AC-7 requires that *"no artifact names an OTLP endpoint
other than the Collector"*, and FRE-1071 AC-5 asserts its effective-config names "the Collector's
endpoint". Under the recommendation, `slm_server`'s artifact would name `localhost:4317` — **a**
Collector, not **the** VPS one. D5's actual prohibition is that "no producer exports spans to a backend
directly", and a local Collector is not a backend, so the recommendation honours the decision while
violating the criterion's literal wording. That wording needs an explicit ruling rather than an
implementer's guess (Proposal 4).

---

## Proposals

Seven, and the cap is not a target.

1. **Provision `<otlp-host>` as a Cloudflare Tunnel ingress → Caddy → `otel-collector:4318`, path-scoped
   to `^/v1/traces$`, with a `log` directive.** Follows the FRE-411 `<es-host>` pattern measured working
   in F3, inherits the `caddy-access-*` evidence trail (F12), and confines a leaked credential to trace
   injection on one path. Contra the reading in FRE-1220's comment that the Caddy hop buys nothing: the
   allowlist *is* the blast-radius control, and ADR-0129 D6 names "a path allowlist for `es`" as an
   earned hop in exactly this sense.

2. **Gate it with a dedicated Access application, Service Auth policy, and its own service token — never
   the ADR-0132 D1 egress pair.** Service Auth is mandatory for a headless producer (F9). A separate
   token keeps the ingress blast radius disjoint from the VPS→Mac and VPS→artifacts egress surfaces.
   Record the token's expiry date somewhere that will be read before it lapses.

3. **Run a Collector on the Mac as the credential custodian and buffer; `slm_server` exports OTLP/gRPC to
   loopback and holds no Cloudflare credential.** This is ADR-0132 D2's environment-credential principle
   applied on the Mac side (F9), and it converts gRPC→HTTP locally so the edge never carries gRPC. It
   also adds retry across Mac sleep, which today's direct-to-ES writer lacks.

4. **Adjudicate ADR-0129 AC-7 / FRE-1071 AC-5 for a two-tier Collector topology** — does "the Collector"
   mean the VPS instance specifically, or any Collector in a chain terminating there? D5's prohibition is
   on exporting to a *backend*; the criteria's wording is narrower than the decision. Settle it in the
   ADR, not in an implementation PR.

5. **Record the Access/gRPC incompatibility as a standing constraint, and do not enable the zone-level
   gRPC toggle.** F6 is a zone-wide property: enabling gRPC would cause Access to ignore gRPC-content-type
   requests on *every* hostname in the zone, silently. This belongs in ADR-0129 or ADR-0132 as a durable
   constraint, because the next person to want gRPC through Cloudflare will otherwise re-derive it — or
   not.

6. **Resolve the Terraform-versus-dashboard contradiction between the April tunnel design docs and
   ADR-0129 D6, and record the answer once.** F10 shows the repository asserting both. The owner can
   settle it with one `terraform state list` in the private repo; whichever is true, one of the two
   in-repo statements is currently misleading and should be corrected rather than left to the next reader.

7. **Do not restart `slm_server` until the ingress is verified from the Mac, and record the sequencing
   lesson.** FRE-1071 removed the ES writer and added OTLP export in one change, so a restart today takes
   `slm_server` telemetry from working (F4: 586 documents, latest 2026-08-08T08:53:36Z) to dark. The
   acceptance procedure in F11 must run on the Mac and pass before that restart. Separately worth noting
   for future chains: the removal was only safe once the export landed somewhere, and the two halves
   could have shipped behind a flag.

---

## Filed tickets

All six were filed to **Backlog** by this study. This list is the completeness instrument: any ticket
traceable to this study and absent from it is a violation.

- [FRE-1223](https://linear.app/frenchforest/issue/FRE-1223) — Provision `<otlp-host>` → Caddy → Collector 4318, path-scoped, behind Access Service Auth (Proposals 1, 2)
- [FRE-1224](https://linear.app/frenchforest/issue/FRE-1224) — Mac-local OTel Collector as credential custodian and buffer (Proposal 3)
- [FRE-1225](https://linear.app/frenchforest/issue/FRE-1225) — Adjudicate ADR-0129 AC-7 / FRE-1071 AC-5 for a chained-Collector topology (Proposal 4)
- [FRE-1226](https://linear.app/frenchforest/issue/FRE-1226) — Record the Cloudflare Access/gRPC incompatibility as a standing constraint (Proposal 5)
- [FRE-1228](https://linear.app/frenchforest/issue/FRE-1228) — Resolve the Terraform-vs-dashboard contradiction on tunnel ingress ownership (Proposal 6)
- [FRE-1230](https://linear.app/frenchforest/issue/FRE-1230) — Mac-side acceptance procedure for OTLP ingress, and the `slm_server` restart gate (Proposal 7)

---

## Method appendix

**What was measured against.** The live production stack on the VPS, 2026-08-08, read-only throughout:
the Docker daemon (container inspection, logs, one throwaway container on the compose network),
Elasticsearch on `localhost:9200`, the gateway's `/telemetry/effective-config` on `127.0.0.1:9001`, the
Caddy egress listener on `127.0.0.1:8600`, and one unauthenticated HTTPS request to the existing
`<es-host>` from the VPS. Repository claims were read at `origin/main` (`23ea9b03`), not at the stale
worktree checkout — the working tree began at `41e76267`, which predates the Collector's existence, and
reading it would have produced a false "no Collector is deployed" finding.

**Stores and windows.**

| Store | Index / object | Window |
|---|---|---|
| Elasticsearch | `slm-requests-*` | 24h / 48h / 7d / 30d, and all-time |
| Elasticsearch | `caddy-access-*` | current monthly index |
| Docker | `cloud-sim-cloudflared` logs | container lifetime from 2026-07-08T19:16:24Z |
| Docker | `cloud-sim-caddy` logs | in-container ring buffer at time of read |
| Docker | host-config / exposed-ports for 4 containers | instantaneous |
| Gateway process | `/telemetry/effective-config` | instantaneous |

**Identifier resolutions performed.**

- `@timestamp` → **`ts`** for the `slm-requests-*` family. The first count returned 0 on `@timestamp`
  against indices visibly holding 2026-08-08 documents; a raw document settled the field name. Recorded
  in F4 because an acceptance criterion written on `@timestamp` here would pass vacuously.
- Compose network name `cloud-sim` → **`seshat_cloud-sim`**. Two probe containers failed to start against
  the un-prefixed name before this was resolved; the F8 measurements are from the successful run.
- Gateway effective-config route → **`/telemetry/effective-config`** (not `/api/observability/...`),
  resolved from the live OpenAPI document.

**What was rejected, and why.**

- **Reasoning about gRPC-through-tunnel from general knowledge.** The commission forbade it and was right
  to: the operative constraint (Access *ignores* gRPC rather than failing closed) is not something
  general knowledge reliably supplies, and it inverts the risk assessment. All four Cloudflare claims are
  quoted verbatim from `developers.cloudflare.com`.
- **Verifying reachability from the VPS.** Explicitly declined; F8 is scoped as a *receiver* measurement
  and F11 is recorded UNVERIFIABLE rather than dressed up. This is the failure mode the commission named.
- **Testing an authenticated request to `<es-host>` using the service token.** The pair is no longer in
  `/opt/seshat/.env` (ADR-0132 D1, verified), and handling it out of `.env.caddy` was unnecessary: F3's
  measured 201s from the Mac are a better positive control than one this session could manufacture.
- **Any Cloudflare mutation.** No dashboard, API or Terraform action was taken. The single external
  request (F3's 403 probe) is a request any internet client could make and changes no state.

**Known limits of this study.**

- Whether Terraform owns the tunnel's ingress rules is **not determinable from here** (F10); the private
  repository holds the answer.
- Reachability from the Mac is **not measurable from here** (F11); the acceptance procedure is specified
  instead of executed.
- Cloudflare's documented behaviour is taken from its documentation, not from an experiment against the
  live zone — no gRPC request was sent through the tunnel, and enabling the zone toggle to test it would
  itself be the change F6 warns against.
- Elasticsearch counts are provisional (FRE-1051): the `slm-requests-*` figures in F4 are a floor, not a
  guaranteed-complete census.

---

## References

- [FRE-1220](https://linear.app/frenchforest/issue/FRE-1220) — commissioning ticket and its comment on the two edge layers
- `docs/architecture_decisions/ADR-0129-opentelemetry-instrumentation-and-trace-visibility.md` — D5 (Collector as trace egress), D6 (exposure topology, the earned-Caddy-hop argument), D7 (`Nothing goes off-box` — egress scope), AC-7, AC-10
- `docs/architecture_decisions/ADR-0132-outbound-authenticated-egress.md` — D1 (Caddy terminates outbound CF Access), D2 (environment vs application credentials), D3 (Caddy access-log capture)
- `docs/architecture_decisions/ADR-0045-infrastructure-cloud-knowledge-layer.md` — Terraform lives in the private repo
- `docs/architecture_decisions/ADR-0064-inbound-user-identity-cloudflare-access.md` — laptop-held Terraform state for Access applications
- `docs/superpowers/specs/2026-04-16-cloudflare-tunnel-terraform-design.md` · `docs/superpowers/specs/2026-04-17-mac-slm-tunnel-design.md` · `docs/superpowers/plans/2026-04-17-mac-slm-tunnel.md` — the `config_src = "cloudflare"` ingress design
- `config/cloud-sim/Caddyfile` — the `<es-host>` ingress block (FRE-411) and the `:8600`/`:8601` egress blocks (ADR-0132 D1)
- [gRPC connections — Cloudflare Network settings](https://developers.cloudflare.com/network/grpc-connections/)
- [gRPC — Cloudflare Tunnel use cases](https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/use-cases/grpc/)
- [Service tokens — Cloudflare One](https://developers.cloudflare.com/cloudflare-one/access-controls/service-credentials/service-tokens/)
- [Access policies — Cloudflare One](https://developers.cloudflare.com/cloudflare-one/access-controls/policies/)
