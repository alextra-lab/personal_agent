# ruff: noqa: D101, D102
"""Pure checks of a live dev-agent container against the spec (ADR-0159 AC-3; FRE-1682).

Each check has a passing case and a seeded negative. A check that cannot fail proves nothing.
"""

from __future__ import annotations

import copy
from collections.abc import Callable, Mapping

import pytest

from personal_agent.dev_agent.container_spec import (
    ContainerProfile,
    load_profiles,
    tmpfs_option,
)
from personal_agent.dev_agent.verification import (
    REQUIRED_ALLOWED_LABELS,
    REQUIRED_DENIED_LABELS,
    csp_header_violations,
    inspect_violations,
    probe_report_violations,
)

_NETWORK = "proj_dev-agent"


def _volumes_of(profile: ContainerProfile) -> dict[str, str]:
    """One plain volume name per mount target of the profile."""
    return {m.target: "vol-" + m.target.strip("/").replace("/", "-") for m in profile.mounts}


Inspect = dict[str, dict[str, object]]


def _inspect_for(profile: ContainerProfile) -> dict[str, object]:
    """A `docker inspect` object in the shape the daemon returns, built from the spec."""
    network = _NETWORK if profile.network == "dev-agent" else "none"
    return {
        "Config": {
            "User": profile.user,
            "Env": [f"{k}={v}" for k, v in profile.env_fixed.items()]
            + [f"{n}=secret" for n in profile.env_from_launcher],
        },
        "HostConfig": {
            "CapDrop": list(profile.cap_drop),
            "CapAdd": None,
            "SecurityOpt": ["no-new-privileges"],
            "Memory": profile.memory_mb * 1024 * 1024,
            "MemorySwap": profile.memory_mb * 1024 * 1024,
            "NanoCpus": int(profile.cpus * 1_000_000_000),
            "PidsLimit": profile.pids_limit,
            "ReadonlyRootfs": True,
            "Init": profile.init,
            "Privileged": False,
            "NetworkMode": network,
            "IpcMode": "private",
            "PidMode": "",
            "UsernsMode": "",
            "Devices": [],
            "PortBindings": {},
            "Tmpfs": {
                t.path: tmpfs_option(t, profile.user).split(":", 1)[1] for t in profile.tmpfs
            },
            "Binds": [
                f"{_volumes_of(profile)[m.target]}:{m.target}{':ro' if m.read_only else ''}"
                for m in profile.mounts
            ],
        },
        "Mounts": [
            {
                "Type": "volume",
                "Name": _volumes_of(profile)[m.target],
                "Source": f"/var/lib/docker/volumes/{_volumes_of(profile)[m.target]}/_data",
                "Destination": m.target,
                "RW": not m.read_only,
            }
            for m in profile.mounts
        ],
        "NetworkSettings": {"Networks": {network: {}}},
    }


def _violations(profile: ContainerProfile, inspect: Mapping[str, object]) -> list[str]:
    return inspect_violations(profile, inspect, network_name=_NETWORK, volumes=_volumes_of(profile))


def _mutate(path: tuple[str, ...], value: object) -> Callable[[dict[str, object]], None]:
    def apply(obj: dict[str, object]) -> None:
        node: dict[str, object] = obj
        for key in path[:-1]:
            child = node[key]
            assert isinstance(child, dict)
            node = child
        node[path[-1]] = value

    return apply


class TestInspect:
    @pytest.mark.parametrize("which", ["agent", "check", "handoff"])
    def test_a_container_that_matches_the_spec_has_no_violation(self, which: str) -> None:
        profile = getattr(load_profiles(), which)
        assert _violations(profile, _inspect_for(profile)) == []

    @pytest.mark.parametrize(
        ("label", "change"),
        [
            ("cap added", _mutate(("HostConfig", "CapAdd"), ["NET_ADMIN"])),
            ("cap not dropped", _mutate(("HostConfig", "CapDrop"), [])),
            ("privileged", _mutate(("HostConfig", "Privileged"), True)),
            ("no-new-privileges off", _mutate(("HostConfig", "SecurityOpt"), [])),
            ("memory 4 GB", _mutate(("HostConfig", "Memory"), 4 * 1024**3)),
            ("swap not capped", _mutate(("HostConfig", "MemorySwap"), -1)),
            ("3 CPUs", _mutate(("HostConfig", "NanoCpus"), 3_000_000_000)),
            ("no process limit", _mutate(("HostConfig", "PidsLimit"), 0)),
            ("writable root", _mutate(("HostConfig", "ReadonlyRootfs"), False)),
            ("root user", _mutate(("Config", "User"), "0:0")),
            ("host pid", _mutate(("HostConfig", "PidMode"), "host")),
            ("host ipc", _mutate(("HostConfig", "IpcMode"), "host")),
            ("a device", _mutate(("HostConfig", "Devices"), [{"PathOnHost": "/dev/kvm"}])),
            ("published port", _mutate(("HostConfig", "PortBindings"), {"80/tcp": [{}]})),
            ("a second network", _mutate(("NetworkSettings", "Networks"), {_NETWORK: {}, "x": {}})),
            ("extra env name", _mutate(("Config", "Env"), ["ANTHROPIC_API_KEY=x"])),
            ("tmpfs grows", _mutate(("HostConfig", "Tmpfs"), {"/tmp": "rw,size=99999m"})),
            ("init off", _mutate(("HostConfig", "Init"), False)),
            (
                "home tmpfs loses its owner",
                _mutate(
                    ("HostConfig", "Tmpfs"),
                    {
                        "/tmp": "rw,nosuid,nodev,noexec,size=512m,mode=1777,uid=10001,gid=10001",
                        "/devagent-home": "rw,nosuid,nodev,exec,size=256m,mode=0700",
                    },
                ),
            ),
            ("no tmpfs at all", _mutate(("HostConfig", "Tmpfs"), {})),
            ("no network mode", _mutate(("HostConfig", "NetworkMode"), "bridge")),
        ],
    )
    def test_each_seeded_change_is_reported(
        self, label: str, change: Callable[[dict[str, object]], None]
    ) -> None:
        profile = load_profiles().agent
        inspect = copy.deepcopy(_inspect_for(profile))
        change(inspect)
        assert _violations(profile, inspect), label

    def test_the_docker_socket_is_reported(self) -> None:
        profile = load_profiles().agent
        inspect = copy.deepcopy(_inspect_for(profile))
        mounts = inspect["Mounts"]
        assert isinstance(mounts, list)
        mounts.append(
            {
                "Type": "bind",
                "Source": "/var/run/docker.sock",
                "Destination": "/var/run/docker.sock",
                "RW": True,
            }
        )
        found = " ".join(_violations(profile, inspect))
        assert "docker.sock" in found

    def test_a_host_bind_in_place_of_a_volume_is_reported(self) -> None:
        profile = load_profiles().agent
        inspect = copy.deepcopy(_inspect_for(profile))
        mounts = inspect["Mounts"]
        assert isinstance(mounts, list)
        mounts[0] = {"Type": "bind", "Source": "/opt", "Destination": "/workspace", "RW": True}
        assert any("bind" in v for v in _violations(profile, inspect))

    def test_a_writable_toolkit_is_reported(self) -> None:
        profile = load_profiles().agent
        inspect = copy.deepcopy(_inspect_for(profile))
        mounts = inspect["Mounts"]
        assert isinstance(mounts, list)
        lib = next(m for m in mounts if m["Destination"] == "/lib")
        lib["RW"] = True
        assert any("/lib" in v for v in _violations(profile, inspect))

    def test_a_read_only_workspace_is_reported(self) -> None:
        profile = load_profiles().agent
        inspect = copy.deepcopy(_inspect_for(profile))
        mounts = inspect["Mounts"]
        assert isinstance(mounts, list)
        workspace = next(m for m in mounts if m["Destination"] == "/workspace")
        workspace["RW"] = False
        assert any("/workspace" in v for v in _violations(profile, inspect))

    def test_a_workspace_from_another_volume_is_reported(self) -> None:
        profile = load_profiles().agent
        inspect = copy.deepcopy(_inspect_for(profile))
        mounts = inspect["Mounts"]
        assert isinstance(mounts, list)
        workspace = next(m for m in mounts if m["Destination"] == "/workspace")
        workspace["Name"] = "an-earlier-job"
        assert any("volume" in v for v in _violations(profile, inspect))

    def test_a_mount_that_the_spec_does_not_list_is_reported(self) -> None:
        profile = load_profiles().agent
        inspect = copy.deepcopy(_inspect_for(profile))
        mounts = inspect["Mounts"]
        assert isinstance(mounts, list)
        mounts.append(
            {"Type": "volume", "Name": "x", "Source": "/v", "Destination": "/cache", "RW": True}
        )
        assert any("/cache" in v for v in _violations(profile, inspect))

    def test_a_missing_mount_is_reported(self) -> None:
        profile = load_profiles().agent
        inspect = copy.deepcopy(_inspect_for(profile))
        inspect["Mounts"] = []
        assert any("/workspace" in v for v in _violations(profile, inspect))

    def test_a_malformed_inspect_object_is_a_violation_not_a_crash(self) -> None:
        assert _violations(load_profiles().agent, {})


_EXPECTED_CSP = {
    "default-src": frozenset({"'none'"}),
    "script-src": frozenset({"https://artifacts.example.com", "'unsafe-inline'"}),
    "webrtc": frozenset({"'block'"}),
}
_ORIGIN = "http://127.0.0.1:41234"
_HEADER = "default-src 'none'; script-src http://127.0.0.1:41234 'unsafe-inline'; webrtc 'block'"


def _csp(header: str) -> list[str]:
    return csp_header_violations(
        header,
        origin=_ORIGIN,
        expected=_EXPECTED_CSP,
        artifacts_origin="https://artifacts.example.com",
    )


class TestCspHeader:
    def test_header_with_the_origin_rebound_matches(self) -> None:
        assert _csp(_HEADER) == []

    def test_token_order_does_not_matter(self) -> None:
        assert (
            _csp(
                _HEADER.replace(
                    "http://127.0.0.1:41234 'unsafe-inline'",
                    "'unsafe-inline' http://127.0.0.1:41234",
                )
            )
            == []
        )

    @pytest.mark.parametrize(
        "header",
        [
            "default-src 'none'; script-src http://127.0.0.1:41234 'unsafe-inline'",
            _HEADER + "; connect-src *",
            _HEADER.replace("'none'", "'self'"),
            _HEADER.replace("http://127.0.0.1:41234", "https://artifacts.example.com"),
            _HEADER.replace("'unsafe-inline'", "'unsafe-inline' 'unsafe-eval'"),
            "",
        ],
    )
    def test_any_directive_difference_is_reported(self, header: str) -> None:
        assert _csp(header)


def _target(label: str, expect: str, result: str) -> dict[str, object]:
    stage = "proxy" if "proxy" in label else "tcp"
    return {
        "label": label,
        "host": "h",
        "port": 1,
        "via": "direct",
        "expect": expect,
        "attempted": True,
        "stage": stage,
        "result": result,
    }


def _good_report() -> dict[str, object]:
    targets = [_target(label, "deny", "failed") for label in sorted(REQUIRED_DENIED_LABELS)]
    targets += [_target(label, "allow", "connected") for label in sorted(REQUIRED_ALLOWED_LABELS)]
    return {"targets": targets, "env": ["PATH", "HOME"]}


class TestProbeReport:
    def test_a_good_report_has_no_violation(self) -> None:
        assert (
            probe_report_violations(_good_report(), allowed_env=frozenset({"PATH", "HOME"})) == []
        )

    def test_a_forbidden_target_that_answers_is_reported(self) -> None:
        report = _good_report()
        targets = report["targets"]
        assert isinstance(targets, list)
        targets[0]["result"] = "connected"
        found = probe_report_violations(report, allowed_env=frozenset({"PATH", "HOME"}))
        assert found and str(targets[0]["label"]) in found[0]

    def test_a_failed_positive_control_is_reported(self) -> None:
        report = _good_report()
        targets = report["targets"]
        assert isinstance(targets, list)
        for t in targets:
            if t["expect"] == "allow":
                t["result"] = "failed"
        assert probe_report_violations(report, allowed_env=frozenset({"PATH", "HOME"}))

    def test_a_probe_that_omits_a_required_target_is_reported(self) -> None:
        report = _good_report()
        targets = report["targets"]
        assert isinstance(targets, list)
        report["targets"] = targets[1:]
        assert probe_report_violations(report, allowed_env=frozenset({"PATH", "HOME"}))

    def test_an_empty_report_is_reported(self) -> None:
        assert probe_report_violations({}, allowed_env=frozenset())

    def test_an_undeclared_environment_name_is_reported(self) -> None:
        report = _good_report()
        report["env"] = ["PATH", "HOME", "AWS_SECRET_ACCESS_KEY"]
        found = probe_report_violations(report, allowed_env=frozenset({"PATH", "HOME"}))
        assert any("AWS_SECRET_ACCESS_KEY" in v for v in found)

    def test_a_probe_cannot_flip_the_polarity_of_a_required_target(self) -> None:
        report = _good_report()
        targets = report["targets"]
        assert isinstance(targets, list)
        postgres = next(t for t in targets if t["label"] == "postgres")
        postgres.update(expect="allow", result="connected")
        found = probe_report_violations(report, allowed_env=frozenset({"PATH", "HOME"}))
        assert any("postgres" in v and "answered" in v for v in found)

    def test_a_duplicate_entry_is_reported(self) -> None:
        report = _good_report()
        targets = report["targets"]
        assert isinstance(targets, list)
        targets.append(dict(targets[0]))
        found = probe_report_violations(report, allowed_env=frozenset({"PATH", "HOME"}))
        assert any("2 times" in v for v in found)

    def test_a_target_that_was_not_attempted_is_reported(self) -> None:
        report = _good_report()
        targets = report["targets"]
        assert isinstance(targets, list)
        targets[0]["attempted"] = False
        found = probe_report_violations(report, allowed_env=frozenset({"PATH", "HOME"}))
        assert any("not attempted" in v for v in found)

    @pytest.mark.parametrize("result", ["skipped", "unresolved", None, ""])
    def test_an_unknown_result_is_reported(self, result: object) -> None:
        report = _good_report()
        targets = report["targets"]
        assert isinstance(targets, list)
        targets[0]["result"] = result
        assert probe_report_violations(report, allowed_env=frozenset({"PATH", "HOME"}))

    def test_a_service_that_failed_only_at_name_resolution_is_not_proof(self) -> None:
        report = _good_report()
        targets = report["targets"]
        assert isinstance(targets, list)
        postgres = next(t for t in targets if t["label"] == "postgres")
        postgres["stage"] = "dns"
        found = probe_report_violations(report, allowed_env=frozenset({"PATH", "HOME"}))
        assert any("postgres" in v and "stage" in v for v in found)

    def test_an_extra_target_that_answers_is_reported(self) -> None:
        report = _good_report()
        targets = report["targets"]
        assert isinstance(targets, list)
        targets.append(_target("prod-ip:redis", "deny", "connected"))
        found = probe_report_violations(report, allowed_env=frozenset({"PATH", "HOME"}))
        assert any("prod-ip:redis" in v for v in found)
