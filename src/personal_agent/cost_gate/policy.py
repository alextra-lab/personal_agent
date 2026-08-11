"""Loader for ``config/governance/budget.yaml`` (ADR-0065 / FRE-304).

Mirrors the pattern in ``personal_agent.config.governance_loader``: read the
YAML file, validate against the Pydantic schema in ``types.py``, return a
frozen ``BudgetConfig`` the gate can consult on every reservation.

The YAML is the canonical source for v1. The ``budget_policies`` table
exists for audit and v2 per-user / per-provider extensions; the gate falls
back to it if the YAML is missing, but normal startup expects a YAML present.
"""

from __future__ import annotations

from pathlib import Path

import asyncpg  # type: ignore[import-untyped]
import structlog
from pydantic import ValidationError

from personal_agent.config.loader import ConfigLoadError, load_yaml_file
from personal_agent.cost_gate.types import BudgetConfig

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


async def sync_budget_policies_to_db(config: BudgetConfig, pool: asyncpg.Pool) -> None:
    """Mirror ``budget.yaml``'s caps into ``budget_policies`` (v1 scope only).

    ``budget_policies`` exists for audit and v2 per-user/per-provider extensions
    (module docstring above), but nothing wrote to it before this function —
    it sat permanently empty, which defeats any dashboard panel joining
    against it (FRE-1209). Call once at app startup, after the config that
    drives enforcement is loaded and validated; the gate itself keeps reading
    the YAML directly and does not consult this table, so a failure here
    cannot affect enforcement.

    A full replace inside one transaction, not a merge: a cap removed from
    YAML must not linger in the DB as a stale, unenforced row that a human
    reading the audit table would mistake for still active.

    Args:
        config: The loaded, validated ``BudgetConfig``.
        pool: An open asyncpg pool (the caller's ``CostGate.pool``).
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
