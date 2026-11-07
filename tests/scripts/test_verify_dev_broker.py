# ruff: noqa: D103
"""The decision logic of scripts/verify_dev_broker.py (FRE-1683). The live run is post-deploy."""

from __future__ import annotations

from typing import Any

from scripts.verify_dev_broker import EXPECTED_ENV, inspect_violations, scan_violations


def _good_inspect() -> dict[str, Any]:
    return {
        "HostConfig": {
            "ReadonlyRootfs": True,
            "CapDrop": ["ALL"],
            "SecurityOpt": ["no-new-privileges:true"],
            "Privileged": False,
            "PortBindings": {},
        },
        "Config": {
            "User": "65534:65534",
            "Env": [f"{name}=x" for name in sorted(EXPECTED_ENV)] + ["PATH=/usr/bin", "LANG=C"],
        },
        "Mounts": [],
        "NetworkSettings": {"Networks": {"seshat_cloud-sim": {}, "seshat_dev-agent": {}}},
    }


def test_a_matching_container_has_no_violation() -> None:
    assert inspect_violations(_good_inspect()) == []


def test_each_boundary_change_is_reported() -> None:
    cases = [
        ("HostConfig", "ReadonlyRootfs", False, "writable"),
        ("HostConfig", "CapDrop", [], "capabilities"),
        ("HostConfig", "SecurityOpt", [], "no-new-privileges"),
        ("HostConfig", "Privileged", True, "privileged"),
        ("HostConfig", "PortBindings", {"8700/tcp": [{}]}, "publishes"),
        ("Config", "User", "", "root"),
    ]
    for section, key, value, word in cases:
        inspect = _good_inspect()
        inspect[section][key] = value
        assert any(word in v for v in inspect_violations(inspect)), (key, value)


def test_a_socket_mount_is_reported() -> None:
    inspect = _good_inspect()
    inspect["Mounts"] = [{"Destination": "/var/run/docker.sock"}]
    assert any("mounts" in v for v in inspect_violations(inspect))


def test_an_undeclared_variable_is_reported_by_name_only() -> None:
    inspect = _good_inspect()
    inspect["Config"]["Env"].append("AGENT_ANTHROPIC_API_KEY=sk-secret-value")
    [violation] = inspect_violations(inspect)
    assert "AGENT_ANTHROPIC_API_KEY" in violation
    assert "sk-secret-value" not in violation


def test_a_third_network_is_reported() -> None:
    inspect = _good_inspect()
    inspect["NetworkSettings"]["Networks"]["seshat_dev-agent-egress"] = {}
    assert any("3 networks" in v for v in inspect_violations(inspect))


def test_a_scan_with_only_the_listener_passes() -> None:
    scan = {"172.19.0.3": [8700], "172.25.0.9": []}
    assert scan_violations(scan, own="172.19.0.3", other="172.25.0.9", expected_port=8700) == []


def test_an_extra_open_port_fails() -> None:
    scan = {"172.19.0.3": [8700, 8701], "172.25.0.9": []}
    assert scan_violations(scan, own="172.19.0.3", other="172.25.0.9", expected_port=8700)


def test_a_missing_listener_fails() -> None:
    """A scan that finds nothing proves nothing: the listener must answer."""
    scan: dict[str, list[int]] = {"172.19.0.3": [], "172.25.0.9": []}
    assert scan_violations(scan, own="172.19.0.3", other="172.25.0.9", expected_port=8700)


def test_the_other_networks_address_answering_fails() -> None:
    scan = {"172.19.0.3": [8700], "172.25.0.9": [8701]}
    assert scan_violations(scan, own="172.19.0.3", other="172.25.0.9", expected_port=8700)
