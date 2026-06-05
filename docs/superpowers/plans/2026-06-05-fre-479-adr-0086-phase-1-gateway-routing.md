# FRE-479 — ADR-0086 Phase 1: Gateway routing (unpin TOOL_USE complexity + artifact-build sub-signal + matrix branch)

**Ticket:** FRE-479 (Approved, Tier-2:Sonnet) · parent FRE-476 · project *Turn Cost & Latency Optimization (artifact builds)*
**ADR:** ADR-0086 D1+D2 (`docs/architecture_decisions/ADR-0086-hybrid-decompose-routing-for-artifact-builds.md`)
**Research:** `docs/research/2026-06-04-artifact-turn-cost-latency-forensics.md` §6
**Scope:** deterministic gateway change only — `request_gateway/intent.py` (D1) + `request_gateway/decomposition.py` (D2) + a default-off rollout flag in `config/settings.py` (D7 guard, owner-approved scope nudge). No executor/sub-agent work (Phase 2 / FRE-480); Phase 3 (FRE-481) owns telemetry/joinability/A/B/rollout.

---

## Goal

1. **D1 (intent.py):** stop hard-pinning `complexity = SIMPLE` for `TOOL_USE`. Factor the artifact-build alternation out of `_TOOL_INTENT_PATTERNS` into a shared `_ARTIFACT_BUILD_PATTERNS`. When an artifact build is detected, append an `artifact_build` signal and floor complexity at `MODERATE` (allowing `COMPLEX` when the message's own heuristics already reach it). Plain tool turns run through `_estimate_complexity` and short single-action lookups resolve to `SIMPLE` (no regression).
2. **D2 (decomposition.py):** replace the unconditional `TOOL_USE → SINGLE, "tool_use_single"` with a complexity branch mirroring `ANALYSIS`/`PLANNING`. Resource-pressure guard untouched.

## Inertness — code-level flag gate (Option A, owner-approved)

ADR-0086 §D7 requires D1/D2 to ship behind `artifact_decomposition_enabled` (default **off**), and §D7 Rollback says setting it false "fully restores the current `TOOL_USE → SINGLE` path." We honour that in Phase 1 rather than relying on a deploy-discipline gate:

- Add `artifact_decomposition_enabled: bool = Field(default=False, …)` to `config/settings.py`.
- `decomposition.py` gates **only the strategy**: flag off → legacy `SINGLE, "tool_use_single"` (byte-for-byte today's routing); flag on → the new complexity branch.
- `intent.py` always computes the new complexity and appends the `artifact_build` signal — telemetry only. Verified: `intent.complexity` is consumed behaviorally **only** by `decomposition._apply_matrix`; every other reference (`pipeline.py`, `executor.py:1649`, context assembly) is a `logger.info` field or passes the whole `intent` object without reading complexity. So emitting MODERATE/COMPLEX with the flag off changes **no routing or budget behavior** — it just lets Phase 3 measure the "would-fire" rate from logs before enabling.

Net: Phase 1 is inert in prod by construction (default-off flag), the rollback lever exists from this phase forward, and Phase 3 (FRE-481) wires the same flag into telemetry/joinability and owns the enable decision.

---

## Step 1 — Tests first (intent.py, D1)

File: `tests/personal_agent/request_gateway/test_intent.py`

In `TestArtifactBuild`, add complexity assertions:

- New test `test_artifact_build_floors_at_moderate`: parametrized over the existing short artifact-build fixtures → assert `result.complexity == Complexity.MODERATE` and `"artifact_build" in result.signals`.
- New test `test_artifact_build_allows_complex`: message that estimates COMPLEX via ≥3 questions AND matches artifact build, e.g.
  `"Build an interactive HTML dashboard. What sections should it have? How should I structure the data? Which charts work best?"`
  → assert `result.complexity == Complexity.COMPLEX` and `"artifact_build" in result.signals`.

In `TestToolUse`, add `test_plain_tool_lookups_stay_simple`: parametrized over the ADR's named plain lookups → assert `Complexity.SIMPLE` and `"artifact_build" not in result.signals`:
- `"Search for files matching *.py"`
- `"Read the config file"`
- `"Check ES health"`

Run: `make test-file FILE=tests/personal_agent/test_intent.py` … actually:
`uv run pytest tests/personal_agent/request_gateway/test_intent.py -q` — confirm the new tests **fail** (artifact builds currently pin SIMPLE; no `artifact_build` signal).

## Step 2 — Implement D1 (intent.py)

`src/personal_agent/request_gateway/intent.py`:

1. Before `_TOOL_INTENT_PATTERNS`, extract the artifact-build alternation (currently lines 110–115) into a shared regex string + compiled pattern:

```python
# Artifact / build intent (FRE-469): "build me an interactive HTML guide",
# "create a dashboard". Shared between _TOOL_INTENT_PATTERNS (so such requests
# route to TOOL_USE not CONVERSATIONAL) and the complexity sub-signal (FRE-479 /
# ADR-0086 D1). Precedent: FRE-256 _TOOL_INTENT_PATTERNS extension.
_ARTIFACT_BUILD_REGEX: str = (
    r"(?:(?:build|make|create|generate)\s+"
    r"(?:me\s+|us\s+)?(?:an?\s+|the\s+|some\s+)?"
    r"(?:\w+\s+){0,3}?"
    r"(?:guide|web\s*page|web\s*site|page|website|dashboard|chart|graph|"
    r"diagram|artifact|visuali[sz]ation|infographic|mock-?up|prototype|"
    r"widget|report|slide\s*show|presentation|app|html|svg|interactive))"
)

_ARTIFACT_BUILD_PATTERNS: re.Pattern[str] = re.compile(r"(?i)" + _ARTIFACT_BUILD_REGEX)
```

2. In `_TOOL_INTENT_PATTERNS`, replace the inline artifact alternation (lines 105–115) with a reference to the shared regex by appending `r"|" + _ARTIFACT_BUILD_REGEX` to the compiled string (keep the explanatory comment).

3. In the TOOL_USE branch (currently lines 304–315), replace `complexity = Complexity.SIMPLE` with:

```python
    # 6. Tool use
    if _TOOL_INTENT_PATTERNS.search(user_message):
        signals.append("tool_intent_pattern")
        task_type = TaskType.TOOL_USE
        confidence = 0.8
        if _ARTIFACT_BUILD_PATTERNS.search(user_message):
            # ADR-0086 D1: artifact builds bias complexity up to a MODERATE floor,
            # allowing COMPLEX when the message's own heuristics already reach it.
            signals.append("artifact_build")
            estimated = _estimate_complexity(user_message, task_type)
            complexity = (
                estimated if estimated is Complexity.COMPLEX else Complexity.MODERATE
            )
        else:
            # Plain tool turns: short single-action lookups resolve to SIMPLE,
            # preserving SINGLE routing (FRE-256/210/469 no-regression guarantee).
            complexity = _estimate_complexity(user_message, task_type)
        return IntentResult(
            task_type=task_type,
            complexity=complexity,
            confidence=confidence,
            signals=signals,
        )
```

Run: `uv run pytest tests/personal_agent/request_gateway/test_intent.py -q` — all green.

## Step 3 — Add the flag (settings.py)

`src/personal_agent/config/settings.py`, in the expansion controller block (near `orchestration_mode`, ~line 434):

```python
    artifact_decomposition_enabled: bool = Field(
        default=False,
        description=(
            "ADR-0086 rollout flag. When False (default), TOOL_USE turns route "
            "to SINGLE (legacy). When True, high-complexity artifact builds route "
            "to HYBRID for tool-using discovery decomposition. Off until FRE-480 "
            "(sub-agent loop) + FRE-481 (telemetry/A/B) land. Rollback = set False."
        ),
    )
```

## Step 4 — Tests first (decomposition.py, D2)

File: `tests/personal_agent/request_gateway/test_decomposition.py`. Import `from personal_agent.config import settings`.

Rewrite `TestToolUseSingle` → `TestToolUseMatrix`. **Flag-off (default)** — preserves legacy routing:
- `test_tool_use_simple_flag_off_single`, `_moderate_flag_off_single`, `_complex_flag_off_single`: each → `SINGLE`, reason `"tool_use_single"` (no monkeypatch; default is False).

**Flag-on** (`monkeypatch.setattr(settings, "artifact_decomposition_enabled", True)`):
- `test_tool_use_simple_is_single`: `(TOOL_USE, SIMPLE)` → `SINGLE`, reason `"tool_use_simple_single"`.
- `test_tool_use_moderate_is_hybrid`: `(TOOL_USE, MODERATE)` → `HYBRID`, reason `"tool_use_moderate_hybrid"`.
- `test_tool_use_complex_is_hybrid`: `(TOOL_USE, COMPLEX)` → `HYBRID`, reason `"tool_use_complex_hybrid"`.
- `test_tool_use_complex_under_pressure_forces_single`: flag on, `(TOOL_USE, COMPLEX)` with `expansion_budget=0` → `SINGLE`/`"zero_budget"`; with `expansion_permitted=False` → `SINGLE`/`"expansion_denied"` (guard precedes the matrix).

Run: `uv run pytest tests/personal_agent/request_gateway/test_decomposition.py -q` — confirm flag-on matrix tests **fail** (current code returns SINGLE/`tool_use_single` unconditionally); flag-off tests pass.

## Step 5 — Implement D2 (decomposition.py)

`src/personal_agent/request_gateway/decomposition.py`: import `from personal_agent.config import settings`; read the flag in `assess_decomposition` and thread it into `_apply_matrix` (keeps `_apply_matrix` a pure function of its args). Replace the `TaskType.TOOL_USE` case:

```python
        case TaskType.TOOL_USE:
            # ADR-0086 D2: gated branch. Flag off (default) preserves the legacy
            # SINGLE path so Phase 1 is inert in prod until FRE-480/481 land.
            if not artifact_decomposition_enabled:
                return DecompositionStrategy.SINGLE, "tool_use_single"
            match complexity:
                case Complexity.SIMPLE:
                    return DecompositionStrategy.SINGLE, "tool_use_simple_single"
                case Complexity.MODERATE:
                    return DecompositionStrategy.HYBRID, "tool_use_moderate_hybrid"
                case _:
                    return DecompositionStrategy.HYBRID, "tool_use_complex_hybrid"
```

`_apply_matrix` signature gains `*, artifact_decomposition_enabled: bool`; `assess_decomposition` passes `settings.artifact_decomposition_enabled`.

Run: `uv run pytest tests/personal_agent/request_gateway/test_decomposition.py -q` — all green.

## Step 6 — Module regression + full suite

- `uv run pytest tests/personal_agent/request_gateway/ -q` (intent, decomposition, pipeline, types — confirm no collateral).
- `make test` (full fast suite).

## Step 7 — Docs

- `docs/research/EVALUATION_DATASET.md:243` — **no change needed**: with the flag off (default, the state the dataset describes), Turn 1 still emits `strategy=single`, `reason=tool_use_single`. The legacy reason string is preserved by the flag-off branch. Confirm during implementation; touch only if the dataset assumes flag-on.
- No ADR status change (ADR-0086 is multi-phase; stays Proposed until the chain ships — master owns status).

## Step 8 — Quality gates

`make mypy` · `make ruff-check` · `make ruff-format` · `pre-commit run --all-files`.

## Step 9 — PR, then STOP

Open PR via template, pre-merge checklist only. Push `worktree-build`. Do not merge/deploy/close/edit MASTER_PLAN.

---

## Risk / regression analysis

- **Flag off = byte-for-byte legacy routing.** The `tool_use_single` reason string and `TOOL_USE → SINGLE` strategy are preserved verbatim by the flag-off branch, so the only code reference to `tool_use_single` (grep-confirmed: this one line) keeps producing the same output in prod. Docs (ADR, research, EVALUATION_DATASET, ADR-0082 stats table) need no edits.
- **No pipeline/integration test** asserts `TOOL_USE → SINGLE` (grep-confirmed; pipeline SINGLE assertions are conversational + zero_budget). The flag defaults off in tests too, so `make test` exercises legacy routing unless a test opts in via monkeypatch.
- **Multi-sentence plain tool turns** (e.g. the existing `"Local test. Run a health check…"` fixture) estimate `MODERATE` and would route `HYBRID` *only when the flag is on*. Flag off (prod default) they stay `SINGLE`. The new `intent.py` complexity is telemetry-only until enabled; existing fixtures assert `task_type` only, so nothing breaks.
- **MODERATE floor sizing (per codex review).** `_estimate_complexity` *can* yield MODERATE for TOOL_USE via `word_count>40` / `sentence_count>3` / `question_count>=2` / `action_verb_count>=2`; the floor is still necessary because **short** artifact-build prompts hit the `word_count<15` SIMPLE early-return before any heuristic fires (`intent.py:179`). MODERATE (not COMPLEX) is the correct floor since MODERATE and COMPLEX both route to HYBRID. For TOOL_USE, `_estimate_complexity` only *reaches* COMPLEX via `question_count>=3` (the action-verb≥3 / word>100 COMPLEX paths are gated to ANALYSIS/PLANNING/DELEGATION), so `test_artifact_build_allows_complex` uses 3 question marks to exercise the COMPLEX-preserving branch.
