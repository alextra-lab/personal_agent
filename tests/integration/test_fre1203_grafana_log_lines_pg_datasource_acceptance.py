"""FRE-1203 — acceptance tests for Explore log-line rendering and the Postgres cost datasource.

Exercises the deployed Grafana, Elasticsearch, and Postgres services directly over HTTP/SQL
against fixture data, following the same pattern as
tests/integration/test_fre1072_tempo_grafana_acceptance.py.

Requires the dev compose stack up:

    docker compose up -d grafana elasticsearch postgres

FRE-375 note: this suite writes an ES fixture document to the same Elasticsearch instance
Grafana's es-agent-logs datasource queries, and attempts (refused) writes against the same
Postgres api_costs table the pg-ledger datasource reads — the dev-compose (prod-equivalent)
substrate, not the isolated `make test-infra-up` stack, because these ACs only mean something
proven against the real datasource wiring (grafana_ro's actual grant, the real provisioning
file). This is the documented "acceptance tests against prod-equivalent stack only" escape
hatch (tests/CLAUDE.md); run with:

    AGENT_ALLOW_TEST_WRITES_TO_PROD_SUBSTRATE=1 PERSONAL_AGENT_INTEGRATION=1 \\
        pytest -m integration tests/integration/test_fre1203_grafana_log_lines_pg_datasource_acceptance.py -v

Every mechanic in this suite was independently verified live during implementation against an
isolated Grafana/Postgres pair bind-mounted to this worktree's provisioning files (not the shared
dev-compose stack) — see the FRE-1203 PR description for the transcript.
"""

from __future__ import annotations

import os
import time
import uuid
from datetime import UTC, datetime
from typing import Any

import asyncpg
import pytest
import requests

pytestmark = pytest.mark.integration

_GRAFANA_URL = os.environ.get("FRE1203_GRAFANA_URL", "http://localhost:3000")
_ES_URL = os.environ.get("FRE1203_ES_URL", "http://localhost:9200")  # fre-375-allow: see docstring
_PG_HOST = os.environ.get("FRE1203_PG_HOST", "localhost")
_PG_PORT = int(os.environ.get("FRE1203_PG_PORT", "5432"))
_GRAFANA_ADMIN_AUTH = ("admin", os.environ.get("GRAFANA_ADMIN_PASSWORD", "grafana_dev_password"))
_GRAFANA_RO_PASSWORD = os.environ.get("GRAFANA_RO_PASSWORD", "grafana_ro_dev_password")
_FIXTURE_INDEX = "agent-logs-fixture-test"


def _require_writes_opt_in() -> None:
    if not os.environ.get("AGENT_ALLOW_TEST_WRITES_TO_PROD_SUBSTRATE"):
        pytest.skip(
            "requires AGENT_ALLOW_TEST_WRITES_TO_PROD_SUBSTRATE=1 (FRE-375 escape hatch — "
            "this suite writes fixtures to the dev-compose Elasticsearch/Postgres, see module "
            "docstring)"
        )


def _http_reachable(url: str) -> bool:
    try:
        requests.get(url, timeout=2)
        return True
    except requests.RequestException:
        return False


def _require_stack() -> None:
    missing = [
        name
        for name, url in (
            ("Grafana", f"{_GRAFANA_URL}/api/health"),
            ("Elasticsearch", _ES_URL),
        )
        if not _http_reachable(url)
    ]
    if missing:
        pytest.skip(
            f"{', '.join(missing)} not reachable — run "
            "`docker compose up -d grafana elasticsearch postgres`"
        )


async def _grafana_ro_connection() -> asyncpg.Connection:
    return await asyncpg.connect(
        host=_PG_HOST,
        port=_PG_PORT,
        user="grafana_ro",
        password=_GRAFANA_RO_PASSWORD,
        database="personal_agent",
        timeout=5,
    )


class TestPart1LogLineRendering:
    """FRE-1203 part 1 — Explore renders a message column, not the raw _source document."""

    def test_message_field_resolves_for_a_fixture_log(self) -> None:
        """Writes a fixture doc to the es-agent-logs index pattern with a known message, then
        queries Grafana's own /api/ds/query (the same path Explore uses) and asserts the
        `message` column — not the full `_source` blob — carries that exact value. This is the
        ticket's own AC: "asserting the field is resolved rather than the whole source document
        being returned."
        """
        _require_stack()
        _require_writes_opt_in()
        message = f"fre-1203 fixture message {uuid.uuid4().hex}"
        doc = {
            "@timestamp": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.000Z"),
            "level": "INFO",
            "event_type": "fre1203_fixture",
            "message": message,
        }
        r = requests.post(f"{_ES_URL}/{_FIXTURE_INDEX}/_doc", json=doc, timeout=10)
        r.raise_for_status()

        def _query() -> dict[str, Any]:
            resp = requests.post(
                f"{_GRAFANA_URL}/api/ds/query",
                auth=_GRAFANA_ADMIN_AUTH,
                json={
                    "queries": [
                        {
                            "refId": "A",
                            "datasource": {"type": "elasticsearch", "uid": "es-agent-logs"},
                            "query": f'event_type:"fre1203_fixture" AND message:"{message}"',
                            "metrics": [{"type": "logs", "id": "1"}],
                        }
                    ],
                    "from": "now-1h",
                    "to": "now",
                },
                timeout=15,
            )
            resp.raise_for_status()
            frame: dict[str, Any] = resp.json()["results"]["A"]["frames"][0]
            return frame

        frame = None
        for _ in range(10):
            frame = _query()
            if frame["schema"]["meta"]["custom"]["total"] > 0:
                break
            time.sleep(1)
        assert frame is not None and frame["schema"]["meta"]["custom"]["total"] > 0, (
            "fixture log never became searchable"
        )

        names = [f["name"] for f in frame["schema"]["fields"]]
        assert "message" in names, "message column not present in the query result schema"
        message_col = frame["data"]["values"][names.index("message")]
        assert message in message_col, (
            f"expected {message!r} in the resolved message column, got: {message_col}"
        )

    def test_datasource_config_declares_the_verified_fields(self) -> None:
        """Companion to the static provisioning-file test
        (tests/scripts/test_grafana_datasource_provisioning.py) — this asserts the *live*
        Grafana instance actually loaded the provisioning, not just that the file is correct.
        """
        _require_stack()
        r = requests.get(
            f"{_GRAFANA_URL}/api/datasources/uid/es-agent-logs",
            auth=_GRAFANA_ADMIN_AUTH,
            timeout=10,
        )
        assert r.status_code == 200
        assert r.json()["jsonData"]["logMessageField"] == "message"
        assert r.json()["jsonData"]["logLevelField"] == "level"


class TestPart2PostgresLedgerDatasource:
    """FRE-1203 part 2 — a Postgres datasource for aggregate cost, behind a read-only role."""

    @pytest.mark.asyncio
    async def test_grafana_ro_can_select_the_cost_ledger(self) -> None:
        _require_stack()
        _require_writes_opt_in()
        conn = await _grafana_ro_connection()
        try:
            count = await conn.fetchval("SELECT count(*) FROM api_costs")
            assert count is not None
        finally:
            await conn.close()

    @pytest.mark.asyncio
    async def test_grafana_ro_refused_on_insert_update_delete(self) -> None:
        """Each demonstrated separately, per the ticket's own AC wording."""
        _require_stack()
        _require_writes_opt_in()
        conn = await _grafana_ro_connection()
        try:
            with pytest.raises(asyncpg.InsufficientPrivilegeError):
                await conn.execute(
                    "INSERT INTO api_costs (provider, model, trace_id) "
                    "VALUES ('fre1203-probe', 'fre1203-probe', gen_random_uuid())"
                )
            with pytest.raises(asyncpg.InsufficientPrivilegeError):
                await conn.execute("UPDATE api_costs SET cost_usd = 0 WHERE false")
            with pytest.raises(asyncpg.InsufficientPrivilegeError):
                await conn.execute("DELETE FROM api_costs WHERE false")
        finally:
            await conn.close()

    def test_datasource_credential_resolved_from_environment(self) -> None:
        """Grafana never echoes secureJsonData back in a GET, but secureJsonFields.password
        reads True only when the env-substituted value was actually set — the live analogue of
        the static no-literal-secret test.
        """
        _require_stack()
        r = requests.get(
            f"{_GRAFANA_URL}/api/datasources/uid/pg-ledger",
            auth=_GRAFANA_ADMIN_AUTH,
            timeout=10,
        )
        assert r.status_code == 200
        assert r.json()["secureJsonFields"]["password"] is True

    def test_per_role_spend_aggregate_reachable_via_datasource(self) -> None:
        """Proves the datasource reaches the ledger, not merely that Grafana accepted the
        configuration — the ticket's own AC wording.
        """
        _require_stack()
        r = requests.post(
            f"{_GRAFANA_URL}/api/ds/query",
            auth=_GRAFANA_ADMIN_AUTH,
            json={
                "queries": [
                    {
                        "refId": "A",
                        "datasource": {"type": "grafana-postgresql-datasource", "uid": "pg-ledger"},
                        "rawSql": (
                            "SELECT role, SUM(running_total) AS spend_usd "
                            "FROM budget_counters WHERE time_window = 'daily' "
                            "GROUP BY role ORDER BY spend_usd DESC"
                        ),
                        "format": "table",
                    }
                ],
                "from": "now-24h",
                "to": "now",
            },
            timeout=15,
        )
        r.raise_for_status()
        frame = r.json()["results"]["A"]["frames"][0]
        names = [f["name"] for f in frame["schema"]["fields"]]
        assert names == ["role", "spend_usd"]
