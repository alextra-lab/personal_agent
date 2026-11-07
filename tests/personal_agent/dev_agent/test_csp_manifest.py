# ruff: noqa: D101, D102
"""The CSP test server of the check and agent images sends the production policy (FRE-1682).

The server sends ``csp_header_template`` from ``docker/dev-agent/csp-manifest.json`` with the
``{ORIGIN}`` placeholder replaced by its own localhost origin. So the policy is right when the
committed template, rebound to localhost, equals ``expected_csp_directives()`` rebound the same way.
The live container check in ``scripts/verify_dev_agent_images.py`` repeats the comparison on the
header that the running server sends.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import urllib.request
from collections.abc import Iterator
from pathlib import Path

import pytest

from personal_agent.config import settings
from personal_agent.dev_agent.verification import csp_header_violations
from personal_agent.observability.artifact_envelope.spec import expected_csp_directives

_REPO = Path(__file__).resolve().parents[3]
_MANIFEST = _REPO / "docker" / "dev-agent" / "csp-manifest.json"
_SERVE = _REPO / "e2e" / "artifact-lib" / "csp-serve.ts"
_ARTIFACTS = "https://artifacts.example.com"
_LOCAL = "http://127.0.0.1:43210"


@pytest.fixture(autouse=True)
def _placeholder_origins(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the two settings that the policy reads, so the result does not depend on `.env`."""
    monkeypatch.setattr(settings, "artifacts_public_base_url", None)
    monkeypatch.setattr(settings, "pwa_public_origin", "https://agent.example.com")


def _template() -> str:
    data = json.loads(_MANIFEST.read_text(encoding="utf-8"))
    assert set(data) == {"csp_header_template"}
    template = data["csp_header_template"]
    assert isinstance(template, str)
    return template


class TestCommittedManifest:
    def test_template_equals_the_expected_policy(self) -> None:
        found = csp_header_violations(
            _template(),
            origin="{ORIGIN}",
            expected=expected_csp_directives(),
            artifacts_origin=_ARTIFACTS,
        )
        assert found == [], "csp-manifest.json drifted from expected_csp_directives()"

    def test_served_header_equals_the_expected_policy_rebound_to_localhost(self) -> None:
        served = _template().replace("{ORIGIN}", _LOCAL)
        found = csp_header_violations(
            served,
            origin=_LOCAL,
            expected=expected_csp_directives(),
            artifacts_origin=_ARTIFACTS,
        )
        assert found == []

    def test_the_template_names_no_real_deployment_host(self) -> None:
        assert re.findall(r"https://[^\s;]+", _template()) == ["https://agent.example.com"]

    @pytest.mark.parametrize(
        "mutate",
        [
            lambda t: t.replace("; webrtc 'block'", ""),
            lambda t: t.replace("connect-src 'none'", "connect-src *"),
            lambda t: t.replace("{ORIGIN}", "https://elsewhere.example.org"),
            lambda t: t + "; object-src *",
        ],
    )
    def test_a_changed_manifest_is_reported(self, mutate) -> None:  # type: ignore[no-untyped-def]
        served = mutate(_template()).replace("{ORIGIN}", _LOCAL)
        found = csp_header_violations(
            served,
            origin=_LOCAL,
            expected=expected_csp_directives(),
            artifacts_origin=_ARTIFACTS,
        )
        assert found


def _node_runs_typescript() -> bool:
    node = shutil.which("node")
    if node is None:
        return False
    version = subprocess.run([node, "--version"], capture_output=True, text=True, check=False)
    major, minor = (int(p) for p in version.stdout.strip().lstrip("v").split(".")[:2])
    return (major, minor) >= (22, 18)


@pytest.fixture
def served_origin(tmp_path: Path) -> Iterator[str]:
    """Start csp-serve.ts as the image does, on an empty root, and yield its origin."""
    root = tmp_path
    (root / "page.html").write_text("<!doctype html><title>t</title>", encoding="utf-8")
    (root / "data.json").write_text("{}", encoding="utf-8")
    process = subprocess.Popen(  # noqa: S603
        ["node", str(_SERVE), str(root), str(_MANIFEST)],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert process.stdout is not None
        origin = process.stdout.readline().strip()
        assert origin.startswith("http://127.0.0.1:")
        yield origin
    finally:
        process.terminate()
        process.wait(timeout=10)


@pytest.mark.skipif(not _node_runs_typescript(), reason="node >= 22.18 is needed to run .ts")
class TestCspServeEntryPoint:
    """The server entry point of the images, run on the host."""

    def test_html_carries_the_expected_policy_for_its_own_origin(self, served_origin: str) -> None:
        with urllib.request.urlopen(f"{served_origin}/page.html") as response:
            header = response.headers.get("Content-Security-Policy") or ""
        found = csp_header_violations(
            header,
            origin=served_origin,
            expected=expected_csp_directives(),
            artifacts_origin=_ARTIFACTS,
        )
        assert found == []

    def test_a_non_html_file_carries_no_policy(self, served_origin: str) -> None:
        with urllib.request.urlopen(f"{served_origin}/data.json") as response:
            assert response.headers.get("Content-Security-Policy") is None
