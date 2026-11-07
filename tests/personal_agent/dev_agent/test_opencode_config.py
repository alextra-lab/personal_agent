# ruff: noqa: D101, D102
"""The agent image's OpenCode config and permission file (ADR-0159 D2.4, D5.1; FRE-1684).

OpenCode 2.2.5 adds the file's ``permissions`` after the built-in rules of every agent, and the
last matching rule wins. The built-in rules contain ``ask`` rules (for example ``read *.env``).
A headless job has no client to answer an ask, so the file must start with a catch-all and contain
no ``ask`` of its own. Observed on 2026-11-07 against ``GET /api/agent`` of a no-network container.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from personal_agent.dev_broker.config import load_broker_config

_REPO = Path(__file__).resolve().parents[3]
_CONFIG = _REPO / "docker" / "dev-agent" / "opencode.json"
_DOCKERFILE = _REPO / "docker" / "dev-agent" / "Dockerfile.browser"


def _config() -> dict[str, Any]:
    return json.loads(_CONFIG.read_text(encoding="utf-8"))


def _effect(rules: list[dict[str, str]], action: str) -> str:
    """Return the effect that OpenCode applies to ``action`` on any resource: the last match."""
    matching = [r for r in rules if r["action"] in (action, "*") and r["resource"] == "*"]
    return matching[-1]["effect"]


class TestPermissions:
    def test_a_catch_all_deny_comes_first_so_no_action_is_unmatched(self) -> None:
        rules = _config()["permissions"]
        assert rules[0] == {"action": "*", "resource": "*", "effect": "deny"}

    def test_no_rule_asks(self) -> None:
        assert all(rule["effect"] != "ask" for rule in _config()["permissions"])

    def test_reads_edits_and_shell_are_allowed(self) -> None:
        rules = _config()["permissions"]
        for action in ("read", "edit", "shell", "grep", "glob"):
            assert _effect(rules, action) == "allow", action

    def test_external_directories_and_the_web_are_denied(self) -> None:
        rules = _config()["permissions"]
        for action in ("external_directory", "webfetch", "websearch", "question", "subagent"):
            assert _effect(rules, action) == "deny", action

    def test_an_unknown_action_is_denied_not_asked(self) -> None:
        assert _effect(_config()["permissions"], "doom_loop") == "deny"


class TestProvider:
    def test_the_only_provider_is_the_broker_route_with_the_job_token(self) -> None:
        broker = load_broker_config()
        provider = _config()["provider"]
        assert list(provider) == ["seshat"]
        options = provider["seshat"]["options"]
        url = urlparse(options["baseURL"])
        assert (url.scheme, url.hostname, url.port, url.path) == (
            "http",
            "dev-broker",
            broker.model_port,
            "/v1",
        )
        assert options["apiKey"] == "{env:SESHAT_JOB_TOKEN}"

    def test_the_models_are_the_brokers_models(self) -> None:
        broker = load_broker_config()
        config = _config()
        assert set(config["provider"]["seshat"]["models"]) == set(broker.models)
        assert config["model"] == f"seshat/{broker.default_model}"


class TestImage:
    def test_the_dockerfile_copies_the_file_read_only(self) -> None:
        text = _DOCKERFILE.read_text(encoding="utf-8")
        assert (
            "COPY --chown=root:root --chmod=0444 opencode.json /etc/opencode/opencode.json" in text
        )
