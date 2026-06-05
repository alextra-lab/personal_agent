# FRE-496 — Interactive-artifact `<script>` → graceful degradation instead of hard-fail

**Ticket:** FRE-496 (Approved, Tier-2:Sonnet, Bug) · Project: *Turn Cost & Latency Optimization (artifact builds)*
**Refs:** ADR-0070 D7 (sandbox), FRE-478 (origin, trace `b5a8449b`), FRE-397 (long-term dynamic-artifact tier), FRE-497 (related: self-correcting retries)
**File:** `src/personal_agent/tools/artifact_tools.py` · tests `tests/personal_agent/tools/test_artifact_tools.py`

## Problem

A request for an *interactive* HTML artifact produces a clean draft, but the model satisfies
"interactive" with JavaScript. `_validate_html_output` (`artifact_tools.py:1025`) then raises
`TerminalToolError` on `<script>` detection → `task_failed`, **user gets no artifact**. The
generation prompt already forbids `<script>` three times (L749–756), so re-stating the
prohibition is not the fix.

## Decision: one bounded CSS-only retry, THEN strip-and-deliver (OWNER-APPROVED 2026-06-05)

On a sandbox-violating first draft, re-run the sub-agent **once** with strengthened CSS-only
guidance that names the specific violation. If the retry is clean → ship it. If it still violates
(or the retry times out / errors) → fall back to strip-and-deliver on the best draft we have +
banner note. Net: at most 2 sub-agent calls, and the user always gets *something* (codex HIGH —
retry may preserve intended interactivity the reframed prompt now enables; strip is the floor).

The retry reminder does **not** echo the previous (large) HTML back — it re-sends the plan with an
appended CSS-only instruction, so attempt 2 costs ~the same as attempt 1, not double.

## Part 1 — Prompt reframe (`_HTML_GENERATION_SYSTEM_PROMPT`, L734)

Replace the SECURITY bullet + add an INTERACTIVITY bullet that *redirects intent* and *states the
consequence*:

- **INTERACTIVITY**: the doc renders in a JavaScript-free sandbox; make it interactive with CSS
  only — `:hover`, `:focus`, `:target`, `<details>/<summary>`, the checkbox-hack, CSS
  transitions/animations, scroll-snap. These are the only mechanisms available.
- **SECURITY (hard constraint)**: any `<script>` (inline OR `src=`) causes the artifact to be
  **REJECTED and the user receives NOTHING**; same for inline event handlers and remote/CDN
  frameworks (Tailwind CDN, Alpine, htmx, jQuery). Inline ALL CSS; load NO external resources.

Keep the mermaid bullet unchanged.

## Part 2 — Graceful degradation (`artifact_tools.py`)

New module-level regexes (near `_SCRIPT_TAG_RE`, L725):
```python
_SCRIPT_BLOCK_RE   = re.compile(r"<\s*script\b[^>]*>.*?</\s*script\s*>", re.IGNORECASE | re.DOTALL)
_SCRIPT_OPEN_RE    = re.compile(r"<\s*script\b[^>]*/?>", re.IGNORECASE)   # stray/self-closing/src=
_SCRIPT_CLOSE_RE   = re.compile(r"</\s*script\s*>", re.IGNORECASE)        # orphan closing tags
_EVENT_HANDLER_ATTR_RE = re.compile(r"""\s+on\w+\s*=\s*("[^"]*"|'[^']*'|[^\s>]+)""", re.IGNORECASE)
_CDN_LINK_RE       = re.compile(r'<\s*link\b[^>]*\bhref\s*=\s*["\']?https?://[^"\'>\s]*["\']?[^>]*>', re.IGNORECASE)
```
Order matters: `_SCRIPT_BLOCK_RE` first (removes `<script>…JS…</script>` whole, so leftover JS
body text is gone), then `_SCRIPT_OPEN_RE`, then `_SCRIPT_CLOSE_RE` (orphan closers). This avoids
shipping dangling JS text (codex MEDIUM). The CDN regex now also matches unquoted `href=`.

New helper:
```python
def _sanitize_sandbox_violations(html: str) -> tuple[str, list[str]]:
    """Strip sandbox-prohibited nodes (ADR-0070 D7) so the artifact can still ship.

    Removes <script> blocks (inline + src=), inline event-handler attributes, and external
    CDN <link> references. Returns (sanitized_html, notes); notes is empty when nothing was
    stripped. Best-effort regex degradation mirroring _mermaid_fallback — the last-resort
    guard remains _validate_html_output.
    """
```
- if `_SCRIPT_TAG_RE` matches → strip BLOCK, then OPEN, then CLOSE → note
- if `_EVENT_HANDLER_RE` matches → strip `_EVENT_HANDLER_ATTR_RE` → note
- if `_CDN_LINK_RE` matches → strip → note

**Note strings must NOT contain the literal `<script` token or any `onX=` token** (codex HIGH:
the banner is injected then re-validated; `_validate_html_output` is raw-text and would self-fail).
Use: `"Removed embedded scripts — JavaScript cannot run in this sandbox."` /
`"Removed inline event-handler attributes."` / `"Removed external CDN resources."`

New helper to surface the note in-document (mirrors mermaid figcaption):
```python
def _inject_sanitization_banner(html: str, notes: Sequence[str]) -> str:
    """Insert a static <aside> banner listing what was stripped (style= attr only, no script)."""
```
Insertion order (codex MEDIUM — bodyless HTML): after `<body…>` if present, else after the
opening `<html…>` tag, else after `<!doctype html>`; **never prepend before the doctype** (would
break the doctype-in-first-200-chars check). If none match, skip the banner (notes still ride in
the result dict). Inline `style=` only — no script, no `on*=` — so it passes `_validate_html_output`.

Retry-reminder builder:
```python
def _css_only_retry_reminder(violation_notes: Sequence[str]) -> str:
    """Augment the user prompt for the bounded retry, naming the sandbox violation."""
```
Returns e.g. *"The previous attempt used JavaScript / inline event handlers / external resources,
which CANNOT run in this sandbox. Regenerate the COMPLETE document with CSS-only interactivity
(:hover, :target, <details>/<summary>, the checkbox-hack, CSS transitions). No <script>, no on*
handlers, no external/CDN resources. Output only the HTML."* — naming which of script/handler/CDN
fired (from `violation_notes`).

**Restructure the single `respond` call (`:1166–1213`) into a bounded loop** (`max_attempts = 2`):
- attempt 1 uses `base_messages`; attempt 2 appends `_css_only_retry_reminder(...)` to the user msg
  (system prompt unchanged, previous HTML NOT echoed).
- per attempt: time it, accumulate `total_sub_agent_ms`, log `artifact_draft_sub_agent_*` with
  `attempt=`. Detect violations via `_, notes = _sanitize_sandbox_violations(candidate)`.
- clean output → keep + break. Violating + attempts remain → `log.warning("artifact_draft_sandbox_retry", …, violation_notes=…)` + continue.
- **attempt 1** timeout → `TerminalToolError` (as today); attempt 1 other exc → `ToolExecutionError` (as today).
- **attempt 2** timeout/exc → do NOT hard-fail: `log.warning("artifact_draft_retry_failed_using_prior", …)`, keep attempt-1 HTML, break (strip handles it).

Then between mermaid render (`:1255`) and validate (`:1260`):
```python
html_content, sanitize_notes = _sanitize_sandbox_violations(html_content)
if sanitize_notes:
    # FRE-496: deliberate steering — the prompt says scripts are "rejected", but here we strip
    # the residue the bounded retry could not clear and ship a static artifact rather than fail.
    html_content = _inject_sanitization_banner(html_content, sanitize_notes)
    log.warning("artifact_draft_sanitized_sandbox_violations", trace_id=trace_id,
                session_id=session_id, span_id=span_id, task_id=task_id,
                notes=sanitize_notes, retry_triggered=retry_triggered)
_validate_html_output(html_content)   # safety net: should not fire after strip for common cases
```
Add to the result block (`:1285`): `result["sanitization_notes"] = sanitize_notes`,
`result["sub_agent_attempts"] = attempts_made`. `sub_agent_duration_ms` = `total_sub_agent_ms`.

`_validate_html_output` keeps its terminal raises as a **defense-in-depth safety net** — if
sanitize ever misses a pathological case we still refuse to ship a script to the sandbox. Update
its docstring to note sanitize runs first.

**Honest limitation (codex MEDIUM):** the validator is raw-text and stricter than the stripper.
The common bug case — real `<script>` tags / handler *attributes* — is fully fixed. A pathological
doc with a literal `onclick=` token in *body text* (e.g. a tutorial code sample) would still trip
the safety net and hard-fail; that is rare and acceptable for round 1 (a JS tutorial more
plausibly uses `<script>`, which we strip). Documented, not silently ignored.

**Prompt deterrence note (codex LOW):** the reframed prompt tells the model scripts cause the
user to "receive NOTHING," but we now strip-and-deliver. This is *deliberate steering* — we want
zero scripts; the degraded path is a safety net, not a feature to rely on. Add a code comment at
the sanitize call site so the discrepancy is intentional, not a bug.

## Tests (`test_artifact_tools.py`)

TDD order — write/adjust failing first, confirm red, implement. Extend `_FakeSubAgentClient` /
`_install_draft_fakes` with an optional `html_sequence: list[str]` so a fake can return a
violating draft on call 1 and a clean draft on call 2 (clamp to last element).

1. **Prompt regression** (new): assert `_HTML_GENERATION_SYSTEM_PROMPT` contains the new wording
   — `"CSS only"`, `":target"`, `"checkbox-hack"`, `"REJECTED"`.
2. **Rewrite `test_artifact_draft_rejects_script_tags`** → `test_artifact_draft_strips_script_tags_and_delivers`:
   script-laden HTML on BOTH attempts → **no raise**; artifact committed; `store.put_calls[0]["content"]`
   has no `<script`; `out["sanitization_notes"]` non-empty; `out["sub_agent_attempts"] == 2` (retry fired).
3. **Rewrite `test_artifact_draft_rejects_event_handlers`** → strips handler, delivers,
   `onclick` gone from stored content, note present.
2b. **New** `test_artifact_draft_sandbox_retry_succeeds`: `html_sequence=[script_html, clean_html]`
    → no raise; `out["sanitization_notes"] == []`; stored content is the clean retry; respond called
    twice; `out["sub_agent_attempts"] == 2`.
2c. **New** `test_artifact_draft_clean_first_pass_no_retry`: clean HTML → respond called once;
    `out["sub_agent_attempts"] == 1`; no `artifact_draft_sandbox_retry` log.
2d. **New** `test_artifact_draft_retry_timeout_falls_back_to_prior`: attempt 1 = script_html,
    attempt 2 raises TimeoutError → no hard-fail; attempt-1 draft stripped + delivered;
    `artifact_draft_retry_failed_using_prior` logged.
2e. **New** `test_artifact_draft_sandbox_retry_logs`: `_spy_artifact_log` records
    `artifact_draft_sandbox_retry` with `violation_notes` on the violating-first-pass path.
4. **New** `test_artifact_draft_strips_cdn_link_and_delivers`: external `<link href="https://...">`
   stripped, delivered, note present.
5. **New** `test_sanitize_sandbox_violations_noop_on_clean_html`: clean HTML → unchanged, `[]`.
6. **New** `test_sanitize_handles_regex_variants` (unit, direct on `_sanitize_sandbox_violations`):
   mixed-case `<SCRIPT>`, self-closing `<script src=… />`, orphan `</script>`, single-quoted &
   unquoted `onclick=`, unquoted `<link href=https://…>` — all stripped.
7. **New** `test_injected_banner_passes_validation` (codex HIGH): a script-laden draft →
   `_validate_html_output(result-html)` does NOT raise; the banner's own text contains no
   `<script`/`onX=` token.
8. **New** `test_sanitize_banner_bodyless_html` (codex MEDIUM): doctype+`<html>` with no `<body>` →
   sanitize+inject still validates; banner present after `<html…>`.
9. **New** `test_validate_html_output_still_terminal_on_script` (unit, direct call): the safety net
   still raises `TerminalToolError` when handed raw script HTML (proves defense-in-depth intact).
10. **New** `test_artifact_draft_sanitization_logs_warning`: `_spy_artifact_log` records
    `artifact_draft_sanitized_sandbox_violations` with `notes`.
11. **New** `test_artifact_draft_mermaid_plus_script_both_handled` (codex MEDIUM): a draft with a
    mermaid block AND a script → mermaid rendered/fallback AND script stripped, artifact ships.

## Quality gates
```bash
make test-file FILE=tests/personal_agent/tools/test_artifact_tools.py   # module, expect all pass
make test                                                               # full suite
make mypy
make ruff-check && make ruff-format
pre-commit run --all-files
```

## Out of scope / follow-ups
- Bounded CSS-only retry → FRE-497.
- Real sandboxed-JS interactivity tier → FRE-397.
- External `src=` on `<img>/<iframe>/<video>/<audio>` (codex HIGH/MEDIUM): these do **not** trip
  `_validate_html_output`, so they never hard-fail — out of scope for this bug. The prompt's
  "load NO external resources" line steers against them; if we later want to enforce it, that's a
  quality follow-up ticket (not the hard-fail this ticket fixes).
- Literal `onX=` in body text remaining terminal — see "Honest limitation" above; revisit with FRE-497.
- `artifact_write_executor` direct path (no `_validate_html_output`) — out of scope; this ticket is
  the *draft* path only (the measured failure). Note for a future hardening ticket if desired.
- Post-deploy live re-run of an interactive-artifact request → master, after deploy (Linear comment).
