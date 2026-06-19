"""Unit tests for the artifact export-to-standalone transform (FRE-530).

ADR-0089 Addendum A5. The transform turns a hosted, ``/lib/``-referencing
artifact into a portable HTML file in two modes: ``inline`` (offline) and
``substitute`` (rewrite ``/lib/`` refs to a public-CDN twin + SRI, inline
fallback). These tests drive the pure transform with a synthetic substitution
map and a fake asset fetcher — no network, no R2, no FastAPI. SRIs are computed
from the fixture bytes so inline-mode verification passes deterministically; a
separate test parses the *real* ``config/artifact_lib_substitution_map.json``
for shape.
"""

from __future__ import annotations

import pytest

from personal_agent.storage.artifact_export import (
    ArtifactExportError,
    LibAsset,
    SubstitutionMap,
    compute_sri,
    export_artifact_html,
    load_substitution_map,
    verify_sri,
)

_ORIGIN = "https://artifacts.frenchforet.com"


class _FakeFetcher:
    """AssetFetcher backed by a dict; records calls; raises on unknown URL."""

    def __init__(self, mapping: dict[str, bytes]) -> None:
        self._mapping = mapping
        self.calls: list[str] = []

    async def fetch(self, url: str) -> bytes:
        self.calls.append(url)
        try:
            return self._mapping[url]
        except KeyError as exc:
            raise ArtifactExportError(f"fake fetcher: no bytes for {url}") from exc


def _asset(
    lib_path: str,
    kind: str,
    *,
    sri: str,
    cdn: str | None = None,
    cors: bool = False,
) -> LibAsset:
    return LibAsset(
        lib_path=lib_path,
        name=lib_path.split("/")[-1],
        kind=kind,
        loading="classic",
        public_cdn_url=cdn,
        sri=sri,
        cors_verified=cors,
        fallback="inline",
    )


def _map(*assets: LibAsset) -> SubstitutionMap:
    return SubstitutionMap(origin=_ORIGIN, by_lib_path={a.lib_path: a for a in assets})


# ---------------------------------------------------------------------------
# SRI helpers
# ---------------------------------------------------------------------------


def test_compute_and_verify_sri_roundtrip() -> None:
    data = b"window.katex = {};"
    digest = compute_sri(data)
    assert digest.startswith("sha384-")
    assert verify_sri(data, digest)
    assert not verify_sri(b"tampered", digest)


# ---------------------------------------------------------------------------
# Inline mode
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_inline_script_replaces_with_inline_block() -> None:
    body = b"window.Chart = function(){};"
    asset = _asset("lib/chartjs@4.4.7/chart.umd.js", "script", sri=compute_sri(body))
    fetcher = _FakeFetcher({f"{_ORIGIN}/lib/chartjs@4.4.7/chart.umd.js": body})
    html = (
        "<html><head>"
        f'<script src="{_ORIGIN}/lib/chartjs@4.4.7/chart.umd.js"></script>'
        "</head><body></body></html>"
    )

    out = await export_artifact_html(html=html, mode="inline", sub_map=_map(asset), fetcher=fetcher)

    assert "window.Chart = function(){};" in out
    assert "src=" not in out
    assert _ORIGIN not in out


@pytest.mark.asyncio
async def test_inline_script_preserves_type_attribute() -> None:
    body = b"export const x = 1;"
    asset = _asset("lib/three@0.171.0/three.iife.min.js", "script", sri=compute_sri(body))
    fetcher = _FakeFetcher({f"{_ORIGIN}/lib/three@0.171.0/three.iife.min.js": body})
    html = f'<script type="module" src="{_ORIGIN}/lib/three@0.171.0/three.iife.min.js"></script>'

    out = await export_artifact_html(html=html, mode="inline", sub_map=_map(asset), fetcher=fetcher)

    assert 'type="module"' in out
    assert "src=" not in out


@pytest.mark.asyncio
async def test_inline_style_inlines_css_and_subresource_fonts() -> None:
    css = b'@font-face{font-family:"KaTeX_Main";src:url(fonts/KaTeX_Main.woff2) format("woff2")}'
    font = b"\x00woff2-bytes\x00"
    cdn = "https://cdn.jsdelivr.net/npm/katex@0.16.47/dist/katex.min.css"
    asset = _asset(
        "lib/katex@0.16.47/katex.min.css", "style", sri=compute_sri(css), cdn=cdn, cors=True
    )
    fetcher = _FakeFetcher(
        {
            cdn: css,
            "https://cdn.jsdelivr.net/npm/katex@0.16.47/dist/fonts/KaTeX_Main.woff2": font,
        }
    )
    html = f'<link rel="stylesheet" href="{_ORIGIN}/lib/katex@0.16.47/katex.min.css">'

    out = await export_artifact_html(html=html, mode="inline", sub_map=_map(asset), fetcher=fetcher)

    assert "<style>" in out
    assert "data:font/woff2;base64," in out
    assert "url(fonts/KaTeX_Main.woff2)" not in out
    assert _ORIGIN not in out


@pytest.mark.asyncio
async def test_inline_css_subresource_rejected_when_extension_not_allowed() -> None:
    css = b"body{background:url(evil.js)}"
    cdn = "https://cdn.jsdelivr.net/npm/katex@0.16.47/dist/katex.min.css"
    asset = _asset(
        "lib/katex@0.16.47/katex.min.css", "style", sri=compute_sri(css), cdn=cdn, cors=True
    )
    fetcher = _FakeFetcher({cdn: css})
    html = f'<link rel="stylesheet" href="{_ORIGIN}/lib/katex@0.16.47/katex.min.css">'

    out = await export_artifact_html(html=html, mode="inline", sub_map=_map(asset), fetcher=fetcher)

    assert "url(evil.js)" in out
    assert "data:" not in out
    # only the CSS itself was fetched; the disallowed subresource was not
    assert fetcher.calls == [cdn]


@pytest.mark.asyncio
async def test_inline_css_subresource_rejected_on_base_escape() -> None:
    css = b"body{src:url(../../../../etc/passwd.woff2)}"
    cdn = "https://cdn.jsdelivr.net/npm/katex@0.16.47/dist/katex.min.css"
    asset = _asset(
        "lib/katex@0.16.47/katex.min.css", "style", sri=compute_sri(css), cdn=cdn, cors=True
    )
    fetcher = _FakeFetcher({cdn: css})
    html = f'<link rel="stylesheet" href="{_ORIGIN}/lib/katex@0.16.47/katex.min.css">'

    out = await export_artifact_html(html=html, mode="inline", sub_map=_map(asset), fetcher=fetcher)

    assert "etc/passwd.woff2" in out
    assert "data:" not in out
    assert fetcher.calls == [cdn]


@pytest.mark.asyncio
async def test_inline_font_url_becomes_data_uri() -> None:
    font = b"\x00source-serif\x00"
    lib_path = "lib/fonts/source-serif-4@4.005/source-serif-4.woff2"
    asset = _asset(lib_path, "font", sri=compute_sri(font))
    fetcher = _FakeFetcher({f"{_ORIGIN}/{lib_path}": font})
    html = (
        "<style>@font-face{font-family:'Source Serif 4';"
        f'src:url("{_ORIGIN}/{lib_path}") format("woff2")}}</style>'
    )

    out = await export_artifact_html(html=html, mode="inline", sub_map=_map(asset), fetcher=fetcher)

    assert "data:font/woff2;base64," in out
    assert _ORIGIN not in out


@pytest.mark.asyncio
async def test_inline_sri_mismatch_raises() -> None:
    asset = _asset("lib/chartjs@4.4.7/chart.umd.js", "script", sri=compute_sri(b"the-pinned-bytes"))
    fetcher = _FakeFetcher({f"{_ORIGIN}/lib/chartjs@4.4.7/chart.umd.js": b"DIFFERENT bytes"})
    html = f'<script src="{_ORIGIN}/lib/chartjs@4.4.7/chart.umd.js"></script>'

    with pytest.raises(ArtifactExportError):
        await export_artifact_html(html=html, mode="inline", sub_map=_map(asset), fetcher=fetcher)


@pytest.mark.asyncio
async def test_inline_katex_chartjs_zero_origin_refs() -> None:
    katex_js = b"window.katex={render(){}};"
    chart_js = b"window.Chart=function(){};"
    katex_css = b'.katex{font-family:"KaTeX_Main"}'
    cdn_css = "https://cdn.jsdelivr.net/npm/katex@0.16.47/dist/katex.min.css"
    sub_map = _map(
        _asset("lib/katex@0.16.47/katex.min.js", "script", sri=compute_sri(katex_js)),
        _asset(
            "lib/katex@0.16.47/katex.min.css",
            "style",
            sri=compute_sri(katex_css),
            cdn=cdn_css,
            cors=True,
        ),
        _asset("lib/chartjs@4.4.7/chart.umd.js", "script", sri=compute_sri(chart_js)),
    )
    fetcher = _FakeFetcher(
        {
            f"{_ORIGIN}/lib/katex@0.16.47/katex.min.js": katex_js,
            cdn_css: katex_css,
            f"{_ORIGIN}/lib/chartjs@4.4.7/chart.umd.js": chart_js,
        }
    )
    html = (
        "<html><head>"
        f'<link rel="stylesheet" href="{_ORIGIN}/lib/katex@0.16.47/katex.min.css">'
        f'<script src="{_ORIGIN}/lib/katex@0.16.47/katex.min.js"></script>'
        f'<script src="{_ORIGIN}/lib/chartjs@4.4.7/chart.umd.js"></script>'
        "</head><body><canvas></canvas></body></html>"
    )

    out = await export_artifact_html(html=html, mode="inline", sub_map=sub_map, fetcher=fetcher)

    assert "artifacts.frenchforet.com" not in out
    assert "window.katex" in out
    assert "window.Chart" in out


@pytest.mark.asyncio
async def test_unmapped_lib_ref_fails_closed() -> None:
    fetcher = _FakeFetcher({})
    html = f'<script src="{_ORIGIN}/lib/foo@9.9.9/bar.js"></script>'

    with pytest.raises(ArtifactExportError):
        await export_artifact_html(html=html, mode="inline", sub_map=_map(), fetcher=fetcher)


# ---------------------------------------------------------------------------
# Substitute mode
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_substitute_script_rewrites_to_cdn_with_sri_and_crossorigin() -> None:
    cdn = "https://cdn.jsdelivr.net/npm/chart.js@4.4.7/dist/chart.umd.js"
    sri = "sha384-deadbeef"
    asset = _asset("lib/chartjs@4.4.7/chart.umd.js", "script", sri=sri, cdn=cdn, cors=True)
    fetcher = _FakeFetcher({})  # substitute never fetches
    html = f'<script src="{_ORIGIN}/lib/chartjs@4.4.7/chart.umd.js"></script>'

    out = await export_artifact_html(
        html=html, mode="substitute", sub_map=_map(asset), fetcher=fetcher
    )

    assert cdn in out
    assert f'integrity="{sri}"' in out
    assert 'crossorigin="anonymous"' in out
    assert _ORIGIN not in out
    assert fetcher.calls == []


@pytest.mark.asyncio
async def test_substitute_does_not_duplicate_existing_integrity() -> None:
    cdn = "https://cdn.jsdelivr.net/npm/chart.js@4.4.7/dist/chart.umd.js"
    asset = _asset("lib/chartjs@4.4.7/chart.umd.js", "script", sri="sha384-x", cdn=cdn, cors=True)
    fetcher = _FakeFetcher({})
    html = (
        f'<script src="{_ORIGIN}/lib/chartjs@4.4.7/chart.umd.js" '
        'integrity="sha384-existing" crossorigin="anonymous"></script>'
    )

    out = await export_artifact_html(
        html=html, mode="substitute", sub_map=_map(asset), fetcher=fetcher
    )

    assert out.count("integrity=") == 1
    assert out.count("crossorigin=") == 1
    assert "sha384-existing" in out


@pytest.mark.asyncio
async def test_substitute_threejs_and_font_fall_back_to_inline() -> None:
    three = b"window.THREE={REVISION:171};"
    font = b"\x00jetbrains\x00"
    three_path = "lib/three@0.171.0/three.iife.min.js"
    font_path = "lib/fonts/jetbrains-mono@2.304/jetbrains-mono.woff2"
    sub_map = _map(
        _asset(three_path, "script", sri=compute_sri(three), cdn=None, cors=False),
        _asset(font_path, "font", sri=compute_sri(font), cdn=None, cors=False),
    )
    fetcher = _FakeFetcher({f"{_ORIGIN}/{three_path}": three, f"{_ORIGIN}/{font_path}": font})
    html = (
        f'<script src="{_ORIGIN}/{three_path}"></script>'
        f"<style>@font-face{{src:url({_ORIGIN}/{font_path})}}</style>"
    )

    out = await export_artifact_html(html=html, mode="substitute", sub_map=sub_map, fetcher=fetcher)

    assert "window.THREE" in out  # three.js inlined
    assert "data:font/woff2;base64," in out  # font inlined
    assert "integrity=" not in out  # nothing substituted
    assert _ORIGIN not in out


# ---------------------------------------------------------------------------
# Real config map shape
# ---------------------------------------------------------------------------


def test_load_real_substitution_map_shape() -> None:
    sub_map = load_substitution_map()
    assert sub_map.origin == _ORIGIN

    katex_css = sub_map.by_lib_path["lib/katex@0.16.47/katex.min.css"]
    assert katex_css.public_cdn_url is not None
    assert katex_css.cors_verified is True
    assert katex_css.sri.startswith("sha384-")

    three = sub_map.by_lib_path["lib/three@0.171.0/three.iife.min.js"]
    assert three.public_cdn_url is None  # inline-only
    assert three.cors_verified is False

    font = sub_map.by_lib_path["lib/fonts/jetbrains-mono@2.304/jetbrains-mono.woff2"]
    assert font.public_cdn_url is None
    assert font.cors_verified is False
