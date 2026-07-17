#!/usr/bin/env python3
"""Live served-response envelope gate (FRE-512 / ADR-0089 D5, layer 2).

Fetches a real artifact URL through the edge and verifies the served envelope
(CSP directive set, MIME, nosniff) with the same verifier the commit-time probe
uses. Exits non-zero unless the envelope is fully intact — master runs this at
the deploy gate (``make verify-envelope URL=…``); it is CI-attachable once an
authorized Access service token exists.

Auth: reads ``CF_ACCESS_CLIENT_ID`` / ``CF_ACCESS_CLIENT_SECRET`` from the
environment when present. Without an authorized token the script reports the
Access denial distinctly and exits 1 (unverifiable ≠ verified).

Usage:
    uv run python scripts/verify_artifact_envelope.py <artifact-url> [--non-html]
    uv run python scripts/verify_artifact_envelope.py --lib [--manifest PATH] [--origin URL]

``--lib`` mode (ADR-0089 Addendum A · A7, FRE-527) verifies every curated
``/lib/`` toolkit asset in the manifest serves the correct executable/typed MIME +
``nosniff`` and is reachable under the artifact CSP. ``eval_gated`` assets (e.g.
paged.js pending eval-free confirmation) are skipped.
"""

from __future__ import annotations

import argparse
import os
import sys

import httpx

from personal_agent.observability.artifact_envelope.spec import (
    DEFAULT_LIB_MANIFEST_PATH,
    load_lib_manifest,
)
from personal_agent.observability.artifact_envelope.verifier import (
    classify_access_denied,
    verify_envelope,
    verify_lib_asset,
)


def _token_headers() -> dict[str, str]:
    """CF Access service-token headers from the environment, when present."""
    client_id = os.environ.get("CF_ACCESS_CLIENT_ID")
    client_secret = os.environ.get("CF_ACCESS_CLIENT_SECRET")
    if client_id and client_secret:
        return {
            "CF-Access-Client-Id": client_id,
            "CF-Access-Client-Secret": client_secret,
        }
    return {}


def _fetch_headers(
    client: httpx.Client, url: str, headers: dict[str, str]
) -> tuple[int, list[tuple[str, str]]]:
    """GET ``url`` and return (status, header pairs) — body never read (D1/D5)."""
    with client.stream("GET", url, headers=headers) as response:
        return int(response.status_code), list(response.headers.multi_items())


def _verify_artifact(url: str, *, expect_html: bool) -> int:
    """Verify one artifact URL's served envelope; return the process exit code."""
    headers = _token_headers()
    try:
        with httpx.Client(timeout=10.0, follow_redirects=False) as client:
            status_code, header_pairs = _fetch_headers(client, url, headers)
    except httpx.HTTPError as exc:
        print(f"PROBE FAILED: {exc}")
        return 1

    if classify_access_denied(status_code, header_pairs):
        print(f"UNVERIFIABLE: Cloudflare Access denied the request (HTTP {status_code}).")
        print("The service token is missing or not authorized on the artifacts Access app.")
        return 1

    report = verify_envelope(status_code, header_pairs, expect_html=expect_html)

    print(f"http_status:            {report.http_status}")
    print(f"csp_present:            {report.csp_present}")
    print(f"served_mime:            {report.served_mime}")
    print(f"mime_ok:                {report.mime_ok}")
    print(f"nosniff_ok:             {report.nosniff_ok}")
    print(f"missing_directives:     {list(report.missing_directives)}")
    print(f"mismatched_directives:  {list(report.mismatched_directives)}")
    print(f"unexpected_directives:  {list(report.unexpected_directives)}")
    print(f"csp_header:             {report.csp_header}")
    if report.envelope_ok:
        print("ENVELOPE OK — every wall present and exact.")
        return 0
    print(f"ENVELOPE FAILURE: {list(report.failures)}")
    return 1


def _verify_lib(manifest_path: str, origin_override: str | None) -> int:
    """Verify every (non-eval-gated) ``/lib/`` asset; return the process exit code."""
    manifest_origin, assets = load_lib_manifest(manifest_path)
    origin = origin_override or manifest_origin
    checkable = [a for a in assets if not a.eval_gated]
    skipped = [a for a in assets if a.eval_gated]
    headers = _token_headers()

    print(f"origin:  {origin}")
    print(f"assets:  {len(checkable)} checked, {len(skipped)} eval-gated (skipped)")
    for asset in skipped:
        print(f"  SKIP (eval-gated)  {asset.path}")

    failed = 0
    try:
        with httpx.Client(timeout=10.0, follow_redirects=False) as client:
            for asset in checkable:
                url = f"{origin}/lib/{asset.path}"
                try:
                    status_code, header_pairs = _fetch_headers(client, url, headers)
                except httpx.HTTPError as exc:
                    print(f"  FAIL  {asset.path}  (probe failed: {exc})")
                    failed += 1
                    continue
                if classify_access_denied(status_code, header_pairs):
                    print(f"  UNVERIFIABLE  {asset.path}  (Access denied, HTTP {status_code})")
                    failed += 1
                    continue
                report = verify_lib_asset(status_code, header_pairs, asset=asset, origin=origin)
                if report.asset_ok:
                    print(f"  OK    {asset.path}  ({report.served_mime})")
                else:
                    print(f"  FAIL  {asset.path}  {list(report.failures)} ({report.served_mime})")
                    failed += 1
    except httpx.HTTPError as exc:
        print(f"PROBE FAILED: {exc}")
        return 1

    if failed:
        print(f"LIB FAILURE: {failed}/{len(checkable)} asset(s) failed.")
        return 1
    print(f"LIB OK — all {len(checkable)} asset(s) reachable + correct MIME + nosniff.")
    return 0


def main() -> int:
    """Verify the served artifact envelope or the curated ``/lib/`` toolkit.

    Returns:
        0 when fully verified; 1 otherwise (including Access-denied / unreachable —
        unverifiable is not verified).
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "url",
        nargs="?",
        help="Artifact URL, e.g. https://artifacts.example.com/<id> (omit in --lib mode)",
    )
    parser.add_argument(
        "--non-html",
        action="store_true",
        help="Verify a non-HTML artifact (served MIME must merely never be executable).",
    )
    parser.add_argument(
        "--lib",
        action="store_true",
        help="Verify the curated /lib/ toolkit assets from the manifest instead of an artifact URL.",
    )
    parser.add_argument(
        "--manifest",
        default=str(DEFAULT_LIB_MANIFEST_PATH),
        help="Path to the /lib/ manifest (default: config/artifact_lib_manifest.json).",
    )
    parser.add_argument(
        "--origin",
        default=None,
        help="Override the serving origin for --lib mode (default: the manifest's origin).",
    )
    args = parser.parse_args()

    if args.lib:
        return _verify_lib(args.manifest, args.origin)

    if not args.url:
        parser.error("an artifact URL is required unless --lib is given")
    return _verify_artifact(args.url, expect_html=not args.non_html)


if __name__ == "__main__":
    sys.exit(main())
