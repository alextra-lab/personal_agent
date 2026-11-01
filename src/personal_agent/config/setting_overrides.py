"""The setting overrides that this process applies (FRE-1722).

The owner edits a parameter in the PWA. The edit goes in the ``settings_overrides`` table, and
the config file stays the default. Readers such as ``load_model_config`` are synchronous and run
outside a request, so they cannot read Postgres. They read this process-level copy instead. The
gateway fills it at startup and replaces it after each committed edit
(``service/settings_edit.py``). A process that never fills it, such as a CLI or a script, reads
the file values.

The gateway must stay one process: a second worker would not see an edit until it restarts.
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType

from pydantic import JsonValue

#: The parameter name of a role binding is this prefix and the role name.
ROLE_PARAMETER_PREFIX = "role."

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
