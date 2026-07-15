# FRE-886: Default attachment processing (Auto) to cloud/Sonnet — images and PDFs

**Backing:** ADR-0101 §8a (images), ADR-0102 §7a (documents) — processing-target routing seam.
**Branch:** `fre-886-default-attachment-cloud-auto`

## Scope

Today, an attachment with no per-attachment override (`processing_target: None`, the "Auto" case)
resolves by **following the bound ExecutionProfile**: a local-profile conversation with a
vision-capable local Qwen never crosses to cloud, even though the owner has observed cloud Sonnet
reads scanned pages materially better. Change the Auto default to route straight to the profile's
cloud escalation model (Sonnet) for both images and PDFs, config-driven so it can be flipped back.

## Acceptance criteria (pulled from ticket)

1. **AC1** — An image with no override (Auto) routes to Sonnet (escalated true), read by cloud model.
2. **AC2** — A PDF with no override does the same via the native PDF document block.
3. **AC3** — A per-attachment `"local"` override still forces local Qwen, no cloud crossing.
4. **AC4** — The $0.50 cost gate (`attachment_cost_confirmation_threshold_usd`) still holds an
   over-threshold turn.
5. **AC5** — The default is config-driven; verified by flipping it back to `"local"` in a test
   (recovers the pre-FRE-886 profile-following behavior).
6. **AC6** — The PWA chip communicates that Auto uses the cloud path.

## Design

Add one new setting, `attachment_default_processing_target: Literal["cloud", "local"]`, default
`"cloud"`. **Codex plan-review flagged that this file's existing plain-`str` mode-setting convention
(`skill_routing_mode`) would let a typo (e.g. `"cluod"`) silently fall through to the profile-following
branch — a discriminated union is the project's own stated convention and closes that hole, so `Literal`
wins over matching the local file convention here.** In both routing resolvers, when there is no
per-attachment override (`effective_target is None`) and the setting is `"cloud"`, **fold into the
existing explicit-`"cloud"`-override branch** (reuse, don't duplicate) instead of the profile-following
branch. When the setting is `"local"`, fall through to the existing profile-following branch unchanged
(this is the pre-FRE-886 behavior, and is how AC5's "flip back" is verified).

This means: when misconfigured (no `escalation_model` on the active profile — never true for
`local.yaml`/`cloud.yaml` today, both already set `escalation_model: claude_sonnet`), Auto fails
closed with `AttachmentUnsupportedError` rather than silently falling back to local. That mirrors the
codebase's existing fail-closed posture for the explicit override and is what "make the default
deliver Sonnet quality" implies — a misconfigured Auto should not silently regress to the
worse-quality path.

The $0.50 cost gate (`_maybe_confirm_attachment_cost`) calls `_effective_attachment_routing_key` →
`_resolve_vision_routing_key`, so it automatically sees the cloud-routed key for Auto attachments and
gates cost identically to an explicit `"cloud"` override — no changes needed there (verified by
reading `executor.py:1827-1899`). **Codex plan-review flagged this as a material side-effect worth
calling out explicitly (not a gap in the design, but easy to miss in review): Auto attachments will now
trigger the $0.50 confirmation prompt in cases that previously routed silently to free local Qwen.**
This is exactly AC4's intent (the cost gate must still hold an over-threshold turn) but is a real
user-facing behavior change worth flagging in the PR/handoff.

**Codex review (second opinion) confirmed:** the fail-closed-without-`escalation_model` tightening is
the right call (not a bug) — keep it, and add a test for it rather than adding a graceful local
fallback. It also corrected one detail in the test-impact analysis below:
`test_ac4_capable_primary_no_override_proceeds` breaks because it sets **no profile at all**
(`get_current_profile()` returns `None`), not merely because its synthetic profile lacks an
escalation model — the planned fix (monkeypatch the setting to `"local"`) still resolves this
correctly either way, since the pre-FRE-886 branch handles `profile is None` by returning
`resolve_model_key(role_name)` unchanged.

## Files touched

### 1. `src/personal_agent/config/settings.py`

Add `from typing import Literal` to the module's imports (not currently imported). Add after
`attachment_cost_confirmation_threshold_usd` (~line 855):

```python
attachment_default_processing_target: Literal["cloud", "local"] = Field(
    default="cloud",
    alias="AGENT_ATTACHMENT_DEFAULT_PROCESSING_TARGET",
    description=(
        "Effective processing_target for an attachment with no per-attachment "
        "override ('Auto') — images (ADR-0101 SS8a) and PDFs (ADR-0102 SS7a). "
        "'cloud' (default, FRE-886): Auto routes straight to the profile's cloud "
        "escalation model, same as an explicit 'cloud' override (cost-gated) — "
        "the local Qwen vision path produced a materially worse read than cloud "
        "Sonnet on a live scanned-page test. 'local' restores the pre-FRE-886 "
        "default: resolve the profile's own model, escalating only if the "
        "profile's allow_cloud_escalation permits. "
        "Env var: AGENT_ATTACHMENT_DEFAULT_PROCESSING_TARGET"
    ),
)
```

(Use `SS` as a stand-in above only because this plan file can't render the section-sign; write the
literal `§` character in the actual source.) `Literal` over plain `str` per codex review — see Design.

### 1b. `tests/test_config/test_settings.py`

Add a new `TestAttachmentDefaultProcessingTarget` class (near `TestAttachmentGuardrailCaps` /
`TestDocumentGuardrailCaps`, ~line 219-240), following the file's existing pattern
(`TestEntityExtractionFewshotFlag`, ~line 243):

- `test_default_is_cloud` — `AppConfig().attachment_default_processing_target == "cloud"`.
- `test_reads_from_env` — `monkeypatch.setenv("AGENT_ATTACHMENT_DEFAULT_PROCESSING_TARGET", "local")`
  → `AppConfig().attachment_default_processing_target == "local"`.
- `test_rejects_invalid_value` — `monkeypatch.setenv(..., "bogus")` →
  `pytest.raises(ValidationError): AppConfig()` (matches the `ValidationError` pattern at
  `test_settings.py:177-185`).

### 2. `src/personal_agent/orchestrator/executor.py`

**`_resolve_vision_routing_key`** (~line 1642-1644) — after computing `effective_target`, before the
`if effective_target == "local":` check:

```python
if effective_target is None and settings.attachment_default_processing_target == "cloud":
    effective_target = "cloud"
```

**`_resolve_document_routing_key`** (~line 1748-1750) — identical two-line addition, same placement
relative to its `effective_target` computation.

Update both functions' docstrings: replace the bare `"# No override — follow the profile default
(§5)."` / `"# No override — follow the profile default."` comments with a one-line note that this
branch now only runs when `attachment_default_processing_target == "local"` (FRE-886).

### 3. `tests/test_orchestrator/test_routing.py`

**Existing tests that break** (all currently assert the old "Auto → profile-follow" behavior with no
`processing_target` set, and none of their synthetic profiles configure `escalation_model` — so under
the new default they'd hit the fail-closed cloud branch instead):

- `test_ac4_capable_primary_no_override_proceeds`
- `test_ac6_native_pdf_capable_primary_no_override_proceeds`
- `test_ac6_vision_only_primary_falls_back_to_rasterize`
- `test_mixed_image_and_document_prefers_native_pdf_when_both_supported`

Fix: add a `monkeypatch: pytest.MonkeyPatch` parameter to each and
`monkeypatch.setattr(settings, "attachment_default_processing_target", "local")` at the top of the
test body, with a one-line docstring addition noting this now exercises the AC5 flip-back path.
(`settings` is the same singleton `executor.py` reads, imported via
`from personal_agent.config import settings` — confirm this import exists in the test file already or
add it.)

**Tests confirmed unaffected** (traced by hand — they converge to the same result either way because
their scenarios already have an `escalation_model` configured and/or already expect the escalation
outcome): `test_ac4_incapable_primary_escalation_permitted_escalates`,
`test_ac4_incapable_primary_escalation_forbidden_raises`,
`test_ac6_incapable_primary_escalation_permitted_escalates_to_native_pdf`,
`test_ac6_primary_and_escalation_both_non_capable_fails_closed`,
`test_mixed_image_and_document_requires_vision_even_if_pdf_native_capable`. Leave as-is; re-verify
with the full suite run in Step 4.

**New tests** (default config, i.e. no monkeypatch — `"cloud"` is the default):

- `test_default_cloud_no_override_routes_to_escalation_model_image` — capable local `primary` +
  profile with `escalation_model="claude_sonnet"` configured (mirrors real `local.yaml`) → assert
  `_resolve_vision_routing_key(ctx, "primary") == "claude_sonnet"` (NOT `"primary"`), proving Auto
  bypasses a perfectly vision-capable local model in favor of cloud (AC1, unit level).
- `test_default_cloud_no_override_routes_to_escalation_model_native_pdf` — same shape for
  `_resolve_document_routing_key`, asserting `("claude_sonnet", "native_pdf")` (AC2).
- `test_default_cloud_no_override_fails_closed_without_escalation_model` — Auto + a profile with no
  `escalation_model` configured → `AttachmentUnsupportedError` (documents the fail-closed tightening;
  matches AC5's "config-driven" requirement by proving the default's failure mode is intentional, not
  silent). Codex plan-review confirmed this tightening is correct, not a bug — keep it.
- `test_default_cloud_no_override_mixed_image_and_document_routes_to_escalation_model` — a PDF +
  image, both with no override, profile with `escalation_model` configured whose model supports both
  `supports_pdf_document` and `supports_vision` → assert `_resolve_document_routing_key` returns
  `(escalation_key, "native_pdf")`, covering the combined-capability predicate (`_capable()`,
  `executor.py:1730-1746`) under the new default, not just the single-attachment case. Added per codex
  plan-review (document/mixed coverage was under-specified in the first pass).

### 4. `docs/architecture_decisions/ADR-0101-agent-vision-ingestion.md` and `ADR-0102-document-ingestion.md`

Add a dated "Status Updates" entry to each (matching the existing trailer format) noting: FRE-886
changed §8a/§7a's documented Auto default from "follow the profile" to "route to the profile's cloud
escalation model," config-driven via `attachment_default_processing_target` (default `"cloud"`), owner
-requested after the AC-SEAM live smoke exposed a quality gap.

### 5. `seshat-pwa/src/components/ChatInput.tsx`

- Header JSDoc (~line 36, 54-55): note Auto now defaults to cloud.
- Auto-state button text (~line 313): `'Auto'` → `'Auto (Cloud)'`.
- Auto-state `aria-label` (~line 308-310) and `title` (~line 311): extend to mention the cloud
  default, e.g. title `"Cycle per-attachment processing target (Auto → Cloud by default / Cloud /
  Local)"`. Confirmed existing tests use non-anchored regex (`/currently Auto/`) so appending text is
  safe — verified against `seshat-pwa/src/__tests__/ChatInput.test.tsx` (no exact-text assertions on
  the bare word "Auto").

### 6. `seshat-pwa/src/__tests__/ChatInput.test.tsx`

Add one assertion (e.g. in the first "cycles Auto -> Cloud" test) that the Auto-state chip text
communicates the cloud default, e.g. `expect(screen.getByText(/Auto.*Cloud/)).toBeDefined()` before
the first click.

## Test commands

```bash
make test-file FILE=tests/test_orchestrator/test_routing.py
make test-file FILE=tests/personal_agent/orchestrator/test_attachment_cost_gate.py
make test-file FILE=tests/test_orchestrator/test_executor.py
make test-file FILE=tests/test_orchestrator/test_document_continuation.py
make test   # full suite
make mypy
make ruff-check
cd seshat-pwa && npm run lint && npm test -- ChatInput
```

## Security review finding (addressed)

Security review flagged (confidence 9/10): reusing the explicit-`"cloud"`-override branch for the
Auto default means `profile.delegation.allow_cloud_escalation` no longer gates whether an Auto
attachment crosses to cloud on the `local` profile (`allow_cloud_escalation: false`) — only
`escalation_model` being configured matters now. Verified this is the ticket's explicit intent (owner
wants Auto to reach cloud "instead of following the local profile default", specifically to fix the
local-profile quality gap the ticket describes) and confirmed `allow_cloud_escalation` has no other
call site in the codebase besides these two attachment-routing functions (`grep` — only
`config/profile.py` definition + `executor.py:1694,1803` reads). Addressed by: (1) a stronger inline
comment at both fold-in sites in `executor.py` stating explicitly that this supersedes
`allow_cloud_escalation` for attachments, and (2) fixing the now-stale comment in
`config/profiles/local.yaml` that previously asserted "no implicit/default escalation happens" — that
invariant is exactly what FRE-886 intentionally changes.

## Considered and declined

Codex plan-review suggested adding a log/trace field distinguishing "routed via explicit override" vs
"routed via config default" for observability. Declined for this ticket: none of the six ACs ask for
it, and it's not needed to make the Auto→cloud behavior work — it would be scope creep beyond what the
ticket requires (CLAUDE.md Simplicity First). Noted here so it isn't silently lost if wanted later.

The high-effort code-review workflow flagged (cleanup, not correctness) that the PWA chip's "Auto
(Cloud)" label is a static string, not a live read of the backend's
`attachment_default_processing_target` setting — if an operator ever flips the env var back to
`"local"`, the chip would say "Cloud" while attachments actually follow the profile. Declined to wire a
new config-exposure endpoint for this: no existing pattern exposes backend `AppConfig` values to the
PWA today (verified — none found), and `attachment_default_processing_target` is an ops-only escape
hatch (deploy-time env var), not a user-facing runtime toggle. Building a sync endpoint for one label
is disproportionate to the ticket's ask. Noted here for whoever next touches this setting.

## Code-review findings addressed

The high-effort code-review workflow (run per Step 8) confirmed 2 correctness findings and 2 cleanup
findings:

1. **CONFIRMED (correctness) — `allow_cloud_escalation` bypass.** Same finding as the security review
   above; addressed via explicit code/config comments (intentional, owner-authorized), not a code
   change.
2. **CONFIRMED (correctness) — no-profile-bound regression.** With no `ExecutionProfile` bound at all
   (e.g. `load_profile` failing in `service/app.py`, which swallows the exception and continues without
   setting a profile), the original fold-in forced `effective_target = "cloud"` even though there's no
   "profile's escalation model" to route to, hard-failing a turn that previously succeeded via the
   profile-independent fallback. **Fixed**: extracted a shared `_apply_default_processing_target`
   helper that only folds into `"cloud"` when a profile is actually bound; added regression tests
   `test_default_cloud_no_profile_bound_falls_back_to_profile_independent_resolution` for both the
   image and document resolvers, and reverted the now-unnecessary test-level workaround in
   `test_content_widening.py::test_vision_routing_decision_log_fires_for_raster_attachment` (verified
   it passes on the source-level fix alone).
3. **CONFIRMED (cleanup) — duplicated fold-in logic.** The same 5-line block was copy-pasted into both
   routing resolvers. **Fixed**: factored into the shared `_apply_default_processing_target` helper
   (also fixes finding #2 in one place instead of two).
4. **CONFIRMED (cleanup) — PWA label doesn't reflect live config.** Declined, documented above.

## Not in scope / explicitly unaffected

- `config/profiles/local.yaml` / `cloud.yaml` — already carry `escalation_model: claude_sonnet`, no
  change needed.
- Cost gate module (`cost_gate/`) and `attachment_cost_confirmation_threshold_usd` — downstream of
  routing, unaffected (traced above).
- Per-attachment explicit `"local"`/`"cloud"` override code paths — untouched, already correct.
