# ruff: noqa: D103
"""FRE-1203 — the Grafana datasource provisioning file's log-line and Postgres-ledger config.

Source-only: parses the committed YAML directly, no docker/live-infra dependency. Field-name
choices here were verified against sampled live Elasticsearch documents during implementation
(see the FRE-1203 PR description for the per-family population counts) — this suite guards the
*declared config*, not field population, which only a live index can prove (see
tests/integration/test_fre1203_grafana_log_lines_pg_datasource_acceptance.py for that).
"""

from __future__ import annotations

import yaml

from personal_agent.config.config_guard import repo_root

_DATASOURCES_PATH = "config/grafana/provisioning/datasources/datasources.yaml"

# uid -> expected logMessageField, verified against real sampled documents (2026-08-09).
# FRE-1212: es-captains-captures and es-insights deleted (unreferenced per FRE-1207 audit).
_EXPECTED_MESSAGE_FIELDS = {
    "es-agent-logs": "message",
    "es-captains-reflections": "title",
    "es-monitors-joinability": "outcome",
    "es-monitors-slm-health": "status",
    "es-monitors-joinability-substrate": "substrate",
}


def _load_datasources() -> dict[str, dict[str, object]]:
    raw = yaml.safe_load((repo_root() / _DATASOURCES_PATH).read_text())
    return {ds["uid"]: ds for ds in raw["datasources"]}


class TestLogLineRenderingFields:
    """FRE-1203 part 1 — Explore renders a message column, not the raw _source document."""

    def test_all_five_active_es_families_declare_a_verified_message_field(self) -> None:
        datasources = _load_datasources()
        for uid, expected_field in _EXPECTED_MESSAGE_FIELDS.items():
            json_data = datasources[uid]["jsonData"]
            assert json_data.get("logMessageField") == expected_field, (
                f"{uid}: expected logMessageField={expected_field!r}, "
                f"got {json_data.get('logMessageField')!r}"
            )

    def test_only_agent_logs_declares_a_level_field(self) -> None:
        """Only agent-logs carries a genuine severity concept (structlog's `level`); the other
        four families are probe/capture records with no severity field, so declaring one there
        would name a key no record carries — the exact trap the ticket warns against.
        """
        datasources = _load_datasources()
        for uid in _EXPECTED_MESSAGE_FIELDS:
            level_field = datasources[uid]["jsonData"].get("logLevelField")
            if uid == "es-agent-logs":
                assert level_field == "level"
            else:
                assert level_field is None, f"{uid}: unexpected logLevelField={level_field!r}"

    def test_trace_id_datalink_untouched(self) -> None:
        """This ticket only adds message/level fields — the existing FRE-1072 trace_id -> Tempo
        data link on es-agent-logs must survive unchanged.
        """
        data_links = _load_datasources()["es-agent-logs"]["jsonData"]["dataLinks"]
        assert any(
            link["field"] == "trace_id" and link["datasourceUid"] == "tempo" for link in data_links
        )


class TestPostgresLedgerDatasource:
    """FRE-1203 part 2 — a Postgres datasource for aggregate cost, behind a read-only role."""

    def test_datasource_declared_with_expected_shape(self) -> None:
        ds = _load_datasources()["pg-ledger"]
        assert ds["type"] == "grafana-postgresql-datasource"
        assert ds["access"] == "proxy"
        assert ds["url"] == "postgres:5432"
        assert ds["jsonData"]["database"] == "personal_agent"

    def test_connects_as_the_readonly_role_not_a_privileged_one(self) -> None:
        ds = _load_datasources()["pg-ledger"]
        assert ds["user"] == "grafana_ro"
        assert ds["user"] not in ("agent", "seshat_app", "sysgraph_role", "recall_role")

    def test_credential_is_env_substituted_not_literal(self) -> None:
        """Single-`$` (not the `$$`-escaped Grafana template vars elsewhere in this file) is
        substituted from the container's environment by Grafana's provisioning loader — verified
        live during implementation (secureJsonFields.password reads True only when
        GRAFANA_RO_PASSWORD is set in the environment).
        """
        ds = _load_datasources()["pg-ledger"]
        assert ds["secureJsonData"]["password"] == "$GRAFANA_RO_PASSWORD"

    def test_no_literal_secret_anywhere_in_the_tracked_file(self) -> None:
        raw = (repo_root() / _DATASOURCES_PATH).read_text()
        assert "grafana_ro_dev_password" not in raw
