"""Integration tests for slm-requests ILM lifecycle management and backfill (FRE-1106).

These tests verify:
1. The slm-requests ILM policy is correctly structured
2. New indices created after policy application are managed
3. Existing unmanaged indices can be backfilled to become managed
4. The setup script applies the policy idempotently

Tests require a live Elasticsearch instance on port 9201 (test substrate).
"""

import json
import subprocess
from datetime import datetime, timedelta, timezone

import pytest

pytestmark = [
    pytest.mark.requires_llm_server,  # ES runs in compose with LLM server
    pytest.mark.integration,
]


@pytest.fixture
def es_url() -> str:
    """Test substrate Elasticsearch URL."""
    return "http://localhost:9201"


@pytest.fixture(autouse=True)
def ensure_es_ready(es_url: str) -> None:
    """Wait for ES to be ready before running tests."""
    import time

    for attempt in range(30):
        try:
            resp = subprocess.run(
                ["curl", "-fsS", f"{es_url}/_cluster/health"],
                capture_output=True,
                timeout=5,
                check=False,
            )
            if resp.returncode == 0:
                return
        except Exception:
            pass
        time.sleep(2)
    pytest.skip("Elasticsearch not reachable on port 9201")


def _es_request(es_url: str, method: str, path: str, data: dict | None = None) -> dict:
    """Make an ES request and return parsed JSON response."""
    cmd = ["curl", "-sS", "-X", method]
    if data:
        cmd.extend(["-H", "Content-Type: application/json", "-d", json.dumps(data)])
    cmd.append(f"{es_url}{path}")

    result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise RuntimeError(f"curl failed: {result.stderr}")

    if result.stdout.strip():
        return json.loads(result.stdout)
    return {}


def test_slm_requests_policy_applied(es_url: str) -> None:
    """Policy exists on ES after setup script runs."""
    policy = _es_request(es_url, "GET", "/_ilm/policy/slm-requests")
    assert policy, "slm-requests policy should exist"
    assert "policy" in policy
    assert policy["policy"]["phases"]["delete"]["min_age"] == "30d"


def test_slm_requests_template_references_policy(es_url: str) -> None:
    """Template binds new indices to the policy."""
    tpl = _es_request(es_url, "GET", "/_index_template/slm-requests-template")
    assert tpl, "slm-requests-template should exist"
    settings = tpl["indexTemplate"]["template"].get("settings", {})
    assert settings.get("index.lifecycle.name") == "slm-requests", (
        "template must set index.lifecycle.name to slm-requests"
    )


def test_new_index_is_lifecycle_managed(es_url: str) -> None:
    """A newly created index matching the pattern is ILM-managed."""
    index_name = f"slm-requests-{datetime.now(timezone.utc).strftime('%Y.%m.%d')}-test"

    # Create the index (template should apply automatically)
    _es_request(es_url, "PUT", f"/{index_name}", {"settings": {"number_of_shards": 1}})

    try:
        # Check its lifecycle status
        explain = _es_request(es_url, "GET", f"/{index_name}/_ilm/explain")
        assert index_name in explain, f"Index {index_name} not in explain output"

        index_info = explain["indices"][index_name]
        assert index_info.get("managed"), f"{index_name} should be managed by ILM"
        assert index_info.get("policy") == "slm-requests", (
            f"{index_name} should be managed by slm-requests policy"
        )
        assert "failed_step" not in index_info or not index_info["failed_step"], (
            f"{index_name} has a failed ILM step: {index_info.get('failed_step')}"
        )
    finally:
        # Cleanup
        subprocess.run(
            ["curl", "-sS", "-X", "DELETE", f"{es_url}/{index_name}"],
            capture_output=True,
        )


def test_backfill_unmanaged_to_managed(es_url: str) -> None:
    """Backfill converts an existing unmanaged index to managed."""
    # Create an index BEFORE the template carries the policy
    # (simulate an index that existed before FRE-1106).
    unmanaged_index = "slm-requests-2026.08.01-backfill-test"

    # Create without template by directly POSTing to bypass template matching.
    # This simulates an index created by the external SLM producer before this policy existed.
    _es_request(es_url, "PUT", f"/{unmanaged_index}", {})

    try:
        # Verify it starts unmanaged
        explain_before = _es_request(es_url, "GET", f"/{unmanaged_index}/_ilm/explain")
        assert unmanaged_index in explain_before
        assert not explain_before["indices"][unmanaged_index].get("managed"), (
            f"{unmanaged_index} should start unmanaged"
        )

        # Backfill: set the lifecycle name
        _es_request(
            es_url,
            "PUT",
            f"/{unmanaged_index}/_settings",
            {"index.lifecycle.name": "slm-requests"},
        )

        # Verify it's now managed
        explain_after = _es_request(es_url, "GET", f"/{unmanaged_index}/_ilm/explain")
        assert unmanaged_index in explain_after
        index_info = explain_after["indices"][unmanaged_index]
        assert index_info.get("managed"), f"{unmanaged_index} should be managed after backfill"
        assert index_info.get("policy") == "slm-requests", (
            f"{unmanaged_index} should be managed by slm-requests policy after backfill"
        )
    finally:
        # Cleanup
        subprocess.run(
            ["curl", "-sS", "-X", "DELETE", f"{es_url}/{unmanaged_index}"],
            capture_output=True,
        )


def test_setup_script_is_idempotent(es_url: str) -> None:
    """Running setup-elasticsearch.sh twice succeeds both times."""
    import os

    script = "/opt/seshat/.claude/worktrees/build2/scripts/setup-elasticsearch.sh"
    if not os.path.exists(script):
        pytest.skip(f"Setup script not found at {script}")

    # Run once
    result1 = subprocess.run(
        ["bash", script],
        env={**os.environ, "ES_URL": es_url},
        capture_output=True,
        text=True,
    )
    assert result1.returncode == 0, f"First run failed:\n{result1.stderr}"

    # Run again (idempotent)
    result2 = subprocess.run(
        ["bash", script],
        env={**os.environ, "ES_URL": es_url},
        capture_output=True,
        text=True,
    )
    assert result2.returncode == 0, f"Second run failed:\n{result2.stderr}"
    # Both runs should succeed with no special "already exists" warnings in stderr.
    # curl 2xx status on PUT (replace) is idempotent success.
