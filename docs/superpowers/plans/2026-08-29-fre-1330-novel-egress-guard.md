# FRE-1330 — Block the named Alibaba bucket; alert on a first-ever fetch_url destination

Ticket: https://linear.app/frenchforest/issue/FRE-1330
Backing design intent: the ticket body itself (no separate ADR) — "Proposed shape" section:
1. targeted block now (cheap, reversible), 2. observe-only novel-destination signal keyed on
eTLD+1, 3. defer the block-vs-surface decision on novel destinations to a future ticket once
data exists.

## Scope

- **AC-3 (block)**: add the exact bucket hostname to `DomainGuard`'s bundled blocklist so it is
  refused pre-connection in `blocklist` mode (the deployment's default), same code path that
  already logs `fetch_url_blocked` in `tools/fetch.py`.
- **AC-1/AC-2 (novel-destination signal)**: new `NovelDestinationTracker` in `security.py`,
  disk-persisted sightings keyed on eTLD+1 (registrable domain), consulted once per `fetch_url`
  call. First sighting of a domain in the trailing N days (default 14) logs a distinct
  `fetch_url_novel_destination` event; a repeat sighting inside the window logs nothing.
  Observe-only — never blocks, per the ticket's explicit "surface first" call.
- **AC-4 (long tail)**: no change to any currently-working destination; the new tracker never
  denies, and the block list addition is an exact-hostname match that cannot collide with the
  22-call history (none of it is `*.aliyuncs.com`).

## Codex plan-review findings, folded in before implementation

Reviewed by `codex:rescue` (go-with-fixes verdict). Material findings and how they're handled:

- **AC-3 latent hole (found, not hypothesized)**: `DomainGuard._refresh()`'s disk-cache branch
  (`security.py:220`) sets `self._blocklist = cached` on a valid disk cache **without** unioning
  `_BUNDLED_BLOCKLIST` — only the network-fetch branch does that union. A stale-but-still-valid
  disk cache from before this deploy (predating the new bundled entry) would silently drop the
  new block on the brainstem's next warm-reload, for up to `ttl_seconds` (1h default). Pre-existing
  bug, but it directly undermines this ticket's AC-3 durability — folded in: union
  `_BUNDLED_BLOCKLIST` on the disk-cache branch too, with its own regression test.
- **Cache transaction atomicity**: the tracker's load→check→update→prune→save sequence must run
  as one unit inside the `asyncio.Lock`, not just individual steps — made explicit below.
- **Atomic write**: `_save()` writes to a `.tmp` sibling then `Path.replace()`s over the target,
  avoiding truncated JSON on a mid-write crash.
- **Fail-open**: the tracker's own load/save failures are caught internally (never raise); the
  call site in `fetch_url_executor` additionally wraps the whole novelty check in
  `try/except Exception` — an observe-only signal must never be able to block or fail a fetch.
- **IP literals**: `_registrable_domain` must not collapse an IPv4 literal by its trailing two
  octets (meaningless grouping) — detect via `ipaddress.ip_address()` and return the literal
  unchanged. Single-label hosts and malformed/empty strings already fall through the existing
  `len(parts) <= 2` branch correctly; added explicit tests for both instead of leaving them
  implicit.
- **Multi-process scope**: the tracker's `asyncio.Lock` is process-local, same scope as
  `DomainGuard`'s own `_refresh_lock` — stated as an accepted parity gap, not a new one.
- **AC-4 breadth**: test the full 22-destination list from the ticket, not a representative
  subset.
- **Concurrency**: add a test that two concurrent `check_and_record()` calls for the same
  brand-new domain produce exactly one `novel=True`.

## Files

- `src/personal_agent/security.py`
  - Add `"routify-file-proxy-sg.oss-ap-southeast-1.aliyuncs.com"` to `_BUNDLED_BLOCKLIST`.
  - Fix `DomainGuard._refresh()`'s disk-cache branch to union `_BUNDLED_BLOCKLIST` (see above).
  - Add `_registrable_domain(hostname) -> str` — last-two-labels eTLD+1 approximation with an
    explicit IP-literal bypass, with a docstring stating the known multi-part-suffix gap (e.g.
    `co.uk`) rather than pulling in a public-suffix-list dependency for a case that has never
    occurred in this deployment's fetch history (22 calls, all single-label public suffixes).
  - Add `NoveltyResult` (frozen dataclass: `novel: bool`, `registrable_domain: str`).
  - Add `NovelDestinationTracker` class: `__init__(cache_path, window_seconds)`,
    `async check_and_record(url) -> NoveltyResult`. The entire load→check→update→prune→save
    sequence runs inside one `asyncio.Lock` acquisition. Persists `{domain: last_seen_iso}` as
    JSON to `telemetry/security/egress_novelty_seen.json` via atomic temp-file-then-replace.
    Prunes entries older than the window on every save. Load/save failures are caught and
    logged, never raised.
  - Add `get_novelty_tracker()` singleton getter (mirrors `get_domain_guard()`), reading the new
    `settings.url_guard_novelty_window_days`.
- `src/personal_agent/config/settings.py`
  - Add `url_guard_novelty_window_days: int` (default 14, `ge=1`, alias
    `AGENT_URL_GUARD_NOVELTY_WINDOW_DAYS`), placed after the existing `url_guard_*` block.
- `src/personal_agent/tools/fetch.py`
  - Import `get_novelty_tracker` alongside the existing `security` imports.
  - In `fetch_url_executor`, immediately after the `fetch_url_started` log and before the
    guarded-client call, wrapped in its own `try/except Exception` (fail-open — an observe-only
    signal must never block or fail a fetch): `novelty = await
    get_novelty_tracker().check_and_record(url)`; if `novelty.novel`, `log.info(
    "fetch_url_novel_destination", trace_id=trace_id, url=url, domain=novelty.registrable_domain)`.
    Runs regardless of whether the fetch is later blocked or errors — it is a signal on what the
    model *targeted*, not on what succeeded.

## Tests (TDD — failing first)

- `tests/test_security/test_domain_guard.py`:
  - The exact bucket hostname from the ticket is present in `_BUNDLED_BLOCKLIST` and
    `DomainGuard.check_url()` on the ticket's literal URL returns `allowed=False,
    reason="blocklist_match"`.
  - **Fold-in regression**: a valid disk cache (domains that do NOT include a bundled entry)
    still results in a bundled domain (e.g. `malware.wicar.org`) being blocked after
    `_refresh()` loads from that cache — proves the union fix, not just the fresh-`__init__`
    default.
- `tests/personal_agent/tools/test_fetch.py`:
  - **AC-3**: `fetch_url_executor` on the ticket's exact URL raises `ToolExecutionError`
    matching "domain guard", with `httpx.AsyncHTTPTransport.handle_async_request` patched to
    raise `AssertionError` if reached (same `_unreachable_transport` pattern already used for
    the SSRF tests in this file) — proves refusal, not a 403 from the origin.
  - **AC-4 regression, full list**: parametrized over all 22 destinations from the ticket's
    call history, asserting `DomainGuard.check_url()` still allows each — proves the new
    bundled entry doesn't widen to a parent domain by accident. Plus one end-to-end
    `fetch_url_executor` case (mocked HTTP 200) for a representative long-tail host, pairing
    with the AC-3 blocked case the way the existing SSRF tests pair refused/allowed.
- New `tests/test_security/test_novelty_tracker.py`:
  - `_registrable_domain`: exact-match cases from the 22-call history (`docs.exa.ai` → `exa.ai`,
    `raw.githubusercontent.com` → `githubusercontent.com`, `en.wikipedia.org` /
    `fr.wikipedia.org` → `wikipedia.org`, `climate-api.open-meteo.com` /
    `marine-api.open-meteo.com` → `open-meteo.com`); the stated multi-part-suffix gap case
    documented as known-wrong (not asserted as correct); an IPv4 literal returned unchanged
    (not collapsed by trailing octets); a single-label host (e.g. `localhost`) returned
    unchanged; empty string → empty string.
  - **AC-1 shape**: first `check_and_record()` call for a domain → `novel=True`; the on-disk
    cache file now contains that domain.
  - **AC-2 (seeded negative)**: second `check_and_record()` call for the same domain, still
    inside the window → `novel=False`. This is the required negative case — a tracker that
    fires on every call would pass a novel-only test suite.
  - Cross-subdomain collapse: `check_and_record("https://docs.exa.ai/x")` then
    `check_and_record("https://exa.ai/y")` — second call is `novel=False` (same eTLD+1 key).
  - Window expiry: a domain whose recorded `last_seen` is older than `window_seconds` (write it
    directly into the cache file, backdated) → `novel=True` again on next sighting.
  - Corrupt/missing cache file → treated as empty (first sighting of anything is novel), no
    exception raised.
  - Concurrency: `asyncio.gather()` two `check_and_record()` calls for the same brand-new
    domain → exactly one result has `novel=True`.
  - Save failure (cache path under a directory that can't be created/written) → the call still
    returns a `NoveltyResult` without raising; a warning is logged.
- `tests/personal_agent/tools/test_fetch.py` addition wiring the tracker into the executor:
  - Fresh domain → `fetch_url_executor` logs `fetch_url_novel_destination` (assert via
    `patch("personal_agent.tools.fetch.log")`, mirroring how `test_domain_guard.py` asserts
    `mock_log.warning.assert_called_once()`). Point the tracker's cache at `tmp_path` via
    `monkeypatch.setattr("personal_agent.tools.fetch.get_novelty_tracker", ...)`.
  - Repeat domain (pre-seeded cache) → the event is NOT logged. This is AC-2's fetch-level
    pairing — a positive-only test here would be exactly the failure mode AC-2 calls out.
  - Tracker raising (simulating an I/O failure) → `fetch_url_executor` still completes
    successfully (fail-open) — proves the try/except wrapping actually works, not just that it
    exists in the diff.

## Non-goals (explicitly out of scope per the ticket)

- No blocking on novel destinations — "surface first" is the ticket's own call, not mine to
  override.
- No public-suffix-list dependency — the last-two-labels approximation covers every domain in
  this deployment's actual fetch history; documented as a residual gap, not silently assumed
  correct.
- No new metrics/counter infrastructure — this codebase's existing "counter" for a log event is
  `telemetry.metrics.get_recent_event_count(event, window_seconds)`, which works against any
  event name for free once the event is logged. AC-1's "and a counter" is satisfied by the event
  existing under a distinct name, not by building new machinery.

## AC verification (for the PR handoff)

- AC-1/AC-2: mechanism proven by unit test (above); ticket explicitly requires live ES
  visibility too ("Fails if demonstrated only by unit test") — that half is a post-deploy
  verification step for master, not something this build seat can satisfy from a worktree with
  no live ES connection. Handoff will include the exact query.
- AC-3: proven end-to-end via `fetch_url_executor` against the literal ticket URL, transport
  patched unreachable.
- AC-4: proven via the parametrized long-tail regression case.
