# FRE-682 — ADR-0102 T2: Document capability flag + cloud pricing in the cost matrix

**Backing:** ADR-0102 §3, §7b; ADR-0099 (config single-source); ADR-0065 (cost gate); ADR-0101 (capability-routing precedent, `supports_vision`).

## Scope findings (before writing code)

- Cloud Claude pricing (`input_cost_per_token` / `output_cost_per_token`) for `claude_sonnet` and
  `claude_haiku` already exists in both `config/models.yaml` and `config/models.cloud.yaml` (landed
  under FRE-691, the ADR-0101 shared cost spine). No pricing change needed — only a regression-guard
  test that currently doesn't exist for these two roles (only `gpt-5.4-nano`/`gpt-5.4-mini` are covered
  by `test_gpt_cloud_ids_reconcile_config_price` in `tests/personal_agent/llm_client/test_pricing.py`).
- The cost-confirmation threshold already exists as a single-source config value:
  `AppConfig.attachment_cost_confirmation_threshold_usd` (`src/personal_agent/config/settings.py:834`,
  `AGENT_ATTACHMENT_COST_CONFIRMATION_THRESHOLD_USD`), added attachment-type-agnostically by FRE-691 —
  its docstring explicitly names "the pricier ADR-0102 PDF path" as a consumer. No new config value
  needed.
- The only genuinely new work is the `supports_pdf_document` capability flag: it does not exist
  anywhere in the codebase yet (verified via grep).

## Plan

1. **`src/personal_agent/llm_client/models.py`** — add `supports_pdf_document: bool = Field(False, ...)`
   to `ModelDefinition`, docstring mirroring `supports_vision` (ADR-0102 §3: "the model accepts a
   provider-side native PDF document block").
2. **`config/models.yaml`** — add `supports_pdf_document: true` to `claude_sonnet` and `claude_haiku`;
   add `supports_pdf_document: false` (explicit, per ADR-0102 Implementation Notes) to `primary` and
   `sub_agent`, right beside their existing `supports_vision: true` lines, so the composition
   (vision-yes / pdf-document-no) reads clearly in-line.
3. **`config/models.cloud.yaml`** — same four edits (deployed-config parity; FRE-734 taught us these two
   files drift independently).
4. **Tests (TDD, failing first):**
   - `tests/test_config/test_model_loader.py`:
     - `test_supports_pdf_document_defaults_false` / `test_supports_pdf_document_explicit_true` —
       mirror the existing `supports_vision` pair.
     - `TestSupportsPdfDocumentDeployedConfig` — mirrors `TestSupportsVisionDeployedConfig`:
       parametrized over both deployed config files, asserts `claude_sonnet`/`claude_haiku` → `True`
       and `primary`/`sub_agent` → `False`. This is the AC-6/AC-8-enabling capability-flag proof (T4
       consumes this flag; this ticket proves it's set correctly at the config layer).
   - `tests/personal_agent/llm_client/test_pricing.py`:
     - New parametrized test (mirrors `test_gpt_cloud_ids_reconcile_config_price`) loading the real
       `config/models.yaml` and `config/models.cloud.yaml`, asserting `claude_sonnet` and
       `claude_haiku` both carry non-`None` `input_cost_per_token`/`output_cost_per_token` — the
       AC-10 pricing-present half this ticket owns.
5. **No changes to:** `config/models.benchmark-*.yaml` (not part of the deployed-config guard today —
   `supports_vision` was never backfilled there either, same precedent); `orchestrator/executor.py`
   routing (T4's scope); `AttachmentRef` / cost-gate pre-flight estimator (T5's scope).

## Quality gates

`make test-file FILE=tests/test_config/test_model_loader.py` ·
`make test-file FILE=tests/personal_agent/llm_client/test_pricing.py` · `make mypy` ·
`make ruff-check` · `make ruff-format` · `pre-commit run --all-files`.

## Self-classification

Standard (touches `src/` Pydantic schema consumed by cost-relevant capability routing) →
codex plan-review before implementing.
