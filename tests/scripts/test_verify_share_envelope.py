"""CLI tests for ``scripts/verify_share_envelope.py`` (FRE-1783, ADR-0153 V-L4, V-L5).

The share hostname is served by ``httpx.MockTransport`` from the fixture responses of
``tests/observability/artifact_envelope/fixtures/share_responses.json``. No network.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest
from scripts import verify_share_envelope

from personal_agent.observability.artifact_envelope.spec import SHARE_LIB_PROBE_PATH

_FIXTURES = json.loads(
    (
        Path(__file__).resolve().parents[1]
        / "observability"
        / "artifact_envelope"
        / "fixtures"
        / "share_responses.json"
    ).read_text(encoding="utf-8")
)
ORIGIN: str = _FIXTURES["share_origin"]
ARTIFACT_ID = "0f0e0d0c-0b0a-4908-8706-050403020100"
TOKEN = "A" * 43
KATEX = b"/* pinned katex bytes */"


def _headers(name: str) -> list[tuple[str, str]]:
    return [(n, v) for n, v in _FIXTURES["responses"][name]["headers"]]


def _lib_headers() -> list[tuple[str, str]]:
    return [
        ("Content-Type", "application/javascript; charset=utf-8"),
        ("Access-Control-Allow-Origin", "*"),
        ("Cache-Control", "private, no-store"),
        ("X-Content-Type-Options", "nosniff"),
    ]


Handler = Callable[[httpx.Request], httpx.Response]


def _share_host(
    overrides: dict[str, httpx.Response] | None = None,
) -> tuple[Handler, list[httpx.Request]]:
    """A share hostname that behaves as L2/L7/L8 say, unless a path is overridden."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        path = request.url.path
        if overrides and path in overrides:
            return overrides[path]
        if path == f"/{SHARE_LIB_PROBE_PATH}":
            return httpx.Response(200, headers=_lib_headers(), content=KATEX)
        if path == f"/s/{TOKEN}":
            if request.method != "GET":
                return httpx.Response(405, headers=_headers("method_405"), content=b"<p>405</p>")
            return httpx.Response(200, headers=_headers("html_200"), content=b"<p>snapshot</p>")
        return httpx.Response(404, headers=_headers("not_found_404"), content=b"<p>404</p>")

    return handler, seen


@pytest.fixture(autouse=True)
def _pin_katex_sri(monkeypatch: pytest.MonkeyPatch) -> None:
    """Point the expected SRI at the fake bytes, as the real map points at the real ones."""
    from personal_agent.storage import artifact_export
    from personal_agent.storage.artifact_export import compute_sri

    real = artifact_export.load_substitution_map()
    entry = real.by_lib_path[SHARE_LIB_PROBE_PATH]
    fake_entry = type(entry)(**{**entry.__dict__, "sri": compute_sri(KATEX)})
    fake = type(real)(
        origin=real.origin, by_lib_path={**real.by_lib_path, entry.lib_path: fake_entry}
    )
    monkeypatch.setattr(verify_share_envelope, "load_substitution_map", lambda: fake)


def _run(argv: list[str], handler: Handler) -> int:
    with httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False) as client:
        return verify_share_envelope.run(argv, client=client)


def _boundary(handler: Handler) -> int:
    return _run(["--boundary", ORIGIN, "--artifact-id", ARTIFACT_ID], handler)


class TestOneResponse:
    def test_good_snapshot_passes(self) -> None:
        handler, _ = _share_host()
        assert _run([f"{ORIGIN}/s/{TOKEN}", "--kind", "html"], handler) == 0

    def test_post_gets_the_405_page(self) -> None:
        handler, seen = _share_host()
        argv = [f"{ORIGIN}/s/{TOKEN}", "--kind", "html", "--status", "405", "--method", "POST"]
        assert _run(argv, handler) == 0
        assert seen[0].method == "POST"

    def test_wrong_kind_fails(self) -> None:
        handler, _ = _share_host()
        assert _run([f"{ORIGIN}/s/{TOKEN}", "--kind", "svg"], handler) == 1

    def test_one_wrong_directive_fails(self) -> None:
        headers = [
            (
                n,
                v.replace("connect-src 'none'", "connect-src *")
                if n == "Content-Security-Policy"
                else v,
            )
            for n, v in _headers("html_200")
        ]
        handler, _ = _share_host({f"/s/{TOKEN}": httpx.Response(200, headers=headers)})
        assert _run([f"{ORIGIN}/s/{TOKEN}", "--kind", "html"], handler) == 1

    def test_access_redirect_fails(self) -> None:
        redirect = httpx.Response(
            302, headers={"Location": "https://team.cloudflareaccess.com/login"}
        )
        handler, _ = _share_host({f"/s/{TOKEN}": redirect})
        assert _run([f"{ORIGIN}/s/{TOKEN}", "--kind", "html"], handler) == 1

    def test_no_access_token_and_no_token_in_output(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setenv("CF_ACCESS_CLIENT_ID", "id")
        monkeypatch.setenv("CF_ACCESS_CLIENT_SECRET", "secret")
        handler, seen = _share_host()
        assert _run([f"{ORIGIN}/s/{TOKEN}", "--kind", "html"], handler) == 0
        assert not any(name.lower().startswith("cf-access") for name in seen[0].headers)
        assert TOKEN not in capsys.readouterr().out


class TestBoundary:
    def test_conforming_share_host_passes(self) -> None:
        handler, seen = _share_host()
        assert _boundary(handler) == 0
        assert {r.url.path for r in seen} == {
            f"/{SHARE_LIB_PROBE_PATH}",
            "/",
            f"/{ARTIFACT_ID}",
            f"/internal/artifacts/{ARTIFACT_ID}",
            f"/{verify_share_envelope._UNKNOWN_LIB_PATH}",
        }

    def test_changed_toolkit_bytes_fail(self) -> None:
        tampered = httpx.Response(200, headers=_lib_headers(), content=b"/* other bytes */")
        handler, _ = _share_host({f"/{SHARE_LIB_PROBE_PATH}": tampered})
        assert _boundary(handler) == 1

    def test_toolkit_with_credentials_header_fails(self) -> None:
        headers = [*_lib_headers(), ("Access-Control-Allow-Credentials", "true")]
        handler, _ = _share_host(
            {f"/{SHARE_LIB_PROBE_PATH}": httpx.Response(200, headers=headers, content=KATEX)}
        )
        assert _boundary(handler) == 1

    @pytest.mark.parametrize(
        "path",
        [
            "/",
            f"/{ARTIFACT_ID}",
            f"/internal/artifacts/{ARTIFACT_ID}",
            "/lib/katex@0.18.2/not-in-the-toolkit.js",
        ],
    )
    def test_each_listed_path_serving_content_fails(self, path: str) -> None:
        leak = httpx.Response(200, headers=_headers("html_200"), content=b"<p>private</p>")
        handler, _ = _share_host({path: leak})
        assert _boundary(handler) == 1

    def test_404_without_error_page_csp_fails(self) -> None:
        bare = [(n, v) for n, v in _headers("not_found_404") if n != "Content-Security-Policy"]
        handler, _ = _share_host({"/": httpx.Response(404, headers=bare)})
        assert _boundary(handler) == 1

    def test_boundary_needs_artifact_id(self) -> None:
        handler, _ = _share_host()
        with pytest.raises(SystemExit):
            _run(["--boundary", ORIGIN], handler)
