# FRE-906: Wire `event_bus_ack_timeout_seconds` (or delete it)

## Scope

`event_bus_ack_timeout_seconds` (src/personal_agent/config/settings.py:1392) is declared
and validated (`ge=10, le=3600`, default 300) but has zero readers in production code —
confirmed by `grep -rn "event_bus_ack_timeout_seconds"` (only the declaration hits) and by
FRE-896's alias-aware audit.

## Investigation findings

ADR-0041 (Redis Streams event bus) explicitly lists, as one of the reasons Redis Streams
was chosen over alternatives: "`XCLAIM` for reassigning stuck messages" (line 73), and
sketches `ack_timeout_seconds: int = 300` in its example config block (line 203) — the
field exists to bound how long a message can sit unacknowledged in a consumer's Pending
Entries List (PEL) before it's considered abandoned and reclaimed.

`src/personal_agent/events/consumer.py`'s `_read_loop` only issues `XREADGROUP` with
`streams={sub.stream: ">"}` — the `>` operator means "only ever-undelivered messages."
There is no `XCLAIM`/`XAUTOCLAIM`/`XPENDING` call anywhere in `events/*.py` (confirmed by
grep). Every subscription in `service/app.py` uses a single, fixed `consumer_name` per
group (e.g. `"consolidator-0"`, `"session-writer-0"` — one process, one consumer per
group, no horizontal fan-out).

**This is a real gap, not just an unused knob.** If the service process is killed
mid-`sub.handler(event)` (OOM kill, `docker restart`, crash), the message stays claimed
by that consumer in Redis's PEL — delivered but never acknowledged. On restart, the same
`ConsumerRunner` reconnects with the *same* `consumer_name` and resumes reading only new
(`>`) messages. The orphaned message is never retried, never dead-lettered, and never
observed again. There is currently no recovery path at all for crash-time in-flight
messages.

**Decision: wire the reader.** The ack-timeout knob should govern a periodic
`XAUTOCLAIM` sweep that reclaims messages idle longer than
`event_bus_ack_timeout_seconds` back to the (single) consumer and reprocesses them
through the existing retry/dead-letter path. This directly matches ADR-0041's stated
design intent ("`XCLAIM` for reassigning stuck messages") and closes a real reliability
gap — deleting the field would leave the gap in place with no visibility.

**Precision on what this buys (codex plan-review, 2026-07-16):** with a fixed
`consumer_name` per group and no horizontal fan-out, this is **restart/self-recovery**,
not live reassignment to a healthy peer — there is no peer. The value is that a process
that crashed mid-handler and restarted (same consumer name) reclaims its own stranded
PEL entries instead of losing them forever. That is still a real, closed gap; it should
just be described accurately as self-recovery in code comments/PR, not "reassigning
work to another consumer."

**Idempotency trade-off, explicit:** today, a crash between handler success and `XACK`
orphans the message with no redelivery — no duplicate side effects are possible, but the
message and its work are lost. After this change, that same race instead produces
redelivery on the next sweep, i.e. the handler may run twice for that narrow window
(work + crash-before-ack). This is standard Redis Streams at-least-once semantics, and
ADR-0041's own risk table already assumes it ("Event ordering across streams not
guaranteed | Consumers designed to be idempotent"). Trading "lost forever" for
"at-least-once, may rarely double-run" is the correct direction and requires no new
idempotency work — it's the assumption the ADR was already built on.

## Design

In `ConsumerRunner._read_loop`, before each `XREADGROUP` poll, check whether
`event_bus_ack_timeout_seconds` has elapsed since the last claim sweep (sweep runs once
immediately at loop start too, so a restart recovers anything orphaned by the *previous*
process). If due, call a new `_claim_stuck_messages` helper that pages through
`XAUTOCLAIM` (`min_idle_time=event_bus_ack_timeout_seconds * 1000`, claiming to
`sub.consumer_name`) and feeds each reclaimed message through the existing
`_process_message` (same handler dispatch, same retry-count/dead-letter semantics — no
new failure path to reason about).

### `src/personal_agent/events/consumer.py`

```python
async def _read_loop(self, sub: Subscription) -> None:
    settings = get_settings()
    block_ms = settings.event_bus_consumer_poll_interval_ms
    max_retries = settings.event_bus_max_retries
    ack_timeout_ms = settings.event_bus_ack_timeout_seconds * 1000
    loop = asyncio.get_event_loop()
    next_claim_at = loop.time()  # sweep once immediately (recover prior-crash PEL)

    while self._running:
        try:
            now = loop.time()
            if now >= next_claim_at:
                await self._claim_stuck_messages(sub, ack_timeout_ms, max_retries)
                next_claim_at = now + settings.event_bus_ack_timeout_seconds

            results = await self._bus.client.xreadgroup(...)  # unchanged
            ...
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            ...  # unchanged

async def _claim_stuck_messages(
    self, sub: Subscription, min_idle_ms: int, max_retries: int
) -> None:
    """Self-reclaim PEL entries idle >= ack timeout (ADR-0041 XCLAIM) and reprocess them.

    The read loop only ever consumes new (">") messages, so a message left
    pending after a crash mid-handler is otherwise never retried, even after
    the process restarts with the same consumer name. Single-consumer-per-group
    topology (see service/app.py) means this reclaims to *itself*, not a live
    peer — restart/crash recovery, not load-balancing reassignment.
    """
    cursor = "0-0"
    try:
        while True:
            cursor, messages, _deleted = await self._bus.client.xautoclaim(
                name=sub.stream,
                groupname=sub.group,
                consumername=sub.consumer_name,
                min_idle_time=min_idle_ms,
                start_id=cursor,
                count=10,
            )
            if messages:
                log.warning(
                    "consumer_claim_swept",
                    stream=sub.stream,
                    group=sub.group,
                    consumer=sub.consumer_name,
                    reclaimed_count=len(messages),
                )
            for message_id, fields in messages:
                await self._process_message(sub, message_id, fields, max_retries)
            if cursor == "0-0":
                break
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        log.error(
            "consumer_claim_stuck_messages_error",
            stream=sub.stream,
            group=sub.group,
            error=str(exc),
            exc_info=True,
        )
```

No changes to `events/redis_backend.py` needed — `xautoclaim` is called directly on
`self._bus.client`, matching the existing pattern for `xreadgroup` (also called directly
on the raw client rather than through a `RedisStreamBus` wrapper method).

Identity threading (ADR-0074): `_claim_stuck_messages` has no `trace_id`/`session_id` in
scope before messages are parsed (pure boot/sweep scope, same class as
`consumer_loop_started` in `start()`) — exempt per
`scripts/check_identity_threaded.py`'s scope rule. Reclaimed messages are processed via
the existing `_process_message`, whose per-event logs already carry `trace_id` after
parsing — no new identity gap introduced.

## Test plan (TDD)

Reclaimed messages flow through the unmodified `_process_message` — retry-count,
`BudgetDenied` handling, and success/ACK semantics are identical to first-delivery
messages and already covered by `test_handler_error_triggers_dead_letter`,
`test_handler_succeeds_after_transient_failures`, and
`test_consumer_budget_denied.py`. Tests 6–7 below exist specifically to confirm the
reclaim path *reaches* `_process_message` cleanly (no double-dead-lettering, no
special-casing bug in the new sweep code) — not to re-verify `_process_message`'s
internals, which are out of scope for this change.

`tests/personal_agent/events/test_consumer.py`:

1. Add `client.xautoclaim = AsyncMock(return_value=("0-0", [], []))` to the shared
   `mock_redis` fixture (every test now exercises the startup sweep; empty result is a
   no-op, existing tests unaffected).
2. New test: `test_claims_and_reprocesses_stuck_message_on_startup` — `xautoclaim` returns
   one pending message on its first call; assert the handler receives it, `xack` is
   called with its message ID, and `xautoclaim` was called with
   `min_idle_time=<settings.event_bus_ack_timeout_seconds * 1000>`,
   `consumername=sub.consumer_name`.
3. New test: `test_claim_sweep_paginates_across_cursor` — `xautoclaim` side-effects two
   calls: first returns a non-`"0-0"` cursor with one message, second returns `"0-0"`
   with a second message; assert both messages are processed and `xautoclaim` was
   called twice, the second time with `start_id` equal to the first call's returned
   cursor.
4. New test: `test_claim_sweep_runs_periodically` — monkeypatch
   `personal_agent.events.consumer.get_settings` to a **fake settings double**
   (`SimpleNamespace`/`MagicMock`, not a real `AppConfig`/`Settings` instance — the
   `ge=10` field bound only applies to real Pydantic validation, not a plain test
   attribute) with `event_bus_ack_timeout_seconds=0.05` (sweep fires on effectively
   every loop iteration); assert `xautoclaim.call_count >= 2` after a short sleep.
5. New test: `test_claim_sweep_error_does_not_crash_read_loop` — `xautoclaim` raises;
   assert the loop keeps running (a subsequent normal `xreadgroup` message still gets
   processed) and `consumer_claim_stuck_messages_error` is logged (no unhandled
   exception propagates out of `_read_loop`).
6. New test: `test_reclaimed_message_exhausting_retries_is_dead_lettered_once` — a
   reclaimed message's handler always raises; assert exactly one `dead_letter` call
   (via `xadd` to the dead-letter stream) and exactly one `xack`, matching the existing
   non-reclaimed dead-letter test's shape (no double dead-lettering from the reclaim
   path re-entering `_process_message`).
7. New test: `test_reclaimed_malformed_message_is_acked_and_skipped` — a reclaimed
   message has no `"data"` field; assert it's ACKed and the handler is never invoked
   (reuses `_process_message`'s existing malformed-message branch — confirms the
   reclaim path shares it rather than special-casing).

Run: `make test-file FILE=tests/personal_agent/events/test_consumer.py`

## Verification (acceptance criteria)

1. `event_bus_ack_timeout_seconds` is read by production code: after the change,
   re-run `uv run python -m scripts.audit.config_usage_audit generate` and confirm the
   field is no longer in the `never-read` bucket.
2. No behavioral regression to the event bus: full `make test` (module first, then full
   suite) plus `make mypy` / `make ruff-check` / `make ruff-format` / `pre-commit run
   --all-files`.
3. Self-review: code-review skill at `low` effort (single-file, well-isolated addition
   to an already-tested consumer loop; no schema/security/cost/memory surface).

## Non-goals

- No change to `event_bus_ack_timeout_seconds`'s default, bounds, or description — the
  existing "Seconds before an unacknowledged message is considered stuck" description
  already matches the new behavior precisely.
- No new consumer-group topology (still single consumer per group) — reclaiming to
  `sub.consumer_name` (itself) is correct for this deployment shape; multi-consumer
  fan-out is out of scope.
- No dashboard/alerting on `consumer_claim_swept` — visibility-only via existing
  structlog → ES pipeline, no new Kibana panel requested by this ticket.
