# FRE-512 — Envelope-integrity verification + served-response tests (ADR-0089 done-bar)

> Status: draft → codex-reviewed → owner-approved (2026-06-07; 3 MUST-FIX + 5 SHOULD addressed; 2 SHOULD-suggestions declined with rationale in D-a/§10)
> Ticket: [FRE-512](https://linear.app/frenchforest/issue/FRE-512) (Approved) · ADR-0089 D5 + ADR-0088 done-bar · Project: Artifact Execution Security
> Blockers cleared: FRE-509 (Worker CSP envelope, Done, live) · FRE-506 (gate-decision telemetry, Done, live) · FRE-511 (sanitizer retired, Done, deployed)
> Branch: `fre-512-envelope-integrity` · One PR

## Context

ADR-0089 D5: the FRE-506 "gate decision" is reframed from content verdicts to **envelope
integrity** — *did every served artifact actually receive its walls?* The alarm-worthy event is
"an artifact was served without its CSP header (or with a directive missing/changed)" — a
deterministic delivery failure. ADR-0088's done-bar: the sealed-box capability is not
shippable-to-default until envelope decisions are observable and a bare delivery is loud.

The envelope itself is live (FRE-509, Worker in `personal_agent_secrets`, commit `314987e`).
This ticket makes it **verifiable from this repo**: a served-response probe + telemetry, and CI
tests that assert the exact envelope against served response headers (never source, never bytes).

## Measured constraints (probed 2026-06-07)

1. **`artifacts.frenchforet.com` is behind Cloudflare Access.** An unauthenticated GET from the
   VPS returns `302 → frenchforest.cloudflareaccess.com` — Access intercepts *before* the Worker,
   so no Worker headers are visible without auth.
2. **The existing service token (`CF_ACCESS_CLIENT_ID/SECRET`, used for the SLM tunnel) is NOT
   authorized on the artifacts Access app** (`service_token_status: false` in the Access meta JWT).
   → One infra-side change is required before the probe can see real headers: add the service
   token to the artifacts Access app policy (terraform, `personal_agent_secrets` — same cross-repo
   seam as FRE-509, applied by owner/master). Until then the probe reports a distinct
   `unverified_access_denied` status (warning, not a false alarm).

## Design decisions (surfaced for review)

**D-a — Scope honestly: every artifact *commit* triggers a served-response probe; this is NOT
literal per-serve telemetry.** Direct browser serves never traverse this repo — only Worker-side
per-request logging (a new Worker→ES pipeline, cross-repo) would satisfy "every served artifact"
literally, and that would *re-invent* rather than *consume* FRE-506. The in-repo realization:
after every artifact commit, the backend issues one real GET through the full edge path
(`https://artifacts.frenchforet.com/{id}`) and records the envelope actually applied. Because the
Worker serves one static policy for every artifact (FRE-509), a commit-time pass + the verifier
contract tests + the alarm condition cover the drift risk. Worker-side per-request logging is
filed as a follow-up ticket (Needs Approval) as the only literal D5 satisfier. (Codex finding 1.)

**D-b — A pure, header-only verifier is the single source of truth.**
`verify_envelope(status_code, headers, expect_html) -> EnvelopeReport` consumes *only* the status
and response headers — by construction it cannot inspect artifact bytes or prompts (the D1/D5
scope boundary holds structurally, not by policy). The same function is used by the probe, the
unit tests, and the live-verification script.

**D-c — Exact-set CSP comparison, set-of-tokens per directive; exactly one well-formed policy.**
Missing directives, value mismatches, *and unexpected extra directives* all fail (an added
`media-src https://x` would silently widen the `default-src 'none'` fallback — extras are not
harmless). Token order within a directive value is not significant (CSP source lists are
unordered) — avoids brittle false alarms. Two malformation classes also fail rather than being
normalized away (codex finding 2): **multiple `Content-Security-Policy` headers** in one response
(`multiple_csp_policies` — cumulative-policy semantics ≠ the one D2 policy we deploy) and
**duplicate directives within one policy** (`duplicate_directive` — browsers ignore the later one,
so a merged-set parse could falsely bless a malformed policy). A
`Content-Security-Policy-Report-Only` header is *not* an enforced CSP and never counts as
presence.

**D-d — Event: `artifact_envelope_integrity`, extending the FRE-506 family.** Keyed by
`trace_id` / `artifact_id` / `session_id` / `user_id` / `slug` (ADR-0074 identity), joinable with
`artifact_gate_decision` on `artifact_id`. Severity encodes the alarm:
- `envelope_ok=true` → `log.info`
- envelope failure (CSP absent / directive missing / mismatch / wrong MIME / nosniff missing) →
  **`log.error`** — the D5 alarm condition
- `unverified_access_denied` (Access wall, token not yet authorized) → `log.warning`
- `probe_failed` (timeout / connection error) → `log.warning`
- probe disabled or no public URL configured → skipped, no event (tests pin the skip)

Access-denial classification is one shared helper `classify_access_denied(status, headers)` used
by both the probe and the CLI script (codex finding 4): any 3xx whose `location` host ends with
`cloudflareaccess.com`, or a 401/403 carrying `Cloudflare-Access` in `www-authenticate`. A
**non-Access** 3xx/4xx/5xx is an envelope failure (`http_error`), never silently accepted or
followed.

**D-e — Probe is inline-awaited, never load-bearing.** Called at the end of
`artifact_write_executor` (covers both `direct_write` and `draft` paths), wrapped so no exception
or timeout can fail the commit. Inline-await over fire-and-forget: a detached task can lose the
event on request cancellation or shutdown (codex finding 3). The latency cost is bounded tightly:
`artifact_envelope_probe_timeout_s` defaults to **2.0 s** (the commit already spends seconds in
R2 + embedding; a 2 s worst-case tax is acceptable, 5 s was not), and `probe_duration_ms` is
recorded on every event. Always GET (never HEAD — Worker HEAD parity is unproven) with
`follow_redirects=False`, streamed and closed after headers — the body (≤5 MB) is never
downloaded, which is also the scope boundary in action.

**D-f — MIME expectation is content-type-aware, compared structurally not textually.** The
`Content-Type` header is parsed into (type/subtype, params): type/subtype and charset compare
case-insensitively (`text/html; charset=UTF-8` passes), any parameter other than `charset` fails,
a missing or duplicate/comma-joined `Content-Type` fails (codex finding 7). For HTML commits the
normalized value must equal `text/html` + `charset=utf-8`. For non-HTML commits the serve must
never carry an executable-script MIME (`application/javascript`, `text/javascript`,
`application/ecmascript`, …, compared after parameter-stripping, case-insensitive) — the D2a
"artifact URL cannot be loaded as a script" property; the served MIME is recorded either way.
(FRE-509's exact non-HTML behavior is reconciled at the post-deploy live check.)

**D-g — "CI fails on a bare serve" is realized in two layers, named for what they are (codex
finding 5).**
- **Layer 1 — verifier contract tests (every `make test` run, no network):** unit tests drive
  `verify_envelope` with synthetic served responses for every failure class — absent CSP, each
  missing directive, a mutated `sandbox` directive (e.g. `allow-same-origin` added), foreign
  `frame-ancestors`, `connect-src` ≠ `'none'`, wrong MIME, missing nosniff, multiple CSP
  policies, duplicate directives, report-only-only, non-Access redirect — and assert each is
  flagged. This pins the verifier and the expected-envelope spec; it is **not** a live
  served-response check and is not claimed as one.
- **Layer 2 — true served-response gate (live, opt-in):** `scripts/verify_artifact_envelope.py
  <url>` fetches a real served response and exits non-zero unless `envelope_ok` — wired as
  `make verify-envelope URL=…`; master runs it at the deploy gate, and it is CI-attachable later
  (a job gated on a `VERIFY_ENVELOPE_URL` env + an authorized Access token) once the token
  exists. Header-level assertions are the served-response proxy for behavioral claims
  (fetch/XHR/WS/beacon blocked, foreign embedder refused); true browser-behavioral confirmation
  remains the open owner human-check from FRE-510/511 (DevTools / drawer check).

**D-h — Residual bound is asserted, not implied.** A dedicated test asserts the spec contains
`webrtc 'block'` and that the `sandbox` directive grants *exactly* `allow-scripts` (no
`allow-same-origin`, no popups/top-navigation/forms/downloads/modals), with a docstring that
documents the two bounded-not-closed residuals (self-navigation; WebRTC on browsers without
`webrtc` directive support) per ADR-0089 D2 tier 2.

## Files

### New

1. **`src/personal_agent/observability/artifact_envelope/__init__.py`** — re-exports.
2. **`src/personal_agent/observability/artifact_envelope/spec.py`** — the expected envelope:
   ```python
   EXPECTED_CSP_DIRECTIVES: Mapping[str, frozenset[str]] = MappingProxyType({
       "default-src": frozenset({"'none'"}),
       "script-src": frozenset({"https://artifacts.frenchforet.com", "'unsafe-inline'"}),
       "style-src": frozenset({"https://artifacts.frenchforet.com", "'unsafe-inline'"}),
       "img-src": frozenset({"https://artifacts.frenchforet.com", "data:"}),
       "font-src": frozenset({"https://artifacts.frenchforet.com", "data:"}),
       "connect-src": frozenset({"'none'"}),
       "worker-src": frozenset({"'none'"}),
       "form-action": frozenset({"'none'"}),
       "base-uri": frozenset({"'none'"}),
       "frame-ancestors": frozenset({"https://agent.frenchforet.com"}),
       "webrtc": frozenset({"'block'"}),
       "sandbox": frozenset({"allow-scripts"}),
   })
   EXPECTED_HTML_MIME = "text/html; charset=utf-8"
   FORBIDDEN_SCRIPT_MIMES: frozenset[str]  # application/javascript, text/javascript, …
   ```
3. **`src/personal_agent/observability/artifact_envelope/verifier.py`** — pure:
   - `parse_csp(header_value: str) -> dict[str, frozenset[str]]` (flags duplicate directives)
   - `classify_access_denied(status_code, headers) -> bool` (shared with probe + CLI, D-d)
   - `@dataclass(frozen=True) EnvelopeReport`: `envelope_ok: bool`, `csp_present: bool`,
     `missing_directives: tuple[str, ...]`, `mismatched_directives: tuple[str, ...]`,
     `unexpected_directives: tuple[str, ...]`, `served_mime: str | None`, `mime_ok: bool`,
     `nosniff_ok: bool`, `http_status: int`, `failures: tuple[str, ...]` (stable codes:
     `missing_csp`, `multiple_csp_policies`, `duplicate_directive`, `csp_directive_missing`,
     `csp_directive_mismatch`, `csp_directive_unexpected`, `wrong_mime`, `executable_mime`,
     `missing_nosniff`, `http_error`)
   - `verify_envelope(status_code: int, headers: Mapping[str, str] | list[tuple[str, str]], *,
     expect_html: bool) -> EnvelopeReport` — accepts multi-value headers so repeated CSP /
     Content-Type headers are observable, not silently merged
4. **`src/personal_agent/observability/artifact_envelope/probe.py`** —
   `async probe_served_envelope(*, public_url, artifact_id, slug, content_type, trace_id,
   session_id, user_id) -> None`: httpx streamed GET (`follow_redirects=False`, service-token
   headers when configured, timeout from settings) → `classify_access_denied` → else
   `verify_envelope` → emit `artifact_envelope_integrity` at the D-d severity, always with
   `probe_duration_ms`. Catches all exceptions; never raises.
5. **`scripts/verify_artifact_envelope.py`** — CLI: GET url → print report → exit 0/1.
   Reads service-token env vars if present. ~60 lines, reuses verifier.
6. **`tests/observability/artifact_envelope/test_verifier.py`** — layer-1
   verifier contract tests (D-g) + residual-bound test (D-h) + parse edge cases
   (case-insensitive header names, whitespace, trailing `;`, duplicate directive, multiple CSP
   headers, report-only-only, `charset=UTF-8` casing, extra Content-Type params, missing
   Content-Type, non-Access 3xx).
7. **`tests/observability/artifact_envelope/test_probe.py`** — mocked httpx:
   happy path emits info event with `envelope_ok=true` + full identity; missing-directive serve
   emits **error** event naming the directive; Access-302 emits `unverified_access_denied`
   warning; timeout emits `probe_failed` warning; never raises; body never read (stream closed —
   assert via mock).

### Modified

8. **`src/personal_agent/tools/artifact_tools.py`** — call the probe at the end of
   `artifact_write_executor` (after `_emit_gate_decision`, before return), guarded by
   `settings.artifact_envelope_probe_enabled` and `public_url is not None`. ~10 lines.
9. **`src/personal_agent/config/settings.py`** — two fields:
   `artifact_envelope_probe_enabled: bool = True`,
   `artifact_envelope_probe_timeout_s: float = 2.0`. Service-token creds reuse the existing
   `cf_access_client_id` / `cf_access_client_secret` (one token, multiple Access app policies).
10. **`docker/elasticsearch/index-template.json`** — explicit properties for every new field
    (walked through the dynamic templates per standing ES-mapping policy):
    `envelope_ok`/`csp_present`/`mime_ok`/`nosniff_ok` → `boolean`; `missing_directives`/
    `mismatched_directives`/`unexpected_directives`/`envelope_failures` → `keyword`;
    `served_mime` → `keyword`; `csp_header` → `keyword` w/ `ignore_above: 2048` (live value
    ~430 chars; the default 1024 cap is the silent-drop trap — raw header is posture data, not
    artifact bytes, and bounded); `probe_status` → `keyword` (also matches `.*_status` dynamic
    rule — explicit anyway); `http_status` → `integer`; `probe_duration_ms` → `integer`.
    (Codex suggested expected/served CSP hashes — skipped: the bounded raw `csp_header` plus
    named mismatched directives already give the debugging signal; hashes of a constant add
    nothing. `public_url_host` skipped: constant per deployment.)
11. **`tests/personal_agent/tools/test_artifact_tools.py`** — extend write/draft tests: probe
    invoked on commit (mocked, asserts identity kwargs); probe exception does not fail the
    commit; probe skipped when disabled / no public URL.
12. **`Makefile`** — `verify-envelope` target wrapping the script.

## Steps (TDD)

1. `spec.py` + `verifier.py` tests first → fail → implement → green:
   `make test-file FILE=tests/observability/artifact_envelope/test_verifier.py`
2. `probe.py` tests first → fail → implement → green:
   `make test-file FILE=tests/observability/artifact_envelope/test_probe.py`
3. Settings + `artifact_tools.py` hook, tests extended first:
   `make test-file FILE=tests/personal_agent/tools/test_artifact_tools.py`
4. ES template + script + Makefile target (script smoke-tested against the live origin from the
   VPS — expected result *today*: `unverified_access_denied`, exit 1, which proves the
   Access-detection path against the real edge).
5. Quality gates: `make test` (full) · `make mypy` · `make ruff-check` · `make ruff-format` ·
   `pre-commit run --all-files`.
6. PR with `.github/PULL_REQUEST_TEMPLATE.md`; STOP (master merges/deploys).

## Acceptance criteria

### Pre-merge (this PR)
- [ ] Every artifact **commit** (both paths) triggers a served-response probe that emits
      `artifact_envelope_integrity` with full ADR-0074 identity (unit-tested, mocked httpx) —
      per-commit, not literal per-serve (D-a; follow-up ticket filed for Worker-side per-request
      logging).
- [ ] A CSP-absent / directive-missing / directive-mutated / wrong-MIME / nosniff-missing /
      malformed-policy serve is flagged `envelope_ok=false` at **error** level naming the failure
      (verifier contract tests per class).
- [ ] Residual-bound test (D-h) passes; sandbox grants exactly `allow-scripts`.
- [ ] No telemetry path reads artifact bytes or prompts (verifier signature = status+headers only;
      probe streams and closes without reading body — asserted in tests).
- [ ] Probe can never fail or slow a commit beyond the configured timeout (exception + timeout
      tests).
- [ ] `make test` · `make mypy` · `make ruff-check` · `make ruff-format` · `pre-commit` clean.

### Post-deploy (master, same session as deploy)
- [ ] Re-apply the ES index template (`./scripts/setup-elasticsearch.sh`) so the new explicit
      properties land before events arrive.
- [ ] **Infra (owner laptop, `personal_agent_secrets`):** add the VPS service token to the
      artifacts Access app policy (terraform; same seam as FRE-509). Until applied, events read
      `probe_status=unverified_access_denied` — visible, not silent.
- [ ] Live E2E after token applied: commit a test artifact → ES shows
      `artifact_envelope_integrity` with `probe_status=verified`, `envelope_ok=true`, exact CSP.
- [ ] `make verify-envelope URL=https://artifacts.frenchforet.com/<live-id>` exits 0.
- [ ] Reconcile D-f: confirm the Worker's served MIME for one non-HTML artifact and adjust the
      spec note if it differs from the committed content type.

### Future-gate
- [ ] If the Worker CSP ever changes (`personal_agent_secrets` `artifacts.js`), `spec.py` must
      change in lockstep — cross-repo seam documented in both files' headers.
- [ ] Optional follow-ups (filed Needs Approval): Worker-side per-request envelope logging
      (true per-serve telemetry); Playwright behavioral residual test (fetch/WS actually blocked
      in-browser).

## Out of scope

- Worker/infra changes (terraform is owner-applied; this repo only documents the exact policy
  needed).
- Browser-behavioral verification (remains the open FRE-510/511 owner human-check).
- Any content inspection — D1/D5 boundary.
