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
