"""Artifact envelope-integrity verification (FRE-512 / ADR-0089 D5).

Observes whether every served artifact actually received its walls — CSP header
present and exact, correct MIME, nosniff — without ever inspecting artifact bytes
or generation prompts (the D1/D5 scope boundary). The served-CSP envelope itself
is deployed by the Worker (FRE-509, ``personal_agent_secrets``); this package only
*verifies* it from the served response.
"""

from personal_agent.observability.artifact_envelope.probe import probe_served_envelope
from personal_agent.observability.artifact_envelope.spec import (
    EXPECTED_CSP_DIRECTIVES,
    EXPECTED_HTML_MIME,
    FORBIDDEN_SCRIPT_MIMES,
)
from personal_agent.observability.artifact_envelope.verifier import (
    EnvelopeReport,
    classify_access_denied,
    parse_csp,
    verify_envelope,
)

__all__ = [
    "EXPECTED_CSP_DIRECTIVES",
    "EXPECTED_HTML_MIME",
    "FORBIDDEN_SCRIPT_MIMES",
    "EnvelopeReport",
    "classify_access_denied",
    "parse_csp",
    "probe_served_envelope",
    "verify_envelope",
]
