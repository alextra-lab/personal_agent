"""Prompt-composition manifest for Captain's Log self-reflection (FRE-409).

Assembles a compact, human-readable 3-line manifest of the prompt composition
used on the *current* turn — from already-fetched trace events — plus a recent
quality signal for the turn's callsite, so the reflection model can notice
composition problems (dead components, prefix instability, rating regressions).

The manifest is injected as a ``prompt_manifest`` input field on
``GenerateReflection`` (``captains_log/reflection_dspy.py``) and mirrors the
content in the manual ``REFLECTION_PROMPT`` template.  It is built inside
``generate_reflection_entry`` (``captains_log/reflection.py``) right after
``get_trace_events`` so no extra query is needed to retrieve the identity data.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from personal_agent.telemetry import get_logger

log = get_logger(__name__)

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


def _format_quality_line(
    callsite: str,
    mean_rating_lookup: Mapping[str, tuple[float, int]] | None,
) -> str:
    """Format the recent-quality-signal line for a callsite.

    Args:
        callsite: The current turn's prompt callsite name.
        mean_rating_lookup: Optional callsite → (mean, n) map; ``None`` or
            missing key renders as "no recent ratings".

    Returns:
        A one-line quality signal string starting with
        ``"Recent quality signal: "``.
    """
    if mean_rating_lookup is not None and callsite in mean_rating_lookup:
        mean_rating, sample_n = mean_rating_lookup[callsite]
        return (
            f"Recent quality signal: {callsite} mean rating = {mean_rating:.2f} "
            f"(last 7 days, n={sample_n})"
        )
    return f"Recent quality signal: {callsite} no recent ratings (last 7 days)"


def build_prompt_manifest(
    trace_events: Sequence[Any],
    *,
    mean_rating_lookup: Mapping[str, tuple[float, int]] | None = None,
) -> str:
    """Build the 3-line prompt-composition manifest for the current turn.

    Best-effort: returns ``"Prompt manifest: unavailable"`` when no
    identity-bearing model call is found.  Never raises.

    Args:
        trace_events: Trace events for the current turn (already fetched by
            the reflection caller via ``get_trace_events``).
        mean_rating_lookup: Optional map of ``prompt_callsite`` →
            ``(mean_rating, n)`` over the trailing window. When absent or
            missing the callsite, the quality line reports
            "no recent ratings".

    Returns:
        A newline-joined string with exactly 3 lines::

            Active prompt components: a, b, c
            Static prefix hash: <16-hex>
            Recent quality signal: <callsite> mean rating = X.XX (last 7 days, n=Y)

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
        ", ".join(components) if components else "(none)"
    )
    hash_line = f"Static prefix hash: {static_hash}"
    quality_line = _format_quality_line(callsite, mean_rating_lookup)

    return "\n".join([components_line, hash_line, quality_line])


async def load_mean_rating_lookup(
    *,
    days: int = 7,
    trace_id: str = "",
) -> dict[str, tuple[float, int]]:
    """Load the per-callsite mean-rating lookup map (best-effort, never raises).

    Wraps ``TelemetryQueries.get_mean_rating_by_callsite`` so the reflection
    caller stays ES-client-free and testable with a plain dict.

    Args:
        days: Lookback window in days.
        trace_id: Trace ID for log correlation.

    Returns:
        A mapping of ``prompt_callsite`` → ``(mean_rating, sample_count)``.
        Returns an empty dict on any failure.
    """
    try:
        # Local import to avoid circular import; TelemetryQueries is a heavy dep.
        from personal_agent.telemetry import TelemetryQueries

        return await TelemetryQueries().get_mean_rating_by_callsite(days=days)
    except Exception:
        log.warning(
            "mean_rating_lookup_unavailable",
            trace_id=trace_id,
            component="prompt_manifest",
            exc_info=True,
        )
        return {}
