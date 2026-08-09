# ADR-0136: The Cloudflare Edge Carries HTTP, Not gRPC — the Zone gRPC Toggle Stays Off, and Protocol Conversion Happens Before the Edge

**Status:** Accepted
**Date:** 2026-08-09
**Deciders:** Owner + adr session (FRE-1226)
**Tags:** security, infrastructure, cloudflare, access, otlp, observability, constraint

---

## Context

**What is the issue we're addressing?**

The FRE-1220 study, looking for a way to get `slm_server`'s OTLP spans from the Mac to the VPS
Collector, found that the obvious symmetry — the in-repo gateway exports OTLP over gRPC, so the Mac
should too — is not awkward across the Cloudflare edge but **unavailable**, and that the configuration
change which looks like it would fix that is a **silent security failure**.

**Two independent walls, and their order matters.** First, Cloudflare Tunnel documents gRPC as unsupported
on public hostname deployments at all — which is the only mode this deployment runs. Second, and separately,
the zone-level setting that makes Cloudflare's reverse proxy handle gRPC would, if enabled, stop Access
enforcing on gRPC-content-type requests across **every** hostname in the zone, with the policies still
displaying as configured. So enabling the toggle would not even deliver a working public-hostname path; it
would only pay the security cost.

This ADR records that as a standing constraint. It is deliberately small: it rules on what may cross the
Cloudflare edge and what may not, and it decides nothing about the OTLP ingress design itself.

### Why this needs an ADR rather than a reference note

The verdict is easy — no telemetry convenience is worth the deployment's only edge authentication layer.
What is *not* easy is seeing why the question even arises, because **a competent reader arrives with a
wrong mental model in a specific, predictable way**: "it's a tunnel, and tunnels carry things." That model
is the reason `--protocol http2` on the cloudflared command line looks like it might be relevant (it is
not), and the reason gRPC looks merely inconvenient rather than unavailable.

So the durable content here is the layered picture, not the verdict. Without it the ruling reads as an
arbitrary prohibition and the next person re-derives it — badly, or not at all.

### Three different things are called "protocol"

```
[off-box producer]                                          [VPS]
      │                                                       │
      │  ①  HTTPS request                                     │
      ▼                                                       │
┌──────────────────────┐    ②  tunnel carrier      ┌────────────────┐   ③
│  Cloudflare edge     │◄────────────────────────► │  cloudflared   │──────► Caddy ──► service
│  (L7 reverse proxy)  │       QUIC or HTTP/2      │  (outbound)    │
│  TLS terminated      │       ← --protocol http2  │                │
│  WAF · Access        │                           └────────────────┘
│  zone gRPC toggle    │
│  lives HERE          │
└──────────────────────┘
```

- **① Client → edge.** Ordinary HTTPS. The *content* may be gRPC framing (HTTP/2, `content-type:
  application/grpc`, trailers). This is the only layer at which the gRPC question lives.
- **② Edge → cloudflared.** What `--protocol` selects: QUIC (default) or HTTP/2, as the carrier for the
  tunnel's own plumbing. It says nothing about which application protocols may ride inside. *(measured:
  `docker-compose.cloud.yml:584` — `tunnel --no-autoupdate --protocol http2 run`.)*
- **③ cloudflared → local service.** Whatever the ingress rule names — here, `http://caddy:80` and
  `http://<container>:<port>` *(measured: live tunnel configuration v7, FRE-1220 F2)*.

**Conflating ② with ① is what made gRPC look tractable.** Setting the carrier to HTTP/2 does not enable
gRPC; the two are independent.

### The tunnel has two modes, with two different security models

| | **Public hostname** (what we run) | **Private network** (WARP) |
|---|---|---|
| What Cloudflare does | L7 reverse proxy — terminates TLS, parses and reconstructs HTTP | Carries a private CIDR; no HTTP parsing |
| gRPC | **Not supported for public hostnames**; and would additionally require the zone toggle | **Supported** |
| Who authenticates | Access application policy — IdP login, or a service token for headless clients | **Device enrolment** — WARP client, Split Tunnels, Zero Trust org |
| Our state | six hostnames, all L7 | `warp-routing: enabled=false` *(FRE-1220 F2)* |

**What is documented**, and what the decision rests on: gRPC is supported via private subnet routing and
unsupported on public hostname deployments; the private path's prerequisites are WARP client deployment,
Split Tunnel configuration and Zero Trust enrolment — so its authentication is device-based rather than an
Access application policy.

**What is reasoned**, marked as such because Cloudflare does not document its internals: the split appears
to track whether the edge is parsing HTTP at all. In public-hostname mode it terminates TLS and reconstructs
requests, so it can only forward what it understands; private routing carries a CIDR, where there are no
requests to parse and correspondingly no Access application to apply. A reader may check this inference —
**nothing below depends on it.**

**Our topology joins no network at all.** cloudflared opens an *outbound* connection from the VPS and holds
it; there is no inbound port and no route table. That is a real security property. Its cost is that
everything crossing must be something the proxy can read and reconstruct.

### The documented facts, quoted rather than paraphrased

From [gRPC connections — Cloudflare Network settings](https://developers.cloudflare.com/network/grpc-connections/)
(page last updated 2026-04-23):

> "Cloudflare Access does not support gRPC traffic sent through Cloudflare's reverse proxy."

> "gRPC traffic will be ignored by Access if gRPC is enabled in Cloudflare."

> "We recommend disabling gRPC for any sensitive origin servers protected by Access or enabling another
> means of authenticating gRPC traffic to your origin servers."

> "When gRPC is not enabled on a zone, Cloudflare will respond to gRPC requests with a `403 Forbidden`
> response."

From [gRPC — Cloudflare Tunnel use cases](https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/use-cases/grpc/):

> "Cloudflare Tunnel supports gRPC traffic via private subnet routing."

> "Public hostname deployments are not currently supported."

### What is true here today

- The zone carries **six public hostnames** — `agent`, `api`, `es`, `graph`, `monitoring`, `observe`
  *(measured: FRE-1220 F2, live tunnel configuration v7)*.
- **Cloudflare Access is the only edge authentication layer this deployment has.** ADR-0129 D6 records the
  consequences already: a stolen Access session "defeats only the edge", and "anything reaching the compose
  network bypasses the edge entirely."
- The in-repo gateway exports OTLP **over gRPC to a compose-network address** — `otel-collector:4317`,
  plaintext *(measured: FRE-1220 F5, `/telemetry/effective-config`)*. This is inside the trust boundary and
  is not affected by anything decided here.
- The Collector's **OTLP/HTTP receiver on 4318 accepts an ordinary JSON POST and answers 200** *(measured:
  FRE-1220 F8)* — the request shape Caddy can path-scope and Access can gate.
- An off-box producer ingress of exactly this shape **already runs in production**: the Mac writes telemetry
  to `<es-host>` over plain HTTP/1.1 through the tunnel, path-allowlisted at Caddy, gated by Access, with an
  unauthenticated request measured returning 403 *(measured: FRE-1220 F3)*.
- **Measured OTLP volume from the off-box producer is a trickle** — 586 documents all-time, 3 in 24 hours
  *(measured: FRE-1220 F4)*. Any wire-efficiency argument for gRPC is unmeasurable at this scale.

---

## Decision

### D1 — The zone-level gRPC toggle stays off, and no gRPC crosses the Cloudflare edge

**Cloudflare's zone-level gRPC setting is not to be enabled.** No design, ticket or implementation may
propose enabling it as a step toward carrying any signal, telemetry or otherwise.

**This is a prohibition, not a prevention, and the difference is stated rather than glossed.** The setting
lives in a control plane this repository does not own. Nothing here can stop it being flipped; what this
ADR does is make the flip *ruled against* and *detectable* (AC-1). Writing "prevented" would assert a
control that does not exist — the same class of overclaim ADR-0129 D8 refuses when disposing of ADR-0128.

**The ruling rests on a documented vendor behaviour and carries its expiry with it.** It holds because
Access *ignores* gRPC rather than refusing it, and because the toggle's scope is the zone rather than the
hostname. **If Cloudflare ships Access enforcement for gRPC, or makes the setting per-hostname, this
decision is to be revisited rather than obeyed.** gRPC is not itself a security risk — it is a wire
protocol. The risk is entirely the edge's failure mode on it, and stating the rule any other way would age
into a false instruction.

### D2 — gRPC stays inside the trust boundary; protocol conversion happens before the edge

**gRPC is a service-to-service protocol and is used as one here.** It is not natively usable from a browser
— browsers cannot control HTTP/2 framing, which is why browser gRPC requires `grpc-web` plus a translating
proxy.

**Why Access does not support gRPC is not documented by Cloudflare, and this decision does not need a
reason** — the behaviour is documented, and that is what the rule rests on. One tempting explanation should
be resisted: "a gRPC client has no browser to follow an identity redirect" does not explain it, because
Access serves headless HTTP clients perfectly well through Service Auth tokens — the very mechanism the
OTLP ingress design uses.

So the rule follows the protocol's own design rather than working around it:

- **Inside the trust boundary** — the compose network, loopback on a single host — **OTLP/gRPC is the
  normal choice** and the in-repo gateway's `otel-collector:4317` export stands unchanged.
- **Across the Cloudflare edge, OTLP travels as HTTP.** Where a producer prefers gRPC locally, the
  conversion happens **before** the edge, on the producer's side.

The resulting asymmetry — gRPC on the VPS, HTTP at the edge hop — is the design, not a compromise, and
chained collectors (agent → gateway) are ordinary OpenTelemetry topology.

### D3 — Before enabling any zone-level Cloudflare setting, establish its scope against the whole inventory

Stated as an investigative habit, deliberately **not** as a blanket ban: a zone-level setting is assessed
against **every hostname in the zone**, not against the hostname motivating the change, and specifically
against whether it alters what Access enforces. The cost is one lookup; the failure it guards against is
silent and zone-wide.

This generalises the *method*, not the verdict. No claim is made here that other zone settings behave like
the gRPC toggle — that is exactly what the lookup is for.

### D4 — What this ADR does not decide

Named explicitly so this document is not read as having settled them:

- **The OTLP ingress design** — the hostname, the Caddy block, the Access application and its service token
  (FRE-1223), and the Mac-local Collector as credential custodian (FRE-1224).
- **ADR-0129 AC-7 / FRE-1071 AC-5 for a chained-Collector topology** — whether "the Collector" means the VPS
  instance or any Collector in a chain terminating there (FRE-1225).
- **Whether Cloudflare objects are Terraform-owned** (FRE-1228), and therefore whether the enforcement
  mechanism in Implementation Notes is available.
- **The broader edge-authentication posture** — whether components behind Access should hold a second,
  independent authentication layer. Surfaced during this discussion, deliberately left open, and reasoned
  about in Consequences rather than decided.

---

## Alternatives Considered

### Option 1: Enable the zone gRPC toggle and add an independent origin-side authentication layer

**Description:** Turn on zone gRPC, then follow Cloudflare's own recommendation — "enabling another means of
authenticating gRPC traffic to your origin servers" — to replace what Access stops doing.

**Pros:**
- Protocol symmetry with the in-repo gateway's gRPC exporter.
- Adding origin-side authentication is Cloudflare's own stated remedy for the Access gap, so the mitigation
  is at least the vendor-recommended one rather than an improvisation.

**Cons:**
- **It does not actually produce a working path.** The toggle governs Cloudflare's reverse proxy; Tunnel
  separately documents gRPC as unsupported on public hostname deployments, which is the only mode we run.
  Enabling it pays the security cost without delivering the capability.
- Because the toggle is zone-wide, the replacement authentication is needed on **every** hostname that could
  receive a gRPC-content-type request — not only the endpoint motivating the change.
- Builds a second, independent authentication layer across the entire surface to carry one telemetry stream.
- The failure mode while it is being built is silent: policies keep displaying as configured.

**Why Rejected:** **Refused on principle, and separately non-functional.** The principled refusal stands on
its own: this trades the deployment's only edge authentication layer — silently, across every hostname it
fronts — to satisfy one endpoint's protocol preference, and what it purchases is unmeasurable (OTLP export
is unary calls, OTLP/HTTP also carries protobuf, and measured volume is 3 documents in 24 hours, F4). A
rewrite of the authentication layer to buy nothing measurable is not a trade-off to weigh.

The functional objection is recorded second **deliberately**, because it is the weaker guarantee: vendor
support matrices change, and if Cloudflare later supports gRPC on public hostnames the first objection is
the one that still holds.

### Option 2: WARP + private subnet routing — the documented supported gRPC path

**Description:** Enable `warp-routing` on the tunnel, deploy the Cloudflare One client on the producer
machine, configure Split Tunnels to route the private CIDR through it, and enrol the device in the Zero
Trust organization. gRPC then works, authenticated by device enrolment.

**Pros:**
- The only path Cloudflare documents as actually supporting gRPC.
- Device-level authentication is a coherent security model, not a weakening of one.
- Previously run on this project, so it is known to work here.

**Cons:**
- Re-plumbs an entire machine's network posture to solve a single telemetry hop.
- Requires device enrolment and Split Tunnel management as an ongoing concern.
- The producer machine's ability to reach the deployment becomes conditional on a vendor client being
  installed, running, enrolled and logged in.

**Why Rejected:** **On layering, with a test a future reader can apply: does the mechanism confine the
vendor dependency to the deployment, or push it onto a device?** WARP pushes it onto the device — in the
owner's words from the discussion that produced this ADR, *"you open the WARP app, not Seshat"*, having run
it earlier in this deployment's life and found it confining.

That is project doctrine in two prior decisions, not a preference: ADR-0129 D5 chose the vanilla upstream
Collector over vendor distributions because "choosing the neutral one is what keeps the backend a
configuration line rather than a commitment," and ADR-0132 D2 scopes the Cloudflare credential as an
*environment* credential — "a deployment without Cloudflare has none of those barriers."

The obvious objection — *the tunnel is already Cloudflare, so how is WARP more coupled?* — does not hold,
because the exit costs differ by an order of magnitude. Today Cloudflare is a compose service and a header
pair any reverse proxy could demand; replacing it changes a compose service and a hostname. With WARP it is
an enrolled agent mediating a machine's network; replacing it means uninstalling that agent and rebuilding
network configuration on every device.

### Option 3: An SSH tunnel from the producer machine

**Description:** Skip the Cloudflare edge entirely; forward a local port to the Collector over SSH.

**Pros:**
- Nothing new is published; the Collector stays compose-internal.
- No Cloudflare capability question arises at all.

**Cons:**
- Requires `autossh`-class supervision built from scratch; the session dies on sleep or network change.
- No evidence trail — nothing equivalent to Caddy's access log reaching `caddy-access-*` (F12).

**Why Rejected:** **Posture inversion.** It is the only option that trades a write-only, single-path
telemetry credential for one granting interactive control of the box — a leaked SSH key yields Neo4j,
Postgres, Elasticsearch and every application credential. For a signal whose worst case is forged spans in
Tempo, that is a severe and unnecessary exchange, and it is also the least available option on a laptop
that sleeps.

### Option 4: Record the finding as a reference note rather than an ADR

**Description:** Put the Cloudflare quotations in `docs/reference/` and cite them from the ingress ticket.

**Pros:**
- Proportionate to what looks like a transcription of four vendor sentences.
- No ADR number spent on a decision the owner characterised as easy.

**Why Rejected:** The content that has to survive is not the quotations — it is the **decision plus the
mental model that makes it non-obvious** (Context). Three genuine alternatives exist, two of them
Cloudflare's own documented paths, and rejecting them carries stated principles that bind future work.
That is an ADR's job, and a reference note carries no ruling the next design is obliged to honour.

---

## Consequences

### Positive Consequences

- **No hostname loses Access enforcement.** The only edge authentication layer this deployment has continues
  to apply wherever it is configured. Stated that way deliberately: **which** of the six hostnames carry an
  Access application is *not* established here — the study's tunnel-configuration read "claims nothing about
  any Cloudflare object not represented in the tunnel's ingress document (Access applications, DNS records
  and WAF rules are not visible in this store)" (F2), and Access was positively measured on one hostname
  only (F3). What this decision preserves is that the toggle does not remove enforcement from wherever it
  exists; enumerating that inventory is part of the broader question below, not a claim made here.
- **The OTLP ingress design is constrained before it is built**, rather than adjudicated in an
  implementation PR — the edge hop is HTTP, and FRE-1223/1224 inherit that as settled.
- **gRPC is used as designed**, inside the trust boundary, rather than forced through a proxy that must
  parse what it forwards.
- **The trap is recorded where the next person will meet it.** Both the Access-ignores-gRPC failure mode and
  the `--protocol http2` conflation now have a durable home.
- **The rejection reasons are testable rather than tasteful** — particularly Option 2's layering test, which
  applies to any future vendor-capability question, not just this one.

### Negative Consequences

- **There is no enforcement mechanism, only a ruling.** The setting lives in a control plane outside this
  repository, and D1 says so plainly rather than implying otherwise.
- **A protocol asymmetry now needs explaining** to anyone reading the two exporter configurations side by
  side — gRPC on the VPS, HTTP at the edge. D2 and the Context diagram are the mitigation.
- **The supported gRPC path is foreclosed for as long as this stands.** If a future need genuinely requires
  gRPC across the edge, Option 2 has to be reopened rather than worked around — which is the intended
  behaviour, but it is a real closed door.
- **A broader question was surfaced and deliberately not answered.** ADR-0129 D6 justified Grafana's
  anonymous `Viewer` posture on explicit equivalence grounds — "Kibana today has all three properties,
  against the same Elasticsearch, behind the same single gate." When Kibana is retired (FRE-1214), that
  comparator ceases to exist and D6's stated justification voids by its own terms. This ADR notes the
  expiry and does not resolve it; it is filed separately as
  [FRE-1233](https://linear.app/frenchforest/issue/FRE-1233).

### Risks and Mitigations

| Risk | Severity | Mitigation |
|------|----------|------------|
| Someone enables the zone gRPC toggle without reading this ADR; Access silently stops enforcing zone-wide | High | AC-1 makes the setting's state directly readable and failable; D3 makes scope-checking the habit for any zone setting |
| Cloudflare changes Access's gRPC behaviour and this ADR becomes a stale prohibition | Medium | D1 states the expiry condition explicitly — the ruling is conditional on the documented behaviour and is to be revisited, not obeyed, if it changes |
| An implementer reads `--protocol http2` as enabling gRPC and re-derives the wrong conclusion | Medium | The Context diagram separates the three layers; Implementation Notes records the conflation by name |
| A producer's OTLP config drifts to gRPC against a public hostname after the ingress lands | Medium | AC-2 reads runtime state rather than repository text, derives its producer population from observed spans rather than a hand list, and follows each Cloudflare-fronted path through Caddy to its upstream. Partial by construction — AC-2 states which paths it does not cover |
| The prohibition is the only control, and nobody checks it again after adjudication | Medium | Accepted and stated. A recurring probe for a setting nobody intends to flip is disproportionate; the gap is named in Implementation Notes rather than papered over |

---

## Implementation Notes

**This ADR changes no code and requires no implementation chain.** No file in this repository is modified by
the decision itself; its output is the ruling, its criteria, and the seam ticket that asserts them.

**The conflation to record by name.** `--protocol http2` on the cloudflared command line selects the
**cloudflared↔edge transport** — HTTP/2 instead of QUIC. It says nothing about which application protocols
the tunnel can carry. Reading it as gRPC-adjacent is what made gRPC look merely inconvenient rather than
unavailable, and it is the single most likely way this ruling gets re-litigated.

**The one candidate enforcement mechanism, and why it is not adopted here.** Declaring the zone's gRPC
setting in Terraform would turn an out-of-band flip from invisible into a `plan` diff — the only mechanism
identified that converts this prohibition into something detectable without a human remembering to look. It
is **not available today**: no zone *setting* is known to be Terraform-declared, and whether Terraform owns
Cloudflare tunnel objects at all is the open contradiction FRE-1228 exists to settle *(FRE-1220 F10 records
the repository asserting both answers)*. It is carried as an open remedy on the seam ticket rather than
proposed as work here.

**Where the constraint binds in practice.** The producer-side conversion point (D2) is the Mac-local
Collector of FRE-1224; the edge-side shape it must produce is the OTLP/HTTP receiver measured in F8. Neither
is decided by this ADR — they are named so the constraint has a visible landing site.

**A black-box external probe was drafted as a third criterion and dropped; the reasoning is kept so it is
not re-attempted.** The idea was to send a request carrying `content-type: application/grpc` to a zone
hostname and rely on Cloudflare's documented "when gRPC is not enabled on a zone, Cloudflare will respond to
gRPC requests with a `403 Forbidden`". It fails as an instrument on three counts, any one of which is
disqualifying:

- **A content-type header is not gRPC.** Cloudflare's own enabling requirements name port 443, TLS, HTTP/2
  advertised over ALPN *and* the content type. A plain HTTPS request wearing the header may never exercise
  the path the criterion claims to test.
- **403 is ambiguous.** Access returns 403 to unauthenticated requests as well — measured on `<es-host>`
  (F3) — so a bare 403 cannot distinguish "gRPC is disabled" from "Access refused me", and the origin can
  produce a 403 of its own.
- **The disambiguating variant rests on an undocumented ordering.** Sending the same request *with* valid
  service-token headers only discriminates if the gRPC check precedes Access, which Cloudflare does not
  state. That is an inference, and a criterion resting on an unverified inference verifies nothing.

AC-1 reads the setting directly and carries the constraint without any of this. Shipping the probe as a
conditional criterion would have added ceremony, not assurance.

---

## Verification / Acceptance Criteria

**How will we know this decision actually delivered — not just merged?**

These are the **ADR's own** criteria. They are asserted in exactly one place — the seam ticket named below —
and are never sliced across other tickets (ADR-0130 D1).

A note on what they measure, because the two were conflated during authoring and the distinction is
load-bearing: **the decision's soundness rests on Cloudflare's documentation and is not contingent on any
measurement.** If the zone can never be probed, "do not enable the toggle" is still correct. What these
criteria measure is the constraint's **observance** — whether it is held today, and whether a breach would
be visible.

- **AC-1 — The zone's gRPC setting reads *disabled*.** · **Check:** the owner reads the zone's gRPC setting
  directly, in the Cloudflare dashboard (Network settings) or via the Cloudflare API, and records the
  observed value. · *Fails if* the setting reads enabled — in which case Access is not enforcing on
  gRPC-content-type requests for any hostname in the zone, and that is the finding. This criterion requires
  an owner action by design; ADR criteria are permitted to (ADR-0130 D1).

- **AC-2 — gRPC OTLP stays inside the trust boundary, and no Cloudflare-fronted path carries it.**

  **Scope, stated first because drafting got it wrong in both directions.** This criterion asserts **all of
  D2**, whose rule is the broader of its two clauses: gRPC belongs *inside the trust boundary* — loopback or
  the compose network — and only the HTTP conversion of it crosses the Cloudflare edge. So a producer
  exporting gRPC to **any** endpoint outside loopback/compose fails here, Cloudflare-fronted or not; the
  failure predicate below is deliberately that wide, and matches D2 rather than narrowing it.

  ADR-0129 D7's "Nothing goes off-box. No SaaS exporter is configured" is **adjacent, not a delegation** —
  it is broader in a different dimension (every signal, not only gRPC) and narrower in this one (it does not
  speak to protocol). Neither criterion discharges any part of the other.

  **Three arms, all required.** A self-reported artifact alone passes vacuously — a producer publishing no
  artifact satisfies it by being invisible — and a port-number check alone misses indirection.

  - **(a) Producer census, derived rather than hand-listed.** Enumerate producers as the union of: every
    distinct `service.name` that produced spans in Tempo over the adjudication window — an *observed*
    census, which catches a producer nobody declared, because a producer successfully crossing the edge is
    by construction visible downstream — **and** the ADR-0129 chain's named producers (the gateway,
    `slm_server`, any Collector from FRE-1224), which catches one that is configured but not currently
    delivering. For each, read its effective-configuration artifact (the gateway's is served at
    `/telemetry/effective-config`, F5) and assert every gRPC endpoint resolves to loopback or a
    compose-network service name. **A producer in the union with no readable artifact is a FAIL, not a pass.**
  - **(b) Path census — no Cloudflare-fronted path terminates at a gRPC receiver.** Read the live tunnel
    ingress rules (F2's method, the connector's logged configuration); for each rule routing into Caddy,
    follow it through to the Caddyfile site block it lands in; assert no resulting upstream is an OTLP gRPC
    receiver as declared in the Collector's own configuration. Following the Caddy hop is the point — a
    check for literal port 4317 in the ingress rules would miss every indirect route, and most of our rules
    are indirect.
  - **(c) No direct exposure that neither arm above would see.** The Collector container publishes no host
    port (F1's method — read the container's port bindings in full, not just grep for 4317).

  · *Fails if* any producer in the census pairs `grpc` with an endpoint outside loopback/compose, **or** any
  producer in the census publishes no readable artifact, **or** any Cloudflare-fronted path resolves to a
  gRPC receiver, **or** the Collector publishes a host port. Non-vacuous today: the gateway's
  `otel-collector:4317` satisfies (a) and would fail it if repointed at `<otlp-host>`; (b) is satisfied by
  the measured six-rule configuration and would fail if any of them, directly or through Caddy, reached a
  gRPC receiver.

  **What a green result does not prove, stated rather than implied.** It is evidence about the paths
  enumerated above — not a proof that no gRPC leaves this host by any means. An unenumerated egress path (a
  second connector, a host process forwarding on its own) sits outside these arms. Closing that would take a
  full network egress audit, which is disproportionate to this decision and is not claimed here.

**Seam ticket:** **[FRE-1232](https://linear.app/frenchforest/issue/FRE-1232)** — *Adjudicate ADR-0136 — the
Cloudflare zone gRPC constraint*. Filed parked
(`Backlog`), **due 2026-08-23**. This ADR has no implementation chain, so its criteria are adjudicable as
soon as it is accepted; the due date allows for merge plus the owner-executed AC-1 read. Master activates it
at the first advance-dispatch on or after that date, and an `adr` session adjudicates it.

---

## References

- [FRE-1226](https://linear.app/frenchforest/issue/FRE-1226) — the commissioning ticket (FRE-1220 study, Proposal 5)
- [FRE-1220](https://linear.app/frenchforest/issue/FRE-1220) — the commissioning study's own ticket
- `docs/research/2026-08-08-fre-1220-otlp-ingress-security-and-cloudflare-capability.md` — findings F2, F3, F4, F5, F6, F7, F8, F9, F10, F12
- `docs/architecture_decisions/ADR-0129-opentelemetry-instrumentation-and-trace-visibility.md` (Accepted) — D5 (Collector as trace egress), D6 (exposure topology, single-gate posture, Kibana retirement directed), D7 (scope), D8 (the overclaim standard)
- `docs/architecture_decisions/ADR-0132-outbound-authenticated-egress.md` (Accepted) — D1 (Caddy terminates outbound CF Access), D2 (environment vs application credentials), D3 (Caddy access-log capture)
- `docs/architecture_decisions/ADR-0064-inbound-user-identity-cloudflare-access.md` (Accepted) — inbound Access for user identity; Alternative C (trusting the tunnel without JWT verification) rejected
- `docs/architecture_decisions/ADR-0045-infrastructure-cloud-knowledge-layer.md` (Accepted) — Terraform lives in the private repository
- `docs/architecture_decisions/ADR-0130-two-tiers-of-acceptance-criteria.md` (Accepted) — D1/D2, the seam-ticket contract these criteria follow
- [FRE-1223](https://linear.app/frenchforest/issue/FRE-1223) — OTLP ingress provisioning (constrained by D2, not decided here)
- [FRE-1224](https://linear.app/frenchforest/issue/FRE-1224) — Mac-local Collector as credential custodian (the D2 conversion point)
- [FRE-1225](https://linear.app/frenchforest/issue/FRE-1225) — ADR-0129 AC-7 adjudication for a chained-Collector topology (out of scope per D4)
- [FRE-1228](https://linear.app/frenchforest/issue/FRE-1228) — Terraform-vs-dashboard ownership of Cloudflare objects (gates the Implementation Notes enforcement mechanism)
- [FRE-1232](https://linear.app/frenchforest/issue/FRE-1232) — **this ADR's seam ticket**; the sole place AC-1 and AC-2 are asserted (ADR-0130 D2)
- [FRE-1233](https://linear.app/frenchforest/issue/FRE-1233) — ADR-0129 D6's anonymous-Viewer justification expires when Kibana is retired (surfaced by this discussion, out of scope per D4)
- [gRPC connections — Cloudflare Network settings](https://developers.cloudflare.com/network/grpc-connections/)
- [gRPC — Cloudflare Tunnel use cases](https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/use-cases/grpc/)
- [Service tokens — Cloudflare One](https://developers.cloudflare.com/cloudflare-one/access-controls/service-credentials/service-tokens/)

---

## Status Updates

### 2026-08-09 — Accepted

**Changed By:** Owner, ruled in session (FRE-1226)

**Reason:** The owner ruled during the authoring discussion: *"We are not going to break cloudflare auth
just so we can have otlp via grpc. That is an easy decision."* Three points were settled by challenge
during that discussion and are recorded because each changed the document:

1. **The prohibition is conditional, not absolute.** An earlier framing — "gRPC is a security risk" — was
   rejected as false and as certain to age into a wrong instruction. gRPC is a wire protocol; the risk is
   Access's failure mode on it. D1 carries the expiry condition.
2. **"Prohibited and detectable", not "prevented".** No control in this repository can stop a dashboard
   setting being changed. D1 states that rather than implying an enforcement that does not exist.
3. **Option 2's rejection rests on layering, not ergonomics.** The owner had previously run WARP on this
   deployment and found it confining — *"you open the WARP app, not Seshat"* — which, stated as a layering
   test rather than a preference, matches ADR-0129 D5 and ADR-0132 D2 and is checkable by a future reader.

---

**Template Version:** 1.1
**Based On:** [Michael Nygard's ADR pattern](https://cognitect.com/blog/2011/11/15/documenting-architecture-decisions)
