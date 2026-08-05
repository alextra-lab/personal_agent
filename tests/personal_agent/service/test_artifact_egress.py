"""Artifact requests traverse the Caddy egress block (ADR-0132 D1, FRE-1144).

The point of the cutover is not merely that the application stops sending a
Cloudflare credential — it is that the request still reaches an Access-gated
origin, by going through the proxy that holds one. Asserting only the absence of
headers would pass just as happily on a build that had lost its route entirely.
These tests assert the positive half: the URL actually requested is the egress
URL, with path and query preserved.
"""

from __future__ import annotations

import pytest

from personal_agent.service.artifact_egress import to_artifacts_egress_url

_EGRESS = "http://caddy:8601"


class TestToArtifactsEgressUrl:
    """The origin is swapped for the egress base; everything else survives."""

    @pytest.mark.parametrize(
        ("public_url", "expected"),
        [
            (
                "https://artifacts.example.com/abc-123",
                "http://caddy:8601/abc-123",
            ),
            (
                "https://artifacts.example.com/lib/katex@0.16.47/katex.min.css",
                "http://caddy:8601/lib/katex@0.16.47/katex.min.css",
            ),
            (
                "https://artifacts.example.com/abc-123?mode=export&v=2",
                "http://caddy:8601/abc-123?mode=export&v=2",
            ),
            (
                "https://artifacts.example.com/",
                "http://caddy:8601/",
            ),
        ],
    )
    def test_rewrites_origin_preserving_the_rest(self, public_url: str, expected: str) -> None:
        """Scheme and authority move to the egress; path, query and fragment do not."""
        assert to_artifacts_egress_url(public_url, egress_base_url=_EGRESS) == expected

    def test_trailing_slash_on_egress_base_does_not_double_up(self) -> None:
        """A trailing slash in configuration must not produce a `//` path."""
        assert (
            to_artifacts_egress_url(
                "https://artifacts.example.com/abc-123", egress_base_url="http://caddy:8601/"
            )
            == "http://caddy:8601/abc-123"
        )

    def test_unset_egress_base_returns_url_unchanged(self) -> None:
        """Deployments with no Cloudflare barrier address the origin directly.

        One custody mode, not two: where there is no proxy there is also no
        credential, so nothing needs rewriting.
        """
        url = "https://artifacts.example.com/abc-123"

        assert to_artifacts_egress_url(url, egress_base_url=None) == url
        assert to_artifacts_egress_url(url, egress_base_url="") == url

    def test_reads_settings_when_no_override_given(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The egress base is read at call time so a config reload is honoured."""
        from personal_agent.service import artifact_egress

        monkeypatch.setattr(artifact_egress.settings, "artifacts_egress_base_url", _EGRESS)

        assert (
            to_artifacts_egress_url("https://artifacts.example.com/abc-123")
            == "http://caddy:8601/abc-123"
        )


class TestExportFetcherRoutesThroughEgress:
    """The export fetcher checks the allowlist first, then rewrites."""

    def _fetcher(self, *, egress: str | None = _EGRESS) -> object:
        from personal_agent.service.artifacts_router import _HttpAssetFetcher

        return _HttpAssetFetcher(
            origin_host="artifacts.example.com",
            allowed_hosts=frozenset({"artifacts.example.com", "cdn.example.net"}),
            egress_base_url=egress,
        )

    def test_origin_url_is_rewritten_onto_the_egress(self) -> None:
        """An artifacts-origin fetch is sent to Caddy, not to the origin."""
        fetcher = self._fetcher()

        assert (
            fetcher._to_egress_url("https://artifacts.example.com/a.css")  # type: ignore[attr-defined]
            == "http://caddy:8601/a.css"
        )

    @pytest.mark.asyncio
    async def test_disallowed_host_is_refused_before_any_rewrite(self) -> None:
        """The SSRF allowlist sees the REAL target, not the proxy.

        Rewriting before the check would replace every host with the proxy's and
        make an arbitrary URL look allowed — the check must run first.
        """
        from personal_agent.storage.artifact_export import ArtifactExportError

        fetcher = self._fetcher()

        with pytest.raises(ArtifactExportError, match="disallowed host"):
            await fetcher.fetch("https://evil.example.org/steal")  # type: ignore[attr-defined]
