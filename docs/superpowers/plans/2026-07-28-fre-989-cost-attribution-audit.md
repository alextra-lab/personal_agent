# FRE-989 — Cost attribution audit: make role resolution total, defaults singular, spend measurable

**Ticket:** FRE-989 (Approved, Urgent, `stream:build1`, `Tier-1:Opus`)
**Backing design:** ADR-0065 (Cost Check Gate — operative). ADR-0120 (supersedes 0065) is **Proposed**, not Accepted.
**Base:** `origin/main` @ `002e2de7`
**Related:** FRE-987 (the incident), FRE-1037 (widened the role enum, landed 3 of the map entries), FRE-974 (vendor cost ledger), FRE-979 (ES cost skill)

---

## 1. Verified state — corrections to the ticket text

Everything below was read on `origin/main` @ `002e2de7`. Where the ticket's description differs, the code wins.

| # | Ticket claim | Verified reality |
|---|---|---|
| F1 | Three roles missing from the map: `skill_routing`, `study`, `session_summary` | **2 of 3 delivered by FRE-1037.** `cost_gate/__init__.py:117-128` now maps `session_summary`→`captains_log`, `vision`→`main_inference`, `skill_routing`→`skill_routing`. **`study` is still absent.** No spend has moved: `study`'s only live site (`scripts/study/categorizer.py:145`) constructs `LiteLLMClient` **directly** with `budget_role="study"`; the eval scripts pass it explicitly to `get_llm_client_for_key`. The gap is latent, not live. |
| F2a | `insights`/`promotion`/`freshness` "pass the gate unbounded" | **Overstated.** `BudgetConfig.caps_for()` (`cost_gate/types.py:204-210`) returns same-role caps **plus** the synthetic `_total` caps. `_total` weekly = `$30.00`, so all three are bounded by the global weekly ceiling. What *is* true: they get **no per-role counter row**, so per-role counter queries and the FRE-547 cap-utilization panel return nothing for them. |
| F2b | Their spend is "unmeasurable" | **True of the read path only, not the ledger.** `LiteLLMClient` writes `purpose=self.budget_role` into `api_costs` on **every** paid call regardless of caps (`litellm_client.py:696`). Per-role spend is fully measurable in Postgres today. The real hole is Elasticsearch: `api_cost_recorded` (`cost_tracker.py:239`) carries **no** `purpose`/role field **and** is emitted at `log.debug`. |
| — | (not in ticket) | **`promotion` and `freshness` are dead lanes.** Declared in `budget.yaml`, mapped in the resolver, but **zero call sites** anywhere in `src/` or `scripts/` use either budget role. `insights` is live (`captains_log/feedback.py:279`). |
| F3 | Three conflicting defaults | **Confirmed exactly.** `get_llm_client_for_key(budget_role="skill_routing")` (`factory.py:158`); `LiteLLMClient.__init__(budget_role="main_inference")` (`litellm_client.py:285`); `budget_role_for`'s `"main_inference"` fallback (`cost_gate/__init__.py:145`). |
| F4 | Two interchangeable acquisition paths | **Three.** `get_llm_client(role_name=…)`, `get_llm_client_for_key(key, budget_role=…)`, and **direct `LiteLLMClient(...)` construction** — 2 production sites (`second_brain/entity_extraction.py:987`, plus `factory._build_client`) and 2 scripts. Every production construction currently passes `budget_role` explicitly, so the constructor default is a latent trap, not a live defect. |
| F5 | Unverified: do embeddings/rerankers pass through the gate? | **Answered: they bypass it entirely.** `record_vendor_cost` (`cost_tracker.py:459-533`) writes an `api_costs` row with `purpose="embedding"`/`"reranker"` and **never calls `CostGate.reserve()`**. Cost is *recorded* but never *reserved*; no cap applies at any window. |
| **F6** | *(new — found during this audit, codex plan-review)* | **The role-name path cannot produce a paid call for `skill_routing` or `study` at all.** Neither role has a Layer-3 binding in `config/model_roles.yaml`, so `resolve_role_target` (`model_loader.py:341-359`) treats the role name as a deployment key, finds no definition, and `_build_client` (`factory.py:76-88`) falls through to **`LocalLLMClient`**. AC-4 as the ticket words it — "a call made through the role-name path … is charged to that role's budget" — describes a path that does not exist. Their real acquisitions are key-based (`executor.py:4003`) and direct construction (`categorizer.py:145`). |
| **F7** | *(new)* | **Gateway streaming is a paid path with no ledger row.** `gateway/chat_api.py` talks to the Anthropic SDK directly; it *does* reserve and commit against the gate (`chat_api.py:463-501`, `:187-202`) but **never calls `record_api_call`**. A paid streaming turn moves the `main_inference` counter and writes **no `api_costs` row**. It also proceeds **ungated** when the gate singleton is absent (`chat_api.py:492-499`). |
| **F8** | *(new — the serious one)* | **The cloud DSPy path bypasses both the gate and the ledger.** `get_dspy_lm` (`dspy_adapter.py:114-142`) builds a raw `dspy.LM` with the provider API key for any cloud-placed role. `reflection_dspy.py:417` uses it with `role=ModelRole.CAPTAINS_LOG`, whose binding is `claude_sonnet` — **cloud**. So Captain's Log reflection, the exact role behind the FRE-987 incident, makes paid calls that reserve nothing and record nothing. |

**One constraint that shapes the whole plan:** ADR-0120's decision 1 is *"Kill hard, process-breaking dollar caps."* It is `Proposed`, so ADR-0065 stays operative — but **setting new dollar caps now is work ADR-0120 would delete.** This plan therefore delivers *attribution and measurability* (which ADR-0120 needs regardless — "you cannot alert on, or gate, a vendor you don't measure") and does **not** invent new cap values.

---

## 2. Scope

**In this PR (write-path attribution + the invariants that keep it true):**

1. Make role resolution **total and fail-closed** — `budget_role_for` raises on an unknown name; add `study`. Validated **at startup**, not only at CI.
2. A **config-guard check** that fails CI when `ModelRole` ∪ resolver map ∪ `budget.yaml` roles drift apart — in **both** directions.
3. **Reconcile the three defaults to zero** — `budget_role` becomes required at all three doors.
4. Make an **absent cap a declared decision**, not a silent omission (`uncapped_roles:` in `budget.yaml`, enforced by the guard).
5. Make per-role spend **measurable in Elasticsearch** — add `purpose` to `api_cost_recorded` and raise it `debug`→`info`.
6. **Close F7** — write the `api_costs` row for gateway streaming (fold-in: ~15 lines; the actual cost is already computed for the commit).
7. **Close F8 — meter *and* gate the cloud DSPy path** (owner decision, 2026-07-29), at **job scope**.
8. **Audit output document** stating the authoritative cost source, its *actual* completeness, and the remaining F5 bypass boundary.

**Deliberately NOT in this PR (stated, not silently dropped):**

- **New dollar caps for `insights`/`promotion`/`freshness`** — see the ADR-0120 constraint above. Recorded as a deliberate uncapped decision instead.
- **Gating embeddings/rerankers** (Finding 5) — a vendor-API surface with no message/token estimator, unlike the DSPy path which reuses the existing chat estimator. Follow-up ticket.
- **The read-path consumer fixes** (Linear comment defects 1 & 2 — stale-counter reads, the `@timestamp` silent-zero trap) — those live in the ES cost skill / agent consumer, not the gate. The audit doc states the contract authoritatively; a follow-up ticket carries the skill change.
- **Removing the dead `promotion`/`freshness` lanes** — config removal beyond the ticket; surfaced in the audit doc.
- **Giving `skill_routing`/`study` a Layer-3 binding** (F6) — would change which model those roles run on. A model-assignment decision, not an audit fix.

### 2.1 AC-2 and AC-3 — what "every path" means after this PR

The ticket words AC-2 as "**every** LLM invocation path" and AC-3 as "spend for **every** declared role is measurable". With F7 and F8 both closed here, the remaining residue is small and named explicitly rather than left implicit:

**Covered after this PR:** `LiteLLMClient` (all three acquisition doors) · gateway streaming (step 6) · the cloud DSPy channel (step 7) · `record_vendor_cost` vendor calls.

**Residue, stated at the gate — measured but not gated, or neither:**

| Path | Reserved? | In `api_costs`? | Disposition |
|---|---|---|---|
| Embeddings / rerankers (F5) | **No** | Yes (`purpose=embedding`/`reranker`) | Follow-up ticket — no token estimator for the vendor API shape |
| `LiteLLMClient` calls with no `session_id` (`litellm_client.py:666-682`) | Yes | **No** | Pre-existing, deliberate ADR-0074 identity contract; named in the audit doc |
| Intermediate LiteLLM retry attempts (`num_retries`, `litellm_client.py:427-437`) | Once for the job | One row for the final response | Pre-existing; named in the audit doc |
| Raw `litellm.acompletion` in two eval scripts | No | No | Follow-up ticket; eval-only, hand-run |

Master's gate should read AC-2/AC-3 as **met for every production serving path, with the four residues above named and three of them ticketed** — not as unqualified totality.

---

## 3. Acceptance criteria → proof

The ticket's "Proof required" paragraph is the AC set.

| AC | Criterion | Proof |
|----|-----------|-------|
| AC-1 | Every budget role declared in the governance config has a corresponding entry in the resolver map, asserted by a test that fails when a role is added to one and not the other | `tests/personal_agent/cost_gate/test_role_map_totality.py::test_budget_yaml_roles_all_resolvable` + `::test_model_role_members_all_mapped` + the `config_guard` check `check_budget_role_coverage` (**both directions**), exercised by `tests/personal_agent/config/test_config_guard_budget_roles.py` — a synthetic role added to *either* side alone → guard emits a finding |
| AC-2 *(narrowed, §2.1)* | Every LLM invocation path resolves to a budget role deterministically, with no silent fallback, demonstrated by exercising each acquisition path | `::test_unknown_role_name_raises` + `tests/test_llm_client/test_budget_role_no_defaults.py` (no default at any of the three doors) + `tests/personal_agent/cost_gate/test_startup_role_totality.py` (startup validation raises on a map/enum/YAML mismatch) + the AC-4 end-to-end tests below. **Residue:** F7 fixed in step 6; F8 documented + ticketed |
| AC-3 *(narrowed, §2.1)* | Spend for every declared role is measurable, including roles that are currently uncapped | `tests/personal_agent/llm_client/test_cost_event_carries_role.py` (`api_cost_recorded` carries `purpose`, emitted at `info`) + `test_uncapped_role_still_records_cost` (a role with **no** per-role cap still writes an `api_costs` row carrying its `purpose`) + `tests/personal_agent/gateway/test_chat_api_records_cost.py` (step 6). **Residue:** the DSPy channel, named in the audit doc |
| AC-4 *(re-proved, see F6)* | A call through the role-name path for `skill_routing` or `study` is charged to that role's budget and denied by that role's cap, verified against the counters rather than by reading the code | **The role-name path for these two roles yields a `LocalLLMClient` and cannot make a paid call (F6)** — so the criterion is proved against the path each role *actually* uses. `tests/personal_agent/cost_gate/test_role_lane_isolation.py` (integration, real test Postgres :5433, provider mocked at the `litellm.acompletion` boundary only, following `test_litellm_gate_wiring.py:176-209`): drive a real call through `get_llm_client_for_key(key, budget_role="skill_routing")` and through `LiteLLMClient(..., budget_role="study")`; assert (a) the **current-window** `budget_counters` row for that role incremented, (b) `main_inference`'s did **not**, (c) the `api_costs` row carries `purpose` = that role, (d) reserving past the role's cap raises `BudgetDenied` naming that role **before** the provider is invoked. Plus `::test_role_name_path_for_skill_routing_is_local` pinning F6 so it cannot regress silently |

---

## 4. Steps

### Step 1 — Failing tests first (TDD)

Create `tests/personal_agent/cost_gate/test_role_map_totality.py`:

```python
"""FRE-989 AC-1/AC-2: role resolution is total and fail-closed."""

from __future__ import annotations

import pytest

from personal_agent.cost_gate import NON_GATED_ROLES, budget_role_for, load_budget_config
from personal_agent.cost_gate.role_map import BUDGET_ROLE_BY_FACTORY_NAME
from personal_agent.llm_client.types import ModelRole


def test_model_role_members_all_mapped() -> None:
    """Every ModelRole member either resolves to a budget lane or is declared non-gated."""
    unmapped = {
        role.value
        for role in ModelRole
        if role.value not in BUDGET_ROLE_BY_FACTORY_NAME and role.value not in NON_GATED_ROLES
    }
    assert not unmapped, (
        f"ModelRole members with no budget lane and no non-gated declaration: {sorted(unmapped)}. "
        "Add an entry to BUDGET_ROLE_BY_FACTORY_NAME or to NON_GATED_ROLES."
    )


def test_budget_yaml_roles_all_resolvable() -> None:
    """Every role declared in budget.yaml is reachable by name through the resolver."""
    declared = set(load_budget_config().roles)
    unreachable = {r for r in declared if budget_role_for_or_none(r) != r}
    assert not unreachable, f"budget.yaml roles not self-resolving: {sorted(unreachable)}"


def test_map_targets_are_declared_budget_roles() -> None:
    """No map entry points at a budget lane budget.yaml does not declare."""
    declared = set(load_budget_config().roles)
    dangling = {k: v for k, v in BUDGET_ROLE_BY_FACTORY_NAME.items() if v not in declared}
    assert not dangling, f"map entries pointing at undeclared budget roles: {dangling}"


def test_study_has_its_own_lane() -> None:
    """FRE-989 F1 residue: study no longer falls through to main_inference."""
    assert budget_role_for("study") == "study"


def test_unknown_role_name_raises() -> None:
    """AC-2: no silent fallback — an unmapped name is a loud failure."""
    with pytest.raises(UnknownBudgetRoleError):
        budget_role_for("definitely_not_a_role")
```

(`budget_role_for_or_none` and `UnknownBudgetRoleError` are introduced in Step 2; the import list above is finalised there.)

Create `tests/test_llm_client/test_budget_role_no_defaults.py` (AC-2, per-path):

```python
"""FRE-989 AC-2: one budget role, set explicitly, at every acquisition door."""

import inspect

import pytest

from personal_agent.llm_client.factory import get_llm_client_for_key
from personal_agent.llm_client.litellm_client import LiteLLMClient


def test_litellm_client_requires_budget_role() -> None:
    """Door 3 (direct construction) has no default to fall into."""
    sig = inspect.signature(LiteLLMClient.__init__)
    assert sig.parameters["budget_role"].default is inspect.Parameter.empty


def test_get_llm_client_for_key_requires_budget_role() -> None:
    """Door 2 (trusted-config key) has no default to fall into."""
    sig = inspect.signature(get_llm_client_for_key)
    assert sig.parameters["budget_role"].default is inspect.Parameter.empty


@pytest.mark.parametrize(
    ("role_name", "expected_lane"),
    [
        ("primary", "main_inference"),
        ("sub_agent", "main_inference"),
        ("artifact_builder", "artifact_builder"),
        ("captains_log", "captains_log"),
        ("session_summary", "captains_log"),
        ("skill_routing", "skill_routing"),
        ("study", "study"),
        ("entity_extraction", "entity_extraction"),
        ("insights", "insights"),
        ("vision", "main_inference"),
    ],
)
def test_role_name_path_resolves_deterministically(role_name: str, expected_lane: str) -> None:
    """Door 1 (role-name) resolves every live role to a declared lane, no fallback."""
    from personal_agent.cost_gate import budget_role_for

    assert budget_role_for(role_name) == expected_lane
```

Create `tests/personal_agent/cost_gate/test_role_lane_isolation.py` (AC-4, integration, real test Postgres :5433 — marked `integration`, fixture pattern from `tests/personal_agent/llm_client/test_litellm_gate_wiring.py:176-209`).

**Not** direct `CostGate.reserve()` calls: a test that resolves a string and reserves proves the gate works, not that the *acquisition path* bills correctly — it would still pass if `get_llm_client` dropped the role, if `respond()` reserved against something else, or if the tracker wrote a null `purpose`. Each test drives a real `respond()` with `litellm.acompletion` mocked at the provider boundary and asserts against real Postgres state:

- `test_skill_routing_charges_its_own_lane_end_to_end` — client from `get_llm_client_for_key(settings.skill_routing_model_key, budget_role="skill_routing")` (the production acquisition, `executor.py:4003`); after `respond()`, assert the **current-window** `budget_counters` row for `role='skill_routing'` moved, `main_inference`'s daily row did **not**, and the `api_costs` row has `purpose='skill_routing'`.
- `test_study_charges_its_own_lane_end_to_end` — same via `LiteLLMClient(..., budget_role="study")` (the production acquisition, `categorizer.py:145`).
- `test_skill_routing_denied_by_its_own_cap` — pre-load the counter past `$0.10`; assert `BudgetDenied.role == "skill_routing"`, `time_window == "daily"`, and that `litellm.acompletion` was **never awaited** (denial precedes spend).
- `test_study_denied_by_its_own_cap` — same against `$5.00`.
- `test_role_name_path_for_skill_routing_is_local` — pins F6: `get_llm_client(role_name="skill_routing")` returns a `LocalLLMClient`, with a docstring saying *why* (no Layer-3 binding), so the ticket's stated path can't be silently assumed paid.

Create `tests/personal_agent/cost_gate/test_startup_role_totality.py` — the totality validator raises on a synthetic map/enum/YAML mismatch and is a no-op on the real config.

Create `tests/personal_agent/llm_client/test_cost_event_carries_role.py` (AC-3) — capture the structlog output of `record_api_call` with `capture_logs`; assert the `api_cost_recorded` entry has `purpose == "insights"` and `log_level == "info"`.

Create `tests/personal_agent/config/test_config_guard_budget_roles.py` (AC-1 guard) — build a synthetic budget config + role map where one side declares a role the other does not; assert `check_budget_role_coverage` returns a finding naming that role; assert the real repo config produces **zero** findings.

**Verify:** `uv run pytest tests/personal_agent/cost_gate/ tests/test_llm_client/test_budget_role_no_defaults.py tests/personal_agent/config/test_config_guard_budget_roles.py -q` → all new tests **fail** (import errors / assertion failures). Record the output.

### Step 2 — Total, fail-closed role resolution

Extract the map into `src/personal_agent/cost_gate/role_map.py` (it is now read by three consumers — the resolver, the guard, and the tests; leaving it private in `__init__.py` forces underscore-imports):

```python
"""Factory role_name → budget-role resolution (ADR-0065; FRE-989 made it total).

Resolution is **total and fail-closed**: a name this module does not know is a
``UnknownBudgetRoleError``, never a silent fallback to ``main_inference``. The
fallback this replaced is what let FRE-1037's newly-threaded ``study`` role bill
against the user-facing budget instead of its own $5 isolation lane, with no
signal at any layer (FRE-989 finding one).

The pairing between this map, ``ModelRole`` and ``config/governance/budget.yaml``
is asserted at CI time by
:func:`personal_agent.config.config_guard.check_budget_role_coverage`, so a role
added to one and not the others fails the build rather than resolving wrongly.
"""


class UnknownBudgetRoleError(ValueError):
    """Raised when a factory role name has no declared budget lane."""


# Roles that legitimately never acquire a gated LLM client. Their vendor cost is
# recorded through ``cost_tracker.record_vendor_cost`` (FRE-974) and does **not**
# pass ``CostGate.reserve`` at all — see FRE-989 finding five, and the audit doc
# at docs/research/2026-07-29-fre-989-cost-attribution-audit.md for the boundary.
NON_GATED_ROLES: frozenset[str] = frozenset({"embedding", "reranker", "reranker_fallback"})

BUDGET_ROLE_BY_FACTORY_NAME: dict[str, str] = {
    # ... existing entries unchanged ...
    # FRE-989: the residue FRE-1037 left behind. study's $5 daily / $7 weekly
    # isolation (FRE-839) exists so a one-time corpus run can never contend with
    # live extraction; without this entry the role-name path defeated it.
    "study": "study",
}
```

Rewrite `budget_role_for` in `cost_gate/__init__.py`:

```python
def budget_role_for(factory_role_name: str) -> str:
    """Resolve a factory ``role_name`` to its budget role key.

    Total and fail-closed (FRE-989): an unrecognised name raises rather than
    defaulting. A silent default is indistinguishable from a correct mapping at
    every layer — the counters, the ledger and the telemetry all record the
    *wrong* lane with full confidence.

    The CI guard (``check_budget_role_coverage``) and the startup validator
    (:func:`validate_role_totality`) together make this raise unreachable for any
    role declared in ``ModelRole`` or ``budget.yaml``. They do **not** make it
    unreachable full stop: ``get_llm_client`` takes ``role_name: str``, so an
    arbitrary runtime string still reaches here and still raises — deliberately.
    On the orchestrator path that surfaces as a failed turn
    (``executor.step_llm_call`` converts it to ``TaskState.FAILED``), which is the
    intended trade: one loud failed turn beats silent mis-billing of every
    subsequent call.

    Args:
        factory_role_name: The ``role_name`` argument to ``get_llm_client``.

    Returns:
        Budget role key (declared in ``budget.yaml``).

    Raises:
        UnknownBudgetRoleError: If the name has no declared budget lane.
    """
    try:
        return BUDGET_ROLE_BY_FACTORY_NAME[factory_role_name]
    except KeyError:
        raise UnknownBudgetRoleError(
            f"No budget lane declared for factory role {factory_role_name!r}. "
            f"Add it to BUDGET_ROLE_BY_FACTORY_NAME (cost_gate/role_map.py) or, "
            f"if it never acquires a gated client, to NON_GATED_ROLES."
        ) from None
```

Re-export `UnknownBudgetRoleError`, `NON_GATED_ROLES`, `BUDGET_ROLE_BY_FACTORY_NAME` from `cost_gate/__init__.py` and add them to `__all__`.

Add `validate_role_totality(config: BudgetConfig) -> None` to `role_map.py` — the same three invariants the CI guard checks, raised as `BudgetConfigError`. Call it from the FastAPI lifespan hook where `set_default_gate` is registered, so a drifted deploy **fails to start** rather than mis-billing at runtime. (CI is not sufficient on its own: `budget.yaml` is a runtime config file that can differ from the tree CI validated — see the ADR-0121 "baked into the image" pattern.)

**Verify:** `uv run pytest tests/personal_agent/cost_gate/test_role_map_totality.py tests/personal_agent/cost_gate/test_startup_role_totality.py -q` → passes.

### Step 3 — Zero defaults at all three doors

- `litellm_client.py:285` — `budget_role: str` (no default). Update the docstring: the default is gone *because* three disagreeing defaults meant an unattributed call landed in an arbitrary bucket (FRE-989 finding three).
- `factory.py:158` — `get_llm_client_for_key(model_key: str, budget_role: str)` (no default). Update the docstring's "default `skill_routing`" paragraph.
- `executor.py:4003` — the one call site relying on the removed default: `get_llm_client_for_key(settings.skill_routing_model_key, budget_role="skill_routing")`.
- Test call sites: add an explicit `budget_role=` to each `LiteLLMClient(...)` construction in `tests/` (13 sites across 6 files) — mechanical, `budget_role="main_inference"` unless the test is lane-specific.

**Verify:** `uv run pytest tests/personal_agent/llm_client/ tests/test_llm_client/ -q` → green. `uv run mypy src/` → clean.

### Step 4 — An absent cap becomes a declared decision

Add to `config/governance/budget.yaml`:

```yaml
# Roles deliberately run without a per-role dollar cap (FRE-989 finding two).
# Listing a role here is a DECISION and is what distinguishes it from having
# forgotten the cap — config_guard fails the build on a declared role that is
# neither capped nor listed here.
#
# All three remain bounded by the weekly `_total` cap ($30) — "uncapped" means
# no *per-role* ceiling, not unlimited.
#
# Why no cap rather than a number: ADR-0120 (Proposed) supersedes ADR-0065 and
# its decision 1 is to remove hard, process-breaking dollar caps outright.
# Inventing cap values now is work that ADR would delete. Per-role spend is
# measurable in api_costs.purpose (FRE-989 step 5), so the number can be set
# from observation whenever ADR-0120 resolves.
uncapped_roles:
  - insights     # live — captains_log/feedback.py
  - promotion    # declared but with NO live call site as of 2026-07-28 (FRE-989)
  - freshness    # declared but with NO live call site as of 2026-07-28 (FRE-989)
```

Add `uncapped_roles: list[str] = Field(default_factory=list, description=...)` to `BudgetConfig` (`cost_gate/types.py`).

Add `check_budget_role_coverage(root)` to `config/config_guard.py` and register it in `run_all_checks`. **Four** findings — the fourth is the reverse direction AC-1 actually asks for, which the first draft of this plan left to the unit test alone:

1. a `ModelRole` member that is neither in `BUDGET_ROLE_BY_FACTORY_NAME` nor in `NON_GATED_ROLES`;
2. a map entry pointing at a budget role `budget.yaml` does not declare;
3. a `budget.yaml` role that has no cap entry and is not in `uncapped_roles`;
4. **a `budget.yaml` role with no self-resolving entry in the map** (`budget_role_for(role) != role`) — this is the direction that would have caught `study`.

**Verify:** `uv run python scripts/check_config.py` → exit 0. `uv run pytest tests/personal_agent/config/test_config_guard_budget_roles.py -q` → passes.

### Step 5 — Make per-role spend measurable in Elasticsearch

`cost_tracker.py:239` — the `api_cost_recorded` emit:
- add `purpose=purpose`;
- change `log.debug` → `log.info`.

Rationale in a comment: a cost event is a ledger record, not a debug detail, and without `purpose` Elasticsearch records *what* a call cost but not *which budget role spent it* — which makes the owner's "what did captains_log cost today" unanswerable from ES by construction (FRE-989, Linear comment defect 3).

Also add `"budget_role": self.budget_role` to `emit_model_call_completed`'s `extra` in `litellm_client.py` (parity with `model_call_started`, which already carries it).

**Verify:** `uv run python -m scripts.audit.telemetry_surface_check --gate --baseline scripts/audit/telemetry_surface_baseline.json` → passes. If the new `purpose` field trips the mapping lint, add the field to the corresponding ES index template rather than baselining the finding.

### Step 6 — Close F7: gateway streaming writes its ledger row

`gateway/chat_api.py` already computes the actual cost to settle the reservation (`_commit_reservation_safe`, `:230-268`). Return that value (or compute it once and pass it to both) and call `cost_tracker.record_api_call(provider="anthropic", model=f"anthropic/{_CLOUD_MODEL}", …, purpose="main_inference", …)` on the success path, alongside the existing `model_call_completed` emit.

Guard it the way `LiteLLMClient` does — best-effort, `log.error` on failure, never break the user's stream.

Also raise the `chat.cost_gate_not_initialized` path (`:492-499`) from `log.warning` to `log.error`: a paid streaming call proceeding with **no** gate is not a warning, it is unmetered spend.

Fold-in justification: this is a ~15-line supporting change that makes AC-3's authoritative-ledger claim true for a path the audit itself discovered. It is not separable work.

**Verify:** `tests/personal_agent/gateway/test_chat_api_records_cost.py` — mock the Anthropic stream, assert one `api_costs` row with `purpose='main_inference'` and the matching `trace_id`; assert a tracker failure does not propagate.

### Step 7 — Close F8: meter and gate the cloud DSPy path

**Why job scope, not per-LM-call.** `dspy.LM.forward` is synchronous and `generate_reflection_dspy` already runs in a worker thread via `asyncio.to_thread` (`reflection.py:404`); the gate is async. Reserving inside `LM.forward` would need an `asyncio.run_coroutine_threadsafe` bridge into the caller's loop — a real failure surface for no gain. Reserving around the *job* is the shape ADR-0120 already names ("keeps its `reserve()`/`commit()`/`refund()` primitive, re-applied at *job* scope").

**Cost is read from DSPy's own history.** Verified on the installed dspy 3.1.3: `BaseLM._process_lm_response` appends `{"cost": response._hidden_params["response_cost"], "usage": dict(response.usage), …}` to `lm.history` per call. `configure_dspy_lm` builds a **fresh** `dspy.LM` per reflection, so `lm.history` is exactly this job's calls. `cost` is `None` on a cache hit — which genuinely cost nothing, so it sums as `0.0`. If DSPy's `disable_history` is set, fall back to `pricing.py` over the usage totals, and if neither is available commit the original estimate (never silently commit zero).

New `src/personal_agent/llm_client/dspy_gate.py`:

```python
@dataclass
class DspyJobCost:
    """Mutable sink a worker thread fills in for its async caller to settle."""
    actual_cost_usd: Decimal = Decimal("0")
    input_tokens: int = 0
    output_tokens: int = 0
    call_count: int = 0
    cost_source: str = "unavailable"  # "dspy_history" | "pricing_fallback" | "unavailable"


def collect_dspy_cost(lm: Any, sink: DspyJobCost) -> None:
    """Sum a finished DSPy job's cost out of the LM's own history."""


@asynccontextmanager
async def gated_dspy_job(
    *, budget_role: str, model: str, messages: Sequence[dict[str, Any]],
    max_tokens: int, trace_ctx: TraceContext,
) -> AsyncIterator[DspyJobCost]:
    """Reserve → run → commit-and-record, around a synchronous DSPy job.

    Reuses ``estimate_reservation_for_call`` so the DSPy channel is sized by the
    same estimator as every other paid call. Refunds on any exception; commits
    the observed cost on success and writes the ``api_costs`` row that makes the
    job attributable (FRE-989 finding eight).
    """
```

`dspy_adapter.py` — extract the role→deployment resolution `configure_dspy_lm` already does into `resolve_dspy_target(role) -> tuple[str, ModelDefinition, bool]` (key, def, `is_cloud`), and have `configure_dspy_lm` call it. **One** resolution, not two — a second copy would be precisely the drift this ticket exists to remove.

`reflection_dspy.py` — `generate_reflection_dspy(..., cost_sink: DspyJobCost | None = None)`; after the `dspy.context` block, `collect_dspy_cost(lm, cost_sink)` when a sink was passed. Keyword-only with a default, so the existing test call sites are untouched.

`reflection.py` — inside the existing DSPy `try` (so a denial degrades exactly like any other DSPy failure and falls through to the already-gated manual path, honouring `captains_log`'s `on_denial: nack` without inventing new control flow):

```python
_key, _def, _is_cloud = resolve_dspy_target(_captains_log_role or ModelRole.CAPTAINS_LOG.value)
if _is_cloud:
    async with gated_dspy_job(
        budget_role=budget_role_for("captains_log"),
        model=f"{_def.provider}/{_def.id}",
        messages=[{"role": "user", "content": user_message}],
        max_tokens=_def.max_tokens or 4096,
        trace_ctx=SystemTraceContext.new("captains_log_reflection", session_id=session_id),
    ) as _cost_sink:
        entry, missing_skill_names = await asyncio.to_thread(..., cost_sink=_cost_sink)
else:
    entry, missing_skill_names = await asyncio.to_thread(...)   # local = free, no reservation
```

Local placement is **not** reserved — mirrors the `LocalLLMClient` / `LiteLLMClient` split; a free call must not consume a paid role's headroom.

The `api_costs` row is written via `record_vendor_cost` (`cost_tracker.py:459`), the identity-lenient, never-raises helper FRE-974 introduced — background reflection has exactly the "varied identity availability" it was built for. Widen its docstring from "OVH-embedding / Voyage-reranker" to "non-`LiteLLMClient` paid paths" (fold-in).

**Verify:** `tests/personal_agent/llm_client/test_dspy_gate.py` — (a) a cloud job reserves, commits the summed history cost, and writes an `api_costs` row with `purpose='captains_log'`; (b) an exception inside the block refunds and re-raises; (c) a cache-hit history (`cost=None`) commits `0.0`, not the estimate; (d) `disable_history` falls back to pricing, and a total failure to price commits the estimate rather than zero; (e) `tests/test_captains_log/test_reflection_dspy_gated.py` — a local-placement role takes **no** reservation, a cloud-placement role does, and `BudgetDenied` falls through to the manual path.

### Step 8 — Audit output document

`docs/research/2026-07-29-fre-989-cost-attribution-audit.md` — the audit's stated conclusion, which the ticket asks for explicitly:

- The verified-state table from §1 (findings F1–F8 as *confirmed / corrected / newly found*, with file:line).
- **Authoritative cost source: Postgres `api_costs`** — append-only, per-call, role-attributed via `purpose`, real timestamps. Stated with its **actual** completeness, not as a clean claim: it covers `LiteLLMClient` calls that carry a session id, `record_vendor_cost` vendor calls, and (after step 6) gateway streaming. It does **not** cover the cloud DSPy channel (F8), calls whose `session_id` is absent (`litellm_client.py:666-682`), or intermediate LiteLLM retry attempts (one row per final response, `litellm_client.py:427-437`).
- **`budget_counters` is a per-window ledger, not a current-state gauge** — a read that does not constrain `window_start` to the current window returns a *closed* window's total (Linear comment defect 1). A missing row for today means zero, never yesterday.
- **Elasticsearch is a mirror**, complete and role-attributed only from this change forward. The `@timestamp` field name is the silent-zero trap (Linear comment defect 2).
- **The remaining bypass boundary, stated:** F5 — embeddings/rerankers record cost but never reserve. Deliberate as of today; gating them is an ADR-0120 decision. (F8 is **closed** in step 7.)
- **The four residues** from §2.1's table, each with its disposition.

### Step 9 — Follow-up tickets (Needs Approval)

Only for genuinely separate, sequenceable or ADR-requiring work (per the fold-in rule):

1. **Counter-read contract for cost consumers** — the ES cost skill / agent read path must constrain to the current window or read `api_costs` directly. (Linear comment defects 1 & 2; touches the skill, not the gate.)
3. **Decide whether `record_vendor_cost` paths should reserve (F5)** — embedding/reranker gating. ADR-0120 decision.
4. **Retire the dead `promotion` / `freshness` budget lanes** — declared with zero call sites; config removal.
5. **Raw `litellm.acompletion` calls in eval scripts** (`scripts/eval/relabel_v2_types.py:278`, `relabel_v2_rels.py:265`) sit outside gate and tracker. Low severity (eval-only, run by hand) but it is unmetered spend.

---

## 5. Quality gates

`make test` (module then full) · `make mypy` · `make ruff-check` · `make ruff-format` · `pre-commit run --all-files` · `uv run python scripts/check_config.py` · the telemetry surface gate (Step 5).

Self-review: **code-review at `high`** (src + cost + schema-adjacent). **security-review** — the diff touches no inputs/subprocess/auth/secrets/network, so likely skipped; run it if Step 5's ES emit change is judged a data-exposure surface (`purpose` is a role name, not PII).

## 6. Risks

| Risk | Mitigation |
|------|-----------|
| Fail-closed `budget_role_for` turns a mis-mapped role from silent mis-billing into a **runtime failure** on a user-facing path | CI guard + startup validator cover every *declared* role; an arbitrary runtime string still raises, by design. Blast radius verified: `executor.step_llm_call` converts it to `TaskState.FAILED` (one failed turn, no process crash) and skill-routing errors are already swallowed (`executor.py:3997-4034`). This is the ticket's explicit mandate ("caught rather than absorbed by a fallback"). |
| `UnknownBudgetRoleError` has no FastAPI handler — only `BudgetDenied` gets the structured 503 (`service/app.py:1362-1404`) | Acceptable: it is a config-drift error, not a user-facing budget outcome, and the generic handler is the right surface. Noted in the audit doc. |
| Making `budget_role` required touches 13 test construction sites | Mechanical; `mypy` + the full suite catch every miss. |
| `debug`→`info` on `api_cost_recorded` increases ES volume | ~185 rows/day observed — negligible against a 480-index estate. |
| The `purpose` field may need an ES mapping entry | Step 5 runs the telemetry gate explicitly and adds the template field rather than baselining. |
