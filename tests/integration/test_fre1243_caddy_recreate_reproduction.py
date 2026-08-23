# ruff: noqa: D103
r"""FRE-1243 — acceptance test for this ticket's own AC-1 and AC-3.

Recreating Caddy alone must no longer stop the access-log flow (AC-1), and the shipped `caddy.*`
evidence shape must be unchanged from the pre-fix mechanism (AC-3). AC-2 (the bug reproduces on
pre-fix code) and this test's own AC-3 field-name baseline were captured once, by hand, against a
separate throwaway harness matching today's pre-fix mechanism exactly (stdout + container-ID
resolution) — that harness was never committed (a file-based Caddyfile can't stand in for the
stdout-based pre-fix mechanism), and its evidence lives in the PR/Linear handoff plus
`tests/scripts/fixtures/fre1243_harness/pre_fix_caddy_field_baseline.json`.

Requires the throwaway, isolated harness up (never the live cloud-sim-* stack):

    docker compose -p fre1243-harness \\
        -f tests/scripts/fixtures/fre1243_harness/docker-compose.harness.yml up -d --build

Then:

    PERSONAL_AGENT_INTEGRATION=1 pytest -m integration \\
        tests/integration/test_fre1243_caddy_recreate_reproduction.py -v

Tear down when done:

    docker compose -p fre1243-harness \\
        -f tests/scripts/fixtures/fre1243_harness/docker-compose.harness.yml down -v
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from datetime import datetime, timezone

import pytest
import requests

from personal_agent.config.config_guard import repo_root

pytestmark = pytest.mark.integration

_CADDY_URL = os.environ.get("FRE1243_HARNESS_CADDY_URL", "http://localhost:18080")
_ES_URL = os.environ.get("FRE1243_HARNESS_ES_URL", "http://localhost:19200")
_COMPOSE_PROJECT = "fre1243-harness"
_COMPOSE_FILE = "tests/scripts/fixtures/fre1243_harness/docker-compose.harness.yml"
_TEMPLATE_FILE = repo_root() / "docker" / "elasticsearch" / "caddy-access-index-template.json"
_BASELINE_FILE = (
    repo_root()
    / "tests"
    / "scripts"
    / "fixtures"
    / "fre1243_harness"
    / "pre_fix_caddy_field_baseline.json"
)
_PROBE_HEADERS = {"X-FRE1243-Probe": "harness"}
_POLL_ATTEMPTS = 30
_POLL_INTERVAL_SECONDS = 1
_OPAQUE_FIELDS = {"headers", "resp_headers"}  # flattened in the index template — not recursed


def _http_reachable(url: str) -> bool:
    try:
        requests.get(url, timeout=2)
        return True
    except requests.RequestException:
        return False


def _require_harness() -> None:
    if not (_http_reachable(_CADDY_URL) and _http_reachable(_ES_URL)):
        pytest.skip(
            f"fre1243-harness not reachable at {_CADDY_URL} / {_ES_URL} — run "
            f"`docker compose -p {_COMPOSE_PROJECT} -f {_COMPOSE_FILE} up -d --build`"
        )


def _compose(*args: str) -> None:
    subprocess.run(
        ["docker", "compose", "-p", _COMPOSE_PROJECT, "-f", _COMPOSE_FILE, *args],
        cwd=repo_root(),
        check=True,
        capture_output=True,
        text=True,
    )


def _install_index_template() -> None:
    template = json.loads(_TEMPLATE_FILE.read_text())
    resp = requests.put(
        f"{_ES_URL}/_index_template/caddy-access-template", json=template, timeout=10
    )
    resp.raise_for_status()


def _send_probe() -> None:
    # Right after a force-recreate the new container may not have finished starting yet —
    # retry through the connection-reset/refused window rather than treating startup lag as
    # a test failure.
    last_error: Exception | None = None
    for _ in range(_POLL_ATTEMPTS):
        try:
            resp = requests.get(_CADDY_URL, headers=_PROBE_HEADERS, timeout=10)
            resp.raise_for_status()
            return
        except requests.RequestException as exc:
            last_error = exc
            time.sleep(_POLL_INTERVAL_SECONDS)
    raise AssertionError(f"caddy never became reachable at {_CADDY_URL}: {last_error}")


def _newest_doc() -> dict | None:
    try:
        resp = requests.get(
            f"{_ES_URL}/caddy-access-*/_search",
            params={"size": 1, "sort": "@timestamp:desc"},
            timeout=10,
        )
    except requests.RequestException:
        return None
    # 404 (no matching index yet) and 503 (transient — e.g. the wildcard resolves against a
    # cluster still settling right after the harness starts) both mean "nothing to see yet",
    # not a real failure; the poll loop's own timeout is what turns persistence into a failure.
    if resp.status_code in (404, 503):
        return None
    resp.raise_for_status()
    hits = resp.json()["hits"]["hits"]
    return hits[0]["_source"] if hits else None


def _poll_for_doc_newer_than(cutoff: datetime) -> dict:
    for _ in range(_POLL_ATTEMPTS):
        doc = _newest_doc()
        if doc is not None:
            doc_ts = datetime.fromisoformat(doc["@timestamp"].replace("Z", "+00:00"))
            if doc_ts > cutoff:
                return doc
        time.sleep(_POLL_INTERVAL_SECONDS)
    raise AssertionError(f"no caddy-access-* document newer than {cutoff.isoformat()} appeared")


def _leaf_field_names(obj: dict, prefix: str = "") -> set[str]:
    names: set[str] = set()
    for key, value in obj.items():
        path = f"{prefix}{key}"
        if isinstance(value, dict) and key not in _OPAQUE_FIELDS:
            names |= _leaf_field_names(value, prefix=f"{path}.")
        else:
            names.add(path)
    return names


@pytest.fixture(scope="module", autouse=True)
def _setup() -> None:
    _require_harness()
    _install_index_template()


def test_ac1_recreating_caddy_alone_does_not_stop_shipping() -> None:
    _send_probe()
    before = _poll_for_doc_newer_than(datetime(1970, 1, 1, tzinfo=timezone.utc))
    cutoff = datetime.fromisoformat(before["@timestamp"].replace("Z", "+00:00"))

    _compose("up", "-d", "--force-recreate", "--no-deps", "caddy")

    _send_probe()
    after = _poll_for_doc_newer_than(cutoff)

    assert after is not None


def test_ac3_governed_types_match_the_committed_index_template() -> None:
    """Types are checked deterministically against the committed contract file — not a baseline."""
    template = json.loads(_TEMPLATE_FILE.read_text())
    expected_types: dict[str, str] = {}

    def _flatten_types(props: dict, prefix: str = "") -> None:
        for key, spec in props.items():
            path = f"{prefix}{key}"
            if "properties" in spec:
                _flatten_types(spec["properties"], prefix=f"{path}.")
            else:
                expected_types[path] = spec["type"]

    _flatten_types(template["template"]["mappings"]["properties"]["caddy"]["properties"])

    resp = requests.get(f"{_ES_URL}/caddy-access-*/_mapping", timeout=10)
    resp.raise_for_status()
    (index_body,) = resp.json().values()
    caddy_props = index_body["mappings"]["properties"]["caddy"]["properties"]

    actual_types: dict[str, str] = {}

    def _flatten_actual(props: dict, prefix: str = "") -> None:
        for key, spec in props.items():
            path = f"{prefix}{key}"
            if "properties" in spec:
                _flatten_actual(spec["properties"], prefix=f"{path}.")
            else:
                actual_types[path] = spec["type"]

    _flatten_actual(caddy_props)

    for field, expected_type in expected_types.items():
        assert actual_types.get(field) == expected_type, (
            f"caddy.{field}: expected type {expected_type!r}, got {actual_types.get(field)!r}"
        )


def test_ac3_field_names_match_the_pre_fix_baseline() -> None:
    """Exact match, not superset-or-equal — a silently dropped or renamed field must fail this."""
    doc = _newest_doc()
    assert doc is not None
    actual_fields = _leaf_field_names(doc["caddy"])

    baseline = json.loads(_BASELINE_FILE.read_text())
    expected_fields = set(baseline["caddy_leaf_fields"])

    assert actual_fields == expected_fields
