# FRE-953 — ADR-0124 Amendment A: narrow the producer to conversation scope

**Ticket:** FRE-953 (Approved, Tier-1) · **Backing:** ADR-0124 Amendment A (merged PR 638)
**Branch:** `fre-953-adr-0124-amendment-a-producer-scope`

## Objective

Correct the FRE-947 producer (merged, undeployed) per Amendment A: the digest is built from the
**conversation**, not from tool payloads. Remove tool payloads and tool arguments from the assembled
prompt (keep name/status/error metadata); remove payload-fed corrections; keep the self-correction
path; keep the status/error-contradiction path (AC-13 requires it). Flip AC-8 to assert absence.
Rebuild AC-12 positives as ≥8 self-corrections. Re-run AC-13 unchanged.

## Owner-approved design decision (the one real fork)

Amendment A's "remove Tier A / Tier B only" contradicts its own **AC-13** (`status_visible` — narration
denied by tool status/error, no self-correction — must still yield a correction; "either **non-payload**
correction path"). Resolution (owner-approved, Opt 1): the producer keeps **two** correction kinds, both
grounded only in content it actually receives, and the schema names them honestly:

- `self_correction` — the assistant corrected the record within the conversation (was Tier B).
- `status_contradiction` — narration denied by tool **status/error** metadata (the payload-free survivor
  of Tier A; what AC-13 tests).

**Removed:** contradiction resting on a tool **payload** (the verification lane Amendment A scoped out).
Payloads/arguments are no longer fed to the producer at all.

## Changes

### src

1. `memory/session_digest.py`
   - `CorrectionTier = Literal["A","B"]` → `Literal["self_correction","status_contradiction"]`.
   - `_TOOL_FIELD_RE`: drop `output`, keep `error` only — the validator now **refuses payload
     citations** (structural enforcement of "payloads never reach the summariser"; the amendment's
     forward-note prefers this over prompt-builder convention). `resolve_locator` returns `None` for a
     `.output` field. **Fold-in / hardening — flag to master.**
   - Update the tier comment block + `Correction` docstring for the two survivors.

2. `second_brain/session_summary.py`
   - `_tool_block`: render **name, status, error only**. Drop `arguments` and `output`/payload lines and
     their missing-evidence notes.
   - `_SYSTEM_PROMPT`: locator grammar → `user_text | assistant_text | tool_result[N].error`; correction
     `tier` → the two new values; rewrite the CORRECTIONS section (remove payload Tier A; define
     `self_correction` + `status_contradiction`; keep the same-proposition test — the FRE-947 fix that
     killed the c12 judgment-vs-data false positive — re-scoped to `status_contradiction`); keep the
     Tier-C never-assert list.
   - Update module docstring, `build_prompt` docstring, `_neutralise_delimiters` docstring (payload
     path gone; still defuse forged structure in the remaining rendered fields — user/assistant/error),
     and the `_TOKEN_ESTIMATE_SAFETY_FACTOR` comment (input is conversation-only now). Keep the AC-5
     oversize machinery (unaffected).

### tests (TDD — write/adjust first)

3. `tests/personal_agent/second_brain/test_session_summary.py`
   - `test_prompt_input_completeness` (AC-8): assert name/status/error **present**, payload + arguments
     **absent**.
   - delete `test_payload_survives_canonical_serialisation`; add `test_tool_payload_and_arguments_absent`.
   - delete `test_missing_arguments_are_declared_...` (arguments no longer rendered).
   - `test_missing_evidence_notice_does_not_suppress_status_based_corrections`: trigger the notice via a
     turn missing its assistant response (not via missing arguments).
   - `test_forged_turn_delimiters_...`: move hostile content from tool `output` → tool `error`.
   - fix `_valid_output` established item to a non-`.output` citation (basis `assistant_reasoning`).

4. `tests/personal_agent/memory/test_session_digest_validator.py`
   - locator tests: `.output` → `.error`/`assistant_text`; add `test_output_field_no_longer_resolves`.
   - rename the two `tier_a` correction tests → `status_contradiction`, evidence via `.error`.
   - the `tier="B"` correction test → `tier="self_correction"`.

### fixtures + eval

5. `tests/fixtures/session_digest/ac12_corrections.json`: remove a1–a6 (payload Tier-A); rebuild
   positives as **8 self-corrections** whose supporting evidence lives in a **visible** field (tool
   `error` or the assistant's own text) — never a payload; `"tier"` → `self_correction`. Keep the 12
   Tier-C negatives unchanged. **Pre-registered before the arm runs.**
6. `tests/fixtures/session_digest/ac13_missing_evidence.json`: **untouched** ("AC-13 unchanged").
7. `scripts/eval/session_digest_eval.py`: `score_ac8` → assert metadata presence + payload/argument
   absence; drop `score_ac9`/`score_ac10` and their `_SETS`/`scorers` entries (AC-9 withdrawn, AC-10
   deferred to owner redesign — must not be evidenced); `score_ac12` evidence-span key → `self_correction`.
8. delete `ac9_tool_only_facts.json`, `ac10_basis_labelling.json` (withdrawn/invalidated).
9. `tests/fixtures/session_digest/REGISTRY.md`: reflect Amendment A (AC-8 absence, AC-9 withdrawn,
   AC-10 deferred, AC-12 self_correction ≥8, AC-13 unchanged).

### docs

10. `docs/research/2026-07-23-fre-953-...eval.md`: the amended arm's results (AC-8/12/13) + writeup.

## Out of scope (confirmed)

Capture/storage of payloads+arguments (unchanged). D1 scheduling, config/role (AC-14), settings,
`memory/service.py`, `models.py`, `scheduler.py` — untouched.

## Verification

- `make test-file FILE=tests/personal_agent/second_brain/test_session_summary.py` and the two
  `memory/test_session_digest_*` files, then module + full `make test`.
- `make mypy` · `make ruff-check` · `make ruff-format` · `pre-commit run --all-files`.
- Paid eval: `uv run python scripts/eval/session_digest_eval.py --out report.json` (AC-8 offline +
  AC-12 + AC-13; ~25 cloud calls; no substrate writes). Commit report verbatim.

## AC proof map

- **AC-8 (amended):** unit `test_prompt_input_completeness` + `score_ac8` — presence of name/status/error
  AND absence of payload/arguments.
- **AC-12 (amended):** `score_ac12` — 0 Tier-C false positives, ≥80% of 8 self-correction positives fire,
  each with a resolvable evidence span.
- **AC-13 (unchanged):** `score_ac13` — payload_absent silent, status_visible + self_correction fire.
