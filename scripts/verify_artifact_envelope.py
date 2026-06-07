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
"""

from __future__ import annotations

import argparse
import os
import sys

import httpx

from personal_agent.observability.artifact_envelope.verifier import (
    classify_access_denied,
    verify_envelope,
)


def main() -> int:
    """Fetch the URL, verify the served envelope, print the report.

    Returns:
        0 when the envelope is fully intact; 1 otherwise (including
        Access-denied / unreachable — unverifiable is not verified).
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("url", help="Artifact URL, e.g. https://artifacts.frenchforet.com/<id>")
    parser.add_argument(
        "--non-html",
        action="store_true",
        help="Verify a non-HTML artifact (served MIME must merely never be executable).",
    )
    args = parser.parse_args()

    headers: dict[str, str] = {}
    client_id = os.environ.get("CF_ACCESS_CLIENT_ID")
    client_secret = os.environ.get("CF_ACCESS_CLIENT_SECRET")
    if client_id and client_secret:
        headers = {
            "CF-Access-Client-Id": client_id,
            "CF-Access-Client-Secret": client_secret,
        }

    try:
        with httpx.Client(timeout=10.0, follow_redirects=False) as client:
            with client.stream("GET", args.url, headers=headers) as response:
                status_code = int(response.status_code)
                header_pairs = list(response.headers.multi_items())
        # Headers only — the body is never read (ADR-0089 D1/D5 scope boundary).
    except httpx.HTTPError as exc:
        print(f"PROBE FAILED: {exc}")
        return 1

    if classify_access_denied(status_code, header_pairs):
        print(f"UNVERIFIABLE: Cloudflare Access denied the request (HTTP {status_code}).")
        print("The service token is missing or not authorized on the artifacts Access app.")
        return 1

    report = verify_envelope(status_code, header_pairs, expect_html=not args.non_html)

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


if __name__ == "__main__":
    sys.exit(main())
