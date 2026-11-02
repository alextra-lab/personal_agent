"""The setting overrides that this process applies (FRE-1722, FRE-1723, FRE-1724).

The owner edits a parameter in the PWA. The edit goes in the ``settings_overrides`` table, and
the config file stays the default. Readers such as ``load_model_config`` are synchronous and run
outside a request, so they cannot read Postgres. They read this process-level copy instead. The
gateway fills it at startup and replaces it after each committed edit
(``service/settings_edit.py``). A process that never fills it, such as a CLI or a script, reads
the file values. A ``CostGate`` is the one exception: it adds the stored caps at ``connect``.

An ``AppConfig`` override changes the one ``settings`` object in place, because every reader holds
that object (:func:`apply_setting_overrides`). A ``next_restart`` field changes only at start, so the
running gateway keeps one value.

The gateway must stay one process: a second worker would not see an edit until it restarts.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from types import MappingProxyType

import structlog
from pydantic import JsonValue

from personal_agent.config.setting_classes import NEXT_RESTART_SETTINGS, NEXT_TURN_SETTINGS

log = structlog.get_logger(__name__)

#: The parameter name of a role binding is this prefix and the role name.
ROLE_PARAMETER_PREFIX = "role."
#: The parameter that names the default effort level.
EFFORT_DEFAULT_LEVEL_PARAMETER = "effort.default_level"
#: The parameter name of an effort preset is this prefix and the level name.
EFFORT_LEVEL_PARAMETER_PREFIX = "effort.level."
#: The parameter name of a budget cap is this prefix, the time window, a dot and the role.
CAP_PARAMETER_PREFIX = "cap."
#: The parameter name of an ``AppConfig`` field is this prefix and the field name.
SETTING_PARAMETER_PREFIX = "setting."
#: The parameter name of a tool policy is this prefix and the tool name.
TOOL_PARAMETER_PREFIX = "tool."
#: The parameter that holds every sub-agent tool decision.
SUB_AGENT_TOOLS_PARAMETER = "sub_agent_tools"
#: The parameter that holds every mode transition rule.
MODE_TRANSITION_RULES_PARAMETER = "mode_transition_rules"
#: The parameter name of a catalog deployment is this prefix and the deployment key.
DEPLOYMENT_PARAMETER_PREFIX = "deployment."
#: The parameter name of a budget role estimate is this prefix and the role.
ROLE_ESTIMATE_PARAMETER_PREFIX = "role_estimate."

_overrides: Mapping[str, JsonValue] = MappingProxyType({})
#: The value of each ``AppConfig`` field that an override changed, as the process loaded it.
_originals: dict[str, object] = {}


def current_overrides() -> Mapping[str, JsonValue]:
    """Return every stored override that this process holds, keyed by parameter name."""
    return _overrides


def replace_overrides(values: Mapping[str, JsonValue]) -> None:
    """Replace this process's copy of the stored overrides.

    Args:
        values: Every row of ``settings_overrides``, keyed by parameter name.
    """
    global _overrides
    _overrides = MappingProxyType(dict(values))


def role_overrides() -> tuple[tuple[str, str], ...]:
    """Return the stored role bindings as sorted ``(role, deployment)`` pairs.

    Returns:
        A hashable value, so that the catalog built from it can be cached.
    """
    return tuple(
        sorted(
            (parameter.removeprefix(ROLE_PARAMETER_PREFIX), value)
            for parameter, value in _overrides.items()
            if parameter.startswith(ROLE_PARAMETER_PREFIX) and isinstance(value, str)
        )
    )


def effort_overrides() -> tuple[tuple[str, str], ...]:
    """Return the stored effort values as sorted ``(parameter, JSON value)`` pairs.

    Returns:
        A hashable value, so that the presets built from it can be cached.
    """
    return tuple(
        sorted(
            (parameter, json.dumps(value, sort_keys=True))
            for parameter, value in _overrides.items()
            if parameter == EFFORT_DEFAULT_LEVEL_PARAMETER
            or parameter.startswith(EFFORT_LEVEL_PARAMETER_PREFIX)
        )
    )


def deployment_overrides() -> tuple[tuple[str, str], ...]:
    """Return the stored deployment edits as sorted ``(key, JSON value)`` pairs.

    Returns:
        A hashable value, so that the catalog built from it can be cached.
    """
    return tuple(
        sorted(
            (parameter.removeprefix(DEPLOYMENT_PARAMETER_PREFIX), json.dumps(value, sort_keys=True))
            for parameter, value in _overrides.items()
            if parameter.startswith(DEPLOYMENT_PARAMETER_PREFIX)
        )
    )


def setting_rows() -> dict[str, JsonValue]:
    """Return the stored ``AppConfig`` values, keyed by field name."""
    return {
        parameter.removeprefix(SETTING_PARAMETER_PREFIX): value
        for parameter, value in _overrides.items()
        if parameter.startswith(SETTING_PARAMETER_PREFIX)
    }


def usable_setting_rows() -> dict[str, JsonValue]:
    """Return the stored rows that pass the checks of ``AppConfig`` together.

    The whole set goes first, because two rows can pass only together. If it fails, passes over the
    rows find the set that one row at a time can reach, so the order of the rows does not matter. A
    row that fails is skipped, and the file value applies, because a start must not fail on a stored
    row. The log holds the type of the error only, because the text can hold a value.

    Returns:
        The usable values, keyed by field name. A read-only or unknown field is never usable.
    """
    from personal_agent.config.settings import AppConfig  # noqa: PLC0415

    def _passes(values: Mapping[str, JsonValue]) -> Exception | None:
        try:
            AppConfig(**values)  # type: ignore[arg-type]
        except Exception as exc:  # any failure of a stored row means the file value applies
            return exc
        return None

    covered = NEXT_TURN_SETTINGS | NEXT_RESTART_SETTINGS
    rows = {field: value for field, value in setting_rows().items() if field in covered}
    if _passes(rows) is None:
        return rows
    usable: dict[str, JsonValue] = {}
    remaining = dict(sorted(rows.items()))
    errors: dict[str, Exception] = {}
    progress = True
    while remaining and progress:
        progress = False
        for field, value in list(remaining.items()):
            error = _passes({**usable, field: value})
            if error is None:
                usable[field] = remaining.pop(field)
                errors.pop(field, None)
                progress = True
            else:
                errors[field] = error
    for field in remaining:
        log.warning(
            "setting_override_skipped", field=field, error_type=type(errors[field]).__name__
        )
    return usable


def apply_setting_overrides(*, at_start: bool) -> None:
    """Make the ``settings`` object hold the stored ``AppConfig`` values.

    This writes to the instance dict and not through ``setattr``, so that ``model_fields_set`` still
    tells a value from ``.env`` from a code default.

    Args:
        at_start: ``True`` at gateway start, which covers ``next_turn`` and ``next_restart`` fields.
            ``False`` after an edit, which covers ``next_turn`` fields only.
    """
    from personal_agent.config import settings  # noqa: PLC0415
    from personal_agent.config.settings import AppConfig  # noqa: PLC0415

    covered = NEXT_TURN_SETTINGS | NEXT_RESTART_SETTINGS if at_start else NEXT_TURN_SETTINGS
    usable = usable_setting_rows()
    live = vars(settings)
    for field in [name for name in _originals if name in covered and name not in usable]:
        live[field] = _originals.pop(field)
    if not usable:
        return
    candidate = AppConfig(**usable)  # type: ignore[arg-type]
    for field in usable:
        if field in covered:
            _originals.setdefault(field, live[field])
            live[field] = getattr(candidate, field)


def setting_file_value(field: str) -> object:
    """Return the value of a field with no override: from ``.env``, or the code default.

    Args:
        field: An ``AppConfig`` field name.

    Returns:
        The value that the process loaded.
    """
    from personal_agent.config import settings  # noqa: PLC0415

    return _originals[field] if field in _originals else getattr(settings, field)


def applied_settings() -> Mapping[str, object]:
    """Return the original value of each field that holds an override now, keyed by field name."""
    return MappingProxyType(dict(_originals))
