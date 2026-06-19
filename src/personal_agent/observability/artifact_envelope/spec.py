"""The expected artifact-serve envelope (ADR-0089 D2, served by FRE-509).

CROSS-REPO SEAM: the authoritative policy lives in the Cloudflare Worker
(``personal_agent_secrets`` → ``infrastructure/terraform-cloudflare/worker/
artifacts.js``, ``ARTIFACT_CSP``). If the Worker CSP ever changes, this module
must change in lockstep — the verifier compares exact directive sets, so any
drift is alarm-visible (which is the point).
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import Literal

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

#: MIME types under which a response body executes as script. This single set has
#: **opposite polarity per surface** (ADR-0089 D2a): an *artifact* URL must never
#: serve one ("cannot be loaded as a <script>"), while a curated `/lib/` script
#: **must** serve one (else `nosniff` stops the browser executing it).
EXECUTABLE_SCRIPT_MIMES: frozenset[str] = frozenset(
    {
        "application/javascript",
        "application/x-javascript",
        "application/ecmascript",
        "text/javascript",
        "text/ecmascript",
        "module/javascript",
    }
)

#: Back-compat alias used on the artifact (negation) side — same set, read as the
#: set an artifact must *not* serve.
FORBIDDEN_SCRIPT_MIMES = EXECUTABLE_SCRIPT_MIMES

# ── Curated /lib/ toolkit (ADR-0089 Addendum A · FRE-527) ─────────────────────
#
# A `/lib/` asset is served from the same Worker as artifacts but plays the
# inverse MIME role. The manifest (`config/artifact_lib_manifest.json`) is the
# single cross-repo lockstep source: the `personal_agent_secrets` Worker hosts
# exactly its origin + paths, and `make verify-lib` asserts each one.

#: A served `/lib/` style asset must serve exactly this.
EXPECTED_STYLE_MIME = "text/css"

#: Required Content-Type per font extension (exact match).
EXPECTED_FONT_MIMES: Mapping[str, str] = MappingProxyType(
    {
        ".woff2": "font/woff2",
        ".woff": "font/woff",
        ".ttf": "font/ttf",
        ".otf": "font/otf",
    }
)

LibAssetKind = Literal["script", "style", "font"]

#: Which artifact CSP directive must admit each asset kind's host (so the asset
#: is genuinely "reachable under the artifact CSP", not merely same-host).
LIB_KIND_CSP_DIRECTIVE: Mapping[LibAssetKind, str] = MappingProxyType(
    {
        "script": "script-src",
        "style": "style-src",
        "font": "font-src",
    }
)

#: The committed manifest, resolved relative to the repo root.
DEFAULT_LIB_MANIFEST_PATH: Path = (
    Path(__file__).resolve().parents[4] / "config" / "artifact_lib_manifest.json"
)


@dataclass(frozen=True)
class LibAsset:
    """One curated toolkit asset hosted under ``/lib/``.

    Attributes:
        name: Library name (e.g. ``"katex"``), for reporting.
        path: Path relative to ``/lib/`` (e.g. ``"katex@0.16.47/katex.min.js"``).
        kind: Whether the asset is loaded as a script, stylesheet, or font.
        eval_gated: True for an asset still pending eval-free confirmation under
            the CSP (e.g. paged.js); excluded from the default assert set.
    """

    name: str
    path: str
    kind: LibAssetKind
    eval_gated: bool = False


def load_lib_manifest(
    path: str | Path = DEFAULT_LIB_MANIFEST_PATH,
) -> tuple[str, tuple[LibAsset, ...]]:
    """Load the curated ``/lib/`` manifest.

    Args:
        path: Path to the manifest JSON (defaults to the committed manifest).

    Returns:
        A ``(origin, assets)`` tuple — the serving origin and the parsed assets.

    Raises:
        ValueError: If the manifest is malformed, names an unknown ``kind``, or a
            font asset has an extension absent from :data:`EXPECTED_FONT_MIMES`.
    """
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    origin = raw.get("origin")
    entries = raw.get("assets")
    if not isinstance(origin, str) or not isinstance(entries, list):
        raise ValueError("manifest must have a string 'origin' and a list 'assets'")

    valid_kinds = set(LIB_KIND_CSP_DIRECTIVE)
    assets: list[LibAsset] = []
    for entry in entries:
        kind = entry.get("kind")
        name = entry.get("name")
        asset_path = entry.get("path")
        if kind not in valid_kinds:
            raise ValueError(f"unknown lib asset kind: {kind!r}")
        if not isinstance(name, str) or not isinstance(asset_path, str):
            raise ValueError(f"lib asset needs string 'name' and 'path': {entry!r}")
        if kind == "font" and PurePosixPath(asset_path).suffix not in EXPECTED_FONT_MIMES:
            raise ValueError(f"font asset has unknown extension: {asset_path!r}")
        assets.append(
            LibAsset(
                name=name,
                path=asset_path,
                kind=kind,
                eval_gated=bool(entry.get("eval_gated", False)),
            )
        )
    return origin, tuple(assets)
