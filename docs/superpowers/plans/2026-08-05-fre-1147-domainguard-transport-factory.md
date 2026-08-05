# FRE-1147 — Wire DomainGuard behind an outbound transport factory; static bypass rule set

ADR-0132 D2. Backing ADR: `docs/architecture_decisions/ADR-0132-outbound-authenticated-egress.md`.
Seam ticket for the ADR's own AC-5 is FRE-1148 (not this ticket) — this plan proves only FRE-1147's
own AC-a/b/c below.

## Ticket's own acceptance criteria (from FRE-1147, this is what "done" means here)

- **AC-a**: integration test parameterized over the enumerated seams drives each seam's real
  production wiring with allowlist mode active — disallowed domain refused before any connection;
  allowlisted domain proceeds.
- **AC-b**: ast-grep rule set runs in repo checks, finds zero enumerated bypass forms outside the
  factory; a seeded violation on a scratch branch makes it fail.
- **AC-c**: guard `off` mode (today's default) — no behavior change, no new refusals, no latency
  regression in unit tests.

## Measured starting state (post-FRE-1144 cutover, confirmed against current `origin/main`)

`DomainGuard.check_url` (`src/personal_agent/security.py:114`) has zero production callers. Every
one of the following constructs its own `httpx.AsyncClient`/SDK client inline, with no shared
factory anywhere in the repo:

| # | Seam (ADR's enumerated list) | File:line | Construction today |
|---|---|---|---|
| 1 | LLM client | `llm_client/client.py:453` | `httpx.AsyncClient(timeout=timeout_config, verify=verify_ssl)` |
| 2 | SLM health — probe (scheduler_runner + provider_health call through this) | `observability/slm_health/probe.py:65` | `httpx.AsyncClient(timeout=timeout_s)` |
| 3 | Embeddings | `memory/embeddings.py:488` (httpx fallback), `:543` (`openai.AsyncOpenAI(api_key=api_key, base_url=endpoint)`, cached in `_openai_clients`) | both |
| 4 | Reranker | `memory/reranker.py:212` | `httpx.AsyncClient(timeout=timeout)` |
| 5 | Artifact export | `service/artifacts_router.py:384` | `httpx.AsyncClient(timeout=self._timeout, follow_redirects=False)` |
| 6 | Envelope probe | `observability/artifact_envelope/probe.py:71` | `httpx.AsyncClient(timeout=timeout, follow_redirects=False)` |
| 7 | Web/search tools | `tools/web.py:227`, `tools/perplexity.py:127`, `tools/context7.py:110`, `tools/linear.py:222` | all `httpx.AsyncClient(...)` |

No existing ast-grep harness exists in this repo (no `sgconfig.yml`, no CI/pre-commit reference) —
this ticket bootstraps it from scratch. `tests/test_security/test_domain_guard.py` is pure unit
coverage of `DomainGuard` in isolation; no seam/integration test exists today.

## Codex round 1 — verdict and plan revisions

Codex reviewed this plan pre-implementation. Verdicts, in order:

1. **Scope: sweep all of `src/`, exclude `ui/` — confirmed.** ADR-0132 D2's own words settle it:
   "forbids the known bypass forms **outside that factory**" and AC-5 "fails if... the static scan
   finds any enumerated bypass form" — neither is qualified to the 7 seams. Correction to my own
   count: **4** additional sites, not 3 (I undercounted) — `gateway/chat_api.py`, `gateway/client.py`,
   `captains_log/linear_client.py`, `service/cf_access_jwt.py`. `ui/` exclusion confirmed sound
   (`service_cli.py` is genuinely sync `httpx.Client`; `service_client.py` is async and technically
   could adopt the factory, but both stay excluded together as one CLI-to-own-backend concern, not an
   ADR-enumerated egress seam).
2. **Transport-subclass design: replaced.** Verified independently (httpx 0.28.1 source,
   `_client.py`): `AsyncClient._send_handling_redirects` invokes `self._event_hooks["request"]` hooks
   in a `while True` loop **before** calling `self._transport_for_url(request.url)` — i.e. hooks fire
   ahead of transport/mount/proxy selection, and re-fire on every redirect. A custom
   `AsyncHTTPTransport` subclass only guards the *default* transport — an explicit `mounts=` or
   `proxy=` on the same client would silently route around it. **Design changes to an async
   request-hook** installed by the factory (see below) — this also eliminates the entire
   verify/cert/http2/trust_env/proxy/mounts kwarg-splitting problem from point 3, since
   `**client_kwargs` now pass straight through to `httpx.AsyncClient` unmodified.
3. **`EgressBlockedError` also needs to survive SDK-level retry wrapping.** OpenAI/Anthropic SDK
   clients catch broadly, retry internally (default `max_retries=2`), then raise their own
   `APIConnectionError`/etc. with our error only as `__cause__`. Not fixed by changing `max_retries`
   (that would also blunt retry behavior for genuine transient errors, an unrelated behavior change
   this ticket doesn't need) — instead, AC-a's test for the two SDK-backed seams (embeddings' OpenAI
   client) asserts on the wrapped exception's `__cause__` chain, not a bare `EgressBlockedError`. The
   guarantee "refused before any connection is attempted" still holds on every one of the SDK's retry
   attempts — the hook fires ahead of transport dispatch each time, so no TCP connection ever forms.
4. **`DomainGuard.ensure_loaded()` is unconditional — a real AC-c latency bug if called blindly.**
   `check_url` short-circuits for `GuardMode.OFF` without touching `_blocklist`/`_allowlist`, but
   `ensure_loaded()` doesn't know that — it always runs the disk-cache/URLhaus-feed dance on a stale
   guard regardless of mode. Calling `await guard.ensure_loaded()` unconditionally from the hook would
   make the *first* request in a fresh OFF-mode process pay a real (up to 15s-timeout) network fetch
   for a blocklist that mode never consults — exactly the "latency regression" AC-c forbids. **Fix:**
   add a small `DomainGuard.mode` read-only property; the hook skips `ensure_loaded()` entirely when
   `guard.mode is GuardMode.OFF`.
5. **`AC-c`'s "off mode (current default)" is imprecise against measured settings.py.**
   `url_guard_mode` defaults to `"blocklist"`, not `"off"` (`settings.py:2160-2162`) — the *effective*
   current behavior is "as-if off" only because zero seams call `check_url` today, not because the
   configured mode is off. Test plan covers both readings so neither is silently assumed: an explicit
   `GuardMode.OFF` unit-test path proves wiring is a true no-op (the strict reading), and a
   `GuardMode.BLOCKLIST` path (the actual configured default) proves none of the 7 seams' real target
   hosts newly refuse (the practical reading) — both documented explicitly rather than picking one
   silently.
6. **`LiteLLMClient`/`litellm.acompletion()` (the cloud-provider path) is out of scope for this
   ticket.** Flagged by codex as a real gap — litellm builds its own internal httpx/SDK clients per
   call, invisible to both the factory and the ast-grep rule (the construction happens inside the
   third-party `litellm` package, not `src/`). Resolved by checking the ADR's own measured inventory
   (lines 42-49): it enumerates exactly `llm_client/client.py`, `slm_health/scheduler_runner.py`,
   `slm_health/probe.py`, `llm_client/provider_health.py` as the "LLM client" seam — the SLM/local
   path — and never mentions `litellm_client.py` anywhere. "LLM client" in the D2 obligation is the
   local/SLM client, not the cloud LiteLLM path. This is a genuine gap in guard coverage for cloud
   provider calls, filed as a `Backlog` ticket (one-line body, ADR-0131 D1) rather than silently
   expanding this ticket's scope further — patching litellm's global session handling is a materially
   different, riskier change (affects every cloud model call, not just egress-guarding).
7. **Per-seam exception handling — narrower catches need widening, verified not assumed.** Several
   seams (`tools/linear.py`, `tools/web.py`, `captains_log/linear_client.py`,
   `observability/slm_health/probe.py`) catch only `httpx.ConnectError`/`httpx.TimeoutException`
   specifically — `EgressBlockedError(httpx.RequestError)` (broader) will NOT be caught by those and
   propagates uncaught unless each site adds an explicit `except EgressBlockedError` (preferred over
   widening to a blanket `except httpx.RequestError`, for a clearer message and to avoid masking other
   request-error subtypes under an existing generic handler). Verified per-seam by the AC-a test, not
   pre-guessed for all ~13 sites in this plan.

Revised factory design (replaces the Transport-subclass sketch below):

```python
class EgressBlockedError(httpx.RequestError):
    """Raised by the DomainGuard request hook before a request is ever sent.

    Subclasses httpx.RequestError (not TransportError — it never reaches a
    transport) so it is still caught by any existing `except httpx.RequestError`
    / `except httpx.HTTPError` handler; narrower per-seam catches (ConnectError,
    TimeoutException) do NOT match it and need an explicit except clause.
    """
    def __init__(self, request: httpx.Request, reason: str, matched_entry: str | None) -> None:
        super().__init__(f"egress blocked by DomainGuard: {request.url} ({reason})", request=request)
        self.reason = reason
        self.matched_entry = matched_entry


async def _guard_request_hook(request: httpx.Request, *, guard: DomainGuard) -> None:
    if guard.mode is not GuardMode.OFF:
        await guard.ensure_loaded()
    result = guard.check_url(str(request.url))
    if not result.allowed:
        log.warning("egress_blocked", url=str(request.url), reason=result.reason, matched_entry=result.matched_entry)
        raise EgressBlockedError(request, result.reason, result.matched_entry)


def create_guarded_http_client(*, guard: DomainGuard | None = None, **client_kwargs: Any) -> httpx.AsyncClient:
    """The one outbound transport factory obliged by ADR-0132 D2.

    Every httpx.AsyncClient keyword continues to work unchanged (verify, cert,
    timeout, follow_redirects, proxy, mounts, ...) — this only installs the
    DomainGuard check as the first request-hook, ahead of any caller-supplied
    hooks, so it fires before transport/mount/proxy selection and on every
    redirect (verified against httpx 0.28.1: AsyncClient._send_handling_redirects
    runs request hooks strictly before _transport_for_url).
    """
    resolved_guard = guard or get_domain_guard()
    existing_hooks = dict(client_kwargs.pop("event_hooks", None) or {})
    hooks = [functools.partial(_guard_request_hook, guard=resolved_guard), *existing_hooks.get("request", [])]
    return httpx.AsyncClient(event_hooks={**existing_hooks, "request": hooks}, **client_kwargs)
```

`DomainGuard` gets one small addition: a `mode` read-only property (`@property def mode(self) -> GuardMode: return self._mode`), needed by point 4 above.

For the two SDK-client seams, `http_client=create_guarded_http_client()` is passed to
`openai.AsyncOpenAI(...)` / `anthropic.AsyncAnthropic(...)` — both accept `http_client:
httpx.AsyncClient | None` (confirmed via signature introspection on the installed versions).

## Scope (confirmed by codex round 1, see above) — the 11 migration sites

**7 enumerated seams** (table above) + **4 additional sites** swept in because AC-b's static check is
unqualified ("outside that factory", not "outside the factory, within the 7 seams"):
`gateway/chat_api.py:94` (`anthropic.AsyncAnthropic`), `gateway/client.py:71` (`httpx.AsyncClient`),
`captains_log/linear_client.py:197` (`httpx.AsyncClient`), `service/cf_access_jwt.py:141`
(`httpx.AsyncClient`, CF JWKS fetch for inbound JWT verification — still an outbound call).

**Excluded**: `ui/service_cli.py` (sync `httpx.Client`) and `ui/service_client.py` (async, but kept
excluded alongside it as one CLI-to-own-backend concern) — CLI-to-`:9000` traffic, not an
ADR-enumerated egress seam; a sync client would also need `DomainGuard.ensure_loaded()`'s async
refresh bootstrapped some other way. Noted as a candidate follow-up, not pulled into this ticket.

**Two explicit exemptions** in the ast-grep rule (commented inline): the factory module itself, and
`security.py`'s own URLhaus feed fetch (the guard cannot gate its own bootstrap without recursion).

**Out of scope, filed as a `Backlog` follow-up**: `llm_client/litellm_client.py` (the cloud-provider
path) — see codex point 6 above.

## Design

### 1. New: guarded-client factory + hook, in `security.py` (co-located with `DomainGuard` — see the
   revised design above for the actual code)

For the two SDK clients (`openai.AsyncOpenAI`, `anthropic.AsyncAnthropic`), both accept
`http_client: httpx.AsyncClient | None` (confirmed via signature introspection on the installed
versions) — pass `http_client=create_guarded_http_client(...)` instead of letting the SDK build its
own transport. This is the literal mechanism behind the ADR's "SDK clients... receive their
transport/base config from the factory."

### 2. Seam-by-seam adoption (drop-in swap, same kwargs, at each of the 11 sites above)

Each is `httpx.AsyncClient(...)` → `create_guarded_http_client(...)` with identical kwargs, or (for
the two SDK sites) `http_client=create_guarded_http_client()` added to the constructor call.
**Exception-handling changes are expected, not incidental** (corrected from round 1 — see codex point
7): `tools/linear.py`, `tools/web.py`, `captains_log/linear_client.py`,
`observability/slm_health/probe.py` (and any other site found to catch only
`httpx.ConnectError`/`httpx.TimeoutException`) get an explicit `except EgressBlockedError` clause
added alongside their existing narrow catches — verified per-seam by the AC-a integration test, fixed
as a fold-in (Step 5), not deferred.

### 3. ast-grep rule set — new `.ast-grep/` config, wired into a pre-commit hook + CI

Rules (Python, `src/personal_agent/` scope, excluding the factory module + `security.py`'s own feed
fetch — commented inline):
- `httpx.Client($$$)` / `httpx.AsyncClient($$$)` construction
- module-level `httpx.get/post/put/patch/delete/request($$$)` calls
- `openai.OpenAI($$$)` / `openai.AsyncOpenAI($$$)` construction
- `anthropic.Anthropic($$$)` / `anthropic.AsyncAnthropic($$$)` construction

Wired as a new `repo: local` hook in `.pre-commit-config.yaml` (matching the existing hand-rolled
Python-AST-checker pattern already there) and a new CI job step. AC-b's "seeded violation on a
scratch branch" is a test fixture: a throwaway file with a bare `httpx.AsyncClient()` call that the
rule must flag — asserted in a unit test that shells out to `ast-grep` against a fixture directory,
not a manual one-off check.

### 4. Tests (TDD — failing first)

- `tests/test_security/test_transport_factory.py` (new): unit tests for `create_guarded_http_client`
  — allowlist mode raises `EgressBlockedError` for a disallowed host with a `MockTransport` beneath
  the client asserting it is never invoked (proves pre-connection refusal); an allowed host reaches
  the mock transport; `OFF` mode never calls `ensure_loaded()` (spy/mock the guard, assert not
  awaited) — the direct AC-c proof; `BLOCKLIST` mode (the actual settings.py default) with a
  pre-loaded empty-match guard passes every one of the 11 sites' real target hosts through unchanged
  — the practical AC-c proof. `verify=`/other kwargs passthrough covered since the design no longer
  splits them (nothing to regress).
- `tests/test_security/test_egress_seams.py` (new): the AC-a integration test, parameterized over
  the 11 sites' real production wiring, `DomainGuard(mode=ALLOWLIST, allowlist={"allowed.example"})`
  injected. Disallowed-domain call refuses before any connection (`MockTransport` beneath, asserted
  unreached, or for the 2 SDK seams: asserting on the wrapped SDK exception's `__cause__` chain per
  codex point 3); allowlisted domain reaches the mock transport boundary.
- `tests/test_security/test_bypass_rules.py` (new): AC-b — runs the ast-grep rule set against (a) the
  current `src/` tree (expect zero matches, after all 11 sites are migrated) and (b) a
  seeded-violation fixture (expect ≥1 match).
- `tests/test_llm_client/`, `tests/test_memory/`, etc. (existing, per touched module): re-run
  unchanged — AC-c requires no behavior change; any existing test that breaks from the factory swap
  is a regression to fix, not a spec change.

### 5. Docs + follow-up filing

- ADR-0132 gets a new Status Update entry (like the FRE-1144 one already there) recording D2 landed,
  the scope decision (full `src/` sweep minus `ui/`), and that D3 (Filebeat/FRE-1146) and the seam
  ticket (FRE-1148) remain open.
- File a `Backlog` ticket (one-line body) for the `LiteLLMClient`/cloud-provider egress gap (codex
  point 6) — not this ticket's scope, but a real, now-documented finding.

## Risk tier: Standard/Complex

Touches `src/` security logic (an actual egress control, not cosmetic) across 11 call sites →
**codex plan-review required** before coding, per this repo's build skill Step 3 — completed above
(round 1). Proceeding to TDD implementation per the revised design.

## Test commands

```
make test-file FILE=tests/test_security/test_transport_factory.py
make test-file FILE=tests/test_security/test_egress_seams.py
make test-file FILE=tests/test_security/test_bypass_rules.py
make test-file FILE=tests/test_security/test_domain_guard.py   # unchanged, still green
make test                                                       # full suite, AC-c regression sweep
make mypy
make ruff-check
```
