# FRE-1435 — SLM-health state model (ADR-0144 D4, rules 1–4)

**Ticket:** FRE-1435 · **ADR:** ADR-0144 D4 · **Unblocks:** FRE-1436 (the two-mode probe and the
call-side writer). This ticket builds the state model only. It adds no new writer.

## Scope

- `SlmHealthSnapshot` gains `observed_monotonic` (the evidence time) and `writer` (`"probe"` or `"call"`).
- The cache orders writes by `observed_monotonic`. A write that is not newer is rejected (rule 2).
- A `down` never expires. `up` and `degraded` expire at the TTL, measured from observation (rule 3).
- A pure function computes a verdict from the in-flight registry: any progress means `up` (rule 1).
- `is_provider_available` for a local provider reads the stored verdict and makes no network call.
- The scheduler probes at a shorter interval while the stored verdict is `down` (rule 4).

## Design

### snapshot.py
- `observed_monotonic: float = Field(default_factory=time.monotonic)`. Process-local. The ordering key.
- `writer: Literal["probe", "call"] = "probe"`. The default keeps every existing constructor valid.

### probe.py
- Pass `observed_monotonic=start` on every snapshot (four `down` returns and `_build_snapshot`).
  The probe's evidence time is its start, not its end. A probe that started before a call failed is
  therefore older than that call's `down`. This is what AC-2 needs.

### cache.py
- Keep one slot `_cached_snapshot`. Delete `_cached_at`. Age comes from `observed_monotonic`.
- `set_cached_snapshot(snapshot) -> bool`: reject when a stored snapshot exists and
  `snapshot.observed_monotonic <= stored.observed_monotonic`. Log `slm_health_stale_write_rejected`
  with both trace ids. Return whether the write landed.
- `get_cached_snapshot(ttl)`: `None` if empty. A stored `down` is returned at any age. Otherwise
  `None` when `now - observed_monotonic > ttl`.
- `verdict_is_down() -> bool`: the stored snapshot exists and is `down`.
- `inflight_verdict(calls, *, silence_threshold_s, now=None) -> Literal["up", "down"] | None`:
  `None` for no calls. `"up"` if any call has `silence_s(now) <= silence_threshold_s`. Else `"down"`.
  `InflightCall` is imported under `TYPE_CHECKING` only, to avoid an import cycle with `llm_client`.

**Revised after codex review.** A newer write clears a `down` only if it is positive evidence
(`_clears_down`): a newer `down`, a `writer="call"` verdict (a call writes non-`down` only on
success), or a probe with `generation_ok=True`. A probe with `generation_ok=False` never clears it.
A liveness-only probe (`generation_ok=None`) clears only a probe-written `down`, because that `down`
rested on liveness alone. Today the GET-only probe is the only writer, so it can still clear its own
`down`: no regression. A call-written `down` (FRE-1436) needs generation evidence.

The interval helper returns `min(down_s, normal_s)` while `down`, so a `down` never probes more
slowly than normal. The real bound is one interval plus one 60 s lifecycle tick.

Not taken from the review: keeping the live probe in `is_provider_available` until FRE-1436. The
ticket's AC-3 and AC-5 require the reader to answer from the verdict alone (a live GET fails the
"no verdict → True" state). The scheduler is on by default, so a dead origin is recorded within one
tick. The only consumer is the session API candidate list, which does not gate real dispatch.

### provider_health.py
- Local branch: `return not verdict_is_down()`. Remove the `probe_slm_health` call and import.
  Uncleared `down` → `False`. Fresh, expired or absent non-`down` → `True` (the cold start).
- `check_all_providers` keeps its signature. The local entry no longer costs a network call.

### settings.py
- `slm_health_probe_down_interval_seconds: float = 60.0`, `ge=60.0`. The lifecycle loop ticks every
  60 s (`LIFECYCLE_CHECK_INTERVAL_SECONDS`), so a smaller value cannot take effect.

### scheduler_runner.py + brainstem/scheduler.py
- `slm_health_probe_interval_s(*, normal_s, down_s) -> float`: `down_s` while `verdict_is_down()`.
- The scheduler reads the down interval at init (same `getattr` idiom) and gates the probe with the
  helper.
- `run_scheduled_slm_health_probe` logs whether `set_cached_snapshot` accepted the write.

### ES template
- `docker/elasticsearch/monitors-slm-health-index-template.json`: add `observed_monotonic` (float) and
  `writer` (keyword). Dynamic mapping already gives these types, so a re-PUT is optional.

### Docs
- Docstrings that say `is_provider_available` probes live: `probe.py`, `scheduler_runner.py`,
  `constraint_options.build_provider_availability`, `provider_health` module docstring.

## Tests (TDD — each fails before the change)

New `tests/observability/test_slm_health_state_model.py`:
- **AC-1** two `InflightCall`s, one silent 200 s, one stamped 1 s ago, threshold 90 s → `"up"`.
  Seeded negative: both silent → `"down"`. No calls → `None`.
- **AC-2** real `run_scheduled_slm_health_probe` → real `probe_slm_health` against an
  `httpx.MockTransport` whose handler waits on an event. While it waits, write a `writer="call"`
  `down`. Release. Stored verdict is the call's `down`. Seeded negative: same probe with no call
  write → its `up` lands.
- **AC-3** publish `down`, age it past 45 s (construct with `observed_monotonic = now - 46`),
  `is_provider_available(local)` is `False`.
- **AC-4a** standing `down`, then a `writer="call"` `up` → verdict `up`, available `True`.
- **AC-4b** standing `down` → interval helper returns 60, not 300 → scheduled tick with a recovered
  mock origin, no real call → verdict `up` → interval helper back to 300.
- **AC-5** the five states, parametrized → `False, False, True, True, True`.
- Local availability makes no HTTP request (`create_guarded_http_client` patched to fail).

Update: `tests/observability/test_slm_health_cache.py` (age from `observed_monotonic`; stale-write
rejection), `tests/personal_agent/llm_client/test_provider_health.py` (local tests read the cache).

## Commands

```bash
make test-file FILE=tests/observability/test_slm_health_state_model.py
make test-file FILE=tests/observability/test_slm_health_cache.py
make test-file FILE=tests/personal_agent/llm_client/test_provider_health.py
make test && make mypy && make ruff-check && make ruff-format && pre-commit run --all-files
```
