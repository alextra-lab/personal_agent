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
    EXPECTED_FONT_MIMES,
    EXPECTED_HTML_MIME,
    EXPECTED_STYLE_MIME,
    FORBIDDEN_SCRIPT_MIMES,
    LIB_KIND_CSP_DIRECTIVE,
    SHARE_LIB_CORS_ORIGIN,
    SHARE_LIB_PROBE_PATH,
    SHARE_LIB_REQUIRED_HEADERS,
    SHARE_MIME_EXTENSIONS,
    SHARE_REQUIRED_HEADERS,
    LibAsset,
    LibAssetKind,
    ShareCspKind,
    expected_csp_directives,
    expected_share_csp_directives,
    load_lib_manifest,
)
from personal_agent.observability.artifact_envelope.verifier import (
    EnvelopeReport,
    LibAssetReport,
    ShareEnvelopeReport,
    ShareLibReport,
    classify_access_denied,
    parse_csp,
    verify_envelope,
    verify_lib_asset,
    verify_share_lib_response,
    verify_share_response,
)

__all__ = [
    "DEFAULT_LIB_MANIFEST_PATH",
    "EXECUTABLE_SCRIPT_MIMES",
    "EXPECTED_FONT_MIMES",
    "EXPECTED_HTML_MIME",
    "EXPECTED_STYLE_MIME",
    "FORBIDDEN_SCRIPT_MIMES",
    "LIB_KIND_CSP_DIRECTIVE",
    "SHARE_LIB_CORS_ORIGIN",
    "SHARE_LIB_PROBE_PATH",
    "SHARE_LIB_REQUIRED_HEADERS",
    "SHARE_MIME_EXTENSIONS",
    "SHARE_REQUIRED_HEADERS",
    "EnvelopeReport",
    "LibAsset",
    "LibAssetKind",
    "LibAssetReport",
    "ShareCspKind",
    "ShareEnvelopeReport",
    "ShareLibReport",
    "classify_access_denied",
    "expected_csp_directives",
    "expected_share_csp_directives",
    "load_lib_manifest",
    "parse_csp",
    "probe_served_envelope",
    "verify_envelope",
    "verify_lib_asset",
    "verify_share_lib_response",
    "verify_share_response",
]
