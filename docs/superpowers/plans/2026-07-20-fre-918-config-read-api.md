# FRE-918 — ADR-0121 T3: config read API + provider-keyed availability

**Ticket:** FRE-918 (Approved) · **ADR:** ADR-0121 §3, §7 step 3 · **Depends on:** FRE-917 (T2, merged PR #588)
**Branch:** `fre-918-adr-0121-t3-config-read-api`

## Scope

1. A session-scoped **config read endpoint** — `GET /api/v1/sessions/{session_id}/config` —
   returning, for every declared role: whether it's `open` or pinned, the **effective resolved
   binding for this session** (selection-applied, not a raw default), and for open roles the
   **candidate list** (available, kind-compatible deployments). Plus a **provider table**
   (placement + live availability).
2. A **per-provider health check**, replacing the "cloud is available iff Anthropic key present"
   proxy with one check per declared provider (secret presence for cloud, live SLM probe for
   local). Feeds the candidate filter (AC-5).

**Out of scope (explicitly deferred):** rewriting `/api/inference/status` (still serves the live
Path pill until T5/FRE-920 removes it — touching its contract now would break the pill);
`sub_agent`/`artifact_builder` profile-redirect bridging in the read payload (T2 established, and
its own tests assert, that session hydration resolves via the selection store + binding default
only, never re-deriving through `ExecutionProfile` — see "Design decision" below); ADR-0122's
per-build artifact_builder picker.

## Design decisions (settled during research + codex plan-review, before coding)

**1. Profile-bridging IS needed — codex plan-review caught a real gap.** My first draft assumed
`get_session`'s existing no-bridge pattern was safe to generalize (relying on the FRE-917
migration backfill + the pill's write-through coupling). Codex review found a live counterexample:
`POST /chat` (`service/app.py:1722-1845`, the legacy non-streaming endpoint — still used by the
CLI) creates a session with only `execution_profile` set and **never** calls
`_resolve_session_selection` or writes a selection-store row. For such a session on the `cloud`
profile, my original design would report the `primary` binding default (`qwen3.6-35b-thinking`,
local) while `get_llm_client()` actually dispatches via `resolve_profile_redirect` to
`claude_sonnet` — exactly the AC-9-slice failure mode ("a binding that differs from what the next
turn uses").

**Corrected design:** `_resolve_role_binding(db, session_uuid, role, execution_profile, config)`
in `gateway/session_api.py`, used by both `get_session` (refactored) and the new endpoint:

```python
async def _resolve_role_binding(db, session_uuid, role, execution_profile, config):
    binding = config.roles.get(role)
    if binding is not None and binding.open:
        stored = await SessionModelSelectionRepository(db).get(session_uuid, role)  # (try/except, log-and-None on failure)
        if stored is not None:
            resolved = resolve_selected_deployment(role, stored, config)
            return resolved, ("server-hydrated" if resolved == stored else "default")

    bridge = _profile_role_bridge(execution_profile, role)  # primary/sub_agent/artifact_builder only
    if bridge is not None and bridge in config.models:
        return bridge, "default"  # trusted profile value — NOT run through the user-selection guardrail

    return resolve_selected_deployment(role, None, config), "default"
```

Key points, each deliberate:
- The **selection-store lookup only happens for `open` roles** — ADR §6's "not consulted against
  the store at all" for pinned roles, taken literally (saves a query for the 6 writer roles too).
- The **profile bridge is checked independently of `open`** — `sub_agent` is pinned re: user
  selection (§6) but is *still* profile-redirected today (`factory.py:150-154`,
  `profile.py:114-116`); these are two orthogonal mechanisms and conflating them was my first
  draft's bug.
- The bridge value is returned **directly**, not through `resolve_selected_deployment` — that
  guardrail is for *user-supplied* keys (open-role check); a profile's own binding is trusted
  config, exactly how `factory.py:152-156` treats it (`resolve_role_target` with no guardrail
  call). Routing it through the guardrail would fail-closed `sub_agent`'s bridge to the local
  default, reintroducing the bug.
- Provenance stays **binary** (`"server-hydrated"` / `"default"`) — matching `get_session`'s
  existing, tested contract (`test_get_session_stale_stored_key_provenance_is_default`,
  `test_get_session_selection_defaults_when_no_row`) — a bridge that happens to resolve is still
  labelled `"default"`, since the label is a coarse UI hint and AC-9-slice's actual gate is the
  *resolved key*, not the label text.

Verified against all three existing `get_session` primary tests (hydration, no-row-local,
stale-key) — all three still pass unchanged with this function (worked through by hand: the
no-row-local case bridges to `local.yaml`'s `primary_model: qwen3.6-35b-thinking`, identical to
the binding default, so neither the resolved key nor the `"default"` label changes).

**2. `/api/inference/status` stays untouched — noted as a deliberate, documented scope
narrowing.** ADR §3/step-3 text literally says the endpoint "becomes provider health"; codex
review confirms this is a real wording gap, not a misreading, but agrees the deferral is
operationally defensible: the endpoint still serves the live Path pill until T5/FRE-920 removes
it, and changing its contract now risks the pill. This will be called out explicitly in the PR
description and Linear handoff for master's gate, rather than silently narrowed — matching how
T1/T2 documented their own staged-delivery amendments directly in the ADR's Status Updates.

**2. `/api/inference/status` stays untouched.** The ADR's step-3 text says the endpoint
"becomes provider health" — but that's Path's own removal (T5), when the pill disappears. Until
then the PWA still polls `?profile=local|cloud`; changing its contract now is an unrelated,
avoidable regression. The new provider-health check is built as its own reusable module and
consumed only by the new read endpoint. `/api/inference/status`'s local probe is reused (same
`probe_slm_health` call), not duplicated behaviourally, just called from a new call site too.

**3. Response shape is a plain dict**, matching `gateway/session_api.py`'s existing convention
(no `response_model=` on any route in this router; `get_session`/`list_sessions` all return
dicts). Introducing a new Pydantic response-model style here would diverge from the file's own
convention for no benefit.

## Files touched

- **New:** `src/personal_agent/llm_client/provider_health.py` — `is_provider_available()`,
  `check_all_providers()`.
- **Edit:** `src/personal_agent/config/model_loader.py` — add `role_candidates()`.
- **Edit:** `src/personal_agent/gateway/session_api.py` —
  - extract `_resolve_role_binding(db, session_uuid, role, config)` (generalizes `get_session`'s
    existing inline primary-selection block to any role; `get_session` is refactored to call it
    for `"primary"`, behaviour-preserving — existing tests must still pass unchanged).
  - add `_deployment_view()`, `_provider_view()` dict builders.
  - add `GET /{session_id}/config` endpoint.
- **New tests:** `tests/test_llm_client/test_provider_health.py`,
  additions to `tests/personal_agent/gateway/test_session_api.py`, a `role_candidates` unit test
  in `tests/test_config/test_model_loader.py`.

## Implementation

### 1. `llm_client/provider_health.py` (new)

```python
async def is_provider_available(provider: ProviderDefinition, settings: AppConfig, *, trace_id=None) -> bool:
    if provider.placement is Placement.CLOUD:
        return provider.auth_env is None or bool(getattr(settings, provider.auth_env, None))
    # LOCAL: live SLM-tunnel probe (reuses probe_slm_health — same call app.py's
    # inference_status makes). "down" excludes; "up"/"degraded" both count as available.
    ...
    return snapshot.status != "down"

async def check_all_providers(config: ModelConfig, settings: AppConfig, *, trace_id=None) -> dict[str, bool]:
    return {key: await is_provider_available(p, settings, trace_id=trace_id) for key, p in config.providers.items()}
```

Cloud check is config-only (secret presence), matching `/api/inference/status`'s existing cloud
branch and the ADR's own text ("endpoint reachable, required secret present" — for a
vendor-managed cloud API, secret presence *is* the practical signal; a live network probe per
cloud provider on every config-read is unwarranted network chattiness this ADR doesn't ask for).

### 2. `config/model_loader.py::role_candidates()` (new, pure function)

```python
def role_candidates(role: str, config: ModelConfig, provider_availability: Mapping[str, bool]) -> list[str]:
    binding = config.roles.get(role)
    if binding is None or not binding.open:
        return []  # ADR-0121 §6: kind-compatible ∩ open — both halves required, not just kind
    required = required_kind_for_role(role)
    return [
        key for key, model in config.models.items()
        if model.kind is required and provider_availability.get(model.provider or "", False)
    ]
```

Codex plan-review flagged that a kind-only filter is weaker than §6's authorization rule
(kind-compatible **∩** open) — fixed by gating on `binding.open` inside the function itself, so
it's safe to call for any role rather than relying on every caller to pre-check `open`. This is
the AC-5 set-equality logic, kept pure and DB/async-free so it's trivially unit-testable.

### 3. `gateway/session_api.py`

`_resolve_role_binding` — see the corrected design above (§ Design decisions 1). `get_session`
(lines 204-237 today) is refactored to call
`_resolve_role_binding(db, uuid, "primary", str(session.execution_profile), config)` instead of
its inline duplicate — verified by hand against all three existing primary tests.

`_profile_role_bridge(profile_name, role)` — small helper, `{"primary": "primary_model",
"sub_agent": "sub_agent_model", "artifact_builder": "artifact_builder_model"}.get(role)`, then
`getattr(load_profile(profile_name), attr, None)` guarded by `(FileNotFoundError, ValueError)`
→ `None` (mirrors `app.py`'s existing `_profile_primary` closure, generalized to any role name).

New endpoint:

```python
@router.get("/{session_id}/config")
async def get_session_config(request, session_id, token=Depends(require_scope("sessions:read")), db=Depends(_get_db)) -> dict[str, Any]:
    # auth, UUID validation, ownership 404 — same pattern as get_session
    config = load_model_config()
    availability = await check_all_providers(config, settings, trace_id=ctx.trace_id)
    roles = {}
    for role, binding in config.roles.items():
        resolved, provenance = await _resolve_role_binding(db, uuid, role, config)
        entry = {"open": binding.open, "resolved": resolved, "provenance": provenance}
        if binding.open:
            entry["candidates"] = [
                _deployment_view(k, config.models[k], config)
                for k in role_candidates(role, config, availability)
            ]
        roles[role] = entry
    providers = [_provider_view(k, p, availability.get(k, False)) for k, p in config.providers.items()]
    return {"session_id": session_id, "roles": roles, "providers": providers}
```

## Acceptance criteria proof plan

- **AC-5** (`role_candidates` + endpoint test): with a mocked availability map
  `{"slm_local": False, "anthropic": True, "openai": True, "voyage": True, "ovh": True}`, assert
  `set(role_candidates("primary", config, availability)) == {"claude_sonnet", "claude_haiku", "gpt-5.4-mini"}`
  exactly — both directions (qwen* absent because `slm_local` is down; claude/gpt present; no
  embedding/reranker key ever appears because `kind` filters first). A second case with everything
  up asserts all 5 LLM deployments present. An endpoint-level test asserts the same set inside
  `roles["primary"]["candidates"]`'s `key` fields.
- **AC-9 slice**: a test seeds a stored `primary` selection (`"claude_sonnet"`) and asserts
  `roles["primary"]["resolved"] == "claude_sonnet"` with `provenance == "server-hydrated"` — not
  the binding default (`qwen3.6-35b-thinking"`) — proving the payload reflects the
  selection-applied value, matching what `get_llm_client()` would actually resolve for that
  session's next turn (same store, same guardrail function).

## Test commands

```bash
make test-file FILE=tests/test_llm_client/test_provider_health.py
make test-file FILE=tests/test_config/test_model_loader.py
make test-file FILE=tests/personal_agent/gateway/test_session_api.py
make test
make mypy
make ruff-check
```
