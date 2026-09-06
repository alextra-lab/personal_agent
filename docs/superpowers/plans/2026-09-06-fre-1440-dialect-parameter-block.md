# FRE-1440 — the client builds its parameter block from the dialect

**Ticket:** FRE-1440 (Approved, Tier-1, stream:build2) · **ADR:** ADR-0145 D3a, D3b
**Blocked by:** FRE-1439 (Done, merged as PR #1088) · **Study:** FRE-1430 F4, F5, F7, F8, F9

## Scope, in five bullets

1. `_local_extra_body` becomes `_dialect_params(dialect, mode)` — one pure function from a
   dialect plus its resolved mode to a litellm parameter block. Both dispatch branches use it.
   The branches keep their transport differences and lose their parameter differences.
2. The cloud branch reads the effective definition. Today it sends `temperature` only when a
   caller passes one (`litellm_client.py:931`).
3. A call-site sampler the dialect rejects raises a typed error before dispatch. It is not
   dropped with a log.
4. On OVH the declared effort is forwarded with `allowed_openai_params=["reasoning_effort"]`.
   litellm's `ovhcloud` map has no `reasoning_effort` and cannot become right (F8).
5. Two repairs the migration exposes: `reflection.py:543` sends a temperature to Sonnet 5,
   which rejects it; `scripts/eval/fre1390_planner_role_ab/harness.py:313` still reads two
   fields FRE-1439 removed.

## What FRE-1439 already landed (verified on `origin/main` at 0b38e2f4)

- `Dialect` enum, `ModeSpec`, `DIALECT_FIELDS`, `DIALECT_VALUE_DOMAINS` in
  `llm_client/models.py`.
- `ModelDefinition.modes` / `.default_mode` / `.dialect` / `.resolve_mode()`.
- The eight catalog entries migrated; the top-level sampler and thinking fields are gone.
- `factory.py:124`, `dspy_adapter.py:174`, `entity_extraction.py:1102-1105` read the mode.
- `config_guard._project_default_mode` projects the mode onto the pre-D3a names, with a
  docstring that names the oracle rewrite as D3b's job — that is this ticket.

Only one reader of a removed field survives: the eval harness above (`scripts/` is outside
`mypy src/`, so it did not fail the gate).

## Design

### The wire map, per dialect (ADR-0145 D3a table, FRE-1430 F1/F2/F3/F5/F6)

| Dialect | Top-level litellm kwargs | `extra_body` | Effort |
|---|---|---|---|
| `llamacpp_qwen` | temperature, top_p, presence_penalty, frequency_penalty, seed | `cache_prompt` (always), top_k, min_p, `repetition_penalty` ← `repeat_penalty`, `chat_template_kwargs` when `enable_thinking is False` | none |
| `ovh_qwen` | temperature, top_p, presence_penalty, frequency_penalty, seed | — | `reasoning_effort` + `allowed_openai_params` |
| `openai_gpt5` | temperature, top_p, frequency_penalty, seed | — | `reasoning_effort`, forwarded natively |
| `anthropic_adaptive` | — | — | `reasoning_effort` ← `effort` |
| `anthropic_budget` | temperature, top_p, top_k | — | `thinking: {type: enabled, budget_tokens}` |

`allowed_openai_params=["reasoning_effort"]` is attached when **both** hold: the dialect's own
lever field is `reasoning_effort` (`ovh_qwen`, `openai_gpt5` — never the two Anthropic dialects,
whose lever is `effort` or `budget_tokens` and where litellm's transformation *is* the wire),
and litellm's map does not already report native support. That is ADR-0145 D3b's rule stated in
code rather than a hard-coded provider list: `provider_reasoning_support` decides only *which
kwarg carries the value*, never *whether the value is sent*. Measured today (F8): OVH returns
`None` and gets the kwarg; `gpt-5.4-mini` returns `True` and does not, so the two live producers
bound to it see no change.

Four rows of the table carry a constraint the dialect's **field set** cannot express. Each is
already enforced where it belongs, and none is a gap this ticket opens:

| Constraint | Where it is enforced | Why not in the call-site gate |
|---|---|---|
| `openai_gpt5` accepts temperature and top_p only at effort `none` | loader, `models.py:713-722` | Cross-field. A mode cannot declare the bad pair. A call site that builds one gets litellm's own rejection, which `drop_params: off` exists to surface |
| `anthropic_budget` takes temperature XOR top_p | loader, `models.py:723-731` | same |
| `frequency_penalty` is in `openai_gpt5`'s accepted set (ADR D3a) but litellm **raises** on it (F5) | nothing declares it; a mode that did would fail loudly at dispatch | Failing loudly is the wanted outcome. Adding a speculative escape hatch for an unused field is not |
| llama.cpp's real wire key is `repeat_penalty`; we send `repetition_penalty`, which that server ignores (F2) | **FRE-1438** owns the rename | Changing the wire key here is a behaviour change on the primary path, in a ticket that promises none |

Two entries in the `llamacpp_qwen` block are deliberate asymmetries, kept because changing
either would alter the primary path on every turn:

- `chat_template_kwargs` is sent only for `enable_thinking: false`. The catalog's two
  `enable_thinking: true` modes record thinking that was already on by omission
  (`config/models.yaml:146`, `:216`), so sending the flag explicitly would be the change.
- `cache_prompt: true` is a transport constant (FRE-433), not a mode-declared sampler. It stays
  unconditional, as today.

### The call-site gate

`DIALECT_FIELDS` is the oracle. One name needs an alias: `anthropic_adaptive` declares `effort`
in configuration and takes `reasoning_effort` as the call-site kwarg, because litellm's
transformation is the wire there.

A rejected value raises `DialectParameterRejected(LLMClientError)`, naming the field, the
dialect and the model. No dialect (a direct construction outside the catalog) means no gate —
today's behaviour.

### The effort, after D3b

The `provider_reasoning_support(...) is None` omit-and-log branch at `litellm_client.py:942-966`
survives **only** on the no-dialect path. A declared dialect that declares the lever forwards
the value. That is the ADR's own fallback rule, and it removes the branch that made a GitHub
fetch decide whether a declaration reached the wire.

`config_guard._check_one_reasoning_declaration` gains the same rule: when the deployment's
dialect declares the effort lever and the value sits inside the dialect's own domain, the
dialect is the authority and the litellm probe is skipped. Anthropic keeps the probe.

**Deliberately not in scope:** widening the guard to walk every selectable `kind: llm` entry.
ADR-0145 D3b names it as a consequence and attributes it to FRE-1421 F15, but FRE-1440's own
"What to build" list does not carry it.

## Steps

1. **`llm_client/models.py`** — add `EFFORT_FORWARDING_DIALECTS`, `dialect_accepts()`, and
   `ModelDefinition.resolve_dialect(provider_def)`. Reuse `resolve_dialect` in
   `ModelConfig._llm_deployments_declare_valid_modes` so the loader and the client cannot drift.
   → verify: `make test-file FILE=tests/personal_agent/llm_client/test_dialect_modes.py`.
2. **`llm_client/types.py`** — add `DialectParameterRejected(LLMClientError)`.
3. **Failing tests first** — `tests/personal_agent/llm_client/test_dialect_parameter_block.py`,
   one class per acceptance criterion, built on the kwargs-capture harness already in
   `test_reasoning_effort_reaches_provider.py:58`. Confirm they fail.
   → verify: `make test-file FILE=tests/personal_agent/llm_client/test_dialect_parameter_block.py`
     shows failures naming the missing behaviour, not import errors.
4. **`llm_client/litellm_client.py`** — replace `_local_extra_body` with `_dialect_params`; add
   `_gate_call_site_params`; wire both branches; restrict the litellm-map branch to the
   no-dialect path.
5. **`captains_log/reflection.py:543`** — drop `temperature=0.3`. Sonnet 5 rejects every
   sampler (F1/F6), and the mode is the only declarative home now.
6. **`config/config_guard.py`** — the declared dialect answers first for the effort.
7. **`scripts/eval/fre1390_planner_role_ab/harness.py`** — read the resolved mode instead of the
   two removed fields.
7b. **`scripts/study/categorizer.py:145`** — fold-in. It loads a `ModelDefinition` and then
   constructs `LiteLLMClient` without passing it, so a catalog-backed call site lands on the
   no-dialect path and loses the declared parameters. One keyword argument.
8. **Docs** — `docs/reference/SLM_SERVER_CLIENT_SEMANTICS.md` and
   `docs/guides/SCHEMA_REFERENCE.md` where they describe the parameter block.
9. **Gates** — `make test` · `make mypy` · `make ruff-check` · `make ruff-format` ·
   `pre-commit run --all-files`; then self-review per the build skill.
10. **AC-2 live probe** — one real OVH call through the client with a mode declaring
    `reasoning_effort: none`; compare billed `completion_tokens` and the absence of
    `message.reasoning` against FRE-1430 F9's measured rows. Scratchpad script, not committed;
    output goes in the handoff.

## Acceptance criteria

| AC | Claim | Proof |
|---|---|---|
| AC-1 | The declared effort reaches the wire on OVH | Captured `litellm.acompletion` kwargs for an `ovh_qwen` model whose mode declares `reasoning_effort: none` carry both `reasoning_effort` and `allowed_openai_params` |
| AC-2 | The OVH call bills as the declared mode | Live probe: billed `completion_tokens` and absent `message.reasoning` match F9's `none` row (4 tokens, no reasoning), not the `xhigh` default (76 with reasoning) |
| AC-3 | No sampler reaches a dialect that rejects it | Captured kwargs for `claude_sonnet` carry no `temperature`, `top_p` or `top_k` — asserted on the factory path **and** on the reflection fallback |
| AC-4 | A rejected call-site sampler raises | `top_p` to an `anthropic_adaptive` model raises `DialectParameterRejected`, and the message names the field and the dialect |
| AC-5 | The four existing sampler call sites still work | `entity_extraction`, `context_compressor`, `skills` each still dispatch their temperature; reflection dispatches none |
| AC-6 | The cloud branch reads the definition | A cloud call with no caller temperature dispatches the resolved mode's value |

## Risks

- **The primary path is local and runs on every turn.** `_dialect_params` must produce a
  byte-identical block for `llamacpp_qwen` today. Guarded by the existing
  `test_local_via_litellm.py` assertions plus an explicit equality test against the old
  `_local_extra_body` output.
- **Two live producers bind `gpt-5.4-mini`.** The cloud branch starts sending that entry's
  declared `temperature: 0.0` by itself. Both callers already pass the same value, so the
  dispatched value does not change.
- **The gate can be too broad.** AC-5 exists for exactly that; each of the three surviving
  call sites gets its own assertion.
