# ADR-0141: One LLM Dispatch Path — Every Call Rides litellm, and the Wire Is Verified, Not Assumed

**Status:** Proposed
**Date:** 2026-09-03
**Deciders:** Project owner + adr session (FRE-1362)
**Tags:** llm-client, dispatch, litellm, egress, concurrency, configuration

---

## Context

Owner-directed 2026-09-03: *"Uniformity in making LLM calls. One code path — DSPy may remain the
outlier for now — but everything else goes through litellm."* This closes ADR-0031's Alternative C,
deferred since March 2026, and supersedes ADR-0121's "Vocabulary by dispatch path" rule.

### The finding that forced this

Seshat sends two dispatch shapes today. `LiteLLMClient` handles cloud placement via
`litellm.acompletion()`. `LocalLLMClient` builds a payload dict and posts it straight through raw
httpx (`client.py:466`), so the literal key `extra_body` goes out on the wire — nothing flattens
it, because that flattening is an **OpenAI-SDK behaviour**, and there is no SDK on that path.

Measured against the live SLM on 2026-09-03 (same prompt, three requests to the 35B on 8502,
identical but for where the thinking control sits):

| Placement of control | Reasoning chars | Completion tokens |
|---|---|---|
| No control (baseline) | 414 | 135 |
| Top-level `chat_template_kwargs` | **0** | **8** |
| Nested under `extra_body` (what Seshat sends) | 557 | 171 |

The nested form is indistinguishable from no control at all. Every key in that block is inert:
`chat_template_kwargs.enable_thinking`, `thinking_budget`, `top_k`, `min_p`,
`repetition_penalty`. The catalog comment describing the primary as "exactly Unsloth's
thinking-mode preset" is true of three of its six values; the other three never leave the process.
Top-level placement is accepted (all four sent top-level returned HTTP 200).

Nothing is visibly broken today because the sub-agent gets thinking-off from backend 8503's
**launch flag**, not from our configuration. The server-side split has been masking a dead
client-side control — and the planned collapse to one served model (FRE-1363) removes exactly that
mask.

### Why the guard did not catch it

FRE-1007 made the local reasoning declaration mandatory: CI fails and boot refuses without it. It
validates that the catalog **declares** the field. Nothing ever checked that the field **arrives**.
A boot-blocking guard was built over a parameter that evaporates in transit. The lesson is the
spine of this ADR: *configuration proves a path exists, never that it runs* — every control this
ADR touches gets a delivery check at the wire or the behaviour, not the declaration.

### Correction to the ADR-0121 record

ADR-0121 states the cloud provider concurrency ceilings are "declared-but-inert until step 2
(FRE-917) unifies the two resolution paths — at which point the ceilings become live with no
further catalog change." FRE-917 is Done since 2026-07-20 — but it unified **resolution** (which
key, which budget lane), not **dispatch**. The `InferenceConcurrencyController` is still
instantiated only by `LocalLLMClient`, so the `openai`/`anthropic`/`voyage`/`ovh` ceilings remain
inert. The ADR records as satisfied something that is not. FRE-1343 (the key-ignoring local door)
is the "second door" FRE-917's own description warned about, still open on the local side.

### Driving requirement

FRE-1363 — the one-served-model dual-role A/B — is blocked on this ADR: it cannot be measured
while four sampler parameters are inert, and the collapse to one backend removes the launch-flag
masking the sub-agent depends on today.

---

## Decision

### D1 — One dispatch path: `LiteLLMClient` for every placement; `LocalLLMClient` is deleted

All LLM calls — local and cloud — dispatch through `LiteLLMClient` /
`litellm.acompletion()`. The factory's placement branch (`factory.py::_build_client`) collapses to
one constructor. The four real direct `LocalLLMClient()` instantiations
(`captains_log/reflection.py:367`, `memory/service.py:271`,
`second_brain/session_summary.py:649`, `second_brain/entity_extraction.py:1190`) move to the
factory; the two docstring examples are rewritten. `LocalLLMClient` and its raw-httpx transport are
**deleted, not shimmed** (owner-confirmed big-bang, 2026-09-03) — a retained-but-unused class is
exactly the kind of second door FRE-1343 documents.

Local deployments dispatch as litellm's OpenAI-compatible provider against the declared
`endpoint`. Non-standard parameters travel via the SDK's `extra_body` mechanism, which **flattens
them into the top-level request JSON** — the behaviour whose absence caused the finding. Their
arrival is asserted by AC-1, not assumed from the SDK contract.

**Out of scope by owner direction:** DSPy (`dspy_adapter.py`, `dspy_gate.py`) already uses litellm
internally and stays as it is. The `ui/` CLI's sync client to our own backend remains out of scope
as in ADR-0132.

**FRE-1343 dissolves by construction** (per ticket AC-6): it exists because `LocalLLMClient()`
takes no model key — every caller gets whatever the catalog resolves. The unified client is
constructed per resolved key (`model_id`, `provider`, endpoint), so local placement honours the
requested key the same way cloud always has. No separate fix ticket is needed; the unification
chain closes it.

### D2 — The egress guard travels with the traffic; the seeded-negative test is the contract

Unification *inverts* the ADR-0132 posture if done naively: today local traffic is guarded
(`create_guarded_http_client`'s DomainGuard hook) and litellm's cloud traffic is not (ADR-0132
scoped it out). Moving local onto litellm would move the guarded majority of calls onto the
unguarded transport. That is a security regression and is not accepted.

Decision (owner-confirmed 2026-09-03):

1. **Mechanism:** the unified client passes a guard-wrapped httpx client into the litellm dispatch
   per call (litellm's `client=` kwarg / `AsyncHTTPHandler(event_hooks=...)` — verified present in
   litellm 1.98.0's `custom_httpx/http_handler.py`). The DomainGuard request hook fires before
   transport dispatch, exactly as at the seven ADR-0132 seams.
2. **The durable guarantee is not the mechanism — it is the seeded negative.** litellm rearranges
   its transport internals routinely (FRE-1324 just upgraded it); an injection point that silently
   detaches on the next upgrade would read as configured while guarding nothing — the FRE-1007
   failure class again. A CI test dispatches a request to a **blocklisted URL through the actual
   litellm dispatch path** and asserts `EgressBlockedError` is raised before any connection is
   attempted. If a litellm upgrade detaches the injection, that test — not an incident — fails.
   The injection mechanism is thereby replaceable; the test is the contract.
3. `litellm.aclient_session` is explicitly **rejected** as the mechanism: verified against litellm
   1.98.0, it is read only by the legacy `llms/base.py` path; the modern `AsyncHTTPHandler` builds
   its own client and ignores it. Configuring it would be configuring a no-op.

This closes, for the whole unified path, the "litellm out of scope" gap ADR-0132's 2026-08-05
status update recorded as a Backlog item.

### D3 — The concurrency controller is re-homed process-wide; the cloud ceilings finally go live

The `InferenceConcurrencyController` (ADR-0029; provider-keyed by FRE-916) leaves
`LocalLLMClient` and becomes a process-level singleton acquired inside the unified client's
`respond()` for **every** provider. This delivers what ADR-0121 promised and FRE-917 did not:
the declared cloud ceilings (`openai`/`anthropic`/`voyage`/`ovh`, all 50) become live. Because 50
is a safety valve, not a throttle, no behavioural change appears at cutover; the local GPU ceiling
(`slm_local`, per-deployment `max_concurrency: 1`) carries over unchanged, including the
priority-tier semantics (`InferencePriority`) and slot-wait telemetry. litellm's own Router
rate-limiting is not used (see Alternatives).

### D4 — The four newly-live parameters, individually (ticket AC-2)

Against the live primary's declared values, at cutover:

| Parameter | Declared (primary) | Effect when it arrives | Decision |
|---|---|---|---|
| `min_p` | 0.0 | **No-op by value** — 0.0 is the neutral element | **Keep.** Declared intent preserved; nothing changes |
| `repetition_penalty` | 1.0 | **No-op by value** — 1.0 is disabled | **Keep.** Same |
| `top_k` | 20 | **Real change** — sampling narrows to the Qwen/Unsloth preset for the first time. Expected: marginally more deterministic output on the tuned 0.6-temperature loop; this is the *intended* preset finally applying, not a new experiment | **Keep.** The catalog value was chosen on evidence (EVAL-2026-05-11) for exactly this behaviour |
| Thinking control | `thinking_budget_tokens: 32768` (primary), `disable_thinking: true` (sub-agent) | **Load-bearing.** `chat_template_kwargs.enable_thinking=false` is behaviourally proven (0 reasoning chars, measured 2026-09-03). `thinking_budget` **delivery** is proven (HTTP 200 top-level); **enforcement is not** — 200 is acceptance, not effect | **Keep both; probe enforcement at cutover.** Honest AC-2 statement for `thinking_budget`: delivery verified, enforcement probed (D6). Neither probe outcome is a regression: today thinking is *effectively uncapped* (the cap never arrives), so "ignored" is the status quo and "enforced at 32768" caps a level typical turns do not approach |

The sub-agent's `disable_thinking` going live under big-bang is double-safe: backend 8503's launch
flag still enforces thinking-off server-side, so the client control activates against an already-off
backend — harmless now, and **required** the moment FRE-1363 collapses to one backend and the
launch flag disappears.

### D5 — `max_tokens` coherence: no silent 8192 cap on the local primary

Discovered during this ADR's discussion (owner-prompted): the primary today sends **no
`max_tokens` at all** — the catalog entry declares none and the executor passes none — so
completion is unbounded (EOS or context exhaustion). But `LiteLLMClient` is constructed with
`max_tokens=model_def.max_tokens or 8192` (`factory.py`), and on our backend (llama.cpp) the
completion budget **includes thinking** (`<think>` is part of the completion stream; FRE-432
measured ~75% thinking share on trivial turns). Naive unification would therefore impose an 8192
joint cap on a primary whose thinking budget alone says 32768 — long-thinking turns would have
their *answers* truncated because thinking ate the cap. Decision:

1. **Omit-means-unbounded is preserved for local placement.** The `or 8192` constructor fallback
   does not apply to local deployments; a deployment that declares no `max_tokens` sends none.
   Any future cap is a deliberate catalog edit, never a constructor default.
2. **A config-guard arithmetic invariant** (extends FRE-1007's check): a local deployment
   declaring both fields with `max_tokens <= thinking_budget_tokens` is a finding (severity:
   safety) — that arithmetic can never produce a complete answer. The guard's own test seeds the
   negative (a violating fixture must produce the finding).

### D6 — Delivery is verified at the behaviour, not the declaration (ticket AC-4)

FRE-1007's declaration guard stays — it catches the wrong-vocabulary and missing-declaration
classes at CI/boot. What un-vacuates it is a **behavioural canary** on the deployed path:

1. **Thinking canary:** the SLM health probe (`observability/slm_health/`) gains a check that
   sends a minimal request with `chat_template_kwargs.enable_thinking=false` **through the unified
   dispatch path** and asserts the response carries zero reasoning content. If the control
   evaporates in transit again — any layer, any upgrade — this probe goes red in the existing
   health surface. A declaration check alone cannot pass this ADR's bar.
2. **Budget probe at cutover (one-off, recorded):** the cutover ticket measures reasoning length
   under a deliberately tiny budget (e.g. `thinking_budget: 128`) vs the baseline, and records
   whether llama-server enforces the cap. The result decides the honest wording of the primary's
   catalog comment and feeds D8's design.
3. **Wire-shape assertion in CI:** a test captures the unified client's outgoing request JSON for
   a local deployment and asserts the four parameters appear **top-level** and the literal key
   `extra_body` does **not** appear. This pins the SDK-flattening behaviour we now depend on
   across litellm/openai upgrades.
4. **Cache delivery read from existing telemetry:** `cache_prompt: true` travels the same
   flattening path; its delivery is already observable through
   `usage.prompt_tokens_details.cached_tokens` on `model_call_completed` — a non-zero cached-token
   read after cutover is the delivery proof, for free.

### D7 — Capability disposition (ticket AC-1): what `LocalLLMClient` carries, and where each lives afterwards

| Capability | Today (local path) | Disposition | Mechanism after unification |
|---|---|---|---|
| Egress guard | `create_guarded_http_client` hook | **Preserved** (and extended to cloud) | D2: per-call guarded client injection + seeded-negative CI contract |
| GPU-aware concurrency + priority tiers | Controller inside `LocalLLMClient` | **Re-homed** | D3: process singleton acquired in unified `respond()`, all providers |
| Four sampler/thinking params | Built into inert `extra_body` | **Preserved — and delivered for the first time** | D4: litellm `extra_body` → SDK flattening → top-level; AC-1 wire assertion |
| `cache_prompt: true` (within-turn KV reuse) | Sent top-level by hand | **Preserved** | Same flattening path; delivery read from `cached_tokens` telemetry (D6.4). Cross-turn KV reuse stays server-side (slot config, ADR-0081/FRE-433) — unaffected by client choice |
| Streaming (SSE, CF-524 avoidance) | `stream=True` + manual SSE aggregation | **Preserved** | litellm `stream=True` + `stream_options: {include_usage: true}`; aggregation via litellm's chunk builder. The CF-524 rationale (bytes keep the proxy alive) carries over unchanged |
| Text tool-call parser (`parse_text_tool_calls`) | Fallback in `adapters.py` response adaptation | **Preserved** | Post-processing step on the unified response for deployments declaring `tool_calling_strategy: "text"`. The primary declares `"native"` and does not exercise it; the parser and its tests survive for deployments that do |
| Telemetry (`model_call_started/completed` with provider+role, model-call spans, prompt identity) | Emitted in `client.py` | **Preserved** | `LiteLLMClient` already emits the canonical pair with provider/role and carries `prompt_identity`; local calls inherit it. Parity asserted by AC-6's census |
| Trace propagation headers (W3C traceparent + X-Trace-Id) | Injected per request | **Preserved** | Injected via litellm `extra_headers` per call, same fields |
| Per-role timeouts (600s primary) | Role-timeout map + httpx timeout config | **Preserved** | Role timeout passed as litellm `timeout` per call; litellm `num_retries` receives our retry budget so the two retry layers do not multiply |
| History sanitiser (FRE-237) | Called before dispatch | **Preserved** | Already called on both paths today; one call site after unification |
| Cost gate (ADR-0065 reserve/commit/refund) | **Not applied** to local (free, self-hosted) | **Preserved by placement** | Local placement skips the gate (no reservation, no Postgres round-trip on the hot turn path); cloud placement unchanged. Unification is of *dispatch*, not billing policy. AC-7 asserts no cloud spend is booked for local calls |
| Reasoning vocabulary | Split **by client class** (ADR-0121) | **Re-anchored, not collapsed** | The split survives as vocabulary **by placement**: local declares `disable_thinking`/`thinking_budget_tokens` (→ `chat_template_kwargs`), cloud declares `reasoning_effort`. The two name genuinely different levers; inventing a translation layer between them would add a new failure mode for zero expressiveness. FRE-1007's guard keeps enforcing the placement-appropriate vocabulary |
| SSL-verify relaxation for localhost | Hand-rolled check | **Dropped** | Deployed endpoints resolve through Caddy/CF (ADR-0132 D4); the localhost special case served dev setups litellm handles via standard `ssl_verify` config if ever needed |

### D8 — Adaptive thinking budget: the strategic follow-up, owner-designated Critical

Unification converts a per-deployment static `thinking_budget_tokens` into a lever that can be set
**per call** (litellm accepts `chat_template_kwargs` per request) — and the Pre-LLM Gateway
already computes a complexity signal (intent classification + decomposition assessment, stages
4–5) before the model is ever called. FRE-432 measured ~75% thinking share on trivial turns:
thinking is the dominant latency and token cost exactly where it adds the least. A
complexity-scaled budget is therefore the single highest-leverage follow-up this ADR enables —
owner-designated **Critical** (2026-09-03): *"major improvement/implications if we manage this
follow up well."*

It is deliberately **not** bundled into the cutover: the lever's enforcement is unproven until
D6.2's probe runs, and a per-turn-varying budget mid-measurement would contaminate the FRE-1363
A/B. Sequencing: unification → enforcement probe → A/B baseline → adaptive budget, filed as its
own Urgent ticket blocked by FRE-1363 (see Implementation Notes).

---

## Alternatives Considered

### Option 1: Fix the placement bug in place — keep two clients, flatten `extra_body` in `LocalLLMClient`

**Description:** Move the five keys top-level in `build_chat_completions_request` and stop there.

**Pros:**
- Smallest possible diff; no litellm exposure on the hot local path
- No egress or concurrency re-homing needed

**Cons:**
- Keeps two dispatch shapes, two retry stacks, two telemetry emitters, two vocabularies — the
  divergence that produced this bug class remains and keeps producing (FRE-1343 stays open)
- Cloud concurrency ceilings stay inert; ADR-0121's record stays wrong
- Every future capability (adaptive budgets, new providers) is built twice

**Why Rejected:** It fixes the symptom and preserves the disease. The owner's directive is
uniformity; this is its negation with a patch on top.

### Option 2: Unify on litellm's Router (or proxy server) instead of direct `acompletion()`

**Description:** Use litellm's Router for dispatch, load-balancing and rate limits.

**Pros:**
- Built-in cooldowns, fallbacks, rate limiting; config-driven model list

**Cons:**
- Replaces our priority-tier semantics (`InferencePriority`: a CRITICAL request pre-empts queued
  BACKGROUND work at slot release) with plain rate limiting — a real capability loss on a
  single-GPU host where queueing discipline matters
- Router solves multi-deployment load balancing we do not have (one GPU, one primary)
- The proxy variant adds a service to operate on a research harness

**Why Rejected:** Wrong tool for a single-host, priority-scheduled workload; loses semantics we
depend on and adds machinery we would maintain for nothing.

### Option 3: Unify on the raw OpenAI SDK (`openai.AsyncOpenAI`) instead of litellm

**Description:** One client path built directly on the OpenAI SDK; Anthropic and others via their
OpenAI-compatible surfaces or per-provider adapters.

**Pros:**
- Fewer layers than litellm; `extra_body` flattening is native SDK behaviour
- `http_client=` injection makes the egress guard trivial

**Cons:**
- Loses litellm's provider normalization (Anthropic prompt-caching decoration, `reasoning_effort`
  transformation, the model-capability map FRE-1007's policy checks ride on, `completion_cost()`)
- Diverges from DSPy, which rides litellm — two ecosystems again, just different ones
- Voyage/OVH would need hand adapters litellm already ships

**Why Rejected:** Re-implements what litellm already provides and is already load-bearing for the
cloud half and DSPy. Uniformity argues for the path most of the system is already on.

### Option 4: Guard egress via `litellm.aclient_session` (module-global client injection)

**Description:** Set the module-level session once at startup; all litellm traffic inherits it.

**Pros:**
- One line at startup; no per-call plumbing

**Cons:**
- **Verified dead:** in litellm 1.98.0 only the legacy `llms/base.py` reads it; the modern
  `AsyncHTTPHandler` constructs its own httpx client and ignores it entirely

**Why Rejected:** It is a configured no-op — the precise failure class this ADR exists to end.
Recorded as an alternative so nobody re-proposes it from the litellm docs.

---

## Consequences

### Positive Consequences

- The tuned preset actually applies: all declared sampler and thinking parameters reach the
  server, with wire-level and behavioural proof they arrive — the FRE-1363 A/B becomes measurable
- One dispatch path: one retry stack, one telemetry emitter, one egress surface, one place to add
  capabilities; the guarded-transport gap ADR-0132 recorded for litellm closes
- Cloud concurrency ceilings go live, delivering ADR-0121's promise; FRE-1343 dissolves by
  construction
- FRE-1007's guard stops being vacuous: declaration checks are now backed by a live behavioural
  canary and a CI wire-shape assertion
- Adaptive per-call thinking budgets (D8) drop from "architecture change" to "small ticket"

### Negative Consequences

- litellm becomes load-bearing on the **hot local turn path**, not just cloud background work; its
  version churn (transport internals, param handling) now risks the primary. Mitigated by the
  seeded-negative egress test, the wire-shape assertion, and the thinking canary — each pinned to
  behaviour, so an upgrade that breaks the contract fails CI/health, not a live turn silently
- Big-bang cutover: all local traffic changes transport in one merge. The blast radius is bounded
  by parity-first scoping (D5 preserves unbounded completion; two of four params are arithmetic
  no-ops) and the canary
- The deleted `LocalLLMClient` takes its battle-tested SSE aggregation and error taxonomy with it;
  litellm's equivalents must be mapped onto our exception types (`LLMTimeout`, `LLMRateLimit`, …)
  during implementation

### Risks and Mitigations

| Risk | Severity | Mitigation |
|------|----------|------------|
| litellm upgrade silently detaches the injected guarded client | High | D2 seeded-negative CI test — the contract is the test, not the mechanism |
| litellm upgrade changes how `extra_body` flattens for OpenAI-compatible endpoints | High | D6.3 wire-shape assertion pins top-level arrival and absence of the literal key |
| `top_k: 20` going live shifts primary output quality | Medium | It is the evidenced preset (EVAL-2026-05-11) finally applying; A/B re-baselines immediately after cutover (FRE-1363 is sequenced behind this chain) |
| `thinking_budget` turns out enforced and interacts badly with long turns | Low | 32768 exceeds typical turn thinking; D6.2 probe quantifies before the A/B leans on it |
| Streaming aggregation differences (usage block, tool-call deltas) between our SSE code and litellm's | Medium | Cutover ticket asserts usage + tool-call parity on recorded streams before the switch |
| Cost gate accidentally engages for local calls (latency + spurious ledger rows) | Medium | AC-7 asserts zero cloud-spend booking for local provider post-cutover |

---

## Implementation Notes

- **Files:** `llm_client/factory.py` (branch collapse), `llm_client/litellm_client.py` (guard
  injection, concurrency acquisition, local-placement param passing, no-8192-for-local),
  `llm_client/client.py` + local-only parts of `adapters.py` (deleted; text tool-call parser and
  response adaptation retained where D7 says), the four direct instantiation sites,
  `observability/slm_health/` (canary), `config/config_guard.py` (D5 arithmetic invariant),
  ast-grep rule update (ADR-0132 set) so the deleted class cannot return
- **Big-bang cutover** (owner-confirmed): one implementation chain, sequenced tickets, no
  dual-running period. The A/B (FRE-1363) runs only after the chain lands
- **AC-5 of the ticket — record corrections shipped with this ADR's PR:** ADR-0121 gains a status
  update superseding "Vocabulary by dispatch path" (now vocabulary by placement, D7) and
  correcting the FRE-917 inert-ceilings note (ceilings go live here, D3); ADR-0031 gains a status
  update recording Alternative C as closed by this ADR
- **Implementation chain (filed at authoring, `Needs Approval`, sequenced with `blockedBy`, no
  stream labels):**
  1. Guard + transport seam: guarded-client injection into litellm dispatch + seeded-negative CI
     test (D2)
  2. Unified local dispatch: local placement through `LiteLLMClient` — params top-level,
     streaming, timeouts, error mapping, telemetry parity, no-8192-for-local (D1, D4, D5, D7) —
     including the factory branch collapse and the four call-site moves
  3. Concurrency re-homing: process singleton, all providers, cloud ceilings live (D3)
  4. Delete `LocalLLMClient` + ast-grep tombstone + docs/AGENTS.md rewrite (D1)
  5. Canary + probes: SLM-health thinking canary, wire-shape CI assertion, config-guard
     arithmetic invariant, cutover budget probe (D5, D6)
  6. *(post-FRE-1363, Urgent)* Adaptive thinking budget from the gateway complexity signal (D8)

---

## Verification / Acceptance Criteria

Adjudicated on the umbrella ticket (FRE-1362) once the implementation chain has landed and
deployed — not at merge of this ADR.

- **AC-1 — The four parameters arrive top-level on the wire.** · **Check:** CI test captures the
  unified client's outgoing request JSON for a local deployment and asserts `top_k`, `min_p`,
  `repetition_penalty` and `chat_template_kwargs` appear at the top level and the literal key
  `extra_body` is absent. · *Fails if* any parameter rides under `extra_body`, is dropped, or the
  assertion never exercises the real dispatch path (a hand-built payload fixture does not count).
- **AC-2 — Thinking-off is behaviourally delivered on the deployed path.** · **Check:** the SLM
  health probe's canary sends a thinking-disabled request through production dispatch and asserts
  zero reasoning content; probe red is visible in the existing health surface. · *Fails if* the
  canary asserts only HTTP status, only declaration presence, or runs against a hand-rolled httpx
  call instead of the unified client.
- **AC-3 — A blocklisted URL cannot escape through the litellm path.** · **Check:** seeded-negative
  CI test dispatches to a guard-blocklisted URL via the unified client and asserts
  `EgressBlockedError` before connection. · *Fails if* the request reaches a transport, or the
  test stubs the layer the guard hangs on.
- **AC-4 — Cloud concurrency ceilings are enforced, not just declared.** · **Check:** test
  registers a provider with ceiling N through the re-homed singleton, issues N+1 concurrent unified
  calls, asserts the N+1th blocks until a slot frees; local priority semantics asserted unchanged
  by the existing controller tests. · *Fails if* cloud calls bypass `request_slot`, or only the
  local provider is exercised.
- **AC-5 — The primary's completion stays unbounded and the guard can prove the arithmetic.** ·
  **Check:** wire capture for the primary asserts no `max_tokens` key when the catalog declares
  none; config-guard test seeds a local deployment with `max_tokens <= thinking_budget_tokens` and
  asserts the finding fires. · *Fails if* the 8192 constructor default reaches a local wire
  payload, or the guard check passes on the seeded violation.
- **AC-6 — One path, provably.** · **Check:** `LocalLLMClient` no longer exists in `src/`
  (ast-grep tombstone rule in CI), **and** post-deploy ES `model_call_completed` events for local
  roles carry provider `slm_local` with the same canonical fields as cloud events (query over a
  live window). · *Fails if* any constructor site survives, or local telemetry loses
  provider/role/prompt-identity parity after the switch.
- **AC-7 — Local calls book no cloud spend.** · **Check:** post-deploy cost-ledger query over a
  live window shows zero reservations/commits attributed to local-placement calls while cloud
  rows continue unchanged. · *Fails if* local traffic produces gate rows or, conversely, cloud
  traffic stops producing them (the gate must not be lost in the re-plumb).
- **AC-8 — Within-turn KV cache still delivers.** · **Check:** post-deploy ES query shows
  non-zero `cached_tokens` on multi-call local turns after cutover, at a rate comparable to the
  pre-cutover baseline. · *Fails if* cached-token reads drop to zero — `cache_prompt` evaporated
  in the transport change.

---

## References

- [FRE-1362](https://linear.app/frenchforest/issue/FRE-1362) — umbrella ticket; the 2026-09-03 live measurement lives in its description
- ADR-0031 — Model Configuration Consolidation; its Alternative C (litellm as the unified abstraction, Deferred) is closed by this ADR
- ADR-0121 — Model Catalog and Selection Layer; "Vocabulary by dispatch path" and the FRE-917 inert-ceilings note superseded here (status updates added by this ADR's PR)
- ADR-0132 — Outbound Authenticated Egress; D2's guard obligation extended to the unified path (D2 here); its recorded litellm scope gap closes
- ADR-0029 — Inference Concurrency Control; the controller re-homed by D3
- ADR-0081 / [FRE-433](https://linear.app/frenchforest/issue/FRE-433) — cross-turn KV reuse (server-side; unaffected, noted in D7)
- ADR-0065 — Cost gate; scope preserved by placement (D7)
- [FRE-1007](https://linear.app/frenchforest/issue/FRE-1007) — the declaration guard this ADR un-vacuates (D6)
- [FRE-917](https://linear.app/frenchforest/issue/FRE-917) — resolution unification (Done); dispatch unification is this ADR
- [FRE-1343](https://linear.app/frenchforest/issue/FRE-1343) — the local key-ignoring door; dissolves under D1
- [FRE-1262](https://linear.app/frenchforest/issue/FRE-1262) — model-SDK confinement guard; its litellm-only match becomes the whole surface
- [FRE-1363](https://linear.app/frenchforest/issue/FRE-1363) — the driving one-model dual-role A/B, blocked on this chain
- [FRE-432](https://linear.app/frenchforest/issue/FRE-432) — thinking-token measurement (~75% thinking share on trivial turns) grounding D5 and D8
- `docs/research` EVAL-2026-05-11 — the sampling-preset evidence behind D4's top_k disposition

---

## Status Updates

### 2026-09-03 - Proposed
**Changed By:** adr session (FRE-1362)
**Reason:** Owner-directed unification, forced by the 2026-09-03 live measurement showing the
local dispatch path's `extra_body` block never reaches the server. Egress mechanism, big-bang
cutover, max_tokens coherence and the Critical designation of the adaptive-budget follow-up all
settled in owner discussion before drafting.
