"""Prompt identity primitive (ADR-0078 D1/D4, FRE-405).

A :class:`PromptIdentity` names *what was sent* on a model call so telemetry can
attribute cost, cache behaviour, and quality to a specific prompt composition.
It is stamped onto every ``model_call_completed`` event via
:func:`personal_agent.llm_client.telemetry.emit_model_call_completed`.

The two hashes serve different purposes:

* ``static_prefix_hash`` — the *cacheable prefix*: the assembled bytes up to the
  first DYNAMIC component (the per-turn memory section). It is stable across turns
  when only memory changes, and shifts when STATIC/SEMI_STATIC prefix content
  changes — making KV-cache erosion measurable.
* ``dynamic_hash`` — the full assembled prompt across all tiers.

This module is intentionally distinct from
:func:`personal_agent.orchestrator.context_window.compute_prefix_hash`, which
guards a separate, tested invariant (head/system message preserved byte-identical
across compression and truncation). See ADR-0078 D4 (revised 2026-05-29).
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field


@dataclass(frozen=True)
class PromptIdentity:
    """Identity of a prompt sent on a single model call.

    Attributes:
        callsite: Symbolic name for the call site (e.g. ``"orchestrator.primary"``,
            ``"gateway.chat"``). See spec §2.1 for the registry.
        component_ids: Ordered tuple of component IDs assembled for this call.
            Empty for call sites without a named composition.
        static_prefix_hash: SHA-256 (16 hex chars) of the cacheable static prefix.
        dynamic_hash: SHA-256 (16 hex chars) of the full assembled prompt.
    """

    callsite: str
    static_prefix_hash: str
    dynamic_hash: str
    component_ids: tuple[str, ...] = field(default_factory=tuple)


PROMPT_COMPONENT_TAXONOMY: tuple[str, ...] = (
    "tool_awareness",
    "deployment_context",
    "operator_stanza",
    "skill_index",
    "skill_bodies",
    "memory_section",
    "artifact_builder_planning_note",
    "tool_use_rules",
    "decomposition_instructions",
)
"""Ordered registry of prompt component IDs for callsite ``orchestrator.primary``.

Single source of truth shared by the prompt-manifest builder (FRE-409) and the
prompt-composition insights detector. Order mirrors assembly order in
``orchestrator/executor.py``.  When the executor gains or removes a component,
update both the executor append-block and this tuple; the sync-guard test
(``tests/personal_agent/llm_client/test_prompt_identity_taxonomy.py``) will
catch drift.
"""


def _short_hash(text: str) -> str:
    """Return the first 16 hex chars of the SHA-256 digest of ``text``.

    Args:
        text: Arbitrary input string (may be empty).

    Returns:
        16-character lowercase hexadecimal string.
    """
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def derive_prompt_identity(
    callsite: str,
    *,
    static_prefix: str,
    full_prompt: str,
    component_ids: tuple[str, ...] = (),
) -> PromptIdentity:
    """Build a :class:`PromptIdentity` from the assembled prompt fragments.

    Args:
        callsite: Symbolic call-site name (spec §2.1).
        static_prefix: The cacheable prefix — assembled bytes up to the first
            DYNAMIC component. For call sites without a static/dynamic split, pass
            the full system prompt here as well.
        full_prompt: The complete assembled prompt (all tiers).
        component_ids: Ordered component IDs included on this call.

    Returns:
        A frozen :class:`PromptIdentity` with both hashes computed.
    """
    return PromptIdentity(
        callsite=callsite,
        static_prefix_hash=_short_hash(static_prefix),
        dynamic_hash=_short_hash(full_prompt),
        component_ids=component_ids,
    )
