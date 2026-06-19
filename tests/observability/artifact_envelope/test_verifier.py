"""Verifier contract tests for the served artifact envelope (FRE-512 / ADR-0089 D5).

These are the layer-1 "served-response tests": they drive ``verify_envelope`` with
synthetic served responses (status + headers only — never bytes) and assert that
every envelope-failure class is flagged. They pin the expected-envelope spec to the
exact ADR-0089 D2 policy deployed by the Worker (FRE-509,
``personal_agent_secrets`` ``artifacts.js`` — cross-repo seam: if the Worker CSP
changes, ``spec.py`` and these tests change in lockstep).
"""

from __future__ import annotations

from personal_agent.observability.artifact_envelope.spec import (
    DEFAULT_LIB_MANIFEST_PATH,
    EXECUTABLE_SCRIPT_MIMES,
    EXPECTED_CSP_DIRECTIVES,
    EXPECTED_FONT_MIMES,
    FORBIDDEN_SCRIPT_MIMES,
    LIB_KIND_CSP_DIRECTIVE,
    LibAsset,
    load_lib_manifest,
)
from personal_agent.observability.artifact_envelope.verifier import (
    classify_access_denied,
    parse_csp,
    verify_envelope,
    verify_lib_asset,
)

ARTIFACT_ORIGIN = "https://artifacts.frenchforet.com"

# The exact policy the Worker serves (FRE-509). Token order within a directive is
# CSP-insignificant; this string mirrors the deployed constant.
GOOD_CSP = (
    "default-src 'none'; "
    "script-src https://artifacts.frenchforet.com 'unsafe-inline'; "
    "style-src https://artifacts.frenchforet.com 'unsafe-inline'; "
    "img-src https://artifacts.frenchforet.com data:; "
    "font-src https://artifacts.frenchforet.com data:; "
    "connect-src 'none'; "
    "worker-src 'none'; "
    "form-action 'none'; "
    "base-uri 'none'; "
    "frame-ancestors https://agent.frenchforet.com; "
    "webrtc 'block'; "
    "sandbox allow-scripts"
)


def good_headers() -> list[tuple[str, str]]:
    """A fully-correct served response header set."""
    return [
        ("Content-Security-Policy", GOOD_CSP),
        ("Content-Type", "text/html; charset=utf-8"),
        ("X-Content-Type-Options", "nosniff"),
    ]


def _replace(headers: list[tuple[str, str]], name: str, value: str) -> list[tuple[str, str]]:
    return [(n, value if n.lower() == name.lower() else v) for n, v in headers]


def _drop(headers: list[tuple[str, str]], name: str) -> list[tuple[str, str]]:
    return [(n, v) for n, v in headers if n.lower() != name.lower()]


class TestHappyPath:
    def test_exact_envelope_passes(self) -> None:
        report = verify_envelope(200, good_headers(), expect_html=True)
        assert report.envelope_ok is True
        assert report.failures == ()
        assert report.csp_present is True
        assert report.mime_ok is True
        assert report.nosniff_ok is True
        assert report.http_status == 200

    def test_token_order_within_directive_is_insignificant(self) -> None:
        reordered = GOOD_CSP.replace(
            "script-src https://artifacts.frenchforet.com 'unsafe-inline'",
            "script-src 'unsafe-inline' https://artifacts.frenchforet.com",
        )
        headers = _replace(good_headers(), "Content-Security-Policy", reordered)
        assert verify_envelope(200, headers, expect_html=True).envelope_ok is True

    def test_header_names_case_insensitive(self) -> None:
        headers = [
            ("content-security-policy", GOOD_CSP),
            ("CONTENT-TYPE", "text/html; charset=utf-8"),
            ("x-content-type-options", "nosniff"),
        ]
        assert verify_envelope(200, headers, expect_html=True).envelope_ok is True

    def test_charset_casing_passes(self) -> None:
        """text/html; charset=UTF-8 is semantically equivalent (codex finding 7)."""
        headers = _replace(good_headers(), "Content-Type", "text/html; charset=UTF-8")
        report = verify_envelope(200, headers, expect_html=True)
        assert report.mime_ok is True
        assert report.envelope_ok is True

    def test_trailing_semicolon_and_whitespace_pass(self) -> None:
        headers = _replace(good_headers(), "Content-Security-Policy", f"  {GOOD_CSP} ; ")
        assert verify_envelope(200, headers, expect_html=True).envelope_ok is True

    def test_mapping_headers_accepted(self) -> None:
        """A plain dict (single-valued headers) is accepted alongside pair lists."""
        report = verify_envelope(200, dict(good_headers()), expect_html=True)
        assert report.envelope_ok is True


class TestCspFailures:
    def test_absent_csp_is_flagged(self) -> None:
        headers = _drop(good_headers(), "Content-Security-Policy")
        report = verify_envelope(200, headers, expect_html=True)
        assert report.envelope_ok is False
        assert report.csp_present is False
        assert "missing_csp" in report.failures

    def test_each_missing_directive_is_flagged(self) -> None:
        for directive in EXPECTED_CSP_DIRECTIVES:
            mutated = "; ".join(
                part
                for part in GOOD_CSP.split("; ")
                if not part.startswith(f"{directive} ") and part != directive
            )
            headers = _replace(good_headers(), "Content-Security-Policy", mutated)
            report = verify_envelope(200, headers, expect_html=True)
            assert report.envelope_ok is False, f"{directive} removal not flagged"
            assert directive in report.missing_directives
            assert "csp_directive_missing" in report.failures

    def test_mutated_sandbox_directive_is_flagged(self) -> None:
        """allow-same-origin added to sandbox = the opaque-origin guarantee gone."""
        mutated = GOOD_CSP.replace(
            "sandbox allow-scripts", "sandbox allow-scripts allow-same-origin"
        )
        headers = _replace(good_headers(), "Content-Security-Policy", mutated)
        report = verify_envelope(200, headers, expect_html=True)
        assert report.envelope_ok is False
        assert "sandbox" in report.mismatched_directives
        assert "csp_directive_mismatch" in report.failures

    def test_foreign_frame_ancestors_is_flagged(self) -> None:
        mutated = GOOD_CSP.replace(
            "frame-ancestors https://agent.frenchforet.com",
            "frame-ancestors https://evil.example.com",
        )
        headers = _replace(good_headers(), "Content-Security-Policy", mutated)
        report = verify_envelope(200, headers, expect_html=True)
        assert report.envelope_ok is False
        assert "frame-ancestors" in report.mismatched_directives

    def test_widened_connect_src_is_flagged(self) -> None:
        mutated = GOOD_CSP.replace("connect-src 'none'", "connect-src https:")
        headers = _replace(good_headers(), "Content-Security-Policy", mutated)
        report = verify_envelope(200, headers, expect_html=True)
        assert report.envelope_ok is False
        assert "connect-src" in report.mismatched_directives

    def test_unexpected_extra_directive_is_flagged(self) -> None:
        """An extra directive silently widens the default-src 'none' fallback."""
        headers = _replace(
            good_headers(),
            "Content-Security-Policy",
            GOOD_CSP + "; media-src https://x.example.com",
        )
        report = verify_envelope(200, headers, expect_html=True)
        assert report.envelope_ok is False
        assert "media-src" in report.unexpected_directives
        assert "csp_directive_unexpected" in report.failures

    def test_multiple_csp_headers_are_flagged(self) -> None:
        """Multiple CSP headers = cumulative policies ≠ the one D2 policy (codex 2)."""
        headers = good_headers() + [("Content-Security-Policy", "img-src https:")]
        report = verify_envelope(200, headers, expect_html=True)
        assert report.envelope_ok is False
        assert "multiple_csp_policies" in report.failures

    def test_duplicate_directive_is_flagged(self) -> None:
        """Browsers ignore the later duplicate — a merged-set parse would lie (codex 2)."""
        headers = _replace(
            good_headers(),
            "Content-Security-Policy",
            GOOD_CSP + "; connect-src https:",
        )
        report = verify_envelope(200, headers, expect_html=True)
        assert report.envelope_ok is False
        assert "duplicate_directive" in report.failures

    def test_report_only_header_is_not_an_enforced_csp(self) -> None:
        headers = _drop(good_headers(), "Content-Security-Policy") + [
            ("Content-Security-Policy-Report-Only", GOOD_CSP)
        ]
        report = verify_envelope(200, headers, expect_html=True)
        assert report.csp_present is False
        assert "missing_csp" in report.failures


class TestMimeAndNosniffFailures:
    def test_wrong_mime_is_flagged(self) -> None:
        headers = _replace(good_headers(), "Content-Type", "text/plain; charset=utf-8")
        report = verify_envelope(200, headers, expect_html=True)
        assert report.envelope_ok is False
        assert report.mime_ok is False
        assert "wrong_mime" in report.failures
        assert report.served_mime == "text/plain; charset=utf-8"

    def test_missing_content_type_is_flagged(self) -> None:
        headers = _drop(good_headers(), "Content-Type")
        report = verify_envelope(200, headers, expect_html=True)
        assert report.mime_ok is False
        assert report.served_mime is None
        assert "wrong_mime" in report.failures

    def test_extra_content_type_parameter_is_flagged(self) -> None:
        headers = _replace(good_headers(), "Content-Type", "text/html; charset=utf-8; foo=bar")
        report = verify_envelope(200, headers, expect_html=True)
        assert report.mime_ok is False

    def test_duplicate_content_type_is_flagged(self) -> None:
        headers = good_headers() + [("Content-Type", "text/html; charset=utf-8")]
        report = verify_envelope(200, headers, expect_html=True)
        assert report.mime_ok is False
        assert "wrong_mime" in report.failures

    def test_executable_mime_on_non_html_is_flagged(self) -> None:
        """The D2a property: an artifact URL must never serve as a script."""
        for mime in sorted(FORBIDDEN_SCRIPT_MIMES):
            headers = _replace(good_headers(), "Content-Type", f"{mime}; charset=utf-8")
            report = verify_envelope(200, headers, expect_html=False)
            assert report.envelope_ok is False, f"{mime} not flagged"
            assert "executable_mime" in report.failures

    def test_non_html_with_html_mime_passes_mime_check(self) -> None:
        """Non-HTML commits only forbid executable MIMEs; text/html is acceptable
        (FRE-509 may force text/html — reconciled at the post-deploy live check).
        """
        report = verify_envelope(200, good_headers(), expect_html=False)
        assert report.mime_ok is True

    def test_missing_nosniff_is_flagged(self) -> None:
        headers = _drop(good_headers(), "X-Content-Type-Options")
        report = verify_envelope(200, headers, expect_html=True)
        assert report.envelope_ok is False
        assert report.nosniff_ok is False
        assert "missing_nosniff" in report.failures


class TestHttpFailures:
    def test_non_access_redirect_is_http_error(self) -> None:
        """A non-Access 3xx is never silently accepted or followed (codex 8)."""
        headers = [("Location", "https://elsewhere.example.com/x")]
        report = verify_envelope(302, headers, expect_html=True)
        assert report.envelope_ok is False
        assert "http_error" in report.failures

    def test_404_is_http_error(self) -> None:
        report = verify_envelope(404, good_headers(), expect_html=True)
        assert report.envelope_ok is False
        assert "http_error" in report.failures
        assert report.http_status == 404


class TestAccessDenialClassifier:
    def test_access_login_redirect_is_denied(self) -> None:
        headers = {
            "Location": (
                "https://frenchforest.cloudflareaccess.com/cdn-cgi/access/login/"
                "artifacts.frenchforet.com?kid=abc"
            )
        }
        assert classify_access_denied(302, headers) is True

    def test_401_with_cloudflare_access_challenge_is_denied(self) -> None:
        headers = {"WWW-Authenticate": 'Cloudflare-Access resource_metadata="https://x"'}
        assert classify_access_denied(401, headers) is True
        assert classify_access_denied(403, headers) is True

    def test_plain_redirect_is_not_access_denial(self) -> None:
        assert classify_access_denied(302, {"Location": "https://elsewhere.example.com"}) is False

    def test_success_is_not_access_denial(self) -> None:
        assert classify_access_denied(200, dict(good_headers())) is False

    def test_lookalike_host_is_not_access_denial(self) -> None:
        """Suffix match must be on the host label boundary, not a substring."""
        headers = {"Location": "https://evilcloudflareaccess.com/login"}
        assert classify_access_denied(302, headers) is False


class TestParseCsp:
    def test_parses_directives_and_token_sets(self) -> None:
        policy = parse_csp("default-src 'none'; img-src https://a data:")
        assert policy.directives["default-src"] == frozenset({"'none'"})
        assert policy.directives["img-src"] == frozenset({"https://a", "data:"})
        assert policy.duplicates == ()

    def test_directive_names_lowercased(self) -> None:
        policy = parse_csp("DEFAULT-SRC 'none'")
        assert "default-src" in policy.directives

    def test_duplicates_reported_first_occurrence_kept(self) -> None:
        policy = parse_csp("connect-src 'none'; connect-src https:")
        assert policy.duplicates == ("connect-src",)
        assert policy.directives["connect-src"] == frozenset({"'none'"})


class TestResidualBound:
    """ADR-0089 D2 tier-2 residuals: asserted as *bounded*, not closed.

    Two egress channels remain open by design and are bounded — not eliminated —
    by the opaque-origin guarantee (tier 1):

    1. **Self-navigation** (``location = 'https://x/?data'`` / ``<meta refresh>``):
       no widely-supported CSP control blocks a document navigating its own
       tab/frame. Bounded because the opaque origin means the artifact only ever
       holds what was baked into it — never the session, storage, or another
       user's data.
    2. **WebRTC on browsers without ``webrtc`` directive support** (WebKit/Firefox
       may silently ignore it): ``RTCPeerConnection`` egress stays possible there.
       Same bound applies.

    The load-bearing tier-1 guarantee rests on the ``sandbox`` directive granting
    *exactly* ``allow-scripts`` — every omitted capability below is load-bearing.
    """

    def test_webrtc_block_is_in_the_spec(self) -> None:
        assert EXPECTED_CSP_DIRECTIVES["webrtc"] == frozenset({"'block'"})

    def test_sandbox_grants_exactly_allow_scripts(self) -> None:
        assert EXPECTED_CSP_DIRECTIVES["sandbox"] == frozenset({"allow-scripts"})

    def test_load_bearing_sandbox_omissions(self) -> None:
        omitted = {
            "allow-same-origin",
            "allow-popups",
            "allow-popups-to-escape-sandbox",
            "allow-top-navigation",
            "allow-top-navigation-by-user-activation",
            "allow-forms",
            "allow-downloads",
            "allow-modals",
        }
        assert EXPECTED_CSP_DIRECTIVES["sandbox"].isdisjoint(omitted)

    def test_egress_directives_are_sealed(self) -> None:
        assert EXPECTED_CSP_DIRECTIVES["connect-src"] == frozenset({"'none'"})
        assert EXPECTED_CSP_DIRECTIVES["form-action"] == frozenset({"'none'"})
        assert EXPECTED_CSP_DIRECTIVES["worker-src"] == frozenset({"'none'"})
        assert EXPECTED_CSP_DIRECTIVES["default-src"] == frozenset({"'none'"})

    def test_frame_ancestors_names_only_the_pwa_origin(self) -> None:
        assert EXPECTED_CSP_DIRECTIVES["frame-ancestors"] == frozenset(
            {"https://agent.frenchforet.com"}
        )


# ── /lib/ toolkit asset verification (FRE-527 / ADR-0089 Addendum A · A3/A7) ───
#
# The /lib/ rule is the exact inverse of the artifact rule: toolkit JS *must*
# serve an executable JS MIME (else nosniff stops the browser executing it),
# whereas an artifact must *never*. Same canonical set, opposite polarity.


def _lib_headers(content_type: str | None, *, nosniff: bool = True) -> list[tuple[str, str]]:
    """A served /lib/ asset response header set."""
    headers: list[tuple[str, str]] = []
    if content_type is not None:
        headers.append(("Content-Type", content_type))
    if nosniff:
        headers.append(("X-Content-Type-Options", "nosniff"))
    return headers


SCRIPT_ASSET = LibAsset(name="chartjs", path="chartjs@4.4.7/chart.umd.js", kind="script")
STYLE_ASSET = LibAsset(name="katex", path="katex@0.16.47/katex.min.css", kind="style")
FONT_ASSET = LibAsset(
    name="jetbrains-mono",
    path="fonts/jetbrains-mono@2.304/jetbrains-mono.woff2",
    kind="font",
)


class TestVerifyLibAssetHappyPath:
    def test_script_with_executable_mime_passes(self) -> None:
        report = verify_lib_asset(
            200, _lib_headers("text/javascript"), asset=SCRIPT_ASSET, origin=ARTIFACT_ORIGIN
        )
        assert report.asset_ok is True
        assert report.failures == ()
        assert report.mime_ok is True
        assert report.nosniff_ok is True
        assert report.csp_host_ok is True

    def test_script_application_javascript_also_passes(self) -> None:
        report = verify_lib_asset(
            200, _lib_headers("application/javascript"), asset=SCRIPT_ASSET, origin=ARTIFACT_ORIGIN
        )
        assert report.asset_ok is True

    def test_style_text_css_passes(self) -> None:
        report = verify_lib_asset(
            200, _lib_headers("text/css"), asset=STYLE_ASSET, origin=ARTIFACT_ORIGIN
        )
        assert report.asset_ok is True

    def test_font_woff2_passes(self) -> None:
        report = verify_lib_asset(
            200, _lib_headers("font/woff2"), asset=FONT_ASSET, origin=ARTIFACT_ORIGIN
        )
        assert report.asset_ok is True


class TestVerifyLibAssetFailures:
    def test_non_executable_script_mime_fails(self) -> None:
        report = verify_lib_asset(
            200, _lib_headers("text/plain"), asset=SCRIPT_ASSET, origin=ARTIFACT_ORIGIN
        )
        assert report.asset_ok is False
        assert "non_executable_script_mime" in report.failures

    def test_script_served_as_html_fails(self) -> None:
        report = verify_lib_asset(
            200,
            _lib_headers("text/html; charset=utf-8"),
            asset=SCRIPT_ASSET,
            origin=ARTIFACT_ORIGIN,
        )
        assert "non_executable_script_mime" in report.failures

    def test_style_wrong_mime_fails(self) -> None:
        report = verify_lib_asset(
            200, _lib_headers("text/plain"), asset=STYLE_ASSET, origin=ARTIFACT_ORIGIN
        )
        assert "wrong_mime" in report.failures

    def test_font_wrong_mime_fails(self) -> None:
        report = verify_lib_asset(
            200, _lib_headers("application/octet-stream"), asset=FONT_ASSET, origin=ARTIFACT_ORIGIN
        )
        assert "wrong_mime" in report.failures

    def test_missing_nosniff_fails(self) -> None:
        report = verify_lib_asset(
            200,
            _lib_headers("text/javascript", nosniff=False),
            asset=SCRIPT_ASSET,
            origin=ARTIFACT_ORIGIN,
        )
        assert report.nosniff_ok is False
        assert "missing_nosniff" in report.failures

    def test_missing_content_type_fails(self) -> None:
        report = verify_lib_asset(
            200, _lib_headers(None), asset=SCRIPT_ASSET, origin=ARTIFACT_ORIGIN
        )
        assert report.served_mime is None
        assert report.asset_ok is False

    def test_http_error_fails(self) -> None:
        report = verify_lib_asset(
            404, _lib_headers("text/javascript"), asset=SCRIPT_ASSET, origin=ARTIFACT_ORIGIN
        )
        assert "http_error" in report.failures

    def test_origin_outside_csp_directive_fails(self) -> None:
        report = verify_lib_asset(
            200, _lib_headers("text/javascript"), asset=SCRIPT_ASSET, origin="https://evil.example"
        )
        assert report.csp_host_ok is False
        assert "csp_host_not_allowed" in report.failures


class TestPolarity:
    """The same JS MIME passes for /lib/ and fails for an artifact — one set, two surfaces."""

    def test_js_mime_passes_lib_and_fails_artifact(self) -> None:
        js_mime = "text/javascript"
        lib_report = verify_lib_asset(
            200, _lib_headers(js_mime), asset=SCRIPT_ASSET, origin=ARTIFACT_ORIGIN
        )
        assert lib_report.asset_ok is True

        artifact_headers = [
            ("Content-Security-Policy", GOOD_CSP),
            ("Content-Type", js_mime),
            ("X-Content-Type-Options", "nosniff"),
        ]
        artifact_report = verify_envelope(200, artifact_headers, expect_html=True)
        assert artifact_report.envelope_ok is False
        assert "executable_mime" in artifact_report.failures

    def test_forbidden_alias_is_the_executable_set(self) -> None:
        assert FORBIDDEN_SCRIPT_MIMES is EXECUTABLE_SCRIPT_MIMES


class TestLibManifest:
    def test_committed_manifest_parses(self) -> None:
        origin, assets = load_lib_manifest(DEFAULT_LIB_MANIFEST_PATH)
        assert origin == ARTIFACT_ORIGIN
        assert len(assets) >= 1
        assert all(isinstance(a, LibAsset) for a in assets)

    def test_every_asset_kind_is_valid(self) -> None:
        _, assets = load_lib_manifest(DEFAULT_LIB_MANIFEST_PATH)
        assert all(a.kind in LIB_KIND_CSP_DIRECTIVE for a in assets)

    def test_font_assets_have_a_known_extension(self) -> None:
        from pathlib import PurePosixPath

        _, assets = load_lib_manifest(DEFAULT_LIB_MANIFEST_PATH)
        for asset in assets:
            if asset.kind == "font":
                assert PurePosixPath(asset.path).suffix in EXPECTED_FONT_MIMES

    def test_manifest_origin_admitted_by_every_used_directive(self) -> None:
        origin, assets = load_lib_manifest(DEFAULT_LIB_MANIFEST_PATH)
        used_directives = {LIB_KIND_CSP_DIRECTIVE[a.kind] for a in assets}
        for directive in used_directives:
            assert origin in EXPECTED_CSP_DIRECTIVES[directive]

    def test_loader_rejects_unknown_kind(self, tmp_path: object) -> None:
        import json
        from pathlib import Path

        bad = Path(str(tmp_path)) / "bad.json"
        bad.write_text(
            json.dumps(
                {
                    "origin": ARTIFACT_ORIGIN,
                    "assets": [{"name": "x", "path": "x@1/x.js", "kind": "wasm"}],
                }
            )
        )
        try:
            load_lib_manifest(bad)
        except ValueError:
            return
        raise AssertionError("expected ValueError for unknown kind")
