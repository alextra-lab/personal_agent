# ADR-0144: A local model call fails honestly, and the human decides the retry

**Status:** Proposed
**Date:** 2026-09-06
**Deciders:** Owner (D1–D4 owner-directed, 2026-09-06) · adr session
**Tags:** reliability, llm-client, observability, timeouts, retry, health

---

## Context

On 2026-09-05 a turn ran for 613.8 seconds and returned a 65-character error. Four sub-agents
had already succeeded. Their work was discarded.

The origin caused it. The llama.cpp backend on the owner's Mac hung. Its watchdog escalated to
SIGKILL and restarted the model in 26 seconds. The owner rebuilt llama.cpp at the upstream fix
and re-ran the identical query. It succeeded in 541.97 seconds with a 3,624-character answer.

**The origin defect is closed and is not ours.** This ADR addresses what the incident exposed in
our own client. Three things, all reachable from this repo.

### The proxy limit is a gap bound, not a total bound

FRE-1398 was filed with the claim that Cloudflare's Proxy Read Timeout makes the 600-second
`default_timeout` unreachable. That claim is wrong.

Cloudflare defines the Proxy Read Timeout as the maximum time between two read operations from the
origin. It bounds the interval **between bytes**. It does not bound total duration. The owner's
own successful re-run ran 541.97 seconds over the same tunnel. An earlier call ran 328 seconds
with 76,706 input tokens over the same endpoint.

`default_timeout` reaches `_respond_local` twice, and only one arm is unreachable:

| Arm | Kind | Fires on the tunnel path |
|---|---|---|
| `asyncio.timeout(effective_timeout_s)` — `litellm_client.py:1596` | Wall clock, started once the inference slot is held and covering both stream creation and consumption. The queue wait before the slot is **not** charged to it | Yes |
| `httpx.Timeout(read=effective_timeout_s)` — `litellm_client.py:1517` | Gap between bytes | No |

Cloudflare's gap bound is always tighter than our 600-second read arm. So the read arm binds
nothing on that path, and a silent origin surfaces as a third-party HTML error page instead of our
own typed exception.

**The exact proxy value is not established, and this ADR does not assume one.** Cloudflare's 524
page in the incident named a 120-second window. Cloudflare's current documentation gives a
125-second default. The zone is terraform-managed, so its configured value is readable. D1 requires
reading it rather than trusting either number.

**A new risk sits under this, and it is untested.** The owner raised the served context window to
262,144 tokens on 2026-09-05. Prefill time grows with input size. If a full-context prefill emits
no bytes for longer than the proxy's gap bound, every such call fails at the proxy. The largest
measured success carried 76,706 input tokens. Nothing measures the range above that.

### We cannot count our own requests to the origin

One incident produced four different attempt counts, and no model explains all four.

| Source | Attempts |
|---|---|
| litellm error text | `Retried: 1 times` |
| Wall clock, 319s at roughly 120s each | about 2 |
| slm_server, observed at the origin | 3 |
| `litellm_client.py:1521`, measured note `2 * num_retries + 1` at `num_retries=3` | 7 |

FRE-1398 AC-3 asks for a deliberate retry policy. A deliberate policy needs a number we trust. We
do not have one. Setting `llm_max_retries` and reading it back proves nothing about how many
requests the origin receives.

**That expansion also makes two attempts unreachable through configuration alone.** At
`2 * num_retries + 1`, `num_retries=0` sends one request and `num_retries=1` sends three. No
integer setting sends two. The current default of 3 sends seven.

This matters because the origin serves three concurrent requests (`--parallel 3`, mirrored by
`max_concurrency: 3` on the `slm_local` provider). A retry budget that expands at the transport can
occupy every slot the box has.

### The health check cannot observe the failure that matters

`probe_slm_health` issues one HTTP `GET`. Answering a `GET` consumes no generation slot. The probe
therefore reports `up` while every generation slot is wedged.

Nine `slm_health_probe_completed` events span the stall window. All nine read `status: up`,
including one at 05:22:34 while the call was failing. FRE-1398 cited exactly those events to rule
out the correct diagnosis.

A health signal that stays green through a total generation stall is worse than no signal. It
argued against the right answer.

---

## Decision

Four decisions. All four are owner-directed, taken on 2026-09-06.

### D1 — The gap bound becomes ours, set below the proxy's, and the real bounds are named

`_respond_local` sets the `read` arm of its `httpx.Timeout` to a deliberate gap bound below the
proxy's, instead of reusing `effective_timeout_s`.

Two results follow. Our own timeout fires first, so a silent origin raises a typed `LLMTimeout`
carrying our trace, and no 524 HTML body reaches `_map_local_dispatch_error`. And the setting stops
reading as a bound it never applies.

Three inputs fix the value, and all three are read rather than assumed:

1. **The proxy's configured value**, read from the terraform state that manages the zone. Neither
   the 524 page's 120 seconds nor the documentation's 125-second default is taken as fact.
2. **The observed maximum time to first byte**, measured across input sizes spanning to the full
   262,144-token window.
3. **A stated margin** between the two. The client and the proxy time different network legs from
   different start points, so the bound is verified through the deployed tunnel and not computed
   from a local clock alone.

**If the measurement shows that a full-context prefill exceeds the proxy's gap bound, that is an
architectural finding and this ADR must be revisited.** In that case the tunnel cannot carry a
full-context call at all, and no client-side setting repairs it. The measurement is therefore part
of the decision, not a detail of its rollout.

`default_timeout`'s own description names what it binds: a wall-clock budget that starts once the
inference slot is held, covering stream creation and consumption, and not a gap.

### D2 — Two attempts, owned by us, and the contract is stated in origin requests

A failed local call is attempted exactly twice, separated by a backoff.

**The contract is the number of requests the origin receives.** One failed call sends exactly two.
A test counts requests at the transport, at the same seam where FRE-1379 already asserts the
background producers' `max_retries=0`.

**litellm's retry budget cannot express this, so we stop using it for the local path.** Each
attempt dispatches with `num_retries=0`, which the measured `2 * num_retries + 1` expansion makes
exactly one request. The second attempt is a loop we own, in `_respond_local`, outside litellm.
This is the only construction that yields two.

Reading configuration back is not evidence. The count is asserted by counting requests.

The backoff obeys two constraints:

1. It exceeds the origin's observed watchdog recovery time, so the second attempt reaches a
   restarted server rather than the same wedged one. The observed restart took 26 seconds.
2. Two attempts plus the backoff fit inside the turn lifetime with time left to ask the user.

The chosen value and the measurement behind it are recorded with the change.

### D3 — Retry exhaustion pauses the turn and asks the user

When the second attempt fails, the turn does not end. It pauses and asks.

**This reuses the ADR-0076 constraint pause. It builds no new mechanism.** That machinery already
persists a pause across a dropped connection (FRE-928), credits the wait back to the turn budget
(`credited_pause_seconds`, FRE-1391), caps the wait by the turn lifetime (FRE-1392), and applies
its safe default when the wait times out.

A new constraint joins `CONSTRAINT_OPTIONS`:

```python
"model_unreachable": [
    ConstraintOption(action_id="retry_once_more", label="Try again"),
    ConstraintOption(action_id="stop_and_report", label="Stop and show me what you have"),
],
```

The registry's convention places the safe default last. `stop_and_report` is that default, so a
timed-out card ends the turn with its partial work rather than spending more. A dropped connection
does **not** resolve the pause: FRE-928 keeps the card answerable for a client that reconnects
inside the timeout.

Six rules govern the pause:

- **The client never asks. It raises.** `_respond_local` has no session, no user and no transport.
  The executor owns the turn, the session and the pause, so the executor owns the ask. The client's
  contract is unchanged: it raises a typed error.
- **Only a local primary call, and only an unreachable-origin failure.** The pause binds
  `executor.py:6208`, but that call site serves cloud primaries too, and today every failure there
  falls through to the generic handler. A new branch precedes that handler. It fires only when the
  dispatched deployment's provider is local **and** the error is `LLMTimeout`, `LLMConnectionError`
  or `LLMServerError` — the origin-unreachable set. Every other failure keeps its current path.
- **Never for a sub-agent.** `sub_agent.py:495` does not raise the card. Four failing sub-agents
  must never produce four cards. A sub-agent failure is already reported to the primary, which
  handles partial results.
- **No stored preference.** The pause is raised with `allow_preference=False`, following the
  `attachment_cost` precedent (ADR-0101 §8b). A remembered "always retry" reinstates the automatic
  retry loop this ADR removes. The decision belongs to a human every time.
- **The human-authorized attempt is one request.** `retry_once_more` re-dispatches with the same
  `num_retries=0` and no second D2 loop. One answer, one request.
- **The card names what already succeeded, and `stop_and_report` delivers it.** This requires a
  change the primary path does not have today: `executor.py:6208` passes no `progress_sink`, so
  partial primary content is discarded with the exception. The primary gains a sink, allocated
  **per attempt**, so text from a failed attempt is never concatenated into a later one.

### D4 — The health check measures generation, and reads the live call before probing

`probe_slm_health` stops being a plain `GET`. It behaves in two modes.

**No local generation in flight** — send one bounded, non-thinking completion request. A fast
answer means `up`. A slow answer means `degraded`. No answer within the bound means `down`.

**A local generation in flight** — send nothing. Read how long that call has gone without a byte.
Below the threshold means `up`. Above it means `down`. With several calls in flight, the verdict
follows the one silent longest.

The second mode is what separates a wedged box from a busy one. A probe that always generates
queues behind three legitimate long calls on a full box and reports `down` while the box is
healthy. Reading the in-flight call removes that false alarm and never competes for a slot.

Six mechanics carry it:

- **Silence is measured from the request, not from the first byte.** `GenerationProgress` stamps
  `generation_started_monotonic` on the first chunk, so a call that has received nothing has no
  timestamp at all — and that is exactly the failure. The registry stamps at dispatch, and a call
  with no chunk yet has an age from then.
- **The last-byte stamp counts every chunk, not only visible text.** `_update_generation_progress`
  folds `delta.content` alone, by design. The stamp is a separate rule in the same loop: any chunk
  advances it, including a reasoning delta. A stamp that tracked content only reports a normal
  thinking phase as silence.
- **A process-level registry of live local calls, keyed per attempt.** `GenerationProgress` is
  caller-owned, so the probe cannot see it. A registry keyed by trace **and attempt** — a trace has
  up to three attempts under D2 and D3 — holds the dispatch stamp and the last-byte stamp. A record
  is inserted at dispatch, inside the slot, and removed in a `finally` that covers success, read
  timeout, wall-clock timeout and cancellation. The registry is read only, never written, by the
  probe.
- **An availability check costs no generation.** `provider_health.is_provider_available` calls
  `probe_slm_health` today, and it is reached from session API paths. Under a generating probe an
  ordinary availability read becomes a model call. So the generating probe runs on the scheduler
  only. It publishes its verdict to a cached snapshot, and `is_provider_available` reads that
  cache.
- **The probe never raises.** `probe_slm_health`'s documented contract is that it always returns a
  snapshot. Every new path — the registry read, the completion request, a malformed response —
  keeps it.
- **The probe yields to user work, but does not preempt it.** It runs at `DEFERRED`, the lowest
  queue priority, under its own deployment key so it cannot consume the primary's single slot, and
  it carries a short timeout. The semaphore is not preemptive, so a probe already holding a slot
  keeps it until it finishes. Its short timeout is what bounds that.

**This registry is not ADR-0143's registry, and the two must not be merged.** ADR-0143 D1 proposes
a turn-level registry keyed by trace with a `started_at` stamp. Turn age cannot serve here: a
healthy 541-second turn and a wedged one are the same age. The signal that separates them is time
since the last byte, which lives one level down at the model call, and one trace holds several.

Both registries are process-local. That is sound only because Seshat runs one worker, the same
invariant ADR-0143 D1 relies on. A move to multiple workers invalidates both.

---

## Alternatives Considered

### Option 1: Raise Cloudflare's Proxy Read Timeout

**Description:** Configure the zone to allow a longer silence before the proxy cuts the connection.

**Pros:**
- No client change.
- Restores the 600-second read arm as a real bound.

**Cons:**
- The limit is fixed below the Enterprise plan.
- It treats the symptom. A silent origin is a fault at any limit.
- It delays detection. A longer limit means a wedged origin holds a slot for longer.

**Why Rejected:** The setting is not available on our plan, and raising it makes the failure slower
to detect rather than less likely.

### Option 2: Emit a keep-alive so the proxy always sees bytes

**Description:** Have the client or Caddy inject periodic bytes into a streaming response, so no
gap ever reaches the proxy's limit.

**Pros:**
- No call ever hits a 524.
- Simple to add at the proxy.

**Cons:**
- A wedged origin then looks alive to every layer above it.
- The fast, correct 524 becomes a silent 600-second hang.
- It defeats D4 directly: the last-byte signal becomes constant and measures nothing.

**Why Rejected:** It hides the exact fault this incident exposed. The 524 was the only accurate
signal produced on 2026-09-05, and this option removes it.

### Option 3: Automatic retry with exponential backoff, no human in the loop

**Description:** Keep retries automatic. Cap them, add backoff, and never interrupt the user.

**Pros:**
- Recovers silently when the origin returns quickly.
- No new interaction to build or explain.

**Cons:**
- We cannot count our own retries, so the cap is unverified by construction.
- An automatic loop against a three-slot box is what turned one hung request into an outage.
- Only the person waiting knows whether the answer still has value after ten minutes.

**Why Rejected:** Owner-directed. The system retries once, then hands the choice to the human. A
policy the system cannot count is not a policy.

### Option 4: Ask slm_server to report slot state on `/health`

**Description:** Extend the origin's health endpoint with queue depth and slot occupancy.
`probe.py` already parses `queue_depth` and `model_loaded` and already computes `degraded`.

**Pros:**
- Free on our side. The parsing exists.
- Reports the origin's own view, which is authoritative about the origin.

**Cons:**
- It lives in another repository and another release cycle.
- It is a second instrument for a fact our own process holds first-hand and without lag.
- A wedged origin may fail to report its own wedged state.

**Why Rejected:** Rejected as the primary instrument, and kept as confirmation. A health verdict
must not depend on a hung process describing itself accurately.

### Option 5: Derive liveness only from live traffic, and never probe

**Description:** Drop the probe. Judge health solely from the last-byte stamps of real calls.

**Pros:**
- Zero added load. No slot ever consumed by monitoring.
- Measures exactly the path that matters.

**Cons:**
- An idle system is unobservable. No traffic means no verdict.
- The first user after an idle period discovers the outage.

**Why Rejected:** It covers only half the state space. D4 probes when idle and reads when busy,
which covers both.

---

## Consequences

### Positive Consequences

- A silent origin produces our own typed error with our trace, not a third-party HTML page.
- One failed call costs two origin requests, and that number is asserted rather than configured.
- A turn whose sub-agents succeeded returns their work, whatever the primary call does.
- The primary call gains partial-content recovery, which only sub-agents have today.
- The health check can observe a generation stall, which is the failure mode that actually occurs.
- The 262,144-token context window gets its first time-to-first-byte measurement, and the zone's
  proxy timeout gets read rather than assumed. Both numbers are load-bearing and nobody holds them.

### Negative Consequences

- The scheduler's health check consumes a generation slot when the box is idle. Monitoring now
  costs inference.
- The local path leaves litellm's retry handling and owns a loop instead. That is one more place
  where retry semantics live.
- A new constraint card is one more interaction the owner must answer, on a path that previously
  failed silently.
- The last-byte stamp adds a write to the hot chunk loop, on every chunk of every local stream.
- Two registries now hold per-trace state at two altitudes, both process-local. Their distinctness
  and the single-worker invariant both need maintaining.

### Risks and Mitigations

| Risk | Severity | Mitigation |
|---|---|---|
| A full-context prefill exceeds the proxy's gap bound, so no client setting repairs the tunnel path | High | D1 makes the measurement part of the decision. The finding reopens this ADR rather than shipping a setting that cannot work |
| The gap bound is set against an assumed proxy value and races it in production | High | D1 reads the configured value from terraform. AC-1 verifies the race through the deployed tunnel with a stated margin |
| Silence is timed from the first byte, so a call that never produces one has no age | High | D4 stamps at dispatch. AC-6(b) fails if a chunkless call has no age |
| The last-byte stamp counts `delta.content` only, so a thinking phase reads as a stall | High | AC-6(a) asserts a reasoning-only phase longer than the bound completes and stays `up` |
| The human-authorized retry re-enters litellm's default budget and sends three requests | High | D3 fixes `num_retries=0` on that attempt. AC-2 counts it |
| An availability check triggers a model call through `provider_health` | Medium | D4 confines generation to the scheduler and serves consumers a cached verdict. AC-8 fails on any completion from an availability read |
| The registry leaks records on cancel or timeout, so a dead call reads as a live stall | Medium | Removal in a `finally` covering every exit. AC-7 drives all five exits |
| A failed attempt's partial text is concatenated into a later attempt's reply | Medium | Per-attempt sink. AC-7 asserts it |
| The probe's own query triggers thinking and times out, reporting a false `down` | Medium | AC-5(a) fails on an idle healthy box that does not read `up` |
| The card appears for a cloud primary or once per failing sub-agent | Medium | D3 gates on local provider plus the origin-unreachable error set. AC-3 fails on either |
| A configuration change silently restores automatic retry | Medium | AC-2 counts requests at the transport. AC-4(c) asserts no preference path bypasses the card |

---

## Implementation Notes

**Files affected:**

- `src/personal_agent/llm_client/litellm_client.py` — the `read` arm (line 1517); the two-attempt
  loop replacing `num_retries` on the local path (line 1527); the last-byte stamp in the chunk loop
  (line 1600); registry insert and `finally` removal around the slot (line 1550).
- `src/personal_agent/llm_client/types.py` — `GenerationProgress` gains a last-byte stamp.
- `src/personal_agent/llm_client/` — the new in-flight local-call registry.
- `src/personal_agent/llm_client/provider_health.py` — reads the cached verdict, never generates.
- `src/personal_agent/orchestrator/constraint_options.py` — the `model_unreachable` entry.
- `src/personal_agent/orchestrator/executor.py` — a typed-error branch before the generic failure
  handler (line 6410), and a per-attempt `progress_sink` on the primary call (line 6208).
- `src/personal_agent/observability/slm_health/probe.py` — the two-mode probe and the cached
  verdict.
- `config/models.yaml` — the probe's deployment entry; the measured gap bound;
  `default_timeout`'s corrected description.

**Testing strategy.** The induced failures do not need a real outage. A fake origin that accepts a
connection and never writes reproduces the silent-origin case exactly, at the transport, and is
what AC-1's race, AC-2, AC-3 and AC-7 run against. AC-1's time-to-first-byte measurement and
AC-5(e) need the live stack, which legitimate long calls produce without wedging anything.

**One obligation carries no acceptance criterion, deliberately.** D1 corrects
`default_timeout`'s description. A description is not an outcome, and an existence-check on its
text is exactly the kind of criterion the no-BS bar rejects. It is a review item on its
implementation ticket, not an AC.

**Dependencies.** None on ADR-0143, which remains `Proposed`. D4's registry is deliberately
separate from ADR-0143 D1's turn registry, so neither blocks the other. Both assume one worker.

**Sequencing.** D1's two measurements run first — the zone's configured value and time to first
byte at full context. Their result decides whether D1 ships a value or reopens this ADR.

---

## Verification / Acceptance Criteria

- **AC-1 — On the deployed tunnel, our read timeout always fires before the proxy's, and no healthy
  call trips it.** · **Check:** Read the zone's configured proxy read timeout from the terraform
  state that manages it. Through the deployed tunnel, point one call at an origin that accepts and
  never writes: assert the raised error is `LLMTimeout`, that it carries the call's trace_id, and
  that it fires at least the stated margin before the zone's configured value. Separately record
  time to first byte from the model-call span across input sizes spanning to 262,144 tokens. ·
  *Fails if* the error is not a typed `LLMTimeout`, if any 524 body reaches
  `_map_local_dispatch_error`, if the margin is unstated or unmet against the value read from
  terraform, or if any call the origin answers inside `default_timeout` is cut by the read arm.

- **AC-2 — One failed call sends exactly two requests, a human-authorized retry sends exactly one
  more, and the gap between them is long enough to matter.** · **Check:** Count requests at the
  transport against the never-writing origin, at the seam where FRE-1379 asserts the background
  producers' `max_retries=0`. Assert the count is 2, and that the interval between them exceeds the
  origin's recorded watchdog recovery time. Then answer `retry_once_more` and assert the count
  rises by exactly 1. · *Fails if* any count differs, if the backoff is shorter than the recorded
  recovery time, if two attempts plus the backoff leave no lifetime for the card, or if the
  assertion reads a configuration value instead of counting requests.

- **AC-3 — A retry-exhausted local primary call asks the user, and the turn acts on the answer.** ·
  **Check:** Induce origin silence on a turn whose sub-agents completed. Assert exactly one
  `CONSTRAINT_PAUSE` for `model_unreachable`, whose context names the completed work. Answer
  `retry_once_more` with an origin that then succeeds, and assert the turn replies with that
  attempt's generated answer. Repeat, answer `stop_and_report`, and assert the reply carries both
  the sub-agent output and the partial primary content held in the attempt's sink. Run the same
  failure inside a sub-agent, and again with a cloud primary. · *Fails if* the turn ends before the
  card, if a card appears for the sub-agent or the cloud primary, if a non-origin failure raises a
  card, if `retry_once_more`'s outcome is discarded, or if `stop_and_report` returns a bare error
  with the completed work dropped.

- **AC-4 — The card's fallbacks behave as declared, in all three.** · **Check:** (a) Let the card
  time out — assert `stop_and_report` applied. (b) Drop the socket and reconnect inside the
  timeout — assert the card is replayed and still answerable. (c) Write a `model_unreachable`
  preference by every path the preference store accepts, then induce the failure. · *Fails if* a
  timeout applies anything but the default, if a disconnect resolves the pause instead of waiting
  for reconnection or timeout, or if any preference path produces a silent retry.

- **AC-5 — The probe's verdict tracks generation, in every state.** · **Check:** (a) Idle and
  healthy → `up` within the probe's own bound. (b) Idle and slow → `degraded`. (c) Idle and
  unanswering → `down`. (d) A local call in flight, silent past the threshold → `down` within one
  scheduler interval, **and** the probe issued no request of its own. (e) The origin saturated by
  legitimate long calls that are still streaming → `up` or `degraded`. · *Fails if* any state
  returns another's verdict, if the probe generates while a call is in flight, or if (a) times out,
  which shows the probe's query is thinking.

- **AC-6 — Silence is measured from the request, and a thinking phase is not silence.** ·
  **Check:** (a) Drive a primary call whose response opens with a reasoning phase longer than the
  gap bound and emits no content deltas — assert the call completes and the probe reads `up`
  throughout. (b) Read the registry for a call that has received no chunk at all — assert it
  reports an age measured from dispatch. · *Fails if* the call in (a) is cut or reads `down`, which
  shows the stamp advancing on `delta.content` only; or if (b) reports no age, or zero, before its
  first byte.

- **AC-7 — The registry holds exactly the live attempts, and no attempt's text leaks into
  another.** · **Check:** Drive concurrent local calls ending by each of five exits — success, read
  timeout, wall-clock timeout, user cancel, and a human-authorized retry. Assert each attempt holds
  its own key, that the registry is empty once all have ended, and that the reply after a
  successful second attempt contains no text from the failed first. · *Fails if* any ended attempt
  remains registered, if two attempts of one trace share a key, or if failed-attempt text appears
  in a later reply.

- **AC-8 — An availability check costs no generation, and no health path raises.** · **Check:**
  Call `is_provider_available` for `slm_local` with the box idle, and assert no completion request
  reaches the origin. Then force each new failure path — registry unavailable, completion request
  rejected, malformed response — and assert `probe_slm_health` returns a snapshot every time. ·
  *Fails if* an availability read triggers a completion, or if any path raises instead of returning
  a `down` snapshot.

**Where these are adjudicated.** On FRE-1398, this ADR's umbrella ticket, once the implementation
chain has landed and deployed. Not at merge of this ADR, and not by any single implementation
ticket.

---

## References

- [FRE-1398](https://linear.app/frenchforest/issue/FRE-1398) — the umbrella ticket, its two
  comments closing AC-1 and AC-1b, and the owner's scope reduction
- [FRE-973](https://linear.app/frenchforest/issue/FRE-973) — prior appearance; added the
  900-second turn guard
- [FRE-401](https://linear.app/frenchforest/issue/FRE-401) — first recorded appearance, a
  `finish_now` synthesis call at roughly 507 seconds
- [FRE-1379](https://linear.app/frenchforest/issue/FRE-1379) — `GenerationProgress`, the
  wall-clock `asyncio.timeout`, and the transport-level retry assertion this ADR reuses
- [FRE-928](https://linear.app/frenchforest/issue/FRE-928) — a pause survives a dropped connection
- [FRE-1392](https://linear.app/frenchforest/issue/FRE-1392) — pause credit and the lifetime cap
  on a pause
- ADR-0076 — the constraint pause and its action-ID registry
- ADR-0101 §8b — the `allow_preference=False` precedent for a decision that must not be remembered
- ADR-0122 §3–§4 — `ConstraintDecision` and the computed-options widening
- ADR-0132 D1 — Caddy holds the CF Access token; the probe constructs no credential
- ADR-0141 — the unified LiteLLM dispatch path this ADR modifies
- ADR-0143 — the turn-level request registry; `Proposed`, deliberately distinct from D4's, and the
  source of the single-worker invariant both rely on
- `src/personal_agent/llm_client/litellm_client.py` — `_respond_local` (line 1344),
  `_map_local_dispatch_error` (line 245), `_update_generation_progress` (line 316)
- `src/personal_agent/llm_client/provider_health.py` — `is_provider_available`
- `src/personal_agent/observability/slm_health/probe.py` — `probe_slm_health`
- [Cloudflare Proxy Read Timeout](https://developers.cloudflare.com/api/resources/zones/subresources/settings/methods/get/)
  — defined as the maximum time between two read operations from origin
- [Cloudflare Error 524](https://developers.cloudflare.com/support/troubleshooting/http-status-codes/cloudflare-5xx-errors/error-524/)
  — the 125-second documented default and its plan-dependent ceiling

---

## Status Updates

### 2026-09-06 - Proposed
**Changed By:** adr session, from FRE-1398
**Reason:** Owner took D1–D4 on 2026-09-06. The origin defect that triggered the incident is
closed by an upstream llama.cpp rebuild. This ADR covers the client-side defects it exposed.
