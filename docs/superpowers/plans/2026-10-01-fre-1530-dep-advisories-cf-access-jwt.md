# FRE-1530 — Patch dependency advisories and fix two `cf_access_jwt.py` defects

> Ticket: FRE-1530 (Approved, `stream:build1`, Tier-2:Sonnet). Backing design: ADR-0132 D2 (guarded
> egress for the JWKS fetch). Risk tier: **Standard** — touches `src/` auth logic and dependency
> floors. Codex plan-review required. One phase = one PR.

## Scope

1. Raise dependency floors so `pip-audit` reports no advisory that has a published fix.
2. Fix Defect A: `RecursionError` escapes `CFAccessVerifier.verify()` (500 instead of 401).
3. Fix Defect B: an unknown `kid` forces a JWKS fetch on every request. Add a cooldown.
4. Fix the install doc trap: bare `uv sync` removes the dev extra.

## Design decisions (stated so the reviewer can challenge them)

- **Floors go in `[tool.uv] override-dependencies`.** `anyio`, `click`, `urllib3`, `diskcache` and
  `virtualenv` are transitive. `pyjwt` and `pip` already have an override line. No new mechanism.
- **`diskcache` has no published fix.** It cannot be floored. AC-4 needs a comment that states the
  exposure. Facts, measured on this branch:
  - In this application only `dspy` enables a `diskcache` cache (`dspy/clients/cache.py`,
    `FanoutCache`). `litellm` also imports it, but `dspy` disables the `litellm` cache.
  - `src/` never calls `dspy.configure_cache`, so the default applies.
  - Measured in the live gateway container (read-only): `DSPY_CACHEDIR` unset, user `root`,
    cache at `/root/.dspy_cache` (mode 755, root), and that path is not in the mount list.
  - The advisory (PYSEC-2026-2447) needs **write access to the cache directory** so a victim reads a
    crafted pickle. No network path in the application writes arbitrary bytes there.
  - Verdict: not exploitable from outside. Recheck when a fixed release appears, and recheck if
    `DSPY_CACHEDIR` is ever pointed at a mounted volume.
- **`pip` and `virtualenv` are folded in.** The ticket marks them "tooling only". Both have fixes.
  Raising them lets `pip-audit` report only the two advisories with no fix. The change is two lines.
- **Cooldown semantics.** A forced refresh runs only if no forced refresh ran in the last
  `_JWKS_FORCE_REFRESH_COOLDOWN_SECONDS` (60). The cooldown clock starts at the last *forced*
  attempt. The initial and TTL fetches do not start it. Reasons:
  - The first kid miss after load still refreshes at once. This keeps the existing documented
    behaviour and `test_jwks_refresh_retries_on_unknown_kid` unchanged (AC-9: a rotated kid verifies).
  - After that, N distinct unknown kids cost at most one fetch per window (AC-7).
  - A failed forced fetch still starts the cooldown. Otherwise a failing upstream is hit on
    every request.
  - The check runs **only inside the lock**. A check before the lock was tried and removed (code
    review): a request that arrives while a forced fetch is in flight would return at once, find
    the old keys and get a 401, although the fetch in flight holds its key. Queued requests wait
    for the fetch, see the cooldown, and read the fresh keys.
- **Assumption on AC-9 wording.** "A rotated `kid` still verifies within one cooldown period" is
  read two ways. Both are tested: (a) the first miss after load verifies at once; (b) after an
  unknown-kid burst used the window, a rotated kid is rejected inside the window and verifies once
  the window ends.
- **Defect A.** Catch `RecursionError` beside `jwt.InvalidTokenError` (header) and `jwt.PyJWTError`
  (decode). Keep the opaque failure message. pyjwt 2.15 also converts it, so the local catch makes
  the 401 contract independent of the library version. Two tests monkeypatch the raise to prove it.
- **Out of scope, noted.** An HTTP error from the JWKS fetch still escapes `verify()` as a 500. The
  ticket does not name it. Not changed here.

## Codex plan-review outcome (1 round, no High findings)

Each claim was checked before it was accepted.

| Finding | Check | Disposition |
|---------|-------|-------------|
| `TypeError` / `OverflowError` escape `verify()` on a signed token with `iat={}`, `exp=[]`, `exp=Infinity` | Reproduced on pyjwt 2.13.0: all three raise and none is a `PyJWTError` | **Folded in, then removed.** The decode `except` first caught `TypeError`, `ValueError`, `OverflowError`. Self-review showed pyjwt 2.15.1 (the new floor) converts all three itself, and the suite stayed green with the extra types removed. The catch was dead code and its comment was false, so it was deleted. The three signed-token tests stay as end-to-end contract guards. |
| Malformed HTTP 200 JWKS body (a JSON array) is cached for an hour, then `.get()` fails | Confirmed by reading `_ensure_jwks` (`self._jwks = resp.json()` runs before any shape check) | **Not changed.** Pre-existing, not named in the ticket. The body comes from Cloudflare through the guarded client, not from a caller. Reported in the handoff. |
| The burst test mixes sequential and concurrent calls, so it cannot prove the in-lock check | Correct: the sequential phase sets the cooldown, so the concurrent phase never reaches the lock | **Accepted.** Separate test, fresh verifier, an event that holds the first forced fetch until all tasks queue on the lock. |
| A failed forced fetch needs a test | Design says a failed attempt starts the cooldown | **Accepted.** Test: old JWKS kept, no second attempt inside the window, retry after the window. |
| Router test must seed `_jwks` and `_cached_at` | Correct: `_cached_at` starts at 0 and the TTL check uses `monotonic()` | **Accepted.** Seed both. A fake client fails the test if it is called. |
| pyjwt 2.15 may wrap the deep header upstream, so the real-token test could pass without the handler fix | Plausible. Red-first runs on 2.13, but the test would then lose its meaning after the upgrade | **Accepted.** Add two tests that monkeypatch `jwt.get_unverified_header` and `jwt.decode` to raise `RecursionError`. |
| diskcache statement: "only DSPy imports it" and "no mount" were not verified at runtime | `litellm` also imports `diskcache`. Live container (read-only check): `DSPY_CACHEDIR` unset, user `root`, `/root/.dspy_cache` exists, not in the mount list | **Accepted.** Comment rewritten from the measured facts. Write access to the cache directory is not claimed to equal prior code execution. |
| Compatibility proof misses entry points | `make test` can stay green while a CLI or MCP path breaks | **Accepted.** Step 3 adds CLI smoke commands and `tests/test_mcp/test_client.py`. |

## Steps

Run every command from `/opt/seshat/.claude/worktrees/build`.

### Step 1 — Failing tests first (on pyjwt 2.13, before any upgrade)

Files:

- `tests/personal_agent/service/test_cf_access_jwt.py` — add tests:
  - `test_verify_rejects_deeply_nested_header_as_verifier_error` — real 20,000-deep header.
  - `test_verify_wraps_recursion_error_from_header_parse` / `..._from_decode` — monkeypatch raise.
  - `test_verify_rejects_signed_token_with_malformed_time_claims` — `iat={}`, `exp=[]`,
    `exp=Infinity`, each signed with the real key.
  - `test_unknown_kid_burst_causes_at_most_one_forced_fetch` — 20 sequential calls, warm cache.
  - `test_concurrent_unknown_kids_share_one_forced_fetch` — fresh verifier, the first forced fetch
    is held on an event until every task has queued on the lock.
  - `test_failed_forced_fetch_keeps_old_jwks_and_starts_cooldown` — old keys kept, no second
    attempt in the window, retry after the fake clock passes the window.
  - `test_forced_refresh_cooldown_is_a_named_constant` — AC-8.
  - `test_rotated_kid_verifies_on_first_miss_after_load` — AC-9 (a).
  - `test_parallel_requests_with_rotated_kid_all_verify` — added after review: a request that
    arrives during the rotation fetch waits for it and verifies.
  - `test_rotated_kid_verifies_after_cooldown_when_window_was_used` — AC-9 (b), fake clock.
- `tests/personal_agent/service/test_artifacts_router.py` — add
  `test_nested_jwt_header_is_401_not_500` using the real `CFAccessVerifier` with a seeded JWKS.

Command: `make test-file FILE=tests/personal_agent/service/test_cf_access_jwt.py`
Expected: the new tests fail (RecursionError escapes; fetch count is above 1; constant missing).
Command: `make test-file FILE=tests/personal_agent/service/test_artifacts_router.py`
Expected: the new router test fails.

### Step 2 — Implement the handler fix and the cooldown

File: `src/personal_agent/service/cf_access_jwt.py`.

```python
_JWKS_TTL_SECONDS = 3600
# A signing-key miss forces a refresh, but at most once per window. Without this, a caller that
# sends random ``kid`` values makes every request fetch the JWKS from Cloudflare.
_JWKS_FORCE_REFRESH_COOLDOWN_SECONDS = 60
```

- `__init__`: `self._last_forced_at: float | None = None`
- `verify()` header: `except (jwt.InvalidTokenError, RecursionError) as exc:`
- `verify()` decode: `except (jwt.PyJWTError, RecursionError) as exc:`
- `_ensure_jwks`: add `_force_cooling_down()` and call it inside the lock only; set
  `self._last_forced_at = time.monotonic()` before the fetch when `force` is true.

```python
def _force_cooling_down(self) -> bool:
    """True when a forced refresh ran less than the cooldown ago."""
    return (
        self._last_forced_at is not None
        and (time.monotonic() - self._last_forced_at) < _JWKS_FORCE_REFRESH_COOLDOWN_SECONDS
    )
```

Command: `make test-file FILE=tests/personal_agent/service/test_cf_access_jwt.py` then the router
file. Expected: all pass.

### Step 3 — Dependency floors

File: `pyproject.toml`, `[tool.uv] override-dependencies`:

| Line | Change |
|------|--------|
| `pyjwt` | `>=2.13.0` becomes `>=2.15.0`, CVE comment (13 advisories, 2.14.0 and 2.15.0 fixes) |
| `urllib3` | `>=2.7.0` becomes `>=2.8.0`, CVE-2026-97687/97688/97689 |
| `pip` | `>=26.1` becomes `>=26.2`, PYSEC-2026-3721 |
| `anyio` | new `>=4.14.2`, CVE-2026-63374/64847 |
| `click` | new `>=8.3.3`, PYSEC-2026-2132 |
| `virtualenv` | new `>=21.7.13`, PYSEC-2026-4011/4012/4013/4014 |
| `diskcache` | comment line only, exposure assessment above (AC-4) |

Also fix `pyproject.toml:7` (`uv sync   # install runtime + dev dependencies`) to `uv sync --extra dev`.

Commands:

- `uv lock`
- `uv sync --extra dev`
- `uv sync --check --extra dev` — expect exit 0 (AC-2)
- `uv run --extra dev pip-audit` — expect only `diskcache` (PYSEC-2026-2447) and
  `CVE-2026-103001` (no fix) (AC-1)
- `uv pip list | grep -iE '^(pyjwt|urllib3|anyio|click|pip|virtualenv) '` — record versions

Compatibility smoke (after the lock; `make test` alone can stay green while these break):

- `uv run agent --help` · `uv run python -m personal_agent.ui.cli --help` · the `memory_cli` entry
  point `--help` (Typer on click 8.3).
- `uv run pytest tests/test_mcp/test_client.py` (anyio cancel-scope handling).
- `uv run python -W error::FutureWarning -c "import urllib3, httpx, requests"` (urllib3 2.8).
- `uv sync --frozen --no-dev --dry-run` — proves the production resolution used by
  `Dockerfile.gateway`.

### Step 4 — Docs

- Root `CLAUDE.md` line 9: `uv sync` becomes `uv sync --extra dev` (AC-5).
- `README.md` lines 115 and 133 hold the same trap for this repo's own install. Line 115 is
  changed. Line 133 installs the **separate** `slm_server` repo and is not changed.
- Regenerate any generated config inventory only if a gate fails on it.

### Step 5 — Gates

`make test` · `make mypy` · `make ruff-check` · `make ruff-format` · `pre-commit run --all-files`.
Then commit. Then `feature-dev:code-reviewer` and `security-review`, both scoped to
`git diff origin/main...HEAD`.

## Acceptance criteria and proof

| AC | Proof |
|----|-------|
| AC-1 | `pip-audit` output after Step 3 |
| AC-2 | `uv sync --check --extra dev` exit code |
| AC-3 | `pyproject.toml` diff: each raised floor has a CVE comment |
| AC-4 | `pyproject.toml` diff: `diskcache` exposure comment |
| AC-5 | `CLAUDE.md` diff |
| AC-6 | `test_verify_rejects_deeply_nested_header_as_verifier_error`, `test_nested_jwt_header_is_401_not_500` |
| AC-7 | `test_unknown_kid_burst_causes_at_most_one_forced_fetch` |
| AC-8 | `test_forced_refresh_cooldown_is_a_named_constant` |
| AC-9 | the two rotated-kid tests |
| AC-10 | Master. Deploy class: `seshat-gateway` rebuild. `Dockerfile.gateway` runs `uv sync --frozen --no-dev`, so the new `uv.lock` reaches the image. |

## Diff class

Not a production write path, not destructive, no schema, no cost or governance code. It is an
**auth-verification path** and a dependency bump of the HTTP stack. Self-serve unless the review
finds a reason to escalate.
