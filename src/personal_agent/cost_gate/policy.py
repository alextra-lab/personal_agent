"""Loader for ``config/governance/budget.yaml`` (ADR-0065 / FRE-304).

Mirrors the pattern in ``personal_agent.config.governance_loader``: read the
YAML file, validate against the Pydantic schema in ``types.py``, return a
frozen ``BudgetConfig`` the gate can consult on every reservation.

The YAML is the canonical source for v1. The ``budget_policies`` table
exists for audit and v2 per-user / per-provider extensions; the gate falls
back to it if the YAML is missing, but normal startup expects a YAML present.

A cap that the owner stored in the settings menu wins over the YAML (FRE-1723). The stored rows live
in ``settings_overrides``, and :func:`apply_cap_overrides` applies them.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from decimal import Decimal
from pathlib import Path

import asyncpg  # type: ignore[import-untyped]
import structlog
from pydantic import JsonValue, ValidationError

from personal_agent.config.loader import ConfigLoadError, load_yaml_file
from personal_agent.config.setting_overrides import (
    CAP_PARAMETER_PREFIX,
    ROLE_ESTIMATE_PARAMETER_PREFIX,
)
from personal_agent.cost_gate.types import BudgetConfig, CapEntry, RoleConfig

log = structlog.get_logger(__name__)


class BudgetConfigError(ConfigLoadError):
    """Raised when ``budget.yaml`` cannot be loaded or validated."""

    pass


def load_budget_config(path: Path | str | None = None) -> BudgetConfig:
    """Load and validate the budget config.

    Args:
        path: Path to ``budget.yaml``. If ``None``, derives the path from
            ``settings.governance_config_path / "budget.yaml"`` so operators
            can keep it alongside the other governance YAMLs.

    Returns:
        Frozen ``BudgetConfig`` ready for cap lookups.

    Raises:
        BudgetConfigError: If the file is missing, malformed, or fails
            schema validation. The error message includes per-field
            validation details to make YAML mistakes obvious in dev.
    """
    if path is None:
        from personal_agent.config import settings  # noqa: PLC0415 — lazy to avoid cycle

        gov_dir = settings.governance_config_path
        if not gov_dir.is_absolute():
            project_root = Path(__file__).parent.parent.parent.parent
            gov_dir = (project_root / gov_dir).resolve()
        path = gov_dir / "budget.yaml"

    path = Path(path)
    if not path.exists():
        raise BudgetConfigError(f"Budget config not found: {path}")

    log.info("loading_budget_config", path=str(path))

    try:
        data = load_yaml_file(path, error_class=BudgetConfigError)
    except ConfigLoadError as e:
        raise BudgetConfigError(f"Failed to read budget config {path}: {e}") from None

    try:
        config = BudgetConfig.model_validate(data)
    except ValidationError as e:
        details = "\n".join(
            f"{' -> '.join(str(loc) for loc in err['loc'])}: {err['msg']}" for err in e.errors()
        )
        raise BudgetConfigError(f"Budget config validation failed at {path}:\n{details}") from None

    log.info(
        "budget_config_loaded",
        path=str(path),
        roles=len(config.roles),
        caps=len(config.caps),
        version=config.version,
    )
    return config


def cap_parameter(cap: CapEntry) -> str:
    """Return the settings menu row name of a cap.

    Args:
        cap: A cap of the budget config.

    Returns:
        ``cap.<time_window>.<role>``.
    """
    return f"{CAP_PARAMETER_PREFIX}{cap.time_window}.{cap.role}"


def _cap_with_value(cap: CapEntry, value: JsonValue) -> CapEntry:
    """Return a cap with a stored value, checked by the model that loads the file."""
    parameter = cap_parameter(cap)
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise BudgetConfigError(f"{parameter} takes a number of US dollars")
    try:
        return CapEntry(time_window=cap.time_window, role=cap.role, cap_usd=Decimal(str(value)))
    except ValidationError as exc:
        reasons = "; ".join(err["msg"] for err in exc.errors())
        raise BudgetConfigError(f"{parameter}: {reasons}") from None


def validate_cap_override(config: BudgetConfig, parameter: str, value: JsonValue) -> float:
    """Check one proposed cap edit (FRE-1723). An edit changes a cap, and never adds one.

    Args:
        config: The budget config as the file states it.
        parameter: The settings menu row name.
        value: The proposed value.

    Returns:
        The value to store, as a JSON number.

    Raises:
        BudgetConfigError: With the reason, when the value is refused.
    """
    for cap in config.caps:
        if cap_parameter(cap) == parameter:
            return float(_cap_with_value(cap, value).cap_usd)
    raise BudgetConfigError(f"{parameter} is not a cap in the budget file")


def apply_cap_overrides(config: BudgetConfig, overrides: Mapping[str, JsonValue]) -> BudgetConfig:
    """Return the config with the stored caps applied (FRE-1723).

    A stored cap that fails is skipped, and the file cap applies. Every paid reservation reads this,
    so a stored row must never make it raise. The settings menu shows a skipped override as not
    usable. A stored cap that the file no longer holds has no effect.

    Args:
        config: The budget config as the file states it.
        overrides: Every stored override, keyed by parameter name.

    Returns:
        The same object when no stored cap applies, else a copy with the new caps.
    """
    caps: list[CapEntry] = []
    for cap in config.caps:
        parameter = cap_parameter(cap)
        if parameter not in overrides:
            caps.append(cap)
            continue
        try:
            caps.append(_cap_with_value(cap, overrides[parameter]))
        except BudgetConfigError as exc:
            log.warning("cap_override_skipped", parameter=parameter, error=str(exc))
            caps.append(cap)
    if all(new is old for new, old in zip(caps, config.caps, strict=True)):
        return config
    return config.model_copy(update={"caps": caps})


def _role_with_value(role: str, config: RoleConfig, value: JsonValue) -> RoleConfig:
    """Return a role estimate with stored fields, checked by the model that loads the file."""
    if not isinstance(value, dict):
        raise BudgetConfigError(f"{ROLE_ESTIMATE_PARAMETER_PREFIX}{role} takes an object")
    unknown = sorted(set(value) - set(RoleConfig.model_fields))
    if unknown:
        raise BudgetConfigError(f"unknown fields: {', '.join(unknown)}")
    merged = {**config.model_dump(mode="json"), **value}
    try:
        return RoleConfig.model_validate_json(json.dumps(merged), strict=True)
    except ValidationError as exc:
        reasons = "; ".join(
            f"{'.'.join(str(loc) for loc in err['loc'])}: {err['msg']}"
            for err in exc.errors(include_input=False, include_url=False)
        )
        raise BudgetConfigError(f"{ROLE_ESTIMATE_PARAMETER_PREFIX}{role}: {reasons}") from None


def validate_role_estimate_override(
    config: BudgetConfig, parameter: str, value: JsonValue
) -> JsonValue:
    """Check one proposed role estimate edit (FRE-1724). An edit never adds a role.

    Args:
        config: The budget config as the file states it.
        parameter: The settings menu row name.
        value: The proposed fields.

    Returns:
        The fields that differ from the file. A later file change to any other field takes effect
        (FRE-1733). The result is empty when no field differs.

    Raises:
        BudgetConfigError: With the reason, when the value is refused.
    """
    role = parameter.removeprefix(ROLE_ESTIMATE_PARAMETER_PREFIX)
    if role not in config.roles:
        raise BudgetConfigError(f"the role {role} is not in the budget file")
    edited = _role_with_value(role, config.roles[role], value)
    dump = edited.model_dump(mode="json")
    assert isinstance(value, dict)
    return {
        field: dump[field]
        for field in sorted(value)
        if getattr(edited, field) != getattr(config.roles[role], field)
    }


def apply_budget_overrides(
    config: BudgetConfig, overrides: Mapping[str, JsonValue]
) -> BudgetConfig:
    """Return the config with the stored caps and role estimates applied (FRE-1724).

    A stored row that fails is skipped with a warning, and the file value applies. Every paid
    reservation reads this, so a stored row must never make it raise.

    Args:
        config: The budget config as the file states it.
        overrides: Every stored override, keyed by parameter name.

    Returns:
        The same object when no stored row applies, else a copy.
    """
    config = apply_cap_overrides(config, overrides)
    roles = dict(config.roles)
    changed = False
    for parameter, value in overrides.items():
        role = parameter.removeprefix(ROLE_ESTIMATE_PARAMETER_PREFIX)
        if not parameter.startswith(ROLE_ESTIMATE_PARAMETER_PREFIX) or role not in roles:
            continue
        try:
            roles[role] = _role_with_value(role, roles[role], value)
            changed = True
        except BudgetConfigError as exc:
            log.warning("role_estimate_override_skipped", parameter=parameter, error=str(exc))
    return config.model_copy(update={"roles": roles}) if changed else config


async def read_stored_caps(pool: asyncpg.Pool) -> dict[str, JsonValue]:
    """Return the stored caps of ``settings_overrides``, keyed by parameter name (FRE-1723).

    Args:
        pool: An open asyncpg pool (the caller's ``CostGate.pool``).

    Returns:
        Every ``cap.*`` row.

    Raises:
        asyncpg.PostgresError: On a connection failure or a missing table.
    """
    rows = await pool.fetch(
        "SELECT parameter, value::text AS value FROM settings_overrides WHERE parameter LIKE $1",
        f"{CAP_PARAMETER_PREFIX}%",
    )
    return {str(row["parameter"]): json.loads(row["value"]) for row in rows}


async def sync_budget_policies_to_db(config: BudgetConfig, pool: asyncpg.Pool) -> None:
    """Mirror the caps that the gate enforces into ``budget_policies`` (v1 scope only).

    ``budget_policies`` exists for audit and v2 per-user/per-provider extensions
    (module docstring above), but nothing wrote to it before this function —
    it sat permanently empty, which defeats any dashboard panel joining
    against it (FRE-1209). Call at app startup, after the config that drives
    enforcement is loaded and validated, and after a cap edit in the settings
    menu (FRE-1723). The gate itself does not consult this table, so a failure
    here cannot affect enforcement.

    A full replace inside one transaction, not a merge: a cap removed from
    YAML must not linger in the DB as a stale, unenforced row that a human
    reading the audit table would mistake for still active.

    Args:
        config: The loaded, validated ``BudgetConfig``, with the stored caps
            applied (``CostGate.config``).
        pool: An open asyncpg pool (the caller's ``CostGate.pool``).

    Raises:
        asyncpg.PostgresError: On a connection failure or a constraint
            violation (e.g. a duplicate ``(time_window, role)`` pair in
            ``budget.yaml``). Each caller wraps this call fail-open and does
            not propagate it.
    """
    async with pool.acquire() as conn, conn.transaction():
        await conn.execute("DELETE FROM budget_policies WHERE user_id IS NULL AND provider IS NULL")
        await conn.executemany(
            """
            INSERT INTO budget_policies (time_window, role, cap_usd)
            VALUES ($1, $2, $3)
            """,
            [(cap.time_window, cap.role, cap.cap_usd) for cap in config.caps],
        )
    log.info("budget_policies_synced", caps=len(config.caps))
