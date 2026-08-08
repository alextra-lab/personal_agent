"""FRE-1070 / ADR-0129 D5 — acceptance tests for the OTel Collector's own criteria.

This ticket's own AC-1 (declared redaction fires, with a positive control) and AC-2 (the
running Collector is the vanilla upstream core distribution). AC-3/AC-4/AC-5 are covered
elsewhere: AC-3 by tests/personal_agent/service/test_telemetry_effective_config.py, AC-4 by
tests/scripts/test_otel_collector_compose_service.py, AC-5 by the existing (unchanged)
tests/test_telemetry/test_es_logger*.py suites.

Requires the dev compose stack up:

    docker compose up -d tempo otel-collector

Then:

    PERSONAL_AGENT_INTEGRATION=1 pytest -m integration \
        tests/integration/test_fre1070_otel_collector_acceptance.py -v
"""

from __future__ import annotations

import os
import subprocess
import time
import uuid

import pytest
import requests

from personal_agent.config.config_guard import repo_root

pytestmark = pytest.mark.integration

_COLLECTOR_OTLP_HTTP_URL = os.environ.get(
    "FRE1070_COLLECTOR_OTLP_HTTP_URL", "http://localhost:4320"
)
_POLL_ATTEMPTS = 15
_POLL_INTERVAL_SECONDS = 1


def _http_reachable(url: str) -> bool:
    try:
        requests.get(url, timeout=2)
        return True
    except requests.RequestException:
        return False


def _require_stack() -> None:
    if not _http_reachable(_COLLECTOR_OTLP_HTTP_URL):
        pytest.skip(
            f"otel-collector not reachable at {_COLLECTOR_OTLP_HTTP_URL} — "
            "run `docker compose up -d tempo otel-collector`"
        )


def _inject_span(trace_id: str, blocked_value: str, survives_value: str) -> None:
    now_ns = int(time.time() * 1e9)
    body = {
        "resourceSpans": [
            {
                "resource": {
                    "attributes": [
                        {"key": "service.name", "value": {"stringValue": "fre1070-fixture"}}
                    ]
                },
                "scopeSpans": [
                    {
                        "scope": {"name": "fre1070-fixture-injector"},
                        "spans": [
                            {
                                "traceId": trace_id,
                                "spanId": trace_id[:16],
                                "name": "fre1070-ac1-fixture-span",
                                "kind": 1,
                                "startTimeUnixNano": str(now_ns - 500_000_000),
                                "endTimeUnixNano": str(now_ns),
                                "attributes": [
                                    {
                                        "key": "fre1070.fixture.blocked",
                                        "value": {"stringValue": blocked_value},
                                    },
                                    {
                                        "key": "fre1070.fixture.survives",
                                        "value": {"stringValue": survives_value},
                                    },
                                ],
                            }
                        ],
                    }
                ],
            }
        ]
    }
    r = requests.post(f"{_COLLECTOR_OTLP_HTTP_URL}/v1/traces", json=body, timeout=10)
    r.raise_for_status()


def _collector_logs_since(seconds_ago: int) -> str:
    result = subprocess.run(
        ["docker", "compose", "logs", "--since", f"{seconds_ago}s", "otel-collector"],
        cwd=repo_root(),
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout


class TestAC1RedactionFiresWithPositiveControl:
    """AC-1 — the declared rule removes the blocked attribute; the control attribute survives."""

    def test_blocked_attribute_absent_survives_attribute_present(self) -> None:
        _require_stack()
        trace_id = uuid.uuid4().hex
        survives_value = f"fre1070-survives-{uuid.uuid4().hex}"
        blocked_value = "fre1070-should-be-deleted"
        elapsed_budget = _POLL_ATTEMPTS * _POLL_INTERVAL_SECONDS + 10

        _inject_span(trace_id, blocked_value=blocked_value, survives_value=survives_value)

        logs = ""
        for _ in range(_POLL_ATTEMPTS):
            logs = _collector_logs_since(elapsed_budget)
            if survives_value in logs:
                break
            time.sleep(_POLL_INTERVAL_SECONDS)

        assert survives_value in logs, (
            "the non-matching control attribute never reached the Collector's debug "
            "exporter — the positive control itself failed, so the redaction result below "
            "would be meaningless"
        )
        assert blocked_value not in logs, (
            "the declared redaction rule did not fire — the blocked attribute survived to "
            "the Collector's own output"
        )


class TestAC2RunningImageIsVanillaUpstreamCore:
    """AC-2 — the running container's image is exactly the core upstream distribution."""

    def test_running_image_is_exact_core_distribution(self) -> None:
        _require_stack()
        container_id = subprocess.run(
            ["docker", "compose", "ps", "-q", "otel-collector"],
            cwd=repo_root(),
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        if not container_id:
            pytest.skip(
                "otel-collector container not found — run `docker compose up -d otel-collector`"
            )

        image = subprocess.run(
            ["docker", "inspect", "--format", "{{.Config.Image}}", container_id],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()

        assert image == "otel/opentelemetry-collector:0.158.0", (
            f"expected the exact vanilla upstream core image, got {image!r} — not a prefix "
            "match, since a lookalike or contrib/vendor image must fail this check"
        )
