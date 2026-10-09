# FRE-1560 — The domain guard keeps its best list when a refresh fails

Ticket: FRE-1560 (Approved). Related: FRE-1552 (Codex finding 3), FRE-1330 (bundled union), FRE-225 (guard).
Tier: Standard (`src/` security logic) — codex plan-review required.

## Defect

`DomainGuard._refresh` (`src/personal_agent/security.py`) has two fail-open paths:

1. The disk cache is older than the TTL and `_fetch_urlhaus` raises. The `except` branch installs
   `_BUNDLED_BLOCKLIST` (four entries). It drops the in-memory list and the stale disk list.
2. `_save_to_disk_cache` sits in the same `try` as the fetch. A write error (`OSError`) discards a
   list that was fetched successfully and installs the four bundled entries.

## Design

New state on `DomainGuard`:

- `_blocklist_source: Literal["bundled", "cache", "feed"]` — where the in-memory list came from.
- `_blocklist_as_of: datetime | None` — when the list content was produced (feed fetch time, or the
  cache file's `cached_at`). `_last_loaded` keeps its meaning (last refresh attempt that set a
  TTL clock), so a fallback still waits one TTL before the next fetch.

Changed helpers:

- `_load_from_disk_cache(*, allow_stale: bool = False) -> tuple[frozenset[str], datetime] | None`.
  Returns the entries and `cached_at`. With `allow_stale=True` it ignores the TTL. The
  shared-platform host drop (FRE-1552) stays.
- `_install(domains, source, as_of)`: sets `_blocklist = domains | _BUNDLED_BLOCKLIST`, the source,
  `as_of`, and calls `_mark_loaded()`.

`_refresh` flow:

1. Fresh cache → `_install(..., "cache", cached_at)`; `log.debug("domain_guard_loaded_from_cache")`.
2. Fetch. On any `Exception`: `log.warning("domain_guard_feed_unavailable", error=...)`, then
   `_fall_back()`.
3. Fetch OK → `_install(..., "feed", now)`. Then save the cache in its own `try/except OSError`.
   On failure: `log.warning("domain_guard_cache_write_failed", error, path, count)`. The fetched
   list stays. Then `log.info("domain_guard_refreshed")`.

`_fall_back()` order (the ticket's order):

1. In-memory list when `_blocklist_source != "bundled"`: keep it, call `_mark_loaded()`. Source `memory`.
2. Else the disk cache with `allow_stale=True`: `_install(..., "cache", cached_at)`. Source `stale_cache`.
3. Else `_blocklist = _BUNDLED_BLOCKLIST`, source `bundled`, `as_of=None`, `_mark_loaded()`.

Log (replaces `domain_guard_using_bundled_fallback`; no other reader of the old name exists in the repo):
`log.warning("domain_guard_using_fallback", source=<memory|stale_cache|bundled>, count=..., age_seconds=<float|None>)`.
`age_seconds` is `now - as_of`; it is `None` for `bundled`.

## Steps

1. Tests first in `tests/test_security/test_domain_guard.py`, class `TestFre1560FailedRefreshKeepsBestList`:
   - AC-1: feed load → move `_last_loaded` past the TTL → fetch raises → `check_url` on a feed host is blocked; log event has `source="memory"` and an `age_seconds` float.
   - AC-2: write a stale cache file (2 h old, holds `old-evil.net`) → fetch raises → host blocked; `source="stale_cache"`, `age_seconds` ≈ 7200.
   - AC-3: fetch OK, `_save_to_disk_cache` patched to raise `OSError` → feed host blocked; `domain_guard_cache_write_failed` logged.
   - AC-4: each test asserts the log event through `structlog.testing.capture_logs()`.
   - Extra: no cache, no memory, fetch raises → `source="bundled"`, `age_seconds is None`.
   - Extra: a second failed refresh after a stale-cache fallback keeps the list (source `memory`).
   - Extra: a legacy stale cache with `github.com` still drops the platform host and keeps dedicated hosts.
   Run: `make test-file FILE=tests/test_security/test_domain_guard.py` → the new tests fail.
2. Implement in `src/personal_agent/security.py` as above. Update the class docstring line "Falls back to a bundled list".
3. Run the same file → all pass. Then `make test-file FILE=tests/test_security`.
4. Gates: `make test` · `make mypy` · `make ruff-check` · `make ruff-format` · `pre-commit run --all-files`.
5. Commit. Self-review (`feature-dev:code-reviewer`, `security-review`) on `git diff origin/main...HEAD`.
6. Rebase, push, open PR, post the handoff comment.

## Risks

- A test that patches `_load_from_disk_cache` or reads its return value would break. None exists (grep checked).
- The log event rename. No reader exists in `src/`, `tests/`, `config/` or `docs/` (grep checked).
- Diff class: security guard, not a production write path, not schema, not cost. Self-serve.

## Codex plan-review disposition

- **Empty or comment-only 200 response** (High) — folded in. An empty fetch result counts as a failed fetch: it goes to `_fall_back()` and never overwrites the cache. Test added.
- **Stale content kept without bound** (Medium) — accepted. `_mark_loaded()` on a fallback stays, so the next fetch waits one TTL. `domain_guard_using_fallback` repeats each TTL with `age_seconds`, so an outage stays visible in ES. A maximum stale age is a separate policy decision.
- **Naive or future `cached_at`** (Medium) — folded in. `TypeError` is caught (a naive timestamp counts as an unreadable cache). The logged age clamps to 0 or more.
- **Concurrent `ensure_loaded()` test** (Low) — skipped. The lock path is unchanged by this ticket.
