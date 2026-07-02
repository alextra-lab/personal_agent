# FRE-691 — Cloud image cost: pricing + pre-flight estimate + ADR-0065 reservation + threshold confirmation + metering

**Ticket:** FRE-691 (Approved) · **Project:** Agent Vision and Attachment Ingestion
**Backing:** ADR-0101 §8b (shared control spine), ADR-0065 (cost gate), ADR-0099 (single-source config)
**Branch:** `fre-691-cloud-image-cost`

---

## Scope (from ticket + ADR-0101 §8b)

Build the **attachment-type-agnostic** cloud cost controls so ADR-0102 (documents, FRE-686) reuses
them rather than rebuilding. Image is the bounded instance (≈1600 tokens/image, no page multiplication).

Two acceptance criteria:

- **AC-10** — pre-flight estimate gates spend, confirm proceeds. Over-threshold turn → **no model call**
  until confirmed, response carries the dollar estimate + proceed-or-keep-local prompt; confirm → model
  called + spend committed; under-threshold → reservation ≈ estimate recorded **before** the call.
- **AC-11** — cloud images priced & metered, not free. Cloud model definition **carries per-token
  pricing**; a cloud image turn commits a **non-zero** actual cost whose token basis **includes image
  tokens** (not text-only). A zero-cost placeholder does not satisfy this.

---

## Current state (post FRE-661/665/666/734) — what already exists

- Attachments resolve to image blocks: `orchestrator/attachment_resolution.py` → `ResolvedAttachments`.
- Vision routing (cloud vs local, `processing_target` override): `executor.py:1361 _resolve_vision_routing_key`.
- ADR-0065 reserve/commit/refund **already wraps every litellm call**: `litellm_client.py:440-587`
  (`estimate_reservation_for_call` → `gate.reserve` → call → `litellm.completion_cost` → `gate.commit`).
- Disclosure append at finalize: `executor.py:3745-3749` (SYNTHESIS→COMPLETED).
- Fixed per-image token estimate: `llm_client/message_content.py:18 IMAGE_BLOCK_TOKEN_ESTIMATE = 1600`.
- Confirm-pause machinery: `executor.py:320 _maybe_pause_for_constraint` + `constraint_options.py`.

**The two real gaps:**
1. **Pricing is not owned by config.** Cost is read from litellm's *shipped* `litellm.model_cost`
   registry. `claude-sonnet-4-6` happens to be present today ($3/$15 MTok), but the ADR (and the
   `models.cloud.yaml` source-of-truth memory) require the **model definition** to carry pricing, so
   cost is deterministic and non-zero regardless of litellm registry drift. AC-11(a).
2. **No per-turn attachment threshold confirmation.** The gate only denies on weekly-cap breach — there
   is no "this image turn will cost $X, proceed?" gate. AC-10.

---

## Design

### Part 1 — Pricing (AC-11)

Make the **model definition** the source of truth for per-token pricing and register it into litellm so
both the estimator (`litellm.model_cost`) and the commit (`litellm.completion_cost`) reconcile non-zero.

1. `llm_client/models.py` — add two optional fields to `ModelDefinition`:
   `input_cost_per_token: float | None = None`, `output_cost_per_token: float | None = None`
   (Google docstrings; USD per token; `None` = rely on litellm/local-free).
2. `config/models.cloud.yaml` **and** `config/models.yaml` (parity — FRE-734 memory) — add pricing to the
   vision-capable cloud Claude entries:
   - `claude_sonnet`: `input_cost_per_token: 0.000003`, `output_cost_per_token: 0.000015`
   - `claude_haiku`: `input_cost_per_token: 0.000001`, `output_cost_per_token: 0.000005`
3. `llm_client/pricing.py` (NEW) — `register_model_pricing(config: ModelConfig) -> int`: for each
   `ModelDefinition` carrying pricing, call `litellm.register_model({f"{provider}/{id}": {...}})` so
   `litellm.model_cost["anthropic/claude-sonnet-4-6"]` = our values. Idempotent, returns count. Threads
   no trace_id (startup, no originating request).
4. `service/app.py` lifespan — after `set_default_gate(...)` (~line 564), call
   `register_model_pricing(load_model_config())` and log `model_pricing_registered count=N`.

**Why register into litellm rather than compute ourselves:** ADR-0101 §8b prescribes reconciliation via
`litellm.completion_cost()`. Registering our config pricing makes that function return our owned,
non-zero value — honoring the ADR while making config the source of truth. `usage.prompt_tokens` from
the provider already includes image tokens, so the committed basis includes images (AC-11b) with no
change to the commit path.

5. **Commit-cost guard (codex High-2)** — `litellm_client.py:575-579` currently calls
   `litellm.completion_cost(completion_response=response)` with **no** model and swallows any exception
   as `cost = 0.0`. If Anthropic returns a *dated* id (`claude-sonnet-4-6-20250514`) not in the
   registry, this silently commits **$0** (AC-11 fails). Fix — two-line hardening:
   - pass `model=self._litellm_model` so litellm resolves the registered (priced) key;
   - on exception **or** a `0.0` result while `usage` has tokens, fall back to
     `input_price × prompt_tokens + output_price × completion_tokens` from our config pricing.
   This guarantees non-zero committed cost with an image-inclusive basis regardless of the response id.

**Registration key note (codex Medium):** `litellm.register_model({"anthropic/claude-sonnet-4-6": …})`
updates the **bare** `claude-sonnet-4-6` entry (litellm resolves known models). Tests therefore assert
the **outcome** — `litellm.completion_cost(model="anthropic/claude-sonnet-4-6", …)` returns our price —
not a specific dict key.

### Part 2 — Pre-flight estimate + threshold confirmation (AC-10)

1. `config/settings.py` — new field (ADR-0099 single-source, not a secret):
   `attachment_cost_confirmation_threshold_usd: float = Field(default=0.50, ge=0.0, description=...)`,
   env `AGENT_ATTACHMENT_COST_CONFIRMATION_THRESHOLD_USD`. **Default value → owner confirms at approval.**
2. `orchestrator/attachment_cost.py` (NEW) — attachment-agnostic estimator:
   `estimate_attachment_cloud_cost_usd(*, block_count, per_block_tokens, input_price_per_token) -> Decimal`.
   Docstring states the image case passes `per_block_tokens=IMAGE_BLOCK_TOKEN_ESTIMATE (1600)` and
   **ADR-0102 (FRE-686) reuses this with page-multiplied tokens — reuse-plus-PDF-specifics, not a fresh
   build.**
3. `orchestrator/constraint_options.py` — add `"attachment_cost"` constraint:
   `[ConstraintOption("proceed_cloud", "Proceed (cloud, ~$X)"), ConstraintOption("keep_local", "Keep local / free")]`
   — `keep_local` **last** = safe default (no spend without confirmation).
4. `orchestrator/executor.py` — `_maybe_confirm_attachment_cost(ctx, ...) -> bool` helper, called from
   `step_init` right after attachment resolution (~line 1878, covers both gateway and legacy paths):
   - Resolve the effective routing key (`_resolve_vision_routing_key(ctx, "primary")`); swallow
     `AttachmentUnsupportedError` and return `True` (let LLM_CALL handle that error as today).
   - No image blocks, or effective model is `provider_type == "local"` (free), or model has no
     `input_cost_per_token` → return `True` (proceed; no gate).
   - Compute estimate = `block_count × 1600 × input_price`. If `≤ threshold` → return `True`.
   - If `> threshold` → `await _maybe_pause_for_constraint(constraint="attachment_cost", context="<$estimate + proceed/keep-local>")`.
     - `proceed_cloud` → return `True` (continue to cloud call; reservation+commit happen in litellm_client).
     - `keep_local` (also timeout / no-WS default) → append a disclosure
       (`"Estimated cloud cost ${X}; kept local/free. Send 'proceed' to run on cloud."`) to
       `ctx.attachment_disclosures`, set `ctx.final_reply` to the estimate+prompt, return `False`.
   - `step_init`: `if not await _maybe_confirm_attachment_cost(...): return TaskState.SYNTHESIS`
     → finalize appends disclosure, returns reply, **no model call** (AC-10a).

**Confirmation is single-turn** via the existing WS constraint-pause waiter (mirrors
`tool_iteration_limit`): the turn blocks on the waiter with images intact; `proceed_cloud` continues the
same turn to the cloud call; `keep_local`/timeout ends the turn with the prompt. No cross-turn NLU.

**Confirmation altitude = per-turn (codex High-1, decision).** Images live in `ctx.messages` and the
turn can re-enter `LLM_CALL` (tool iterations, hybrid synthesis), so a single confirmation authorizes
the **whole turn's** cloud-vision usage, not one call. This is the correct semantic: images-in-context
is a turn-level property, and the user is deciding "run this image turn on cloud." The gate lives in
`step_init` (runs once/turn) and sets `ctx.attachment_cost_confirmed = True` on proceed, so re-entry to
`LLM_CALL` never re-prompts. **Runaway backstop:** the per-call ADR-0065 reservation still fires on
*every* call independently and denies once the weekly cap is hit — a multi-call image turn cannot exceed
the budget even under one confirmation.

**Stored-preference safety (codex Medium-3, decision).** `_maybe_pause_for_constraint` silently applies
a saved preference and can persist a remembered choice. For a **cost** confirmation a silent
"always proceed cloud" would spend money without asking — against the owner's ask-before-spend posture.
Add a minimal `allow_preference: bool = True` param to `_maybe_pause_for_constraint` (default preserves
all existing callers) and pass `allow_preference=False` for `attachment_cost`: it always pauses when
over threshold and never persists an always-spend preference.

### Joinability (§8c, ADR-0074)

Reserve/commit already thread `trace_id`. All new log events (`attachment_cost_gate_*`,
`model_pricing_registered`) carry `trace_id` + `session_id` where an originating request exists
(startup registration has none — mirrors reaper). No new Cypher/bus writes.

---

## Atomic steps (TDD)

Each acceptance criterion gets an **outcome** test.

1. **[test-first]** `tests/personal_agent/llm_client/test_pricing.py` — after `register_model_pricing`,
   assert the **outcome**: `litellm.completion_cost(model="anthropic/claude-sonnet-4-6", …)` returns our
   configured price (not a raw dict-key assertion — litellm updates the bare key). → fails (module absent).
2. Implement `ModelDefinition` fields + `pricing.py` + yaml pricing (both files) → test 1 passes.
3. **[test-first]** `test_pricing.py::test_cloud_image_turn_commits_nonzero_with_image_basis` (AC-11b):
   build a fake litellm response whose `usage.prompt_tokens` includes 1600 image tokens; assert the
   commit-cost path returns `> 0` and **scales with image tokens**. Add
   `test_dated_response_model_still_commits_nonzero` (codex High-2): response `model` is a dated,
   unmapped id → the guard's config-pricing fallback still yields non-zero. → implement the
   `litellm_client.py:575-579` guard (pass model + fallback).
4. **[test-first]** `tests/personal_agent/orchestrator/test_attachment_cost.py` — estimator math:
   `4 blocks × 1600 × 0.000003 = $0.0192`; and `estimate_attachment_cloud_cost_usd` is attachment-agnostic
   (page-multiplied token count → proportionally higher cost). → fails (module absent) → implement
   `attachment_cost.py`.
5. Add settings field + `attachment_cost` constraint options + `allow_preference` param on
   `_maybe_pause_for_constraint`.
6. **[test-first]** `tests/personal_agent/orchestrator/test_attachment_cost_gate.py`:
   - `test_over_threshold_no_ws_stops_with_prompt_no_model_call` (AC-10a): threshold=0.001, cloud image
     turn, no WS waiter → default `keep_local` → `final_reply` contains the `$` estimate + a
     proceed/keep-local prompt; LLM client `.respond` **not** called.
   - `test_confirm_proceeds_and_commits` (AC-10b): inject `proceed_cloud` decision → `.respond` called;
     `gate.commit` invoked with non-zero actual (mocked).
   - `test_under_threshold_reserves_covering_estimate_before_call` (AC-10c): threshold=1.00, single image
     → proceeds; a reservation **≥ the image estimate** (proving the image basis is reserved, not merely
     non-zero) is recorded **before** `.respond` (assert reserve precedes respond).
   - `test_multi_call_turn_confirmed_once_each_call_cap_reserved` (codex High-1): one confirmation;
     re-entry to `LLM_CALL` does **not** re-prompt (`ctx.attachment_cost_confirmed`), and each call still
     hits `gate.reserve` (weekly-cap backstop).
   - `test_over_threshold_ignores_stored_always_proceed` (codex Medium-3): a saved "proceed" preference
     does **not** silently spend for `attachment_cost` (`allow_preference=False`).
7. Implement `_maybe_confirm_attachment_cost` + `step_init` short-circuit + `ctx.attachment_cost_confirmed`
   → tests pass.
8. Wire `register_model_pricing` into `service/app.py` lifespan.
9. **Quality gates:** `make test-file FILE=...` (each new file) → `make test` (module) → full `make test`
   → `make mypy` → `make ruff-check` + `make ruff-format` → `pre-commit run --all-files`.

---

## Files touched

| File | Change |
|------|--------|
| `src/personal_agent/llm_client/models.py` | +2 optional pricing fields on `ModelDefinition` |
| `config/models.cloud.yaml` | pricing on `claude_sonnet`, `claude_haiku` |
| `config/models.yaml` | same pricing (parity) |
| `src/personal_agent/llm_client/pricing.py` | NEW — `register_model_pricing` |
| `src/personal_agent/llm_client/litellm_client.py` | commit-cost guard: pass model + config-pricing fallback |
| `src/personal_agent/service/app.py` | call registration at startup |
| `src/personal_agent/config/settings.py` | `attachment_cost_confirmation_threshold_usd` |
| `src/personal_agent/orchestrator/attachment_cost.py` | NEW — attachment-agnostic estimator |
| `src/personal_agent/orchestrator/constraint_options.py` | `attachment_cost` constraint |
| `src/personal_agent/orchestrator/executor.py` | `_maybe_confirm_attachment_cost` + `step_init` gate; `_maybe_pause_for_constraint` gains `allow_preference` |
| `src/personal_agent/orchestrator/types.py` | `ExecutionContext.attachment_cost_confirmed: bool = False` |
| `tests/...` | 3 new test files (pricing, estimator, gate) |

## Out of scope (deferred / other ADRs)

- Documents/PDF page-multiplied cost — ADR-0102 / FRE-686 (reuses this machinery).
- Local-turn zero metering already flows via `ctx.turn_cost_usd += 0`; no new local path.
- GPT cloud entry pricing — add only if a follow-up needs it (Claude vision is the AC surface).

## Risks / gotchas

- `litellm.register_model` mutates a **global** — registration is idempotent and startup-only; tests call
  it explicitly and are order-independent (they assert our exact keys).
- Pricing must land in **both** `models.yaml` and `models.cloud.yaml` (prod loads `.cloud`).
- Deploy = `seshat-gateway` rebuild (always-ask class) — master decides timing.
