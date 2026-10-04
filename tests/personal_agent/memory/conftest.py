"""Shared fixtures for the memory tests."""

from __future__ import annotations

import pytest

import personal_agent.memory.proactive as proactive_mod


@pytest.fixture
def proactive_rerank_gate_off(monkeypatch: pytest.MonkeyPatch) -> None:
    """Unarm the proactive reranker bound (FRE-1545) for tests of the path's other gates.

    Those tests call ``build_proactive_suggestions`` with no reranker mapping. With the bound
    armed, the fail-safe default reports every zero-evidence candidate as UNAVAILABLE, which
    would make each of them a test of the reranker gate instead of the gate it names. Applied
    explicitly per module (``pytestmark``), never autouse, so the reranker gate's own tests
    run against the deployed configuration.
    """
    monkeypatch.setattr(proactive_mod.settings, "proactive_memory_rerank_relevance_bound", None)
