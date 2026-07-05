"""FRE-771 — the frozen V1 prompt snapshot's monkeypatch is exception-safe.

The powered-A/B driver runs two sequential phases against the shared `entity_extraction`
module global (`_EXTRACTION_PROMPT_TEMPLATE`) — never concurrently (FRE-771 plan § D2).
This confirms the patch always restores the original (production V2) template, even when
the patched block raises, so a failed V1-phase run can never leave the live extractor
silently pointed at the retired prompt. Isolated from `fre771_powered_ab.py` (which
imports `harness.py`, requiring live cloud credentials at import time) so this stays
unit-testable in CI.
"""

from __future__ import annotations

import pytest
from scripts.eval.fre630_extraction_quality.fre771_v1_prompt_snapshot import (
    _V1_PROMPT_TEMPLATE_SNAPSHOT,
    v1_prompt_template_active,
)

from personal_agent.second_brain import entity_extraction


def test_v1_prompt_template_restored_after_normal_exit() -> None:
    """The monkeypatch activates V1 inside the block and restores V2 after it."""
    original = entity_extraction._EXTRACTION_PROMPT_TEMPLATE
    with v1_prompt_template_active():
        assert entity_extraction._EXTRACTION_PROMPT_TEMPLATE == _V1_PROMPT_TEMPLATE_SNAPSHOT
    assert entity_extraction._EXTRACTION_PROMPT_TEMPLATE is original


def test_v1_prompt_template_restored_after_exception() -> None:
    """A failure inside the patched block still restores the original template."""
    original = entity_extraction._EXTRACTION_PROMPT_TEMPLATE
    with pytest.raises(RuntimeError):
        with v1_prompt_template_active():
            assert entity_extraction._EXTRACTION_PROMPT_TEMPLATE == _V1_PROMPT_TEMPLATE_SNAPSHOT
            raise RuntimeError("simulated V1-phase failure")
    assert entity_extraction._EXTRACTION_PROMPT_TEMPLATE is original


def test_v1_snapshot_is_the_retired_seven_type_prompt() -> None:
    """The frozen snapshot names the retired V1 types, not the live V2 ones."""
    assert '"Technology"' in _V1_PROMPT_TEMPLATE_SNAPSHOT
    assert '"Concept"' in _V1_PROMPT_TEMPLATE_SNAPSHOT
    assert "MethodOrConcept" not in _V1_PROMPT_TEMPLATE_SNAPSHOT
    assert "KnowledgeArtifact" not in _V1_PROMPT_TEMPLATE_SNAPSHOT
