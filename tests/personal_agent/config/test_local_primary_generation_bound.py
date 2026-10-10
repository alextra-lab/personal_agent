"""FRE-1562 — every local deployment bounds its generation, from a measurement.

ADR-0141 D5 keeps omit-means-unbounded as the local default and says any future cap is a
deliberate catalog edit. This module pins that edit: the five local ``kind: llm`` entries in
``config/models.yaml`` declare a ``max_tokens`` that clears every normal call ever recorded,
and the cloud entries keep the values they had.

The measurement (AC-1), read-only against the VPS Elasticsearch on 2026-10-10:

    index:  agent-logs-*
    filter: event_type = model_call_completed, role = primary, provider = slm_local
            (``event_type``/``role``/``provider``/``model`` are keyword fields; a ``.keyword``
            sub-field returns zero hits)
    metric: max(output_tokens) — the stream's completion tokens, thinking included on llama.cpp

    last 30 days:        n = 604,   max = 8,654 (qwen3.8-flash-next, ended in a tool call)
    since 2026-07-30:    n = 1,236, max = 10,263 (qwen3.6-35-A3B, retired but still a catalog entry)

The event records only calls that finished, so both figures describe normal calls.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from personal_agent.config.model_loader import load_model_config, resolve_role_definition
from personal_agent.llm_client.models import Placement

_CATALOG = Path(__file__).resolve().parents[3] / "config" / "models.yaml"

#: Largest ``output_tokens`` of a completed local primary call, last 30 days.
OBSERVED_NORMAL_MAX_30D = 8654
#: Largest ``output_tokens`` of a completed local primary call, since 2026-07-30.
OBSERVED_NORMAL_MAX_ALL_TIME = 10263

_LOCAL_LLM_KEYS = (
    "qwen3.6-35b-thinking",
    "qwen3.8-flash-next",
    "qwen3.8-27b-mtplx",
    "qwen3.8-flash-next-mtplx",
    "gemma-4-26b-a4b",
)

#: ``max_tokens`` of the cloud ``kind: llm`` entries before FRE-1562 (AC-4: they do not change).
_CLOUD_MAX_TOKENS_BEFORE = {
    "qwen3.8-27b-ovh": 32768,
    "claude_sonnet": 128000,
    "claude_haiku": 64000,
    "gpt-5.4-mini": 8192,
}


@pytest.fixture(scope="module")
def catalog():  # type: ignore[no-untyped-def]
    return load_model_config(_CATALOG)


def test_the_five_local_llm_entries_are_the_whole_local_llm_set(catalog) -> None:  # type: ignore[no-untyped-def]
    """Guard the guard: a new local ``kind: llm`` entry must be added to this module's list."""
    local_llm = {
        key
        for key, definition in catalog.models.items()
        if catalog.placement_of(key) is Placement.LOCAL and definition.kind == "llm"
    }
    assert local_llm == set(_LOCAL_LLM_KEYS)


@pytest.mark.parametrize("key", _LOCAL_LLM_KEYS)
def test_every_local_llm_entry_declares_a_bound_above_every_normal_call(catalog, key: str) -> None:  # type: ignore[no-untyped-def]
    """AC-1: the bound is declared and sits above the observed maximum of normal turns."""
    bound = catalog.models[key].max_tokens
    assert bound is not None, f"{key} declares no max_tokens"
    assert bound > OBSERVED_NORMAL_MAX_ALL_TIME > OBSERVED_NORMAL_MAX_30D


def test_the_bound_is_the_same_on_every_local_entry(catalog) -> None:  # type: ignore[no-untyped-def]
    """One number, one rationale: a session that swaps deployments keeps the same ceiling."""
    assert {catalog.models[key].max_tokens for key in _LOCAL_LLM_KEYS} == {12288}


def test_the_primary_role_resolves_to_a_bounded_definition() -> None:
    """The PRIMARY role's effective definition carries the bound (what the client is built from)."""
    definition = resolve_role_definition("primary")
    assert definition is not None
    assert definition.max_tokens == 12288


def test_cloud_entries_keep_their_max_tokens(catalog) -> None:  # type: ignore[no-untyped-def]
    """AC-4: only local entries change."""
    assert {
        key: catalog.models[key].max_tokens for key in _CLOUD_MAX_TOKENS_BEFORE
    } == _CLOUD_MAX_TOKENS_BEFORE
