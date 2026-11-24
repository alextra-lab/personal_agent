#!/usr/bin/env python3
"""Live check of the share hostname's envelope (FRE-1783, ADR-0153 Amendment 1 L7, V-L4, V-L5).

Two modes, both exit 0 only when every check passes:

* One response (V-L4): GET (or ``--method POST`` for the 405 page) a share URL and
  verify its status, the four share headers, no ``Set-Cookie``, the exact CSP of
  the declared kind, the MIME and, on a 200, ``Content-Disposition``::

      uv run python scripts/verify_share_envelope.py https://share.example.com/s/<token> --kind html
      uv run python scripts/verify_share_envelope.py https://share.example.com/s/<bad> --kind html --status 404

* Boundary (V-L5, share hostname part): ``/lib/katex@0.18.2/katex.min.js`` returns
  200 with the pinned bytes (SRI from ``config/artifact_lib_substitution_map.json``)
  and the share ``/lib/`` headers; ``/``, ``/{X}``, ``/internal/artifacts/{X}`` and an
  unknown ``/lib/`` path each return the 404 page with the error-page CSP::

      uv run python scripts/verify_share_envelope.py --boundary https://share.example.com --artifact-id <X>

The share hostname is an Access bypass, so no Cloudflare Access token is ever sent:
the check sees what a fresh browser sees. Redirects are not followed. The URL is
never printed, since a share URL carries its token.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from typing import cast
from urllib.parse import quote, urlsplit

import httpx

from personal_agent.observability.artifact_envelope.spec import (
    SHARE_LIB_PROBE_PATH,
    ShareCspKind,
)
from personal_agent.observability.artifact_envelope.verifier import (
    classify_access_denied,
    verify_share_lib_response,
    verify_share_response,
)
from personal_agent.storage.artifact_export import load_substitution_map

_KINDS: tuple[ShareCspKind, ...] = ("html", "svg", "media")
#: A ``/lib/`` path that the toolkit does not hold; it must get the 404 page.
_UNKNOWN_LIB_PATH = "lib/katex@0.18.2/not-in-the-toolkit.js"


_DEFAULT_PORTS = {"https": 443, "http": 80}


def _origin_of(url: str) -> str:
    """Return the serialized origin of ``url``: lowercase, no default port.

    Raises:
        ValueError: If ``url`` has no scheme or host, or a malformed port.
    """
    parts = urlsplit(url)
    scheme = parts.scheme.lower()
    host = parts.hostname
    if not scheme or not host:
        raise ValueError("URL needs a scheme and a host")
    port = parts.port
    suffix = f":{port}" if port is not None and port != _DEFAULT_PORTS.get(scheme) else ""
    return f"{scheme}://{host}{suffix}"


def _check_one(
    client: httpx.Client,
    url: str,
    *,
    origin: str,
    kind: ShareCspKind,
    expected_status: int,
    method: str,
    label: str,
) -> bool:
    """Fetch one share URL (headers only) and print its verdict."""
    try:
        with client.stream(method, url) as response:
            status_code = int(response.status_code)
            header_pairs = list(response.headers.multi_items())
    except (httpx.HTTPError, httpx.InvalidURL) as exc:
        print(f"  FAIL  {label}  (request failed: {type(exc).__name__})")
        return False

    if classify_access_denied(status_code, header_pairs):
        print(f"  FAIL  {label}  (Cloudflare Access intercepted it, HTTP {status_code})")
        return False

    report = verify_share_response(
        status_code,
        header_pairs,
        kind=kind,
        share_origin=origin,
        expected_status=expected_status,
    )
    if report.envelope_ok:
        print(f"  OK    {label}  (HTTP {status_code}, kind {kind})")
        return True
    print(f"  FAIL  {label}  {list(report.failures)}")
    print(f"        http_status={report.http_status} expected={report.expected_status}")
    print(f"        served_mime={report.served_mime}")
    print(f"        missing_headers={list(report.missing_headers)}")
    print(f"        wrong_headers={list(report.wrong_headers)}")
    print(f"        missing_directives={list(report.missing_directives)}")
    print(f"        mismatched_directives={list(report.mismatched_directives)}")
    print(f"        unexpected_directives={list(report.unexpected_directives)}")
    print(f"        csp_header={report.csp_header}")
    return False


def _check_lib(client: httpx.Client, origin: str) -> bool:
    """Fetch the pinned toolkit file and compare it to the substitution map."""
    expected_sri = load_substitution_map().by_lib_path[SHARE_LIB_PROBE_PATH].sri
    label = f"/{SHARE_LIB_PROBE_PATH}"
    try:
        response = client.get(f"{origin}/{SHARE_LIB_PROBE_PATH}")
    except (httpx.HTTPError, httpx.InvalidURL) as exc:
        print(f"  FAIL  {label}  (request failed: {type(exc).__name__})")
        return False
    if classify_access_denied(int(response.status_code), list(response.headers.multi_items())):
        print(f"  FAIL  {label}  (Cloudflare Access intercepted it, HTTP {response.status_code})")
        return False
    report = verify_share_lib_response(
        int(response.status_code),
        list(response.headers.multi_items()),
        response.content,
        expected_sri=expected_sri,
    )
    if report.lib_ok:
        print(f"  OK    {label}  (HTTP 200, pinned bytes, {report.served_mime})")
        return True
    print(f"  FAIL  {label}  {list(report.failures)} (HTTP {report.http_status})")
    return False


def _check_boundary(client: httpx.Client, origin: str, artifact_id: str) -> bool:
    """Run the share-hostname part of V-L5; True when every path passes."""
    x = quote(artifact_id, safe="")
    not_found = {
        "/": f"{origin}/",
        "/{X}": f"{origin}/{x}",
        "/internal/artifacts/{X}": f"{origin}/internal/artifacts/{x}",
        "/lib/<unknown>": f"{origin}/{_UNKNOWN_LIB_PATH}",
    }
    results = [_check_lib(client, origin)]
    for label, url in not_found.items():
        results.append(
            _check_one(
                client,
                url,
                origin=origin,
                kind="html",
                expected_status=404,
                method="GET",
                label=label,
            )
        )
    return all(results)


def run(argv: Sequence[str] | None = None, *, client: httpx.Client | None = None) -> int:
    """Parse arguments and run one mode.

    Args:
        argv: Command-line arguments (defaults to ``sys.argv[1:]``).
        client: An HTTP client to use instead of a fresh one (tests).

    Returns:
        0 when every check passed, else 1.
    """
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "url", nargs="?", help="A share URL, e.g. https://share.example.com/s/<token>"
    )
    parser.add_argument("--kind", choices=_KINDS, help="html (snapshot or error page), svg, media")
    parser.add_argument(
        "--status",
        type=int,
        default=None,
        help="Expected HTTP status (default 200, or 405 with --method POST)",
    )
    parser.add_argument("--method", choices=("GET", "POST"), default=None)
    parser.add_argument("--boundary", metavar="ORIGIN", help="Run the V-L5 path checks on ORIGIN")
    parser.add_argument("--artifact-id", help="The artifact id X used by --boundary")
    args = parser.parse_args(argv)

    if args.boundary:
        if args.url or args.kind or args.status is not None or args.method is not None:
            parser.error("--boundary takes no share URL, --kind, --status or --method")
        if not args.artifact_id:
            parser.error("--boundary needs --artifact-id")
        if urlsplit(args.boundary).path not in ("", "/") or urlsplit(args.boundary).query:
            parser.error("--boundary takes an origin, e.g. https://share.example.com")
        target = args.boundary
    else:
        if not args.url or not args.kind:
            parser.error("a share URL and --kind are required unless --boundary is given")
        method: str = args.method or "GET"
        expected_status: int = (
            args.status if args.status is not None else (405 if method == "POST" else 200)
        )
        if expected_status != 200 and args.kind != "html":
            parser.error("an error page is always --kind html (ADR-0153 L7)")
        target = args.url
    try:
        origin = _origin_of(target)
    except ValueError:
        parser.error("not a valid http(s) URL")

    own_client = client is None
    http = client or httpx.Client(timeout=10.0, follow_redirects=False, trust_env=False)
    try:
        print(f"share origin: {origin}")
        if args.boundary:
            ok = _check_boundary(http, origin, args.artifact_id)
        else:
            ok = _check_one(
                http,
                args.url,
                origin=origin,
                kind=cast(ShareCspKind, args.kind),
                expected_status=expected_status,
                method=method,
                label=f"{method} share URL",
            )
    finally:
        if own_client:
            http.close()

    print("SHARE ENVELOPE OK" if ok else "SHARE ENVELOPE FAILURE")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(run())
