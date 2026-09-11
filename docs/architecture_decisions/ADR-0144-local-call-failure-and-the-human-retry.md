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

> **Correction, 2026-09-11 (master, from FRE-1487).** The sentence that stood here — *"The origin
> defect is closed and is not ours"* — was wrong, and the paragraph above records the evidence that
> misled it. The rebuild restarted the process, and **the restart is what fixed it**, not the build.
>
> The defect was the launcher, not llama.cpp. `slm_server` started the backend with `stderr` on a
> pipe nothing read after startup. The macOS pipe buffer holds 65,440 bytes, llama.cpp queues a
> further 512 log entries, and then every thread that logs blocks. That is roughly 122 KiB of
> `stderr` per backend lifetime — 63 to 152 requests, depending on log volume per request.
>
> Reproduced on the origin 2026-09-11 with a 0.6B model launched the same way: requests 1–152
> succeeded, 153 hung, `/health` answered while `/slots` did not, and `SIGTERM` was ignored for 10
> seconds. **Reading the pipe completed the stalled request immediately**, released the pending
> `SIGTERM`, and the process exited with code 0 within 2 seconds. 125,037 bytes came out.
>
> So the defect was never closed. It recurred nine times between 2026-08-24 and 2026-09-11, and
> request count predicts those stalls (63, 73, 74, 75, 77, 106, 137) while uptime does not (2.5 to
> 22.5 hours). The same unread pipe is also why no llama-server log exists for any stall window:
> the defect destroyed the evidence needed to diagnose it.
>
> Fixed on the origin 2026-09-11 — each backend's `stderr` now goes to a log file, with the
> previous run kept as `.log.prev`.
>
> **What this changes for the decisions below: nothing, and that is worth stating.** D1–D4 address
> client-side defects that are real whether or not the origin was at fault. What it does change is
> their *motivation* — a silent origin is not a rare event to be tolerated, and FRE-1433's in-flight
> registry is now the highest-value of them, because our telemetry was blind for the full ten
> minutes the backend was frozen.

This ADR addresses what the incident exposed in our own client. Three things, all reachable from
this repo.

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
125-second default. The zone's configured value is readable from the zone itself. D1 requires
reading it rather than trusting either number. The Cloudflare terraform that manages the tunnel
lives outside this repository and declares no `proxy_read_timeout` resource, so the zone is the
source, not this repo's tree.

**A new risk sits under this, and it is untested.** The owner raised the served context window to
262,144 tokens on 2026-09-05, and the active primary deployment is `qwen3.8-flash-next`. Prefill
time grows with input size. If a full-context prefill emits no bytes for longer than the proxy's
gap bound, every such call fails at the proxy. The largest measured success carried 76,706 input
tokens. Nothing measures the range above that.

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

**Polling cannot fix this on its own.** The scheduled probe runs every 300 seconds
(`slm_health_probe_interval_seconds`), and its cached verdict is fresh for 45 seconds
(`slm_health_cache_ttl_seconds`), after which `get_cached_snapshot` returns `None`. So no verdict
exists for 255 seconds of every cycle. A stall that our own timeout ends inside two minutes can
begin and finish entirely between two ticks, and be seen by nobody.

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

1. **The proxy's configured value, read from the live zone.** Neither the 524 page's 120 seconds
   nor the documentation's 125-second default is taken as fact. The observed value and where it was
   read are recorded with the change, because no file in this repository holds it.
2. **The observed maximum time to first byte**, measured on the active primary deployment across
   input sizes spanning to the full 262,144-token window.
3. **A stated margin** between the two. The client and the proxy time different network legs from
   different start points, so the bound is verified through the deployed tunnel and not computed
   from a local clock alone.

**If the measurement shows that a full-context prefill exceeds the proxy's gap bound, that is an
architectural finding and this ADR must be revisited.** In that case the tunnel cannot carry a
full-context call at all, and no client-side setting repairs it. The measurement is therefore part
of the decision, not a detail of its rollout.

`default_timeout`'s own description names what it binds: a per-attempt wall-clock budget that
starts once the inference slot is held, covering stream creation and consumption, and not a gap.

### D2 — Two attempts, owned by us, for a named failure class only

**The contract is the number of requests the origin receives.** One failed call sends exactly two
when the failure is retry-eligible. A test counts requests at the transport, at the same seam where
FRE-1379 already asserts the background producers' `max_retries=0`.

**litellm's retry budget cannot express this, so the local path stops using it.** Each attempt
dispatches with `num_retries=0`, which the measured `2 * num_retries + 1` expansion makes exactly
one request. The second attempt is a loop we own. This is the only construction that yields two.

**Where the loop sits.** The retry controller wraps the slot, not the reverse. Each attempt
independently acquires its inference slot, registers with the D4 registry, dispatches under its own
`asyncio.timeout(default_timeout)`, consumes, closes its stream, unregisters, and releases the slot.
The backoff sleeps only after that unwind, holding no slot. A backoff that slept inside
`request_slot` would occupy origin capacity while sending nothing.

**Only a fast failure is retried, and this is what keeps the budget honest.** Two classes qualify:

- `LLMTimeout` raised by the **gap** bound — our sub-proxy read timeout, which fires in roughly two
  minutes.
- `LLMConnectionError` — a transport failure, bounded by `_LOCAL_NON_READ_TIMEOUT_S` at 10 seconds.

Everything else fails after one attempt. A wall-clock expiry is not retried: the origin was
answering, only too slowly, and a second full-length attempt cannot fit inside the turn. A 4xx, an
`LLMInvalidResponse`, a rate limit and an application 5xx are not transport failures, and a retry
does not address them.

**Retry eligibility and card eligibility are two different gates, and they are not the same set.**
D2 decides whether a second attempt is made. D3 decides whether the user is asked. A wall-clock
expiry is in D3's set and not in D2's. An application 5xx is in neither, and keeps the handling it
has today.

**The two timeouts are distinguished where they are raised, not after.** `LLMTimeout` is an empty
subclass, so a propagated one carries no discriminator. It does not need to: the gap bound arrives
as an `httpx` read timeout and the wall-clock bound as the `asyncio.timeout` expiry, and
`_respond_local` sees both before mapping. The retry decision is made there. The propagated error
records which bound fired as a field, for diagnosis only — no consumer branches on it.

That eligibility rule is what makes the arithmetic work. Two eligible attempts plus the backoff
cost roughly four minutes, which leaves the 900-second turn deadline ample room for the card. Two
600-second attempts would not.

The backoff obeys two constraints:

1. It exceeds the origin's observed watchdog recovery time, so the second attempt reaches a
   restarted server rather than the same wedged one. The observed restart took 26 seconds.
2. Two attempts plus the backoff fit inside the turn lifetime with time left to ask the user.

The chosen value and the measurement behind it are recorded with the change.

### D3 — Retry exhaustion pauses the turn and asks the user

When the second attempt fails, or when a non-eligible failure occurs, the turn does not end. It
pauses and asks.

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
- **Only a local primary call, and only an origin that produced no answer.** The pause binds
  `executor.py:6208`, but that call site serves cloud primaries too, and today every failure there
  falls through to the generic handler. A new branch precedes that handler. It fires only when the
  dispatched deployment's provider is local **and** the error is `LLMTimeout` — either bound — or
  `LLMConnectionError`. A wall-clock expiry raises the card even though D2 does not retry it: the
  user still has no answer, and the choice is still theirs. **`LLMServerError` is deliberately
  excluded**: `_map_local_dispatch_error` maps any 5xx there, so an application 500 from a healthy,
  reachable origin would otherwise raise the card. A 5xx means the origin answered. A 524 is the one
  5xx that means the opposite, and D1's gap bound is what stops one from arriving — a 524 that still
  reaches the mapper is a D1 failure to surface, not a case to fold in here.
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

### D4 — Health is written by real traffic, and probed only when there is none

Two sources now write one verdict, and consumers read it.

**A failing call writes `down`, and a succeeding one writes `up`.** When a local call's gap bound
fires, or its connection fails, the client has first-hand proof that the origin did not answer.
That call publishes `down` as it fails. A local call that completes publishes `up`. This is what
makes the stall observable at all: the scheduled probe runs every 300 seconds, and a stall our own
timeout ends in two minutes fits entirely between two ticks. Observation must not depend on
sampling a condition shorter than the sampling interval.

Publishing is best-effort. A failed write is logged and never replaces the original `LLMTimeout` or
`LLMConnectionError`, and never turns a health concern into a turn failure.

**The scheduler probes only an idle box.** `probe_slm_health` stops being a plain `GET`. When no
local generation is in flight it sends one bounded, non-thinking completion request: a fast answer
means `up`, a slow answer means `degraded`, no answer within the bound means `down`. When a local
generation **is** in flight it sends nothing and reads the in-flight calls instead.

That second mode is what separates a wedged box from a busy one. A probe that always generates
queues behind three legitimate long calls and reports `down` while the box is healthy. Reading the
in-flight call removes that false alarm and never competes for a slot.

**Two writers need one state model, and it has four rules.** The `slm_local` provider serves three
concurrent requests, and a verdict written by one call must not misrepresent the others.

1. **Progress outranks silence.** The box is generating if **any** in-flight call advanced its
   last-byte stamp within the threshold. `down` requires that **no** in-flight call has progressed
   within it. One stalled attempt beside a streaming sibling is not a wedged box, and the earlier
   "longest silent wins" rule got that wrong.
2. **The newer observation wins, and lateness is measured at observation.** Every verdict carries
   the monotonic time the evidence was gathered, not the time it was written. The cache accepts a
   write only when its observation time is newer than the stored one, so a slow probe that started
   before a call failed cannot overwrite that call's fresher `down`. `SlmHealthSnapshot` gains that
   stamp and the writer's identity.
3. **`down` is sticky. Only positive evidence clears it.** A `down` does not expire. It is replaced
   by a successful local call or a successful probe — evidence that the origin generated something.
   The earlier policy let a `down` decay to available after the 45-second TTL, which re-admitted
   dispatch to a still-dead origin and spent the next request rediscovering the same outage. `up`
   and `degraded` still expire at the TTL.
4. **A `down` shortens the probe interval until it clears.** Recovery needs positive evidence, and
   at 300 seconds a wedged-then-recovered origin stays blocked far longer than it is broken. While
   the verdict is `down`, the probe runs at a shorter interval. The box is idle by definition in
   that state, so the probe costs one small generation per interval and competes with nothing.

Seven mechanics carry the rest:

- **Silence is measured from dispatch, not from the first byte.** `GenerationProgress` stamps
  `generation_started_monotonic` on the first chunk, so a call that has received nothing has no
  timestamp at all — and that is exactly the failure. The registry stamps at dispatch, and a call
  with no chunk yet has an age from then.
- **The last-byte stamp counts every chunk, not only visible text.** `_update_generation_progress`
  folds `delta.content` alone, by design. The stamp is a separate rule in the same loop: any chunk
  advances it, including a reasoning delta. A stamp that tracked content only reports a normal
  thinking phase as silence.
- **The registry key is globally unique, not an ordinal.** One trace holds many model calls — the
  primary, each tool-loop round, each sub-agent — so `(trace_id, attempt)` collides. The key is the
  model call's own `span_id`, already generated per call at `litellm_client.py:1559`, plus the
  attempt ordinal within that call.
- **Removal is structural, not enumerated.** The record is removed in a `finally` around the whole
  attempt, so every exit is covered — success, cancellation, and any exception, including a
  connection failure before the stream opens and an aggregation failure after it closes. The
  existing stream-close `finally` is narrower and is not the right seam.
- **An availability check costs no generation, and reads under the state model above.**
  `provider_health.is_provider_available` calls `probe_slm_health` today, and session API paths
  reach it. Under a generating probe an ordinary availability read becomes a model call. So
  generation is confined to the scheduler and to real calls, and consumers read the published
  verdict: an uncleared `down` is unavailable, a fresh `up` or `degraded` is available, and an
  expired or absent verdict is available. That last case is the cold start, which must not block
  every local call — and a call that dispatches into a wedged origin discovers it within the gap
  bound and writes the verdict itself.
- **The verdict is durable, not only cached.** The 45-second in-process cache serves dispatch
  gating. A call-written verdict is also emitted as a health event through the existing
  `agent-monitors-slm-health` path, so an outage that a later session must diagnose leaves a record
  that outlives the cache entry.
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
since the last byte, which lives one level down at the model call, and one trace holds many.

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

### Option 5: Shorten the probe interval until polling can catch a stall

**Description:** Keep the probe as the only writer, and reduce
`slm_health_probe_interval_seconds` below the gap bound so no stall fits between two ticks.

**Pros:**
- One writer, one code path, no new publisher.
- Needs a settings change and nothing else.

**Cons:**
- Every tick on an idle box costs a generation. At a sub-two-minute interval that is continuous
  inference to observe nothing.
- It still samples. A stall shorter than the new interval still escapes.
- The failing call already holds the observation first-hand, so the polling is redundant work.

**Why Rejected:** It pays continuously to rediscover something one call already knows. D4 lets real
traffic write the verdict and keeps the probe for the idle case, where nothing else can speak.

---

## Consequences

### Positive Consequences

- A silent origin produces our own typed error with our trace, not a third-party HTML page.
- One failed call costs two origin requests, and that number is asserted rather than configured.
- A turn whose sub-agents succeeded returns their work, whatever the primary call does.
- The primary call gains partial-content recovery, which only sub-agents have today.
- A generation stall is recorded by the call that suffers it, so detection no longer depends on
  sampling a condition shorter than the sampling interval.
- The 262,144-token context window gets its first time-to-first-byte measurement, and the zone's
  proxy timeout gets read rather than assumed. Both numbers are load-bearing and nobody holds them.

### Negative Consequences

- The scheduler's health check consumes a generation slot when the box is idle. Monitoring now
  costs inference.
- The local path leaves litellm's retry handling and owns a loop instead. That is one more place
  where retry semantics live.
- Health now has two writers and a four-rule state model. That is more machinery than a cached
  probe result, and the ordering and stickiness rules must survive future edits.
- A sticky `down` can block local dispatch for as long as the shortened probe interval, where the
  old policy would have re-admitted a request. That is deliberate: the blocked interval is bounded
  and observable, and the alternative spends real turns rediscovering a known outage.
- A new constraint card is one more interaction the owner must answer, on a path that previously
  failed silently.
- The last-byte stamp adds a write to the hot chunk loop, on every chunk of every local stream.
- Two registries now hold per-call state at two altitudes, both process-local. Their distinctness
  and the single-worker invariant both need maintaining.

### Risks and Mitigations

| Risk | Severity | Mitigation |
|---|---|---|
| A full-context prefill exceeds the proxy's gap bound, so no client setting repairs the tunnel path | High | D1 makes the measurement part of the decision. The finding reopens this ADR rather than shipping a setting that cannot work |
| The gap bound is set against an assumed proxy value and races it in production | High | D1 reads the live zone value and records its source. AC-1 verifies the race through the deployed tunnel with a stated margin |
| The backoff sleeps while holding an inference slot, occupying origin capacity for nothing | High | D2 places the loop outside `request_slot`. AC-2 asserts no slot is held across the backoff |
| Retry eligibility is left as "a failed call", so a 4xx or a wall-clock expiry is retried | High | D2 names two eligible classes. AC-2 drives the non-eligible ones and asserts one request |
| Silence is timed from the first byte, so a call that never produces one has no age | High | D4 stamps at dispatch. AC-6(b) fails if a chunkless call has no age |
| The last-byte stamp counts `delta.content` only, so a thinking phase reads as a stall | High | AC-6(a) asserts a reasoning-only phase longer than the bound completes and stays `up` |
| A stall begins and ends between two 300-second ticks and is never recorded | High | D4 makes the failing call the writer. AC-9 fails if the record depends on a tick |
| An application 500 from a healthy origin raises the retry card | High | D3 excludes `LLMServerError`. AC-3 drives that case and fails on a card |
| The registry key collides across two model calls in one trace | Medium | Keyed on the per-call `span_id` plus attempt. AC-7 drives two calls sharing a trace and an ordinal |
| A proven `down` decays to available on the TTL, re-admitting dispatch to a dead origin | High | D4 rule 3 makes `down` sticky until positive evidence. AC-8 and AC-10 both fail on a decaying `down` |
| A stalled call marks the provider `down` while a sibling streams | High | D4 rule 1: progress outranks silence. AC-5(f) fails on a `down` |
| A slow probe's write overwrites a newer call-written `down` | Medium | D4 rule 2 orders on observation time, not write time. AC-5(h) fails if the stale write lands |
| A sticky `down` blocks dispatch long after the origin recovers | Medium | D4 rule 4 shortens the probe interval while `down`. AC-10 fails if recovery needs a real call |
| An availability read blocks every local call on a cold start | Medium | An absent verdict reads as available. AC-8's fifth state fails otherwise |
| A failed health write masks the caller's real error | Medium | Publishing is best-effort. AC-8 asserts the original error survives |
| The registry leaks records on an exception the enumerated list missed | Medium | Removal is a `finally` around the whole attempt, not an enumeration. AC-7 drives exception exits |
| A failed attempt's partial text is concatenated into a later attempt's reply | Medium | Per-attempt sink. AC-7 asserts it |
| The probe's own query triggers thinking and times out, reporting a false `down` | Medium | AC-5(a) fails on an idle healthy box that does not read `up` |
| A configuration change silently restores automatic retry | Medium | AC-2 counts requests at the transport. AC-4(c) asserts no preference path bypasses the card |

---

## Implementation Notes

**Files affected:**

- `src/personal_agent/llm_client/litellm_client.py` — the `read` arm (line 1517); a two-attempt
  controller wrapping `request_slot` (line 1550) with `num_retries=0` per attempt (line 1527); the
  last-byte stamp in the chunk loop (line 1600); registry insert and `finally` removal around each
  attempt; the failing-call health write.
- `src/personal_agent/llm_client/types.py` — `GenerationProgress` gains a last-byte stamp.
- `src/personal_agent/llm_client/` — the new in-flight local-call registry.
- `src/personal_agent/llm_client/provider_health.py` — reads the published verdict under the stated
  freshness policy, never generates.
- `src/personal_agent/observability/slm_health/cache.py` — observation-time ordering on write, a
  sticky `down`, and a reader that distinguishes uncleared, fresh, expired and absent.
- `src/personal_agent/observability/slm_health/snapshot.py` — `SlmHealthSnapshot` gains the
  observation stamp and the writer's identity that rule 2 orders on.
- `src/personal_agent/observability/slm_health/scheduler_runner.py` — the shortened interval while
  the verdict is `down`.
- `src/personal_agent/orchestrator/constraint_options.py` — the `model_unreachable` entry.
- `src/personal_agent/orchestrator/executor.py` — a typed-error branch before the generic failure
  handler (line 6410), and a per-attempt `progress_sink` on the primary call (line 6208).
- `src/personal_agent/observability/slm_health/probe.py` — the two-mode probe.
- `config/models.yaml` — the probe's deployment entry; the measured gap bound;
  `default_timeout`'s corrected description.

**Testing strategy.** The induced failures do not need a real outage. A fake origin that accepts a
connection and never writes reproduces the silent-origin case exactly, at the transport, and is
what AC-1's race, AC-2, AC-3, AC-7 and AC-9 run against. AC-1's time-to-first-byte measurement and
AC-5(e) need the live stack, which legitimate long calls produce without wedging anything.

**One obligation carries no acceptance criterion, deliberately.** D1 corrects `default_timeout`'s
description. A description is not an outcome, and an existence-check on its text is exactly the kind
of criterion the no-BS bar rejects. It is a review item on its implementation ticket, not an AC.

**Dependencies.** None on ADR-0143, which remains `Proposed`. D4's registry is deliberately
separate from ADR-0143 D1's turn registry, so neither blocks the other. Both assume one worker.

**Sequencing.** D1's two measurements run first — the zone's configured value and time to first
byte at full context on `qwen3.8-flash-next`. Their result decides whether D1 ships a value or
reopens this ADR.

---

## Verification / Acceptance Criteria

- **AC-1 — On the deployed tunnel, our read timeout always fires before the proxy's, and no call
  inside the healthy gap envelope trips it.** · **Check:** Read the zone's configured proxy read
  timeout from the live zone and record the value and its source. Through the deployed tunnel,
  point one call at an origin that accepts and never writes: assert the raised error is
  `LLMTimeout`, that it carries the call's trace_id, and that it fires at least the stated margin
  before the zone's value. Separately record time to first byte and maximum inter-chunk gap from
  the model-call span across input sizes spanning to 262,144 tokens on `qwen3.8-flash-next`. ·
  *Fails if* the error is not a typed `LLMTimeout`, if any 524 body reaches
  `_map_local_dispatch_error`, if the margin is unstated or unmet against the value read from the
  zone, or if the bound sits below the measured healthy gap envelope.

- **AC-2 — Exactly two requests for an eligible failure, exactly one for every other, and no slot
  held across the backoff.** · **Check:** Count requests at the transport, at the seam where
  FRE-1379 asserts the background producers' `max_retries=0`. Drive a gap timeout and a connection
  failure: assert 2 requests each, that the interval between them exceeds the origin's recorded
  watchdog recovery time, and that the inference slot is released for the whole backoff. Drive a
  wall-clock expiry, a 4xx, an `LLMInvalidResponse`, a rate limit and an application 500: assert 1
  request each. Then answer `retry_once_more` and assert the count rises by exactly 1. · *Fails if*
  any count differs, if a slot is held during a backoff, if the backoff is shorter than the
  recorded recovery time, or if the assertion reads a configuration value instead of counting
  requests.

- **AC-3 — A retry-exhausted local primary call asks the user, the turn acts on the answer, and
  nothing else raises the card.** · **Check:** Induce origin silence on a turn whose sub-agents
  completed. Assert exactly one `CONSTRAINT_PAUSE` for `model_unreachable`, whose context names the
  completed work. Answer `retry_once_more` with an origin that then succeeds, and assert the turn
  replies with that attempt's generated answer. Repeat, answer `stop_and_report`, and assert the
  reply carries both the sub-agent output and the partial primary content held in the attempt's
  sink. Assert a wall-clock expiry also raises the card, after exactly one attempt. Then drive five
  negatives: the same failure inside a sub-agent, the same failure on a cloud primary, an
  application 500 from a reachable local origin, a 4xx, and an `LLMInvalidResponse`. · *Fails if*
  the turn ends before the card, if a wall-clock expiry raises no card or raises one after two
  attempts, if any of the five negatives raises a card, if `retry_once_more`'s outcome is discarded,
  or if `stop_and_report` returns a bare error with the completed work dropped.

- **AC-4 — The card's fallbacks behave as declared, in all three.** · **Check:** (a) Let the card
  time out — assert `stop_and_report` applied. (b) Drop the socket and reconnect inside the
  timeout — assert the card is replayed and still answerable. (c) Write a `model_unreachable`
  preference by every path the preference store accepts, then induce the failure. · *Fails if* a
  timeout applies anything but the default, if a disconnect resolves the pause instead of waiting
  for reconnection or timeout, or if any preference path produces a silent retry.

- **AC-5 — The probe's verdict tracks generation, in every state, and costs the primary nothing.** ·
  **Check:** (a) Idle and healthy → `up` within the probe's own bound. (b) Idle and slow →
  `degraded`. (c) Idle and unanswering → `down`. (d) A local call in flight, silent past the
  threshold → `down`, **and** the probe issued no request of its own. (e) The origin saturated by
  legitimate long calls that are still streaming → `up` or `degraded`. (f) One stalled call beside
  one still streaming, both in flight → not `down`. (g) With the provider semaphore at its full
  capacity of 3 and a probe among the holders, dispatch a `USER_FACING` primary call and record its
  slot-acquisition wait; repeat with three real calls instead. Assert the probe case waits no
  longer. (h) Start a probe against a slow origin, and while it is in flight let a real call fail:
  assert the probe's later write does not replace the call's `down`. · *Fails if* any state returns
  another's verdict, if the probe generates while a call is in flight, if (a) times out — which
  shows the probe's query is thinking — if (f) reports `down`, if (g) shows the probe adding
  measurable wait, or if (h) lets the stale write land.

- **AC-6 — Silence is measured from dispatch, and a thinking phase is not silence.** · **Check:**
  (a) Drive a primary call whose response opens with a reasoning phase longer than the gap bound
  and emits no content deltas — assert the call completes and the verdict reads `up` throughout.
  (b) Read the registry for a call that has received no chunk at all — assert it reports an age
  measured from dispatch. · *Fails if* the call in (a) is cut or reads `down`, which shows the
  stamp advancing on `delta.content` only; or if (b) reports no age, or zero, before its first
  byte.

- **AC-7 — The registry holds exactly the live attempts, keyed uniquely, and no attempt's text
  leaks into another.** · **Check:** Drive concurrent local calls ending by success, gap timeout,
  wall-clock timeout, user cancel, a connection failure before the stream opens, and an aggregation
  failure after it closes. Assert the registry is empty once all have ended. Drive two distinct
  model calls under one trace_id, both at attempt 1, concurrently, and assert both are registered
  and distinguishable. Assert the reply after a successful second attempt contains no text from the
  failed first. · *Fails if* any ended attempt remains registered, if the two same-ordinal calls
  collide or overwrite, or if failed-attempt text appears in a later reply.

- **AC-8 — An availability check costs no generation, follows the state model, and never raises.** ·
  **Check:** Call `is_provider_available` for `slm_local` with the box idle and
  assert no completion request reaches the origin. Then drive five verdict states — an uncleared
  `down`, a `down` written longer than the TTL ago with nothing since, a fresh `up`, an `up` past
  the TTL, and no verdict at all on a cold start — and assert the return is `False`, `False`,
  `True`, `True`, `True` in that order. Then force each new failure path —
  registry unavailable, completion request rejected, malformed response, health write rejected —
  and assert
  `probe_slm_health` returns a snapshot every time, and that a rejected health write never replaces
  the caller's own error. · *Fails if* an availability read triggers a completion, if any of the
  five states returns the wrong answer — the second is the one a decaying `down` fails — or if any
  path raises instead of returning a snapshot.

- **AC-9 — A stall shorter than the probe interval is still recorded, from either evidence.** ·
  **Check:** With the scheduler's 300-second probe disabled entirely, drive one local call into a
  silent origin and let its gap bound fire. Assert a `down` verdict is published, attributed to
  that call's trace_id, before any scheduled probe runs. Repeat with a refused connection instead
  of a silent origin. · *Fails if* either case publishes no verdict without a tick, which shows the
  observation still depends on sampling a condition shorter than the sampling interval.

- **AC-10 — A `down` clears only on positive evidence, and recovery is detected without traffic.** ·
  **Check:** Drive a local call into a silent origin so it publishes `down`. Send no further
  traffic. Wait past the 45-second cache TTL and assert `is_provider_available` still returns
  `False`. Then let the origin recover and assert the verdict flips to `up` from the probe alone,
  within the shortened `down`-state interval and with no real call in between. Separately, publish
  a `down`, then complete one successful local call, and assert the verdict is `up`. · *Fails if*
  the `down` decays to available on the TTL alone, if recovery needs a real call to be noticed, or
  if a successful call leaves the `down` standing.

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
- ADR-0083 — the SLM-health probe and its snapshot cache
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
- `src/personal_agent/observability/slm_health/cache.py` — `get_cached_snapshot`, 45-second TTL
- `src/personal_agent/config/settings.py` — `slm_health_probe_interval_seconds` 300,
  `slm_health_cache_ttl_seconds` 45
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

### 2026-09-11 - Proposed (premise corrected)
**Changed By:** master, from FRE-1487
**Reason:** The origin defect was **not** closed by the rebuild. The rebuild restarted the process,
which emptied an unread `stderr` pipe; the freeze then recurred nine times between 2026-08-24 and
2026-09-11. Root cause reproduced on the origin and fixed there on 2026-09-11. See the Correction
in Context. D1–D4 are unchanged and still stand; FRE-1433 gains weight, because our own telemetry
emitted nothing for the full ten minutes the backend was frozen.
