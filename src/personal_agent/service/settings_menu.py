"""Read-only settings menu payload (FRE-1702, stage 1 of FRE-1698).

Builds the one response that the PWA settings menu renders: nine groups and a System
group. Each row carries the parameter name, its live value, the source that wins, and
(for an override) the value it overrides. The menu edits nothing. The only write is the
``DELETE /api/v1/preferences/constraint`` that clears a saved choice.

Values come from the running gateway: the loaded ``AppConfig``, the loaded catalogs, the
live ``CostGate`` config, and the database rows. They do not come from repo files.

Safety rules (AC-4). A secret, infrastructure or identity value is never returned: the
row holds ``"set"`` or ``"not set"``. Three layers enforce this, and each works alone:

1. the group rule (``HIDDEN_VALUE_GROUPS`` of ``settings_menu_groups``);
2. a name denylist (``_HIDDEN_SUFFIXES``, ``_HIDDEN_NAMES``) for settings that carry an
   endpoint or a domain but sit in another group;
3. :func:`scrub`, which rewrites any URL or path in the finished response.

A group builder that fails returns the fixed code ``"unavailable"``. The exception text
goes to the server log only.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal
from uuid import UUID

import structlog
from pydantic import BaseModel, ConfigDict, Field, JsonValue
from pydantic_core import to_jsonable_python

from personal_agent.config.settings import AppConfig
from personal_agent.orchestrator.constraint_options import (
    ARTIFACT_BUILDER_CONSTRAINT,
    CONSTRAINT_OPTIONS,
    artifact_builder_default_key,
    default_action_id,
    resolve_artifact_builder_key,
)
from personal_agent.service.settings_menu_groups import HIDDEN_VALUE_GROUPS, SETTING_GROUPS

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from personal_agent.config.seshat_effort import SeshatEffortConfig
    from personal_agent.cost_gate.types import BudgetConfig
    from personal_agent.governance.models import GovernanceConfig
    from personal_agent.llm_client.models import ModelConfig

log = structlog.get_logger(__name__)

Source = Literal["code default", ".env", "config file", "database row", "session"]

#: Fields whose default factory reads the process environment. ``model_fields_set`` does
#: not list them, so the ``.env`` check treats them as env-driven.
ENV_DRIVEN_FIELDS: frozenset[str] = frozenset({"environment"})

#: Settings that carry an endpoint or a domain but sit outside the hidden groups.
_HIDDEN_NAMES: frozenset[str] = frozenset({"slm_health_url", "url_guard_allowlist"})
_HIDDEN_SUFFIXES: tuple[str, ...] = (
    "_url",
    "_uri",
    "_endpoint",
    "_host",
    "_path",
    "_dir",
    "_origin",
    "_origins",
    "_dsn",
    "_token",
    "_secret",
    "_password",
    "_email",
    "_key",
    "_key_id",
    "_allowlist",
)

#: A URL, an absolute path of two or more segments, a home or variable path, or a glob.
_PATH_LEAF = re.compile(r"://|\*\*|^~|^\$|^(?:/[^/\s]+){2,}")

#: Governance fields that hold paths or commands. The menu shows a count in their place.
_COUNTED_TOOL_FIELDS: tuple[str, ...] = (
    "allowed_paths",
    "forbidden_paths",
    "unattended_paths",
    "allowed_commands",
    "forbidden_commands",
)

_TURN_RUNS_APPENDIX_GROUPS = frozenset(
    {"routing and planner", "context and history", "timeouts and limits"}
)
_APPENDIX_TO_MENU_GROUP: Mapping[str, str] = {
    "saved choices": "saved_choices",
    "modes and effort": "effort_and_modes",
    "budgets and cost": "budgets",
    "memory and recall": "memory",
    "tools and governance": "tools",
    "grounding and quality": "grounding",
    **{name: "turn_runs" for name in _TURN_RUNS_APPENDIX_GROUPS},
}

#: Constraints for which the turn never reads a saved choice (``allow_preference=False``).
_NEVER_APPLIED: frozenset[str] = frozenset({"attachment_cost", "spend_threshold"})


class Overridden(BaseModel):
    """The value that a row overrides, and where that value lives."""

    model_config = ConfigDict(frozen=True)

    value: JsonValue
    source: Source


class SettingRow(BaseModel):
    """One parameter in the menu.

    Attributes:
        name: Stable row name, for example ``setting.llm_timeout_seconds``.
        value: The live value. ``"set"`` or ``"not set"`` for a hidden value.
        source: The source that wins for the next turn.
        overrides: The value that this row overrides, when it overrides one.
        clearable: ``True`` only for a saved choice that exists. The Clear button reads it.
        note: A short plain-language remark about the row.
    """

    model_config = ConfigDict(frozen=True)

    name: str
    value: JsonValue
    source: Source
    overrides: Overridden | None = None
    clearable: bool = False
    note: str | None = None


class SettingsGroup(BaseModel):
    """A menu group. ``error`` is the fixed code ``"unavailable"`` when the group failed."""

    model_config = ConfigDict(frozen=True)

    id: str
    title: str
    rows: list[SettingRow] = Field(default_factory=list)
    error: Literal["unavailable"] | None = None


class SettingsMenu(BaseModel):
    """The whole menu: nine groups, then the read-only System group."""

    model_config = ConfigDict(frozen=True)

    groups: list[SettingsGroup]
    system: SettingsGroup


@dataclass(frozen=True)
class SessionInputs:
    """The open conversation's stored choices.

    Attributes:
        selections: Role to deployment key, from ``session_model_selections``.
        effort_level: The stored ``sessions.seshat_effort`` value, or ``None``.
    """

    selections: Mapping[str, str]
    effort_level: str | None


@dataclass(frozen=True)
class MenuInputs:
    """Everything that :func:`build_menu` reads. ``None`` marks a source that failed to load.

    Attributes:
        app_settings: The loaded ``AppConfig`` of the running gateway.
        models: The loaded model catalog.
        effort: The loaded Seshat effort presets.
        governance: The loaded governance files.
        budget: The running ``CostGate`` config, or ``None`` when no gate is started.
        saved_choices: The request user's ``constraint_name`` to ``preferred_action`` map,
            or ``None`` when the read failed.
        session: The open conversation's stored choices, or ``None`` with no conversation.
        location_consent: The user's stored consent, or ``None`` when it could not be read.
        grounding: ``(model_key, level)`` rows of ``grounding_enforcement_state``.
        provider_available: The turn's provider-availability predicate.
    """

    app_settings: AppConfig
    models: ModelConfig | None
    effort: SeshatEffortConfig | None
    governance: GovernanceConfig | None
    budget: BudgetConfig | None
    saved_choices: Mapping[str, str] | None
    session: SessionInputs | None
    location_consent: bool | None
    grounding: Sequence[tuple[str, str]] | None
    provider_available: Callable[[str], bool]


class SessionNotFoundError(LookupError):
    """The requested conversation does not exist or belongs to another user."""


class _Unavailable(Exception):
    """A source that a group needs did not load."""


# ---------------------------------------------------------------------------
# Redaction
# ---------------------------------------------------------------------------


def scrub(value: JsonValue) -> JsonValue:
    """Rewrite every URL or path in ``value`` to ``"set"``.

    The last layer of AC-4. It walks nested lists and dicts and changes string leaves only.
    A single-segment value such as ``/no_think`` is not a path and stays.

    Args:
        value: A JSON-compatible value.

    Returns:
        The same structure with each URL or path leaf replaced.
    """
    if isinstance(value, str):
        return "set" if _PATH_LEAF.search(value) else value
    if isinstance(value, list):
        return [scrub(item) for item in value]
    if isinstance(value, dict):
        return {key: scrub(item) for key, item in value.items()}
    return value


def _scrub_row(row: SettingRow) -> SettingRow:
    overrides = (
        Overridden(value=scrub(row.overrides.value), source=row.overrides.source)
        if row.overrides is not None
        else None
    )
    note = scrub(row.note) if row.note is not None else None
    return row.model_copy(update={"value": scrub(row.value), "overrides": overrides, "note": note})


def _json(value: object) -> JsonValue:
    return to_jsonable_python(value, fallback=str)  # type: ignore[no-any-return]


def _set_or_not(value: object) -> str:
    return "not set" if value in (None, "", [], (), {}) else "set"


def menu_group_of(field: str) -> str:
    """Return the id of the menu group that holds a setting's row.

    Args:
        field: An ``AppConfig`` field name.

    Returns:
        A group id from the nine groups, or ``"system"``.
    """
    return _APPENDIX_TO_MENU_GROUP.get(SETTING_GROUPS[field], "system")


def _is_hidden(field: str) -> bool:
    return (
        SETTING_GROUPS[field] in HIDDEN_VALUE_GROUPS
        or field in _HIDDEN_NAMES
        or field.endswith(_HIDDEN_SUFFIXES)
    )


# ---------------------------------------------------------------------------
# AppConfig setting rows
# ---------------------------------------------------------------------------


def _code_default(field: str) -> tuple[bool, object]:
    try:
        return True, AppConfig.model_fields[field].get_default(call_default_factory=True)
    except Exception:  # a default factory that needs validated data has no standalone default
        return False, None


def _setting_row(app_settings: AppConfig, field: str) -> SettingRow:
    live = getattr(app_settings, field)
    from_env = field in app_settings.model_fields_set or field in ENV_DRIVEN_FIELDS
    source: Source = ".env" if from_env else "code default"
    if _is_hidden(field):
        return SettingRow(name=f"setting.{field}", value=_set_or_not(live), source=source)
    overrides = None
    if from_env and field not in ENV_DRIVEN_FIELDS:
        has_default, default = _code_default(field)
        if has_default and _json(default) != _json(live):
            overrides = Overridden(value=_json(default), source="code default")
    return SettingRow(
        name=f"setting.{field}", value=_json(live), source=source, overrides=overrides
    )


def _setting_rows(inp: MenuInputs, menu_group: str) -> list[SettingRow]:
    return [
        _setting_row(inp.app_settings, field)
        for field in sorted(AppConfig.model_fields)
        if menu_group_of(field) == menu_group
    ]


# ---------------------------------------------------------------------------
# Group builders
# ---------------------------------------------------------------------------


def _need[T](value: T | None) -> T:
    if value is None:
        raise _Unavailable
    return value


def _effective_primary(inp: MenuInputs) -> tuple[str, str | None]:
    """Return ``(deployment the next turn uses, the default it overrides or None)``."""
    from personal_agent.config.model_loader import resolve_selected_deployment

    models = _need(inp.models)
    default = resolve_selected_deployment("primary", None, models)
    stored = inp.session.selections.get("primary") if inp.session is not None else None
    binding = models.roles.get("primary")
    if stored is not None and binding is not None and binding.open:
        effective = resolve_selected_deployment("primary", stored, models)
        if effective == stored:
            return effective, default
    return default, None


def _group_conversation(inp: MenuInputs) -> list[SettingRow]:
    effort = _need(inp.effort)
    primary, primary_default = _effective_primary(inp)
    primary_row = SettingRow(
        name="session.primary_model",
        value=primary,
        source="session" if primary_default is not None else "config file",
        overrides=(
            Overridden(value=primary_default, source="config file")
            if primary_default is not None
            else None
        ),
    )
    stored_level = inp.session.effort_level if inp.session is not None else None
    if stored_level in effort.levels:
        effort_row = SettingRow(
            name="session.effort_level",
            value=str(stored_level),
            source="session",
            overrides=Overridden(value=effort.default_level, source="config file"),
        )
    else:
        effort_row = SettingRow(
            name="session.effort_level", value=effort.default_level, source="config file"
        )
    return [primary_row, effort_row]


def _saved_choice_default(constraint: str, inp: MenuInputs) -> Overridden:
    if constraint == ARTIFACT_BUILDER_CONSTRAINT:
        return Overridden(
            value=artifact_builder_default_key(_need(inp.models)), source="config file"
        )
    return Overridden(
        value=f"decision card (safe default: {default_action_id(constraint)})",
        source="code default",
    )


def _group_saved_choices(inp: MenuInputs) -> list[SettingRow]:
    saved_choices = _need(inp.saved_choices)
    rows: list[SettingRow] = []
    for constraint in [*CONSTRAINT_OPTIONS, ARTIFACT_BUILDER_CONSTRAINT]:
        saved = saved_choices.get(constraint)
        note = (
            "The turn never applies a saved choice here. The card always asks."
            if constraint in _NEVER_APPLIED
            else None
        )
        if saved is None:
            rows.append(
                SettingRow(
                    name=f"saved_choice.{constraint}",
                    value="not set",
                    source="code default",
                    note=note,
                )
            )
            continue
        rows.append(
            SettingRow(
                name=f"saved_choice.{constraint}",
                value=saved,
                source="database row",
                overrides=_saved_choice_default(constraint, inp),
                clearable=True,
                note=note,
            )
        )
    if inp.app_settings.location_enabled:
        consent: JsonValue = "unavailable" if inp.location_consent is None else inp.location_consent
        rows.append(SettingRow(name="location.consent", value=consent, source="database row"))
    else:
        rows.append(
            SettingRow(
                name="location.consent",
                value="off (the operator flag location_enabled is off)",
                source="code default",
            )
        )
    return [*rows, *_setting_rows(inp, "saved_choices")]


def _group_models_per_role(inp: MenuInputs) -> list[SettingRow]:
    from personal_agent.config.model_loader import resolve_selected_deployment
    from personal_agent.llm_client.models import INHERIT_DEPLOYMENT

    models = _need(inp.models)
    primary, primary_default = _effective_primary(inp)
    rows: list[SettingRow] = []
    for role, binding in models.roles.items():
        default = resolve_selected_deployment(role, None, models)
        note = "open" if binding.open else "pinned"
        if role == "primary":
            rows.append(
                SettingRow(
                    name=f"role.{role}",
                    value=primary,
                    source="session" if primary_default is not None else "config file",
                    overrides=(
                        Overridden(value=primary_default, source="config file")
                        if primary_default is not None
                        else None
                    ),
                    note=note,
                )
            )
        elif binding.deployment == INHERIT_DEPLOYMENT:
            rows.append(
                SettingRow(
                    name=f"role.{role}",
                    value=primary,
                    source="session" if primary_default is not None else "config file",
                    note=f"{note}; inherits the primary model",
                )
            )
        elif role == ARTIFACT_BUILDER_CONSTRAINT:
            rows.append(_artifact_builder_role_row(inp, models, default, note))
        else:
            rows.append(
                SettingRow(name=f"role.{role}", value=default, source="config file", note=note)
            )
    return rows


def _artifact_builder_role_row(
    inp: MenuInputs, models: ModelConfig, default: str, note: str
) -> SettingRow:
    """Show the deployment that the next build uses, by the turn's own resolver."""
    if inp.saved_choices is None:
        return SettingRow(
            name=f"role.{ARTIFACT_BUILDER_CONSTRAINT}",
            value=default,
            source="config file",
            note=f"{note}; saved choices could not be read, so a saved choice may override this",
        )
    saved = inp.saved_choices.get(ARTIFACT_BUILDER_CONSTRAINT)
    if saved is None or saved == "always_pause":
        return SettingRow(
            name=f"role.{ARTIFACT_BUILDER_CONSTRAINT}",
            value=default,
            source="config file",
            note=note,
        )
    effective = resolve_artifact_builder_key(
        saved, models, is_provider_available=inp.provider_available
    )
    if effective == saved:
        return SettingRow(
            name=f"role.{ARTIFACT_BUILDER_CONSTRAINT}",
            value=effective,
            source="database row",
            overrides=Overridden(value=default, source="config file"),
            note=note,
        )
    return SettingRow(
        name=f"role.{ARTIFACT_BUILDER_CONSTRAINT}",
        value=default,
        source="config file",
        note=f"{note}; the saved choice {saved} is not usable now, so the default applies",
    )


def _group_effort_and_modes(inp: MenuInputs) -> list[SettingRow]:
    effort = _need(inp.effort)
    models = _need(inp.models)
    rows = [
        SettingRow(name="effort.default_level", value=effort.default_level, source="config file")
    ]
    for level, spec in effort.levels.items():
        rows.append(
            SettingRow(
                name=f"effort.level.{level}",
                value=_json(spec.model_dump(mode="json", exclude={"level"})),
                source="config file",
            )
        )
    for key, definition in models.models.items():
        dump = definition.model_dump(mode="json", exclude_none=True)
        if "endpoint" in dump:
            dump["endpoint"] = _set_or_not(definition.endpoint)
        rows.append(SettingRow(name=f"deployment.{key}", value=_json(dump), source="config file"))
    return [*rows, *_setting_rows(inp, "effort_and_modes")]


def _group_budgets(inp: MenuInputs) -> list[SettingRow]:
    budget = _need(inp.budget)
    rows = [
        SettingRow(
            name=f"cap.{cap.time_window}.{cap.role}", value=float(cap.cap_usd), source="config file"
        )
        for cap in budget.caps
    ]
    rows.extend(
        SettingRow(
            name=f"role_estimate.{role}",
            value=_json(config.model_dump(mode="json")),
            source="config file",
        )
        for role, config in budget.roles.items()
    )
    rows.append(
        SettingRow(name="uncapped_roles", value=list(budget.uncapped_roles), source="config file")
    )
    return [*rows, *_setting_rows(inp, "budgets")]


def _group_tools(inp: MenuInputs) -> list[SettingRow]:
    governance = _need(inp.governance)
    rows: list[SettingRow] = []
    for name, policy in governance.tools.items():
        dump = policy.model_dump(mode="json", exclude=set(_COUNTED_TOOL_FIELDS))
        for counted in _COUNTED_TOOL_FIELDS:
            dump[counted] = len(getattr(policy, counted))
        rows.append(SettingRow(name=f"tool.{name}", value=_json(dump), source="config file"))
    rows.append(
        SettingRow(
            name="sub_agent_tools",
            value=_json(
                {k: v.model_dump(mode="json") for k, v in governance.sub_agent_tools.items()}
            ),
            source="config file",
        )
    )
    rows.append(
        SettingRow(
            name="mode_transition_rules",
            value=_json(
                {k: v.model_dump(mode="json") for k, v in governance.transition_rules.items()}
            ),
            source="config file",
        )
    )
    return [*rows, *_setting_rows(inp, "tools")]


def _group_grounding(inp: MenuInputs) -> list[SettingRow]:
    grounding = _need(inp.grounding)
    rows = [
        SettingRow(name=f"grounding_enforcement.{model_key}", value=level, source="database row")
        for model_key, level in grounding
    ]
    return [*rows, *_setting_rows(inp, "grounding")]


def _group_system(inp: MenuInputs) -> list[SettingRow]:
    return _setting_rows(inp, "system")


def _group_builders() -> list[tuple[str, str, Callable[[MenuInputs], list[SettingRow]]]]:
    """Return ``(id, title, builder)`` for the nine groups, in menu order.

    Builders are looked up by module name at call time, so a test can replace one.
    """
    return [
        ("conversation", "This conversation", _group_conversation),
        ("saved_choices", "Saved choices", _group_saved_choices),
        ("models_per_role", "Models per role", _group_models_per_role),
        ("effort_and_modes", "Effort presets and model modes", _group_effort_and_modes),
        ("budgets", "Budgets and cost", _group_budgets),
        ("turn_runs", "How a turn runs", lambda inp: _setting_rows(inp, "turn_runs")),
        ("memory", "Memory and recall", lambda inp: _setting_rows(inp, "memory")),
        ("tools", "Tools and governance", _group_tools),
        ("grounding", "Grounding and quality", _group_grounding),
    ]


def _run_group(
    group_id: str,
    title: str,
    builder: Callable[[MenuInputs], list[SettingRow]],
    inp: MenuInputs,
    trace_id: str,
) -> SettingsGroup:
    try:
        rows = [_scrub_row(row) for row in builder(inp)]
    except Exception as exc:
        log.warning(
            "settings_menu_group_failed",
            group=group_id,
            error=str(exc),
            error_type=type(exc).__name__,
            trace_id=trace_id,
        )
        return SettingsGroup(id=group_id, title=title, error="unavailable")
    return SettingsGroup(id=group_id, title=title, rows=rows)


def build_menu(inp: MenuInputs, *, trace_id: str = "settings-menu") -> SettingsMenu:
    """Build the settings menu from already-loaded inputs. Pure: no I/O.

    Args:
        inp: The loaded sources. A source that failed to load is ``None``.
        trace_id: Trace identifier for the failure log.

    Returns:
        Nine groups in menu order and the System group. A group whose builder fails holds
        no rows and the error code ``"unavailable"``. The other groups still render.
    """
    groups = [
        _run_group(group_id, title, builder, inp, trace_id)
        for group_id, title, builder in _group_builders()
    ]
    system = _run_group("system", "System (read-only)", _group_system, inp, trace_id)
    return SettingsMenu(groups=groups, system=system)


# ---------------------------------------------------------------------------
# Input collection (I/O)
# ---------------------------------------------------------------------------


def _load_or_none[T](name: str, loader: Callable[[], T], trace_id: str) -> T | None:
    try:
        return loader()
    except Exception as exc:
        log.warning(
            "settings_menu_source_failed",
            source=name,
            error=str(exc),
            error_type=type(exc).__name__,
            trace_id=trace_id,
        )
        return None


async def collect_inputs(
    db: AsyncSession,
    *,
    user_id: UUID,
    session_id: UUID | None,
    location_consent: bool | None,
    trace_id: str,
) -> MenuInputs:
    """Read every source that the menu shows.

    Args:
        db: Async database session.
        user_id: The request user. Saved choices and the conversation are theirs only.
        session_id: The open conversation, or ``None``.
        location_consent: The user's consent as the route read it, or ``None`` when unread.
        trace_id: Trace identifier for failure logs.

    Returns:
        The inputs for :func:`build_menu`. A source that fails to load is ``None``.

    Raises:
        SessionNotFoundError: When ``session_id`` names no conversation of this user.
    """
    from sqlalchemy import select

    from personal_agent.config import (
        load_governance_config,
        load_model_config,
        settings,
    )
    from personal_agent.config.seshat_effort import load_seshat_effort
    from personal_agent.cost_gate import get_default_gate_or_none
    from personal_agent.orchestrator.constraint_options import build_provider_availability
    from personal_agent.service.models import GroundingEnforcementStateModel
    from personal_agent.service.repositories.constraint_preferences_repository import (
        ConstraintPreferencesRepository,
    )
    from personal_agent.service.repositories.session_model_selection_repository import (
        SessionModelSelectionRepository,
    )
    from personal_agent.service.repositories.session_repository import SessionRepository

    session_inputs: SessionInputs | None = None
    if session_id is not None:
        session = await SessionRepository(db).get(session_id, user_id=user_id)
        if session is None:
            raise SessionNotFoundError(str(session_id))
        session_inputs = SessionInputs(
            selections=await SessionModelSelectionRepository(db).get_all(session_id),
            effort_level=str(session.seshat_effort) if session.seshat_effort is not None else None,
        )

    saved: dict[str, str] | None = None
    try:
        rows = await ConstraintPreferencesRepository(db).list_for_user(user_id)
        saved = {str(r.constraint_name): str(r.preferred_action) for r in rows}
    except Exception as exc:
        log.warning(
            "settings_menu_source_failed", source="saved_choices", error=str(exc), trace_id=trace_id
        )

    grounding: list[tuple[str, str]] | None
    try:
        result = await db.execute(
            select(GroundingEnforcementStateModel.model_key, GroundingEnforcementStateModel.level)
        )
        grounding = [(str(k), str(v)) for k, v in result.all()]
    except Exception as exc:
        log.warning(
            "settings_menu_source_failed", source="grounding", error=str(exc), trace_id=trace_id
        )
        grounding = None

    models = _load_or_none("models", load_model_config, trace_id)
    gate = get_default_gate_or_none()
    return MenuInputs(
        app_settings=settings,
        models=models,
        effort=_load_or_none("effort", load_seshat_effort, trace_id),
        governance=_load_or_none("governance", load_governance_config, trace_id),
        budget=gate.config if gate is not None else None,
        saved_choices=saved,
        session=session_inputs,
        location_consent=location_consent,
        grounding=grounding,
        provider_available=(
            build_provider_availability(models, settings)
            if models is not None
            else (lambda _provider: False)
        ),
    )
