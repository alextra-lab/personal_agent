# FRE-1007 — Background producers inherit their reasoning configuration by omission

**Ticket:** [FRE-1007](https://linear.app/frenchforest/issue/FRE-1007/background-producers-inherit-their-reasoning-configuration-by-omission)
**Backing ADR:** ADR-0121 (Implemented) — Layer 1 providers / Layer 2 deployments / Layer 3 bindings
**Scope:** declaration half ONLY. The attribution half (per-producer roles, per-role counters,
per-producer caps) is out of scope — ADR-0120 is still Proposed.
**Diff class:** escalated — changes what is sent to paid providers on every background call.

> **Revision 3 (2026-08-23), after implementation surfaced a boot failure.** litellm's per-model
> capability map is **fetched from GitHub at import**; the bundled fallback has no entry for
> `claude-sonnet-5`, and in that state litellm reports every reasoning parameter unsupported.
> Revision 2's guard read that as "the provider forbids a declaration" and refused to boot the
> application. Two corrections, both in §3: the check now distinguishes *unknown* from
> *unsupported*, and the transformation-dependent findings are **policy** class (CI) while the
> structural ones stay **safety** (boot). This also vindicates codex's *nit* #3, which I had
> under-weighted, and explains its blocker #1 — codex was observing litellm without the map.
>
> **Revision 2 (2026-08-23), after codex plan-review.** Revision 1 proposed declaring
> `reasoning_effort: medium` on `gpt-5.4-mini` and `high` on `claude_haiku`, both claimed
> behaviour-preserving. **Both were wrong, and one would have broken production.** Every claim
> below is now backed by a direct run of litellm's own transformation against the installed
> version, recorded in §2. Codex's blocker #1 (litellm rejects effort for `claude-sonnet-5`) is
> the one finding I did **not** accept — it is contradicted by direct execution, twice, on the
> version it cites; see §7.

---

## 1. The defect

`ModelDefinition.reasoning_effort` and `RoleBinding.reasoning_effort` are both
`Literal["low","medium","high","xhigh"] | None = None`. Nothing rejects a producer that declares
nothing, so "this producer runs at a chosen reasoning depth" is a convention, not a type.

## 2. Measurement — what litellm 1.89.2 actually forwards

Run against the installed venv, no network calls to a provider. This table is the design.

| model (provider) | omitted | `none` | `low` | `high` |
|---|---|---|---|---|
| `claude-sonnet-5` (anthropic) | `{}` | `{}` | `thinking:{adaptive}` + `output_config:{effort:low}` | `thinking:{adaptive}` + `output_config:{effort:high}` |
| `claude-haiku-4-5-20251001` (anthropic) | `{}` | `{}` | `thinking:{enabled,budget:1024}` + **`max_tokens→5120`** | `thinking:{enabled,budget:4096}` + **`max_tokens→8192`** |
| `gpt-5.4-mini` (openai, `temperature: 0.0`) | `temperature:0.0` | `temperature:0.0` + `reasoning_effort:"none"` | **`UnsupportedParamsError`** | **`UnsupportedParamsError`** |
| `Qwen3.6-27B` (ovhcloud) | `{}` | **`UnsupportedParamsError`** | **`UnsupportedParamsError`** | **`UnsupportedParamsError`** |

Four findings, each of which changes the plan:

**(a) The vocabulary diverges *within* one vendor, not just across vendors.** `claude-sonnet-5`
advertises `supports_output_config`, so effort becomes an adaptive-thinking block. `claude-haiku-4-5`
advertises only `supports_reasoning`, so litellm takes the **legacy** path: it converts effort into
an explicit thinking budget *and silently rewrites `max_tokens`*. The ticket predicted a
per-provider split; the real split is per-**model**.

**(b) Declaring an effort on `gpt-5.4-mini` would break production.** The deployment pins
`temperature: 0.0` (FRE-758). litellm rejects `temperature≠1` together with any effort above
`none`. `entity_extraction` and `compressor` both bind to it. Revision 1's `medium` was not a
cost-neutral declaration — it was an outage.

**(c) `none` is not in our type, and it is the only correct value for two deployments.**
`scripts/eval/fre630_extraction_quality/cells.py:142-166` records a *measured* in-repo finding:
"gpt-5.4-mini with reasoning_effort unset produces ZERO reasoning tokens (identical to explicit
'none') — the ticket's 'medium (default)' premise was wrong". So the deliberate choice for the
extractor is exactly `none`, and the current `Literal` **cannot express it**. This is the ticket's
own defect one level deeper: not merely that the field is optional, but that its vocabulary cannot
represent "deliberately no reasoning", which forces every declarer toward an expensive value.

**(d) `none` means different things per provider, so it is not universally a declaration.** On
OpenAI it is a real wire value (`reasoning_effort:"none"`). On Anthropic litellm **drops it** — the
request goes out bare and the provider default applies. So `none` on an Anthropic deployment would
satisfy the letter of the rule while sending nothing: the exact wired-but-not-effective trap. The
guard must reject it *there* and accept it *on OpenAI*, and it can only know the difference by
asking litellm.

## 3. The rule this encodes

For every `kind: llm` deployment **that a role binds to**, require a declaration in the vocabulary
its dispatch path can actually carry, and verify the declared value against litellm's own
transformation:

| dispatch path | required | rejected |
|---|---|---|
| `placement: local` (LocalLLMClient → `extra_body`) | `disable_thinking` or `thinking_budget_tokens` | `reasoning_effort` (never sent on this path) |
| cloud, litellm supports `reasoning_effort` for that model | `reasoning_effort`, and the declared value must produce a **non-empty, non-raising** transformation for that model **together with the deployment's own declared params** | a value litellm drops (decorative) or rejects (outage) |
| cloud, litellm has no capability record for the model | — | nothing is concluded — *unknown* is not *forbidden* (see below) |

The middle row is the load-bearing one: the guard does not compare against a hand-written table,
it **runs the declared value through litellm's own transformation for that exact model, with that
deployment's other declared parameters, and requires a real result**. That single predicate rejects
`medium` on `gpt-5.4-mini` (raises, because of the pinned temperature), rejects `none` on
`claude_sonnet` (empty — decorative), and accepts `none` on `gpt-5.4-mini` (real wire value). Both
of revision 1's errors are caught by it automatically.

**Severity split (Revision 3).** Asking litellm what a value *becomes* depends on a map fetched
from GitHub at import. That makes those findings right for CI and wrong for boot:

* **Safety** (fails CI *and* refuses boot): declaration missing, vocabulary mismatch, thinking-disable
  on a tool-using cloud model. All decidable from the catalog alone.
* **Policy** (fails CI only): declaration ineffective (dropped), declaration rejected. Caught before
  deploy, where the map is present; never able to take the application down.

The loophole codex raised (a producer bound to a provider with no lever) stays closed without a
dedicated check, which is just as well since that check could not be made reliable: undeclared it
fails the safety check, declared it fails the policy one. `qwen3.6-27b-ovh` is bound by no role
today, so this is a ratchet, not a migration.

**Prohibition (ticket, encoded not judged):** effective `disable_thinking: true` on a **tool-using
deployment dispatched through litellm** is a safety finding. The ticket scopes this itself — the
existing flag is the *local Qwen chat-template* mechanism ("the risk is that it gets repurposed
rather than that it is already misused") — so `qwen3.6-35b-instruct` stays legal.

## 4. Type change

Extend the effort vocabulary to `Literal["none","low","medium","high","xhigh"]` on both
`ModelDefinition.reasoning_effort` and `RoleBinding.reasoning_effort`. Without `none`, finding (c)
makes the mandatory rule unsatisfiable for the two OpenAI-bound producers except by an illegal or
expensive value. `minimal` is deliberately not added: `claude-sonnet-5` accepts it (aliasing to
`low`) but `gpt-5.4-mini` rejects it, and nothing needs it.

## 5. Declarations — measured behaviour-preserving

Only role-bound `kind: llm` deployments are in scope; two of the four already declare.

| deployment | bound by | declaration | behaviour-preserving? |
|---|---|---|---|
| `qwen3.6-35b-thinking` | primary | `thinking_budget_tokens: 32768` *(already)* | unchanged |
| `qwen3.6-35b-instruct` | sub_agent | `disable_thinking: true` *(already)* | unchanged |
| `claude_sonnet` | artifact_builder, captains_log, session_summary, insights, vision | **`reasoning_effort: high`** | yes — `adaptive`+`high` is the provider default it inherits today; now stated |
| `gpt-5.4-mini` | entity_extraction, compressor | **`reasoning_effort: none`** | yes — measured identical to omission (`cells.py:142`), and the only value legal beside the pinned `temperature: 0.0` |

No binding-level overrides. Revision 1 proposed `captains_log → medium`; codex correctly showed it
would be inert — `reflection.py:363` resolves the role to a *deployment key* and passes that key to
`configure_dspy_lm`, so `resolve_role_target` finds no binding (`model_loader.py:355-368`). Dropping
the override removes the problem rather than working around it.

`reflection.py:547`'s hard-coded `reasoning_effort="medium"` is removed so configuration is the single
source. Effect: the *manual fallback* path moves `medium → high`, converging it onto what
reflection's **primary** DSPy path already ran at (the provider's own default). Noted in the handoff.

**Out of scope, surfaced not fixed:** `claude_haiku` is bound by no role but is reached through
`settings.skill_routing_model_key`, and the router calls it with `max_tokens=256` (`skills.py:474`).
Per §2 any effective effort there rewrites `max_tokens` to 5120–8192 and enables thinking on a
routing call. That is a real finding, needs evidence, and touches a user-facing latency path →
**separate ticket**, not folded in.

## 6. Steps

1. **Type** — add `"none"` to both `reasoning_effort` Literals (`llm_client/models.py:159,296`).
   → verify: `make mypy` clean; existing catalog still loads.
2. **Guard vocabulary probe** — `config_guard._reasoning_wire_shape(model_id, provider, declared_params)`
   returning the transformed dict or the raised error, via a lazily-imported
   `litellm.utils.get_optional_params`. Lazy because `config_guard` is imported at settings-import
   time (mirrors `check_budget_role_coverage`'s lazy `cost_gate` import).
   → verify: unit test reproduces the §2 table exactly — it fails loudly if litellm's mapping moves.
3. **`check_reasoning_declaration(root)`** — safety findings:
   `reasoning_declaration_missing` · `reasoning_declaration_ineffective` (empty transform) ·
   `reasoning_declaration_rejected` (raises) · `reasoning_vocabulary_mismatch` (wrong path) ·
   `reasoning_undeclarable_binding` (ovhcloud row) · `thinking_disable_on_tool_model`.
   Registered in `run_all_checks`.
   → verify: `scripts/check_config.py` exits 0 on the real repo after step 4.
4. **Declare** the two values from §5 in `config/models.yaml`, each commented with its *reason and
   its measured evidence*, not just the value.
   → verify: step 3's clean run.
5. **Seeded negatives** — fixtures under `tests/personal_agent/config/fixtures/`:
   `reasoning_undeclared/`, `reasoning_ineffective/` (`none` on an Anthropic bound deployment),
   `reasoning_rejected/` (an effort beside `temperature: 0.0` on gpt-5), `reasoning_undeclarable/`
   (a role bound to an ovhcloud deployment), `thinking_disable_tool_model/`.
   → verify: each fixture yields exactly its finding; `check_config.py --root <fixture>` exits 1.
6. **Effective — cloud door** — `LiteLLMClient.__init__(..., *, reasoning_effort=None)`; `respond()`
   prefers the per-call argument, else the constructor value; `factory._build_client` passes
   `model_def.reasoning_effort`.
7. **Effective — DSPy door** — `configure_dspy_lm` forwards `model_def.reasoning_effort` to `dspy.LM`.
8. **Remove** `reflection.py:547`'s hard-coded effort.
9. **Fail closed at startup** — `enforce_reasoning_declaration(config, *, root=None)` called from
   `load_app_config()` beside `enforce_required_secrets`. A plain function, not a `model_validator`,
   for the reason `enforce_required_secrets` already documents.
10. **Docs** — ADR-0121 addendum recording the rule and the §2 measurement table; catalog comments.

## 7. Codex plan-review disposition

| # | Finding | Disposition |
|---|---|---|
| 1 | litellm rejects effort for `claude-sonnet-5` | **Rejected — contradicted by measurement.** Two independent runs on litellm 1.89.2 return `thinking:{adaptive}`+`output_config`. `litellm.model_cost["claude-sonnet-5"]` carries `supports_output_config: True`. §2 row 1. |
| 2 | ovhcloud "declaration forbidden" is a fail-open loophole | **Accepted.** Closed by forbidding the *binding* instead (§3 last row). |
| 3 | `gpt-5.4-mini medium` / `claude_haiku high` are not behaviour-preserving | **Accepted — and worse than reported.** `medium` + the pinned `temperature: 0.0` **raises**. §2(b). |
| 4 | `captains_log` binding override never reaches DSPy | **Accepted.** Override dropped (§5). |
| 5 | `scripts/` construct `LiteLLMClient` directly, bypassing the default | **Partially accepted.** Out of the production path; the constructor default is still the right seam. Noted in handoff, not fixed. |
| 6 | Haiku `high` collides with skill routing's `max_tokens=256` | **Accepted.** Measured: `max_tokens` is rewritten to 8192. Haiku is unbound → out of guard scope; filed as a separate ticket (§5). |
| 7 | reflection's `temperature=0.3`+`medium` is a latent failure | **Rejected as stated** (rests on #1 — sonnet-5 accepts effort, and temperature is not rejected for Anthropic). The line is removed anyway (§5). |
| 8 | ACs satisfiable on paper | **Accepted.** §8 tightened: AC-2 asserts the *transformed* payload, AC-3 drives the transformation from the loaded catalog, AC-5 exercises `load_app_config()`. |

## 8. Acceptance criteria — the ticket's PROOF REQUIRED

| # | Criterion | Proof |
|---|---|---|
| AC-1 | "A producer binding that omits the reasoning configuration fails the guard, demonstrated by adding one and watching it fail" | `reasoning_undeclared` fixture — a **role-bound producer deployment** with no declaration; `check_config.py --root` exits 1 |
| AC-2 | "The declared configuration is visible in the request actually sent to the provider, not merely present in configuration" | Test drives the digest's **own door** (`get_llm_client_for_key("claude_sonnet", …)`), captures `litellm.acompletion` kwargs, **then feeds them through `get_optional_params`** and asserts `output_config.effort == "high"` on the transformed payload — not merely that a kwarg was passed |
| AC-3 | "For at least one non-Anthropic binding, evidence the declaration is expressed in that provider's vocabulary and survives the litellm transformation" | Test **loads the real catalog**, resolves `entity_extraction` → `gpt-5.4-mini`, and transforms its declared value *with its declared `temperature`*: yields `reasoning_effort:"none"`, against Anthropic's `thinking`+`output_config` from the same field |
| AC-4 | "Do not permit a thinking disable as a cost lever on any model that uses tools" | `thinking_disable_tool_model` seeded negative; local Qwen sub-agent stays legal (mechanism, not misuse) |
| AC-5 | "Enforcement at startup … refuses to start. Not a warning and not a default" | Test calls **`load_app_config()`** (not the helper) with the repo root pointed at a seeded fixture and asserts `ValueError` |

## 9. Risks

- **litellm's mapping is provider truth that can move under us.** Step 2's test pins the whole §2
  table; if litellm changes, it fails loudly rather than silently changing what the guard requires.
- **Guard cost.** `get_optional_params` runs once per bound llm deployment (4 today) at
  pre-commit/CI and startup. Negligible; litellm is already imported by the client layer.
- **Selection path.** A user selecting an undeclarable deployment for a *user-turn* role is out of
  the ticket's background-producer scope; recorded as a known boundary in the handoff.
