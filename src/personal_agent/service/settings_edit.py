"""Edit settings from the PWA: owner check, validation and the override writes.

The override table holds the edit, and the config file stays the default (FRE-1722). A later ticket
adds a parameter family by adding a branch to :func:`validate_override` and :func:`file_value`. The
schema does not change. FRE-1723 added the effort presets and the budget caps. FRE-1724 added the
``AppConfig`` settings, the governance rows, the deployments and the role estimates.

A hidden setting is write-only (FRE-1724 AC-4). The response, the audit row and the log hold ``"set"``
or ``"not set"``. Only the override row holds the value, because the gateway must apply it. A secret
never gets here: :func:`validate_override` refuses it and does not read its value.
"""

from __future__ import annotations

import asyncio
import json
import re
from typing import Annotated
from uuid import UUID

import structlog
from pydantic import JsonValue, TypeAdapter, ValidationError
from pydantic_core import to_jsonable_python
from sqlalchemy.ext.asyncio import AsyncSession

from personal_agent.config import get_settings, settings
from personal_agent.config.governance_loader import (
    TOOL_PATH_FIELDS,
    GovernanceConfigError,
    file_governance_config,
    validate_governance_override,
)
from personal_agent.config.model_loader import (
    ModelConfigError,
    file_deployment_fields,
    file_role_deployments,
    load_model_config,
    validate_deployment_override,
    validate_role_override,
)
from personal_agent.config.seshat_effort import (
    check_round_ceilings,
    file_seshat_effort,
    forget_effort_overrides,
    get_seshat_effort,
    preset_values,
    validate_effort_override,
)
from personal_agent.config.setting_classes import setting_class
from personal_agent.config.setting_overrides import (
    CAP_PARAMETER_PREFIX,
    DEPLOYMENT_PARAMETER_PREFIX,
    EFFORT_DEFAULT_LEVEL_PARAMETER,
    EFFORT_LEVEL_PARAMETER_PREFIX,
    MODE_TRANSITION_RULES_PARAMETER,
    ROLE_ESTIMATE_PARAMETER_PREFIX,
    ROLE_PARAMETER_PREFIX,
    SETTING_PARAMETER_PREFIX,
    SUB_AGENT_TOOLS_PARAMETER,
    TOOL_PARAMETER_PREFIX,
    apply_setting_overrides,
    current_overrides,
    replace_overrides,
    setting_file_value,
    usable_setting_rows,
)
from personal_agent.config.settings import AppConfig
from personal_agent.cost_gate import (
    BudgetConfigError,
    get_default_gate_or_none,
    load_budget_config,
    sync_budget_policies_to_db,
)
from personal_agent.cost_gate.policy import (
    cap_parameter,
    validate_cap_override,
    validate_role_estimate_override,
)
from personal_agent.exceptions import SeshatEffortConfigError
from personal_agent.llm_client.models import INHERIT_DEPLOYMENT
from personal_agent.llm_client.pricing import register_model_pricing
from personal_agent.orchestrator.constraint_options import build_provider_availability
from personal_agent.service.auth import RequestUser
from personal_agent.service.database import AsyncSessionLocal
from personal_agent.service.repositories.settings_overrides_repository import (
    SettingsOverridesRepository,
)
from personal_agent.service.settings_menu import is_hidden_setting
from personal_agent.telemetry.trace import SystemTraceContext

log = structlog.get_logger(__name__)

#: Covers the write and the update of the process copy. Without it, the copy can take the writes
#: in another order than the table, and turns then use a value that the table no longer holds.
_edit_lock = asyncio.Lock()

#: A URL that holds a password. It belongs in ``.env``, not in the override store.
_PASSWORD_IN_URL = re.compile(r"://[^/@\s]*:[^/@\s]*@")
#: A validator message with one of these can hold a configured URL, address or path.
_UNSAFE_MESSAGE = re.compile(r"://|@|/")
#: These fields bound the effort presets, so an edit must still fit every preset.
_ROUND_CEILING_FIELDS = frozenset(
    {"sub_agent_max_tool_iterations", "sub_agent_rounds_by_thoroughness"}
)


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
        if parameter.startswith(ROLE_ESTIMATE_PARAMETER_PREFIX):
            return validate_role_estimate_override(load_budget_config(), parameter, value)
        if parameter.startswith(DEPLOYMENT_PARAMETER_PREFIX):
            key = parameter.removeprefix(DEPLOYMENT_PARAMETER_PREFIX)
            return validate_deployment_override(key, value)
        if _is_governance_parameter(parameter):
            return validate_governance_override(parameter, value)
    except (
        SeshatEffortConfigError,
        BudgetConfigError,
        ModelConfigError,
        GovernanceConfigError,
    ) as exc:
        raise SettingEditRefused(str(exc)) from exc
    if parameter.startswith(SETTING_PARAMETER_PREFIX):
        return _validate_setting(parameter, value)
    if not parameter.startswith(ROLE_PARAMETER_PREFIX):
        raise SettingEditRefused(f"{parameter} cannot be edited in the settings menu")
    _validate_role_binding(parameter, value)
    return value


def _is_governance_parameter(parameter: str) -> bool:
    return parameter in (
        SUB_AGENT_TOOLS_PARAMETER,
        MODE_TRANSITION_RULES_PARAMETER,
    ) or parameter.startswith(TOOL_PARAMETER_PREFIX)


def _holds_url_password(value: JsonValue) -> bool:
    if isinstance(value, str):
        return bool(_PASSWORD_IN_URL.search(value))
    if isinstance(value, list):
        return any(_holds_url_password(item) for item in value)
    if isinstance(value, dict):
        return any(_holds_url_password(item) for item in value.values())
    return False


def _refusal_reason(parameter: str, exc: ValidationError, *, hidden: bool) -> str:
    """Return a reason that holds no value, for the response and the log.

    A cross-field message can quote a configured URL with its password, so a hidden field gets the
    error types only, and any other field loses a message that can hold an address or a path.
    """
    errors = exc.errors(include_input=False, include_url=False)
    if hidden:
        return f"{parameter} refused: {', '.join(sorted({err['type'] for err in errors}))}"
    messages = [
        "the settings checks refuse this value"
        if _UNSAFE_MESSAGE.search(err["msg"])
        else err["msg"]
        for err in errors
    ]
    return f"{parameter}: {'; '.join(messages)}"


def _validate_setting(parameter: str, value: JsonValue) -> JsonValue:
    """Check a proposed ``AppConfig`` value. A secret is refused before its value is read."""
    field = parameter.removeprefix(SETTING_PARAMETER_PREFIX)
    try:
        row = setting_class(field)
    except KeyError:
        raise SettingEditRefused(f"{parameter} is not a setting") from None
    if row.applies == "read_only":
        raise SettingEditRefused(f"{parameter} cannot be edited: {row.reason}")
    if _holds_url_password(value):
        raise SettingEditRefused(f"{parameter}: a URL with a password belongs in .env")
    hidden = is_hidden_setting(field)
    info = AppConfig.model_fields[field]
    annotation = Annotated[info.annotation, *info.metadata] if info.metadata else info.annotation
    try:
        TypeAdapter(annotation).validate_json(json.dumps(value), strict=True)  # type: ignore[arg-type]
        candidate = AppConfig(**{**usable_setting_rows(), field: value})  # type: ignore[arg-type]
    except ValidationError as exc:
        raise SettingEditRefused(_refusal_reason(parameter, exc, hidden=hidden)) from None
    if field in _ROUND_CEILING_FIELDS:
        try:
            check_round_ceilings(get_seshat_effort(), candidate)
        except SeshatEffortConfigError as exc:
            raise SettingEditRefused(str(exc)) from exc
    return to_jsonable_python(getattr(candidate, field), fallback=str)  # type: ignore[no-any-return]


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
    if parameter.startswith(SETTING_PARAMETER_PREFIX):
        return _file_setting(parameter.removeprefix(SETTING_PARAMETER_PREFIX))
    if parameter.startswith(DEPLOYMENT_PARAMETER_PREFIX):
        return file_deployment_fields(parameter.removeprefix(DEPLOYMENT_PARAMETER_PREFIX))
    if parameter.startswith(ROLE_ESTIMATE_PARAMETER_PREFIX):
        return _file_role_estimate(parameter.removeprefix(ROLE_ESTIMATE_PARAMETER_PREFIX))
    if _is_governance_parameter(parameter):
        return _file_governance(parameter)
    return None


def _is_hidden_parameter(parameter: str) -> bool:
    field = parameter.removeprefix(SETTING_PARAMETER_PREFIX)
    return (
        parameter.startswith(SETTING_PARAMETER_PREFIX)
        and field in AppConfig.model_fields
        and is_hidden_setting(field)
    )


def _file_setting(field: str) -> JsonValue:
    if field not in AppConfig.model_fields:
        return None
    value = setting_file_value(field)
    if is_hidden_setting(field):
        return "not set" if value in (None, "", [], (), {}) else "set"
    return to_jsonable_python(value, fallback=str)  # type: ignore[no-any-return]


def _file_role_estimate(role: str) -> JsonValue:
    try:
        config = load_budget_config().roles.get(role)
    except BudgetConfigError:
        return None
    return config.model_dump(mode="json") if config is not None else None


def _file_governance(parameter: str) -> JsonValue:
    config = file_governance_config()
    if parameter == SUB_AGENT_TOOLS_PARAMETER:
        return {
            name: entry.model_dump(mode="json") for name, entry in config.sub_agent_tools.items()
        }
    if parameter == MODE_TRANSITION_RULES_PARAMETER:
        return {
            name: entry.model_dump(mode="json") for name, entry in config.transition_rules.items()
        }
    policy = config.tools.get(parameter.removeprefix(TOOL_PARAMETER_PREFIX))
    if policy is None:
        return None
    return policy.model_dump(mode="json", exclude=set(TOOL_PATH_FIELDS))


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
        The value that was stored. A preset holds every field, and a cap is a float. A hidden
        setting returns ``"set"``. A deployment, a role estimate or a tool holds the fields that
        differ from the file. With no such field, the row is removed and this returns ``{}``.

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
    if stored_value == {} and _stores_changed_fields(parameter):
        # Every sent field equals the file, so no override is needed (FRE-1733). The validators
        # refuse an empty value. An empty row reads as an override that holds nothing. This runs
        # outside the lock, which Clear takes.
        await clear_override(db, parameter, changed_by=changed_by, trace_id=trace_id)
        return stored_value
    hidden = _is_hidden_parameter(parameter)
    repo = SettingsOverridesRepository(db)
    async with _edit_lock:
        found = await repo.find(parameter)
        old_value = found[0] if found is not None else file_value(parameter)
        if hidden and found is not None:
            old_value = "not set" if found[0] is None else "set"
        await repo.set(
            parameter=parameter,
            value=stored_value,
            changed_by=changed_by,
            old_value=old_value,
            hidden=hidden,
        )
        replace_overrides({**current_overrides(), parameter: stored_value})
        await _after_edit(parameter, trace_id)
    returned: JsonValue = "set" if hidden else stored_value
    log.info(
        "setting_override_stored",
        parameter=parameter,
        old_value=old_value,
        new_value=returned,
        user_id=str(changed_by),
        trace_id=trace_id,
    )
    return returned


def _stores_changed_fields(parameter: str) -> bool:
    """Return whether a parameter stores only the fields that differ from the file (FRE-1733)."""
    return parameter.startswith(
        (DEPLOYMENT_PARAMETER_PREFIX, ROLE_ESTIMATE_PARAMETER_PREFIX, TOOL_PARAMETER_PREFIX)
    )


def _register_pricing(parameter: str, trace_id: str) -> None:
    """Register the price of each priced deployment again, after a deployment edit (FRE-1733).

    Budget counts read the litellm cost table. A failure logs an error and does not undo the edit.
    The next edit or the next start repairs the table. Registration only adds or overwrites. A price
    that an edit or a Clear removes, and the key of an old ``id`` or ``provider``, stay until a restart.
    """
    if not parameter.startswith(DEPLOYMENT_PARAMETER_PREFIX):
        return
    try:
        register_model_pricing(load_model_config())
    except Exception as exc:
        log.error(
            "model_pricing_registration_failed",
            parameter=parameter,
            error=str(exc),
            error_type=type(exc).__name__,
            trace_id=trace_id,
        )


async def _after_edit(parameter: str, trace_id: str) -> None:
    """Bring the process up to the committed edit. The table is already right."""
    if parameter.startswith(SETTING_PARAMETER_PREFIX):
        apply_setting_overrides(at_start=False)
        if parameter.removeprefix(SETTING_PARAMETER_PREFIX) in _ROUND_CEILING_FIELDS:
            # The cached presets depend on the ceiling, and their cache key does not hold it.
            forget_effort_overrides()
    _register_pricing(parameter, trace_id)
    await _mirror_caps(parameter, trace_id)


def _check_clear_keeps_the_ceiling(parameter: str) -> None:
    """Refuse a Clear that drops a ceiling below a preset, as an edit to that value is refused."""
    field = parameter.removeprefix(SETTING_PARAMETER_PREFIX)
    if not parameter.startswith(SETTING_PARAMETER_PREFIX) or field not in _ROUND_CEILING_FIELDS:
        return
    remaining = {name: value for name, value in usable_setting_rows().items() if name != field}
    try:
        check_round_ceilings(get_seshat_effort(), AppConfig(**remaining))  # type: ignore[arg-type]
    except (SeshatEffortConfigError, ValidationError) as exc:
        reason = (
            str(exc)
            if isinstance(exc, SeshatEffortConfigError)
            else _refusal_reason(parameter, exc, hidden=is_hidden_setting(field))
        )
        raise SettingEditRefused(reason) from None


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

    Raises:
        SettingEditRefused: With the reason, when a Clear leaves a preset above its ceiling.
    """
    repo = SettingsOverridesRepository(db)
    async with _edit_lock:
        _check_clear_keeps_the_ceiling(parameter)
        cleared = await repo.clear(
            parameter=parameter,
            changed_by=changed_by,
            new_value=file_value(parameter),
            hidden=_is_hidden_parameter(parameter),
        )
        remaining = {key: val for key, val in current_overrides().items() if key != parameter}
        replace_overrides(remaining)
        if cleared:
            await _after_edit(parameter, trace_id)
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
    apply_setting_overrides(at_start=True)
    log.info("setting_overrides_loaded", parameters=sorted(overrides), trace_id=trace_id)
    _warn_if_a_preset_exceeds_a_stored_ceiling(trace_id)


def _warn_if_a_preset_exceeds_a_stored_ceiling(trace_id: str) -> None:
    """Log a stored ceiling that a preset exceeds. An edit refuses this, so a later file change did it.

    The fatal check of the effort file runs before the overrides load. This one must not stop the
    start, because the menu then cannot undo the row.
    """
    try:
        check_round_ceilings(get_seshat_effort(), get_settings())
    except SeshatEffortConfigError as exc:
        log.error("effort_round_ceiling_conflict", error=str(exc), trace_id=trace_id)
