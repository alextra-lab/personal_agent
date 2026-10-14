"""Prompt-composition manifest for Captain's Log self-reflection (FRE-409).

Assembles a compact, human-readable 2-line manifest of the prompt composition
used on the *current* turn — from already-fetched trace events — so the reflection
model can notice composition problems (dead components, prefix instability).

FRE-1608: the per-callsite mean user rating was removed from the manifest. On the
0-3 scale 2,276 of 2,317 ratings are 2, so the mean carried no signal and every
reflection read "2.00/3" as low quality.

The manifest is injected as a ``prompt_manifest`` input field on
``GenerateReflection`` (``captains_log/reflection_dspy.py``) and mirrors the
content in the manual ``REFLECTION_PROMPT`` template.  It is built inside
``generate_reflection_entry`` (``captains_log/reflection.py``) right after
``get_trace_events`` so no extra query is needed to retrieve the identity data.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

_PRIMARY_CALLSITE = "orchestrator.primary"


def _extract_primary_model_call(
    trace_events: Sequence[Any],
) -> Mapping[str, Any] | None:
    """Return the best identity-bearing model_call_completed event for this turn.

    Prefers the ``orchestrator.primary`` callsite; falls back to the last
    ``model_call_completed`` event that carries ``prompt_static_prefix_hash``.

    Args:
        trace_events: Trace events for the current turn (from ``get_trace_events``).

    Returns:
        The chosen event mapping, or ``None`` when no identity-bearing event
        exists.
    """
    candidate: Mapping[str, Any] | None = None
    for raw_event in trace_events:
        try:
            event: Mapping[str, Any] = raw_event
            if not isinstance(event, Mapping):
                continue
            # Log-file events use "event"; ES-shaped events use "event_type".
            # Accept both so the manifest works whether fed from get_trace_events
            # (local log source, "event" key) or from ES-shaped callers.
            event_name = event.get("event") or event.get("event_type")
            if event_name != "model_call_completed":
                continue
            if not event.get("prompt_static_prefix_hash"):
                continue
        except Exception:  # noqa: BLE001  # pragma: nocover
            continue
        candidate = event  # last identity-bearing event as fallback
        if event.get("prompt_callsite") == _PRIMARY_CALLSITE:
            return event
    return candidate


def build_prompt_manifest(trace_events: Sequence[Any]) -> str:
    """Build the 2-line prompt-composition manifest for the current turn.

    Best-effort: returns ``"Prompt manifest: unavailable"`` when no
    identity-bearing model call is found.  Never raises.

    Args:
        trace_events: Trace events for the current turn (already fetched by
            the reflection caller via ``get_trace_events``).

    Returns:
        A newline-joined string with exactly 2 lines::

            Active prompt components: a, b, c
            Static prefix hash: <16-hex>

        When the call records no component ids, the first line reads
        ``Active prompt components: not recorded for callsite <callsite>``. Only
        the ``orchestrator.primary`` callsite composes named components. Every other
        callsite sends an empty list, which is "not recorded", not "no components".

        Returns ``"Prompt manifest: unavailable"`` when no identity-bearing
        event is present.
    """
    event = _extract_primary_model_call(trace_events)
    if event is None:
        return "Prompt manifest: unavailable"

    callsite = str(event.get("prompt_callsite") or "unknown")

    raw_components = event.get("prompt_component_ids")
    components: list[str] = []
    if isinstance(raw_components, list):
        components = [str(c) for c in raw_components if c]

    static_hash = str(event.get("prompt_static_prefix_hash") or "unknown")

    components_line = "Active prompt components: " + (
        ", ".join(components) if components else f"not recorded for callsite {callsite}"
    )
    hash_line = f"Static prefix hash: {static_hash}"

    return "\n".join([components_line, hash_line])
