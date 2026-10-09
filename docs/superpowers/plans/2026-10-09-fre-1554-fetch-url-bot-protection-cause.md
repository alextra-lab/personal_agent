# FRE-1554 — fetch_url names the cause of a bot-protection 403, and sends an honest UA

**Ticket:** FRE-1554 (Approved, High, Tier-2). **Class:** Standard (changes `src/` logic in a network tool). **Related:** FRE-1552 (same tool, domain guard).

## Scope

1. On a 403 that carries a bot-protection signal, the error text for the model names the protection and says that a retry will not help.
2. The `fetch_url_http_error` log event carries the protection headers that were present.
3. The tool sends `Seshat-User/0.1 (personal research assistant; user-initiated)` as its User-Agent.

Out of scope: a browser fetch path, a proxy, robots.txt, remembering blocked URLs, the curl examples in `docs/skills/fetch-url.md` (they are the bash fallback, not this tool).

## Design

All changes are in `src/personal_agent/tools/fetch.py`.

- Constant `_USER_AGENT`, used in the `create_guarded_http_client(headers=...)` call.
- Constant `_PROTECTION_HEADERS = ("server", "cf-mitigated", "x-datadome", "cf-ray")`.
- Function `_protection_headers(headers) -> dict[str, str]`: returns the headers from `_PROTECTION_HEADERS` that are present. Works on `httpx.Headers` (case-insensitive). A repeated header logs as its comma-joined value.
- Function `_header_tokens(headers, name) -> frozenset[str]`: lower-case, comma-split tokens of every value from `headers.get_list(name)`. Codex review: `Headers.get()` joins repeated values (`"nginx, AkamaiGHost"`), so a rule must match a token, never the whole string and never a prefix.
- Function `_bot_protection_message(status, headers) -> str | None`: returns the cause sentence for status 403 only, else `None`. Rules, first match wins:
  - token `challenge` in `cf-mitigated` → `Cloudflare challenge`
  - `x-datadome` present → `DataDome`
  - token `akamaighost` in `server` → `Akamai`
  - `server: cloudflare` or `cf-ray` present, with no `cf-mitigated` → hedged text: the site is behind Cloudflare and may use its bot protection. This case does not claim a protection, because an origin 403 behind Cloudflare carries the same headers.
- Text for a firm match: `HTTP 403 fetching <url>: blocked by the site's bot protection (<name>). Retrying will not help.`
- A 403 with none of these headers, and every non-403 error, keeps the text `HTTP <status> fetching <url>`.
- The log call adds `response_headers=_protection_headers(resp.headers)` to `fetch_url_http_error`. It logs for every error status.

Why 403 only: Akamai and Cloudflare put `server` on 5xx responses too. A "retry will not help" claim on a temporary 503 would be false.

## Steps (TDD)

Test file: `tests/personal_agent/tools/test_fetch.py`. New tests use a real `httpx.Response` (case-insensitive headers) through the existing `_mock_client` helper.

1. Write failing tests:
   - `test_403_firm_protection_names_it_and_says_retry_will_not_help` (AC-1), parametrized over three cases. Each asserts BOTH the provider name and `Retrying will not help`:
     - `cf-mitigated: challenge` → `Cloudflare challenge`
     - `server: AkamaiGHost` → `Akamai`
     - `x-datadome: protected` → `DataDome`
   - `test_403_repeated_headers_still_match` (AC-1): two `cf-mitigated: challenge` fields, and `server: nginx` plus `server: AkamaiGHost`. Each still names the protection.
   - `test_403_server_token_must_match_exactly` (AC-1, seeded negative): `server: AkamaiGHostile` claims no protection.
   - `test_403_without_protection_headers_claims_no_protection` (AC-1, seeded negative): `server: nginx`. Expect the exact plain text `HTTP 403 fetching <url>`.
   - `test_403_behind_cloudflare_without_challenge_is_hedged` (AC-1): `server: cloudflare`, `cf-ray`. Expect `may use`, and no `Retrying will not help`.
   - `test_503_from_akamai_keeps_plain_message`: status 503, `server: AkamaiGHost`. Expect the exact plain text `HTTP 503 fetching <url>`.
   - `test_http_error_log_carries_protection_headers` (AC-2): patch `fetch.log`. Expect the `fetch_url_http_error` call has `response_headers` equal to the present protection headers, and no unrelated header (`set-cookie`).
   - `test_http_error_log_with_no_protection_headers_logs_empty_dict` (AC-2, negative).
   - `test_user_agent_is_honest_and_not_browser_shaped` (AC-3): real `create_guarded_http_client`, patched `getaddrinfo` and `handle_async_request`, capture `request.headers["user-agent"]`. Expect the captured header to equal the full string `Seshat-User/0.1 (personal research assistant; user-initiated)`. Also assert `bot` absent (case-insensitive) and `Mozilla` absent.
2. Run `make test-file FILE=tests/personal_agent/tools/test_fetch.py`. Expect the new tests to fail.
3. Implement the constants, both functions, and the two call-site edits in `fetch.py`.
4. Run the same command. Expect all tests pass, including the older `test_http_error_raises` (404, `match="HTTP 404"`).
5. Gates: `make test` · `make mypy` · `make ruff-check` · `make ruff-format` · `pre-commit run --all-files`.
6. Commit. Self-review with `feature-dev:code-reviewer` on `git diff origin/main...HEAD`. Run `security-review` (the diff touches network code and logging of response headers).
7. Rebase on `origin/main`, re-run the gates, push, open the PR, post the handoff comment on FRE-1554.

## Acceptance criteria and proof

| AC | Proof |
|----|-------|
| AC-1 | The five 403 tests above, plus the 503 test |
| AC-2 | The two log tests |
| AC-3 | `test_user_agent_is_honest_and_not_browser_shaped` |
| AC-4 | Live, master, after deploy (not in this PR) |

## Risks

- Header values in the log: `server`, `cf-mitigated`, `x-datadome`, `cf-ray` hold no secret and no PII. The log keeps an allowlist, never the full header set.
- A log field name that is new to ES: the main index template has `dynamic: true`, so the field is accepted.
- Diff class: no production write path, no deletion, no schema change, no cost or governance code. Self-serve.
