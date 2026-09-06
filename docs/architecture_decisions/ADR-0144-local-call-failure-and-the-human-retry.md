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

FRE-1398 was filed with the claim that Cloudflare's 120-second Proxy Read Timeout makes the
600-second `default_timeout` unreachable. That claim is wrong.

The Proxy Read Timeout bounds the interval **between bytes**. It does not bound total duration.
The owner's own successful re-run ran 541.97 seconds over the same tunnel. An earlier call ran
328 seconds with 76,706 input tokens over the same endpoint.

`default_timeout` reaches `_respond_local` twice, and only one arm is unreachable:

| Arm | Kind | Fires on the tunnel path |
|---|---|---|
| `asyncio.timeout(effective_timeout_s)` — `litellm_client.py:1592` | Total wall clock from slot acquisition | Yes |
| `httpx.Timeout(read=effective_timeout_s)` — `litellm_client.py:1517` | Gap between bytes | No |

Cloudflare's 120-second gap bound is always tighter than our 600-second read arm. So the read arm
binds nothing on that path, and a silent origin surfaces as a third-party HTML error page instead
of our own typed exception.

**A new risk sits under this, and it is untested.** The owner raised the served context window to
262,144 tokens on 2026-09-05. Prefill time grows with input size. If a full-context prefill emits
no bytes for more than 120 seconds, every such call fails at the proxy. The largest measured
success carried 76,706 input tokens. Nothing measures the range above that.

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

This matters because the origin serves three concurrent requests (`--parallel 3`, mirrored by
`max_concurrency: 3` on the `slm_local` provider). A retry budget that expands at the transport
can occupy every slot the box has.

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

`_respond_local` sets the `read` arm of its `httpx.Timeout` to a deliberate gap bound that is
strictly below Cloudflare's 120 seconds, instead of reusing `effective_timeout_s`.

Two results follow. Our own timeout fires first, so a silent origin raises a typed `LLMTimeout`
carrying our trace, and no 524 HTML body reaches the error mapper. And the setting stops reading
as a bound it never applies.

The value is **measured, not chosen**. Time to first byte is recorded across input sizes spanning
to the full 262,144-token window. The gap bound sits above the observed maximum with margin.

**If the measurement shows that a full-context prefill exceeds 120 seconds, that is an
architectural finding and this ADR must be revisited.** In that case the tunnel cannot carry a
full-context call at all, and no client-side setting repairs it. The measurement is therefore
part of the decision, not a detail of its rollout.

`default_timeout`'s own description names what it binds: a total wall-clock budget from slot
acquisition, and not a gap.

### D2 — One retry, and the contract is stated in origin requests

A failed local call is retried exactly once, after a backoff.

**The contract is the number of requests the origin receives, not the value of `num_retries`.**
One failed call sends exactly two requests. Whatever configuration produces that is the
configuration. A test counts requests at the transport, at the same seam where FRE-1379 already
asserts the background producers' `max_retries=0`.

Reading configuration back is not evidence. The measured note at `litellm_client.py:1521` records
`2 * num_retries + 1` transport requests against litellm 1.98.0, so a naive `num_retries=1`
produces three requests — exactly the capacity of a three-slot box.

The backoff obeys two constraints:

1. It exceeds the origin's observed watchdog recovery time, so the second attempt reaches a
   restarted server rather than the same wedged one. The observed restart took 26 seconds.
2. Two attempts plus the backoff fit inside the turn lifetime with time left to ask the user.

The chosen value and the measurement behind it are recorded with the change.

### D3 — Retry exhaustion pauses the turn and asks the user

When the second attempt fails, the turn does not end. It pauses and asks.

**This reuses the ADR-0076 constraint pause. It builds no new mechanism.** That machinery already
persists a pause across a dropped connection (FRE-928), credits the wait back to the turn budget
(`credited_pause_seconds`, FRE-1391), caps the wait by the turn lifetime (FRE-1392), and applies a
safe default on timeout or disconnect.

A new constraint joins `CONSTRAINT_OPTIONS`:

```python
"model_unreachable": [
    ConstraintOption(action_id="retry_once_more", label="Try again"),
    ConstraintOption(action_id="stop_and_report", label="Stop and show me what you have"),
],
```

The registry's convention places the safe default last. `stop_and_report` is that default, so a
timeout or a disconnect ends the turn with its partial work rather than spending more.

Four rules govern the pause:

- **The client never asks. It raises.** `_respond_local` has no session, no user and no transport.
  The executor owns the turn, the session and the pause, so the executor owns the ask. The client's
  contract is unchanged: it raises a typed error.
- **Primary calls only.** The pause binds `executor.py:6208`, the user-facing primary call. It does
  not bind `sub_agent.py:495`. Four failing sub-agents must never produce four cards. A sub-agent
  failure is already reported to the primary, which handles partial results.
- **No stored preference.** The pause is raised with `allow_preference=False`, following the
  `attachment_cost` precedent (ADR-0101 §8b). A remembered "always retry" reinstates the automatic
  retry loop this ADR removes. The decision belongs to a human every time.
- **The card names what already succeeded, and `stop_and_report` delivers it.** The pause context
  states which work completed. On `stop_and_report` the turn returns that work — sub-agent results
  and any partial content held in `GenerationProgress` (FRE-1379) — not a bare error string.

### D4 — The health check measures generation, and reads the live call before probing

`probe_slm_health` stops being a plain `GET`. It behaves in two modes.

**No local generation in flight** — send one bounded, non-thinking completion request. A fast
answer means `up`. A slow answer means `degraded`. No answer within the bound means `down`.

**A local generation in flight** — send nothing. Read how long that call has gone without a byte.
Below the threshold means `up`. Above it means `down`.

The second mode is what separates a wedged box from a busy one. A probe that always generates
queues behind three legitimate long calls on a full box and reports `down` while the box is
healthy. Reading the in-flight call removes that false alarm and never competes for a slot.

Three mechanics carry it:

- **The last-byte stamp counts every chunk, not only visible text.** `_update_generation_progress`
  folds `delta.content` alone, by design. The stamp is a separate rule in the same loop: any chunk
  advances it, including a reasoning delta. A stamp that tracked content only reports a normal
  thinking phase as silence.
- **A process-level registry of live local calls.** `GenerationProgress` is caller-owned, so the
  probe cannot see it. A small registry of in-flight local calls, keyed by trace and holding the
  last-byte stamp, gives the probe its read.
- **The probe never preempts user work.** It runs at the lowest inference priority, under its own
  deployment key so it cannot consume the primary's single slot, and it carries a short timeout.

**This registry is not ADR-0143's registry, and the two must not be merged.** ADR-0143 D1 proposes
a turn-level registry keyed by trace with a `started_at` stamp. Turn age cannot serve here: a
healthy 541-second turn and a wedged one are the same age. The signal that separates them is time
since the last byte, which lives one level down at the model call.

---

## Alternatives Considered

### Option 1: Raise Cloudflare's Proxy Read Timeout

**Description:** Configure the zone to allow a longer silence before the proxy cuts the connection.

**Pros:**
- No client change.
- Restores the 600-second read arm as a real bound.

**Cons:**
- The 120-second limit is fixed below the Enterprise plan.
- It treats the symptom. A silent origin is a fault at any limit.
- It delays detection. A longer limit means a wedged origin holds a slot for longer.

**Why Rejected:** The setting is not available on our plan, and raising it makes the failure slower
to detect rather than less likely.

### Option 2: Emit a keep-alive so the proxy always sees bytes

**Description:** Have the client or Caddy inject periodic bytes into a streaming response, so no
gap ever reaches 120 seconds.

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
- The health check can observe a generation stall, which is the failure mode that actually occurs.
- The 262,144-token context window gets its first time-to-first-byte measurement. That number is
  load-bearing and nobody holds it today.

### Negative Consequences

- The health check consumes a generation slot when the box is idle. Monitoring now costs inference.
- A new constraint card is one more interaction the owner must answer, on a path that previously
  failed silently.
- The last-byte stamp adds a write to the hot chunk loop, on every chunk of every local stream.
- Two registries now hold per-trace state at two altitudes. Their distinctness needs maintaining.

### Risks and Mitigations

| Risk | Severity | Mitigation |
|---|---|---|
| A full-context prefill exceeds 120 seconds, so no gap bound repairs the tunnel path | High | D1 makes the measurement part of the decision. The finding reopens this ADR rather than shipping a setting that cannot work |
| The last-byte stamp counts `delta.content` only, so a thinking phase reads as a stall | High | AC-6 asserts a reasoning-only phase longer than the bound completes and stays `up` |
| The probe's own query triggers thinking and times out, reporting a false `down` | Medium | AC-5(b) fails on a false `down`. Thinking suppression is verified against the served model, not assumed |
| The probe consumes the primary's single slot and delays user work | Medium | Lowest priority, its own deployment key, short timeout |
| A configuration change silently restores automatic retry | Medium | AC-2 counts requests at the transport. AC-4 asserts no preference path bypasses the card |
| The card appears once per failing sub-agent | Medium | D3 binds the primary call site only. AC-3 fails if a sub-agent failure raises a card |

---

## Implementation Notes

**Files affected:**

- `src/personal_agent/llm_client/litellm_client.py` — the `read` arm at line 1517; the retry budget
  at line 1527; the last-byte stamp in the chunk loop at line 1600.
- `src/personal_agent/llm_client/types.py` — `GenerationProgress` gains a last-byte stamp.
- `src/personal_agent/llm_client/` — the new in-flight local-call registry.
- `src/personal_agent/orchestrator/constraint_options.py` — the `model_unreachable` entry.
- `src/personal_agent/orchestrator/executor.py` — the pause at the primary call site, line 6208.
- `src/personal_agent/observability/slm_health/probe.py` — the two-mode probe.
- `config/models.yaml` — the measured gap bound; `default_timeout`'s corrected description.

**Testing strategy.** The induced failures do not need a real outage. A fake origin that accepts a
connection and never writes reproduces the silent-origin case exactly, at the transport, and is
what AC-1, AC-2 and AC-3 run against. AC-5 needs the live stack for its busy case, which
legitimate long calls produce without wedging anything.

**Dependencies.** None on ADR-0143, which remains `Proposed`. D4's registry is deliberately
separate from ADR-0143 D1's turn registry, so neither blocks the other.

**Sequencing.** D1's measurement runs first. Its result decides whether D1 ships a value or
reopens this ADR.

---

## Verification / Acceptance Criteria

- **AC-1 — Our gap bound cuts before Cloudflare, and never cuts a healthy call.** · **Check:**
  Run primary calls at input sizes spanning to the full 262,144-token window and record time to
  first byte from the model-call span. Then point the client at an origin that accepts and never
  writes. · *Fails if* any call the origin eventually answers is terminated by the read arm, or if
  a 524 HTML body still reaches `_map_local_error`, or if the bound was set without the
  measurement.

- **AC-2 — One failed call sends exactly two requests to the origin.** · **Check:** Count requests
  at the transport against the never-writing origin, at the seam where FRE-1379 asserts the
  background producers' `max_retries=0`. · *Fails if* the count is anything but 2, or if the
  assertion reads a configuration value instead of counting requests.

- **AC-3 — A retry-exhausted primary call asks the user, and the turn survives to act on the
  answer.** · **Check:** Induce origin silence on a turn whose sub-agents completed. Assert a
  `CONSTRAINT_PAUSE` event for `model_unreachable`, and that its context names the completed work.
  Answer `retry_once_more` and assert a third origin request. Repeat, answer `stop_and_report`, and
  assert the reply carries the sub-agent output. · *Fails if* the turn ends before the card, if a
  sub-agent failure raises a card, or if `stop_and_report` returns a bare error with the completed
  work discarded.

- **AC-4 — No stored preference can make the retry automatic.** · **Check:** Write a
  `model_unreachable` preference by every path the preference store accepts, then induce the
  failure. · *Fails if* any path produces a silent retry instead of the card.

- **AC-5 — The health check reports `down` on a wedged origin and never on a merely busy one.** ·
  **Check:** (a) With a local call in flight whose stream has stalled past the threshold, assert the
  probe reports `down` within one scheduler interval **and** issued no generation request of its
  own. (b) With the origin saturated by legitimate long calls, assert the probe reports `up` or
  `degraded`. · *Fails if* either case returns the other's verdict, or if the probe generates while
  a call is in flight, or if the probe's own query times out on an idle healthy box.

- **AC-6 — A thinking phase is not read as silence.** · **Check:** Drive a primary call whose
  response opens with a reasoning phase longer than the gap bound and emits no content deltas.
  Assert the call completes, and that the health check reports `up` throughout. · *Fails if* the
  call is cut, or if the probe reports `down`, either of which shows the stamp advancing on
  `delta.content` only.

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
- [FRE-1392](https://linear.app/frenchforest/issue/FRE-1392) — pause credit and the lifetime cap
  on a pause
- ADR-0076 — the constraint pause and its action-ID registry
- ADR-0101 §8b — the `allow_preference=False` precedent for a decision that must not be remembered
- ADR-0122 §3–§4 — `ConstraintDecision` and the computed-options widening
- ADR-0132 D1 — Caddy holds the CF Access token; the probe constructs no credential
- ADR-0141 — the unified LiteLLM dispatch path this ADR modifies
- ADR-0143 — the turn-level request registry; `Proposed`, and deliberately distinct from D4's
- `src/personal_agent/llm_client/litellm_client.py` — `_respond_local`, `_map_local_error`,
  `_update_generation_progress`
- `src/personal_agent/observability/slm_health/probe.py` — `probe_slm_health`
- [Cloudflare Error 524](https://developers.cloudflare.com/support/troubleshooting/http-status-codes/cloudflare-5xx-errors/#error-524-a-timeout-occurred)
  — the Proxy Read Timeout and its plan-dependent limit

---

## Status Updates

### 2026-09-06 - Proposed
**Changed By:** adr session, from FRE-1398
**Reason:** Owner took D1–D4 on 2026-09-06. The origin defect that triggered the incident is
closed by an upstream llama.cpp rebuild. This ADR covers the client-side defects it exposed.
