"""Unit tests for the FRE-531 E2E artifact-fixture builder.

The builder (``scripts/build_e2e_artifact_fixtures.py``) produces the inputs the
Playwright render harness consumes: a ``/lib/`` mirror, a hosted-style artifact
that references it, and the **real** ``export_artifact_html(mode="inline")``
standalone. These tests drive the builder with a synthetic substitution map and
a fake fetcher (SRIs computed from the fixture bytes) — no network, no R2.

They lock two invariants:
  * the CSP header template the harness serves is byte-derived from the single
    Python source of truth (``EXPECTED_CSP_DIRECTIVES``), eval-free; and
  * the produced standalone is self-contained (no ``/lib/`` refs survive; fonts
    inline as ``data:`` URIs) while the ``/lib/`` mirror is written for the
    hosted-render scenario.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from scripts.build_e2e_artifact_fixtures import (
    ARTIFACTS_ORIGIN,
    FixtureSpec,
    build_csp_header_template,
    build_fixtures,
    parse_csp_header,
)

from personal_agent.observability.artifact_envelope.spec import EXPECTED_CSP_DIRECTIVES
from personal_agent.storage.artifact_export import (
    ArtifactExportError,
    LibAsset,
    SubstitutionMap,
    compute_sri,
)

_ORIGIN = ARTIFACTS_ORIGIN

# Synthetic library bytes — small but shaped enough to exercise the transform.
_KATEX_JS = b"window.katex={render:function(){}};"
_KATEX_CSS = b"@font-face{font-family:KaTeX_Main;src:url(fonts/KaTeX_Test.woff2) format('woff2')}"
_CHART_JS = b"window.Chart=function(){};Chart.getChart=function(){return null};"
_FONT = b"wOF2\x00\x01synthetic-font-bytes"

_KATEX_JS_PATH = "lib/katex@0.16.47/katex.min.js"
_KATEX_CSS_PATH = "lib/katex@0.16.47/katex.min.css"
_CHART_JS_PATH = "lib/chartjs@4.4.7/chart.umd.js"

_CDN_BASE = "https://cdn.jsdelivr.net/npm"
_KATEX_CSS_CDN = f"{_CDN_BASE}/katex@0.16.47/dist/katex.min.css"
_FONT_CDN = f"{_CDN_BASE}/katex@0.16.47/dist/fonts/KaTeX_Test.woff2"


class _FakeFetcher:
    """AssetFetcher backed by a dict; raises on an unknown URL (like the real one)."""

    def __init__(self, mapping: dict[str, bytes]) -> None:
        self._mapping = mapping
        self.calls: list[str] = []

    async def fetch(self, url: str) -> bytes:
        self.calls.append(url)
        try:
            return self._mapping[url]
        except KeyError as exc:  # pragma: no cover - defensive
            raise ArtifactExportError(f"fake fetcher: no bytes for {url}") from exc


def _asset(lib_path: str, kind: str, data: bytes, *, cdn: str | None) -> LibAsset:
    return LibAsset(
        lib_path=lib_path,
        name=lib_path.split("/")[-1],
        kind=kind,
        loading="classic",
        public_cdn_url=cdn,
        sri=compute_sri(data),
        cors_verified=cdn is not None,
        fallback="inline",
    )


def _sub_map() -> SubstitutionMap:
    return SubstitutionMap(
        origin=_ORIGIN,
        by_lib_path={
            _KATEX_JS_PATH: _asset(
                _KATEX_JS_PATH,
                "script",
                _KATEX_JS,
                cdn=f"{_CDN_BASE}/katex@0.16.47/dist/katex.min.js",
            ),
            _KATEX_CSS_PATH: _asset(_KATEX_CSS_PATH, "style", _KATEX_CSS, cdn=_KATEX_CSS_CDN),
            _CHART_JS_PATH: _asset(
                _CHART_JS_PATH,
                "script",
                _CHART_JS,
                cdn=f"{_CDN_BASE}/chart.js@4.4.7/dist/chart.umd.js",
            ),
        },
    )


def _spec() -> FixtureSpec:
    artifact_html = (
        "<!doctype html><html><head>"
        f'<link rel="stylesheet" href="/{_KATEX_CSS_PATH}">'
        f'<script src="/{_KATEX_JS_PATH}"></script>'
        f'<script src="/{_CHART_JS_PATH}"></script>'
        "</head><body><div id=f></div><canvas id=c></canvas></body></html>"
    )
    pagedjs_html = "<!doctype html><html><body><p>paged</p></body></html>"
    return FixtureSpec(
        artifact_html=artifact_html,
        pagedjs_html=pagedjs_html,
        mirror_lib_paths=(_KATEX_CSS_PATH, _KATEX_JS_PATH, _CHART_JS_PATH),
        katex_formula_tex="E = mc^2",
        chart_data=(3, 1, 4, 1, 5, 9, 2, 6),
    )


def _fetcher_with_correct_bytes() -> _FakeFetcher:
    # Both the mirror writer and export request the *hosted source*: the CDN twin
    # for a style asset, the artifacts origin for a script. Fonts come from the
    # CDN base (resolved relative to the style's CDN source).
    return _FakeFetcher(
        {
            f"{_ORIGIN}/{_KATEX_JS_PATH}": _KATEX_JS,
            f"{_ORIGIN}/{_CHART_JS_PATH}": _CHART_JS,
            _KATEX_CSS_CDN: _KATEX_CSS,
            _FONT_CDN: _FONT,
        }
    )


# ---------------------------------------------------------------------------
# CSP header template — single source of truth (EXPECTED_CSP_DIRECTIVES)
# ---------------------------------------------------------------------------


def test_csp_header_template_is_eval_free_and_matches_spec() -> None:
    """The emitted CSP template is the exact spec directive set, eval-free, with {ORIGIN}."""
    template = build_csp_header_template()

    # The artifacts host token is replaced by the {ORIGIN} placeholder.
    assert "{ORIGIN}" in template
    assert ARTIFACTS_ORIGIN not in template

    parsed = parse_csp_header(template.replace("{ORIGIN}", ARTIFACTS_ORIGIN))
    assert parsed == {k: set(v) for k, v in EXPECTED_CSP_DIRECTIVES.items()}

    # The eval-free property the paged.js gate depends on.
    assert "'unsafe-eval'" not in template
    assert parsed["script-src"] == {ARTIFACTS_ORIGIN, "'unsafe-inline'"}
    assert parsed["sandbox"] == {"allow-scripts"}


# ---------------------------------------------------------------------------
# build_fixtures — mirror + self-contained standalone
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_build_fixtures_writes_mirror_and_self_contained_standalone(tmp_path: Path) -> None:
    """Builds the /lib/ mirror (fonts beside the CSS) + a self-contained inline standalone."""
    manifest = await build_fixtures(
        out_dir=tmp_path, sub_map=_sub_map(), fetcher=_fetcher_with_correct_bytes(), spec=_spec()
    )

    # /lib/ mirror is written for the hosted-render scenario, fonts beside the CSS.
    assert (tmp_path / _KATEX_JS_PATH).read_bytes() == _KATEX_JS
    assert (tmp_path / _KATEX_CSS_PATH).read_bytes() == _KATEX_CSS
    assert (tmp_path / _CHART_JS_PATH).read_bytes() == _CHART_JS
    assert (tmp_path / "lib/katex@0.16.47/fonts/KaTeX_Test.woff2").read_bytes() == _FONT

    # The standalone is self-contained: no /lib/ refs survive, libs inlined,
    # the font baked as a data: URI.
    standalone = (tmp_path / "standalone.html").read_text(encoding="utf-8")
    assert "/lib/" not in standalone
    assert "<style>" in standalone and "<script>" in standalone
    assert "data:font/woff2;base64," in standalone
    assert "url(fonts/KaTeX_Test.woff2)" not in standalone

    # The hosted artifact + paged.js fixtures are written verbatim.
    assert (tmp_path / "artifact.html").read_text(encoding="utf-8").startswith("<!doctype html>")
    assert (tmp_path / "pagedjs.html").exists()

    # The build manifest drives the harness (CSP source of truth + paths + data).
    on_disk = json.loads((tmp_path / "build-manifest.json").read_text(encoding="utf-8"))
    assert on_disk == manifest
    assert "{ORIGIN}" in manifest["csp_header_template"]
    assert manifest["artifact"] == "artifact.html"
    assert manifest["standalone"] == "standalone.html"
    assert manifest["pagedjs"] == "pagedjs.html"
    assert manifest["katex_formula_tex"] == "E = mc^2"
    assert manifest["chart_data"] == [3, 1, 4, 1, 5, 9, 2, 6]


@pytest.mark.asyncio
async def test_build_fixtures_fails_closed_on_sri_mismatch(tmp_path: Path) -> None:
    """A tampered asset that fails the pinned-SRI check aborts the build."""
    tampered = _fetcher_with_correct_bytes()
    tampered._mapping[f"{_ORIGIN}/{_CHART_JS_PATH}"] = b"tampered-chart-bytes"

    with pytest.raises(ArtifactExportError, match="SRI mismatch"):
        await build_fixtures(out_dir=tmp_path, sub_map=_sub_map(), fetcher=tampered, spec=_spec())
