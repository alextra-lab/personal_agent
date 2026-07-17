"""Artifact export-to-standalone transform (ADR-0089 Addendum A5 · FRE-530).

A hosted artifact references the curated, Access-gated ``/lib/`` shelf
(``https://artifacts.example.com/lib/<name>@<version>/…``). To make such an
artifact *portable* this module rewrites those references in one of two modes:

* **inline** (default, offline-portable): fetch the pinned ``/lib/`` bytes (and
  a stylesheet's own ``url(...)`` subresources, e.g. KaTeX fonts) and inline
  them — scripts → inline ``<script>``, CSS → inline ``<style>``, fonts → ``data:``
  URIs — into one self-contained file that renders with **no network**.
* **substitute** (opt-in, lean/online): rewrite each ``/lib/`` reference to the
  **same version on a public CDN**, emitting ``integrity`` (our pinned
  ``sha384``) + ``crossorigin`` so the browser byte-verifies it (Subresource
  Integrity). Only assets with a CORS-verified public twin are substituted;
  three.js and the fonts have none, so they fall back to inline.

Trust posture:

* Inline mode fetches the primary bytes and **verifies them against the map's
  pinned SRI** — a mismatch fails the export (never ship un-pinned bytes).
* Stylesheet ``url(...)`` subresources are not individually pinned; they are
  fetched under a strict guard (relative-only, must stay under the parent
  asset's base dir, extension allowlist, size caps) and baked as ``data:`` URIs.
* Substitute mode never fetches at export time — the **browser** enforces SRI at
  view time (fail-closed on CDN drift).

The transform is pure and dependency-injected (an :class:`AssetFetcher`); the
gateway wires a real httpx-backed fetcher. An exported file **leaves the sealed
envelope** — it runs unsandboxed wherever opened — which is acceptable because
export is user-initiated and the standing "never bake secrets into an artifact"
rule (ADR-0089 D4) bounds what a file can carry.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol
from urllib.parse import urljoin, urlparse

ExportMode = Literal["inline", "substitute"]

# Subresource hardening (decision §2.2): only same-version-dir font files,
# bounded in size, are inlined from a stylesheet's url(...) refs.
_SUBRESOURCE_EXTS: frozenset[str] = frozenset({"woff2", "woff", "ttf", "otf"})
_MAX_SUBRESOURCE_BYTES = 2 * 1024 * 1024
_MAX_TOTAL_INLINE_BYTES = 8 * 1024 * 1024

_MIME_BY_EXT: dict[str, str] = {
    "woff2": "font/woff2",
    "woff": "font/woff",
    "ttf": "font/ttf",
    "otf": "font/otf",
}

_DEFAULT_MAP_PATH = (
    Path(__file__).resolve().parents[3] / "config" / "artifact_lib_substitution_map.json"
)


class ArtifactExportError(RuntimeError):
    """An artifact could not be exported.

    Raised on an SRI mismatch, an asset fetch failure, or an unmapped ``/lib/``
    reference that would break the portability promise.
    """


@dataclass(frozen=True)
class LibAsset:
    """One entry of the export substitution map (ADR-0089 A5 / FRE-527)."""

    lib_path: str
    name: str
    kind: str  # "script" | "style" | "font"
    loading: str  # "classic" | "module"
    public_cdn_url: str | None
    sri: str
    cors_verified: bool
    fallback: str

    @property
    def substitutable(self) -> bool:
        """Whether this asset has a faithful, CORS-verified public-CDN twin."""
        return bool(self.public_cdn_url) and self.cors_verified


@dataclass(frozen=True)
class SubstitutionMap:
    """The artifacts ``/lib/`` origin + its per-asset export map, keyed by path."""

    origin: str
    by_lib_path: dict[str, LibAsset]


class AssetFetcher(Protocol):
    """Fetches the raw bytes for a URL; raises :class:`ArtifactExportError` on failure."""

    async def fetch(self, url: str) -> bytes:
        """Return the raw bytes at ``url`` (raises on any non-success)."""
        ...


# ---------------------------------------------------------------------------
# Map loading
# ---------------------------------------------------------------------------

# Caches the parsed file content only — never the resolved origin, since that
# depends on the mutable settings.artifacts_public_base_url (FRE-895) and must
# be recomputed on every call so a process/test that changes the setting after
# first load sees it, not a stale cached value (mirrors expected_csp_directives()
# in observability/artifact_envelope/spec.py, which is uncached for the same reason).
_MAP_CACHE: dict[str, tuple[str, dict[str, LibAsset]]] = {}


def load_substitution_map(path: Path | None = None) -> SubstitutionMap:
    """Load + cache the export substitution map from JSON.

    Args:
        path: Override path (tests); defaults to
            ``config/artifact_lib_substitution_map.json``.

    Returns:
        The parsed :class:`SubstitutionMap`. The map's ``origin`` is a neutral
        placeholder (FRE-895); when ``settings.artifacts_public_base_url`` is
        configured, it overrides the placeholder so export targets the real origin.

    Raises:
        ArtifactExportError: If the file is missing or malformed.
    """
    from personal_agent.config import settings  # noqa: PLC0415

    resolved = (path or _DEFAULT_MAP_PATH).resolve()
    cache_key = str(resolved)
    cached = _MAP_CACHE.get(cache_key)

    if cached is None:
        try:
            raw = json.loads(resolved.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ArtifactExportError(f"cannot load substitution map at {resolved}: {exc}") from exc

        by_lib_path: dict[str, LibAsset] = {}
        for entry in raw.get("entries", []):
            asset = LibAsset(
                lib_path=entry["lib_path"],
                name=entry.get("name", ""),
                kind=entry["kind"],
                loading=entry.get("loading", "classic"),
                public_cdn_url=entry.get("public_cdn_url"),
                sri=entry["sri"],
                cors_verified=bool(entry.get("cors_verified", False)),
                fallback=entry.get("fallback", "inline"),
            )
            by_lib_path[asset.lib_path] = asset

        cached = (str(raw["origin"]).rstrip("/"), by_lib_path)
        _MAP_CACHE[cache_key] = cached

    file_origin, by_lib_path = cached
    origin = (settings.artifacts_public_base_url or file_origin).rstrip("/")
    return SubstitutionMap(origin=origin, by_lib_path=by_lib_path)


# ---------------------------------------------------------------------------
# SRI
# ---------------------------------------------------------------------------


def compute_sri(data: bytes, algo: str = "sha384") -> str:
    """Return the Subresource-Integrity digest ``"<algo>-<base64>"`` for ``data``."""
    digest = hashlib.new(algo, data).digest()
    return f"{algo}-{base64.b64encode(digest).decode('ascii')}"


def verify_sri(data: bytes, expected: str) -> bool:
    """Return whether ``data`` matches the ``"<algo>-<base64>"`` SRI ``expected``."""
    algo, _, _ = expected.partition("-")
    if algo not in hashlib.algorithms_available:
        return False
    return compute_sri(data, algo) == expected


# ---------------------------------------------------------------------------
# Reference discovery
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Ref:
    """One ``/lib/`` reference found in the artifact HTML."""

    start: int
    end: int
    kind: Literal["script", "link", "url"]
    tag: str  # the full matched text
    url: str  # the matched /lib/ URL
    asset: LibAsset


def _lib_path_of(url: str) -> str:
    """Normalise a full / protocol-relative / absolute-path ``/lib/`` URL to a map key."""
    if url.startswith("//"):
        url = "https:" + url
    elif url.startswith("/"):
        return url.lstrip("/")
    return urlparse(url).path.lstrip("/")


def _lib_url_pattern(host: str) -> str:
    """Regex alternation matching this origin's ``/lib/`` URLs (3 ref forms)."""
    tail = r"/lib/[^\"'>)\s]+"
    return rf"(?:(?:https?:)?//{re.escape(host)}{tail}|{tail})"


def _find_refs(html: str, sub_map: SubstitutionMap) -> list[_Ref]:
    """Locate every ``/lib/`` reference; fail closed on an unmapped path."""
    host = urlparse(sub_map.origin).netloc
    liburl = _lib_url_pattern(host)
    patterns: list[tuple[Literal["script", "link", "url"], re.Pattern[str]]] = [
        (
            "script",
            re.compile(
                rf"<script\b[^>]*?\bsrc\s*=\s*(['\"]?)(?P<url>{liburl})\1[^>]*?>\s*</script\s*>",
                re.IGNORECASE | re.DOTALL,
            ),
        ),
        (
            "link",
            re.compile(
                rf"<link\b[^>]*?\bhref\s*=\s*(['\"]?)(?P<url>{liburl})\1[^>]*?>",
                re.IGNORECASE | re.DOTALL,
            ),
        ),
        (
            "url",
            re.compile(rf"url\(\s*(['\"]?)(?P<url>{liburl})\1\s*\)", re.IGNORECASE),
        ),
    ]

    refs: list[_Ref] = []
    for kind, pattern in patterns:
        for m in pattern.finditer(html):
            url = m.group("url")
            lib_path = _lib_path_of(url)
            asset = sub_map.by_lib_path.get(lib_path)
            if asset is None:
                raise ArtifactExportError(
                    f"artifact references an unmapped /lib/ asset: {lib_path!r} "
                    "(not in the export substitution map)"
                )
            refs.append(
                _Ref(start=m.start(), end=m.end(), kind=kind, tag=m.group(0), url=url, asset=asset)
            )

    # Deterministic, non-overlapping order (start ascending; drop nested overlaps).
    refs.sort(key=lambda r: r.start)
    deduped: list[_Ref] = []
    last_end = -1
    for ref in refs:
        if ref.start >= last_end:
            deduped.append(ref)
            last_end = ref.end
    return deduped


# ---------------------------------------------------------------------------
# Inlining
# ---------------------------------------------------------------------------

_TYPE_ATTR_RE = re.compile(r"""\btype\s*=\s*(['"]?)([^'">\s]+)\1""", re.IGNORECASE)
_CSS_URL_RE = re.compile(r"""url\(\s*(['"]?)(?P<url>[^'")]+)\1\s*\)""", re.IGNORECASE)
_SCHEME_RE = re.compile(r"^[a-z][a-z0-9+.\-]*:", re.IGNORECASE)


def _primary_source(asset: LibAsset, origin: str) -> str:
    """Where to fetch an asset's primary bytes in inline mode.

    A stylesheet with a public twin is fetched from the **CDN base** so its own
    relative ``url(...)`` subresources resolve (SRI still anchors it to our
    pinned bytes); everything else is fetched from our ``/lib/`` origin.
    """
    if asset.kind == "style" and asset.public_cdn_url:
        return asset.public_cdn_url
    return f"{origin}/{asset.lib_path}"


async def _fetch_verified(asset: LibAsset, origin: str, fetcher: AssetFetcher) -> tuple[bytes, str]:
    source = _primary_source(asset, origin)
    data = await fetcher.fetch(source)
    if not verify_sri(data, asset.sri):
        raise ArtifactExportError(
            f"SRI mismatch for {asset.lib_path!r} fetched from {source} — refusing to inline"
        )
    return data, source


def _ext_of(path: str) -> str:
    return path.rsplit(".", 1)[-1].split("?")[0].split("#")[0].lower()


def _data_uri(data: bytes, mime: str) -> str:
    return f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"


async def _inline_css_subresources(
    css: str, css_source: str, fetcher: AssetFetcher, budget: list[int]
) -> str:
    """Replace a stylesheet's relative ``url(...)`` font refs with ``data:`` URIs."""
    base_dir = css_source.rsplit("/", 1)[0] + "/"
    for m in reversed(list(_CSS_URL_RE.finditer(css))):
        ref = m.group("url").strip()
        if _SCHEME_RE.match(ref) or ref.startswith("//") or ref.startswith("/"):
            continue  # only relative refs are inlined; data:/absolute left untouched
        ext = _ext_of(ref)
        if ext not in _SUBRESOURCE_EXTS:
            continue
        resolved = urljoin(css_source, ref)
        if not resolved.startswith(base_dir):
            continue  # path-escape guard
        data = await fetcher.fetch(resolved)
        if len(data) > _MAX_SUBRESOURCE_BYTES or budget[0] + len(data) > _MAX_TOTAL_INLINE_BYTES:
            continue
        budget[0] += len(data)
        uri = _data_uri(data, _MIME_BY_EXT.get(ext, "application/octet-stream"))
        css = css[: m.start("url")] + uri + css[m.end("url") :]
    return css


async def _render_inline(ref: _Ref, origin: str, fetcher: AssetFetcher, budget: list[int]) -> str:
    """Produce the inline replacement for one reference."""
    data, source = await _fetch_verified(ref.asset, origin, fetcher)
    if ref.kind == "script":
        text = data.decode("utf-8", errors="replace")
        type_m = _TYPE_ATTR_RE.search(ref.tag)
        type_attr = f' type="{type_m.group(2)}"' if type_m else ""
        return f"<script{type_attr}>{text}</script>"
    if ref.kind == "link":
        css = data.decode("utf-8", errors="replace")
        css = await _inline_css_subresources(css, source, fetcher, budget)
        return f"<style>{css}</style>"
    # url(...) — a font (or other binary) primary asset
    uri = _data_uri(data, _MIME_BY_EXT.get(_ext_of(ref.url), "application/octet-stream"))
    return f'url("{uri}")'


# ---------------------------------------------------------------------------
# Substitution
# ---------------------------------------------------------------------------


def _inject_attrs_before_close(tag: str, attrs: str) -> str:
    """Insert ``attrs`` just before the opening tag's closing ``>`` (handles ``/>``)."""
    idx = tag.find(">")
    if idx == -1:
        return tag
    if idx > 0 and tag[idx - 1] == "/":
        idx -= 1
    return f"{tag[:idx]} {attrs}{tag[idx:]}"


def _render_substitute(ref: _Ref) -> str | None:
    """Rewrite a script/link to its CDN twin + SRI; ``None`` → caller inlines."""
    asset = ref.asset
    if ref.kind == "url" or not asset.substitutable:
        return None
    assert asset.public_cdn_url is not None
    new = ref.tag.replace(ref.url, asset.public_cdn_url, 1)
    if "integrity=" not in new.lower():
        new = _inject_attrs_before_close(new, f'integrity="{asset.sri}" crossorigin="anonymous"')
    return new


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


async def export_artifact_html(
    *,
    html: str,
    mode: ExportMode,
    sub_map: SubstitutionMap,
    fetcher: AssetFetcher,
) -> str:
    """Transform a hosted artifact's HTML into a portable standalone document.

    Args:
        html: The stored artifact HTML.
        mode: ``"inline"`` (offline-portable) or ``"substitute"`` (CDN + SRI).
        sub_map: The export substitution map (origin + per-asset entries).
        fetcher: Byte source for ``/lib/`` assets + stylesheet subresources.

    Returns:
        The rewritten HTML (no remaining references to the ``/lib/`` origin).

    Raises:
        ArtifactExportError: On an unmapped ``/lib/`` reference, an SRI mismatch,
            or a fetch failure.
    """
    refs = _find_refs(html, sub_map)
    if not refs:
        return html

    budget = [0]  # mutable shared total-inlined-bytes counter
    replacements: list[tuple[int, int, str]] = []
    for ref in refs:
        if mode == "substitute":
            sub = _render_substitute(ref)
            replacement = (
                sub
                if sub is not None
                else await _render_inline(ref, sub_map.origin, fetcher, budget)
            )
        else:
            replacement = await _render_inline(ref, sub_map.origin, fetcher, budget)
        replacements.append((ref.start, ref.end, replacement))

    out = html
    for start, end, replacement in reversed(replacements):
        out = out[:start] + replacement + out[end:]
    return out
