# FRE-1746 — Search past conversations in the PWA

Ticket: FRE-1746 (Approved, `stream:build2`). Related: FRE-565 (canceled). Tier: Standard (touches
`src/` logic and a per-user data boundary), so codex plan-review is required.

## Scope

The PWA drawer lists `GET /api/v1/sessions?limit=20`. That call returns the 20 newest sessions. An
older conversation cannot be found. This change adds a search box. It matches the text of the
owner's own messages and returns sessions of any age, newest first.

## Store decision (AC-4)

Postgres answers the search. Reasons:

- The message text lives in `sessions.messages` (JSONB). Postgres is the only store that holds the
  full text of a conversation. Elasticsearch holds telemetry logs, not the conversation.
- `sessions.user_id` is in the same row. The scope filter and the text match run in one statement.
  A cross-user leak is not possible by construction.
- The data is small: 1519 session rows, 3.2 MB of `messages` in total (read on 2026-11-07). A scan
  per request is cheap. No index, no schema change, no migration, no new store, no paid call.
- The session "title" in the list is the first 60 characters of the first user message
  (`_extract_title`). It lives in `messages`, so the text match covers it.
- Limit: the retention sweep clears `messages` after 180 days of inactivity (`purged_at`).
  A purged session has no text in any store. Search cannot match it. Report this at handoff.
- Limit: `session_label` (model-generated, Neo4j) is not searched. Searching it needs a second store.

## Design

Repository: `SessionRepository.search(query, limit, user_id)` in
`src/personal_agent/service/repositories/session_repository.py`.

- `user_id` is a required argument. No unscoped search exists.
- The query is escaped for `LIKE` and wrapped in `%…%`. It is a bind parameter. The escape
  character is `!` (codex review: a backslash literal depends on `standard_conforming_strings`).
  `!` is escaped first, then `%` and `_`.
- SQL: `SELECT … WHERE user_id = :uid AND EXISTS (jsonb_array_elements(messages) …)` with
  `ILIKE … ESCAPE '!'`, `ORDER BY last_active_at DESC LIMIT :limit`.
- It matches only `user` and `assistant` messages. It reads the `content` text. For list content it
  reads only `text` blocks whose `text` is a JSON string (same rule as `get_text_content`). It never
  matches JSON keys or metadata.
- Each `jsonb_array_elements` input (`messages` and `content`) is behind a
  `CASE jsonb_typeof(…) WHEN 'array'` guard, and each element is checked with
  `jsonb_typeof(…) = 'object'`. Odd JSON shapes are skipped without an error (codex review).

Gateway: `list_sessions` in `src/personal_agent/gateway/session_api.py` gets
`q: str | None = Query(default=None, max_length=200)`.

- `q` stripped and non-empty → `repo.search(term, limit, user_id=user_id)`.
- `q` missing, empty or blank → `repo.list_recent(limit, user_id=user_id)` (AC-3, unchanged path).
- The log line records `query_length`, never the query text.
- The `service/app.py` `GET /sessions` route stays unchanged. The PWA does not call it.

PWA:

- `seshat-pwa/src/lib/agui-client.ts`: `listSessions(limit = 20, query?: string)`. It appends
  `&q=<encoded>` only for a non-blank query.
- `seshat-pwa/src/components/SessionList.tsx`: a search input above the list. Empty input →
  `listSessions(20)` (the current behaviour, immediate). Non-empty input → `listSessions(50, term)`
  after a 300 ms debounce. A cancel flag drops stale responses. The input stays mounted while
  results load. A no-match state reads "No conversations match “…”.".

## Steps and tests (TDD: write the test, see it fail, then implement)

1. Repository test, real Postgres on :5433 (marker `integration`, pattern of
   `test_fre1733_session_writes_db.py`):
   `tests/personal_agent/service/test_fre1746_session_search_db.py`
   Command: `uv run python -m pytest tests/personal_agent/service/test_fre1746_session_search_db.py -m integration -q`
   Cases:
   - matches user text and assistant text, case-insensitive;
   - AC-1: a session 400 days old matches, and results are newest first;
   - AC-2: a second user's session with the same word never appears;
   - `%` and `_` match literally (`100%` does not match everything);
   - no match on JSON keys or metadata (query `model_role` returns nothing);
   - list content: a `text` block matches, an `image_url` block does not;
   - a purged session (`messages = '[]'`) never matches;
   - `limit` is respected;
   - text that appears only in a `tool` or `system` message does not match;
   - odd JSON shapes (scalar elements, object or null content, a non-string `text` value, a
     non-array `messages`) are skipped without an error;
   - a term with `!`, `\`, `%` and `_` matches only itself, and near misses do not match.
2. Implement `SessionRepository.search`.
3. API unit tests (mocked repository) in `tests/personal_agent/gateway/test_session_api.py`:
   - `?q=foo` calls `search` with `user_id`, not `list_recent`;
   - the term is stripped before the call;
   - `?q=` and `?q=%20%20` call `list_recent` (AC-3);
   - `q` over 200 characters returns 422;
   - the response shape equals the list shape.
   Command: `make test-file FILE=tests/personal_agent/gateway/test_session_api.py`
4. Implement the gateway change.
5. PWA tests in `seshat-pwa/src/__tests__/SessionList.test.tsx` and a new
   `seshat-pwa/src/__tests__/list-sessions-search.test.ts`:
   - typing calls `listSessions(50, "term")` after the debounce;
   - clearing the input calls `listSessions(20)` again and shows the recent list (AC-3);
   - no-match message renders;
   - the search box stays mounted and focused while results load;
   - a stale answer, a stale failure and a late recent list do not change state (codex review);
   - clearing the box calls `listSessions(20)` with no timer advance;
   - `listSessions` builds `?limit=50&q=a%20b%26c` and omits `q` when blank.
   Command: `cd seshat-pwa && npx vitest run src/__tests__/SessionList.test.tsx src/__tests__/list-sessions-search.test.ts`
6. Implement the PWA change.
7. Gates: `make test` · `make mypy` · `make ruff-check` · `make ruff-format` ·
   `pre-commit run --all-files` · `cd seshat-pwa && npm run lint && npx vitest run`.
8. Commit, self-review (`feature-dev:code-reviewer`, `security-review`), fix, rebase, PR, handoff.

## Acceptance criteria and proof

| AC | Proof |
|----|-------|
| AC-1 | DB test: 400-day-old match, newest first, text and title text. PWA test: search box calls the API. |
| AC-2 | DB test: other user's session never appears. API test: `user_id` threaded to `search`. |
| AC-3 | API test: blank `q` → `list_recent`. PWA test: cleared input restores the recent list. |
| AC-4 | This document and the PR body: Postgres, with the reasons above. No new store, no paid call. |
| AC-5 | The tests above, and the gates as CI runs them. Master verifies live. |

## Out of scope

Highlighting the match, ranking by relevance, searching `session_label`, a full-text index, and the
`service/app.py` `GET /sessions` route.
