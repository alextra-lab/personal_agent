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
