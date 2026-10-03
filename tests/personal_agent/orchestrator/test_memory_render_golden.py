"""Byte-identity guard for the memory renderer (FRE-1471).

The renderer's selection moved into a shared function so the planner digest can reuse it.
The golden file was recorded from the renderer **before** that move, over every item kind,
the render caps, blank items, the legacy undeclared shapes, a deictic entity and an episode
with ``key_entities`` — with and without a source registry. The renderer must still emit
exactly these bytes, the same rendered identities and the same render report.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from personal_agent.grounding.source_registry import SourceRegistry
from personal_agent.orchestrator.executor import _render_memory_section_with_ids

_GOLDEN: dict[str, Any] = json.loads(
    (Path(__file__).parent / "golden" / "memory_render_golden.json").read_text(encoding="utf-8")
)


def _render(items: list[dict[str, Any]], with_registry: bool) -> dict[str, Any]:
    registry = SourceRegistry(turn_id="golden-turn") if with_registry else None
    text, ids, report = _render_memory_section_with_ids(items, registry)
    return {
        "text": text,
        "ids": list(ids),
        "recall_emitted": report.recall_emitted,
        "cause": report.cause,
    }


@pytest.mark.parametrize("case", sorted(_GOLDEN))
@pytest.mark.parametrize("variant", ["plain", "registry"])
def test_renderer_output_is_byte_identical_to_the_recorded_golden(case: str, variant: str) -> None:
    recorded = _GOLDEN[case]

    actual = _render(recorded["items"], with_registry=variant == "registry")

    assert actual == recorded[variant]
