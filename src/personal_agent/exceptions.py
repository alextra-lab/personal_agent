"""Central exception types for the personal_agent package.

CLAUDE.md instructs contributors to raise errors from
``personal_agent.exceptions`` rather than ``ValueError`` / bare ``except:``.
This module exists to satisfy that contract; for now it carries the small
set of errors introduced by ADR-0074 (FRE-376) and stays intentionally
minimal — additional exceptions can be migrated here in their own changes.
"""

from __future__ import annotations


class MissingIdentityError(ValueError):
    """Raised when an event sink is asked to write a row without identity.

    ADR-0074 (FRE-376) makes ``(trace_id, session_id)`` a hard precondition
    on every observability write so cost rows, log lines, and graph nodes
    can be joined to the session and request that produced them. Sinks like
    ``CostTracker.record_api_call`` raise this rather than silently inserting
    NULL.
    """


class InvalidMessageError(ValueError):
    """Raised when a message persisted to ``sessions.messages[]`` is malformed.

    Phase 1 of ADR-0074 (FRE-376) requires every assistant message to record
    ``model``, ``model_role``, and ``model_config_path`` so per-message model
    attribution survives in Postgres. ``SessionRepository.append_message``
    raises this when those fields are missing.
    """


class UnknownSessionError(ValueError):
    """Raised when a transport event is appended for a session that does not exist.

    The AG-UI event buffer allocates ``seq`` from the session's own counter
    (``sessions.last_event_seq``, FRE-1040), so a missing session row means no
    sequence number can be issued. Raised rather than skipped because the client
    renders strictly in sequence order: dropping the write silently would leave
    the response persisted but never delivered, which is the exact failure this
    numbering scheme exists to prevent.
    """


class ESHandlerLoopError(RuntimeError):
    """Raised when the Elasticsearch log handler is driven from a foreign loop.

    FRE-1055 binds the handler's queue, consumer task and Elasticsearch client
    to a single owner event loop captured at ``connect()``. Lifecycle calls
    (``drain``, ``disconnect``) touch all three, so running one on a different
    loop would mutate structures another loop owns. Raised rather than tolerated
    because the silent version of this bug — work scheduled onto the wrong loop
    and never run — is exactly the class of loss this ticket exists to remove.

    ``emit()`` deliberately does not raise this: it must remain callable from
    any thread or loop, and hands off via ``call_soon_threadsafe`` instead.
    """


class AttachmentUnsupportedError(ValueError):
    """Raised when a turn's attachment cannot be delivered to the model.

    Covers two related fail-closed cases (ADR-0101): routing (§5/§8a) — no
    reachable model can serve the attachment, no vision-capable model on the
    bound profile, escalation forbidden, or a ``"local"`` override with no
    capable local model; and resolution (§6, FRE-666) — a guardrail cap is
    exceeded after transformation, or the declared content type is neither a
    supported raster type nor a known non-raster type. Always raised with a
    message naming the unsupported modality, surfaced to the user verbatim.
    """
