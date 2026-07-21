"""Constraint governance action-ID registry (ADR-0076).

Each governed constraint exposes a fixed set of options. Every option has a
stable ``action_id`` (snake_case) that is independent of the display label
shown in the PWA ``DecisionCard``. Stored preferences and wire messages carry
the ``action_id``, so renaming a button label never invalidates persisted
state.

Convention: the **last** option in each list is the safe default applied on
timeout, disconnect, or no active WebSocket connection.
"""

from __future__ import annotations

import contextvars
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable

    from personal_agent.config.settings import AppConfig
    from personal_agent.llm_client.models import ModelConfig


@dataclass(frozen=True)
class ConstraintOption:
    """A single user-selectable option for a constraint pause.

    Attributes:
        action_id: Stable identifier persisted in preferences and sent on the
            wire. Never changes once shipped.
        label: Human-readable display label rendered by the PWA.
    """

    action_id: str
    label: str


CONSTRAINT_OPTIONS: dict[str, list[ConstraintOption]] = {
    "tool_iteration_limit": [
        ConstraintOption(action_id="continue_10", label="Continue (10 more)"),
        ConstraintOption(action_id="finish_now", label="Finish now"),
    ],
    "context_compression": [
        ConstraintOption(action_id="compress_continue", label="Compress and continue"),
        ConstraintOption(action_id="stop_here", label="Stop here instead"),
    ],
    # ADR-0101 §8b / FRE-691: pre-flight cloud-attachment cost confirmation. The
    # safe default (last) is keep_local — no cloud spend without explicit confirm.
    "attachment_cost": [
        ConstraintOption(action_id="proceed_cloud", label="Proceed on cloud"),
        ConstraintOption(action_id="keep_local", label="Keep local / free"),
    ],
}


#: The constraint name whose options are computed from the ADR-0121 catalog at
#: pause time rather than looked up from :data:`CONSTRAINT_OPTIONS` (ADR-0122 §3).
ARTIFACT_BUILDER_CONSTRAINT = "artifact_builder"

#: Constraints whose option sets are computed, not static. Membership is what the
#: executor guard and the settings-validation surface branch on. Kept as a set (not
#: a bare equality on the one name) so a second computed decision type — the ADR-0122
#: pattern is designed to generalise — is a one-line addition, not a new branch.
COMPUTED_OPTION_CONSTRAINTS: frozenset[str] = frozenset({ARTIFACT_BUILDER_CONSTRAINT})


@dataclass(frozen=True)
class ComputedConstraintOption:
    """A catalog-derived option for a computed-options constraint (ADR-0122 §3).

    Unlike :class:`ConstraintOption` (a static button label), an option here is a
    catalog deployment carrying the display detail the ADR-0076 ``DecisionCard``
    needs, so the user chooses on visible tradeoffs rather than a bare key. The
    detail is a pure projection of the ADR-0121 catalog — never hand-typed — so it
    cannot drift from what the model actually is.

    Attributes:
        action_id: The deployment key. Stable, persisted in preferences and sent on
            the wire — the same contract as :attr:`ConstraintOption.action_id`.
        label: Human-readable label rendered by the PWA (the deployment key today;
            the PWA card ticket refines display).
        summary: One-line intended-use string from the catalog definition.
        input_cost_per_token: USD per input token, or ``None`` for unpriced/local.
        output_cost_per_token: USD per output token, or ``None`` for unpriced/local.
        context_length: Maximum context window, in tokens.
        max_output_tokens: Maximum output tokens, or ``None`` for the provider
            default (the large-output axis the card surfaces — FRE-478 precedent).
    """

    action_id: str
    label: str
    summary: str
    input_cost_per_token: float | None
    output_cost_per_token: float | None
    context_length: int
    max_output_tokens: int | None


def option_ids(constraint: str) -> list[str]:
    """Return the valid ``action_id`` values for a constraint.

    Args:
        constraint: Constraint name (key of :data:`CONSTRAINT_OPTIONS`).

    Returns:
        List of stable ``action_id`` strings, in display order.

    Raises:
        KeyError: If ``constraint`` is not a known constraint name.
    """
    return [opt.action_id for opt in CONSTRAINT_OPTIONS[constraint]]


def default_action_id(constraint: str) -> str:
    """Return the safe default ``action_id`` for a constraint.

    The default is the last option in the constraint's option list — applied
    on timeout, disconnect, or when no WebSocket connection is active.

    Args:
        constraint: Constraint name (key of :data:`CONSTRAINT_OPTIONS`).

    Returns:
        The default option's stable ``action_id``.

    Raises:
        KeyError: If ``constraint`` is not a known constraint name.
    """
    return CONSTRAINT_OPTIONS[constraint][-1].action_id


# ── Computed-options decision type (ADR-0122 §3 / FRE-881) ────────────────────
# The static machinery above assumes a closed option set indexed by constraint
# name. The artifact builder's options are instead COMPUTED from the ADR-0121
# catalog at pause time. The functions below are the widening that admits that:
# an availability predicate, the option computation, the configured default, and
# the two dispatchers the executor guard and the settings-validation surface call.


def build_provider_availability(
    config: "ModelConfig", settings: "AppConfig"
) -> "Callable[[str], bool]":
    """Return a synchronous provider-availability predicate (ADR-0121 §3).

    Provider health here is config-derived and **synchronous**: a cloud provider is
    available when its credential is configured (the same secret-presence signal as
    ``llm_client.provider_health.is_provider_available`` and the
    ``/api/inference/status`` cloud branch); the no-auth local SLM tunnel is treated
    as available without a probe.

    The authoritative per-provider health source is
    :func:`personal_agent.llm_client.provider_health.check_all_providers` (FRE-918),
    which is **async** because its local path does a live SLM-tunnel probe. This
    predicate deliberately does not call it: ``resolve_options_and_default`` runs on
    the synchronous executor-guard path, and threading async + a live probe into it
    belongs with the build-boundary pause wiring (FRE-882 / ADR-0122 §4), which is
    already async and owns the fail-closed dispatch. The cloud logic is identical, so
    the two never disagree on a cloud provider; only the local live-probe refinement
    is deferred to that seam.

    Args:
        config: The catalog whose ``providers`` mapping is consulted.
        settings: The ``AppConfig`` holding provider credentials, read by the
            provider's ``auth_env`` field name.

    Returns:
        A predicate ``provider_name -> bool``. Fails closed: an unknown provider
        name is unavailable.
    """

    def _available(provider_name: str) -> bool:
        provider = config.providers.get(provider_name)
        if provider is None:
            return False
        if provider.auth_env is None:
            return True
        return bool(getattr(settings, provider.auth_env, None))

    return _available


def compute_artifact_builder_options(
    config: "ModelConfig", *, is_provider_available: "Callable[[str], bool]"
) -> list[ComputedConstraintOption]:
    """Compute the artifact builder's options from the catalog (ADR-0122 §3, AC-6).

    The option set is exactly the catalog deployments of ``kind: llm`` whose
    provider is available — asserted in both directions by AC-6: a non-llm
    deployment never leaks in, a deployment whose provider is down is absent, an
    available one is present. When the ``artifact_builder`` role binding itself is
    pinned (``open: false``), no deployment is ever a legal selection
    (:func:`personal_agent.config.model_loader.is_selectable_binding`, which
    :func:`resolve_artifact_builder_key` fail-closed-checks a card pick against) —
    so this returns no options rather than offering choices every one of which
    would silently be overridden to the default at build time (ADR-0122 §4).

    Args:
        config: The ADR-0121 catalog to read deployments and detail from.
        is_provider_available: Predicate deciding whether a deployment's provider
            is currently usable — injected so callers (and tests) control the
            availability source. Build the live one with
            :func:`build_provider_availability`.

    Returns:
        The available llm deployments as :class:`ComputedConstraintOption` values,
        in catalog (insertion) order; empty when the role is pinned.
    """
    from personal_agent.llm_client.models import ModelKind

    binding = config.roles.get(ARTIFACT_BUILDER_CONSTRAINT)
    if binding is None or not binding.open:
        return []

    options: list[ComputedConstraintOption] = []
    for key, definition in config.models.items():
        if definition.kind is not ModelKind.LLM:
            continue
        provider = definition.provider
        if provider is None or not is_provider_available(provider):
            continue
        options.append(
            ComputedConstraintOption(
                action_id=key,
                label=key,
                summary=definition.summary,
                input_cost_per_token=definition.input_cost_per_token,
                output_cost_per_token=definition.output_cost_per_token,
                context_length=definition.context_length,
                max_output_tokens=definition.max_tokens,
            )
        )
    return options


def artifact_builder_default_key(config: "ModelConfig") -> str:
    """Return the configured-default deployment key for the artifact builder.

    The safe fallback the card carries for timeout / disconnect / no active socket
    — the role's own ADR-0121 Layer-3 binding default (ADR-0122 §1/§4). Read from
    the binding directly and guarded: a catalog with no ``artifact_builder`` binding
    has no safe default to offer, so this raises rather than returning a role-name
    string that names no deployment and would later dispatch to a non-existent model.

    Note the timeout *dispatch* is not driven by this key — ADR-0122 §4 keeps the
    no-decision path on ``get_llm_client(role_name="artifact_builder")``; this is the
    action_id the event/card carries. Wiring the two together is the FRE-882 seam, so
    this deliberately returns the Layer-3 binding rather than threading the (being-
    deleted, ADR-0121) ExecutionProfile redirect.

    Args:
        config: The catalog carrying the ``artifact_builder`` role binding.

    Returns:
        The configured default deployment key.

    Raises:
        ModelConfigError: If the catalog defines no ``artifact_builder`` binding.
    """
    binding = config.roles.get(ARTIFACT_BUILDER_CONSTRAINT)
    if binding is None:
        from personal_agent.config.model_loader import ModelConfigError

        raise ModelConfigError(
            f"catalog defines no {ARTIFACT_BUILDER_CONSTRAINT!r} Layer-3 binding; "
            "cannot resolve the artifact-builder default (ADR-0122 §4)"
        )
    return binding.deployment


def resolve_artifact_builder_key(
    selected_key: str,
    config: "ModelConfig",
    *,
    is_provider_available: "Callable[[str], bool]",
) -> str:
    """Fail-closed catalog check for a resolved artifact-builder key (ADR-0122 §4, AC-4).

    ``selected_key`` — the ``action_id`` from a card pick or a stored preference —
    must exist in the catalog, be ``kind: llm``, belong to the (open)
    ``artifact_builder`` role, and have an available provider. Any failure
    substitutes the configured default — never an arbitrary model, never no model.
    Existence/kind/open are :func:`personal_agent.config.model_loader.is_selectable_binding`'s
    own guardrail (ADR-0121 §6); this layers provider availability on top, the same
    signal :func:`compute_artifact_builder_options` filters the card's option set by.

    Args:
        selected_key: The resolved decision to validate.
        config: The catalog to validate against.
        is_provider_available: Provider-availability predicate — build the live one
            with :func:`build_provider_availability`.

    Returns:
        ``selected_key`` when every check passes; otherwise
        :func:`artifact_builder_default_key`'s configured default.
    """
    from personal_agent.config.model_loader import is_selectable_binding

    if is_selectable_binding(ARTIFACT_BUILDER_CONSTRAINT, selected_key, config):
        provider = config.models[selected_key].provider
        if provider is not None and is_provider_available(provider):
            return selected_key
    return artifact_builder_default_key(config)


class ConstraintDecision(str):
    """A resolved constraint ``action_id``, carrying how it was resolved (ADR-0122 §4).

    Compares and hashes as the plain ``action_id`` string, so
    ``_maybe_pause_for_constraint``'s existing callers (which pattern-match a bare
    string, e.g. ``if decision == "proceed_cloud":``) are unaffected. Only a caller
    that must route differently for a genuine decision versus a no-decision
    fallback — the artifact-builder build boundary, which switches between
    ``get_llm_client_for_key`` and the role-name path — needs ``resolution``.

    Attributes:
        resolution: One of ``"preference_applied"`` (a standing preference
            pre-resolved silently), ``"user_choice"`` (an interactive card pick),
            ``"timeout_default"`` (no answer within the timeout),
            ``"connection_lost"`` (no active WebSocket connection), or
            ``"user_cancel"`` (the Stop button cancelled the pending pause).
    """

    resolution: str

    def __new__(cls, action_id: str, resolution: str) -> "ConstraintDecision":
        """Construct from the resolved ``action_id`` and how it was resolved.

        Args:
            action_id: The resolved action/deployment key — the instance's own
                string value.
            resolution: How resolution happened; see the class docstring.

        Returns:
            The new ``ConstraintDecision`` instance.
        """
        instance = super().__new__(cls, action_id)
        instance.resolution = resolution
        return instance


# ── Turn-scoped artifact-builder resolution carrier (ADR-0122 §2/§4 / FRE-930) ────
# The builder decision is raised at TURN START (``step_init``) but consumed at the
# build boundary inside ``artifact_draft`` — which, like every tool executor,
# receives only a ``TraceContext``, never the ``ExecutionContext``
# (``tools/executor.py`` passes ``ctx=trace_ctx``). So the resolution crosses that
# boundary through an async-safe ``ContextVar``, the same mechanism
# ``config.selection`` uses for per-turn model selection and
# ``observability.topology.seam`` uses for the topology label. ``asyncio.gather``
# child tasks inherit the creating task's context, so a value set in ``step_init``
# survives to an ``artifact_draft`` dispatched several tool iterations later (AC-1).
#
# The same resolution is also the authoritative turn-scoped state on
# ``ExecutionContext`` (``artifact_builder_resolution``, which AC-10a asserts
# directly); this ContextVar mirrors it purely to reach the tool boundary. ``None``
# means the turn-start ask did not run (no ``artifact_build_intent`` signal) — a
# missed prediction the build boundary degrades to the configured default and logs
# (AC-11). ``step_init`` sets it and ``execute_task`` token-resets it in a ``finally``
# so no pick outlives its turn (AC-10c).

_artifact_builder_resolution: contextvars.ContextVar[ConstraintDecision | None] = (
    contextvars.ContextVar("artifact_builder_resolution", default=None)
)


def set_artifact_builder_resolution(
    resolution: ConstraintDecision | None,
) -> contextvars.Token[ConstraintDecision | None]:
    """Publish this turn's artifact-builder resolution for the current async context.

    Args:
        resolution: The resolved :class:`ConstraintDecision` to carry to the build
            boundary, or ``None``.

    Returns:
        A token for :func:`reset_artifact_builder_resolution` (used by
        ``execute_task``'s lifecycle ``finally`` and by test isolation).
    """
    return _artifact_builder_resolution.set(resolution)


def get_artifact_builder_resolution() -> ConstraintDecision | None:
    """Return this turn's artifact-builder resolution, or ``None`` if the ask never ran.

    Returns:
        The :class:`ConstraintDecision` set by ``step_init`` for the current async
        context, or ``None`` when no turn-start ask touched the carrier (no signal, or
        a background/direct tool call) — which the build boundary treats as a missed
        prediction (AC-11).
    """
    return _artifact_builder_resolution.get()


def reset_artifact_builder_resolution(
    token: contextvars.Token[ConstraintDecision | None],
) -> None:
    """Restore the artifact-builder resolution to a prior value.

    Args:
        token: The token returned by :func:`set_artifact_builder_resolution`.
    """
    _artifact_builder_resolution.reset(token)


def resolve_options_and_default(constraint: str) -> tuple[list[str], str]:
    """Return ``(action_ids, default_action_id)`` for a constraint — static or computed.

    The single entry the executor's pause helper calls, replacing the direct
    ``option_ids`` / ``default_action_id`` lookups so a computed constraint no
    longer ``KeyError``s the static registry (ADR-0122 §3 executor-guard seam).

    Args:
        constraint: The constraint name.

    Returns:
        The valid action-id list and the safe-default action id. For the computed
        path the ids are the availability-filtered catalog llm deployment keys and
        the default is the configured builder default; note the default is kept
        independent of the option set (it is the fail-closed fallback, and appending
        an unavailable default would break AC-6's "provider down → absent").
    """
    if constraint in COMPUTED_OPTION_CONSTRAINTS:
        from personal_agent.config import settings as live_settings
        from personal_agent.config.model_loader import load_model_config

        config = load_model_config()
        is_available = build_provider_availability(config, live_settings)
        options = compute_artifact_builder_options(config, is_provider_available=is_available)
        return [opt.action_id for opt in options], artifact_builder_default_key(config)
    return option_ids(constraint), default_action_id(constraint)


def is_known_constraint(constraint: str) -> bool:
    """Whether ``constraint`` is a governed constraint (static or computed)."""
    return constraint in CONSTRAINT_OPTIONS or constraint in COMPUTED_OPTION_CONSTRAINTS


def _valid_preference_actions_for_config(constraint: str, config: "ModelConfig") -> set[str]:
    """Valid ``preferred_action`` values for a computed constraint, given a catalog.

    Split from :func:`valid_preference_actions` so it can be unit-tested against a
    hand-built catalog. A saved preference names a model to *always* use, so it is
    validated on catalog membership (``kind: llm``) — deliberately **not**
    availability-filtered, so a stored preference survives a transient provider
    outage (the pause-time options provider is the availability-filtered surface).

    Args:
        constraint: The computed constraint name (currently ``artifact_builder``).
        config: The catalog whose llm keys are the valid actions.

    Returns:
        ``{"always_pause"} ∪ catalog llm keys``.
    """
    return {"always_pause", *_catalog_llm_keys(config)}


def valid_preference_actions(constraint: str) -> set[str]:
    """Valid ``preferred_action`` values for a constraint (settings validation).

    The computed path consults the ADR-0121 catalog (ADR-0122 §3 settings seam);
    static constraints keep registry-based validation.

    Args:
        constraint: The constraint name.

    Returns:
        The set of accepted ``preferred_action`` values, always including the
        reserved ``always_pause``.
    """
    if constraint in COMPUTED_OPTION_CONSTRAINTS:
        from personal_agent.config.model_loader import load_model_config

        return _valid_preference_actions_for_config(constraint, load_model_config())
    return {"always_pause", *option_ids(constraint)}


def _catalog_llm_keys(config: "ModelConfig") -> list[str]:
    """Return the catalog deployment keys of ``kind: llm``, in catalog order."""
    from personal_agent.llm_client.models import ModelKind

    return [key for key, definition in config.models.items() if definition.kind is ModelKind.LLM]
