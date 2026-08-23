# ruff: noqa: D103
"""FRE-1146 / ADR-0132 D3 / FRE-1243 — static validation of config/filebeat/filebeat.yml.

Parses the repo's Filebeat config directly (no live Filebeat/ES touched) and asserts the
shape a codex plan-review round required: a stable filestream input id, the fixed-path
input (FRE-1243 — no more container-ID resolution), the caddy-only logger filtering, the
@timestamp fidelity processor, and that Filebeat's own template/ILM machinery stays out of
the way of scripts/setup-elasticsearch.sh (ADR-0128's single sanctioned mapping path).
"""

from __future__ import annotations

from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
FILEBEAT_YML = REPO_ROOT / "config" / "filebeat" / "filebeat.yml"


def _load_config() -> dict:
    return yaml.safe_load(FILEBEAT_YML.read_text())


def test_filestream_input_has_stable_id() -> None:
    config = _load_config()
    inputs = config["filebeat.inputs"]
    assert len(inputs) == 1
    assert inputs[0]["type"] == "filestream"
    assert inputs[0]["id"] == "caddy-access"


def test_input_path_is_fixed_caddy_log_path() -> None:
    """FRE-1243: no more per-recreate container-ID resolution — a fixed path, always.

    The second entry matches Caddy's `roll_uncompressed` rotated backups, so Filebeat can still
    find a segment it missed entirely (e.g. it was down across a rotation) rather than losing it.
    """
    config = _load_config()
    paths = config["filebeat.inputs"][0]["paths"]
    assert paths == ["/var/log/caddy/access.log", "/var/log/caddy/access-*.log"]


def test_no_container_parser_present() -> None:
    """FRE-1243: the file already holds Caddy's raw JSON lines — nothing to unwrap."""
    config = _load_config()
    assert "parsers" not in config["filebeat.inputs"][0]


def test_decode_json_fields_processor_targets_caddy_namespace() -> None:
    config = _load_config()
    decoders = [p["decode_json_fields"] for p in config["processors"] if "decode_json_fields" in p]
    assert len(decoders) == 1
    decoder = decoders[0]
    assert decoder["fields"] == ["message"]
    assert decoder["target"] == "caddy"


def test_timestamp_processor_sources_caddy_ts() -> None:
    """FRE-1243: restore @timestamp fidelity lost when the container parser was removed.

    Without it, @timestamp would default to Filebeat's harvest time instead of the real event
    time — restore it from Caddy's own recorded `ts`.
    """
    config = _load_config()
    processors = config["processors"]
    timestamp_procs = [p["timestamp"] for p in processors if "timestamp" in p]
    assert len(timestamp_procs) == 1
    proc = timestamp_procs[0]
    assert proc["field"] == "caddy.ts"
    assert "UNIX" in proc["layouts"]

    decode_idx = next(i for i, p in enumerate(processors) if "decode_json_fields" in p)
    timestamp_idx = next(i for i, p in enumerate(processors) if "timestamp" in p)
    assert timestamp_idx > decode_idx, "timestamp processor must run after caddy.ts is decoded"


def test_drop_event_filters_on_access_logger() -> None:
    """Defensive second filter: only decoded access-log records survive (codex finding)."""
    config = _load_config()
    drops = [p["drop_event"] for p in config["processors"] if "drop_event" in p]
    assert len(drops) == 1
    regexp = drops[0]["when"]["not"]["regexp"]
    assert "caddy.logger" in regexp


def test_output_index_uses_fre1036_monthly_dash_convention() -> None:
    config = _load_config()
    assert config["output.elasticsearch"]["index"] == "caddy-access-%{+yyyy-MM}"


def test_own_template_and_ilm_setup_disabled() -> None:
    """Single-writer rule (ADR-0128): scripts/setup-elasticsearch.sh owns the mapping."""
    config = _load_config()
    assert config["setup.template.enabled"] is False
    assert config["setup.ilm.enabled"] is False
