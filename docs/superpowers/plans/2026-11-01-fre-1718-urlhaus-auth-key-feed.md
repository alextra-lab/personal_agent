# FRE-1718 — Domain guard: move to the keyed URLhaus export

> Ticket: FRE-1718 (Approved, Urgent, Tier-2). Tier: Standard (src logic, secret, network).
> Backing design: ADR-0028 (egress security), FRE-1560 (fail-open fallback), FRE-1552 (shared platforms).

## Problem

`https://urlhaus.abuse.ch/downloads/text/` returns HTTP 410 (the whole `/downloads/` path is retired).
abuse.ch now serves the dumps from `https://urlhaus-api.abuse.ch/v2/files/exports/<Auth-Key>/<file>`.
The guard keeps its stale disk cache (43,635 domains, 8+ days old) and gets no new entries.
The only signal is a log line.

## Facts from master (2026-11-01, live check with the real key)

- `.../exports/<key>/recent.csv` returns 200, text/plain, 32 MB, 121,764 rows, 6,804 distinct hosts.
- Columns: `id,dateadded,url,url_status,last_online,threat,tags,urlhaus_link,reporter`. Comment lines start with `#`.
- abuse.ch asks for at most one dump fetch per 5 minutes.
- The owner decides later whether a fuller export is used. The file name sits in one constant.
- The key is in `pass` at `seshat/AGENT_ABUSE_CH_AUTH_KEY`. This session never reads it. Tests use a fake key.

## Design

1. **Setting.** `abuse_ch_auth_key: str | None` in `config/settings.py`, `json_schema_extra={"secret": True}`
   (same style as `perplexity_api_key`). Env name `AGENT_ABUSE_CH_AUTH_KEY`. Add the name, no value, to `.env.example`.
   Add the field to the `"secrets"` group in `service/settings_menu_groups.py`.
2. **Constants** in `security.py`: `_URLHAUS_EXPORT_BASE`, `_URLHAUS_EXPORT_FILE = "recent.csv"`,
   `_URLHAUS_URL_COLUMN = 2`, `_FEED_TIMEOUT_SECONDS = 60.0` (32 MB body), `_MIN_FEED_FETCH_INTERVAL_SECONDS = 300.0`,
   `_STALE_BLOCKLIST_ALERT_SECONDS = 86400.0`.
3. **Fetch.** `_fetch_urlhaus`:
   - No key → raise `UrlhausFeedError("AGENT_ABUSE_CH_AUTH_KEY is not set")` before any network call.
   - Build the URL with `urllib.parse.quote(key, safe="")`.
   - Catch `httpx.HTTPStatusError` → `UrlhausFeedError(f"feed returned HTTP {status}")`; catch `httpx.HTTPError` and
     `httpx.InvalidURL` → `UrlhausFeedError(f"feed request failed: {type(exc).__name__}")`. Always `from None`.
     Reason: an httpx error text includes the URL, and the key is in the URL path.
   - Parse with `csv.reader` over the lines that do not start with `#`, in `asyncio.to_thread` (about 120,000 rows). Take column 2 as the URL.
     Keep the existing host / shared-platform / URL-entry logic.
   - `UrlhausFeedError(RuntimeError)` is a private class in `security.py`.
4. **Rate.** The throttle sits inside `_fetch_urlhaus`, so every fetch path passes it (scheduler, `refresh()`,
   failures, restarts). It refuses a request when the last request was under 300 s ago. The last-request time is held
   in memory and in a marker file `<cache>.attempt` (its mtime), so a restart does not reset it. A throttled call raises
   `UrlhausFeedError("fetch throttled ...")` and the guard keeps its list. `get_domain_guard` also floors the TTL at 300 s.
5. **Stale signal (AC-3).** In `_fall_back`, after the existing `domain_guard_using_fallback` log, emit
   `domain_guard_blocklist_stale` (warning) when the source is `bundled` or the age is over 24 h.
   Fields: `source`, `count`, `age_seconds` (None for bundled), `max_age_seconds`. ES `agent-logs-*` holds the fields.
   No Grafana panel in this PR (see PR text).
6. **Docs.** Fix the stale "no key required" comment. Update `docs/reference/CONFIG_INVENTORY.md` if its check requires it.

## Steps (TDD)

1. Tests first in `tests/test_security/test_domain_guard.py`, class `TestFre1718KeyedUrlhausExport`
   (fake key `"test-key-not-real"`, `httpx.MockTransport` through a patched `AsyncClient`):
   - request path is `/v2/files/exports/test-key-not-real/recent.csv` (AC-1 mechanics).
   - CSV parse: comments skipped, column 2 used, quoted comma in `tags` ok, shared-platform URL entry kept,
     header row without `#` skipped.
   - **AC-2**: a 410 response with a stale disk cache → `domain_guard_using_fallback` has `source=stale_cache` and
     `age_seconds` ≈ cache age; the cached domain still blocks.
   - key absent from every captured log line and from the `UrlhausFeedError` text for: 410, 500, `ConnectError`,
     `ReadTimeout`.
   - no key set → no network call, fallback, text names the setting only.
   - **AC-3**: `domain_guard_blocklist_stale` fires for age 30 h and for bundled-only, and does not fire for age 2 h.
   - `get_domain_guard` with `url_guard_cache_ttl_seconds=60` yields a guard TTL of 300.
2. Update `_guard_loaded_from_feed` to wrap URL lines into CSV rows and set the fake key (existing FRE-1552 tests keep their meaning).
3. Run `make test-file FILE=tests/test_security/test_domain_guard.py` — expect the new tests to fail first.
4. Implement items 1–6. Re-run the file — expect all pass.
5. Gates: `make test`, `make mypy`, `make ruff-check`, `make ruff-format`, `pre-commit run --all-files`.
6. Commit, then `feature-dev:code-reviewer` and `security-review` on `git diff origin/main...HEAD`.

## Acceptance criteria and proof

| AC | Proof |
|----|-------|
| AC-1 | Unit test for the URL and the parse. Live proof is master's post-deploy check: `domain_guard_refreshed` with `count` from the new export. |
| AC-2 | `test_410_keeps_stale_cache_and_logs_age`. |
| AC-3 | `test_stale_event_*` tests. ES field `age_seconds` on event `domain_guard_blocklist_stale`. |
| Key hygiene | `test_key_never_appears_in_logs_or_errors`. |

## Out of scope

Grafana panel or alert rule (policy: built in the UI, not hand-written; master or the owner decides).
Choice of a fuller export (owner decision).

## Codex plan review (2026-11-01) — outcome

1. High, five-minute rule not enforced for forced refresh, failures, restarts → accepted. Item 4 above.
2. Medium, parse blocks the event loop and spikes memory → accepted in part: parse in `asyncio.to_thread`, lazy
   `io.StringIO` lines instead of `splitlines()`. The body (about 32 MB) is still held once as text.
3. Low, the empty-feed test passes through the missing-key path → accepted. The test sets the key through an autouse
   fixture and asserts the mocked request ran.
4. Low, `CONFIG_INVENTORY.md` is mandatory → accepted. Regenerated with `scripts/audit/config_inventory.py generate`.
