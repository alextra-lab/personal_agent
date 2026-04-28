"""Tests for FRE-282 intent-based skill doc injection.

Verifies that get_skill_block(message) routes to the right skill doc
rather than injecting all 9 docs on every request.
"""

from unittest.mock import patch

import pytest

from personal_agent.orchestrator.skills import get_skill_block


# ── Fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def primitives_enabled():
    """Ensure prefer_primitives_enabled is True for all routing tests."""
    with patch("personal_agent.orchestrator.skills.settings") as mock_settings:
        mock_settings.prefer_primitives_enabled = True
        yield mock_settings


# ── Guard: primitives disabled ────────────────────────────────────────────────


def test_returns_empty_when_primitives_disabled():
    with patch("personal_agent.orchestrator.skills.settings") as s:
        s.prefer_primitives_enabled = False
        assert get_skill_block("Show me errors in the last hour") == ""


# ── bash.md always injected ───────────────────────────────────────────────────


def test_bash_always_present_with_message():
    block = get_skill_block("Show me errors in the last hour")
    assert "bash" in block.lower()


def test_bash_present_with_no_message():
    block = get_skill_block(None)
    assert "bash" in block.lower()


def test_bash_present_with_unrecognised_message():
    block = get_skill_block("What's the weather in Tokyo?")
    assert "bash" in block.lower()


# ── Routing: query-elasticsearch ─────────────────────────────────────────────


@pytest.mark.parametrize("message", [
    "Show me errors in the last hour",
    "How many times did the agent call query_elasticsearch today?",
    "What's the p95 LLM call latency over the last 24 hours?",
    "Find a trace from the past week where the agent hit the loop gate",
    "elasticsearch indices with most docs",
    "Query agent-logs for tool_call events",
])
def test_routes_to_elasticsearch_skill(message: str):
    block = get_skill_block(message)
    assert "agent-logs-" in block or "ES|QL" in block or "elasticsearch" in block.lower()


# ── Routing: list-directory ───────────────────────────────────────────────────


@pytest.mark.parametrize("message", [
    "How many YAML files are under /app/config?",
    "List files in /app/config",
    "What's in the /app/src/personal_agent/tools folder?",
    "How many Python files are in the source tree?",
])
def test_routes_to_list_directory_skill(message: str):
    block = get_skill_block(message)
    assert "find" in block and "wc -l" in block


# ── Routing: system-metrics ───────────────────────────────────────────────────


@pytest.mark.parametrize("message", [
    "What's the current CPU load?",
    "How much memory is the agent service using right now?",
    "Is disk space getting low?",
])
def test_routes_to_system_metrics_skill(message: str):
    block = get_skill_block(message)
    assert "free -m" in block or "top -bn1" in block or "proc" in block.lower()


# ── Routing: system-diagnostics ──────────────────────────────────────────────


@pytest.mark.parametrize("message", [
    "List the top 10 processes by memory usage",
    "Which container ports are listening?",
    "Show me what the system has been doing for the last 5 minutes (load, swap, IO)",
])
def test_routes_to_system_diagnostics_skill(message: str):
    block = get_skill_block(message)
    assert "vmstat" in block or "ps aux" in block or "ss -tunlp" in block


# ── Routing: infrastructure-health ───────────────────────────────────────────


@pytest.mark.parametrize("message", [
    "Check infrastructure health",
    "Is Postgres reachable?",
    "Are Neo4j and Elasticsearch both up?",
    "All backend services healthy right now?",
])
def test_routes_to_infrastructure_health_skill(message: str):
    block = get_skill_block(message)
    assert "postgres" in block.lower() and "neo4j" in block.lower()


# ── Routing: fetch-url ────────────────────────────────────────────────────────


@pytest.mark.parametrize("message", [
    "Fetch https://example.com/api/status and tell me what it says",
    "Read the README on https://github.com/anthropics/anthropic-sdk-python",
    "What's the current Anthropic pricing? Check https://www.anthropic.com/pricing",
])
def test_routes_to_fetch_url_skill(message: str):
    block = get_skill_block(message)
    assert "curl" in block and ("http" in block or "url" in block.lower())


# ── No double-injection of bash.md ───────────────────────────────────────────


def test_bash_not_duplicated_in_block():
    block = get_skill_block("Show me errors in the last hour")
    # bash.md content appears exactly once (header appears once)
    assert block.count("# bash — Shell Command Executor") == 1


# ── Block structure ───────────────────────────────────────────────────────────


def test_block_contains_header():
    block = get_skill_block("What's the CPU load?")
    assert "Skill Library" in block


def test_unrecognised_message_returns_only_bash():
    """A message that matches no route should return bash.md only — not all 9 docs."""
    block = get_skill_block("Please write me a haiku about the ocean")
    assert "query-elasticsearch" not in block.lower()
    assert "infrastructure-health" not in block.lower()
    assert "system-metrics" not in block.lower()
    assert "bash" in block.lower()
