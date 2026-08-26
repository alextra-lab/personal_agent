"""Factory role_name → budget-role resolution (ADR-0065; made total by FRE-989).

Resolution is **total and fail-closed**: a name this module does not know raises
:class:`UnknownBudgetRoleError` rather than silently defaulting to
``main_inference``. A silent default is indistinguishable from a correct mapping
at every downstream layer — the counters, the ledger and the telemetry all
record the *wrong* lane with full confidence, so the mis-billing is invisible
precisely where you would look for it.

``study`` is the worked example, and its precise shape matters: the role is
capped in ``budget.yaml`` with its own $5 isolation lane (FRE-839, so a one-time
corpus run can never contend with live extraction) yet had **no entry here**, so
the role-name door resolved it to ``main_inference``. No spend actually moved —
every live ``study`` call site names its lane explicitly — so this was a
reachable-but-unused door, not an incident. It is named because it shows the
failure is silent by construction: nothing distinguishes "resolved correctly"
from "fell through" at any layer. FRE-1037 widened ``ModelRole`` from four
members to fourteen, tripling the fallback's blast radius, which is why totality
is enforced here rather than left to review.

Three mechanisms keep the map, ``ModelRole`` and ``config/governance/budget.yaml``
in agreement, so this module's raise is unreachable for any *declared* role:

- :func:`validate_role_totality` — called from the FastAPI lifespan hook, so a
  drifted deploy refuses to start (``budget.yaml`` is a runtime file baked into
  the image; CI validates the tree, not the container).
- ``config_guard.check_budget_role_coverage`` — the same invariants at CI time.
- ``tests/personal_agent/cost_gate/test_role_map_totality.py``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import structlog

if TYPE_CHECKING:
    from collections.abc import Mapping
    from collections.abc import Set as AbstractSet

    from personal_agent.cost_gate.types import BudgetConfig


log = structlog.get_logger(__name__)


class UnknownBudgetRoleError(ValueError):
    """Raised when a factory role name has no declared budget lane."""


# Roles that legitimately never acquire a gated LLM client. Their vendor spend is
# recorded through ``cost_tracker.record_vendor_cost`` (FRE-974) and does **not**
# pass ``CostGate.reserve`` — a deliberate boundary, not an oversight (FRE-989
# finding five). Closing it needs a token estimator for the vendor API shape,
# which is a separate decision; see the audit doc under docs/research/.
NON_GATED_ROLES: frozenset[str] = frozenset({"embedding", "reranker", "reranker_fallback"})


# Maps the factory's ``role_name`` argument (used by callers of
# ``get_llm_client``) to the budget role keys declared in budget.yaml. New call
# sites should prefer a budget-role name directly; the aliases below exist to
# give pre-existing call sites their lane without a sweeping rename.
BUDGET_ROLE_BY_FACTORY_NAME: dict[str, str] = {
    # Executor / orchestrator roles → main_inference (user-facing flow)
    "primary": "main_inference",
    "sub_agent": "main_inference",
    "compressor": "main_inference",
    "router": "main_inference",
    "reasoning": "main_inference",
    "standard": "main_inference",
    "main_inference": "main_inference",
    # Background consumers
    "captains_log_role": "captains_log",
    "captains_log": "captains_log",
    "insights_role": "insights",
    "insights": "insights",
    "entity_extraction_role": "entity_extraction",
    "entity_extraction": "entity_extraction",
    "promotion_role": "promotion",
    "promotion": "promotion",
    "freshness_role": "freshness",
    "freshness": "freshness",
    # Artifact builder — own lane, not main_inference (ADR-0118 T1, FRE-879).
    "artifact_builder": "artifact_builder",
    # FRE-1037: explicit entries for the newly-threaded ModelRole members, so
    # none of them silently fall through to a default.
    # session_summary shares captains_log's lane — ADR-0124 D2's existing,
    # explicit deferral of a dedicated budget class, now declared rather than
    # coincidental.
    "session_summary": "captains_log",
    # vision escalations are part of a user-facing turn and no dedicated
    # budget.yaml lane exists for them; main_inference is correct, now explicit.
    "vision": "main_inference",
    "skill_routing": "skill_routing",
    # FRE-1281: the ADR-0138 span classifier shares entity_extraction's lane rather than
    # opening its own. Both are background structured-extraction passes over a turn's
    # text, and a new lane would need an entry in the REAL budget.yaml, which is
    # gitignored (FRE-1209) — so adding one here would pass CI against the .example and
    # then fail validate_role_totality at startup on the deployed box. Splitting the
    # attribution later is a config edit, not a code change.
    "span_extraction": "entity_extraction",
    # FRE-1286: the ADR-0138 D3(d) entailment judge, on the same terms and for the same
    # reason as span_extraction above — the REAL budget.yaml is gitignored (FRE-1209), so
    # a dedicated lane declared here would pass CI against the .example and then fail
    # validate_role_totality at startup on the deployed box. Both arms share the entry:
    # the inline arm is on the turn path and the sampled arm is background, but a role
    # name resolves to one lane, and splitting them is a config edit, not a code change.
    "entailment": "entity_extraction",
    # FRE-989: the residue FRE-1037 left behind. study is capped in budget.yaml
    # ($5 daily / $7 weekly, FRE-839) but was absent here, so the role-name door
    # resolved it to main_inference and its isolation did not apply.
    "study": "study",
}


def budget_role_for(factory_role_name: str) -> str:
    """Resolve a factory ``role_name`` to its budget role key.

    Total and fail-closed (FRE-989): an unrecognised name raises rather than
    defaulting.

    The CI guard and the startup validator together make this raise unreachable
    for any role declared in ``ModelRole`` or ``budget.yaml``. They do **not**
    make it unreachable outright — ``get_llm_client`` takes ``role_name: str``,
    so an arbitrary runtime string still reaches here and still raises, by
    design. On the orchestrator path that surfaces as a failed turn
    (``executor.step_llm_call`` converts it to ``TaskState.FAILED``), which is
    the intended trade: one loud failed turn beats silently mis-billing every
    call that follows.

    Args:
        factory_role_name: The ``role_name`` argument to ``get_llm_client``.

    Returns:
        Budget role key, as declared in ``budget.yaml``.

    Raises:
        UnknownBudgetRoleError: If the name has no declared budget lane.
    """
    try:
        return BUDGET_ROLE_BY_FACTORY_NAME[factory_role_name]
    except KeyError:
        # The remediation detail goes to the log, not the exception message: on
        # the orchestrator path a raised error can be rendered straight into the
        # assistant's stream, and internal module paths do not belong there.
        log.error(
            "budget_role_unmapped",
            factory_role_name=factory_role_name,
            remediation=(
                "add an entry to BUDGET_ROLE_BY_FACTORY_NAME in "
                "cost_gate/role_map.py, or — if the role never acquires a gated "
                "LLM client — to NON_GATED_ROLES in the same module"
            ),
        )
        raise UnknownBudgetRoleError(
            f"No budget lane declared for factory role {factory_role_name!r}."
        ) from None


def role_totality_findings(
    config: BudgetConfig,
    *,
    role_map: Mapping[str, str] | None = None,
    non_gated: AbstractSet[str] | None = None,
) -> list[str]:
    """Return one message per role-declaration inconsistency; empty when sound.

    Shared by the startup validator and ``config_guard`` so the two can never
    disagree about what "consistent" means — a second copy of these invariants
    would be the very drift this module exists to prevent.

    Args:
        config: The loaded budget configuration to check the map against.
        role_map: Factory-name → budget-lane mapping. Defaults to the live
            :data:`BUDGET_ROLE_BY_FACTORY_NAME`; injectable so a caller can
            check a candidate pairing (and so tests can perturb one side of the
            invariant without mutating module state).
        non_gated: Roles that never acquire a gated client. Defaults to
            :data:`NON_GATED_ROLES`.

    Returns:
        Human-readable findings, each naming the offending role and the remedy.
    """
    from personal_agent.llm_client.types import ModelRole

    effective_map = BUDGET_ROLE_BY_FACTORY_NAME if role_map is None else role_map
    effective_non_gated = NON_GATED_ROLES if non_gated is None else non_gated

    findings: list[str] = []
    declared = set(config.roles)
    capped = {cap.role for cap in config.caps}
    uncapped = set(config.uncapped_roles)

    for role in ModelRole:
        if role.value in effective_map or role.value in effective_non_gated:
            continue
        findings.append(
            f"ModelRole.{role.name} ({role.value!r}) has no budget lane and is not "
            f"declared in NON_GATED_ROLES — it would raise at client acquisition."
        )

    for name, lane in sorted(effective_map.items()):
        if lane not in declared:
            findings.append(
                f"role map entry {name!r} points at budget role {lane!r}, which "
                f"budget.yaml does not declare."
            )

    for name in sorted(declared):
        if effective_map.get(name) != name:
            findings.append(
                f"budget.yaml declares role {name!r} but the role map does not "
                f"resolve that name to itself — the role-name door would bill it "
                f"elsewhere or raise."
            )
        if name not in capped and name not in uncapped:
            findings.append(
                f"budget.yaml role {name!r} has no cap entry and is not listed in "
                f"uncapped_roles — a forgotten cap is indistinguishable from a "
                f"deliberate one. Add a cap, or declare the decision."
            )
        if name in capped and name in uncapped:
            findings.append(
                f"budget.yaml role {name!r} is both capped and listed in "
                f"uncapped_roles — that is drift, not a decision."
            )

    for name in sorted(uncapped - declared):
        findings.append(
            f"uncapped_roles names {name!r}, which budget.yaml no longer declares "
            f"as a role — remove the stale entry."
        )

    for name in sorted(set(effective_non_gated) & set(effective_map)):
        findings.append(
            f"role {name!r} is declared both non-gated and mapped to a budget lane "
            f"— it must be exactly one."
        )

    return findings


def validate_role_totality(
    config: BudgetConfig,
    *,
    role_map: Mapping[str, str] | None = None,
    non_gated: AbstractSet[str] | None = None,
) -> None:
    """Raise unless the role map, ``ModelRole`` and ``budget.yaml`` all agree.

    Called from the FastAPI lifespan hook alongside ``set_default_gate``, so a
    deploy whose baked ``budget.yaml`` has drifted from the code refuses to
    start rather than mis-billing for the life of the container.

    Args:
        config: The loaded budget configuration.
        role_map: Optional factory-name → budget-lane mapping override; see
            :func:`role_totality_findings`.
        non_gated: Optional non-gated role set override.

    Raises:
        BudgetConfigError: If any role declaration is inconsistent. The message
            lists every finding, so one restart surfaces all of them.
    """
    from personal_agent.cost_gate.policy import BudgetConfigError

    findings = role_totality_findings(config, role_map=role_map, non_gated=non_gated)
    if findings:
        raise BudgetConfigError(
            "budget role declarations are inconsistent:\n  - " + "\n  - ".join(findings)
        )
