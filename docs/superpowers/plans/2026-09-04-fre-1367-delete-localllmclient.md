# FRE-1367 — ADR-0141 T4: delete LocalLLMClient across the census

Ticket: https://linear.app/frenchforest/issue/FRE-1367
Backing ADR: `docs/architecture_decisions/ADR-0141-one-llm-dispatch-path.md` (D1, D2, AC-6)
Blocked-by FRE-1366 (T3, concurrency re-homing) — **Done**, PR #1032 merged 2026-09-04. T2
(FRE-1365, local dispatch through LiteLLMClient) is also already merged — confirmed by reading
`tests/personal_agent/llm_client/test_local_via_litellm.py` (812 lines, full AC-9 parity suite)
and `llm_client/factory.py` (placement branch already collapsed, zero `LocalLLMClient` reference).

## Revision history

- **Round 1** (this file's original draft): full census, codex plan-review requested.
- **Round 2** (this version): codex returned "needs plan revision first" with 5 findings. Every
  one investigated and resolved with direct verification (not just codex's say-so) — see
  "Round-2 corrections" below. Proceeding to implementation on this version; the escalated diff
  class (owner `/code-review ultra` before merge) is the safety net for anything still missed.

## Round-2 corrections (codex findings, each independently verified)

1. **ast-grep rule-leak bug (critical, reproduced live).** Placing the two new rule files in
   `.ast-grep/rules/` would make them part of the *global* rule set via `sgconfig.yml`'s
   `ruleDirs`. Reproduced directly: dropped a probe `no-raw-litellm-dispatch`-shaped rule into
   `.ast-grep/rules/` and ran the real `scripts/check_egress_bypass_rules.py` (which does a bare
   `ast-grep scan <target>`, no `--rule` flag) — it immediately flagged `litellm_client.py`'s own
   two legitimate `litellm.acompletion()` calls (lines 1017, 1520). Confirmed the fix: `ast-grep
   scan --rule <path> <target>` scopes *exclusively* to the one named rule regardless of
   `sgconfig.yml` (verified: pointing `--rule` at the unrelated `no-raw-anthropic-client.yml`
   against a file with only a `litellm.acompletion()` call reports zero findings). **Fix:** the
   two new rules live in a new directory, `.ast-grep/llm-dispatch-rules/`, *not* listed in
   `sgconfig.yml`'s `ruleDirs` — invisible to every bare `ast-grep scan` elsewhere, used only via
   this ticket's own script's explicit `--rule` flag.
2. **Parser "blind spot" — not a parser bug, a real string literal.** Re-inspected
   `scripts/eval_04b_occupancy_curve.py` with raw byte tools (not the Read tool, which was
   rendering it misleadingly): `_TOOL_OUTPUT_TEMPLATE = """\` opens at line 85 and closes at line
   210. Everything between — including the `from personal_agent.llm_client.client import
   LocalLLMClient` "import" and `run_turn()`'s `llm_client: LocalLLMClient` "parameter" my
   original census flagged — is **synthetic filler text inside a string constant** simulating a
   large tool-output blob (the file's own docstring: "Simulate a large tool result (~1 800
   tokens) from a file read"). Confirmed via `ast.parse()` (parses fine — it's one big string) and
   by re-running the ast-grep tombstone rule against the file, which correctly reports nothing (as
   it should — the earlier isolated CLI test already proved the pattern ignores string content).
   **Corrected finding 7 entirely: there is no dead code to retype here, and no real
   `LocalLLMClient` reference exists in this file's actual code.** Left as a one-word content edit
   only (see step B.8 below) purely for a strict textual reading of AC-6, not because anything
   depends on it.
3. **Three test-file dispositions, resolved by reading full bodies (not guessed):**
   - `tests/test_security/test_egress_seams.py`'s `TestLlmClientSeam` (seam 1 of ADR-0132's
     7-seam FRE-1147 audit) is **not** redundant with `test_local_via_litellm.py`'s
     `TestEgressGuardOnTheLocalRoute`: that suite always injects `client._egress_guard` directly
     (a test seam bypassing the process-wide singleton), while this test exercises the *production
     default* fallback path (`self._egress_guard or get_domain_guard()` — confirmed present in
     `litellm_client.py`) via a global `monkeypatch.setattr("personal_agent.security.
     get_domain_guard", ...)`. Different code path, real coverage. **Rewrite in place**, not
     delete: swap `LocalLLMClient(base_url=..., ...)` for a direct `LiteLLMClient(model_id=...,
     provider="slm_local", placement=Placement.LOCAL, model_def=ModelDefinition(...,
     endpoint=<the test's base_url>), budget_role="main_inference")`, leaving `_egress_guard`
     unset so it falls through to the monkeypatched global.
   - `tests/personal_agent/llm_client/test_telemetry_parity.py`'s
     `test_local_client_calls_started_with_correct_args` exercises a genuinely distinct edge case
     — `provider is None` (no `providers:` block declared) falling back to `"unknown"` in the
     started-event payload, plus the started-fires/completed-does-not asymmetry on failure. Not
     covered elsewhere (`test_local_via_litellm.py`'s fixtures always declare
     `provider: "slm_local"`). **Rewrite in place**: construct `LiteLLMClient` directly with
     `provider=None`, `placement=Placement.LOCAL`, `model_def=ModelDefinition(id="test-primary",
     endpoint="http://mock-slm.test/v1", context_length=32768, max_concurrency=2,
     default_timeout=60)` (all other `ModelDefinition` fields have defaults — checked the
     Pydantic model), patch `litellm.acompletion` to raise, assert the same started-kwargs.
   - `tests/personal_agent/config/test_catalog_snapshot.py`'s `_capture_concurrency_and_timeouts`
     — read `concurrency.py`'s `_build_controller_from_catalog()` (ADR-0141 D3's actual
     re-homing): it registers providers/models from `load_model_config()` **exactly** mirroring
     what `LocalLLMClient.__init__` used to do (its own docstring says so), so the "concurrency"
     half of the snapshot is unaffected in content, only in *how* it's obtained (the process-wide
     singleton instead of a throwaway client instance). The "timeouts" half is now **genuinely
     redundant**, not just differently-obtained: `litellm_client.py:1427-1428` confirms the
     unified client reads `model_def.default_timeout` directly (no more independent
     `ModelRole`-keyed re-resolution with a hardcoded PRIMARY=60/SUB_AGENT=45 fallback) — the
     exact field `_definition_of()` already captures under dimension 1 (Resolution). The original
     reason for a *separate* timeout dimension (an independent re-resolution path that could
     silently diverge from dimension 1) is architecturally gone. **Rewrite, dropping the
     "timeouts" half, keeping "concurrency":**
     ```python
     def _capture_concurrency() -> dict[str, Any]:
         """Capture semaphore registration from the process-wide controller (ADR-0141 D3).

         Timeout resolution folded into dimension 1 (Resolution): the unified client
         reads model_def.default_timeout directly per-call (litellm_client.py) rather
         than re-resolving an independent ModelRole-keyed map the way LocalLLMClient
         used to — the divergence risk a separate dimension existed to catch is gone.
         """
         from personal_agent.llm_client.concurrency import (
             get_inference_concurrency_controller,
             set_inference_concurrency_controller,
         )

         set_inference_concurrency_controller(None)  # force rebuild from the catalog
         return {"concurrency": get_inference_concurrency_controller().get_status()}
     ```
     Rename `build_snapshot()`'s `"runtime"` key to `"concurrency"`; update the module's
     "four dimensions" docstring to three (Resolution now folds in timeouts, Concurrency,
     Pricing); update `test_catalog_behaviour_matches_golden`'s comparison line and its assertion
     message. **Regenerate the golden** (`uv run python -m
     tests.personal_agent.config.test_catalog_snapshot`) and diff it — per the file's own caution
     comment, confirm only the `"concurrency"`/removed-`"timeouts"` shape changed, nothing in
     `"resolution"`/`"pricing"` drifted from environment differences; hand-fix if it did.
4. **DSPy question resolved.** Read `dspy_adapter.py`: `configure_dspy_lm()` is a standalone
   module-level function, independent of any client class — confirmed the majority of
   `test_dspy_adapter.py`'s ~30 tests already call it directly. Only a `llm_client` fixture
   (`return LocalLLMClient()`) and ~6 call sites using `llm_client.get_dspy_lm(role=...)` (a
   3-line pass-through to `configure_dspy_lm()` that lived on the class) touch the deleted class.
   **Delete** the two tests that are specifically *about* the `get_dspy_lm()` wrapper method
   itself (it no longer exists); **retarget** the other `llm_client.get_dspy_lm(role=X)` call
   sites to `configure_dspy_lm(role=X)` directly (drop the `llm_client` fixture once nothing
   references it). Also fix the file's own header docstring ("Integration tests for DSPy adapter
   and LocalLLMClient.get_dspy_lm()") and module-level `LocalLLMClient` import.
5. **Eval-rater catalog deviation — direction confirmed, framing corrected.** Codex partially
   corrected my reasoning: FRE-1007's mandatory-declaration guard only checks **role-bound**
   catalog entries (`config_guard.py`), so an unbound `gpt-5.4` entry would not actually trip it —
   my original stated reason was wrong. The real reason to avoid adding it stands on different,
   still-solid ground: a correct catalog entry needs trustworthy `context_length`,
   `default_timeout`, pricing, and concurrency metadata I have no measured values for, and
   inventing them is the kind of fabrication this codebase's culture explicitly rejects (measured
   claims, not assumed ones — see `config/models.yaml`'s own comments throughout). **Keep the
   direct-`LiteLLMClient` design**, but this is a real, documented deviation from the ADR's literal
   `get_llm_client_for_key` wording and must be surfaced prominently to master for an explicit
   go/no-go before merge, not silently decided — already planned as an "Open questions" item
   below; strengthened the wording there.
6. **Wording-only fix**: finding (was #3, now folded above) said "zero production call sites
   remain" — true only for the four ADR-named `src/personal_agent/` application modules; the
   sentence read as broader than intended. `scripts/migrate_fre865_entity_class_backfill.py` was
   always correctly handled in section B (never omitted from the plan's actual changes) — just the
   summary sentence was ambiguous. Reworded below.
7. **Census gaps found and added**: `llm_client/factory.py`'s docstring comment at
   `_build_client`'s `Raises:` section ("Before ADR-0141 this fell through to a bare
   `LocalLLMClient()`") and `scripts/study/categorizer.py`'s docstring comment ("Neither
   `LiteLLMClient` nor `LocalLLMClient` expose a native seed passthrough") were both missing from
   the comment-cleanup list — added to step B.9.
8. **`llm_client/__init__.py` census gap**: the plan already said "remove the lazy `__getattr__`
   branch + `__all__` entry" but omitted the `TYPE_CHECKING`-guarded import
   (`from personal_agent.llm_client.client import LocalLLMClient`, inside `if TYPE_CHECKING:`) and
   the stale module docstring ("This module provides the LocalLLMClient..."). Both added to step A.

## Pre-flight findings (unchanged from round 1, still verified correct)

- **`llm_client/client.py` is entirely `LocalLLMClient`** (729 lines, one class) — delete the
  whole file, not an edit.
- **`factory.py`**: zero live references (T2 finished the placement-branch collapse); one
  docstring comment (round-2 addition, see above).
- **Zero live call sites in the four D1-named `src/personal_agent/` application modules**
  (`captains_log/reflection.py`, `memory/service.py`, `second_brain/session_summary.py`,
  `second_brain/entity_extraction.py`) — T2 already migrated them. Everything else with a live
  reference (the migration script, the DSPy prototypes, the eval scripts) is handled explicitly in
  section B; everything else with a *comment-only* mention is handled in section B.9.
- ast-grep mechanics (bare-identifier tombstone pattern; `litellm.acompletion($$$)`/
  `litellm.completion($$$)` call pattern; `--globs` exemption) verified directly against the real
  CLI (see round-2 correction 1's reproduction, and the original round-1 isolated tests). A
  repo-wide grep for the literal call form `litellm\.a?completion\(` (not docstring prose)
  confirms zero call sites outside `llm_client/` other than the two eval scripts this ticket
  migrates — so the new rule's target scope (`src/`, `scripts/`, `experiments/`, `tests/`) has no
  false positives once B's migrations land.
- **FRE-1262's existing guard** (`tests/observability/topology/test_ci_teeth.py`,
  `_sdk_import_offenders`) is a different, narrower mechanism (bare `import litellm` detection,
  scoped only to `src/personal_agent/`) — additive, not redundant, with this ticket's new
  call-level, wider-scope rule.

## Changes

### A. Delete the class

1. **Delete** `src/personal_agent/llm_client/client.py` entirely.
2. **`src/personal_agent/llm_client/__init__.py`**: remove the `LocalLLMClient` branch from the
   lazy `__getattr__`, drop `"LocalLLMClient"` from `__all__`, remove the `TYPE_CHECKING`-guarded
   `from personal_agent.llm_client.client import LocalLLMClient` import, and reword the module
   docstring (currently "This module provides the LocalLLMClient for interacting with local LLM
   servers...").

### B. Migrate real call sites

3. **`scripts/migrate_fre865_entity_class_backfill.py`** (~line 447-484): replace the
   `if model_def is not None and placement is not LOCAL: LiteLLMClient(...) else: LocalLLMClient()`
   block with a single `get_llm_client_for_key(role, budget_role="entity_extraction")` call. Drop
   the now-unneeded `Placement`/`LiteLLMClient` imports inside the function; keep
   `load_model_config`/`ModelRole`. Update the docstring's "Deliberately bypasses
   `get_llm_client(role_name=...)`" paragraph to explain why `get_llm_client_for_key` is used
   (same underlying reasoning, now via the door built for exactly this).
4. **`experiments/dspy_prototype/test_case_a_reflection.py`, `test_case_b_router.py`**: replace
   `from personal_agent.llm_client import LocalLLMClient, ModelRole` with
   `from personal_agent.llm_client import ModelRole` +
   `from personal_agent.llm_client.factory import get_llm_client`; replace the
   `LocalLLMClient(base_url=settings.llm_base_url, timeout_seconds=..., max_retries=...)`
   construction with `get_llm_client(role_name="primary")` (both dispatch `ModelRole.PRIMARY`).
   Note: `settings.llm_base_url` is not a real `AppConfig` field (grepped and confirmed) — these
   scripts would already `AttributeError` if run today; the migration removes the dead reference
   as a necessary side effect.
5. **`experiments/dspy_prototype/test_case_c_tools.py`**: same import swap;
   `get_llm_client(role_name="sub_agent")` (dispatches `ModelRole.SUB_AGENT`).
6. **`scripts/eval/fre630_extraction_quality/relabel_v2_types.py` and `relabel_v2_rels.py`**
   (`_call_rater`, identical shape in both): replace the manual `import litellm` +
   `litellm.acompletion(**kwargs)` + manual `api_key=` resolution with:
   ```python
   from personal_agent.llm_client.litellm_client import LiteLLMClient
   from personal_agent.llm_client.types import ModelRole
   from personal_agent.telemetry.trace import SystemTraceContext

   client = LiteLLMClient(
       model_id=rater.model_id,
       provider=rater.provider,
       max_tokens=200,
       budget_role="study",
   )
   kwargs: dict[str, Any] = {}
   if rater.temperature is not None:
       kwargs["temperature"] = rater.temperature
   try:
       response = await client.respond(
           role=ModelRole.STUDY,
           messages=[{"role": "user", "content": prompt}],
           trace_ctx=SystemTraceContext.new("fre630_relabel_v2"),
           **kwargs,
       )
   except Exception as exc:  # noqa: BLE001 — a rater outage is a data point, not a crash
       return RaterResponse(type_label="", rationale="", raw_text="", error=type(exc).__name__)
   text = response.get("content") or ""
   return _parse_rater_response(text)
   ```
   (`relabel_v2_rels.py`'s `RaterResponse` uses `rel_label=""` in the except branch instead of
   `type_label=""` — same shape otherwise.) Drop the manual `settings.anthropic_api_key`/
   `openai_api_key` resolution — `LiteLLMClient` resolves credentials itself from the catalog's
   `providers.{openai,anthropic}.auth_env` (verified both exist in `config/models.yaml` with
   `auth_env` set: `openai_api_key`/`anthropic_api_key` — a straight behavior-preserving
   deletion). Update both files' module docstrings (the "calls litellm.acompletion() DIRECTLY...
   deliberate, called-out exception" paragraphs) to describe the new dispatch instead. Uses
   `budget_role="study"`, matching the existing precedent in `scripts/study/categorizer.py:145`
   for the same kind of one-off research dispatch.
7. **`scripts/eval/fre630_extraction_quality/adr0109_boundary_probe.py`**: no dispatch-code
   change (it only imports `classify_all` from `relabel_v2_types`, never calls litellm itself) —
   update its docstring's now-false "Like relabel_v2_types, this calls litellm.acompletion()
   DIRECTLY" sentence.
8. **`scripts/eval_04b_occupancy_curve.py`**: **no code change** — round-2 correction 2 established
   the `LocalLLMClient` "reference" here is inert text inside a `_TOOL_OUTPUT_TEMPLATE` string
   constant (synthetic ~1800-token filler simulating a tool result), not real code; `run_turn()`
   itself is part of that same string, not a live function. Optional one-word content edit inside
   the string (`LocalLLMClient` → `LiteLLMClient`) for a strict textual AC-6 reading only — no
   functional effect either way, noted as inert in the handoff.
9. **Comment/docstring wording only** — reword every remaining prose mention to drop the literal
   name in: `config/config_guard.py` (2 lines), `orchestrator/executor.py` (3 lines),
   `orchestrator/context_compressor.py` (1), `orchestrator/sub_agent.py` (1),
   `observability/joinability/walk.py` (1), `captains_log/reflection.py` (1),
   `llm_client/models.py` (2), `llm_client/concurrency.py` (3), `llm_client/telemetry.py` (1),
   `llm_client/prompt_identity.py` (2), `llm_client/dspy_adapter.py` (1),
   `llm_client/litellm_client.py` (4), `llm_client/factory.py` (1, round-2 addition),
   `scripts/study/categorizer.py` (1, round-2 addition), `scripts/eval/fre1337_intent_probe/probe.py`
   (1 — also correct its FRE-1343 framing: that local-key-ignoring behavior "dissolves by
   construction" per this ADR, not merely historical color),
   `tests/personal_agent/config/fixtures/reasoning_vocabulary_mismatch/config/models.yaml` (1, a
   YAML comment).

### C. Test suite — rewrite wholesale (D1's own directive)

10. **Delete** `tests/test_llm_client/test_client.py` wholesale.
11. **Port 5 assertions** into `tests/personal_agent/llm_client/test_local_via_litellm.py` (reuses
    its existing `_dispatch()`/`_local_catalog()` harness — no new fixtures needed):
    - `TestSystemPromptAndParams.test_system_prompt_is_prepended_on_the_wire` — pass
      `system_prompt="..."` through `_dispatch()`, assert `body["messages"][0] ==
      {"role": "system", "content": "..."}`.
    - `test_model_default_temperature_reaches_the_wire` — no caller temperature,
      `body["temperature"] == 0.6` (the `_local_catalog()` fixture's declared value).
    - `test_caller_temperature_overrides_the_model_default` — `temperature=0.1` passed,
      `body["temperature"] == 0.1`.
    - `test_response_format_reaches_the_wire` — `response_format={"type": "json_object"}` passed,
      `body["response_format"] == {"type": "json_object"}`.
    - `TestErrorTaxonomy.test_404_maps_to_client_error` — `status=404` →
      `pytest.raises(LLMClientError)` (the generic catch-all branch of
      `_map_local_dispatch_error`, confirmed by reading it — no existing test exercises that final
      `return LLMClientError(...)` line).
    - **Round-2 addition**: extend `TestTelemetryParity.test_failure_emits_the_model_call_error_event`
      with an assertion that the emitted `model_call_error` payload carries `session_id` (the old
      `test_respond_error_logs_session_id` assertion codex caught as otherwise silently dropped).
12. **Comment-only fixes** (no logic change): `tests/test_orchestrator/conftest.py`,
    `tests/personal_agent/llm_client/test_litellm_trailing_role_guard.py`,
    `tests/personal_agent/orchestrator/test_step_planning_events.py`,
    `tests/test_captains_log/test_reflection_manual_fallback_role.py`,
    `tests/personal_agent/cost_gate/test_role_lane_isolation.py`,
    `tests/personal_agent/memory/test_generate_query_paraphrases.py` — reword prose mentions.
13. **Real rewrites, resolved (round-2 corrections 3-4, full detail above — not repeated here)**:
    - `tests/test_security/test_egress_seams.py` — rewrite `TestLlmClientSeam` in place against
      direct `LiteLLMClient` construction (local placement, `_egress_guard` left unset).
    - `tests/personal_agent/llm_client/test_telemetry_parity.py` — rewrite
      `test_local_client_calls_started_with_correct_args` in place against direct `LiteLLMClient`
      construction with `provider=None`.
    - `tests/personal_agent/orchestrator/test_content_widening.py` — **delete** the FRE-1037
      `MagicMock(spec=LocalLLMClient)` hazard test (codex: obsolete, the preceding test already
      covers unified-client relabeling — verify this claim by reading the preceding test before
      deleting, don't take it purely on faith).
    - `tests/personal_agent/config/test_catalog_snapshot.py` — rewrite `_capture_concurrency_and_timeouts`
      → `_capture_concurrency` (drops the now-redundant timeout dimension), update `build_snapshot()`,
      the module docstring's dimension count, and the golden-comparison assertion; regenerate and
      diff the golden file.
    - `tests/test_llm_client/test_dspy_adapter.py` — delete the two `get_dspy_lm()`-specific
      tests; retarget the ~6 other `llm_client.get_dspy_lm(role=X)` call sites to
      `configure_dspy_lm(role=X)` directly; drop the `llm_client` fixture; fix the header
      docstring and import.
    - `tests/evaluation/model_benchmarks.py`, `tests/evaluation/ab_testing.py`,
      `tests/test_llm_client/test_integration.py`, `tests/test_llm_client/benchmark_response_times.py`
      — bare `LocalLLMClient()` → `get_llm_client(role_name="primary")` (mechanical; read each
      call site's surrounding ~10 lines before editing to confirm no other LocalLLMClient-specific
      assumption is embedded).

### D. Confinement — two new ast-grep rules + seeded-negative tests (AC-a)

14. **`.ast-grep/llm-dispatch-rules/no-local-llm-client.yml`** (note: new directory, NOT
    `.ast-grep/rules/` — round-2 correction 1):
    ```yaml
    id: no-local-llm-client
    language: python
    severity: error
    message: >-
      LocalLLMClient was deleted (ADR-0141 D1) — every LLM call, local and cloud,
      dispatches through LiteLLMClient via personal_agent.llm_client.factory.
      A local-only replacement client must not be reintroduced.
    rule:
      pattern: LocalLLMClient
    ```
15. **`.ast-grep/llm-dispatch-rules/no-raw-litellm-dispatch.yml`**:
    ```yaml
    id: no-raw-litellm-dispatch
    language: python
    severity: error
    message: >-
      litellm.acompletion()/litellm.completion() must be confined to llm_client/
      (ADR-0141 AC-6) — dispatch through the factory (get_llm_client /
      get_llm_client_for_key) or LiteLLMClient directly instead.
    rule:
      any:
        - pattern: litellm.acompletion($$$)
        - pattern: litellm.completion($$$)
    ```
16. **New pre-commit hook + script** `scripts/check_llm_dispatch_confinement.py`:
    ```python
    """LLM dispatch confinement — ADR-0141 AC-6 / FRE-1367.

    Two rules: LocalLLMClient cannot be reintroduced anywhere, and litellm's
    acompletion()/completion() are confined to llm_client/. Scoped across
    src/, scripts/, experiments/, tests/ — the ticket's full "executable code"
    surface (grepped: zero real call sites exist outside llm_client/ once the
    two eval-script bypasses are migrated, so this scope has no false positives).

    The two rule files live in .ast-grep/llm-dispatch-rules/, NOT .ast-grep/rules/
    — that directory is in sgconfig.yml's ruleDirs and is auto-loaded by every
    bare `ast-grep scan` elsewhere (notably check_egress_bypass_rules.py), which
    would otherwise flag litellm_client.py's own two legitimate dispatch calls
    (reproduced and confirmed during planning). --rule scopes exclusively to the
    named rule regardless of sgconfig.yml (also verified), so this script's own
    invocations are unaffected by which directory the files live in.
    """

    from __future__ import annotations

    import subprocess
    import sys
    from pathlib import Path

    REPO_ROOT = Path(__file__).resolve().parent.parent
    TARGETS = ["src/personal_agent", "scripts", "experiments", "tests"]
    RULES_DIR = REPO_ROOT / ".ast-grep" / "llm-dispatch-rules"
    TOMBSTONE_RULE = RULES_DIR / "no-local-llm-client.yml"
    DISPATCH_RULE = RULES_DIR / "no-raw-litellm-dispatch.yml"


    def _scan(rule: Path, globs: list[str] | None = None) -> int:
        cmd = ["ast-grep", "scan", "--rule", str(rule)]
        for g in globs or []:
            cmd += ["--globs", g]
        cmd += TARGETS
        return subprocess.run(cmd, cwd=REPO_ROOT, check=False).returncode


    def main() -> int:
        rc1 = _scan(TOMBSTONE_RULE)
        rc2 = _scan(DISPATCH_RULE, globs=["!**/llm_client/**"])
        return rc1 or rc2


    if __name__ == "__main__":
        sys.exit(main())
    ```
    Add to `.pre-commit-config.yaml` mirroring the egress-bypass hook's shape, `files:
    '^(src|scripts|experiments|tests)/.*\.py$'`.
17. **Seeded-negative tests** `tests/test_security/test_llm_dispatch_confinement.py`, mirroring
    `test_bypass_rules.py`'s exact structure (real-tree-is-clean + seeded-fixture-fires), per the
    owner's explicit instruction: **prove each rule fails CI on a seeded violation, run that
    proof, then remove the fixture** — not just assert a clean tree:
    - `TestRealTreeIsClean.test_no_local_llm_client_in_repo` /
      `test_no_raw_litellm_dispatch_outside_llm_client` — run the two `_scan()`-equivalent calls
      against the real tree, assert `returncode == 0`.
    - `TestSeededViolationFires.test_local_llm_client_reference_is_flagged` — a `tmp_path` fixture
      containing `from x import LocalLLMClient`, `ast-grep scan --rule no-local-llm-client.yml
      <fixture>`, assert `returncode != 0` and the rule id in stdout.
    - `test_litellm_acompletion_outside_llm_client_is_flagged` — same pattern for
      `litellm.acompletion(...)` in a fixture NOT under an `llm_client/` dir.
    - `test_litellm_acompletion_inside_llm_client_is_not_flagged` — same call, fixture path
      contains `llm_client/`, `--globs '!**/llm_client/**'` applied — `returncode == 0` (the
      exemption actually works).

### E. Docs

18. **Rewrite `src/personal_agent/llm_client/AGENTS.md`** — currently describes `LocalLLMClient`,
    a nonexistent `client.generate()` method, and an "LM Studio Setup" section. Rewrite to
    describe the unified `LiteLLMClient` + factory (`get_llm_client`/`get_llm_client_for_key`),
    ADR-0141's placement model (local via litellm's OpenAI-compatible route, cloud via native
    providers), the egress guard, and the cost-gate skip for local placement. Update the
    "Structure" file list (no `client.py`); update the "Search" grep example; drop "LM Studio
    Setup" (deployment is llama.cpp/MLX, not LM Studio) and "No API key needed" (false for cloud
    placement, which now shares this client).

## Tests / verification

- `uv run pytest tests/personal_agent/llm_client/ tests/test_llm_client/ tests/test_security/ tests/personal_agent/orchestrator/test_content_widening.py tests/personal_agent/config/test_catalog_snapshot.py tests/personal_agent/cost_gate/test_role_lane_isolation.py tests/test_orchestrator/ tests/personal_agent/memory/test_generate_query_paraphrases.py tests/test_captains_log/ tests/evaluation/ -q`
- `uv run python scripts/check_llm_dispatch_confinement.py` — exit 0 on the real tree.
- `uv run python scripts/check_egress_bypass_rules.py` — exit 0, confirming the new rules did
  **not** leak into the existing egress-bypass scan (the round-2 bug, now structurally prevented).
- `uv run python -m tests.personal_agent.config.test_catalog_snapshot` then `git diff` the golden
  — confirm only the expected shape change, nothing else drifted.
- `make test` · `make mypy` · `make ruff-check` + `make ruff-format` · `pre-commit run --all-files`.

## Open questions to flag to master (not blocking implementation, but real — surfaced, not decided unilaterally)

- **Deviation from the ADR's literal `get_llm_client_for_key` wording** for the two eval-script
  raters — direct `LiteLLMClient` construction instead, because one rater model (`gpt-5.4` full)
  has no catalog entry and fabricating pricing/timeout/concurrency metadata isn't safe without a
  live measurement. Codex reviewed this reasoning and agreed with the outcome while correcting my
  FRE-1007 framing (the guard only checks role-bound entries, so that specific concern was wrong;
  the metadata-fabrication concern stands independently). **Needs explicit master sign-off before
  merge**, not a unilateral call.
- **`test_catalog_snapshot.py`'s golden file changes shape** (drops the "timeouts" dimension,
  folds it into "resolution") — flagging the regenerated golden's diff for master's own read
  before merge, per that file's own documented caution about environment-dependent bytes.

## Diff class

**Escalate.** Deletes a 729-line production class four application modules used until T2's recent
migration, changes the eval scripts' cost/egress posture from unguarded to guarded, and touches
the production LLM dispatch call chain broadly. Codex's review agreed this is mechanically
required by the diff-class criteria (destructive/deleting code + cost/governance-adjacent
behavior change), not lessened by T2/T3 having already done the higher-risk dispatch-path work.
Per Step 6: note in the PR body + handoff "diff class: escalated — flagged for owner
`/code-review ultra` before merge."
