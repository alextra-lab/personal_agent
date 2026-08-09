# ruff: noqa: D103
"""FRE-1224 — the Mac-local Collector config: custody, buffering, HTTP-only egress.

Source-only, in the style of test_otel_collector_compose_service.py: parses the committed YAML
directly, so it always runs under plain `make test` with no docker, no live Collector and no
network. The genuinely live half of AC-4 (queue overflow actually dropping, and the drop being
observable) needs a running binary and is proven by the probe documented in
docs/guides/MAC_OTEL_COLLECTOR.md — a static parse cannot assert runtime behaviour and this file
does not pretend to.
"""

from __future__ import annotations

import re

import yaml

from personal_agent.config.config_guard import repo_root

_CONFIG_PATH = "config/otel/mac-collector-config.yaml"

# The three variables the launch wrapper must export. Named here so this test and
# test_mac_otelcol_launch_contract.py cannot drift apart silently.
REQUIRED_ENV_VARS = (
    "SESHAT_OTLP_INGRESS_URL",
    "SESHAT_OTLP_CF_ACCESS_CLIENT_ID",
    "SESHAT_OTLP_CF_ACCESS_CLIENT_SECRET",
)

# Anchored: for asserting a single value is *exactly* an env reference and nothing more.
_ENV_REF = re.compile(r"^\$\{env:([A-Z0-9_]+)\}$")
# Unanchored: for sweeping the whole file text. Keep both — using the anchored pattern with
# findall silently matches nothing and the assertion then passes vacuously.
_ENV_REF_SCAN = re.compile(r"\$\{env:([A-Z0-9_]+)\}")

# FRE-1239's merged Caddy block caps a single request at 20MiB. The batch processor must carry a
# hard span ceiling so a post-outage backlog flush cannot build an export that the edge refuses.
_CADDY_REQUEST_CAP_MIB = 20


def _raw_text() -> str:
    return (repo_root() / _CONFIG_PATH).read_text()


def _config() -> dict[str, object]:
    doc = yaml.safe_load(_raw_text())
    assert isinstance(doc, dict), "collector config must parse to a mapping"
    return doc


def _egress_exporter_name() -> str:
    """The one network exporter, i.e. everything that is not the local `debug` exporter."""
    names = [name for name in _config()["exporters"] if name != "debug"]
    assert len(names) == 1, f"expected exactly one egress exporter, got {names!r}"
    return names[0]


class TestReceiver:
    """AC-1 and AC-5 — accepts what slm_server already sends, over HTTP only."""

    def test_http_receiver_binds_loopback_on_4318(self) -> None:
        """slm_server's default endpoint is http://localhost:4318, so this port is not a free
        choice — it is the contract that lets AC-1 hold with no producer-side change.
        """
        protocols = _config()["receivers"]["otlp"]["protocols"]
        assert protocols["http"]["endpoint"] == "127.0.0.1:4318"

    def test_receiver_is_loopback_not_all_interfaces(self) -> None:
        """Unlike the VPS Collector (0.0.0.0, it serves the compose network), the Mac agent must
        not be reachable from off-box: nothing outside this machine may inject spans that would
        then be forwarded under our Cloudflare Access credential.
        """
        endpoint = _config()["receivers"]["otlp"]["protocols"]["http"]["endpoint"]
        assert not endpoint.startswith("0.0.0.0"), f"receiver is off-box reachable: {endpoint!r}"

    def test_no_grpc_receiver_is_declared(self) -> None:
        """slm_server speaks OTLP/HTTP (verified in slm_server telemetry.py at ea2b0b8), so a
        gRPC receiver would be unused listening surface. ADR-0136 keeps gRPC off this path.
        """
        protocols = _config()["receivers"]["otlp"]["protocols"]
        assert "grpc" not in protocols


class TestEgressExporter:
    """AC-2, AC-3, AC-5 — the custody boundary and the shape the Caddy allowlist accepts."""

    def test_exporter_is_otlp_http_not_the_deprecated_alias(self) -> None:
        """v0.158.0's otlphttp exporter README states `otlphttp` is a deprecated alias for
        `otlp_http` and will be removed. Committing the alias buys future churn for nothing.
        """
        name = _egress_exporter_name()
        assert name.split("/")[0] == "otlp_http", f"expected otlp_http exporter, got {name!r}"

    def test_endpoint_is_an_env_reference_never_a_literal_host(self) -> None:
        """AC-3 — the ingress hostname is deployment identity (FRE-895) and must not be committed."""
        spec = _config()["exporters"][_egress_exporter_name()]
        assert _ENV_REF.match(str(spec["endpoint"])), (
            f"endpoint must be a ${{env:...}} reference, got {spec['endpoint']!r}"
        )

    def test_endpoint_uses_base_form_so_it_matches_the_caddy_allowlist(self) -> None:
        """FRE-1239's block allows `method POST` + `^/v1/traces$` + empty query only. The base
        `endpoint` form makes the exporter append /v1/traces with no query, matching by
        construction; a hand-written `traces_endpoint` could drift off the allowlisted path.
        """
        spec = _config()["exporters"][_egress_exporter_name()]
        assert "traces_endpoint" not in spec

    def test_both_cf_access_headers_are_env_references(self) -> None:
        """AC-2 — the Collector is the custodian, and AC-3 — no secret is committed."""
        headers = _config()["exporters"][_egress_exporter_name()]["headers"]
        for header in ("CF-Access-Client-Id", "CF-Access-Client-Secret"):
            assert header in headers, f"missing {header}"
            assert _ENV_REF.match(str(headers[header])), (
                f"{header} must be a ${{env:...}} reference, got {headers[header]!r}"
            )

    def test_env_references_match_the_documented_variable_names(self) -> None:
        """Guards the contract shared with the launch wrapper: a rename on one side only would
        otherwise surface as empty Access headers at runtime, not as a test failure.
        """
        referenced = set(_ENV_REF_SCAN.findall(_raw_text()))
        assert set(REQUIRED_ENV_VARS) == referenced, (
            f"config references {referenced!r}, expected {set(REQUIRED_ENV_VARS)!r}"
        )


class TestBuffering:
    """AC-4 — the queue and retry behaviour, to the bound the criterion actually claims."""

    def test_sending_queue_is_enabled(self) -> None:
        spec = _config()["exporters"][_egress_exporter_name()]
        assert spec["sending_queue"]["enabled"] is True

    def test_retry_is_enabled_and_never_gives_up(self) -> None:
        """max_elapsed_time: 0 means retries never stop (v0.158.0 exporterhelper). Any positive
        value would silently define a window after which spans are discarded.
        """
        retry = _config()["exporters"][_egress_exporter_name()]["retry_on_failure"]
        assert retry["enabled"] is True
        assert retry["max_elapsed_time"] == 0

    def test_batch_has_a_hard_span_ceiling(self) -> None:
        """FRE-1239's Caddy block caps one request at 20MiB. Default `batch` has no hard ceiling,
        so a post-outage backlog flush — the deepest-queue moment — could build an export the edge
        refuses. send_batch_max_size bounds it.
        """
        batch = _config()["processors"]["batch"]
        assert batch["send_batch_max_size"] > 0
        assert batch["send_batch_max_size"] >= batch["send_batch_size"]


class TestNoTelemetryLeakIntoLocalLogs:
    """Codex plan-review, Should-Fix: launchd redirects stdout to a persistent file."""

    def test_debug_exporter_is_basic_verbosity(self) -> None:
        """`normal`/`detailed` writes every span attribute into an ever-growing local log, and it
        grows fastest exactly while the downstream is intentionally unreachable. `basic` reports
        counts, which is all AC-1 needs to prove receipt.
        """
        assert _config()["exporters"]["debug"]["verbosity"] == "basic"


class TestNoGrpcAnywhere:
    """AC-5 / ADR-0136 — the edge never carries gRPC, and the zone toggle stays off."""

    def test_port_4317_appears_nowhere_in_the_config(self) -> None:
        assert "4317" not in _raw_text()


class TestPipeline:
    def test_traces_pipeline_wiring(self) -> None:
        traces = _config()["service"]["pipelines"]["traces"]
        assert traces["receivers"] == ["otlp"]
        assert traces["processors"] == ["batch"]
        assert set(traces["exporters"]) == {_egress_exporter_name(), "debug"}

    def test_only_a_traces_pipeline_is_declared(self) -> None:
        """This Collector exists to carry slm_server's spans. A metrics or logs pipeline would be
        unrequested scope carrying the same credential.
        """
        assert set(_config()["service"]["pipelines"]) == {"traces"}
