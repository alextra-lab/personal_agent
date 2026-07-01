# FRE-665: Model `supports_vision` flag + capability-driven routing with fail-fast

**Ticket 3 of the ADR-0101 chain.** Backing: ADR-0101 Decision §5, §8a; ADR-0099; ADR-0033.
Branch: `fre-665-vision-capability-routing`.

## Scope

This ticket builds the **routing decision only** — not content-block construction (FRE-666)
and not cost-gate reservation (FRE-691), both of which depend on this ticket. Concretely:

1. `ModelDefinition.supports_vision` flag (schema + config values).
2. The routing seam asserts vision capability when an image attachment is present: proceed,
   escalate, or fail closed with a new `AttachmentUnsupportedError`.
3. The `processing_target` per-attachment override (`"local"` / `"cloud"` / `None`) is honored
   ahead of the profile default, per §8a.

**Acceptance criteria owned:** AC-4 (routing guarantees vision capability), AC-9 (per-attachment
override honored, fail-closed) — the FRE-665 slice only (no cost-gate assertion; that's FRE-691).

## Design decisions (confirmed with owner)

- **Default local routing is unaffected.** `primary`/`sub_agent` (Qwen3.6-35B-A3B) get
  `supports_vision: true` — they are the vision-capable deployed build per the ADR's grounding
  facts. A local-profile turn with no override stays on Qwen; cloud is never touched.
- **Cloud override target:** populate `config/profiles/local.yaml`'s existing
  `delegation.escalation_model: claude_sonnet` (+ `escalation_provider: anthropic`). This field is
  read *directly* by the forced `"cloud"` override path, bypassing `allow_cloud_escalation`. The
  implicit/no-override escalation branch (§5) still checks `allow_cloud_escalation` and stays
  `false` for local — unchanged default behavior.
- **Conflicting per-attachment overrides in one turn** (one `"local"`, one `"cloud"`): `"local"`
  wins — the whole turn is treated as local-only. Errs toward never leaking a locally-pinned image.

## Step 1 — `ModelDefinition.supports_vision` flag

**File:** `src/personal_agent/llm_client/models.py`

Add to `ModelDefinition` (near `supports_function_calling`, ~line 154):

```python
supports_vision: bool = Field(
    False,
    description=(
        "Whether this model/deployment accepts image content blocks (ADR-0101 §5). "
        "A deployment property, not inferred — set explicitly per model definition."
    ),
)
```

Add one line to the class docstring's Attributes list.

**Test:** `tests/test_config/test_model_loader.py` — add
`test_model_definition_supports_vision_defaults_false` (no field set → `False`) and
`test_model_definition_supports_vision_explicit_true` (constructed with `supports_vision=True`
→ `True`).

## Step 2 — set the flag on deployed models

**File:** `config/models.yaml`

Add `supports_vision: true` to `primary`, `sub_agent`, `claude_sonnet`, `claude_haiku`. Leave all
other model entries (`reasoning_heavy`, `coding_large_context`, `gpt-5.4-nano`, `gpt-5.4-mini`,
`compressor`, `embedding`, `reranker`) without the field (defaults `False` — correct, none of them
are vision-capable / relevant).

**Test:** extend the existing "loads real config/models.yaml" test (wherever `test_model_loader.py`
already asserts on the real file) with an assertion that
`config.models["primary"].supports_vision is True` and same for `sub_agent`, `claude_sonnet`,
`claude_haiku`.

## Step 3 — populate the cloud-override escalation target

**File:** `config/profiles/local.yaml`

```yaml
delegation:
  allow_cloud_escalation: false
  escalation_provider: anthropic
  escalation_model: claude_sonnet
```

Add a comment: `# escalation_model is used only by the explicit per-attachment "cloud"`
`# processing_target override (ADR-0101 §8a) — allow_cloud_escalation stays false, so no`
`# implicit/default escalation happens.`

**Test:** `tests/test_config/test_profile.py` — add
`test_local_profile_escalation_model_set_for_vision_override` asserting
`load_profile("local").delegation.escalation_model == "claude_sonnet"` and
`load_profile("local").delegation.allow_cloud_escalation is False` (unchanged default behavior).

## Step 4 — `AttachmentUnsupportedError`

**File:** `src/personal_agent/exceptions.py`

```python
class AttachmentUnsupportedError(ValueError):
    """Raised when a turn cannot be routed to a vision-capable model for an attachment.

    ADR-0101 §5/§8a: routing never silently falls back or crosses a data-egress
    boundary implicitly. When no reachable model can serve an attachment — no
    vision-capable model on the bound profile, escalation forbidden, or a
    ``"local"`` override with no capable local model — this is raised with a
    message naming the unsupported modality, surfaced to the user verbatim.
    """
```

## Step 5 — routing seam

**File:** `src/personal_agent/orchestrator/executor.py`

Add a module-level constant and a new function near `_determine_initial_model_role` (~line 1313):

```python
_RASTER_IMAGE_CONTENT_TYPES = frozenset(
    {"image/png", "image/jpeg", "image/gif", "image/webp"}
)


def _resolve_vision_routing_key(ctx: ExecutionContext, role_name: str) -> str:
    """Resolve the model config key for this role, enforcing vision capability.

    No-op (returns the profile-resolved key unchanged) when the turn carries no
    raster-image attachment. Otherwise asserts the serving model supports vision —
    escalating or failing closed per ADR-0101 §5/§8a. ``"local"`` wins when
    attachments in the same turn carry conflicting ``processing_target`` values.

    Args:
        ctx: Execution context carrying ``attachments`` (FRE-661).
        role_name: The model role string (e.g. "primary").

    Returns:
        The model config key to use for this call.

    Raises:
        AttachmentUnsupportedError: No reachable model can serve the attachment.
    """
    from personal_agent.config.profile import get_current_profile, resolve_model_key

    image_attachments = [
        a for a in ctx.attachments if a.content_type in _RASTER_IMAGE_CONTENT_TYPES
    ]
    if not image_attachments:
        return resolve_model_key(role_name)

    targets = {a.processing_target for a in image_attachments if a.processing_target}
    effective_target: str | None = "local" if "local" in targets else next(iter(targets), None)

    models = load_model_config().models
    profile = get_current_profile()

    if effective_target == "local":
        # Bypass profile resolution deliberately: "local" must mean role_name's
        # own raw local deployment (e.g. "primary" → Qwen), never whatever a
        # cloud-bound profile would otherwise redirect this role to. Using
        # resolve_model_key() here would return "claude_sonnet" under an active
        # cloud profile — exactly the boundary crossing "local" must prevent
        # (ADR-0101 §8a; caught in codex plan review 2026-07-01).
        model_def = models.get(role_name)
        if (
            model_def is not None
            and model_def.provider_type == "local"
            and model_def.supports_vision
        ):
            return role_name
        raise AttachmentUnsupportedError(
            "This image is pinned to local-only processing, but the local model "
            "does not support vision. It will not be escalated to cloud."
        )

    if effective_target == "cloud":
        esc_key = profile.delegation.escalation_model if profile else None
        esc_def = models.get(esc_key) if esc_key else None
        if esc_def is not None and esc_def.provider_type != "local" and esc_def.supports_vision:
            return esc_key
        raise AttachmentUnsupportedError(
            "This image is marked for cloud processing, but no vision-capable "
            "cloud model is configured for the active profile."
        )

    # No override — follow the profile default (§5).
    key = resolve_model_key(role_name)
    model_def = models.get(key)
    if model_def is not None and model_def.supports_vision:
        return key

    if profile is not None and profile.delegation.allow_cloud_escalation:
        esc_key = profile.delegation.escalation_model
        esc_def = models.get(esc_key) if esc_key else None
        if esc_def is not None and esc_def.supports_vision:
            return esc_key

    raise AttachmentUnsupportedError(
        "This turn includes an image, but the model serving this conversation "
        "does not support vision and no cloud escalation is available."
    )
```

(`load_model_config` is already imported at module scope via `personal_agent.config` — confirm the
existing import; add if missing. `AttachmentUnsupportedError` imported from
`personal_agent.exceptions`.)

### Wire into `step_llm_call`

**Design constraint (codex plan review 2026-07-01):** `tests/test_orchestrator/test_executor.py`
and `test_routing.py` patch `personal_agent.llm_client.factory.get_llm_client` directly for the
existing no-attachment path (e.g. `test_execute_simple_task`,
`test_execute_task_uses_profile_resolved_model_config` at test_executor.py:103,733). Unconditionally
switching the call site to `get_llm_client_for_key` would silently stop intercepting those mocks.
Instead, branch **only when escalation actually changed the key**, so the zero-attachment /
no-escalation path calls the exact same `get_llm_client(role_name=...)` the existing tests already
mock — no changes needed to those tests:

```python
from personal_agent.config.profile import resolve_model_key

role_key = resolve_model_key(model_role.value)  # unchanged existing resolution
effective_model_key = _resolve_vision_routing_key(ctx, model_role.value)

if effective_model_key == role_key:
    from personal_agent.llm_client.factory import get_llm_client

    llm_client = get_llm_client(role_name=model_role.value)  # exact existing call, unchanged
else:
    from personal_agent.cost_gate import budget_role_for
    from personal_agent.llm_client.factory import get_llm_client_for_key

    llm_client = get_llm_client_for_key(
        effective_model_key, budget_role=budget_role_for(model_role.value)
    )
...
model_config = llm_client.model_configs.get(effective_model_key)
```

`_resolve_vision_routing_key`'s no-attachment branch returns `resolve_model_key(role_name)` — the
same value as `role_key` — so the `==` check reliably detects "no escalation happened" without
duplicating the no-attachment special case. This call must stay **inside** the existing `try:`
block (~line 2479) so a raised `AttachmentUnsupportedError` flows into the existing
`except Exception as e:` handler (~line 3009) and its `classify_error(e)` call, rather than
propagating uncaught above the state machine.

**New tests only** (the escalation branch) need to patch `get_llm_client_for_key`; existing tests
are untouched.

## Step 6 — error classification (user-visible surfacing)

**File:** `src/personal_agent/error_classification.py`

- Add `"attachment_unsupported"` to `ClassifiedError.category`'s `Literal`.
- Add a branch in `classify_error`:

```python
from personal_agent.exceptions import AttachmentUnsupportedError

if isinstance(error, AttachmentUnsupportedError):
    return ClassifiedError(
        category="attachment_unsupported",
        reason=str(error),
        next_step="Remove the attachment, or resubmit without the local/cloud override.",
        actions=("stop",),
    )
```

Place this branch before the generic fallback (order relative to the other branches doesn't
matter — `AttachmentUnsupportedError` is disjoint from the LLM-client exception hierarchy).

**Test:** `tests/test_orchestrator/` (or wherever `error_classification` is tested) —
`test_classify_error_attachment_unsupported` asserting category, reason echoes the raised message,
actions `== ("stop",)`.

## Step 7 — routing tests (AC-4, AC-9)

**File:** `tests/test_orchestrator/test_routing.py` (new test class, e.g. `TestVisionRouting`)

Using `configure_mock_llm_client_model_configs` / direct `ExecutionContext` construction with
`attachments=(AttachmentRef(...),)`:

1. **AC-4a — capable primary, no override:** local profile, `primary.supports_vision=True` (real
   config) → `_resolve_vision_routing_key` returns `"primary"` unchanged.
2. **AC-4b — incapable primary, escalation permitted:** monkeypatch/construct a `ModelConfig` where
   the resolved primary has `supports_vision=False`, profile has
   `allow_cloud_escalation=True` + `escalation_model` set to a vision-capable entry → returns the
   escalation key.
3. **AC-4c — incapable primary, escalation forbidden:** same but `allow_cloud_escalation=False` →
   raises `AttachmentUnsupportedError`.
4. **AC-9a — `"local"` override, escalation-permitted profile, non-vision local model:** assert
   `AttachmentUnsupportedError` is raised and no cloud key is ever returned (no silent escalation).
5. **AC-9b — `"cloud"` override on a local-profile conversation:** assert the returned key is
   `local.yaml`'s configured `escalation_model` (`"claude_sonnet"`) — the cloud vision path — even
   though the profile's own `allow_cloud_escalation` is `False`.
6. **Conflict tie-break:** one attachment `"local"`, one `"cloud"` in the same
   `ctx.attachments` tuple → effective target resolves to `"local"`.
7. **No image attachment:** `ctx.attachments=()` or a non-raster `content_type` (e.g.
   `application/pdf`) → returns `resolve_model_key(role_name)` unchanged, no exception, no vision
   logic triggered (document types are ADR-0102 territory, out of scope here).

Run: `make test-file FILE=tests/test_orchestrator/test_routing.py`

## Step 8 — full quality gates

```bash
make test-file FILE=tests/test_config/test_model_loader.py
make test-file FILE=tests/test_config/test_profile.py
make test-file FILE=tests/test_orchestrator/test_routing.py
make test        # full suite
make mypy
make ruff-check
make ruff-format
pre-commit run --all-files
```

## Explicitly out of scope (owned by later tickets in the chain)

- Constructing the actual image content block / turn-assembly injection — **FRE-666**.
- Cost-gate pre-flight estimate/reservation for the forced `"cloud"` path — **FRE-691**.
- PWA per-attachment override affordance — **FRE-692**.
- Joinability threading on routing telemetry — **FRE-693**.
- The `ADR-0099` "validator rejects a vision-capable profile whose primary lacks the flag" note in
  the ticket's prose — not a numbered AC on this ticket's slice, and the fail-closed routing logic
  above already is the enforcement mechanism (per the ADR's own Risks table: "Capability flag
  reflects the deployed build... routing escalates/raises rather than failing opaquely"). Deferred;
  flag as a follow-up ticket only if the owner wants a startup-time check in addition.
