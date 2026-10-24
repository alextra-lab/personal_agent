# FRE-1672 — fetch_url session failure memo

Ticket: FRE-1672 (source: FRE-1666 study, finding F6). Tier: Standard (touches `src/` logic).
No ADR. The change is local to `src/personal_agent/tools/fetch.py`.

## Scope

`fetch_url` retries a URL that already failed in the same session. Production showed 5 repeats in
one week, and one page timed out 5 times (100 s). Add a session-scoped memo of failed fetches.

Rules (from the ticket):

- A non-retryable failure repeated for the same URL in the session returns the earlier error at
  once. The message says "This URL already failed in this session". No network call is made.
- A retryable failure (timeout, connect failure, 429, 5xx) gets one more real attempt. After the
  second failure, the first rule applies.

## Design decisions

1. **Where.** The memo lives in `fetch_url_executor`, not in `download_text`. `download_text` has
   no `ctx`, and the FRE-1641 harness calls it directly. The harness must see no memo.
2. **Session key.** `ctx.session_id`. The orchestrator and the sub-agent runner both build the
   `TraceContext` with `session_id`. If `session_id` is `None`, the memo is off for that call.
   Behavior is then the same as before this change.
3. **Retryable or not.** Use the `retryable` flag of the raised `FetchError`. The ticket's
   non-retryable codes (403/404 as `http_error`, `no_readable_text`, `blocked_by_domain_guard`,
   `private_target`) set `retryable=False`. The retryable classes (timeout, `connect_failed`,
   429, 5xx) set `retryable=True`. The flag also covers `unsupported_binary`, `pdf_unreadable`
   and `fetch_failed`, which are `False` and repeat with the same result.
4. **No lock for parallel calls.** Two parallel calls for one URL can both fetch. That is the
   behavior before this change. A memo update has no `await` between its read and its write, so
   no count is lost. A success that clears an entry is correct. A lock is out of scope.
5. **URL key.** The stripped URL string, exact match. The ticket names the same URL. Variants
   (`old.reddit.com`, `?openless=true`) stay out of scope.
6. **Refusal error.** A new `FetchError` with the stored code, `retryable=False`, the message
   `This URL already failed in this session: <stored message>`, and `next_action` that says not to
   call `fetch_url` for this URL again. The code stays in the declared `FetchCode` set.
7. **Success clears the entry.** A success after a retryable failure removes the memo entry.
8. **Bounds.** At most 256 sessions (least recently used leaves first) and 100 URLs per session.
   The memo is in process memory. A restart clears it.
9. **Order in the executor.** Validation, then the memo check, then the novelty check. A repeat is
   never novel, so skipping the novelty I/O loses no signal.
10. **Stored time.** Each entry keeps `failed_at` (UTC). The refusal log line carries it.

## Steps

1. **Failing tests first.** Create `tests/personal_agent/tools/test_fetch_failure_memo.py`.
   An autouse fixture clears `personal_agent.tools.fetch._failed_fetches`.
   - `test_second_call_after_404_makes_no_request` (AC-1): two calls, `client.get` call count is 1,
     the second error holds "already failed in this session" and "HTTP 404".
   - `test_timeout_gets_one_more_attempt_then_stored_error` (AC-2): call count is 2 after three
     calls, the third error holds "already failed in this session" and "timed out".
   - `test_different_session_fetches_again` (AC-3): session B reaches the network.
   - `test_different_url_same_session_is_not_blocked`.
   - `test_no_session_id_disables_memo`: three calls, call count is 3.
   - `test_no_readable_text_is_memoized`: an empty body page, second call makes no request.
   - `test_success_clears_a_retryable_failure`: timeout, then success, then a failing call reaches
     the network again.
   - `test_session_cap_evicts_the_oldest_session`: patch `_MEMO_MAX_SESSIONS` to 2, three sessions
     fail, the first session fetches again.
   - `test_url_cap_evicts_the_oldest_url_in_a_session`: same shape with `_MEMO_MAX_URLS`.
   - `test_each_retryable_class_gets_one_more_attempt`: connect failure, 429 and 503, each fetched
     twice, then refused.
   Command: `make test-file FILE=tests/personal_agent/tools/test_fetch_failure_memo.py`
   Expected: all fail (`_failed_fetches` missing).
2. **Implement** in `src/personal_agent/tools/fetch.py`: `_FailedFetch` frozen dataclass,
   `_failed_fetches` store, `_repeat_failure`, `_remember_failure`, `_forget_failure`, and the
   executor wiring (try/except `FetchError` around `download_text` and the empty-text check).
3. **Run** the new file, then `make test-file FILE=tests/personal_agent/tools/test_fetch.py`.
4. **Gates:** `make test` · `make mypy` · `make ruff-check` · `make ruff-format` ·
   `pre-commit run --all-files`.
5. **Commit**, then self-review on `git diff origin/main...HEAD`: `feature-dev:code-reviewer`
   and `security-review` (input handling, memory bound).
6. **PR and handoff comment** on FRE-1672.

## Diff class

Not escalated: no production write path, no deletion, no schema, no cost or governance code.
In-memory state only.

## Post-deploy (master)

A `seshat-gateway` rebuild. Verify live: in one session, call `fetch_url` on a URL that returns
404 twice. The second result must hold "already failed in this session" and no new
`fetch_url_http_error` log line.
