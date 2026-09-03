# ADR-0141: One LLM Dispatch Path — Every Call Rides litellm, and the Wire Is Verified, Not Assumed

**Status:** Proposed
**Date:** 2026-09-03
**Deciders:** Project owner + adr session (FRE-1362)
**Tags:** llm-client, dispatch, litellm, egress, concurrency, configuration

---

## Context

Owner-directed 2026-09-03: *"Uniformity in making LLM calls. One code path — DSPy may remain the
outlier for now — but everything else goes through litellm."* On acceptance, this closes
ADR-0031's Alternative C, deferred since March 2026, and supersedes ADR-0121's "Vocabulary by
dispatch path" rule.

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
one constructor. The direct `LocalLLMClient()` constructor census
(executable non-test code — the test suite's many constructors are rewritten wholesale against
the unified client at deletion and swept by AC-6's scan): four production sites (`captains_log/reflection.py:367`, `memory/service.py:271`,
`second_brain/session_summary.py:649`, `second_brain/entity_extraction.py:1190`), one executable
migration script (`scripts/migrate_fre865_entity_class_backfill.py:484`), three DSPy prototype
scripts (`experiments/dspy_prototype/test_case_{a,b,c}_*.py`), and two docstring examples. All
move to the factory or are rewritten; the big-bang deletion must not leave a broken repository
entry point outside `src/`. `LocalLLMClient` and its raw-httpx transport are **deleted, not
shimmed** (owner-confirmed big-bang, 2026-09-03) — a retained-but-unused class is exactly the kind
of second door FRE-1343 documents.

Local deployments dispatch as litellm's OpenAI-compatible provider against the declared
`endpoint`. Two provider names must not be conflated here: the **catalog/telemetry provider stays
`slm_local`** (concurrency keying, telemetry fields, ADR-0121 semantics all keep it), while the
**litellm dispatch string is `openai/{model_id}` with `api_base` set to the deployment endpoint**
— litellm 1.98.0 does not recognise `slm_local/` as a provider prefix and fails with "LLM Provider
NOT provided" before any transport. The unified client owns this mapping; nothing outside it sees
the `openai/` prefix. Non-standard parameters travel via the SDK's `extra_body` mechanism, which
**flattens them into the top-level request JSON** — the behaviour whose absence caused the
finding. Their arrival is asserted by AC-1, not assumed from the SDK contract.

**Out of scope by owner direction:** DSPy (`dspy_adapter.py`, `dspy_gate.py`) already uses litellm
internally and stays as it is. The `ui/` CLI's sync client to our own backend remains out of scope
as in ADR-0132.

**Undeclared outliers found at review and brought in scope:** two eval scripts call
`litellm.acompletion()` directly (`scripts/eval/fre630_extraction_quality/relabel_v2_{rels,types}.py`,
with `adr0109_boundary_probe.py` reusing the latter and documenting the bypass). "One dispatch
path" that quietly excepts executable scripts is the census failure this ADR keeps correcting, so
they **migrate to the factory** (`get_llm_client_for_key`) in the implementation chain — which
also puts them behind the egress guard and the cost gate they currently bypass. Any future direct
`litellm.acompletion()` outside `llm_client/` is forbidden by the AC-6 boundary.

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

1. **Mechanism, per dispatch route:** litellm selects a different HTTP stack per provider route,
   so the injection must match the route it guards. For the local OpenAI-compatible route,
   litellm's `client=` kwarg expects an **`AsyncOpenAI` object** (verified against litellm 1.98.0:
   `main.py` routes OpenAI-compatible calls to `openai_chat_completions`, whose handler reads
   `.api_key`/`._base_url`/`.chat` off the injected client) — so the unified client passes
   `AsyncOpenAI(http_client=create_guarded_http_client(...))` per call, the exact
   `http_client=`-into-SDK pattern ADR-0132 D2 already sanctions at the `gateway/chat_api.py`
   seam. Cloud provider routes that ride litellm's `AsyncHTTPHandler` instead take the guard via
   that handler's `event_hooks` parameter, or whatever hook point the route verifiably honours.
   The route-by-route verification is an implementation obligation, and the point of clause 2 is
   that no route's claim is taken on faith.
2. **The durable guarantee is not the mechanism — it is the seeded negative, per distinct
   injection mechanism in use.** litellm rearranges its transport internals routinely (FRE-1324
   just upgraded it); an injection point that silently detaches on the next upgrade would read as
   configured while guarding nothing — the FRE-1007 failure class again. CI tests dispatch a
   request to a **blocklisted URL through the actual litellm dispatch path**, one test per
   materially distinct route mechanism the catalog's providers actually use — at cutover that is
   the OpenAI-SDK route (local `openai/` dispatch; also `openai` cloud) **and** the
   `AsyncHTTPHandler` route (`anthropic`); a provider whose route introduces a third mechanism
   adds a third test. A passing negative on one mechanism proves nothing about the other.

   The exception contract needs two layers, because the wrapped-exception shapes differ per
   route and one of them is unrecoverable: the OpenAI SDK wraps request-hook exceptions in
   `APIConnectionError ... from err` (causal chain intact, unwrappable — verified against openai
   2.24.0), but litellm's Anthropic handler catches a hook exception and raises a **new
   `AnthropicError` without `from e`** (verified against litellm 1.98.0 `anthropic/chat/handler.py`)
   — the guard's exception is unrecoverable from the causal chain on that route. So:
   **(layer 1, owns the exception type)** the unified client runs the DomainGuard check itself,
   pre-dispatch, on the resolved endpoint/api_base — route-independent, raises
   `EgressBlockedError` directly, preserving the ADR-0132 exception contract for every caller;
   **(layer 2, owns the transport)** the per-route injected hook remains as depth — it also
   covers URLs the SDK constructs internally and redirect hops, where its guarantee is *no
   connection*, not exception type. Each mechanism's seeded negative asserts both: the caller
   sees `EgressBlockedError` (layer 1), and — with layer 1 disabled in the test — a sentinel
   transport is never reached (layer 2, whatever wrapper the route surfaces). If a litellm
   upgrade detaches an injection, that mechanism's layer-2 test — not an incident — fails. The
   injection mechanisms are thereby replaceable; the tests are the contract.
3. `litellm.aclient_session` (module-global client injection) is **rejected** as the mechanism —
   for partial coverage, not total inertness: verified against litellm 1.98.0, the modern OpenAI
   path *does* honour it (`llms/openai/common_utils.py` returns it into `AsyncOpenAI`), but
   `AsyncHTTPHandler`-based provider routes build their own client and ignore it. A global that
   guards some routes and silently leaks on others reads as guarded while it is not — worse than
   no guard. Per-call injection keeps each route's mechanism explicit and each placement's seeded
   negative honest.

This closes, for the whole unified path, the "litellm out of scope" gap ADR-0132's 2026-08-05
status update recorded as a Backlog item.

### D3 — The concurrency controller is re-homed process-wide; the cloud ceilings finally go live

The `InferenceConcurrencyController` (ADR-0029; provider-keyed by FRE-916) leaves
`LocalLLMClient` and becomes a process-level singleton acquired inside the unified client's
`respond()` for every provider **that dispatches through it** — the chat-completion providers:
`slm_local`, `anthropic`, `openai`, `ovhcloud`. Their declared cloud ceilings (50) become live,
delivering that part of what ADR-0121 promised and FRE-917 did not. Because 50 is a safety valve,
not a throttle, no behavioural change appears at cutover; the local GPU ceiling (`slm_local`,
per-deployment `max_concurrency: 1`) carries over unchanged, including the priority-tier
semantics (`InferencePriority`) and slot-wait telemetry.

Stated honestly rather than by omission: the `voyage` (reranker) and `ovh` (embedder) ceilings do
**not** become live here — those deployments dispatch through `memory/embeddings.py`'s direct SDK
and `memory/reranker.py`'s HTTP path, which never enter `respond()` and never acquired the
controller before either. Their ceilings remain declared-but-inert, status quo; wiring those two
non-chat paths into the controller is out of this ADR's scope and is recorded as such (an earlier
draft claimed all cloud ceilings go live — that was an overclaim). litellm's own Router
rate-limiting is not used (see Alternatives).

### D4 — The four newly-live parameters, individually (ticket AC-2)

Against the live primary's declared values, at cutover:

| Parameter | Declared (primary) | Effect when it arrives | Decision |
|---|---|---|---|
| `min_p` | 0.0 | **No-op by value** — 0.0 is the neutral element | **Keep.** Declared intent preserved; nothing changes |
| `repetition_penalty` | 1.0 | **No-op by value** — 1.0 is disabled | **Keep.** Same |
| `top_k` | 20 | **Real change** — sampling narrows to the Qwen/Unsloth model-card preset for the first time. Expected: marginally more deterministic output on the tuned 0.6-temperature loop; this is the *intended* preset finally applying, not a new experiment. Honest evidence note: the value is the model card's recommendation, not an isolated measurement — EVAL-2026-05-11 evidenced the `temperature`, not `top_k` | **Keep.** The declared preset applies as written; the FRE-1363 A/B re-baselines immediately after cutover, which is where its actual effect gets measured |
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
   safety). Precisely stated: the budget is a ceiling, not guaranteed consumption, so such a
   configuration is not *impossible* — it is a no-guaranteed-answer-headroom configuration, which
   this guard rejects as policy. The guard's own test seeds the negative (a violating fixture
   must produce the finding).

### D6 — Delivery is verified at the behaviour, not the declaration (ticket AC-4)

FRE-1007's declaration guard stays — it catches the wrong-vocabulary and missing-declaration
classes at CI/boot. What un-vacuates it is a **behavioural canary** on the deployed path:

1. **Thinking canary — with a positive control, against a thinking-capable endpoint:** the SLM
   health probe (`observability/slm_health/`) gains a paired check through the unified dispatch
   path: first a baseline request with thinking enabled that **must show non-zero reasoning
   content** (proving the instrument can see reasoning at all — `LiteLLMClient` today discards it,
   returning `reasoning_trace=None` unconditionally, so without this arm the canary would pass
   vacuously; reasoning preservation is a D7 obligation for exactly this reason), then a request
   with `chat_template_kwargs.enable_thinking=false` asserting zero reasoning content. Both arms
   target a thinking-capable endpoint — a zero-reasoning read against a backend whose *launch
   flag* already disables thinking (8503 today) proves nothing about the client control. If the
   control evaporates in transit again — any layer, any upgrade — this probe goes red in the
   existing health surface. A declaration check alone cannot pass this ADR's bar.
2. **Budget probe at cutover (one-off, recorded):** the cutover ticket measures reasoning length
   under a deliberately tiny budget (e.g. `thinking_budget: 128`) vs the baseline, and records
   whether llama-server enforces the cap. The result decides the honest wording of the primary's
   catalog comment and feeds D8's design.
3. **Wire-shape assertion in CI:** a test captures the unified client's outgoing request JSON for
   a local deployment and asserts every non-standard parameter appears **top-level** — `top_k`,
   `min_p`, `repetition_penalty`, `cache_prompt`, and *both* thinking shapes
   (`chat_template_kwargs.enable_thinking` for a disabling deployment, the sibling top-level
   `thinking_budget` key for a budget-declaring one — they are distinct keys, not one) — and that
   the literal key `extra_body` does **not** appear. This pins the SDK-flattening behaviour we now
   depend on across litellm/openai upgrades.
4. **Cache delivery:** `cache_prompt: true` is proven delivered by the wire assertion above — the
   telemetry read is corroboration, not proof, because current llama.cpp defaults cache reuse on,
   so cached tokens can stay non-zero even with the flag dropped. The corroborating field on
   `model_call_completed` is **`cache_read_tokens`** (mapped from the raw
   `usage.prompt_tokens_details.cached_tokens` / `cache_read_input_tokens`).

### D7 — Capability disposition (ticket AC-1): what `LocalLLMClient` carries, and where each lives afterwards

| Capability | Today (local path) | Disposition | Mechanism after unification |
|---|---|---|---|
| Egress guard | `create_guarded_http_client` hook | **Preserved** (and extended to cloud) | D2: per-call guarded client injection + seeded-negative CI contract |
| GPU-aware concurrency + priority tiers | Controller inside `LocalLLMClient` | **Re-homed** | D3: process singleton acquired in unified `respond()` for every provider dispatching through it (chat providers; `voyage`/`ovh` stay outside, per D3) |
| Four sampler/thinking params | Built into inert `extra_body` | **Preserved — and delivered for the first time** | D4: litellm `extra_body` → SDK flattening → top-level; AC-1 wire assertion |
| `cache_prompt: true` (within-turn KV reuse) | Sent top-level by hand | **Preserved** | Same flattening path; delivery proven by the D6.3 wire assertion, corroborated by `cache_read_tokens` telemetry (D6.4). Cross-turn KV reuse stays server-side (slot config, ADR-0081/FRE-433) — unaffected by client choice |
| Reasoning-content preservation (`<think>` / `reasoning_content` → `reasoning_trace`) | Extracted by the local response adapter (`adapters.py:373`): inline `<think>…</think>` is stripped from visible content into `reasoning_trace`, with the dedicated `reasoning_content` field as fallback — **both shapes** | **Preserved — requires new work, both shapes** | `LiteLLMClient` today returns `reasoning_trace=None` unconditionally; the unified client must carry over the same two-shape extraction. Mapping only the dedicated field would leak `<think>` text into answers and blind the D6.1 positive control; asserted by AC-9 |
| Streaming (SSE, CF-524 avoidance) | `stream=True` + manual SSE aggregation | **Preserved** | litellm `stream=True` + `stream_options: {include_usage: true}`; aggregation via litellm's chunk builder. The CF-524 rationale (bytes keep the proxy alive) carries over unchanged |
| Text tool-call parser (`parse_text_tool_calls`) | **Unconditional** fallback in `adapters.py` response adaptation — it fires whenever structured tool calls are absent, regardless of declared strategy, so even the `"native"` primary exercises it when the model emits a textual call | **Preserved with the same semantics** | Same unconditional fallback, applied to the unified response. Gating it on `tool_calling_strategy: "text"` would silently remove an existing recovery path for native-strategy models |
| Telemetry (`model_call_started/completed` with provider+role, model-call spans, prompt identity) | Emitted in `client.py` | **Preserved** | `LiteLLMClient` already emits the canonical pair with provider/role and carries `prompt_identity`; local calls inherit it, with provider reported as `slm_local` (the catalog name, not the `openai/` dispatch prefix — D1). Parity asserted by AC-9 |
| Trace propagation headers | Injected per request: W3C `traceparent`, `X-Trace-Id`, `X-Span-Id`, and `X-Session-Id` when available — all four, not two | **Preserved** | Injected via litellm `extra_headers` per call, same four fields (slm_server still reads the legacy pair) |
| Per-role timeouts (600s primary) | Role-timeout map + httpx timeout config | **Preserved** | Role timeout passed as litellm `timeout` per call; litellm `num_retries` receives our retry budget so the two retry layers do not multiply |
| History sanitiser (FRE-237) | Called before dispatch | **Preserved** | Already called on both paths today; one call site after unification |
| Cost gate (ADR-0065 reserve/commit/refund) | **Not applied** to local (free, self-hosted) | **Preserved by placement** | Local placement skips the gate (no reservation, no Postgres round-trip on the hot turn path); cloud placement unchanged. Unification is of *dispatch*, not billing policy. AC-7 asserts no cloud spend is booked for local calls |
| Reasoning vocabulary | Split **by client class** (ADR-0121) | **Re-anchored, not collapsed** | The split survives as vocabulary **by placement**: local declares `disable_thinking` (→ `chat_template_kwargs.enable_thinking=false`) or `thinking_budget_tokens` (→ the sibling top-level `thinking_budget` key — two distinct wire shapes, per D4), cloud declares `reasoning_effort`. The two vocabularies name genuinely different levers; inventing a translation layer between them would add a new failure mode for zero expressiveness. FRE-1007's guard keeps enforcing the placement-appropriate vocabulary |
| SSL-verify relaxation for localhost | Hand-rolled check | **Dropped** | Deployed endpoints resolve through Caddy/CF (ADR-0132 D4); the localhost special case served dev setups litellm handles via standard `ssl_verify` config if ever needed |

### D8 — Adaptive thinking budget: the strategic follow-up, owner-designated Critical

Unification converts a per-deployment static `thinking_budget_tokens` into a lever that can be set
**per call** (litellm forwards the top-level `thinking_budget` key per request, the same
flattening path as the other non-standard params) — and the Pre-LLM Gateway
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
- Router **does** carry priority scheduling (verified against 1.98.0: `default_priority`,
  `schedule_acompletion`, a lower-value-wins heap in `scheduler.py`) — an earlier draft of this
  ADR wrongly claimed it did not

**Cons:**
- Fit, not capability absence: Router's scheduler prioritizes within its own deployment-routing
  queue, while our controller enforces per-provider ceilings **plus per-deployment sub-limits**
  with slot-wait telemetry wired into our health surface — proven code whose semantics the A/B
  and the GPU host depend on. Migrating to Router's scheduler is a rewrite of working queueing
  for no new capability
- Router's model-list config duplicates the catalog (`models.yaml`) — two sources of truth for
  the same deployments
- Router solves multi-deployment load balancing we do not have (one GPU, one primary); the proxy
  variant adds a service to operate on a research harness

**Why Rejected:** On fit: it duplicates config authority and replaces working, telemetry-wired
queueing with an equivalent-at-best scheduler. May be revisited if multi-deployment routing ever
becomes real.

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
- Actually honoured on the OpenAI SDK routes (verified against 1.98.0:
  `llms/openai/common_utils.py` returns it into `AsyncOpenAI`) — which includes the local
  OpenAI-compatible path

**Cons:**
- **Partial coverage:** `AsyncHTTPHandler`-based provider routes construct their own httpx client
  and ignore the global entirely (verified in `custom_httpx/http_handler.py`). Which routes read
  it is an undocumented internal that has already churned across litellm versions

**Why Rejected:** A global that guards some routes and silently leaks on others reads as guarded
while it is not — worse than no guard, and unauditable as litellm's internals move. Per-call
injection (D2.1) keeps each route's mechanism explicit; the per-placement seeded negatives (D2.2)
are what make any mechanism trustworthy.

---

## Consequences

### Positive Consequences

- The tuned preset actually applies: all declared sampler and thinking parameters reach the
  server, with wire-level and behavioural proof they arrive — the FRE-1363 A/B becomes measurable
- One dispatch path: one retry stack, one telemetry emitter, one egress surface, one place to add
  capabilities; the guarded-transport gap ADR-0132 recorded for litellm closes
- Chat-provider cloud concurrency ceilings go live, delivering that part of ADR-0121's promise (`voyage`/`ovh` stay inert, stated in D3); FRE-1343 dissolves by
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
| `top_k: 20` going live shifts primary output quality | Medium | It is the model-card preset finally applying; A/B re-baselines immediately after cutover (FRE-1363 is sequenced behind this chain) |
| `thinking_budget` turns out enforced and interacts badly with long turns | Low | 32768 exceeds typical turn thinking; D6.2 probe quantifies before the A/B leans on it |
| Streaming aggregation differences (usage block, tool-call deltas) between our SSE code and litellm's | Medium | Cutover ticket asserts usage + tool-call parity on recorded streams before the switch |
| Cost gate accidentally engages for local calls (latency + spurious ledger rows) | Medium | AC-7 asserts zero cloud-spend booking for local provider post-cutover |

---

## Implementation Notes

- **Files:** `llm_client/factory.py` (branch collapse), `llm_client/litellm_client.py`
  (route-appropriate guard injection per D2.1 — `AsyncOpenAI(http_client=guarded)` on the local
  OpenAI-compatible route; concurrency acquisition; local-placement param passing;
  reasoning-content preservation; no-8192-for-local), `llm_client/client.py` + local-only parts
  of `adapters.py` (deleted; the unconditional text tool-call fallback and response adaptation
  retained per D7), the full constructor census from D1 (four `src/` sites, one `scripts/`
  migration, three `experiments/` prototypes, two docstrings), `observability/slm_health/`
  (canary), `config/config_guard.py` (D5 arithmetic invariant), ast-grep rule update (ADR-0132
  set) so the deleted class cannot return
- **Big-bang cutover** (owner-confirmed): one implementation chain, sequenced tickets, no
  dual-running period. The A/B (FRE-1363) runs only after the chain lands
- **AC-5 of the ticket — record corrections shipped with this ADR's PR:** ADR-0121 gains a status
  update with the factual FRE-917 inert-ceilings correction (effective immediately) and the
  vocabulary-by-placement supersession (effective on this ADR's acceptance); ADR-0031 gains a
  status update recording Alternative C's closure as proposed here, effective on acceptance
- **Implementation chain (filed at authoring, `Needs Approval`, sequenced with `blockedBy`, no
  stream labels):**
  1. Guard + transport seam: guarded-client injection into litellm dispatch + seeded-negative CI
     test (D2)
  2. Unified local dispatch: local placement through `LiteLLMClient` — params top-level,
     streaming, timeouts, error mapping, telemetry parity, reasoning-content preservation,
     no-8192-for-local (D1, D4, D5, D7) — including the factory branch collapse and the
     production call-site moves
  3. Concurrency re-homing: process singleton for every provider dispatching through
     `respond()`; chat-provider cloud ceilings live (D3)
  4. Delete `LocalLLMClient` across the full D1 census — `src/`, `scripts/`, `experiments/` —
     migrate the two direct `litellm.acompletion()` eval scripts to the factory, extend the
     confinement rules (direct `acompletion()` outside `llm_client/` forbidden, AC-6) + ast-grep
     tombstone + docs/AGENTS.md rewrite (D1)
  5. Canary + probes: SLM-health thinking canary, wire-shape CI assertion, config-guard
     arithmetic invariant, cutover budget probe (D5, D6)
  6. *(post-FRE-1363, Urgent)* Adaptive thinking budget from the gateway complexity signal (D8)

---

## Verification / Acceptance Criteria

Adjudicated on the umbrella ticket (FRE-1362) once the implementation chain has landed and
deployed — not at merge of this ADR.

- **AC-1 — Every non-standard parameter arrives top-level on the wire, in its correct shape.** ·
  **Check:** CI test captures the unified client's outgoing request JSON through the real dispatch
  path and asserts, for a budget-declaring local deployment (the primary's shape): `top_k`,
  `min_p`, `repetition_penalty`, `cache_prompt` and the top-level `thinking_budget` key; and for a
  disabling deployment (the sub-agent's shape): `chat_template_kwargs.enable_thinking=false` —
  with the literal key `extra_body` absent in both. · *Fails if* any parameter rides under
  `extra_body`, is dropped, the two thinking shapes are conflated into one assertion, or the test
  hand-builds the payload instead of exercising the dispatch path.
- **AC-2 — Thinking-off is behaviourally delivered, with a live instrument.** · **Check:** the SLM
  health canary runs both arms of D6.1 against a thinking-capable endpoint through production
  dispatch: the thinking-enabled arm returns non-zero reasoning content (positive control — proves
  the unified client preserves reasoning rather than discarding it) and the thinking-disabled arm
  returns zero; probe red is visible in the existing health surface. · *Fails if* either arm is
  missing, the endpoint's launch flag already disables thinking (the 8503 case — zero reasoning
  there proves nothing), or the canary asserts only HTTP status or declaration presence.
- **AC-3 — A blocklisted URL cannot escape through any litellm route in use.** · **Check:**
  seeded-negative CI tests, **one per distinct route mechanism the catalog's providers use** (at
  cutover: the OpenAI-SDK route and the `AsyncHTTPHandler` route — D2.2), each dispatching to a
  guard-blocklisted URL via the unified client and asserting both layers of D2.2: the caller
  receives `EgressBlockedError` (layer 1, the pre-dispatch check — the route's own wrapper shapes
  make chain-unwrapping unreliable, so the type guarantee never depends on them), and, with
  layer 1 disabled in the test, a sentinel transport is never reached (layer 2, the injected
  hook). · *Fails if* any in-use mechanism lacks its own test, the request reaches a transport,
  the caller-facing type depends on unwrapping a route wrapper, or the test stubs the layer the
  guard hangs on.
- **AC-4 — Chat-provider concurrency ceilings are enforced, not just declared.** · **Check:** test
  registers a cloud chat provider with ceiling N through the re-homed singleton, issues N+1
  concurrent unified calls, asserts the N+1th blocks until a slot frees; local priority semantics
  asserted unchanged by the existing controller tests. Scope is D3's, stated: `voyage`/`ovh`
  (reranker/embedder) never dispatch through `respond()` and are explicitly out of scope — a green
  here claims nothing about them. · *Fails if* cloud chat calls bypass `request_slot`, only the
  local provider is exercised, or the ADR text anywhere claims the non-chat ceilings went live.
- **AC-5 — The primary's completion stays unbounded and the guard can prove the arithmetic.** ·
  **Check:** wire capture for the primary asserts no `max_tokens` key when the catalog declares
  none; config-guard test seeds a local deployment with `max_tokens <= thinking_budget_tokens` and
  asserts the finding fires. · *Fails if* the 8192 constructor default reaches a local wire
  payload, or the guard check passes on the seeded violation.
- **AC-6 — The dispatch boundary is enforced, not just the class deleted.** · **Check:** zero
  `LocalLLMClient` references in **executable code** — `src/`, `scripts/`, `experiments/`, `tests/`
  — (documentation, including this ADR, necessarily keeps the name; a repo-wide text count is
  unsatisfiable and proves nothing), **and** the static boundary makes a *replacement* dispatch
  impossible to land silently: the ADR-0132 ast-grep rule set (raw `httpx.Client`/`AsyncClient`
  construction outside the factory) and the FRE-1262 SDK-confinement guard remain enforced, and
  the confinement is extended to **direct `litellm.acompletion()`/`litellm.completion()` calls
  outside `llm_client/`** — which also catches the two eval-script bypasses D1 migrates. Each rule
  carries a **seeded negative**: fixtures constructing a raw-httpx LLM dispatch and a direct
  `acompletion()` call outside `llm_client/` must trip. · *Fails if* any executable reference
  survives, either seeded negative passes, or the check is only the existence-scan (a renamed
  raw-httpx client or a direct litellm call would satisfy a tombstone alone).
- **AC-7 — Local calls neither book cloud spend nor touch the gate.** · **Check:** unit test
  asserts a local-placement `respond()` performs no `gate.reserve()` call at all (call-level
  assert — the "no Postgres round-trip on the hot path" obligation of D7, not just clean
  accounting); post-deploy cost-ledger query over a live window shows zero reservations/commits
  attributed to local-placement calls while cloud rows continue unchanged. · *Fails if* local
  traffic reaches the gate, or cloud traffic stops producing gate rows (the gate must not be lost
  in the re-plumb).
- **AC-8 — `cache_prompt` survives the transport change.** · **Check:** delivery is the AC-1 wire
  assertion (`cache_prompt` top-level); outcome corroboration is a post-deploy ES query on
  `model_call_completed.cache_read_tokens` (the actual event field) over a 7-day window, at ≥50%
  of the pre-cutover 7-day baseline rate for multi-call local turns. · *Fails if* the wire key is
  absent, or the cache-read rate collapses below the threshold. (Telemetry alone cannot pass this
  AC: llama.cpp currently defaults cache reuse on, so non-zero reads without the wire key would be
  the backend masking a dropped parameter — the failure mode this whole ADR exists to end.)
- **AC-9 — Dispatch parity: what D7 promises, tests assert.** · **Check:** cutover parity suite —
  (a) streaming aggregation: a recorded pre-cutover SSE stream replayed through the unified path
  yields the same `LLMResponse` usage block and tool-call set; (b) error taxonomy: connection
  refusal, read timeout, 429 and 5xx from a stub server map to `LLMConnectionError`, `LLMTimeout`,
  `LLMRateLimit`, `LLMServerError` respectively; (c) header capture asserts all four propagation
  headers (`traceparent`, `X-Trace-Id`, `X-Span-Id`, `X-Session-Id`); (d) role-timeout test
  asserts the primary's 600s reaches the transport config; (e) the history sanitiser is invoked on
  the unified path (call assert); (f) telemetry parity: `model_call_completed` for a local call
  carries provider `slm_local`, role, and prompt-identity fields matching the canonical shape
  (field asserts on the emitted event); (g) text tool-call fallback: a response carrying a textual
  tool call and no structured `tool_calls` yields parsed tool calls through the unified path —
  today's `LiteLLMClient` reads only structured calls, so this leg fails until the fallback is
  ported; (h) reasoning preservation, both shapes: an inline `<think>…</think>` response and a
  `reasoning_content`-field response each populate `reasoning_trace` with visible content clean of
  think-tags. · *Fails if* any leg is missing or stubbed at the layer it claims to test.

---

## References

- [FRE-1362](https://linear.app/frenchforest/issue/FRE-1362) — umbrella ticket; the 2026-09-03 live measurement lives in its description
- ADR-0031 — Model Configuration Consolidation; its Alternative C (litellm as the unified abstraction, Deferred) closes on this ADR's acceptance
- ADR-0121 — Model Catalog and Selection Layer; the FRE-917 inert-ceilings note is factually corrected now, and "Vocabulary by dispatch path" is superseded on this ADR's acceptance (status updates added by this ADR's PR)
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
- EVAL-2026-05-11 (referenced from `config/models.yaml`'s primary entry) — evidences the primary's `temperature: 0.6`; `top_k: 20` itself is the Qwen/Unsloth model-card preset, not an isolated measurement (noted honestly in D4)

---

## Status Updates

### 2026-09-03 - Proposed
**Changed By:** adr session (FRE-1362)
**Reason:** Owner-directed unification, forced by the 2026-09-03 live measurement showing the
local dispatch path's `extra_body` block never reaches the server. Egress mechanism, big-bang
cutover, max_tokens coherence and the Critical designation of the adaptive-budget follow-up all
settled in owner discussion before drafting.
