# FRE-919 — ADR-0121 T4: telemetry migration, profile → provider + model

**ADR:** [ADR-0121](../../architecture_decisions/ADR-0121-model-catalog-and-selection-layer.md) §8
("What this supersedes" / D5), Sequencing step 4.
**Linear:** FRE-919, Approved, `stream:build1`, `Tier-2:Sonnet`.
**Depends on:** FRE-917 (T2, merged). **Coordinates with:** ADR-0120 (FRE-898 chain).

## Acceptance criteria this ticket must satisfy

- **AC-8** (ADR-0121): after two turns run on two different selected primary models, the
  cost and telemetry records for each turn carry the provider and model actually used, and
  querying spend grouped by model returns non-zero, correctly-split values. Fails if records
  still carry only a profile dimension, or if the recorded model differs from the
  `model_call_completed` model for that turn.
- **Additionally:** no Kibana panel that previously rendered on the `profile` dimension is
  left blank — each is either re-pointed at provider/model, or deliberately retired with that
  noted.

## Pre-implementation finding (why this plan deviates from the ADR's literal prose)

The ADR's §8 prose describes this step as "`TraceContext.profile` → provider + model" and
implies a **live** ES field that must be retained read-only for one deploy so historical
dashboards don't go blank. Verified against source before writing this plan
([feedback_verify_adr_claims_against_source]):

- `TraceContext.profile` (`telemetry/trace.py:50`) is **never read outside `trace.py` itself**
  — not bound into `structlog.contextvars` (only `trace_id`/`session_id`/`user_id` are, in
  `service/app.py:_bind_request_identity`), not included in the `MODEL_CALL_COMPLETED` /
  `MODEL_CALL_STARTED` payload (`llm_client/telemetry.py`), and not read by any Postgres
  write path. Confirmed by exhaustive grep across `src/` and `tests/`.
- No ES index template (`docker/elasticsearch/*.json`) declares a `profile` property, and a
  grep of every `config/kibana/dashboards/*.ndjson` `sourceField` finds zero panels
  referencing `profile`. `llm_performance.ndjson` already groups by `model` + `role`.
- The one production call site that ever sets `profile=` on a `TraceContext`
  (`gateway/chat_api.py:321`, `profile="cloud"`) constructs a throwaway context fed straight
  into `emit_model_call_completed`, which does not read `.profile`. Write-only, dead value.
- The durable `api_costs` Postgres table (`docker/postgres/init.sql:90-103`) **already has**
  `provider VARCHAR(50) NOT NULL` and `model VARCHAR(100) NOT NULL` columns, and
  `LiteLLMClient.record_api_call` already populates both correctly (`self.provider`, sourced
  from `ModelDefinition.provider` since ADR-0121 T1). No schema change needed there.

**Conclusion:** there is no live `profile` telemetry dimension to preserve — the ADR's D5
narrative was aspirational/stale, not a description of running code. The real gap is narrower
and different: the `MODEL_CALL_STARTED`/`MODEL_CALL_COMPLETED` ES events carry `model` but no
explicit `provider` (the `endpoint` field conflates a URL for local calls with a provider name
for cloud calls — not a substitute). This plan (a) removes the dead `profile` field outright
(no retain-for-one-deploy needed — nothing to break), and (b) adds `provider` at the per-call
emission layer, which is the correct grain (a trace can span multiple models; `profile` was
wrong-grained from the start). This finding will be flagged explicitly in the codex plan
review and the final ticket comment to master, since it's a correction to the ADR's own text
(the same class of self-correction FRE-916/FRE-917 made during their builds).

## Codex plan-review finding (pre-existing bug AC-8 would otherwise catch)

Codex verified the "profile is dead" finding independently (confirmed) and flagged a real,
in-scope gap: `LiteLLMClient` writes **two different model strings for the same call** —
`emit_model_call_completed(model=self._litellm_model, ...)` (ES, `"anthropic/claude-sonnet-4-6"`,
litellm_client.py:699) vs `cost_tracker.record_api_call(model=self.model_id, ...)` (`api_costs`,
bare `"claude-sonnet-4-6"`, litellm_client.py:657). AC-8's fail clause is explicit: "Fails if...
the recorded model differs from the model-call-completed model for that turn" — today it does,
for every cloud call. Verified before deciding the fix: no downstream consumer reads
`api_costs.model` expecting the bare form (`get_cost_by_purpose`/`get_total_cost`/
`get_weekly_cost` filter by `provider`, not `model`; `route_trace/assembler.py`,
`route_trace/ledger.py`, `insights/engine.py` don't read `api_costs.model` at all; no Kibana
dashboard sources from `api_costs`). **Fix:** change the `record_api_call` call in
`litellm_client.py` to pass `model=self._litellm_model` (matching the ES canonical string)
instead of `model=self.model_id` — a one-line change confined to the write, zero blast radius
elsewhere. (`LocalLLMClient` never calls `record_api_call` at all — free local calls aren't
cost-tracked — so no equivalent fix is needed there.)

Codex also flagged that `gateway/chat_api.py`'s direct-Anthropic `/chat` endpoint never calls
`record_api_call` at all (cost-gate reservation only, no `api_costs` row). Confirmed this is a
**separate, pre-existing gap in the standalone Seshat Gateway** (`gateway/chat_api.py:1-13`
docstring: "Cloud-native chat endpoint for the Seshat Gateway"), hardcoded to
`_CLOUD_MODEL = "claude-sonnet-5"` and never routed through the ADR-0121 catalog/factory/primary-
selection path at all — out of scope for AC-8 (which is about turns on **selected primary
models**, i.e. the orchestrator path). Left as-is; noted in the final ticket comment as a
discovered-but-out-of-scope observation, not fixed here.

## Steps

1. **`src/personal_agent/telemetry/trace.py`** — remove `profile` field from `TraceContext`
   (dataclass field + `new_trace()` + `new_span()`) and from `SystemTraceContext.new()`.
   Update the class docstring (currently describes `profile` under "Attributes") to drop it
   and add a one-line note that the dimension moved to per-call `provider`/`model` on
   `model_call_completed` (ADR-0121 §8). Verify: `grep -n profile src/personal_agent/telemetry/trace.py` returns nothing.

2. **`src/personal_agent/gateway/chat_api.py`** — at `_emit_gateway_model_call_completed`
   (~line 321): drop `profile="cloud"` from the `TraceContext(...)` construction (dead kwarg,
   now an invalid one). Add `provider="anthropic"` to the `emit_model_call_completed(...)`
   call below it (the gateway path is hardcoded to the Anthropic SDK, so this is a literal,
   not a lookup).

3. **`src/personal_agent/telemetry/events.py`** — add `"provider"` to
   `CANONICAL_MODEL_CALL_STARTED_FIELDS` (the completed set inherits it via the existing
   union).

4. **`src/personal_agent/llm_client/telemetry.py`** — add a required `provider: str` kwarg to
   both `emit_model_call_started` and `emit_model_call_completed`; include `"provider": provider`
   in each payload dict. Update both docstrings' `Args:`.

5. **`src/personal_agent/llm_client/client.py`** (`_do_request`, both emit call sites) — pass
   `provider=model_config.provider or "unknown"`. (`ModelDefinition.provider` is `str | None`;
   the `_deployments_reference_known_providers` validator only enforces non-None when
   `providers:` is populated, so test fixtures with an empty `providers:` dict can still yield
   `None` — never let a missing provider crash a chat turn.)

6. **`src/personal_agent/llm_client/litellm_client.py`** (both emit call sites) — pass
   `provider=self.provider` (already the ADR-0121 catalog provider key, e.g. `"anthropic"`,
   set from `model_def.provider` in `factory.py:81`). Also fix the `record_api_call` call
   (~line 656): change `model=self.model_id` → `model=self._litellm_model` so `api_costs.model`
   matches `MODEL_CALL_COMPLETED.model` for the same turn (codex plan-review finding — see
   above).

7. **`docker/elasticsearch/index-template.json`** — add `"provider": {"type": "keyword"}` to
   `template.mappings.properties`, next to the existing `"model"`/`"model_role"` entries (same
   treatment, per the ticket's mapping-audit note and the project's FRE-533 first-pass-mapping
   history).

8. **`tests/scripts/test_es_templates.py`** — add `"provider": "keyword"` to the `scalars` dict
   in `test_logs_dashboard_referenced_fields_are_explicit` so the new field is locked in
   the same way `model`/`role` are.

9. **`src/personal_agent/llm_client/cost_tracker.py`** — add `get_cost_by_model(self, days:
   int = 7, provider: str | None = None) -> dict[str, float]`, mirroring
   `get_cost_by_purpose` exactly (`GROUP BY model` instead of `GROUP BY purpose`). This is the
   concrete "querying spend grouped by model" surface AC-8 requires — not speculative, it's
   the direct proof mechanism the criterion names.

10. **Tests** —
    - `tests/personal_agent/llm_client/test_telemetry_parity.py`: remove `profile=base.profile`
      from `_ctx_with_session`; add `provider="anthropic"` to every
      `emit_model_call_started`/`emit_model_call_completed` call in
      `TestCanonicalEmitContract` (four call sites); in `TestClientWiring`, assert
      `s_kwargs["provider"] == "anthropic"` for the LiteLLM wiring test and
      `s_kwargs["provider"] == "unknown"` for the LocalLLMClient wiring test (its fixture YAML
      declares no `providers:`, exercising the fallback deliberately).
    - `tests/test_telemetry/test_trace.py`: add
      `test_trace_context_has_no_profile_field` — regression guard
      (`"profile" not in {f.name for f in dataclasses.fields(TraceContext)}`).
    - New `tests/personal_agent/llm_client/test_cost_by_model.py`: mock `pool.acquire` per the
      `test_cost_tracker_identity.py` pattern; assert `get_cost_by_model` returns
      correctly-split, non-zero values for two distinct models (direct AC-8 proof at the
      query layer).
    - `test_telemetry_parity.py`'s `TestClientWiring.test_litellm_client_calls_both_helpers_with_matched_span`:
      additionally assert `c_kwargs["model"] == "anthropic/claude-sonnet-4-6"` (unchanged) and
      add a new assertion/test that `record_api_call`'s `model` kwarg equals
      `emit_model_call_completed`'s `model` kwarg for the same call — the direct regression
      guard for the codex-flagged mismatch.

11. **Kibana** — no panel changes: verified zero dashboards reference `profile` (see finding
    above), so the "no panel left blank" clause is vacuously satisfied. Note this explicitly
    in the final ticket comment rather than silently skipping it.

## Test commands

```bash
make test-file FILE=tests/personal_agent/llm_client/test_telemetry_parity.py
make test-file FILE=tests/test_telemetry/test_trace.py
make test-file FILE=tests/personal_agent/llm_client/test_cost_by_model.py
make test-file FILE=tests/scripts/test_es_templates.py
make test-file FILE=tests/personal_agent/llm_client/test_cost_tracker_identity.py
make test        # full suite
make mypy
make ruff-check
make ruff-format
pre-commit run --all-files
```

## Non-goals (explicitly out of scope, folded-in note for master)

- No PWA/Kibana dashboard changes (nothing references `profile` today).
- No change to `sessions.execution_profile` / the Path pill — that's T5 (FRE-920), the seam
  ticket.
- No change to `endpoint`'s existing (inconsistent) semantics on the two clients — out of this
  ticket's blast radius; `provider` is added alongside it, not as a replacement.
