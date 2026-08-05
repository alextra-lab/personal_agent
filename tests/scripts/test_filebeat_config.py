# ruff: noqa: D103
"""FRE-1146 / ADR-0132 D3 — static validation of config/filebeat/filebeat.yml.

Parses the repo's Filebeat config directly (no live Filebeat/ES touched) and asserts the
shape a codex plan-review round required: a stable filestream input id, the caddy-only
stream/logger filtering, and that Filebeat's own template/ILM machinery stays out of the way
of scripts/setup-elasticsearch.sh (ADR-0128's single sanctioned mapping path).
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


def test_input_path_uses_resolved_container_id_placeholder() -> None:
    config = _load_config()
    paths = config["filebeat.inputs"][0]["paths"]
    assert any("${CADDY_CONTAINER_ID}" in p for p in paths)


def test_container_parser_scoped_to_stdout() -> None:
    """Caddy's access logs are stdout-only; excludes stderr runtime output (codex finding)."""
    config = _load_config()
    parsers = config["filebeat.inputs"][0]["parsers"]
    container_parsers = [p["container"] for p in parsers if "container" in p]
    assert container_parsers, "expected a container parser entry"
    assert container_parsers[0]["stream"] == "stdout"


def test_decode_json_fields_processor_targets_caddy_namespace() -> None:
    config = _load_config()
    decoders = [p["decode_json_fields"] for p in config["processors"] if "decode_json_fields" in p]
    assert len(decoders) == 1
    decoder = decoders[0]
    assert decoder["fields"] == ["message"]
    assert decoder["target"] == "caddy"


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
