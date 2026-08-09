# ruff: noqa: D103
"""FRE-1239 — static guards for the OTLP ingress Caddy site block.

Static: parses the repo Caddyfile text directly. Proves what's decidable from the committed
file alone (AC-4, plus the request-shape hardening a codex plan-review round required). The
live end-to-end path (AC-1: span reaches Tempo, AC-2: refused paths never reach the Collector,
AC-3: the request is caddy-access-* log-shipped) requires the running cloud-sim stack and is
proven by master post-deploy — see tests/scripts/test_fre1239_otlp_ingress_smoke.py for the
hermetic behavioral proof this repo *can* run without touching that live stack.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
CADDYFILE_PATH = REPO_ROOT / "config" / "cloud-sim" / "Caddyfile"

_BLOCK_START = re.compile(r"^http://\{\$OTLP_HOST:[^}]*\}:80 \{", re.MULTILINE)


def _otlp_block() -> str:
    text = CADDYFILE_PATH.read_text()
    match = _BLOCK_START.search(text)
    assert match is not None, "no OTLP_HOST site block found in the Caddyfile"
    # Brace-match from the opening '{' to find the block's own closing '}'.
    start = match.end() - 1
    depth = 0
    for i, ch in enumerate(text[start:], start=start):
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[match.start() : i + 1]
    raise AssertionError("unbalanced braces in the OTLP_HOST site block")


def test_host_is_env_var_with_example_default_no_literal_domain() -> None:
    block = _otlp_block()
    assert "{$OTLP_HOST:otlp.example.com}" in block
    # No dot-separated real-looking domain other than the example default.
    domains = re.findall(r"[a-z0-9-]+\.[a-z0-9-]+\.[a-z]{2,}", block)
    assert domains == ["otlp.example.com"], f"unexpected literal domain(s): {domains}"


def test_upstream_is_http_4318_not_grpc_4317() -> None:
    block = _otlp_block()
    assert "reverse_proxy otel-collector:4318" in block
    assert "4317" not in block


def test_path_allowlist_is_exact_traces_path() -> None:
    block = _otlp_block()
    assert "path_regexp ^/v1/traces$" in block


def test_matcher_restricts_method_and_forbids_query_string() -> None:
    block = _otlp_block()
    assert re.search(r"\bmethod POST\b", block), "must restrict to POST"
    assert re.search(r'\bquery ""', block), "must forbid a non-empty query string"


def test_matcher_restricts_to_compose_network_origin() -> None:
    block = _otlp_block()
    assert "remote_ip 172.25.0.0/16" in block, (
        "must restrict to the cloud-sim compose network (docker-compose.cloud.yml's own "
        "subnet) so a direct-to-VPS-IP request bypassing Cloudflare Access is refused"
    )


def test_request_body_is_size_capped() -> None:
    block = _otlp_block()
    assert re.search(r"request_body\s*\{[^}]*max_size 20MiB", block, re.DOTALL)


def test_fallback_is_forbidden() -> None:
    block = _otlp_block()
    assert 'respond "Forbidden" 403' in block


def test_block_has_a_log_directive() -> None:
    block = _otlp_block()
    assert re.search(r"\blog\s*\{", block), "no log directive — AC-3 has no evidence trail"


def test_log_redacts_cloudflare_access_credential_headers() -> None:
    block = _otlp_block()
    for header in (
        "Cf-Access-Jwt-Assertion",
        "Cf-Access-Client-Id",
        "Cf-Access-Client-Secret",
    ):
        assert f"headers>{header} delete" in block, (
            f"{header} is not redacted from the access log — AC-3's own evidence trail "
            "would carry a bearer credential into caddy-access-*"
        )
