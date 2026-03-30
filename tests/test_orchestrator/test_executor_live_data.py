"""Regression tests for live-data tool routing heuristics.

Skipped: `_requires_live_data_lookup` was removed from `executor.py`; routing now
uses gateway + tools. Delete this module when live-data heuristics are re-specified.
"""

from __future__ import annotations

import pytest

pytest.skip(
    "Obsolete: _requires_live_data_lookup no longer exists on executor",
    allow_module_level=True,
)
