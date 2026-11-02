"""Edit settings from the PWA: owner check, validation and the override writes.

The override table holds the edit, and the config file stays the default (FRE-1722). A later ticket
adds a parameter family by adding a branch to :func:`validate_override` and :func:`file_value`. The
schema does not change. FRE-1723 added the effort presets and the budget caps.
"""

from __future__ import annotations

import asyncio
from uuid import UUID

import structlog
from pydantic import JsonValue
from sqlalchemy.ext.asyncio import AsyncSession

from personal_agent.config import settings
from personal_agent.config.model_loader import (
    ModelConfigError,
    file_role_deployments,
    load_model_config,
    validate_role_override,
)
from personal_agent.config.seshat_effort import (
    file_seshat_effort,
    preset_values,
    validate_effort_override,
)
from personal_agent.config.setting_overrides import (
    CAP_PARAMETER_PREFIX,
    EFFORT_DEFAULT_LEVEL_PARAMETER,
    EFFORT_LEVEL_PARAMETER_PREFIX,
    ROLE_PARAMETER_PREFIX,
    current_overrides,
    replace_overrides,
)
from personal_agent.cost_gate import (
    BudgetConfigError,
    get_default_gate_or_none,
    load_budget_config,
    sync_budget_policies_to_db,
)
from personal_agent.cost_gate.policy import cap_parameter, validate_cap_override
from personal_agent.exceptions import SeshatEffortConfigError
from personal_agent.llm_client.models import INHERIT_DEPLOYMENT
from personal_agent.orchestrator.constraint_options import build_provider_availability
from personal_agent.service.auth import RequestUser
from personal_agent.service.database import AsyncSessionLocal
from personal_agent.service.repositories.settings_overrides_repository import (
    SettingsOverridesRepository,
)
from personal_agent.telemetry.trace import SystemTraceContext

log = structlog.get_logger(__name__)

#: Covers the write and the update of the process copy. Without it, the copy can take the writes
#: in another order than the table, and turns then use a value that the table no longer holds.
_edit_lock = asyncio.Lock()


class SettingEditRefused(ValueError):
    """An edit that this module refuses. The message is the reason shown to the owner."""


def is_owner(user: RequestUser) -> bool:
    """Return whether a request comes from the owner, on a verified identity.

    Dev mode reads the email from a plaintext header, or falls back to the owner, so a dev mode
    identity never counts. With no owner email configured, nobody is the owner.

    Args:
        user: The resolved request identity.

    Returns:
        ``True`` only for a verified identity that equals ``AGENT_OWNER_EMAIL``.
    """
    owner_email = settings.agent_owner_email
    return bool(
        settings.gateway_auth_enabled and owner_email and user.email.lower() == owner_email.lower()
    )


def _is_effort_parameter(parameter: str) -> bool:
    return parameter == EFFORT_DEFAULT_LEVEL_PARAMETER or parameter.startswith(
        EFFORT_LEVEL_PARAMETER_PREFIX
    )


def validate_override(parameter: str, value: JsonValue) -> JsonValue:
    """Check one proposed edit. Each family goes through the model that loads its file.

    Args:
        parameter: The settings menu row name.
        value: The proposed value.

    Returns:
        The value to store.

    Raises:
        SettingEditRefused: With the reason, when the parameter cannot be edited or the value
            is invalid.
    """
    try:
        if _is_effort_parameter(parameter):
            return validate_effort_override(parameter, value)
        if parameter.startswith(CAP_PARAMETER_PREFIX):
            return validate_cap_override(load_budget_config(), parameter, value)
    except (SeshatEffortConfigError, BudgetConfigError) as exc:
        raise SettingEditRefused(str(exc)) from exc
    if not parameter.startswith(ROLE_PARAMETER_PREFIX):
        raise SettingEditRefused(f"{parameter} cannot be edited in the settings menu yet")
    _validate_role_binding(parameter, value)
    return value


def _validate_role_binding(parameter: str, value: JsonValue) -> None:
    role = parameter.removeprefix(ROLE_PARAMETER_PREFIX)
    if not isinstance(value, str):
        raise SettingEditRefused(f"{parameter} takes a deployment key")
    try:
        validate_role_override(role, value)
    except ModelConfigError as exc:
        raise SettingEditRefused(str(exc)) from exc
    if value == INHERIT_DEPLOYMENT:
        return
    config = load_model_config()
    provider = config.models[value].provider
    if not provider or not build_provider_availability(config, settings)(provider):
        raise SettingEditRefused(f"the provider of {value} has no credential on this gateway")


def file_value(parameter: str) -> JsonValue:
    """Return the value that applies when a parameter has no override.

    Args:
        parameter: The settings menu row name.

    Returns:
        The config file value, or ``None`` when the file no longer holds this parameter.
    """
    if parameter.startswith(ROLE_PARAMETER_PREFIX):
        return file_role_deployments().get(parameter.removeprefix(ROLE_PARAMETER_PREFIX))
    if parameter == EFFORT_DEFAULT_LEVEL_PARAMETER:
        return file_seshat_effort().default_level
    if parameter.startswith(EFFORT_LEVEL_PARAMETER_PREFIX):
        level = parameter.removeprefix(EFFORT_LEVEL_PARAMETER_PREFIX)
        return (
            preset_values(file_seshat_effort(), level)
            if level in file_seshat_effort().levels
            else None
        )
    if parameter.startswith(CAP_PARAMETER_PREFIX):
        return _file_cap(parameter)
    return None


def _file_cap(parameter: str) -> JsonValue:
    try:
        caps = load_budget_config().caps
    except BudgetConfigError:
        return None
    return next((float(cap.cap_usd) for cap in caps if cap_parameter(cap) == parameter), None)


async def _mirror_caps(parameter: str, trace_id: str) -> None:
    """Keep ``budget_policies`` equal to the caps that the gate enforces, after a cap edit.

    The dashboard joins on that table, and the gate does not read it. So a failure logs an error and
    does not undo the edit. The next edit or the next start repairs the table.
    """
    gate = get_default_gate_or_none()
    if not parameter.startswith(CAP_PARAMETER_PREFIX) or gate is None or gate.pool is None:
        return
    try:
        await sync_budget_policies_to_db(gate.config, gate.pool)
    except Exception as exc:
        log.error(
            "budget_policies_sync_failed",
            parameter=parameter,
            error=str(exc),
            error_type=type(exc).__name__,
            trace_id=trace_id,
        )


async def set_override(
    db: AsyncSession, parameter: str, value: JsonValue, *, changed_by: UUID, trace_id: str
) -> JsonValue:
    """Validate and store one override. The next read of the parameter sees the new value.

    Args:
        db: Async database session.
        parameter: The settings menu row name.
        value: The proposed value.
        changed_by: The user who made the edit.
        trace_id: Trace identifier for the log.

    Returns:
        The value that was stored. A preset holds every field, and a cap is a float.

    Raises:
        SettingEditRefused: With the reason. Nothing is stored.
    """
    try:
        stored_value = validate_override(parameter, value)
    except SettingEditRefused as exc:
        log.warning(
            "setting_override_refused_invalid",
            parameter=parameter,
            reason=str(exc),
            trace_id=trace_id,
        )
        raise
    repo = SettingsOverridesRepository(db)
    async with _edit_lock:
        stored = await repo.get(parameter)
        old_value = stored if stored is not None else file_value(parameter)
        await repo.set(
            parameter=parameter, value=stored_value, changed_by=changed_by, old_value=old_value
        )
        replace_overrides({**current_overrides(), parameter: stored_value})
        await _mirror_caps(parameter, trace_id)
    log.info(
        "setting_override_stored",
        parameter=parameter,
        old_value=old_value,
        new_value=stored_value,
        user_id=str(changed_by),
        trace_id=trace_id,
    )
    return stored_value


async def clear_override(
    db: AsyncSession, parameter: str, *, changed_by: UUID, trace_id: str
) -> bool:
    """Remove one override. The file value applies again.

    Args:
        db: Async database session.
        parameter: The settings menu row name.
        changed_by: The user who pressed Clear.
        trace_id: Trace identifier for the log.

    Returns:
        ``True`` when a row existed. With no row, nothing is written.
    """
    repo = SettingsOverridesRepository(db)
    async with _edit_lock:
        cleared = await repo.clear(
            parameter=parameter, changed_by=changed_by, new_value=file_value(parameter)
        )
        remaining = {key: val for key, val in current_overrides().items() if key != parameter}
        replace_overrides(remaining)
        if cleared:
            await _mirror_caps(parameter, trace_id)
    log.info(
        "setting_override_cleared",
        parameter=parameter,
        cleared=cleared,
        user_id=str(changed_by),
        trace_id=trace_id,
    )
    return cleared


async def load_overrides_at_startup() -> None:
    """Fill the process copy from the table. On a failure, log it and the file values apply."""
    trace_id = SystemTraceContext.new("load_setting_overrides").trace_id
    try:
        async with AsyncSessionLocal() as db:
            overrides = await SettingsOverridesRepository(db).list_all()
    except Exception as exc:
        log.warning(
            "setting_overrides_load_failed",
            error=str(exc),
            error_type=type(exc).__name__,
            trace_id=trace_id,
        )
        return
    replace_overrides(overrides)
    log.info("setting_overrides_loaded", parameters=sorted(overrides), trace_id=trace_id)
