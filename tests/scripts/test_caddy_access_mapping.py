# ruff: noqa: D103
"""FRE-1146 / ADR-0132 D3 — caddy-access-* mapping guards.

Static: parses the repo template JSON directly. Guards the mapping-explosion fix a codex
plan-review round required — Caddy's request/response headers are attacker-influenced
(arbitrary inbound header names), so they must be mapped ``flattened`` rather than left to
blanket ``dynamic: true`` (which would create one field per distinct header key ever seen).
"""

from __future__ import annotations

import json
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
TEMPLATE_PATH = REPO_ROOT / "docker" / "elasticsearch" / "caddy-access-index-template.json"


def _load_template() -> dict:
    return json.loads(TEMPLATE_PATH.read_text())


def test_request_headers_are_flattened_not_dynamic() -> None:
    template = _load_template()
    caddy_props = template["template"]["mappings"]["properties"]["caddy"]["properties"]
    assert caddy_props["request"]["properties"]["headers"]["type"] == "flattened"


def test_resp_headers_are_flattened_not_dynamic() -> None:
    template = _load_template()
    caddy_props = template["template"]["mappings"]["properties"]["caddy"]["properties"]
    assert caddy_props["resp_headers"]["type"] == "flattened"


def test_logger_field_is_keyword() -> None:
    """caddy.logger is the field AC-a's egress/inbound distinction relies on."""
    template = _load_template()
    caddy_props = template["template"]["mappings"]["properties"]["caddy"]["properties"]
    assert caddy_props["logger"]["type"] == "keyword"
