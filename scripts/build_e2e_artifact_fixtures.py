#!/usr/bin/env python3
"""Build the FRE-531 E2E artifact render-harness fixtures (ADR-0089 Addendum A7).

The Playwright harness (``e2e/artifact-lib/``) needs three things produced from
the *real* curated-toolkit plumbing, with no access to the live Access-gated
``/lib/`` origin:

1. a ``/lib/`` **mirror** of the version-pinned toolkit bytes (so a hosted-style
   artifact can be served locally under the artifact CSP);
2. a hosted-style **artifact.html** (a KaTeX formula + a Chart.js chart) that
   references that ``/lib/`` mirror, plus a minimal **pagedjs.html** for the
   eval-gate scenario; and
3. a **standalone.html** produced by the real
   :func:`personal_agent.storage.artifact_export.export_artifact_html` in
   ``inline`` mode — the offline-export artifact the harness opens with no network.

Real bytes, no live Access: each ``/lib/`` asset is fetched from its public-CDN
twin recorded in ``config/artifact_lib_substitution_map.json`` and **byte-verified
against the map's pinned ``sha384`` SRI** (fail-closed on CDN drift). The single
source of truth for the served CSP is
:data:`personal_agent.observability.artifact_envelope.spec.EXPECTED_CSP_DIRECTIVES`
— this script emits it (host token → ``{ORIGIN}`` placeholder) into the build
manifest, so the TypeScript harness never re-declares the policy.

Usage:
    uv run python scripts/build_e2e_artifact_fixtures.py --out e2e/artifact-lib/.fixtures
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urljoin

import httpx

from personal_agent.observability.artifact_envelope.spec import EXPECTED_CSP_DIRECTIVES
from personal_agent.storage.artifact_export import (
    ArtifactExportError,
    AssetFetcher,
    LibAsset,
    SubstitutionMap,
    export_artifact_html,
    load_substitution_map,
    verify_sri,
)

#: The artifacts ``/lib/`` origin (host token in the artifact CSP). Rebound to the
#: harness's own localhost origin via the ``{ORIGIN}`` placeholder.
ARTIFACTS_ORIGIN = "https://artifacts.frenchforet.com"
_ORIGIN_PLACEHOLDER = "{ORIGIN}"

#: Only woff2 fonts are mirrored — every curated @font-face lists woff2 first and
#: browsers prefer it, so the woff/ttf fallbacks are never requested.
_MIRROR_FONT_EXTS: frozenset[str] = frozenset({"woff2"})
_CSS_URL_RE = re.compile(r"""url\(\s*(['"]?)(?P<url>[^'")]+)\1\s*\)""", re.IGNORECASE)
_SCHEME_RE = re.compile(r"^[a-z][a-z0-9+.\-]*:", re.IGNORECASE)

# Real curated-toolkit pins exercised by the harness (mirror the manifest paths).
_KATEX_CSS = "lib/katex@0.16.47/katex.min.css"
_KATEX_JS = "lib/katex@0.16.47/katex.min.js"
_CHART_JS = "lib/chartjs@4.4.7/chart.umd.js"
_PAGEDJS = "lib/pagedjs@0.4.3/paged.polyfill.min.js"

_KATEX_FORMULA_TEX = "E = mc^2"
_CHART_DATA: tuple[int, ...] = (3, 1, 4, 1, 5, 9, 2, 6)


@dataclass(frozen=True)
class FixtureSpec:
    """The inputs that define one E2E fixture set.

    Attributes:
        artifact_html: The hosted-style artifact (KaTeX + Chart.js) referencing
            ``/lib/`` — used for the hosted-render and offline-export scenarios.
        pagedjs_html: A minimal artifact loading paged.js — the eval-gate scenario.
        mirror_lib_paths: The ``/lib/`` asset paths to write into the mirror.
        katex_formula_tex: The TeX source the harness asserts KaTeX rendered.
        chart_data: The dataset the harness asserts the live Chart instance holds.
    """

    artifact_html: str
    pagedjs_html: str
    mirror_lib_paths: tuple[str, ...]
    katex_formula_tex: str
    chart_data: tuple[int, ...]


def parse_csp_header(header: str) -> dict[str, set[str]]:
    """Parse a CSP header string into ``{directive: {tokens}}`` (order-insensitive)."""
    parsed: dict[str, set[str]] = {}
    for chunk in header.split(";"):
        parts = chunk.split()
        if not parts:
            continue
        parsed[parts[0]] = set(parts[1:])
    return parsed


def build_csp_header_template(origin_placeholder: str = _ORIGIN_PLACEHOLDER) -> str:
    """Render :data:`EXPECTED_CSP_DIRECTIVES` as a header string with a host placeholder.

    The artifacts host token is replaced by ``origin_placeholder`` so the harness
    can rebind it to its own localhost serving origin while preserving the exact
    directive set (the one fidelity gap the live ``verify-envelope`` closes).

    Args:
        origin_placeholder: Token substituted for the artifacts origin.

    Returns:
        A ``"directive tokens; …"`` CSP header string, tokens sorted for stability.
    """
    directives: list[str] = []
    for directive, tokens in EXPECTED_CSP_DIRECTIVES.items():
        rendered = sorted(origin_placeholder if t == ARTIFACTS_ORIGIN else t for t in tokens)
        directives.append(" ".join([directive, *rendered]))
    return "; ".join(directives)


def _hosted_source(asset: LibAsset, origin: str) -> str:
    """The URL the *hosted* primary bytes come from (mirrors export's primary rule)."""
    if asset.kind == "style" and asset.public_cdn_url:
        return asset.public_cdn_url
    return f"{origin}/{asset.lib_path}"


async def _mirror_css_fonts(
    css_bytes: bytes, asset: LibAsset, out_dir: Path, css_lib_path: str, fetcher: AssetFetcher
) -> None:
    """Fetch + write a stylesheet's relative woff2 font subresources into the mirror."""
    if not asset.public_cdn_url:
        return
    css = css_bytes.decode("utf-8", errors="replace")
    css_dir = out_dir / Path(css_lib_path).parent
    for match in _CSS_URL_RE.finditer(css):
        ref = match.group("url").strip()
        if _SCHEME_RE.match(ref) or ref.startswith("//") or ref.startswith("/"):
            continue
        ext = ref.rsplit(".", 1)[-1].split("?")[0].split("#")[0].lower()
        if ext not in _MIRROR_FONT_EXTS:
            continue
        font_bytes = await fetcher.fetch(urljoin(asset.public_cdn_url, ref))
        dest = css_dir / ref
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(font_bytes)


async def build_fixtures(
    *, out_dir: Path, sub_map: SubstitutionMap, fetcher: AssetFetcher, spec: FixtureSpec
) -> dict[str, object]:
    """Build the E2E fixture set into ``out_dir``.

    Writes the ``/lib/`` mirror (byte-verified against the pinned SRI), the hosted
    ``artifact.html`` / ``pagedjs.html``, the inline-export ``standalone.html``
    (via the real transform), and a ``build-manifest.json`` driving the harness.

    Args:
        out_dir: Destination directory (created if absent).
        sub_map: The export substitution map (origin + per-asset CDN twin + SRI).
        fetcher: Byte source for ``/lib/`` assets + stylesheet subresources.
        spec: The fixture definition.

    Returns:
        The build manifest (also written to ``build-manifest.json``).

    Raises:
        ArtifactExportError: On an SRI mismatch or a fetch failure.
    """
    out_dir.mkdir(parents=True, exist_ok=True)

    for lib_path in spec.mirror_lib_paths:
        asset = sub_map.by_lib_path[lib_path]
        source = _hosted_source(asset, sub_map.origin)
        data = await fetcher.fetch(source)
        if not verify_sri(data, asset.sri):
            raise ArtifactExportError(
                f"SRI mismatch for {lib_path!r} fetched from {source} — refusing to mirror"
            )
        dest = out_dir / lib_path
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
        if asset.kind == "style":
            await _mirror_css_fonts(data, asset, out_dir, lib_path, fetcher)

    (out_dir / "artifact.html").write_text(spec.artifact_html, encoding="utf-8")
    (out_dir / "pagedjs.html").write_text(spec.pagedjs_html, encoding="utf-8")

    standalone = await export_artifact_html(
        html=spec.artifact_html, mode="inline", sub_map=sub_map, fetcher=fetcher
    )
    (out_dir / "standalone.html").write_text(standalone, encoding="utf-8")

    manifest: dict[str, object] = {
        "csp_header_template": build_csp_header_template(),
        "artifact": "artifact.html",
        "standalone": "standalone.html",
        "pagedjs": "pagedjs.html",
        "lib_dir": "lib",
        "katex_formula_tex": spec.katex_formula_tex,
        "chart_data": list(spec.chart_data),
        "mirror_lib_paths": list(spec.mirror_lib_paths),
    }
    (out_dir / "build-manifest.json").write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
    )
    return manifest


class CdnFetcher:
    """Real :class:`AssetFetcher` for the hermetic harness build.

    Two URL classes are served:
      * an artifacts-origin ``/lib/`` URL is **remapped** to its public-CDN twin
        from the substitution map (raises if the asset has no twin) — this is what
        :func:`export_artifact_html` and the mirror writer request for scripts; and
      * an absolute CDN URL is fetched directly (a style's CDN primary and its
        relative ``url(...)`` font subresources).

    Content integrity is enforced by the callers' pinned-SRI check, not here.
    """

    def __init__(self, sub_map: SubstitutionMap, client: httpx.AsyncClient) -> None:
        """Bind the substitution map (origin + CDN twins) and an open HTTP client."""
        self._origin = sub_map.origin
        self._by_path = sub_map.by_lib_path
        self._client = client

    async def fetch(self, url: str) -> bytes:
        """Return the bytes for ``url`` (raises :class:`ArtifactExportError` on failure)."""
        target = url
        prefix = f"{self._origin}/"
        if url.startswith(prefix):
            lib_path = url[len(prefix) :]
            asset = self._by_path.get(lib_path)
            if asset is None or not asset.public_cdn_url:
                raise ArtifactExportError(
                    f"no public-CDN twin for {lib_path!r} — cannot build hermetically"
                )
            target = asset.public_cdn_url
        try:
            response = await self._client.get(target)
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise ArtifactExportError(f"fetch failed for {target}: {exc}") from exc
        return response.content


def _real_spec() -> FixtureSpec:
    """The real KaTeX + Chart.js fixture and the paged.js eval-gate fixture."""
    artifact_html = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>FRE-531 curated toolkit render fixture</title>
<link rel="stylesheet" href="/{_KATEX_CSS}">
<script src="/{_KATEX_JS}"></script>
<script src="/{_CHART_JS}"></script>
</head>
<body>
<h1>Curated toolkit render</h1>
<div id="formula"></div>
<canvas id="chart" width="320" height="200"></canvas>
<script>
  katex.render({json.dumps(_KATEX_FORMULA_TEX)}, document.getElementById("formula"), {{
    throwOnError: false,
  }});
  new Chart(document.getElementById("chart").getContext("2d"), {{
    type: "bar",
    data: {{
      labels: {json.dumps([str(i) for i in range(len(_CHART_DATA))])},
      datasets: [{{ label: "fixture", data: {json.dumps(list(_CHART_DATA))} }}],
    }},
    options: {{ animation: false, responsive: false }},
  }});
</script>
</body>
</html>
"""
    pagedjs_html = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>FRE-531 paged.js eval-gate fixture</title>
<style>@page {{ size: 200px 260px; margin: 12px; }}</style>
<script src="/{_PAGEDJS}"></script>
</head>
<body>
{"".join(f"<p>paged content line {i}</p>" for i in range(40))}
</body>
</html>
"""
    return FixtureSpec(
        artifact_html=artifact_html,
        pagedjs_html=pagedjs_html,
        mirror_lib_paths=(_KATEX_CSS, _KATEX_JS, _CHART_JS, _PAGEDJS),
        katex_formula_tex=_KATEX_FORMULA_TEX,
        chart_data=_CHART_DATA,
    )


async def _amain(out_dir: Path) -> int:
    sub_map = load_substitution_map()
    async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
        fetcher = CdnFetcher(sub_map, client)
        try:
            manifest = await build_fixtures(
                out_dir=out_dir, sub_map=sub_map, fetcher=fetcher, spec=_real_spec()
            )
        except ArtifactExportError as exc:
            print(f"FIXTURE BUILD FAILED: {exc}")
            return 1
    print(f"Built E2E fixtures in {out_dir}")
    for key in ("artifact", "standalone", "pagedjs"):
        print(f"  {key:10s} {manifest[key]}")
    print(f"  mirror     {len(manifest['mirror_lib_paths'])} /lib/ assets")  # type: ignore[arg-type]
    return 0


def main() -> int:
    """CLI entry point: build the E2E fixtures into ``--out``.

    Returns:
        ``0`` on success; ``1`` if any asset could not be fetched or SRI-verified
        (unverifiable is not built — the harness must not run on stale bytes).
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--out",
        default="e2e/artifact-lib/.fixtures",
        help="Output directory for the fixture set (default: e2e/artifact-lib/.fixtures).",
    )
    args = parser.parse_args()
    return asyncio.run(_amain(Path(args.out)))


if __name__ == "__main__":
    sys.exit(main())
