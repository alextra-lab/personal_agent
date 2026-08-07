# ruff: noqa: D103
"""FRE-1072 — dashboard panel targets must not disagree with their datasource's timeField.

Master-gate bounce (2026-08-07): six panels bucketed date_histogram on `@timestamp` against
Elasticsearch families that carry no such field (started_at/probed_at/timestamp instead) —
datasources.yaml already declares the correct timeField per family, but the panels overrode it
with a hardcoded `timeField` key on the target itself, so Grafana's outer date-range filter
matched zero documents. The panels rendered empty against real data while erroring on nothing —
AC-6's live check couldn't catch it, because its own acceptance fixtures were seeded with
@timestamp everywhere, which proves self-consistency, not corpus match.

This is the static, no-live-ES-needed check that would have caught it — runs under plain
`make test`, not gated behind `-m integration`, so it can't silently stop running.
"""

from __future__ import annotations

import json
from pathlib import Path

import yaml

from personal_agent.config.config_guard import repo_root

_DASHBOARDS_DIR = repo_root() / "config/grafana/dashboards"
_DATASOURCES_FILE = repo_root() / "config/grafana/provisioning/datasources/datasources.yaml"


def _datasource_time_fields() -> dict[str, str]:
    doc = yaml.safe_load(_DATASOURCES_FILE.read_text())
    return {
        ds["uid"]: ds["jsonData"]["timeField"]
        for ds in doc["datasources"]
        if ds["type"] == "elasticsearch"
    }


def _dashboard_files() -> list[Path]:
    return sorted(_DASHBOARDS_DIR.glob("*.json"))


def test_no_panel_target_overrides_the_datasource_timefield() -> None:
    """The datasource's own provisioned timeField must govern — a target-level override is
    exactly the bug: it silently zeroes every result against a family whose real time field
    differs from whatever was hardcoded.
    """
    offenders = []
    for path in _dashboard_files():
        dashboard = json.loads(path.read_text())
        for panel in dashboard.get("panels", []):
            for target in panel.get("targets", []):
                if (
                    target.get("datasource", {}).get("type") == "elasticsearch"
                    and "timeField" in target
                ):
                    offenders.append(f"{path.name}::{panel['title']}")
    assert not offenders, (
        f"panel target(s) carry an explicit timeField, overriding the datasource default: {offenders}"
    )


def test_every_date_histogram_field_matches_its_datasource() -> None:
    time_fields = _datasource_time_fields()
    offenders = []
    for path in _dashboard_files():
        dashboard = json.loads(path.read_text())
        for panel in dashboard.get("panels", []):
            for target in panel.get("targets", []):
                ds_uid = target.get("datasource", {}).get("uid")
                if ds_uid not in time_fields:
                    continue
                for agg in target.get("bucketAggs", []):
                    if (
                        agg.get("type") == "date_histogram"
                        and agg.get("field") != time_fields[ds_uid]
                    ):
                        offenders.append(
                            f"{path.name}::{panel['title']} buckets on {agg.get('field')!r}, "
                            f"but {ds_uid} is configured with timeField {time_fields[ds_uid]!r}"
                        )
    assert not offenders, "\n".join(offenders)
