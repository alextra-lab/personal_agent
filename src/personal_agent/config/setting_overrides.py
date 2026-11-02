"""The setting overrides that this process applies (FRE-1722, FRE-1723).

The owner edits a parameter in the PWA. The edit goes in the ``settings_overrides`` table, and
the config file stays the default. Readers such as ``load_model_config`` are synchronous and run
outside a request, so they cannot read Postgres. They read this process-level copy instead. The
gateway fills it at startup and replaces it after each committed edit
(``service/settings_edit.py``). A process that never fills it, such as a CLI or a script, reads
the file values. A ``CostGate`` is the one exception: it adds the stored caps at ``connect``.

The gateway must stay one process: a second worker would not see an edit until it restarts.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from types import MappingProxyType

from pydantic import JsonValue

#: The parameter name of a role binding is this prefix and the role name.
ROLE_PARAMETER_PREFIX = "role."
#: The parameter that names the default effort level.
EFFORT_DEFAULT_LEVEL_PARAMETER = "effort.default_level"
#: The parameter name of an effort preset is this prefix and the level name.
EFFORT_LEVEL_PARAMETER_PREFIX = "effort.level."
#: The parameter name of a budget cap is this prefix, the time window, a dot and the role.
CAP_PARAMETER_PREFIX = "cap."

_overrides: Mapping[str, JsonValue] = MappingProxyType({})


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
