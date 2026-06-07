# FRE-511 — Retire the artifact sanitizer from the security path + reframe the generation prompt

> Status: draft → codex-reviewed → owner-approved
> Ticket: [FRE-511](https://linear.app/frenchforest/issue/FRE-511) (Approved) · ADR-0089 D1/D7 · Project: Artifact Execution Security
> Blockers cleared: FRE-509 (Worker CSP, Done, deployed) · FRE-510 (iframe `allow-scripts`, Done, deployed)
> Branch: `fre-511-retire-artifact-sanitizer` · One PR (single ADR phase: D1/D7 commit-side cleanup)

## Context

ADR-0089 D1: security is a property of the served-CSP envelope (FRE-509) + opaque-origin sandbox
(FRE-510), **not** of inspecting artifact bytes. Both walls are live. The regex sanitizer in
`tools/artifact_tools.py` is therefore no longer a security boundary — remove it from the security
path, stop steering the model away from JS, and keep only quality (malformation) validation plus a
non-load-bearing analytics label.

Scope notes:
- FRE-500's `artifact_sandbox_enforcement_enabled` flag and the `_debug_dump_sandbox` /
  `_DRAFT_ATTEMPT_COUNTS` WIP named in the ticket **do not exist in the codebase** (FRE-500 was
  canceled pre-implementation). Nothing to remove beyond `artifact_tools.py` + tests.
- FRE-512 (separate, Approved) owns the serve-side envelope-integrity telemetry reframe. This
  ticket only collapses the commit-side FRE-506 label vocabulary.
- No consumer outside `artifact_tools.py` reads `sanitization_notes` / `sub_agent_attempts` result
  keys or the `artifact_gate_decision` event (grepped src/, scripts/, seshat-pwa/).

## Design decisions (surfaced for review)

**D-a — FRE-506 event vocabulary collapses to `committed` / `not_applicable`.**
The event name `artifact_gate_decision` and its field names are kept (ES mapping + FRE-505
dashboard continuity; `gate_decision` is a keyword field — new values index fine). With no gate
left, the honest values are: `committed` (any HTML commit, from either path) and `not_applicable`
(non-HTML). `bypassed` disappears — its alarm semantics ("a commit path ran no gate") are
meaningless when *no* path has a gate; FRE-512's serve-side "bypass = delivery failure" replaces
it. The `gate_ran` field is dropped (always-false noise). `script_count`/`handler_count`/
`cdn_count` stay as the ticket's optional `declares_scripts`-style analytics label; `commit_path`
(`draft`/`direct_write`) stays.

**D-b — counting moves entirely into `artifact_write_executor`.**
The `_gate_decision`/`_gate_violations` internal params are removed; `_commit_path` stays so the
draft path labels its commits. Counts are computed once at commit time for every HTML artifact —
one code path, no draft/write divergence (the exact divergence that caused the `87cbd720`
incident).

**D-c — `_validate_html_output` keeps its name; loses its terminal raises.**
Min-length / DOCTYPE / `</html>` checks remain recoverable `ToolExecutionError`s (quality, the
model can retry). The `TerminalToolError` script/handler raises and FRE-402 sandbox-retry
rationale are deleted.

**D-d — draft loop unrolls to a single attempt.**
The 2-attempt violation-retry machinery (FRE-496) is deleted. First-pass timeout →
`TerminalToolError` (unchanged); first-pass exception → `ToolExecutionError` (unchanged);
FRE-478 cap-hit warning stays. Result keys `sanitization_notes` and `sub_agent_attempts` are
dropped from the result dict (no consumers).

**D-e — prompt: no `/lib/` toolkit advertised yet.**
FRE-509 created the `/lib/<name>@<version>.js` route but no libraries are curated yet (open
curation ticket per ADR-0089). The prompt therefore requires fully self-contained inline JS and
still bans external/CDN references (the CSP would silently block them — better to never emit
them). Mermaid markup stays as the portability lane.

## New `_HTML_GENERATION_SYSTEM_PROMPT` (full text)

```
You are an HTML document generator. You receive a structured plan and produce \
a complete, standalone HTML document.

REQUIREMENTS:
- Output ONLY the HTML document. No explanation, no markdown fences, no preamble.
- Start with <!DOCTYPE html> and end with </html>.
- Define a complete design system in a <style> block in <head>:
  * CSS custom properties for colors: --color-primary, --color-secondary, \
--color-accent, --color-bg, --color-surface, --color-text, --color-muted.
  * Spacing scale: --spacing-1 through --spacing-8 (0.25rem increments).
  * Typography: --font-sans, --font-mono; size classes from text-xs to text-3xl.
  * Utility classes: flex, grid, gap-1 through gap-6, p-1 through p-8, \
m-1 through m-8, text-center, text-left, text-right, font-bold, font-medium, \
rounded, rounded-lg, shadow, shadow-lg, hidden, w-full.
- INTERACTIVITY: JavaScript is available. The document runs in a sandboxed, \
sealed page — inline <script> blocks and event handlers run normally, so use \
them freely for genuine interactivity: simulations, explorable diagrams, \
charts, animations, calculators, tabs, filters. Prefer plain CSS (:hover, \
:target, <details>/<summary>, transitions) when it does the job with less \
code; reach for JavaScript when the experience genuinely needs it.
- SEALED-BOX CONSTRAINTS (hard, enforced by the runtime — design within them):
  * No network: fetch/XHR/WebSocket/beacon are blocked. Embed ALL data \
inline in the document.
  * No storage: localStorage, sessionStorage, IndexedDB, and cookies are \
unavailable. Keep state in JS variables or the DOM.
  * No external resources: no CDN scripts/styles/fonts/images (Tailwind CDN, \
Alpine.js, jQuery, Google Fonts, etc. will silently fail to load). Everything \
must be inline: CSS in <style>, JS in <script>, images as data: URIs or \
inline SVG.
  * No popups, no form submission to external endpoints.
- PORTABILITY (choose deliberately): static diagrams that should travel with \
the file — flowcharts, architecture diagrams, sequence/class diagrams — use \
<pre class="mermaid">…</pre> markup with Mermaid syntax; the server renders \
these to static inline SVG, so the document stays self-contained and viewable \
anywhere. Example: <pre class="mermaid">graph LR; A[Start] --> B[End];</pre>. \
Use JavaScript instead when the experience is genuinely interactive — such an \
artifact is viewed on its hosted page.
- Use semantic HTML5 elements: header, main, section, article, footer, \
nav, aside, figure, figcaption.
- Responsive: use CSS media queries (@media) for mobile/tablet/desktop.
- Accessibility: heading hierarchy (h1 > h2 > h3), alt text on images, \
ARIA labels where helpful, sufficient color contrast.
- For data tables: <table> with <thead>/<tbody>, striped rows via \
nth-child, sticky header if many rows.
- For metrics/KPIs: card layout with large number and small label beneath.
- For comparison layouts: CSS grid with equal-width columns.
- Maximum document size: aim for under 200KB of HTML text.
```

## Steps (TDD)

All paths relative to `/opt/seshat/.claude/worktrees/build`.

### 1. Branch
```bash
git checkout -b fre-511-retire-artifact-sanitizer
```

### 2. Tests first — rewrite `tests/personal_agent/tools/test_artifact_tools.py`

**Delete** (machinery being retired):
- `test_artifact_draft_strips_script_tags_and_delivers`, `test_artifact_draft_strips_event_handlers_and_delivers`,
  `test_artifact_draft_strips_cdn_link_and_delivers`, `test_artifact_draft_strips_glued_handler_and_delivers`
- `test_artifact_draft_sandbox_retry_succeeds`, `test_artifact_draft_clean_first_pass_no_retry`,
  `test_artifact_draft_retry_timeout_falls_back_to_prior`, `test_artifact_draft_sandbox_retry_logs`,
  `test_artifact_draft_sanitization_logs_warning`
- `test_sanitize_sandbox_violations_noop_on_clean_html`, `test_sanitize_handles_regex_variants`,
  `test_sanitize_strips_attribute_and_whitespace_close_tags`, `test_sanitize_strips_unterminated_script_tag`,
  `test_sanitize_strips_glued_event_handler_preserving_quote`, `test_sanitize_empty_value_handler_stripped`,
  `test_injected_banner_passes_validation`, `test_sanitize_banner_bodyless_html`
- `test_validate_html_output_still_terminal_on_script`, `test_html_generation_prompt_redirects_to_css_only`,
  `test_system_prompt_prohibits_scripts`
- `test_gate_decision_enforced_pass_clean_draft`, `test_gate_decision_stripped_when_draft_sanitizes`,
  `test_gate_decision_rejected_on_terminal_sandbox_violation`, `test_gate_decision_bypassed_on_direct_html_write`
  (these are also the only tests asserting `gate_ran` — the field's removal orphans no other test)

**Rework**:
- `test_artifact_draft_mermaid_plus_script_both_handled` → new posture: mermaid block renders to
  inline SVG **and** the `<script>` survives intact in the committed bytes (codex finding #1).

**Add** (write first; confirm each fails against current code):
- `test_artifact_draft_script_artifact_committed_intact` — sub-agent returns HTML with `<script>`
  + `onclick` + `</html>`; assert stored bytes contain both, unmodified; no banner `<aside>`; no
  `sanitization_notes` key in result.
- `test_artifact_draft_event_handler_committed_intact` — `onclick` handler survives commit.
- `test_artifact_write_direct_script_html_committed` — direct `artifact_write` of `<script>` HTML
  commits intact (already true; pins the invariant).
- `test_validate_html_output_accepts_scripts` — `_validate_html_output` does not raise on a
  well-formed document containing `<script>`/`onclick`.
- `test_validate_html_output_rejects_truncated` / `_rejects_missing_doctype` / `_rejects_tiny` —
  malformation still raises `ToolExecutionError` (keep/extend existing
  `test_artifact_draft_rejects_missing_doctype`).
- `test_artifact_draft_single_attempt_on_script_output` — sub-agent emitting `<script>` triggers
  exactly one `respond()` call (no retry).
- `test_system_prompt_allows_scripts` — prompt contains no script prohibition: asserts
  `"REJECTED" not in prompt`, no `"cannot run"` / `"JavaScript-free"`; asserts presence of
  sealed-box steering: `"No network"`, `"No storage"`, `"mermaid"`, portability language
  (`"travel"`), `"inline"`.
- `test_gate_decision_committed_on_draft_html` / `test_gate_decision_committed_on_direct_html` /
  `test_gate_decision_not_applicable_for_non_html` (rework existing) — new vocabulary; **all
  three counts** asserted (`script_count`, `handler_count`, `cdn_count` reflect content — codex
  finding #5); no `gate_ran` field.
- Keep: all `artifact_write`/`list`/`read` tests, mermaid tests, plan-truncation tests, cap-hit
  tests, timeout tests, `test_validate_html_output_allows_data_on_attributes` (now also via
  the counting regex).

```bash
make test-file FILE=tests/personal_agent/tools/test_artifact_tools.py   # expect: new tests FAIL
```

### 3. Implement — `src/personal_agent/tools/artifact_tools.py`

1. **Imports**: drop `Sequence` (orphaned by banner removal); `import re` keeps but stale
   `# noqa: F401` comment is removed (module-level `re.compile` is a real use).
2. **Delete**: `_SCRIPT_BLOCK_RE`, `_SCRIPT_OPEN_RE`, `_SCRIPT_CLOSE_RE`, `_EVENT_HANDLER_ATTR_RE`,
   `_sanitize_sandbox_violations`, `_inject_sanitization_banner`, `_css_only_retry_reminder`.
3. **Keep as analytics-only** (comment update: non-load-bearing label, ADR-0089 D1/D5):
   `_SCRIPT_TAG_RE`, `_EVENT_HANDLER_RE`, `_CDN_LINK_RE`, `_count_sandbox_violations`.
4. **`_validate_html_output`**: remove both `TerminalToolError` raises + FRE-402 comment; docstring
   → quality validator (ADR-0089 D1: malformation only, never a security decision).
5. **`_emit_gate_decision`**: docstring + `decision` doc → `committed | not_applicable`; drop
   `gate_ran` param/field.
6. **`artifact_write_executor`**: drop `_gate_decision`/`_gate_violations` params (keep
   `_commit_path`); emission block becomes: HTML → `committed` + counts; else `not_applicable`.
   Docstring updated.
7. **`artifact_draft_executor`**: unroll loop to single attempt (keep timeout→Terminal,
   exception→ToolExecutionError, cap-hit log, fence strip); delete violation/retry/strip/banner
   blocks and the `rejected` emission path; drop `sanitization_notes`/`sub_agent_attempts` from
   result + docstring; call `artifact_write_executor(..., _commit_path="draft")`.
   **Preserve processing order** (codex finding #3): truncate plan → respond → fence-strip →
   cap-hit check → mermaid render → malformation validate → write.
8. **`_HTML_GENERATION_SYSTEM_PROMPT`**: replace with the text above.
9. **Comment hygiene** (same file only): `_ALLOWED_CONTENT_TYPES` comment cites superseded
   ADR-0070 D7 → cite ADR-0089; module docstring sentence about draft flow unchanged.
10. **Out of scope** (codex finding #6, recorded explicitly): the pre-existing `ctx: Any` /
   `dict[str, Any]` signatures shared by every tool executor in `tools/` predate this ticket and
   are not narrowed here — surgical-change rule; a module-wide typing cleanup is its own ticket.

### 4. Verify
```bash
make test-file FILE=tests/personal_agent/tools/test_artifact_tools.py   # expect: all pass
make test            # full unit suite — expect: pass (3300+)
make mypy            # expect: clean
make ruff-check && make ruff-format
pre-commit run --all-files
```

### 5. Docs
- This plan file (committed).
- No skill docs / READMEs reference the sanitizer (grep `sanitize` in docs/skills, README) — verify.

### 6. PR
- Template `.github/PULL_REQUEST_TEMPLATE.md`; pre-merge checklist only.
- PR notes for master: (a) FRE-496 + FRE-500 are confirmed superseded — close/obsolete them at
  merge per ticket; (b) FRE-505 Kibana gate-decision panel will start showing `committed` /
  `not_applicable` instead of the five old values — no mapping change (keyword field), panel may
  need a label refresh; (c) deploy after merge enables full script-artifact E2E (MASTER_PLAN open
  item from FRE-510).

## Acceptance criteria (ticket → verification)

| Criterion | Verified by |
|---|---|
| No security decision by inspecting artifact bytes | grep: no `TerminalToolError` on content; `_count_sandbox_violations` callers emit telemetry only |
| Interactive `<script>` artifact committed intact | `test_artifact_draft_script_artifact_committed_intact`, `test_artifact_write_direct_script_html_committed` |
| Malformation still rejects truncated/empty doc | `test_validate_html_output_rejects_*`, existing doctype test |
| Prompt reframed; regression test on wording | `test_system_prompt_allows_scripts` |
| `make test` / `make mypy` / `make ruff` clean | step 4 |
