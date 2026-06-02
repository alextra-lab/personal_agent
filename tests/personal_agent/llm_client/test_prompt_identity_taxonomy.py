"""Sync-guard: PROMPT_COMPONENT_TAXONOMY must stay consistent with executor.py (FRE-409)."""

from __future__ import annotations

import ast
import re
from pathlib import Path


class TestPromptComponentTaxonomy:
    """PROMPT_COMPONENT_TAXONOMY is the single source of truth for component IDs.

    The executor hard-codes component IDs in _component_ids appends at
    executor.py:2521-2538.  This test asserts they all appear in the taxonomy,
    catching drift when the executor gains a new component but the constant is
    not updated.
    """

    def test_taxonomy_is_non_empty_tuple(self) -> None:
        from personal_agent.llm_client.prompt_identity import PROMPT_COMPONENT_TAXONOMY

        assert isinstance(PROMPT_COMPONENT_TAXONOMY, tuple)
        assert len(PROMPT_COMPONENT_TAXONOMY) > 0

    def test_taxonomy_contains_all_executor_component_ids(self) -> None:
        """Every component_id appended in executor._component_ids must be in the taxonomy."""
        from personal_agent.llm_client.prompt_identity import PROMPT_COMPONENT_TAXONOMY

        # These are the 8 component IDs appended in executor.py:2521-2538.
        # Update this list when the executor gains a new component.
        executor_component_ids = {
            "tool_awareness",
            "deployment_context",
            "operator_stanza",
            "skill_index",
            "skill_bodies",
            "memory_section",
            "tool_use_rules",
            "decomposition_instructions",
        }
        taxonomy_set = set(PROMPT_COMPONENT_TAXONOMY)
        missing = executor_component_ids - taxonomy_set
        assert missing == set(), (
            f"executor.py component IDs not in PROMPT_COMPONENT_TAXONOMY: {missing}"
        )

    def test_all_taxonomy_entries_are_strings(self) -> None:
        from personal_agent.llm_client.prompt_identity import PROMPT_COMPONENT_TAXONOMY

        for entry in PROMPT_COMPONENT_TAXONOMY:
            assert isinstance(entry, str), f"Non-string entry in taxonomy: {entry!r}"
            assert entry, "Empty string in taxonomy"

    def test_executor_source_contains_expected_appends(self) -> None:
        """Smoke-test: executor source file contains the expected _component_ids.append calls."""
        executor_path = (
            Path(__file__).parent.parent.parent.parent
            / "src"
            / "personal_agent"
            / "orchestrator"
            / "executor.py"
        )
        source = executor_path.read_text()
        for component_id in (
            "tool_awareness",
            "deployment_context",
            "operator_stanza",
            "skill_index",
            "skill_bodies",
            "memory_section",
            "tool_use_rules",
            "decomposition_instructions",
        ):
            assert f'_component_ids.append("{component_id}")' in source, (
                f"executor.py no longer appends '{component_id}'; "
                "update either executor.py or PROMPT_COMPONENT_TAXONOMY"
            )
