"""Artifact envelope-integrity verification (FRE-512 / ADR-0089 D5).

Observes whether every served artifact actually received its walls — CSP header
present and exact, correct MIME, nosniff — without ever inspecting artifact bytes
or generation prompts (the D1/D5 scope boundary). The served-CSP envelope itself
is deployed by the Worker (FRE-509, ``personal_agent_secrets``); this package only
*verifies* it from the served response.
"""

from personal_agent.observability.artifact_envelope.probe import probe_served_envelope
from personal_agent.observability.artifact_envelope.spec import (
    DEFAULT_LIB_MANIFEST_PATH,
    EXECUTABLE_SCRIPT_MIMES,
    EXPECTED_CSP_DIRECTIVES,
    EXPECTED_FONT_MIMES,
    EXPECTED_HTML_MIME,
    EXPECTED_STYLE_MIME,
    FORBIDDEN_SCRIPT_MIMES,
    LIB_KIND_CSP_DIRECTIVE,
    LibAsset,
    LibAssetKind,
    load_lib_manifest,
)
from personal_agent.observability.artifact_envelope.verifier import (
    EnvelopeReport,
    LibAssetReport,
    classify_access_denied,
    parse_csp,
    verify_envelope,
    verify_lib_asset,
)

__all__ = [
    "DEFAULT_LIB_MANIFEST_PATH",
    "EXECUTABLE_SCRIPT_MIMES",
    "EXPECTED_CSP_DIRECTIVES",
    "EXPECTED_FONT_MIMES",
    "EXPECTED_HTML_MIME",
    "EXPECTED_STYLE_MIME",
    "FORBIDDEN_SCRIPT_MIMES",
    "LIB_KIND_CSP_DIRECTIVE",
    "EnvelopeReport",
    "LibAsset",
    "LibAssetKind",
    "LibAssetReport",
    "classify_access_denied",
    "load_lib_manifest",
    "parse_csp",
    "probe_served_envelope",
    "verify_envelope",
    "verify_lib_asset",
]
