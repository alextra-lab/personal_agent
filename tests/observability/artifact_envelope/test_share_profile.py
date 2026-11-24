"""Share-hostname profile of the envelope verifier (FRE-1783, ADR-0153 Amendment 1 L7/L8, V-L4).

The fixture responses in ``fixtures/share_responses.json`` hold the header sets that L7
specifies for each kind. Every fixture must pass, and each seeded negative (one wrong
directive, one missing header, a cookie) must fail with its stable failure code.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import cast

import pytest

from personal_agent.observability.artifact_envelope.spec import (
    SHARE_REQUIRED_HEADERS,
    ShareCspKind,
    expected_share_csp_directives,
)
from personal_agent.observability.artifact_envelope.verifier import (
    ShareEnvelopeReport,
    ShareLibReport,
    verify_share_lib_response,
    verify_share_response,
)
from personal_agent.storage.artifact_export import compute_sri

_FIXTURES = json.loads(
    (Path(__file__).parent / "fixtures" / "share_responses.json").read_text(encoding="utf-8")
)
SHARE_ORIGIN: str = _FIXTURES["share_origin"]
RESPONSES: dict[str, dict[str, object]] = _FIXTURES["responses"]

Pairs = list[tuple[str, str]]


def _fixture(name: str) -> tuple[ShareCspKind, int, Pairs]:
    entry = RESPONSES[name]
    headers = [(str(n), str(v)) for n, v in cast(list[list[str]], entry["headers"])]
    return cast(ShareCspKind, entry["kind"]), cast(int, entry["status"]), headers


def _replace(headers: Pairs, name: str, value: str) -> Pairs:
    return [(n, value if n.lower() == name.lower() else v) for n, v in headers]


def _drop(headers: Pairs, name: str) -> Pairs:
    return [(n, v) for n, v in headers if n.lower() != name.lower()]


def _csp(headers: Pairs) -> str:
    return next(v for n, v in headers if n.lower() == "content-security-policy")


def _verify(
    name: str, headers: Pairs | None = None, *, kind: ShareCspKind | None = None
) -> ShareEnvelopeReport:
    fixture_kind, status, fixture_headers = _fixture(name)
    return verify_share_response(
        status,
        fixture_headers if headers is None else headers,
        kind=kind or fixture_kind,
        share_origin=SHARE_ORIGIN,
        expected_status=status,
    )


class TestExpectedPolicy:
    def test_html_policy_matches_adr_l7(self) -> None:
        lib = f"{SHARE_ORIGIN}/lib/"
        assert dict(expected_share_csp_directives("html", SHARE_ORIGIN)) == {
            "default-src": frozenset({"'none'"}),
            "script-src": frozenset({lib, "'unsafe-inline'"}),
            "style-src": frozenset({lib, "'unsafe-inline'"}),
            "img-src": frozenset({lib, "data:"}),
            "font-src": frozenset({lib, "data:"}),
            "connect-src": frozenset({"'none'"}),
            "worker-src": frozenset({"'none'"}),
            "form-action": frozenset({"'none'"}),
            "base-uri": frozenset({"'none'"}),
            "frame-ancestors": frozenset({"'none'"}),
            "webrtc": frozenset({"'block'"}),
            "sandbox": frozenset({"allow-scripts"}),
        }

    def test_svg_policy_is_html_policy_without_host_source(self) -> None:
        html = expected_share_csp_directives("html", SHARE_ORIGIN)
        svg = expected_share_csp_directives("svg", SHARE_ORIGIN)
        assert set(svg) == set(html)
        for directive, tokens in svg.items():
            assert tokens == {t for t in html[directive] if not t.startswith("https://")}

    def test_media_policy_has_no_sandbox(self) -> None:
        assert dict(expected_share_csp_directives("media", SHARE_ORIGIN)) == {
            "default-src": frozenset({"'none'"}),
            "frame-ancestors": frozenset({"'none'"}),
            "form-action": frozenset({"'none'"}),
            "base-uri": frozenset({"'none'"}),
        }

    def test_trailing_slash_on_origin_is_normalized(self) -> None:
        assert expected_share_csp_directives("html", f"{SHARE_ORIGIN}/") == (
            expected_share_csp_directives("html", SHARE_ORIGIN)
        )

    def test_share_headers_match_adr_l7(self) -> None:
        assert dict(SHARE_REQUIRED_HEADERS) == {
            "x-robots-tag": ("noindex", "nofollow"),
            "referrer-policy": ("no-referrer",),
            "cache-control": ("private", "no-store"),
            "x-content-type-options": ("nosniff",),
        }


class TestFixturesPass:
    @pytest.mark.parametrize("name", sorted(RESPONSES))
    def test_each_recorded_response_passes(self, name: str) -> None:
        report = _verify(name)
        assert report.failures == ()
        assert report.envelope_ok is True

    def test_token_order_and_header_case_are_insignificant(self) -> None:
        _, _, headers = _fixture("html_200")
        lowered = [(n.lower(), v) for n, v in headers]
        reordered = _replace(lowered, "cache-control", "no-store, private")
        assert _verify("html_200", reordered).envelope_ok is True


class TestSeededNegatives:
    """One wrong value must fail. These are the seeded negatives for V-L4."""

    def test_one_wrong_directive_fails_html(self) -> None:
        _, _, headers = _fixture("html_200")
        wrong = _csp(headers).replace(
            "frame-ancestors 'none'", "frame-ancestors https://agent.example.com"
        )
        report = _verify("html_200", _replace(headers, "Content-Security-Policy", wrong))
        assert report.envelope_ok is False
        assert report.mismatched_directives == ("frame-ancestors",)
        assert "csp_directive_mismatch" in report.failures

    def test_host_source_not_path_scoped_fails(self) -> None:
        _, _, headers = _fixture("html_200")
        wrong = _csp(headers).replace(
            f"script-src {SHARE_ORIGIN}/lib/", f"script-src {SHARE_ORIGIN}"
        )
        report = _verify("html_200", _replace(headers, "Content-Security-Policy", wrong))
        assert report.mismatched_directives == ("script-src",)

    def test_artifact_host_in_place_of_share_host_fails(self) -> None:
        _, _, headers = _fixture("not_found_404")
        wrong = _csp(headers).replace(SHARE_ORIGIN, "https://artifacts.example.com")
        report = _verify("not_found_404", _replace(headers, "Content-Security-Policy", wrong))
        assert set(report.mismatched_directives) == {
            "script-src",
            "style-src",
            "img-src",
            "font-src",
        }

    def test_svg_with_host_source_fails(self) -> None:
        _, _, headers = _fixture("svg_200")
        wrong = _csp(headers).replace("img-src data:", f"img-src {SHARE_ORIGIN}/lib/ data:")
        report = _verify("svg_200", _replace(headers, "Content-Security-Policy", wrong))
        assert report.mismatched_directives == ("img-src",)

    def test_media_with_sandbox_fails(self) -> None:
        _, _, headers = _fixture("pdf_200")
        wrong = _csp(headers) + "; sandbox"
        report = _verify("pdf_200", _replace(headers, "Content-Security-Policy", wrong))
        assert report.unexpected_directives == ("sandbox",)
        assert "csp_directive_unexpected" in report.failures

    def test_missing_directive_fails(self) -> None:
        _, _, headers = _fixture("gone_410")
        wrong = _csp(headers).replace("; webrtc 'block'", "")
        report = _verify("gone_410", _replace(headers, "Content-Security-Policy", wrong))
        assert report.missing_directives == ("webrtc",)
        assert "csp_directive_missing" in report.failures

    def test_html_policy_on_an_image_fails(self) -> None:
        """The kind is declared by the caller, never inferred from the response."""
        _, _, html_headers = _fixture("html_200")
        _, _, png_headers = _fixture("png_200")
        headers = _replace(png_headers, "Content-Security-Policy", _csp(html_headers))
        report = _verify("png_200", headers)
        assert "csp_directive_unexpected" in report.failures

    def test_absent_csp_fails(self) -> None:
        _, _, headers = _fixture("method_405")
        report = _verify("method_405", _drop(headers, "Content-Security-Policy"))
        assert "missing_csp" in report.failures

    def test_two_csp_headers_fail(self) -> None:
        _, _, headers = _fixture("html_200")
        headers = [*headers, ("Content-Security-Policy", "default-src *")]
        assert "multiple_csp_policies" in _verify("html_200", headers).failures

    def test_repeated_value_token_fails(self) -> None:
        _, _, headers = _fixture("html_200")
        wrong = _csp(headers).replace(
            f"script-src {SHARE_ORIGIN}/lib/", f"script-src {SHARE_ORIGIN}/lib/ {SHARE_ORIGIN}/lib/"
        )
        report = _verify("html_200", _replace(headers, "Content-Security-Policy", wrong))
        assert "duplicate_csp_value" in report.failures

    def test_duplicate_directive_fails(self) -> None:
        _, _, headers = _fixture("html_200")
        wrong = _csp(headers) + "; connect-src *"
        report = _verify("html_200", _replace(headers, "Content-Security-Policy", wrong))
        assert "duplicate_directive" in report.failures

    @pytest.mark.parametrize("header", sorted(SHARE_REQUIRED_HEADERS))
    def test_each_missing_share_header_fails(self, header: str) -> None:
        _, _, headers = _fixture("not_found_404")
        report = _verify("not_found_404", _drop(headers, header))
        assert report.envelope_ok is False
        assert report.missing_headers == (header,)
        assert "missing_share_header" in report.failures

    @pytest.mark.parametrize(
        ("header", "value"),
        [
            ("X-Robots-Tag", "noindex"),
            ("Referrer-Policy", "strict-origin-when-cross-origin"),
            ("Cache-Control", "public, max-age=60"),
            ("Cache-Control", "private, no-store, max-age=60"),
            ("Cache-Control", "private, private, no-store"),
        ],
    )
    def test_wrong_share_header_value_fails(self, header: str, value: str) -> None:
        _, _, headers = _fixture("png_200")
        report = _verify("png_200", _replace(headers, header, value))
        assert report.wrong_headers == (header.lower(),)
        assert "wrong_share_header" in report.failures

    def test_set_cookie_fails(self) -> None:
        _, _, headers = _fixture("gone_410")
        headers = [*headers, ("Set-Cookie", "__cf_bm=abc; HttpOnly")]
        report = _verify("gone_410", headers)
        assert report.set_cookie_present is True
        assert "set_cookie_present" in report.failures

    def test_unexpected_status_fails(self) -> None:
        kind, _, headers = _fixture("not_found_404")
        report = verify_share_response(
            200, headers, kind=kind, share_origin=SHARE_ORIGIN, expected_status=404
        )
        assert "unexpected_status" in report.failures

    @pytest.mark.parametrize(
        ("name", "mime"),
        [
            ("html_200", "text/html"),
            ("html_200", "text/plain; charset=utf-8"),
            ("svg_200", "text/html; charset=utf-8"),
            ("png_200", "image/svg+xml"),
            ("pdf_200", "application/octet-stream"),
            ("png_200", "text/javascript"),
        ],
    )
    def test_wrong_mime_fails(self, name: str, mime: str) -> None:
        _, _, headers = _fixture(name)
        report = _verify(name, _replace(headers, "Content-Type", mime))
        assert report.mime_ok is False
        assert {"wrong_mime", "executable_mime"} & set(report.failures)

    @pytest.mark.parametrize(
        "disposition",
        [
            'attachment; filename="photo.png"',
            "inline",
            'inline; filename="my photo (1).png"',
            'inline; filename="../photo.png"',
            "inline; filename=photo.png",
            'inline; filename="photo.jpg"',
            f'inline; filename="{"a" * 81}.png"',
        ],
    )
    def test_bad_content_disposition_on_200_fails(self, disposition: str) -> None:
        _, _, headers = _fixture("png_200")
        report = _verify("png_200", _replace(headers, "Content-Disposition", disposition))
        assert "bad_content_disposition" in report.failures

    def test_missing_content_disposition_on_200_fails(self) -> None:
        _, _, headers = _fixture("svg_200")
        report = _verify("svg_200", _drop(headers, "Content-Disposition"))
        assert "bad_content_disposition" in report.failures

    def test_error_page_needs_no_content_disposition(self) -> None:
        assert _verify("gone_410").envelope_ok is True


_LIB_BODY = b"/* katex */"


def _lib_headers() -> Pairs:
    """The share /lib/ 200 header set (master, 2026-11-24, agreed with the Worker session)."""
    return [
        ("Content-Type", "application/javascript; charset=utf-8"),
        ("Access-Control-Allow-Origin", "*"),
        ("Cache-Control", "private, no-store"),
        ("X-Content-Type-Options", "nosniff"),
    ]


def _verify_lib(
    headers: Pairs | None = None, body: bytes = _LIB_BODY, status: int = 200
) -> ShareLibReport:
    return verify_share_lib_response(
        status,
        _lib_headers() if headers is None else headers,
        body,
        expected_sri=compute_sri(_LIB_BODY),
    )


class TestLibResponse:
    def test_pinned_bytes_and_headers_pass(self) -> None:
        report = _verify_lib()
        assert report.failures == ()
        assert report.lib_ok is True

    def test_changed_bytes_fail(self) -> None:
        assert _verify_lib(body=b"tampered").failures == ("sri_mismatch",)

    def test_non_200_fails(self) -> None:
        assert "http_error" in _verify_lib(status=404).failures

    def test_csp_on_lib_fails(self) -> None:
        headers = [*_lib_headers(), ("Content-Security-Policy", "default-src 'none'")]
        assert "unexpected_csp" in _verify_lib(headers).failures

    @pytest.mark.parametrize("value", ["null", "https://share.example.com", "*, *"])
    def test_wrong_cors_origin_fails(self, value: str) -> None:
        headers = _replace(_lib_headers(), "Access-Control-Allow-Origin", value)
        assert "wrong_cors" in _verify_lib(headers).failures

    def test_missing_cors_origin_fails(self) -> None:
        headers = _drop(_lib_headers(), "Access-Control-Allow-Origin")
        assert "wrong_cors" in _verify_lib(headers).failures

    @pytest.mark.parametrize(
        "extra",
        [
            ("Access-Control-Allow-Credentials", "true"),
            ("Access-Control-Allow-Methods", "GET"),
            ("Access-Control-Expose-Headers", "Content-Length"),
        ],
    )
    def test_other_cors_header_fails(self, extra: tuple[str, str]) -> None:
        assert "unexpected_cors_header" in _verify_lib([*_lib_headers(), extra]).failures

    @pytest.mark.parametrize(
        ("header", "value"),
        [("Cache-Control", "public, max-age=31536000, immutable"), ("X-Content-Type-Options", "")],
    )
    def test_wrong_lib_header_fails(self, header: str, value: str) -> None:
        headers = _replace(_lib_headers(), header, value)
        assert "wrong_share_header" in _verify_lib(headers).failures

    def test_missing_nosniff_fails(self) -> None:
        headers = _drop(_lib_headers(), "X-Content-Type-Options")
        assert "missing_share_header" in _verify_lib(headers).failures

    def test_non_executable_mime_fails(self) -> None:
        headers = _replace(_lib_headers(), "Content-Type", "text/plain")
        assert "non_executable_script_mime" in _verify_lib(headers).failures

    def test_set_cookie_fails(self) -> None:
        headers = [*_lib_headers(), ("Set-Cookie", "a=b")]
        assert "set_cookie_present" in _verify_lib(headers).failures
