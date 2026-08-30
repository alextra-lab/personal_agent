"""The fixture dataset — loader + dataclass.

``expected_task_type`` is set only for fixtures whose deterministic classification is the
ground truth the fixture exists to exercise (the AC-5 seeded-agreement case, and the two
unambiguous-pattern cases); it is ``None`` for the disagreement case (the GPSR question)
where no answer is presupposed — that is the whole point of measuring it.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml  # type: ignore[import-untyped]

_DEFAULT_PATH = Path(__file__).parent / "fixtures.yaml"


@dataclass(frozen=True)
class Fixture:
    """One question to run through every arm.

    Attributes:
        label: Unique identifier (tag in the report).
        note: Why this fixture is in the set.
        message: The exact user message text.
        expected_task_type: The deterministic classifier's expected ``TaskType.value``,
            or ``None`` when the fixture deliberately presupposes no answer.
    """

    label: str
    note: str
    message: str
    expected_task_type: str | None


def load_fixtures(path: Path = _DEFAULT_PATH) -> list[Fixture]:
    """Load the fixture dataset from YAML.

    Args:
        path: Path to ``fixtures.yaml``.

    Returns:
        Ordered list of fixtures.

    Raises:
        ValueError: If the file has no ``fixtures`` list, or labels are not unique.
    """
    raw = yaml.safe_load(path.read_text())
    entries = raw.get("fixtures") if isinstance(raw, dict) else None
    if not entries:
        raise ValueError(f"No 'fixtures' found in {path}")
    fixtures = [
        Fixture(
            label=str(f["label"]),
            note=str(f.get("note", "")).strip(),
            message=str(f["message"]),
            expected_task_type=f.get("expected_task_type"),
        )
        for f in entries
    ]
    labels = [f.label for f in fixtures]
    if len(labels) != len(set(labels)):
        raise ValueError(f"Duplicate fixture labels in {path}: {labels}")
    return fixtures
