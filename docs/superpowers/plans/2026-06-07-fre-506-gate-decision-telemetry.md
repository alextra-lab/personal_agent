# FRE-506 — Sandbox gate-decision telemetry on every artifact commit

**Ticket:** FRE-506 (Approved, Tier-2:Sonnet, project *Observability Foundation*). Blocks FRE-512. L0 dep of ADR-0089.
**Refs:** ADR-0089 (D1 retire sanitizer-as-security; **D5 reframes FRE-506**), ADR-0070 D7 (superseded), FRE-496 (the gate), FRE-509 (Worker CSP — separate repo), FRE-512 (done-bar consumes this).
**Evidence:** trace `87cbd720` / artifact `da216aa4` — see the STEP-1 Confirm comment on FRE-506.

---

## STEP 1 — Confirm (DONE)

Posted to FRE-506. Summary: the sandbox gate (`_validate_html_output` + `_sanitize_sandbox_violations`) is wired **only** into `artifact_draft_executor` (`tools/artifact_tools.py:1464,1493,1507`). `artifact_write_executor` (`:309–449`) runs **no** HTML gate. In the incident, `artifact_draft` ran the gate but **failed a malformation check** ("missing `</html>`", attempt-2 truncated by the output cap) → the model then called **`artifact_write` directly** with its own ~70 KB HTML (1 `<script>` + 2 `onclick`), which committed with **zero gate events**. Bypass = ungated second commit path; nothing logged that a gate was skipped.

## Design decision (for owner sign-off)

**Vocabulary tension to resolve:** FRE-506's AC asks for content-vocabulary decisions (`enforced_pass / rejected / stripped / bypassed`). ADR-0089 **D5** reframes FRE-506 toward **envelope integrity** (was the CSP header served?) and **D1** says content verdicts must never be load-bearing for security.

**Recommended reconciliation (implement this unless owner steers otherwise):**
- Emit the **content-vocabulary gate decision the ticket names**, but frame the event as an **explicit non-load-bearing observation/label** — exactly what ADR-0089 D1 ("survives as a cheap best-effort label that nothing depends on for safety") and D5's scope boundary permit. It is the L0 commit-time signal FRE-512 consumes.
- **Envelope integrity** (served CSP header present/correct) is a *served-response* fact produced by the Worker (FRE-509, infra repo) and verified by FRE-512's served-response tests — **not** observable at commit time in this repo. So this repo emits the commit-time decision; the envelope probe is FRE-512/FRE-509. The two are complementary, not competing.
- **Minimal correctness:** **do NOT** add strip/validate to `artifact_write` (that would deepen the sanitize-output posture ADR-0089 D1 is retiring). Instead **document the exemption in code** and emit `bypassed` so it is observable. Safety for that path is the served-CSP envelope (ADR-0089 D2/D3), not content inspection.

This satisfies FRE-506's AC verbatim while staying consistent with ADR-0089. Alternative (if owner prefers): emit pure envelope-integrity vocabulary now and defer content labels — rejected here because the served CSP isn't in this repo and isn't built yet (FRE-509), so there'd be nothing to observe.

## Decision vocabulary (the label, non-load-bearing)

`gate_decision` (field name ends in `_decision` → maps to ES `keyword` via the live `agent-logs` dynamic template):

| value | meaning | emitted from |
|---|---|---|
| `enforced_pass` | gate ran, content had no sandbox violations | `artifact_draft` (chains to write) |
| `stripped` | gate ran, sanitized violations, then committed | `artifact_draft` (chains to write) |
| `rejected` | gate ran, sandbox `TerminalToolError` (script/handler the stripper missed) — **no commit** | `artifact_draft` (on the terminal raise) |
| `bypassed` | **no gate ran** — HTML committed via direct `artifact_write` | `artifact_write` (direct path) |
| `not_applicable` | non-HTML artifact (csv/json/png/svg/md) — sandbox gate is N/A | `artifact_write` (direct path) |

Each event also carries violation counts (the label): `script_count`, `handler_count`, `cdn_count` (ints → ES `long`), plus `commit_path` (`draft` | `direct_write`), `content_type`, `artifact_id`, `slug`, `size_bytes`, identity (`trace_id`/`session_id`/`user_id`), and `gate_ran` (bool). Malformation failures (missing DOCTYPE/`</html>`, too-small) are **not** sandbox verdicts — they keep their existing `ToolExecutionError` + `tool_call_failed` telemetry and emit **no** `gate_decision` (no commit, no sandbox judgment reached).

## ES mapping audit (per the "audit every field first pass" rule)

Walked every field through the live `agent-logs-template` dynamic_templates:
- `gate_decision` → `^(.*_decision|…)$` regex template → **keyword** ✓ (enum, ≪1024 chars).
- `commit_path` → falls to `default_string_keyword` → **keyword** (ignore_above 1024; values `draft`/`direct_write` are short) ✓.
- `content_type` → `.*_type` → **keyword** ✓ (already used).
- `*_id` (`artifact_id`/`trace_id`/`session_id`/`user_id`) → `*_id` template → **keyword** ✓.
- `slug` → default keyword ✓.
- `script_count`/`handler_count`/`cdn_count` → integers → dynamic **long** ✓ (no float `0.0→long` trap — these are ints).
- `size_bytes` → integer → **long** ✓.
- `gate_ran` → boolean ✓.
**No floats, no long-text/error/digest fields** → neither the float-precision trap nor the keyword-`ignore_above` indexing-drop trap applies. **No template change and no explicit properties needed.** (`event` → `event_type` mapping confirmed: `es_handler.py:121`.)

---

## Implementation (all in `src/personal_agent/tools/artifact_tools.py`)

### Step A — violation-count helper
Add near the detectors (~after line 759):
```python
def _count_sandbox_violations(html: str) -> tuple[int, int, int]:
    """Count sandbox-relevant constructs for the gate-decision label (non-load-bearing).

    Returns ``(script_count, handler_count, cdn_count)`` using the same detectors the
    gate uses. This is an analytics label only (ADR-0089 D1/D5) — nothing depends on it
    for safety.
    """
    return (
        len(_SCRIPT_TAG_RE.findall(html)),
        len(_EVENT_HANDLER_RE.findall(html)),
        len(_CDN_LINK_RE.findall(html)),
    )
```

### Step B — single emit helper
```python
_HTML_CONTENT_TYPE = "text/html; charset=utf-8"

def _emit_gate_decision(
    *, trace_id, session_id, user_id, artifact_id, slug, content_type,
    size_bytes, decision, commit_path, gate_ran, violations,
) -> None:
    """Emit the per-commit sandbox gate-decision label (FRE-506, ADR-0089 D5).

    Observation only — never a security verdict. A ``bypassed`` decision is the
    alarm signal that a commit path ran no gate.
    """
    s, h, c = violations
    log.info(
        "artifact_gate_decision",
        trace_id=trace_id, session_id=session_id, user_id=str(user_id) if user_id else None,
        # rejected path has no artifact_id yet → emit null, never the string "None"
        # (codex review: downstream presence filters must not match a literal).
        artifact_id=str(artifact_id) if artifact_id else None, slug=slug, content_type=content_type,
        size_bytes=size_bytes, gate_decision=decision, commit_path=commit_path,
        gate_ran=gate_ran, script_count=s, handler_count=h, cdn_count=c,
    )
```

### Step C — `artifact_write_executor`: emit on every commit + accept gate provenance
- Add **keyword-only, internal** params (NOT in the `ToolDefinition` schema, so the model cannot set them):
  `_gate_decision: str | None = None`, `_gate_violations: tuple[int, int, int] | None = None`, `_commit_path: str = "direct_write"`.
- After the Postgres commit + `artifact_write_committed` log (~`:439`), classify and emit exactly once:
  - if `_gate_decision` is not None → `decision=_gate_decision`, `violations=_gate_violations or (0,0,0)`, `commit_path=_commit_path`, `gate_ran=True`.
  - elif `content_type == _HTML_CONTENT_TYPE` → `violations=_count_sandbox_violations(content)`, `decision="bypassed"`, `commit_path="direct_write"`, `gate_ran=False`.
  - else → `decision="not_applicable"`, `violations=(0,0,0)`, `gate_ran=False`.
- Add a short comment at the top of the function: *artifact_write is intentionally an ungated commit path (ADR-0089 D1 retires content-inspection-as-security); the served-CSP envelope is the boundary (D2/D3, FRE-509). We emit `bypassed` for visibility, not enforcement.*

> `content` is already in scope (decoded into `payload`; we count on the raw `content` string, which is the HTML text for the html type).

### Step D — `artifact_draft_executor`: pass the gate decision through; emit `rejected` on terminal
- Just before the final strip (`:1493`), capture `pre_strip_violations = _count_sandbox_violations(html_content)`.
- After `_validate_html_output(html_content)` passes (`:1507`), compute `draft_decision = "stripped" if sanitize_notes else "enforced_pass"`.
- Pass through to the chained write call (`:1518`): `_gate_decision=draft_decision, _gate_violations=pre_strip_violations, _commit_path="draft"`.
- Wrap `_validate_html_output(html_content)` so a sandbox `TerminalToolError` emits `gate_decision="rejected"` (with `pre_strip_violations`, `gate_ran=True`, no `artifact_id` yet) **before** re-raising. Malformation `ToolExecutionError` is **not** wrapped (not a sandbox verdict).

No other files change. No behavior changes except the added telemetry (and the new internal kwargs, which are inert for existing callers).

---

## Tests (TDD — write first, watch fail, then implement) — `tests/personal_agent/tools/test_artifact_tools.py`

Use the existing `_install_fakes` fixture + `structlog.testing.capture_logs()` (already the repo pattern). Helper: `_gate_events(logs) = [e for e in logs if e.get("event") == "artifact_gate_decision"]`.

1. `test_gate_decision_bypassed_on_direct_html_write` — `artifact_write_executor` with `content_type=text/html` containing `<script>…</script>` + `onclick="x()"` → exactly one gate event, `gate_decision="bypassed"`, `commit_path="direct_write"`, `gate_ran=False`, `script_count==1`, `handler_count>=1`.
2. `test_gate_decision_not_applicable_for_non_html` — direct `artifact_write` of `application/json` → `gate_decision="not_applicable"`, counts all 0.
3. `test_gate_decision_enforced_pass_clean_draft` — `artifact_draft_executor` with a fake sub-agent client returning a clean DOCTYPE…`</html>` doc → gate event `gate_decision="enforced_pass"`, `commit_path="draft"`, `gate_ran=True`.
4. `test_gate_decision_stripped_when_draft_sanitizes` — fake sub-agent returns a doc with an inline `onclick` (kept through to the final strip) → `gate_decision="stripped"`, `handler_count>=1`.
5. `test_gate_decision_rejected_on_terminal_sandbox_violation` — force `_validate_html_output` to raise `TerminalToolError` (monkeypatch a doc the stripper can't clear, e.g. literal `onclick=` in body text) → gate event `gate_decision="rejected"`, `gate_ran=True`, and the `TerminalToolError` still propagates; **no** `artifact_write_committed`.
6. `test_direct_write_emits_single_gate_event` — exactly one `artifact_gate_decision` per commit (no double-emit from the draft→write chain in tests 3/4).

Existing tests must stay green (the new kwargs default to None; existing call sites unchanged).

---

## Quality gates (all before PR)
`make test-file FILE=tests/personal_agent/tools/test_artifact_tools.py` → then `make test` · `make mypy` · `make ruff-check` + `make ruff-format` · `pre-commit run --all-files`.

## Verify (ticket STEP 3)
- Unit: tests 1–6 green. Decision visible incl. `bypassed`.
- A forced violation is visible in telemetry on whichever path it takes (test 1 = direct-write `bypassed` with counts; test 5 = draft `rejected`).

## Out of scope (named, not done here)
- Serving the CSP envelope on the Worker (FRE-509, infra repo). Served-response envelope-integrity tests (FRE-512). Removing the sanitizer from the security path (ADR-0089 D1 — a later FRE-508/512 step). This ticket is **visibility only**; it removes/relaxes nothing.

## Follow-ups to file (Needs Approval, Observability Foundation)
- None anticipated beyond the already-tracked FRE-509/FRE-512. If the owner prefers pure envelope-vocabulary now, that becomes a scope change on this ticket rather than a new one.
