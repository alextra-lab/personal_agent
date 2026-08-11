"""FRE-1072 / ADR-0129 D6 — acceptance tests for the Tempo + Grafana stack.

Exercises the deployed Tempo and Grafana services directly over HTTP against fixture data
injected at Tempo's own OTLP receiver and Elasticsearch's own index API — none of these tests
wait on FRE-1070's OTel Collector or on real application traffic, matching the ticket's own AC
preamble.

Requires the dev compose stack up:

    docker compose up -d tempo grafana elasticsearch

FRE-375 note: this suite writes fixture documents directly to the same Elasticsearch instance
Grafana's `es-agent-logs` datasource queries — the dev-compose (prod-equivalent) substrate, not
the isolated `make test-infra-up` stack, because AC-3's correlation only means something proven
against the real datasource wiring. This is the documented "acceptance tests against
prod-equivalent stack only" escape hatch (tests/CLAUDE.md); run with:

    AGENT_ALLOW_TEST_WRITES_TO_PROD_SUBSTRATE=1 PERSONAL_AGENT_INTEGRATION=1 \
        pytest -m integration tests/integration/test_fre1072_tempo_grafana_acceptance.py -v

Fixture documents are written to `agent-logs-fixture-test`, matching the `agent-logs*` index
pattern the `es-agent-logs` datasource is provisioned against, so they route the same way real
records would without touching a real dated index.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import requests
import yaml

pytestmark = pytest.mark.integration

# Overridable so this suite can run against a docker-compose stack with remapped host ports
# (e.g. a shared VPS where sibling worktree sessions already occupy the standard ports) without
# editing the test — the defaults are the documented dev-compose ports.
_TEMPO_URL = os.environ.get("FRE1072_TEMPO_URL", "http://localhost:3200")
# 4328, not 4318: FRE-1224 moved Tempo's host bindings to 4327/4328 because host 4318 belongs to
# the Mac-local OTel Collector (slm_server's compiled-in default OTLP endpoint).
_TEMPO_OTLP_URL = os.environ.get("FRE1072_TEMPO_OTLP_URL", "http://localhost:4328")
_GRAFANA_URL = os.environ.get("FRE1072_GRAFANA_URL", "http://localhost:3000")
_ES_URL = os.environ.get("FRE1072_ES_URL", "http://localhost:9200")  # fre-375-allow: see docstring
_GRAFANA_ADMIN_AUTH = ("admin", os.environ.get("GRAFANA_ADMIN_PASSWORD", "grafana_dev_password"))
_FIXTURE_INDEX = "agent-logs-fixture-test"


def _require_writes_opt_in() -> None:
    if not os.environ.get("AGENT_ALLOW_TEST_WRITES_TO_PROD_SUBSTRATE"):
        pytest.skip(
            "requires AGENT_ALLOW_TEST_WRITES_TO_PROD_SUBSTRATE=1 (FRE-375 escape hatch — "
            "this suite writes fixtures to the dev-compose Elasticsearch, see module docstring)"
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
            ("Tempo", f"{_TEMPO_URL}/ready"),
            ("Grafana", f"{_GRAFANA_URL}/api/health"),
            ("Elasticsearch", _ES_URL),
        )
        if not _http_reachable(url)
    ]
    if missing:
        pytest.skip(
            f"{', '.join(missing)} not reachable — run `docker compose up -d tempo grafana elasticsearch`"
        )


def _inject_span(trace_id: str, span_name: str) -> None:
    now_ns = int(time.time() * 1e9)
    body = {
        "resourceSpans": [
            {
                "resource": {
                    "attributes": [
                        {"key": "service.name", "value": {"stringValue": "fre1072-fixture"}}
                    ]
                },
                "scopeSpans": [
                    {
                        "scope": {"name": "fre1072-fixture-injector"},
                        "spans": [
                            {
                                "traceId": trace_id,
                                "spanId": trace_id[:16],
                                "name": span_name,
                                "kind": 1,
                                "startTimeUnixNano": str(now_ns - 500_000_000),
                                "endTimeUnixNano": str(now_ns),
                            }
                        ],
                    }
                ],
            }
        ]
    }
    r = requests.post(f"{_TEMPO_OTLP_URL}/v1/traces", json=body, timeout=10)
    r.raise_for_status()


def _index_log_fixture(trace_id: str, message: str) -> None:
    doc = {
        "@timestamp": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.000Z"),
        "trace_id": trace_id,
        "event_type": "fre1072_fixture",
        "message": message,
    }
    r = requests.post(f"{_ES_URL}/{_FIXTURE_INDEX}/_doc", json=doc, timeout=10)
    r.raise_for_status()


@pytest.fixture
def fixture_trace_id() -> Iterator[str]:
    _require_stack()
    yield uuid.uuid4().hex


class TestAC1MaxDuration:
    """AC-1 — a 14-day TraceQL metrics query is accepted, not rejected on the duration limit."""

    def test_fourteen_day_window_accepted(self) -> None:
        _require_stack()
        end = int(time.time())
        start = end - 14 * 24 * 3600
        r = requests.get(
            f"{_TEMPO_URL}/api/metrics/query_range",
            params={"q": "{} | rate()", "start": start, "end": end, "step": "1h"},
            timeout=15,
        )
        assert r.status_code == 200, (
            f"14-day TraceQL metrics query was rejected: {r.status_code} {r.text} — "
            "the documented Tempo default (24h) would reject this; query_frontend.metrics.max_duration "
            "must be raised in docker/tempo/tempo.yaml"
        )

    def test_configured_max_is_still_enforced(self) -> None:
        """Guards against a no-op override: an excessive window must still be rejected."""
        _require_stack()
        end = int(time.time())
        start = end - 400 * 3600  # comfortably past the configured 360h ceiling
        r = requests.get(
            f"{_TEMPO_URL}/api/metrics/query_range",
            params={"q": "{} | rate()", "start": start, "end": end, "step": "1h"},
            timeout=15,
        )
        assert r.status_code == 400
        assert "exceeds the maximum allowed duration" in r.text


class TestAC2FixtureSpanRetrievable:
    """AC-2 — a fixture span is retrievable from Tempo by its trace id."""

    def test_injected_span_fetched_by_trace_id(self, fixture_trace_id: str) -> None:
        _inject_span(fixture_trace_id, "fre1072-ac2-fixture-span")
        time.sleep(2)
        r = requests.get(
            f"{_TEMPO_URL}/api/traces/{fixture_trace_id}",
            headers={"Accept": "application/json"},
            timeout=10,
        )
        assert r.status_code == 200
        data: dict[str, Any] = r.json()
        span = data["batches"][0]["scopeSpans"][0]["spans"][0]
        assert span["name"] == "fre1072-ac2-fixture-span"


class TestAC3TraceToLogsCorrelation:
    """AC-3 — trace-to-logs resolves in both directions for a fixture pair sharing trace_id."""

    def test_es_query_for_span_trace_id_returns_fixture_log(self, fixture_trace_id: str) -> None:
        """Trace→logs direction: replicate the query Grafana's tracesToLogsV2 config constructs
        (`trace_id:"<traceId>"` against the es-agent-logs datasource) and confirm it resolves the
        fixture log record sharing that trace id — the same query verified live via Grafana's own
        Explore UI (a real "Explore the logs for this in split view" link, clicked and confirmed to
        return exactly this fixture) during implementation.
        """
        _require_writes_opt_in()
        _inject_span(fixture_trace_id, "fre1072-ac3-fixture-span")
        message = f"fre-1072 AC-3 fixture {fixture_trace_id}"
        _index_log_fixture(fixture_trace_id, message)

        def _query() -> dict[str, Any]:
            r = requests.post(
                f"{_GRAFANA_URL}/api/ds/query",
                auth=_GRAFANA_ADMIN_AUTH,
                json={
                    "queries": [
                        {
                            "refId": "A",
                            "datasource": {"type": "elasticsearch", "uid": "es-agent-logs"},
                            "query": f'trace_id:"{fixture_trace_id}"',
                            "metrics": [{"type": "logs", "id": "1"}],
                        }
                    ],
                    "from": "now-1h",
                    "to": "now",
                },
                timeout=15,
            )
            r.raise_for_status()
            frame_result: dict[str, Any] = r.json()["results"]["A"]["frames"][0]
            return frame_result

        # Elasticsearch's default 1s refresh interval means a just-written document isn't
        # immediately searchable — poll rather than trust a fixed sleep (flaky live, especially
        # right after this fixture index is first created).
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
        values = frame["data"]["values"]
        # Field-level columns vary by index mapping (an ad-hoc test index vs. one matching the
        # real template) — `_source` is always present and always carries the full document, so
        # asserting against it is robust to that difference.
        source_col = values[names.index("_source")]
        assert any(message in str(doc) for doc in source_col), (
            f"fixture message not found in matched docs: {source_col}"
        )

    def test_fixture_log_carries_trace_id_datalink_to_tempo(self, fixture_trace_id: str) -> None:
        """Logs→trace direction: the es-agent-logs datasource must be provisioned with a data
        link on `trace_id` targeting the Tempo datasource — verified live via the Grafana UI
        (the expanded log row's "Links" section showed "trace_id → Tempo", clicked and confirmed
        to resolve the same trace). This test asserts the provisioning config that produces that
        link is actually in place.
        """
        r = requests.get(
            f"{_GRAFANA_URL}/api/datasources/uid/es-agent-logs",
            auth=_GRAFANA_ADMIN_AUTH,
            timeout=10,
        )
        assert r.status_code == 200
        data_links = r.json()["jsonData"]["dataLinks"]
        assert any(
            link["field"] == "trace_id" and link["datasourceUid"] == "tempo" for link in data_links
        ), f"expected a trace_id -> tempo data link, got {data_links}"


class TestAC6DashboardPanelsExecuteCleanly:
    """AC-6 — every dashboard in the FRE-533 inventory has a named Grafana equivalent whose
    query executes against its datasource without a query or datasource error.

    Walks every provisioned dashboard (config/grafana/dashboards/*.json — 14, per the corrected
    inventory in the FRE-1072 plan's Revision 2 §0, decremented by FRE-1209's `cost_budget` rebuild)
    and re-issues each panel's own target through Grafana's /api/ds/query, the same execution path
    a rendered panel uses. Emptiness is not a failure per the ticket's own AC-6 text; only a
    query/datasource error is.
    """

    def test_every_panel_query_executes_without_error(self) -> None:
        _require_stack()
        r = requests.get(
            f"{_GRAFANA_URL}/api/search",
            params={"type": "dash-db"},
            auth=_GRAFANA_ADMIN_AUTH,
            timeout=10,
        )
        assert r.status_code == 200
        dashboards = r.json()
        # "rebuilt-from-kibana", not just "fre-1072" — the health_check.json canary (AC-5) also
        # carries the fre-1072 tag but is explicitly not part of the AC-6 rebuild inventory.
        # FRE-1209 dropped this tag from cost_budget.json (rebuilt onto Postgres, tagged
        # grafana-native instead), decrementing 15→14. FRE-1212 deleted the empty request_traces.json
        # (all 3 panels disposed delete), decrementing 14→13.
        ours = [d for d in dashboards if "rebuilt-from-kibana" in d.get("tags", [])]
        assert len(ours) == 13, (
            f"expected 13 FRE-1072 dashboards provisioned, found {len(ours)}: {[d['title'] for d in ours]}"
        )

        now_ms = int(time.time() * 1000)
        failures: list[str] = []
        total_panels = 0

        for d in ours:
            dr = requests.get(
                f"{_GRAFANA_URL}/api/dashboards/uid/{d['uid']}",
                auth=_GRAFANA_ADMIN_AUTH,
                timeout=10,
            )
            assert dr.status_code == 200
            panels = dr.json()["dashboard"].get("panels", [])
            for panel in panels:
                total_panels += 1
                targets = panel.get("targets", [])
                if not targets:
                    continue
                queries = [{**t, "datasource": panel["datasource"]} for t in targets]
                qr = requests.post(
                    f"{_GRAFANA_URL}/api/ds/query",
                    auth=_GRAFANA_ADMIN_AUTH,
                    json={
                        "queries": queries,
                        "from": str(now_ms - 24 * 3600 * 1000),
                        "to": str(now_ms),
                    },
                    timeout=20,
                )
                if qr.status_code != 200:
                    failures.append(
                        f"[{d['title']}] {panel['title']}: HTTP {qr.status_code} {qr.text[:200]}"
                    )
                    continue
                for result in qr.json().get("results", {}).values():
                    if result.get("error"):
                        failures.append(f"[{d['title']}] {panel['title']}: {result['error']}")

        assert total_panels > 0
        assert not failures, f"{len(failures)}/{total_panels} panels failed:\n" + "\n".join(
            failures
        )


class TestAC10GrafanaAuthBracketing:
    """AC-10 — Grafana serves anonymously at no more than Viewer, hides its login form, and
    still admits the admin over basic auth. Three checks, per the ticket's own spec.
    """

    def test_anonymous_role_bracketed_from_both_sides(self) -> None:
        """(a) Anonymous can query (role >= Viewer) but cannot create a dashboard (role <= Viewer).
        Both halves required: querying alone doesn't rule out Editor/Admin, and a 403 alone
        doesn't rule out a misconfiguration that denies everyone including Viewer.
        """
        _require_stack()
        now_ms = int(time.time() * 1000)
        query_resp = requests.post(
            f"{_GRAFANA_URL}/api/ds/query",
            json={
                "queries": [
                    {
                        "refId": "A",
                        "datasource": {"type": "elasticsearch", "uid": "es-agent-logs"},
                        "query": "*",
                        "metrics": [{"type": "count", "id": "1"}],
                        "bucketAggs": [
                            {
                                "type": "date_histogram",
                                "id": "2",
                                "field": "@timestamp",
                                "settings": {"interval": "1h"},
                            }
                        ],
                    }
                ],
                "from": str(now_ms - 3_600_000),
                "to": str(now_ms),
            },
            timeout=15,
        )
        assert query_resp.status_code == 200, (
            f"anonymous query must succeed (role >= Viewer): {query_resp.text}"
        )

        create_resp = requests.post(
            f"{_GRAFANA_URL}/api/dashboards/db",
            json={
                "dashboard": {"title": f"fre1072-ac10-probe-{uuid.uuid4().hex}", "panels": []},
                "overwrite": False,
            },
            timeout=10,
        )
        assert create_resp.status_code == 403, (
            f"anonymous dashboard creation must be refused (role <= Viewer): {create_resp.status_code} {create_resp.text}"
        )

    def test_login_form_hidden(self) -> None:
        """(b) /login itself is fetched — not the UI root, which omits the form under anonymous
        access regardless of this setting (the ticket's own stated discriminator) — and the
        page's embedded bootstrap config confirms disableLoginForm is actually set, not merely
        that no form happened to render.
        """
        _require_stack()
        r = requests.get(f"{_GRAFANA_URL}/login", timeout=10)
        assert r.status_code == 200
        assert '"disableLoginForm":true' in r.text

    def test_admin_endpoint_denies_anonymous_and_admits_admin_basic_auth(self) -> None:
        """(c) GET /api/admin/settings: denied anonymously, succeeds with admin basic auth.
        Both halves required — an endpoint that also serves anonymous callers could pass on the
        admin leg alone even with basic auth disabled or ignored.
        """
        _require_stack()
        anon = requests.get(f"{_GRAFANA_URL}/api/admin/settings", timeout=10)
        assert anon.status_code == 403, (
            f"anonymous admin/settings must be denied: {anon.status_code}"
        )

        admin = requests.get(
            f"{_GRAFANA_URL}/api/admin/settings", auth=_GRAFANA_ADMIN_AUTH, timeout=10
        )
        assert admin.status_code == 200, (
            f"admin basic auth must succeed: {admin.status_code} {admin.text}"
        )


class TestAC4DurationPanel:
    """AC-4 — a single duration source answers the latency question: the request_timing
    dashboard's Tempo-sourced panel reads span duration only (TraceQL quantile_over_time), no
    union with any Elasticsearch field, and returns a non-empty point for a day fixture spans
    exist on.
    """

    def test_panel_query_is_tempo_only_no_es_field(self) -> None:
        dashboard_path = (
            Path(__file__).resolve().parents[2] / "config/grafana/dashboards/request_timing.json"
        )
        dashboard = json.loads(dashboard_path.read_text())
        panel = next(p for p in dashboard["panels"] if "Tempo span duration" in p["title"])
        assert panel["datasource"]["type"] == "tempo"
        target = panel["targets"][0]
        assert target["datasource"]["type"] == "tempo"
        assert "elasticsearch" not in json.dumps(target).lower()
        assert "duration" in target["query"]

    def test_panel_query_returns_nonempty_point_for_fixture_spans(
        self, fixture_trace_id: str
    ) -> None:
        _inject_span(fixture_trace_id, "fre1072-ac4-fixture-span")

        def _query() -> list[dict[str, Any]]:
            r = requests.post(
                f"{_GRAFANA_URL}/api/ds/query",
                auth=_GRAFANA_ADMIN_AUTH,
                json={
                    "queries": [
                        {
                            "refId": "A",
                            "datasource": {"type": "tempo", "uid": "tempo"},
                            "queryType": "traceql",
                            "query": "{} | quantile_over_time(duration, 0.5, 0.95)",
                        }
                    ],
                    "from": "now-1h",
                    "to": "now",
                },
                timeout=15,
            )
            r.raise_for_status()
            frames: list[dict[str, Any]] = r.json()["results"]["A"].get("frames", [])
            return frames

        # The local-blocks metrics-generator processor needs the span to be pushed through its
        # own live-trace WAL before it's queryable — not instant (verified live: ~10-15s).
        frames: list[dict[str, Any]] = []
        for _ in range(15):
            frames = _query()
            if frames:
                break
            time.sleep(2)
        assert frames, "expected a non-empty p50/p95 series once fixture spans exist"


class TestAC5GrafanaOnlineAndKibanaRetained:
    """AC-5 — Grafana is online and serving real query results, and Kibana is deliberately
    retained and healthy. Two separable checks per the ticket's own text.
    """

    def test_grafana_health_endpoint(self) -> None:
        _require_stack()
        r = requests.get(f"{_GRAFANA_URL}/api/health", timeout=10)
        assert r.status_code == 200
        assert r.json().get("database") == "ok"

    def test_named_dashboard_panel_serves_the_exact_fixture(self) -> None:
        """Not satisfied by an empty placeholder or a healthy-but-wrong datasource: reads the
        health_check dashboard's own stored panel query (not a hand-typed equivalent) and
        confirms it returns exactly the fixture this test injects, and only that fixture.
        """
        _require_writes_opt_in()
        marker = f"fre1072-ac5-{uuid.uuid4().hex}"
        doc = {
            "@timestamp": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.000Z"),
            "event_type": "fre1072_health_check",
            "message": marker,
        }
        r = requests.post(f"{_ES_URL}/{_FIXTURE_INDEX}/_doc", json=doc, timeout=10)
        r.raise_for_status()

        dr = requests.get(
            f"{_GRAFANA_URL}/api/dashboards/uid/fre1072-health-check",
            auth=_GRAFANA_ADMIN_AUTH,
            timeout=10,
        )
        assert dr.status_code == 200
        panel = dr.json()["dashboard"]["panels"][0]
        target = dict(panel["targets"][0])
        # The panel's own base filter, narrowed to this run's marker — repeated test runs (or a
        # prior run's leftover fixture) otherwise all satisfy the bare event_type filter and the
        # earliest match wins the race, not necessarily this run's own document.
        target["query"] = f'{target["query"]} AND message:"{marker}"'

        frame = None
        for _ in range(10):
            qr = requests.post(
                f"{_GRAFANA_URL}/api/ds/query",
                auth=_GRAFANA_ADMIN_AUTH,
                json={
                    "queries": [{**target, "datasource": panel["datasource"]}],
                    "from": "now-5m",
                    "to": "now",
                },
                timeout=15,
            )
            qr.raise_for_status()
            frame = qr.json()["results"]["A"]["frames"][0]
            if frame["schema"]["meta"]["custom"]["total"] > 0:
                break
            time.sleep(1)
        assert frame is not None
        assert frame["schema"]["meta"]["custom"]["total"] > 0, (
            "health-check panel (scoped to this run's marker) returned no rows for its own fixture"
        )
        names = [f["name"] for f in frame["schema"]["fields"]]
        source_col = frame["data"]["values"][names.index("_source")]
        assert any(marker in str(doc) for doc in source_col), (
            f"fixture marker not found: {source_col}"
        )

    def test_kibana_still_declared_in_cloud_compose(self) -> None:
        compose_path = Path(__file__).resolve().parents[2] / "docker-compose.cloud.yml"
        doc = yaml.safe_load(compose_path.read_text())
        assert "kibana" in doc["services"], (
            "Kibana retention is a deliberate design decision (ADR-0129 D6) — must stay declared"
        )

    @pytest.mark.skipif(
        not Path("/opt/seshat").is_dir(), reason="requires /opt/seshat (this VPS only)"
    )
    def test_kibana_status_available_live(self) -> None:
        r = requests.get("http://localhost:5601/api/status", timeout=10)
        assert r.status_code == 200
        assert '"level":"available"' in r.text
