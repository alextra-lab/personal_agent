# ruff: noqa: D103
"""Tests for the seshat-dispatch MCP channel server (FRE-871, ADR-0116 Phase 1).

The channel's security-critical behaviour — a fail-closed shared-secret sender
gate and a localhost-only bind — lives in ``server.mjs`` (SDK-free) and is proven
by the co-located ``server.test.mjs`` ``node --test`` suite. This module bridges
that suite into ``make test`` (the master-gate-readable proof surface) by shelling
out to ``node --test`` and asserting it passes, and separately asserts the plugin
package is well-formed so the channel is loadable as an allowlisted plugin.

``node --test`` needs no npm install (``server.mjs`` imports only node builtins),
so the bridge is deterministic wherever a Node runtime exists; it skips loudly
when Node is absent rather than silently passing.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

_CHANNEL_DIR = (
    Path(__file__).resolve().parents[2]
    / "scripts"
    / "dispatch"
    / "channel"
    / "plugins"
    / "seshat-dispatch"
)


def _node_major() -> int | None:
    """Return the installed Node major version, or ``None`` if node is absent.

    ``server.test.mjs`` uses the stable ``node:test`` runner and global ``fetch``
    (both Node 18+); a pre-18 node in PATH would fail the suite rather than skip,
    so the bridge must gate on the version, not merely on the binary's presence.
    """
    node = shutil.which("node")
    if node is None:
        return None
    result = subprocess.run(  # noqa: S603 - fixed argv, no shell, trusted binary
        [node, "--version"], capture_output=True, text=True, check=False
    )
    try:
        return int(result.stdout.strip().lstrip("v").split(".")[0])
    except (ValueError, IndexError):
        return None


@pytest.mark.skipif(
    (_node_major() or 0) < 18, reason="node >= 18 (node:test + global fetch) not available"
)
def test_channel_gate_js_suite_passes() -> None:
    """The ``server.test.mjs`` gate suite (403 on bad/missing secret, localhost bind) passes."""
    result = subprocess.run(  # noqa: S603 - fixed argv, no shell, trusted local test file
        [shutil.which("node") or "node", "--test", "server.test.mjs"],
        cwd=_CHANNEL_DIR,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, f"node --test failed:\n{result.stdout}\n{result.stderr}"


def test_plugin_manifest_is_well_formed() -> None:
    """The plugin manifest names the channel and matches the marketplace source entry."""
    plugin = json.loads((_CHANNEL_DIR / ".claude-plugin" / "plugin.json").read_text())
    assert plugin["name"] == "seshat-dispatch"

    marketplace = json.loads(
        (_CHANNEL_DIR.parents[1] / ".claude-plugin" / "marketplace.json").read_text()
    )
    assert marketplace["name"] == "seshat-dispatch"
    entry = next(p for p in marketplace["plugins"] if p["name"] == "seshat-dispatch")
    assert entry["source"] == "./plugins/seshat-dispatch"


def test_mcp_json_registers_the_channel_server() -> None:
    """The plugin's ``.mcp.json`` starts ``webhook.mjs`` as the ``seshat-dispatch`` server."""
    mcp = json.loads((_CHANNEL_DIR / ".mcp.json").read_text())
    server = mcp["mcpServers"]["seshat-dispatch"]
    assert server["command"] == "node"
    assert any("webhook.mjs" in arg for arg in server["args"])


def test_webhook_cross_references_lifecycle_rules_session_boundary() -> None:
    """FRE-872: the boundary language has one traceable normative source.

    ``webhook.mjs``'s MCP instructions already state the boundary ("act within
    THIS session only... never push to, merge, approve, close, or deploy a
    branch/PR you do not own") but didn't name where that invariant is owned.
    This regression-guards the explicit cross-reference to
    ``.claude/skills/lifecycle-rules.md`` § Session boundary (the ADR's cited
    normative source, per that file's own single-source rule).
    """
    text = (_CHANNEL_DIR / "webhook.mjs").read_text()
    assert "lifecycle-rules.md" in text
    assert "Session boundary" in text
