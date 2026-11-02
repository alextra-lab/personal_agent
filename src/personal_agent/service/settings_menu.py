"""Settings menu payload (FRE-1702, FRE-1722, FRE-1723, FRE-1724; FRE-1698).

Builds the one response that the PWA settings menu renders: nine groups and a System
group. Each row carries the parameter name, its live value, the source that wins, and
(for an override) the value it overrides. A row also states when an edit applies, or why it is
read-only (``config/setting_classes.py``), and an editable row carries the default that applies with
no edit and its edit control. The writes live in ``settings_edit`` and in the existing preference and
session routes. This module writes nothing.

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

import json
import re
import types
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Literal, Union, get_args, get_origin
from uuid import UUID

import structlog
from pydantic import BaseModel, ConfigDict, Field, JsonValue
from pydantic_core import to_jsonable_python

from personal_agent.config.governance_loader import TOOL_PATH_FIELDS
from personal_agent.config.seshat_effort import (
    EFFORT_LEVELS,
    PlannerPolicy,
    PrimaryModeName,
    preset_values,
)
from personal_agent.config.setting_classes import (
    LOCATION_OFF_REASON,
    NEVER_APPLIED_REASON,
    NO_CONVERSATION_REASON,
    ONE_OPTION_REASON,
    PINNED_REASON,
    ApplyClass,
    row_class,
    setting_class,
)
from personal_agent.config.setting_overrides import (
    DEPLOYMENT_PARAMETER_PREFIX,
    EFFORT_DEFAULT_LEVEL_PARAMETER,
    EFFORT_LEVEL_PARAMETER_PREFIX,
    MODE_TRANSITION_RULES_PARAMETER,
    ROLE_ESTIMATE_PARAMETER_PREFIX,
    ROLE_PARAMETER_PREFIX,
    SETTING_PARAMETER_PREFIX,
    SUB_AGENT_TOOLS_PARAMETER,
    TOOL_PARAMETER_PREFIX,
)
from personal_agent.config.settings import AppConfig
from personal_agent.orchestrator.constraint_options import (
    ARTIFACT_BUILDER_CONSTRAINT,
    CONSTRAINT_OPTIONS,
    PREFERENCE_NEVER_APPLIED,
    artifact_builder_default_key,
    default_action_id,
    preference_options,
    resolve_artifact_builder_key,
)
from personal_agent.service.settings_menu_groups import HIDDEN_VALUE_GROUPS, SETTING_GROUPS

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from personal_agent.config.seshat_effort import SeshatEffortConfig
    from personal_agent.cost_gate.types import BudgetConfig, CapEntry
    from personal_agent.governance.models import GovernanceConfig
    from personal_agent.llm_client.models import ModelConfig, ModelDefinition

log = structlog.get_logger(__name__)

Source = Literal["code default", ".env", "config file", "database row", "session", "override"]

#: Marks a parameter with no stored row. ``None`` cannot do this, because a stored value can be null.
_NOT_STORED: JsonValue = "<not stored>"

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

_RESTART_NOTE = "applies at the next gateway restart"
_CONCURRENCY_NOTE = "an edit of max_concurrency applies at the next gateway restart"
_HOLDS_PATH_NOTE = "this value holds a path or an address, so it stays in the file"
_STALE_OVERRIDE_NOTE = "the stored override is not usable now, so the file value applies"
_D7_NOTE = (
    "this is the default level: an edit changes its planner prompt, and no D7 run "
    "(ADR-0157) qualifies the new prompt"
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


class Overridden(BaseModel):
    """The value that a row overrides, and where that value lives."""

    model_config = ConfigDict(frozen=True)

    value: JsonValue
    source: Source


class SettingEdit(BaseModel):
    """The edit control of a row.

    Attributes:
        kind: Which write the control calls, and which control it shows.
        options: The values that a select offers. Empty for a preset and a cap.
        current: The stored or bound value, or ``None`` when nothing is stored. A preset has no
            single value, so its control reads the row value.
        preset_choices: For a preset, the values that each named field allows.
        input: For a ``setting`` kind, which control the PWA shows.
        nullable: Whether an empty control saves a null.
    """

    model_config = ConfigDict(frozen=True)

    kind: Literal[
        "role_binding",
        "saved_choice",
        "effort_default_level",
        "effort_preset",
        "budget_cap",
        "setting",
        "session_primary",
        "session_effort",
        "location_consent",
    ]
    options: list[str]
    current: str | None
    preset_choices: dict[str, list[str]] | None = None
    input: Literal["boolean", "number", "text", "select", "json"] | None = None
    nullable: bool = False


class SettingRow(BaseModel):
    """One parameter in the menu.

    Attributes:
        name: Stable row name, for example ``setting.llm_timeout_seconds``.
        value: The live value. ``"set"`` or ``"not set"`` for a hidden value.
        source: The source that wins for the next turn.
        overrides: The value that this row overrides, when it overrides one.
        clearable: ``True`` for a saved choice or an override that exists. The Clear button
            reads it.
        note: A short plain-language remark about the row.
        file_default: The value that applies with no edit, and where it lives.
        edit: The edit control, for a row that this user can edit.
        applies: When an edit applies. ``read_only`` means that no edit reaches it.
        read_only_reason: Why the row is read-only.
    """

    model_config = ConfigDict(frozen=True)

    name: str
    value: JsonValue
    source: Source
    overrides: Overridden | None = None
    clearable: bool = False
    note: str | None = None
    file_default: Overridden | None = None
    edit: SettingEdit | None = None
    applies: ApplyClass | None = None
    read_only_reason: str | None = None


class SettingsGroup(BaseModel):
    """A menu group. ``error`` is the fixed code ``"unavailable"`` when the group failed."""

    model_config = ConfigDict(frozen=True)

    id: str
    title: str
    rows: list[SettingRow] = Field(default_factory=list)
    error: Literal["unavailable"] | None = None


class SettingsMenu(BaseModel):
    """The whole menu: nine groups, then the System group."""

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
        file_bindings: Role to deployment as the config file states it, with no override.
            ``None`` means ``models`` holds the file values.
        overrides: The stored overrides that this process holds.
        is_owner: Whether the request user can edit a global setting.
        file_effort: The presets as the file states them. ``None`` means ``effort`` holds the
            file values.
        file_budget: The budget as the file states it. ``None`` means ``budget`` holds the file
            values.
        file_governance: The governance files, with no override. ``None`` means ``governance``
            holds the file values.
        file_models: The catalog as the file states it. ``None`` means ``models`` holds the file
            values.
        applied_settings: The original value of each ``AppConfig`` field that holds an override.
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
    file_bindings: Mapping[str, str] | None = None
    overrides: Mapping[str, JsonValue] = field(default_factory=dict)
    is_owner: bool = False
    file_effort: SeshatEffortConfig | None = None
    file_budget: BudgetConfig | None = None
    file_governance: GovernanceConfig | None = None
    file_models: ModelConfig | None = None
    applied_settings: Mapping[str, object] = field(default_factory=dict)


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
    file_default = (
        Overridden(value=scrub(row.file_default.value), source=row.file_default.source)
        if row.file_default is not None
        else None
    )
    edit = (
        row.edit.model_copy(
            update={
                "options": [str(scrub(option)) for option in row.edit.options],
                "current": str(scrub(row.edit.current)) if row.edit.current is not None else None,
            }
        )
        if row.edit is not None
        else None
    )
    if edit is not None and edit.input == "json" and row.edit and edit.current != row.edit.current:
        # The scrub would replace the whole JSON text, and a Save would then send the mask.
        edit = None
        note = f"{note}; {_HOLDS_PATH_NOTE}" if note else _HOLDS_PATH_NOTE
    return row.model_copy(
        update={
            "value": scrub(row.value),
            "overrides": overrides,
            "note": note,
            "file_default": file_default,
            "edit": edit,
        }
    )


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


def is_hidden_setting(field: str) -> bool:
    """Return whether the menu, the audit record and the log hide the value of a setting.

    Args:
        field: An ``AppConfig`` field name.

    Returns:
        ``True`` for a secret, infrastructure or identity value.
    """
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


def _unwrap_optional(annotation: object) -> tuple[object, bool]:
    """Return ``(the annotation with no None, whether it allowed None)``."""
    if get_origin(annotation) not in (Union, types.UnionType):
        return annotation, False
    args = [arg for arg in get_args(annotation) if arg is not type(None)]
    nullable = len(args) != len(get_args(annotation))
    return (args[0], nullable) if len(args) == 1 else (annotation, nullable)


def _setting_input(
    field: str,
) -> tuple[Literal["boolean", "number", "text", "select", "json"], list[str], bool]:
    """Return ``(control, select options, nullable)`` from the annotation of a field."""
    annotation, nullable = _unwrap_optional(AppConfig.model_fields[field].annotation)
    if annotation is bool:
        return "boolean", [], nullable
    if annotation in (int, float):
        return "number", [], nullable
    if annotation is str:
        return "text", [], nullable
    if get_origin(annotation) is Literal:
        return "select", [str(arg) for arg in get_args(annotation)], nullable
    if isinstance(annotation, type) and issubclass(annotation, Enum):
        return "select", [str(member.value) for member in annotation], nullable
    return "json", [], nullable


def _setting_edit(field: str, live: object, *, hidden: bool) -> SettingEdit:
    control, options, nullable = _setting_input(field)
    current: str | None
    if hidden or live is None:
        current = None
    elif control == "json":
        current = json.dumps(_json(live), sort_keys=True)
    elif control == "boolean":
        current = "true" if live else "false"
    else:
        value = _json(live)
        current = str(value)
    return SettingEdit(
        kind="setting", options=options, current=current, input=control, nullable=nullable
    )


def _base_source(app_settings: AppConfig, field: str) -> Source:
    from_env = field in app_settings.model_fields_set or field in ENV_DRIVEN_FIELDS
    return ".env" if from_env else "code default"


def _setting_notes(inp: MenuInputs, field: str, *, hidden: bool) -> list[str]:
    """Say what a stored row does on this process, when the live value does not show it."""
    name = f"{SETTING_PARAMETER_PREFIX}{field}"
    restart = setting_class(field).applies == "next_restart"
    stored = inp.overrides.get(name, _NOT_STORED)
    applied = field in inp.applied_settings
    live = _json(getattr(inp.app_settings, field))
    notes = [_RESTART_NOTE] if restart and inp.is_owner else []
    if stored is _NOT_STORED:
        if applied:
            notes.append("Clear applies at the next gateway restart")
        return notes
    if applied and stored == live:
        return notes
    if not restart:
        return [_STALE_OVERRIDE_NOTE]
    notes.append(
        "a stored value waits for the next gateway restart"
        if hidden
        else f"the stored value {json.dumps(stored)} waits for the next gateway restart"
    )
    return notes


def _setting_row(inp: MenuInputs, field: str) -> SettingRow:
    app_settings = inp.app_settings
    name = f"{SETTING_PARAMETER_PREFIX}{field}"
    live = getattr(app_settings, field)
    hidden = is_hidden_setting(field)
    applied = field in inp.applied_settings
    base = inp.applied_settings[field] if applied else live
    base_source = _base_source(app_settings, field)
    source: Source = "override" if applied else base_source
    editable = inp.is_owner and setting_class(field).applies != "read_only"
    notes = _setting_notes(inp, field, hidden=hidden) if editable else []
    common: dict[str, object] = {
        "note": "; ".join(notes) if notes else None,
        "clearable": editable and name in inp.overrides,
        "edit": _setting_edit(field, live, hidden=hidden) if editable else None,
    }
    if hidden:
        file_default = Overridden(value=_set_or_not(base), source=base_source) if editable else None
        return SettingRow(
            name=name, value=_set_or_not(live), source=source, file_default=file_default
        ).model_copy(update=common)
    overrides = None
    if applied:
        overrides = Overridden(value=_json(base), source=base_source)
    elif base_source == ".env" and field not in ENV_DRIVEN_FIELDS:
        has_default, default = _code_default(field)
        if has_default and _json(default) != _json(live):
            overrides = Overridden(value=_json(default), source="code default")
    return SettingRow(
        name=name,
        value=_json(live),
        source=source,
        overrides=overrides,
        file_default=Overridden(value=_json(base), source=base_source) if editable else None,
    ).model_copy(update=common)


def _setting_rows(inp: MenuInputs, menu_group: str) -> list[SettingRow]:
    return [
        _setting_row(inp, field)
        for field in sorted(AppConfig.model_fields)
        if menu_group_of(field) == menu_group
    ]


def _json_edit(current: JsonValue) -> SettingEdit:
    return SettingEdit(
        kind="setting", options=[], current=json.dumps(current, sort_keys=True), input="json"
    )


def _override_row(
    inp: MenuInputs,
    name: str,
    value: JsonValue,
    live: JsonValue,
    file_fields: JsonValue,
    *,
    whole: bool = False,
    owner_note: str | None = None,
) -> SettingRow:
    """Build a row that a JSON override edits.

    Args:
        inp: The menu inputs.
        name: The row name.
        value: The value that the row shows.
        live: The editable fields that apply now, in the form of the stored value.
        file_fields: The same fields as the file states them.
        whole: A stored value replaces the whole row. Otherwise it holds the edited fields.
        owner_note: A note that only the owner needs.

    Returns:
        The row, with its source, Clear, default and control.
    """
    stored = inp.overrides.get(name, _NOT_STORED)
    applies = False
    if stored is not _NOT_STORED:
        applies = (
            stored == live
            if whole
            else isinstance(stored, dict)
            and isinstance(live, dict)
            and all(live.get(key) == field_value for key, field_value in stored.items())
        )
    notes = [_STALE_OVERRIDE_NOTE] if stored is not _NOT_STORED and not applies else []
    if inp.is_owner and owner_note:
        notes.append(owner_note)
    return SettingRow(
        name=name,
        value=value,
        source="override" if applies else "config file",
        note="; ".join(notes) if notes else None,
        clearable=inp.is_owner and stored is not _NOT_STORED,
        file_default=Overridden(value=file_fields, source="config file"),
        edit=_json_edit(live) if inp.is_owner else None,
    )


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


def _override_applies(inp: MenuInputs, role: str) -> bool:
    """Return whether a stored override binds this role. A stale one does not."""
    stored = inp.overrides.get(f"{ROLE_PARAMETER_PREFIX}{role}")
    binding = inp.models.roles.get(role) if inp.models is not None else None
    return stored is not None and binding is not None and binding.deployment == stored


def _binding_source(inp: MenuInputs, role: str) -> Source:
    """Return where a role's binding comes from."""
    return "override" if _override_applies(inp, role) else "config file"


_READ_ONLY_NO_CONVERSATION: Mapping[str, object] = {
    "applies": "read_only",
    "read_only_reason": NO_CONVERSATION_REASON,
}


def _group_conversation(inp: MenuInputs) -> list[SettingRow]:
    effort = _need(inp.effort)
    primary, primary_default = _effective_primary(inp)
    binding_source = _binding_source(inp, "primary")
    primary_row = SettingRow(
        name="session.primary_model",
        value=primary,
        source="session" if primary_default is not None else binding_source,
        overrides=(
            Overridden(value=primary_default, source=binding_source)
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
            overrides=Overridden(
                value=effort.default_level, source=_default_level_source(inp, effort)
            ),
        )
    else:
        effort_row = SettingRow(
            name="session.effort_level",
            value=effort.default_level,
            source=_default_level_source(inp, effort),
        )
    return [
        _with_session_primary_edit(primary_row, inp),
        _with_session_effort_edit(effort_row, inp),
    ]


def _with_session_primary_edit(row: SettingRow, inp: MenuInputs) -> SettingRow:
    """Add the model picker's write to the row. It goes through the existing selection route."""
    from personal_agent.config.model_loader import role_binding_options

    models = _need(inp.models)
    if inp.session is None:
        return row.model_copy(update=_READ_ONLY_NO_CONVERSATION)
    binding = models.roles.get("primary")
    if binding is None or not binding.open:
        return row.model_copy(update={"applies": "read_only", "read_only_reason": PINNED_REASON})
    options = role_binding_options("primary", models, is_provider_available=inp.provider_available)
    current = str(row.value)
    return row.model_copy(
        update={
            "edit": SettingEdit(
                kind="session_primary",
                options=options if current in options else [current, *options],
                current=current,
            )
        }
    )


def _with_session_effort_edit(row: SettingRow, inp: MenuInputs) -> SettingRow:
    """Add the effort control's write to the row. It goes through the existing effort route."""
    if inp.session is None:
        return row.model_copy(update=_READ_ONLY_NO_CONVERSATION)
    return row.model_copy(
        update={
            "edit": SettingEdit(
                kind="session_effort", options=list(EFFORT_LEVELS), current=str(row.value)
            )
        }
    )


def _saved_choice_default(constraint: str, inp: MenuInputs) -> Overridden:
    if constraint == ARTIFACT_BUILDER_CONSTRAINT:
        return Overridden(
            value=artifact_builder_default_key(_need(inp.models)),
            source=_binding_source(inp, ARTIFACT_BUILDER_CONSTRAINT),
        )
    return Overridden(
        value=f"decision card (safe default: {default_action_id(constraint)})",
        source="code default",
    )


def _saved_choice_edit(
    constraint: str, saved: str | None, inp: MenuInputs
) -> tuple[Overridden | None, SettingEdit | None]:
    """Return the default with no saved choice, and the control. A saved choice is per user."""
    if constraint == ARTIFACT_BUILDER_CONSTRAINT and inp.models is None:
        return None, None
    file_default = _saved_choice_default(constraint, inp)
    if constraint == ARTIFACT_BUILDER_CONSTRAINT and inp.file_bindings is not None:
        file_default = Overridden(
            value=inp.file_bindings.get(constraint, file_default.value), source="config file"
        )
    if constraint in PREFERENCE_NEVER_APPLIED:
        return file_default, None
    edit = SettingEdit(
        kind="saved_choice", options=preference_options(constraint, inp.models), current=saved
    )
    return file_default, edit


def _group_saved_choices(inp: MenuInputs) -> list[SettingRow]:
    saved_choices = _need(inp.saved_choices)
    rows: list[SettingRow] = []
    for constraint in [*CONSTRAINT_OPTIONS, ARTIFACT_BUILDER_CONSTRAINT]:
        saved = saved_choices.get(constraint)
        note = (
            "The turn never applies a saved choice here. The card always asks."
            if constraint in PREFERENCE_NEVER_APPLIED
            else None
        )
        file_default, edit = _saved_choice_edit(constraint, saved, inp)
        read_only: dict[str, object] = (
            {"applies": "read_only", "read_only_reason": NEVER_APPLIED_REASON}
            if constraint in PREFERENCE_NEVER_APPLIED
            else {}
        )
        if saved is None:
            rows.append(
                SettingRow(
                    name=f"saved_choice.{constraint}",
                    value="not set",
                    source="code default",
                    note=note,
                    file_default=file_default,
                    edit=edit,
                ).model_copy(update=read_only)
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
                file_default=file_default,
                edit=edit,
            ).model_copy(update=read_only)
        )
    if inp.app_settings.location_enabled:
        consent: JsonValue = "unavailable" if inp.location_consent is None else inp.location_consent
        rows.append(
            SettingRow(
                name="location.consent",
                value=consent,
                source="database row",
                edit=(
                    SettingEdit(
                        kind="location_consent",
                        options=[],
                        current="true" if inp.location_consent else "false",
                        input="boolean",
                    )
                    if inp.location_consent is not None
                    else None
                ),
            )
        )
    else:
        rows.append(
            SettingRow(
                name="location.consent",
                value="off (the operator flag location_enabled is off)",
                source="code default",
                applies="read_only",
                read_only_reason=LOCATION_OFF_REASON,
            )
        )
    return [*rows, *_setting_rows(inp, "saved_choices")]


def _group_models_per_role(inp: MenuInputs) -> list[SettingRow]:
    from personal_agent.config.model_loader import resolve_selected_deployment
    from personal_agent.llm_client.models import INHERIT_DEPLOYMENT

    models = _need(inp.models)
    primary, primary_default = _effective_primary(inp)
    primary_source = _binding_source(inp, "primary")
    rows: list[SettingRow] = []
    for role, binding in models.roles.items():
        default = resolve_selected_deployment(role, None, models)
        note = "open" if binding.open else "pinned"
        if role == "primary":
            rows.append(
                SettingRow(
                    name=f"role.{role}",
                    value=primary,
                    source="session" if primary_default is not None else primary_source,
                    overrides=(
                        Overridden(value=primary_default, source=primary_source)
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
                    source="session" if primary_default is not None else _binding_source(inp, role),
                    note=f"{note}; inherits the primary model",
                )
            )
        elif role == ARTIFACT_BUILDER_CONSTRAINT:
            rows.append(_artifact_builder_role_row(inp, models, default, note))
        else:
            rows.append(
                SettingRow(
                    name=f"role.{role}",
                    value=default,
                    source=_binding_source(inp, role),
                    note=note,
                )
            )
    return [_with_role_edit(row, inp, models) for row in rows]


def _with_role_edit(row: SettingRow, inp: MenuInputs, models: ModelConfig) -> SettingRow:
    """Add the file default, the stale-override note and the owner's control to a role row."""
    from personal_agent.config.model_loader import role_binding_options

    role = row.name.removeprefix(ROLE_PARAMETER_PREFIX)
    binding = models.roles[role].deployment
    file_binding = (inp.file_bindings or {}).get(role, binding)
    stored = inp.overrides.get(row.name)
    notes = [row.note] if row.note else []
    if stored is not None and not _override_applies(inp, role):
        notes.append(f"the override {stored} is not usable now, so the file value applies")
    if role == "primary" and row.source == "session" and inp.is_owner:
        notes.append("this conversation keeps its own model, so an edit reaches new conversations")
    update: dict[str, object] = {
        "file_default": Overridden(value=file_binding, source="config file"),
        "note": "; ".join(notes) if notes else None,
    }
    options = role_binding_options(role, models, is_provider_available=inp.provider_available)
    choices = options if binding in options else [binding, *options]
    if len(choices) <= 1:
        update["applies"] = "read_only"
        update["read_only_reason"] = ONE_OPTION_REASON
    if inp.is_owner:
        if len(choices) > 1:
            update["edit"] = SettingEdit(kind="role_binding", options=choices, current=binding)
        update["clearable"] = stored is not None
    return row.model_copy(update=update)


def _artifact_builder_role_row(
    inp: MenuInputs, models: ModelConfig, default: str, note: str
) -> SettingRow:
    """Show the deployment that the next build uses, by the turn's own resolver."""
    binding_source = _binding_source(inp, ARTIFACT_BUILDER_CONSTRAINT)
    if inp.saved_choices is None:
        return SettingRow(
            name=f"role.{ARTIFACT_BUILDER_CONSTRAINT}",
            value=default,
            source=binding_source,
            note=f"{note}; saved choices could not be read, so a saved choice may override this",
        )
    saved = inp.saved_choices.get(ARTIFACT_BUILDER_CONSTRAINT)
    if saved is None or saved == "always_pause":
        return SettingRow(
            name=f"role.{ARTIFACT_BUILDER_CONSTRAINT}",
            value=default,
            source=binding_source,
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
            overrides=Overridden(value=default, source=binding_source),
            note=note,
        )
    return SettingRow(
        name=f"role.{ARTIFACT_BUILDER_CONSTRAINT}",
        value=default,
        source=binding_source,
        note=f"{note}; the saved choice {saved} is not usable now, so the default applies",
    )


def _default_level_source(inp: MenuInputs, effort: SeshatEffortConfig) -> Source:
    """Return where the default level comes from. A stale override does not count."""
    stored = inp.overrides.get(EFFORT_DEFAULT_LEVEL_PARAMETER)
    return "override" if stored is not None and stored == effort.default_level else "config file"


def _effort_default_row(inp: MenuInputs, effort: SeshatEffortConfig) -> SettingRow:
    file_effort = inp.file_effort or effort
    source = _default_level_source(inp, effort)
    stored = inp.overrides.get(EFFORT_DEFAULT_LEVEL_PARAMETER)
    notes = [_STALE_OVERRIDE_NOTE] if stored is not None and source != "override" else []
    if inp.is_owner:
        notes.append(
            f"a conversation keeps its level, so an edit reaches new conversations; {_D7_NOTE}"
        )
    return SettingRow(
        name=EFFORT_DEFAULT_LEVEL_PARAMETER,
        value=effort.default_level,
        source=source,
        note="; ".join(notes) if notes else None,
        file_default=Overridden(value=file_effort.default_level, source="config file"),
        clearable=inp.is_owner and stored is not None,
        edit=(
            SettingEdit(
                kind="effort_default_level",
                options=list(EFFORT_LEVELS),
                current=effort.default_level,
            )
            if inp.is_owner
            else None
        ),
    )


def _effort_level_row(inp: MenuInputs, effort: SeshatEffortConfig, level: str) -> SettingRow:
    file_effort = inp.file_effort or effort
    name = f"{EFFORT_LEVEL_PARAMETER_PREFIX}{level}"
    value = preset_values(effort, level)
    stored = inp.overrides.get(name)
    applies = stored is not None and stored == value
    notes = [_STALE_OVERRIDE_NOTE] if stored is not None and not applies else []
    if inp.is_owner and level == effort.default_level:
        notes.append(_D7_NOTE)
    return SettingRow(
        name=name,
        value=value,
        source="override" if applies else "config file",
        note="; ".join(notes) if notes else None,
        file_default=Overridden(value=preset_values(file_effort, level), source="config file"),
        clearable=inp.is_owner and stored is not None,
        edit=(
            SettingEdit(
                kind="effort_preset",
                options=[],
                current=None,
                preset_choices={
                    "planner_policy": list(get_args(PlannerPolicy)),
                    "primary_mode": list(get_args(PrimaryModeName)),
                },
            )
            if inp.is_owner
            else None
        ),
    )


def _group_effort_and_modes(inp: MenuInputs) -> list[SettingRow]:
    effort = _need(inp.effort)
    models = _need(inp.models)
    rows = [
        _effort_default_row(inp, effort),
        *(_effort_level_row(inp, effort, level) for level in effort.levels),
    ]
    file_models = (inp.file_models or models).models
    for key, definition in models.models.items():
        rows.append(_deployment_row(inp, key, definition, file_models.get(key, definition)))
    return [*rows, *_setting_rows(inp, "effort_and_modes")]


def _deployment_fields(definition: ModelDefinition) -> dict[str, JsonValue]:
    """Return the editable fields of a deployment. The endpoint stays in the file."""
    dump: dict[str, JsonValue] = definition.model_dump(
        mode="json", exclude_none=True, exclude={"endpoint"}
    )
    return dump


def _deployment_row(
    inp: MenuInputs, key: str, definition: ModelDefinition, file_definition: ModelDefinition
) -> SettingRow:
    name = f"{DEPLOYMENT_PARAMETER_PREFIX}{key}"
    dump = definition.model_dump(mode="json", exclude_none=True)
    if "endpoint" in dump:
        dump["endpoint"] = _set_or_not(definition.endpoint)
    return _override_row(
        inp,
        name,
        _json(dump),
        _deployment_fields(definition),
        _deployment_fields(file_definition),
        owner_note=_CONCURRENCY_NOTE,
    )


def _cap_row(inp: MenuInputs, cap: CapEntry, file_cap: float | None) -> SettingRow:
    """Show the cap that the gate enforces, with the file value and the owner's control."""
    from personal_agent.cost_gate.policy import cap_parameter

    name = cap_parameter(cap)
    value = float(cap.cap_usd)
    stored = inp.overrides.get(name)
    applies = isinstance(stored, int | float) and not isinstance(stored, bool) and stored == value
    return SettingRow(
        name=name,
        value=value,
        source="override" if applies else "config file",
        note=_STALE_OVERRIDE_NOTE if stored is not None and not applies else None,
        file_default=Overridden(
            value=file_cap if file_cap is not None else value, source="config file"
        ),
        clearable=inp.is_owner and stored is not None,
        edit=(
            SettingEdit(kind="budget_cap", options=[], current=str(value)) if inp.is_owner else None
        ),
    )


def _group_budgets(inp: MenuInputs) -> list[SettingRow]:
    from personal_agent.cost_gate.policy import cap_parameter

    budget = _need(inp.budget)
    file_caps = {cap_parameter(cap): float(cap.cap_usd) for cap in (inp.file_budget or budget).caps}
    rows = [_cap_row(inp, cap, file_caps.get(cap_parameter(cap))) for cap in budget.caps]
    file_roles = (inp.file_budget or budget).roles
    for role, config in budget.roles.items():
        name = f"{ROLE_ESTIMATE_PARAMETER_PREFIX}{role}"
        live = _json(config.model_dump(mode="json"))
        file_fields = _json(file_roles.get(role, config).model_dump(mode="json"))
        rows.append(_override_row(inp, name, live, live, file_fields))
    rows.append(
        SettingRow(name="uncapped_roles", value=list(budget.uncapped_roles), source="config file")
    )
    return [*rows, *_setting_rows(inp, "budgets")]


def _group_tools(inp: MenuInputs) -> list[SettingRow]:
    governance = _need(inp.governance)
    file_governance = inp.file_governance or governance
    rows: list[SettingRow] = []
    for name, policy in governance.tools.items():
        row_name = f"{TOOL_PARAMETER_PREFIX}{name}"
        editable = policy.model_dump(mode="json", exclude=set(TOOL_PATH_FIELDS))
        file_policy = file_governance.tools.get(name, policy)
        dump = {**editable, **{field: len(getattr(policy, field)) for field in TOOL_PATH_FIELDS}}
        rows.append(
            _override_row(
                inp,
                row_name,
                _json(dump),
                _json(editable),
                _json(file_policy.model_dump(mode="json", exclude=set(TOOL_PATH_FIELDS))),
            )
        )
    for row_name, live, file_map in (
        (SUB_AGENT_TOOLS_PARAMETER, governance.sub_agent_tools, file_governance.sub_agent_tools),
        (
            MODE_TRANSITION_RULES_PARAMETER,
            governance.transition_rules,
            file_governance.transition_rules,
        ),
    ):
        live_json = _json({k: v.model_dump(mode="json") for k, v in live.items()})
        file_json = _json({k: v.model_dump(mode="json") for k, v in file_map.items()})
        rows.append(_override_row(inp, row_name, live_json, live_json, file_json, whole=True))
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


def _with_class(row: SettingRow) -> SettingRow:
    """Add the class of a row. A builder sets it first for a dynamic exception to the table."""
    if row.applies is not None:
        return row
    result = row_class(row.name)
    return row.model_copy(update={"applies": result.applies, "read_only_reason": result.reason})


def _run_group(
    group_id: str,
    title: str,
    builder: Callable[[MenuInputs], list[SettingRow]],
    inp: MenuInputs,
    trace_id: str,
) -> SettingsGroup:
    try:
        rows = [_with_class(_scrub_row(row)) for row in builder(inp)]
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
    system = _run_group("system", "System", _group_system, inp, trace_id)
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
    is_owner: bool = False,
) -> MenuInputs:
    """Read every source that the menu shows.

    Args:
        db: Async database session.
        user_id: The request user. Saved choices and the conversation are theirs only.
        session_id: The open conversation, or ``None``.
        location_consent: The user's consent as the route read it, or ``None`` when unread.
        trace_id: Trace identifier for failure logs.
        is_owner: Whether the request user can edit a global setting.

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
    from personal_agent.config.governance_loader import file_governance_config
    from personal_agent.config.model_loader import file_model_config, file_role_deployments
    from personal_agent.config.seshat_effort import file_seshat_effort, get_seshat_effort
    from personal_agent.config.setting_overrides import applied_settings, current_overrides
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
        effort=_load_or_none("effort", get_seshat_effort, trace_id),
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
        file_bindings=_load_or_none("file_bindings", file_role_deployments, trace_id),
        overrides=current_overrides(),
        is_owner=is_owner,
        file_effort=_load_or_none("file_effort", file_seshat_effort, trace_id),
        file_budget=gate.file_config if gate is not None else None,
        file_governance=_load_or_none("file_governance", file_governance_config, trace_id),
        file_models=_load_or_none("file_models", file_model_config, trace_id),
        applied_settings=applied_settings(),
    )
