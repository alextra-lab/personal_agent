# FRE-1364 — ADR-0141 T1: egress guard on the litellm path (two-layer contract)

**Ticket:** FRE-1364 (Approved) · **ADR:** ADR-0141 D2 · **Umbrella:** FRE-1362
**Branch:** `fre-1364-egress-guard-two-layer`

## Scope

Chain head of ADR-0141's implementation chain (step 1 of 6). Local dispatch does **not**
move to litellm yet (that is T2/FRE-1365) — this ticket only builds the guard/transport seam
inside the existing `LiteLLMClient` (cloud path) so the mechanism and its seeded-negative
contract exist before any traffic is unified onto it.

Two layers, per ADR-0141 D2.1/D2.2:

1. **Layer 1 — pre-dispatch, route-independent, owns the exception type.** Runs the
   `DomainGuard` check on the resolved `api_base` before calling `litellm.acompletion()`,
   raising `EgressBlockedError` directly.
2. **Layer 2 — per-route injected hook, owns no-connection depth.** Two route mechanisms are
   in scope, both currently reachable via the cloud catalog:
   - **OpenAI-SDK route** (`provider="openai"`; local `openai/` joins this set in T2) — inject
     `client=AsyncOpenAI(http_client=create_guarded_http_client(...))` into `litellm.acompletion()`.
   - **AsyncHTTPHandler route** (`provider="anthropic"`, and — verified below —
     `provider="ovhcloud"`, which rides litellm's generic `base_llm_http_handler` on the same
     `AsyncHTTPHandler` transport, not a third mechanism) — inject
     `client=AsyncHTTPHandler(event_hooks={"request": [guard_hook]})`.

### Verified against litellm 1.98.0 (installed version matches ADR's citations)

- `main.py`'s top-level `acompletion()` forwards a `client` kwarg through `**kwargs` unchanged.
- `llms/openai/openai.py::_get_openai_client` — when `client` is not None, uses it **directly**
  (only `organization`/`max_retries` are set dynamically on it — **not** `api_key`/`base_url`),
  so our own `AsyncOpenAI(...)` construction must set `api_key=`/`base_url=` itself (same
  pattern as `memory/embeddings.py:543`).
- `llms/anthropic/chat/handler.py:414` and `llms/custom_httpx/llm_http_handler.py` (used by
  `ovhcloud`, confirmed via `main.py::_complete_ovhcloud` → `base_llm_http_handler.completion`)
  both do `client if client is not None and isinstance(client, AsyncHTTPHandler) else None` —
  a non-`AsyncHTTPHandler` object passed as `client=` is silently dropped, so Layer 2 for this
  route **must** pass an actual `AsyncHTTPHandler`, not a raw `httpx.AsyncClient`.
- `AsyncHTTPHandler.__init__(event_hooks=...)` bakes the hooks into its internally-built
  `httpx.AsyncClient` at construction (`create_client()`); no `api_key`/`base_url` plumbing is
  needed for this route — those still flow through litellm's normal `api_key`/`api_base` kwargs,
  untouched by the `client=` injection.
- `AsyncHTTPHandler.create_client()` hardcodes `follow_redirects=True` — AC-c's redirect-hop
  property holds "for free" on this route without any extra config on our side.

## Files

1. **`src/personal_agent/security.py`** — add two small functions, both reusing the existing
   `_guard_request_hook` body (single source of truth for the check-and-raise logic):
   - `check_egress_or_raise(url: str, *, guard: DomainGuard | None = None) -> None` — Layer 1.
     Calls `guard.note_staleness()` (never fetches, mirrors `_guard_request_hook` / FRE-1162)
     then `guard.check_url(url)`; raises `EgressBlockedError` built from a synthetic
     `httpx.Request("POST", url)` when refused.
   - `guard_event_hooks(*, guard: DomainGuard | None = None) -> dict[str, list[...]]` — returns
     `{"request": [functools.partial(_guard_request_hook, guard=guard or get_domain_guard())]}`,
     for httpx-compatible objects (like `AsyncHTTPHandler`) that take `event_hooks=` directly
     instead of being built via `create_guarded_http_client`.

2. **`src/personal_agent/llm_client/litellm_client.py`** — **authoritative final design**
   (supersedes the inline draft that originally lived here; see the round-1/round-2 sections
   below for why each piece is shaped this way):
   - `LiteLLMClient.__init__`: add keyword-only `egress_guard: DomainGuard | None = None`
     (test seam; `None` in production resolves the process singleton at call time, same
     pattern as `create_guarded_http_client`'s own `guard` parameter).
   - Module-level route classification + known default hosts + caches:
     ```python
     _OPENAI_SDK_ROUTE_PROVIDERS = frozenset({"openai"})
     _ASYNC_HTTP_HANDLER_ROUTE_PROVIDERS = frozenset({"anthropic", "ovhcloud"})
     _KNOWN_DEFAULT_HOSTS = {"anthropic": "https://api.anthropic.com", "openai": "https://api.openai.com"}

     # Layer-2 client caches (round-2 fix #2): cache only the piece litellm never
     # mutates in place. The OpenAI-SDK route caches the guarded httpx.AsyncClient
     # (the connection pool) and builds a fresh AsyncOpenAI wrapper per call, because
     # litellm mutates .max_retries/.organization on a passed-in AsyncOpenAI object on
     # every call (openai.py::_set_dynamic_params_on_client) — sharing that object
     # across concurrent calls races. AsyncHTTPHandler has no equivalent per-call
     # mutation (verified), so it caches whole, and is provider-independent (carries
     # no auth/base_url state) so anthropic and ovhcloud share one entry per guard.
     _guarded_httpx_clients: dict[tuple[str | None, int], httpx.AsyncClient] = {}
     _guarded_async_http_handlers: dict[int, AsyncHTTPHandler] = {}
     ```
   - In `respond()`, after `provider_def` is resolved and before the cost gate:
     - **Layer 1:** `check_egress_or_raise(provider_def.base_url or _KNOWN_DEFAULT_HOSTS.get(self.provider), guard=self._egress_guard)`.
       Runs unconditionally (a provider in neither map has nothing to check, which only happens
       for a provider Layer 2's fail-closed branch below would also refuse). Deliberately does
       **not** feed into `litellm_kwargs["api_base"]` (round-1 fix #1 — pinning it risks a
       byte-mismatch against litellm's own undocumented per-provider default URL and silently
       breaking real dispatch; Layer 2 still blocks the connection regardless of which host
       litellm actually resolves to, so AC-3's no-escape guarantee holds even on this one
       documented residual edge — see round-1 fix #1 for the full reasoning).
     - **Layer 2:** build `litellm_kwargs["client"]` from `self.provider`:
       - in `_OPENAI_SDK_ROUTE_PROVIDERS` →
         ```python
         cache_key = (provider_def.base_url, id(resolved_guard))
         pooled = _guarded_httpx_clients.setdefault(
             cache_key, create_guarded_http_client(guard=resolved_guard)
         )
         litellm_kwargs["client"] = AsyncOpenAI(
             api_key=api_key or "unused", base_url=provider_def.base_url, http_client=pooled
         )
         ```
       - in `_ASYNC_HTTP_HANDLER_ROUTE_PROVIDERS` →
         ```python
         litellm_kwargs["client"] = _guarded_async_http_handlers.setdefault(
             id(resolved_guard), AsyncHTTPHandler(event_hooks=guard_event_hooks(guard=resolved_guard))
         )
         ```
       - else → raise `LLMClientError` naming the unclassified provider and pointing at
         ADR-0141 D2 (fail closed rather than silently dispatch unguarded — matches the ADR's
         "that is a security regression and is not accepted" stance; a new provider must be
         classified into one of the two sets before it can dispatch).

3. **`src/personal_agent/config/settings.py`** — round-2 fix #1 (**not** `config_guard.py`; that
   was round-1's design and is superseded — see round-2 fix #1 for why a CI-only check of an
   env var CI never sets is the wrong enforcement point): add
   `enforce_experimental_litellm_handler_disabled(config: AppConfig) -> None`, following the
   `enforce_slm_endpoint_declared` pattern immediately above it in the file (unconditional
   `ValueError`, no severity split), called from `load_app_config()` alongside the other
   `enforce_*` calls.

4. **Tests — `tests/personal_agent/llm_client/test_litellm_client_egress_guard.py`** (new).
   Per AC-3's own bar ("fails if... the test stubs the layer the guard hangs on"), these tests
   do **not** mock `litellm.acompletion` — they let it run for real down to the transport. Test
   technique per round-1 fix #3 / round-2 fix #4: `check_url` spy as the primary assertion,
   route-appropriate transport patch as the secondary one (`httpx.AsyncHTTPTransport` for
   openai, `AsyncHTTPHandler._create_async_transport` → `httpx.MockTransport` for anthropic —
   **not** a single global patch, which is wrong for the AsyncHTTPHandler route since it
   defaults to an aiohttp transport). An `autouse` fixture clears + closes both module-level
   caches on teardown (round-2 fix #3). Unrelated side effects stubbed the same way
   `test_litellm_provider_auth.py` already does (cost gate, cost tracker, settings, catalog).

5. **`tests/personal_agent/config/test_settings_enforcement.py`** (or wherever
   `enforce_slm_endpoint_declared`'s own test already lives — colocate with it) — seeded-negative
   for `enforce_experimental_litellm_handler_disabled()`: set the env var truthy, assert
   `load_app_config()`/the function directly raises `ValueError`.

   For **each** of `provider="openai"` (OpenAI-SDK route) and `provider="anthropic"`
   (AsyncHTTPHandler route), using a real `DomainGuard(mode=GuardMode.BLOCKLIST, ...)` seeded
   with a test blocklisted hostname and `provider_def.base_url` pointed at that host, plus
   `guard.check_url = MagicMock(wraps=guard.check_url)` (primary assertion target) and the
   route-appropriate transport patch (secondary — `httpx.AsyncHTTPTransport.handle_async_request`
   for openai, `AsyncHTTPHandler._create_async_transport` → `httpx.MockTransport` for anthropic):
   - **AC-a(1):** guard attached normally → `respond()` raises `EgressBlockedError`;
     `check_url` asserted called with the blocklisted URL; transport sentinel asserted never
     reached (Layer 1 short-circuits before `litellm.acompletion` even runs).
   - **AC-a(2):** `monkeypatch.setattr(litellm_client, "check_egress_or_raise", lambda *a, **k: None)`
     (Layer 1 off) → `respond()` still raises (any exception — the route's own wrapper shape,
     per ADR D2.2); `check_url` still asserted called (from Layer 2's own hook this time, not
     Layer 1) with the blocklisted URL; transport sentinel still asserted never reached (proves
     Layer 2 alone blocks it, independent of Layer 1).
     - Seeded-negative proof each test can fail (ADR AC-a's own requirement): a variant/manual
       check removing that route's `client=` injection (e.g. temporarily classify the provider
       into neither set, or monkeypatch `guard_event_hooks`/the `AsyncOpenAI` construction to
       drop the guard) turns the `check_url`-called assertion red — done once by hand during
       implementation, recorded in the handoff, not committed as a test that intentionally fails.

   - **AC-b (one test per route mechanism):** guard attached, `BLOCKLIST` mode, **empty**
     blocklist, allowed host → transport sentinel returns a 200 OpenAI-shaped JSON body →
     `respond()` succeeds, returns a normal `LLMResponse`. `provider="openai"` covers the
     OpenAI-SDK route; `provider="ovhcloud"` covers the AsyncHTTPHandler route with the same
     OpenAI-shaped fixture (verified safe — `OVHCloudChatConfig` inherits
     `OpenAIGPTConfig`'s response parsing unmodified), so this doesn't need Anthropic's native
     response schema hand-built. Existing suite (provider_auth, gate_wiring, telemetry_parity,
     cancellation_refund, trailing_role_guard, cache_pricing, reasoning_effort — all mock
     `litellm.acompletion` directly and already cover anthropic/openai dispatch shapes) staying
     green is the parity evidence that normal (non-guard-focused) behaviour is unchanged.

   - **AC-c (AsyncHTTPHandler route — `anthropic` — where `follow_redirects=True` genuinely
     holds by litellm's own hardcoded default in `create_client()`, verified above):** **Layer 1
     disabled for this test only** (round-2 fix #4 — isolates the assertion to Layer 2's
     per-hop hook, and avoids Layer 1's own `check_url` call plus the base-vs-`/v1/messages`
     URL-text mismatch confounding it). The `MockTransport` handler returns a 307 redirect on
     the first request (to a second, blocklisted, host) and would return 200 on a second request
     — asserted never reached; `respond()` raises (from the guard firing on the redirected hop,
     not the first one — proving the hook re-fires per-hop, not just once at the top-level
     call); the **last two** entries of `guard.check_url.call_args_list`, compared by extracted
     hostname (not exact URL string), confirm the first hop's host was allowed and the second
     (redirected-to) host was what tripped the guard.

## Explicitly out of scope (later tickets in the chain)

- Local dispatch does not move to litellm here (T2/FRE-1365) — no `openai/{local_model}` route
  is exercised by production code yet, only the mechanism it will reuse.
- Concurrency re-homing (D3), `max_tokens` coherence (D5), canaries (D6), `LocalLLMClient`
  deletion (D1/AC-6) are all later tickets.

## Revision after codex plan-review round 1

Codex found three real defects in the round-1 design; all three are fixed below (design fixes,
not just findings), plus two smaller hardening items it raised. Verified independently against
the installed litellm 1.98.0 / openai SDK source before adopting each fix.

1. **Layer 1 ran zero checks for `anthropic`/`openai` (no declared `base_url`) — real gap.**
   Fix: add `_KNOWN_DEFAULT_HOSTS = {"anthropic": "https://api.anthropic.com", "openai": "https://api.openai.com"}`
   (host-only — `DomainGuard.check_url` only extracts the hostname, so this need not be
   litellm's byte-exact internal default URL, which for anthropic includes a `/v1/messages`
   suffix I will **not** try to reproduce). Layer 1 now always checks
   `provider_def.base_url or _KNOWN_DEFAULT_HOSTS.get(self.provider)`.
   **Deliberately not** threaded into `litellm_kwargs["api_base"]` — pinning that too would risk
   a byte-mismatch against litellm's own undocumented default and silently break real dispatch
   (verified: litellm's Anthropic default is the *full* URL
   `"https://api.anthropic.com/v1/messages"`, not a bare host+path split I can safely
   reconstruct). This leaves one **documented, accepted residual gap**: if an ambient env var
   (`OPENAI_BASE_URL`, `ANTHROPIC_API_BASE`/`ANTHROPIC_BASE_URL` — both verified to exist and
   both verified to lose to an explicit `api_base`/`base_url` kwarg, which we still don't always
   pass) redirects the *actual* dispatch host away from what Layer 1 checked, the caller gets
   the route's own wrapped exception rather than a clean `EgressBlockedError` — but **Layer 2
   still blocks the connection regardless of which host litellm resolves to**, since it wraps
   whatever request object the route actually builds. AC-3's guarantee (no escape) holds; only
   AC-1-style exception-type cleanliness has this one documented edge case. Not closing this
   further in T1 — doing so needs either byte-exact per-provider default URLs (fragile,
   litellm-version-coupled) or introspecting litellm's resolved URL before calling it (no clean
   seam for that today).
   Existing `test_anthropic_and_openai_dispatch_unchanged` (`test_litellm_provider_auth.py`,
   FRE-1155 AC5) is **unaffected** — `litellm_kwargs["api_base"]` behaviour is untouched.

2. **`EXPERIMENTAL_OPENAI_BASE_LLM_HTTP_HANDLER` env var can silently detach the OpenAI-SDK
   route's guard.** Verified: `litellm/main.py:2510` reads
   `get_secret_bool("EXPERIMENTAL_OPENAI_BASE_LLM_HTTP_HANDLER")`, and if true, OpenAI dispatch
   reroutes through `base_llm_http_handler.completion()`, which requires an `AsyncHTTPHandler`
   and silently drops any non-`AsyncHTTPHandler` `client=` kwarg — our injected `AsyncOpenAI`
   would vanish with no error. Not set anywhere in this repo today (grepped `src/`, `config/`,
   `.env.example` — no hits), but nothing stops a future env change from flipping it. Fix: add
   `check_experimental_litellm_handler_disabled()` to `config/config_guard.py`
   (`run_all_checks` registry, same pattern as the other checks there — e.g.
   `check_reasoning_declaration`), asserting the env var is not truthy; seeded-negative test
   sets it truthy and asserts the `Finding` fires. This is a config-declaration guard, not a
   wire-behaviour one — acceptable here because there is nothing to run it against (the
   mechanism is *rejected*, not implemented and verified — same posture as ADR-0141 D2.2's
   rejection of `litellm.aclient_session` for "partial coverage" reasons).

3. **The planned sentinel-transport test technique was unsound for the AsyncHTTPHandler
   route.** Verified: litellm's `AsyncHTTPHandler` defaults to an **aiohttp**-backed transport
   (`_should_use_aiohttp_transport()` returns `True` unless `litellm.disable_aiohttp_transport`
   or `DISABLE_AIOHTTP_TRANSPORT` env is set — neither is set here), producing
   `LiteLLMAiohttpTransport`, not `httpx.AsyncHTTPTransport`. Patching
   `httpx.AsyncHTTPTransport.handle_async_request` would never fire for this route, so a
   *broken* guard injection and a *working* one would both "pass" the old test design (the
   broken case would raise from a real failed network call in CI's sandboxed environment,
   accidentally satisfying "some exception was raised") — exactly the false-negative AC-a's own
   "each test proven able to fail" bar exists to catch. This is **not** litellm behaviour my
   change introduces — `get_async_httpx_client()` (litellm's own default path) already
   constructs `AsyncHTTPHandler` the same way today, so this is pre-existing, unrelated to this
   ticket, just newly relevant because the test needs to defeat it.
   Fix, two parts:
   - **Primary assertion, both routes:** wrap `guard.check_url` with
     `unittest.mock.MagicMock(wraps=guard.check_url)` and assert it was called with the expected
     target (or redirected-to) URL. This proves the guard mechanism actually engaged,
     independent of which transport class ends up underneath — the thing AC-a actually cares
     about.
   - **Secondary assertion (defense in depth, "sentinel transport"):** for the
     AsyncHTTPHandler route, patch the **staticmethod**
     `litellm.llms.custom_httpx.http_handler.AsyncHTTPHandler._create_async_transport` to return
     an `httpx.MockTransport(sentinel_handler)` — this bypasses the aiohttp/httpx branch
     entirely, deterministic regardless of whether the aiohttp extra is installed. For the
     OpenAI-SDK route, `create_guarded_http_client()` builds a **plain** `httpx.AsyncClient()`
     with no litellm involvement, so `httpx.AsyncHTTPTransport.handle_async_request` (the real
     default there) remains a valid patch target — confirmed by codex, only the
     `AsyncHTTPHandler` route was flagged.

4. **AC-b's success coverage only proved the OpenAI-SDK route; the AsyncHTTPHandler route's
   injection could break invisibly.** Fix: add a second real-dispatch success test using
   `provider="ovhcloud"` (an `_ASYNC_HTTP_HANDLER_ROUTE_PROVIDERS` member) — verified
   `OVHCloudChatConfig(OpenAIGPTConfig)` overrides neither `transform_response` nor
   `async_transform_request`, so it parses the **same OpenAI-shaped JSON body** as the
   OpenAI-SDK route's fixture, with no need to hand-construct Anthropic's native response
   schema. `anthropic` itself stays reserved for the seeded-negative (AC-a) and redirect (AC-c)
   tests, which never need to parse a success body.

5. **Guarded clients were being rebuilt from scratch on every `respond()` call** — pool churn,
   and (the sharper version of the same finding) a leaked pooled `AsyncHTTPHandler`/`AsyncOpenAI`
   on any call that raises before use (e.g. a budget-denied call after the client was already
   built — though after this fix, construction happens after the layer-1/budget checks so that
   specific case is avoided by ordering; caching is the fix for the general case, calls that do
   proceed). Fix: module-level caches, mirroring `memory/embeddings.py:531`'s existing
   `_openai_clients` pattern in this codebase (so this is a repeated idiom, not a new one):
   ```python
   _guarded_openai_clients: dict[tuple[str, str | None, str, int], AsyncOpenAI] = {}
   _guarded_async_http_handlers: dict[tuple[str, int], AsyncHTTPHandler] = {}
   ```
   Keyed **including `id(resolved_guard)`** — not just provider/endpoint/api_key — so a test
   constructing `LiteLLMClient(egress_guard=test_guard)` with a fresh `DomainGuard` per test
   never receives a stale cached client bound to a *different* test's guard (a real
   cross-test-pollution risk with a plain provider-keyed cache). In production the guard is the
   process singleton (`get_domain_guard()`, stable `id()` for the process lifetime), so the
   cache is fully effective there — this key shape costs nothing in the common case and buys
   test isolation. No explicit `aclose()`/shutdown wiring — matches the existing
   `embeddings.py` precedent exactly (relies on process exit); not introducing a new lifecycle
   contract this ticket doesn't need.

## Revision after codex plan-review round 2

Round 2 confirmed round-1 fixes #1 and #3 hold up, and found two more real defects (not just
documentation gaps) in round-1's own fixes, plus two smaller corrections. Verified independently.

1. **Round-1 fix #2 (`EXPERIMENTAL_OPENAI_BASE_LLM_HTTP_HANDLER` rejection) only ran in CI, not
   at boot — a runtime gap, not just a CI one.** Verified: `run_all_checks` is invoked only by
   `scripts/check_config.py` (pre-commit/CI); `load_app_config()` (`config/settings.py:3306`)
   calls three specific `enforce_*` functions directly and never calls `run_all_checks`. So a
   deployment env change could flip this flag after CI passed, with nothing at boot to catch it
   — and this is exactly the class of gap ADR-0141 exists to close (declaration checked, runtime
   not). Fix, following the **existing** `enforce_slm_endpoint_declared` pattern in the same file
   (an env/deployment-truth check, not a repo-file scan — so it doesn't belong in
   `config_guard.run_all_checks` at all; a CI-only check of an env var CI never sets would be
   vacuously green and add false confidence). Dropping the `config_guard.py` addition entirely
   in favour of:
   ```python
   def enforce_experimental_litellm_handler_disabled(config: AppConfig) -> None:
       """Refuse to boot if EXPERIMENTAL_OPENAI_BASE_LLM_HTTP_HANDLER is set (ADR-0141 D2.2).

       That litellm flag reroutes OpenAI-SDK-route dispatch through the generic
       AsyncHTTPHandler-based handler, which silently drops a non-AsyncHTTPHandler
       `client=` kwarg — our injected guarded AsyncOpenAI would vanish with no error.
       """
   ```
   in `config/settings.py`, called unconditionally from `load_app_config()` alongside
   `enforce_slm_endpoint_declared(config)` — no severity split needed (this is a boolean
   env-var presence check, not a graded repo-content scan).

2. **Round-1 fix #5's cache races on a value litellm mutates in place.** Verified:
   `llms/openai/openai.py:399`'s `_set_dynamic_params_on_client` sets `client.max_retries` (and
   `.organization`) **in place** on whatever `AsyncOpenAI` object we pass as `client=`, on
   *every* call — and the SDK reads that mutable attribute mid-request
   (`openai/_base_client.py`). Two concurrent `respond()` calls sharing one cached `AsyncOpenAI`
   object with different `max_retries` values (a real scenario once D3 re-homes concurrency to
   allow up to 50 concurrent chat-provider calls) can stomp each other's setting between the
   mutation and the read. Fix: cache only the **guarded `httpx.AsyncClient`** (the actual
   connection pool — the expensive part to rebuild) at the OpenAI-SDK route, keyed
   `(base_url, id(guard))`, and construct a **fresh** `AsyncOpenAI(http_client=cached_httpx_client,
   api_key=..., base_url=...)` wrapper on every call — each call gets its own mutable
   `.max_retries`/`.organization`, no shared state to race on, while the pool itself is still
   reused. Verified there is no equivalent per-call mutation on a passed-in `AsyncHTTPHandler`
   (grepped `llms/anthropic/chat/handler.py` and `llms/custom_httpx/llm_http_handler.py` for any
   `client.<attr> =` — none), so the AsyncHTTPHandler-route cache stays whole-object, keyed
   `(id(guard),)` (provider-independent — it carries no auth/base_url state, so `anthropic` and
   `ovhcloud` can share one cached handler per guard).

3. **Test cache lifecycle:** the guard-ID-keyed caches prevent *stale* reuse across tests but not
   *unbounded growth* — every fresh per-test `DomainGuard` leaves a permanent, unclosed cache
   entry (`AsyncHTTPHandler` owns a real `httpx.AsyncClient` with a `close()`). Fix: an
   `autouse` fixture in the new test module that, on teardown, closes every cached client
   (`await handler.close()` / `await client.aclose()`) and clears both cache dicts.

4. **AC-c's exact-URL / exact-call-count assertion was fragile — three compounding issues:**
   Layer 1 (a separate `check_url` call, on `provider_def.base_url` verbatim) runs *before*
   Layer 2's per-redirect-hop hook calls, so the total isn't "2 in order," and Anthropic's
   handler appends `/v1/messages` to a bare base — the URL Layer 2's hook actually sees on the
   first hop differs textually from what Layer 1 checked. Fix: **disable Layer 1 for this one
   test** (monkeypatch `check_egress_or_raise` to a no-op, same technique as AC-a(2)) so only
   Layer 2's per-hop hook calls remain to reason about, and assert on the **last two** entries of
   `guard.check_url.call_args_list` by extracted hostname (matching `DomainGuard`'s own
   `_extract_hostname`), not exact URL string equality or an exact total call count.

5. **AC mapping table only named the OpenAI AC-b test, not the OVHCloud one** — the "## Files"
   section's test spec already lists both (round-1 fix #4); this is a doc-consistency fix in the
   closing "Acceptance criteria mapping" section below: AC-b maps to both the openai-route and
   ovhcloud-route success tests.

## Test commands

```bash
make test-file FILE=tests/personal_agent/llm_client/test_litellm_client_egress_guard.py
make test                  # full suite — confirms no regression in the existing LiteLLMClient tests
make mypy
make ruff-check
make ruff-format
pre-commit run --all-files
```

## Acceptance criteria mapping (this ticket's own, from the Linear description)

- **AC-a** → the four seeded-negative tests above (2 routes × {layer1-on, layer1-off}).
- **AC-b** → the openai-route **and** ovhcloud-route success tests + existing suite staying
  green.
- **AC-c** → the AsyncHTTPHandler-route (`anthropic`) redirect test.
