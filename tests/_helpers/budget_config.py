"""Budget-config loading for tests, tolerant of the gitignored real file.

The real ``config/governance/budget.yaml`` is gitignored (owner ruling
2026-08-09, FRE-1209): operational spend ceilings are deployment config, not
source, and this repo is public. A developer machine and the VPS both have the
real file; a fresh clone and CI have only ``budget.yaml.example``.

:func:`load_budget_config_for_tests` prefers the real file and falls back to the
template, so structural assertions (roles agree with ``ModelRole`` and the
cost-gate role map) run everywhere and are checked against the *deployed* caps
wherever those exist. Only the dollar amounts differ between the two files, and
no structural test reads them.
"""

from __future__ import annotations

from pathlib import Path

from personal_agent.cost_gate import load_budget_config
from personal_agent.cost_gate.types import BudgetConfig


def budget_config_path() -> Path:
    """Return the budget config path to use in tests.

    Returns:
        The real ``config/governance/budget.yaml`` when present, else the
        committed ``budget.yaml.example`` template.
    """
    governance = Path(__file__).resolve().parents[2] / "config" / "governance"
    real = governance / "budget.yaml"
    return real if real.exists() else governance / "budget.yaml.example"


def load_budget_config_for_tests() -> BudgetConfig:
    """Load the budget config, preferring the real file over the template.

    Returns:
        Validated ``BudgetConfig`` from whichever file :func:`budget_config_path`
        selected.

    Raises:
        BudgetConfigError: If the selected file is missing or fails validation.
    """
    return load_budget_config(budget_config_path())
