"""The expected artifact-serve envelope (ADR-0089 D2, served by FRE-509).

CROSS-REPO SEAM: the authoritative policy lives in the Cloudflare Worker
(``personal_agent_secrets`` → ``infrastructure/terraform-cloudflare/worker/
artifacts.js``, ``ARTIFACT_CSP``). If the Worker CSP ever changes, this module
must change in lockstep — the verifier compares exact directive sets, so any
drift is alarm-visible (which is the point).
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType

#: Exact ADR-0089 D2 policy: directive → set of value tokens. Token order within
#: a directive is CSP-insignificant; directive *presence and values* are exact.
EXPECTED_CSP_DIRECTIVES: Mapping[str, frozenset[str]] = MappingProxyType(
    {
        "default-src": frozenset({"'none'"}),
        "script-src": frozenset({"https://artifacts.frenchforet.com", "'unsafe-inline'"}),
        "style-src": frozenset({"https://artifacts.frenchforet.com", "'unsafe-inline'"}),
        "img-src": frozenset({"https://artifacts.frenchforet.com", "data:"}),
        "font-src": frozenset({"https://artifacts.frenchforet.com", "data:"}),
        "connect-src": frozenset({"'none'"}),
        "worker-src": frozenset({"'none'"}),
        "form-action": frozenset({"'none'"}),
        "base-uri": frozenset({"'none'"}),
        "frame-ancestors": frozenset({"https://agent.frenchforet.com"}),
        "webrtc": frozenset({"'block'"}),
        "sandbox": frozenset({"allow-scripts"}),
    }
)

#: HTML artifacts must serve exactly this (compared structurally: type/subtype
#: and charset case-insensitive, no other parameters permitted).
EXPECTED_HTML_MIME = "text/html; charset=utf-8"

#: MIME types under which a response body executes as script — an artifact URL
#: must never serve one (ADR-0089 D2a: "cannot be loaded as a <script>").
FORBIDDEN_SCRIPT_MIMES: frozenset[str] = frozenset(
    {
        "application/javascript",
        "application/x-javascript",
        "application/ecmascript",
        "text/javascript",
        "text/ecmascript",
        "module/javascript",
    }
)
