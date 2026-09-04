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

Three ways to build a :class:`PromptIdentity`:

* :func:`derive_prompt_identity` — the low-level primitive: hashes exactly the two
  strings it's given. Callers that already have a real static/dynamic split
  (``gateway.chat``'s single fixed persona) call it directly.
* :func:`derive_orchestrator_prompt_identity` — used by ``orchestrator.primary``.
  Hashes the actual ``request_messages``/``tools`` handed to
  :meth:`LLMClient.respond`, not an intermediate candidate string — see FRE-1008.
* :func:`derive_fallback_prompt_identity` — used by ``LiteLLMClient`` when a
  caller passes no explicit ``prompt_identity`` (every
  non-taxonomy callsite: captains_log, second_brain, memory paraphrasing,
  sub-agents, etc.). Resolves the effective system content (``system_prompt``, or
  an embedded system-role message) as the static prefix, and the rest of the
  request as the dynamic tail.

FRE-1008 fixed a defect where both ``orchestrator.primary`` and the two fallback
callsites computed ``static_prefix_hash``/``dynamic_hash`` from the same input,
making them structurally incapable of differing (ADR-0078 D4's cache-erosion
signal was inert). Under ADR-0081's volatility-gradient layout, per-turn dynamic
content is inlined into the current user turn rather than spliced into
``system_prompt`` — ``dynamic_hash`` must follow it there.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from personal_agent.llm_client.message_content import get_text_content


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
    "grounding_contract",
    "tool_awareness",
    "deployment_context",
    "operator_stanza",
    "skill_index",
    "skill_bodies",
    "memory_section",
    "salient_highlights",
    "artifact_builder_planning_note",
    "current_datetime",
    "tool_use_rules",
    "decomposition_instructions",
)
"""Registry of prompt component IDs for callsite ``orchestrator.primary``.

Single source of truth shared by the prompt-manifest builder (FRE-409) and the
prompt-composition insights detector. When the executor gains or removes a
component, update both the executor append-block and this tuple; the sync-guard
test (``tests/personal_agent/llm_client/test_prompt_identity_taxonomy.py``) will
catch drift.

Tuple order mirrors the sequence ``executor.py`` evaluates components for
inclusion in ``_component_ids`` (its append order) — **not** necessarily the
final byte-assembly order of the composed ``system_prompt`` string (e.g. the
STATIC tool-use rules are assembled *before* the SEMI-STATIC tool-awareness
block, but appear after it here). ``component_ids`` is a descriptive audit
field only; it feeds neither ``static_prefix_hash`` nor ``dynamic_hash``
(FRE-1008 confirmed the static/dynamic boundary itself is captured by direct
code position in ``executor.py``, independent of this tuple's order).
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


def _serialize_dynamic_content(
    request_messages: Sequence[Mapping[str, Any]],
    tools: Sequence[Mapping[str, Any]] | None = None,
) -> str:
    """Deterministic, structure-preserving serialization of an actual wire request.

    Used as the ``dynamic_hash`` input for every callsite with no
    component-aware split: ``orchestrator.primary``'s per-turn tail (FRE-1008)
    and every fallback callsite (:func:`derive_fallback_prompt_identity`). A
    naive join of message text would let two structurally different requests
    collide on identical concatenated text — role tags and a per-block
    fingerprint (ordered, not deduplicated, so a changed/added non-text block
    like an image attachment moves the hash even when no text differs) make
    the serialization sensitive to structure, not just text. Tool schemas are
    included when present: a callsite that starts/stops sending tools, or
    changes a tool's schema, must move ``dynamic_hash``.

    Args:
        request_messages: The message list actually sent to the model.
        tools: The tool definitions actually sent, if any.

    Returns:
        A deterministic string encoding role, content-block shape, text, tool
        calls, and tool definitions.
    """
    parts: list[str] = []
    for m in request_messages:
        role = m.get("role", "")
        content = m.get("content")
        block_descriptors = ""
        if isinstance(content, list):
            # Ordered, not deduplicated: a naive `{b.get("type") for b in content}`
            # set collapses both count and content of same-typed non-text blocks
            # (e.g. two different image_url blocks both just say "image_url" once),
            # leaving dynamic_hash blind to a turn that genuinely added/changed
            # attachments (ADR-0101). Non-text blocks are fingerprinted by hashing
            # the whole block (get_text_content only extracts "text"-type content).
            descriptors = []
            for b in content:
                if not isinstance(b, dict):
                    continue
                btype = b.get("type", "")
                descriptors.append(
                    "text"
                    if btype == "text"
                    else f"{btype}:{_short_hash(json.dumps(b, sort_keys=True, default=str))}"
                )
            block_descriptors = ",".join(descriptors)
        parts.append(f"[{role}|{block_descriptors}]{get_text_content(content)}")
        if m.get("tool_calls"):
            parts.append(f"[tool_calls]{json.dumps(m['tool_calls'], sort_keys=True, default=str)}")
    if tools:
        parts.append(f"[tools]{json.dumps(list(tools), sort_keys=True, default=str)}")
    return "\n".join(parts)


def derive_orchestrator_prompt_identity(
    *,
    static_prefix: str,
    request_messages: Sequence[Mapping[str, Any]],
    tools: Sequence[Mapping[str, Any]] | None = None,
    component_ids: tuple[str, ...] = (),
) -> PromptIdentity:
    """Build the ``orchestrator.primary`` :class:`PromptIdentity` (FRE-1008).

    ``static_prefix`` is ``inner_system_before_memory`` — captured before any
    VOLATILE content is assembled. ``dynamic_hash`` must cover what is
    actually sent: ``request_messages`` (which, under ADR-0081's
    volatility-gradient layout, carries the per-turn volatile tail inlined
    into the current user turn) plus ``tools`` — not a precomputed candidate
    block, since the inline step can no-op (e.g. no eligible target message)
    and diverge from what a precomputed string would represent. Call with the
    same ``request_messages``/``tools`` values passed to
    :meth:`LLMClient.respond` on this call.

    Args:
        static_prefix: The cacheable prefix (``inner_system_before_memory``).
        request_messages: The message list actually sent to the model.
        tools: The tool definitions actually sent, if any.
        component_ids: Ordered component IDs included on this call.

    Returns:
        A frozen :class:`PromptIdentity`.
    """
    full_prompt = f"{static_prefix}\n\n{_serialize_dynamic_content(request_messages, tools)}"
    return derive_prompt_identity(
        "orchestrator.primary",
        static_prefix=static_prefix,
        full_prompt=full_prompt,
        component_ids=component_ids,
    )


def _first_system_message_text(request_messages: Sequence[Mapping[str, Any]]) -> str:
    """Return the text content of the first system-role message, or ``""``."""
    for m in request_messages:
        if m.get("role") == "system":
            return get_text_content(m.get("content"))
    return ""


def derive_fallback_prompt_identity(
    callsite: str,
    *,
    system_prompt: str | None,
    request_messages: Sequence[Mapping[str, Any]],
    tools: Sequence[Mapping[str, Any]] | None = None,
) -> PromptIdentity:
    """Build a :class:`PromptIdentity` for call sites with no component-aware split.

    Used by ``LiteLLMClient`` when the caller passes no explicit
    ``prompt_identity`` — every callsite except ``orchestrator.primary``
    and ``gateway.chat`` (FRE-1008). ``static_prefix`` is the effective system
    content: ``system_prompt`` if given, else the first embedded system-role
    message (fixes the empty-string collapse seen live when a caller folds
    system content into ``messages`` instead of the ``system_prompt``
    parameter). ``dynamic_hash`` covers that plus every non-system message and
    any tools actually sent — the real per-call variation these single-persona
    call sites do carry, even though the persona itself is typically fixed.

    Args:
        callsite: Symbolic call-site name, e.g. ``f"role.{role.value}"``.
        system_prompt: The caller's ``system_prompt`` argument, if any.
        request_messages: The message list actually sent to the model.
        tools: The tool definitions actually sent, if any.

    Returns:
        A frozen :class:`PromptIdentity`.
    """
    static_content = system_prompt or _first_system_message_text(request_messages)
    non_system = [m for m in request_messages if m.get("role") != "system"]
    full_prompt = f"{static_content}\n\n{_serialize_dynamic_content(non_system, tools)}"
    return derive_prompt_identity(callsite, static_prefix=static_content, full_prompt=full_prompt)
