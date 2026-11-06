#!/usr/bin/env python3
"""Live check of the dev-agent network, egress proxy and images (ADR-0159 AC-3; FRE-1682).

Run it on a host with Docker, after ``make dev-agent-build``::

    make dev-agent-verify ARGS="--compose-project fre1682t1 --up --down"

What it does, in order:

1. Starts the compose service ``dev-agent-proxy`` (with ``--up``), and checks that the
   ``dev-agent`` network is internal.
2. Starts canary listeners on a separate network, on the production service ports.
3. Runs the probe in an agent container that is built from ``config/dev_agent/container_profiles.yaml``
   and attached to ``dev-agent``. Every canary, the metadata address, GitHub, an IPv4 and an IPv6
   literal, the registry on port 80 and the registry address as a literal must fail. The registry
   through the proxy must work.
4. Compares ``docker inspect`` of that container with the spec.
5. Runs the same probe on the canary network. The canaries must answer there. A probe that never
   answers proves nothing.
6. Checks ``opencode --version`` against the pin, the CSP header of the test server, Chromium
   under the hardened flags (with a page that violates the CSP, as a seeded negative), and the
   handoff wrapper on a repo with hostile local config.

It never opens a connection to a production container unless ``--include-production-ips`` is set.
It prints one JSON report and exits 1 if any check fails.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import socket
import subprocess
import sys
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from personal_agent.config import settings
from personal_agent.dev_agent.container_spec import (
    ContainerProfile,
    ContainerProfiles,
    build_create_args,
    load_profiles,
)
from personal_agent.dev_agent.verification import (
    csp_header_violations,
    declared_env_names,
    inspect_violations,
    probe_report_violations,
)
from personal_agent.observability.artifact_envelope.spec import expected_csp_directives

_REPO = Path(__file__).resolve().parents[1]
_COMPOSE_FILE = _REPO / "docker-compose.cloud.yml"
_PROBE = "/opt/probe/probe.py"

#: Required variables of the compose file. The proxy uses none of them. Compose needs a value to
#: parse the other services, so these are placeholders and not credentials.
_COMPOSE_PLACEHOLDERS: Mapping[str, str] = {
    name: "unused-by-dev-agent-proxy"
    for name in (
        "CF_ACCESS_TEAM_DOMAIN",
        "GRAFANA_ADMIN_PASSWORD",
        "GRAFANA_CF_ACCESS_AUD",
        "GRAFANA_RO_PASSWORD",
        "NEO4J_PASSWORD",
        "POSTGRES_PASSWORD",
        "SESHAT_APP_PASSWORD",
    )
}

#: Canary listeners on the production ports. The label is the label that the checker requires.
_CANARY_PORTS: Mapping[str, int] = {
    "postgres": 5432,
    "neo4j": 7687,
    "elasticsearch": 9200,
    "redis": 6379,
    "caddy-8600": 8600,
    "caddy-8601": 8601,
    "gateway": 9001,
}
_CANARY_SCRIPT = (
    "import socket,threading\n"
    "def serve(p):\n"
    "    s=socket.socket(); s.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1)\n"
    "    s.bind(('0.0.0.0',p)); s.listen(16)\n"
    "    while True:\n"
    "        c,_=s.accept(); c.close()\n"
    "for p in {ports}:\n"
    "    threading.Thread(target=serve,args=(p,),daemon=True).start()\n"
    "threading.Event().wait()\n"
)
#: Production service names on the compose network, and their ports (used only with the flag).
_PRODUCTION_SERVICES: Mapping[str, tuple[int, ...]] = {
    "postgres": (5432,),
    "neo4j": (7687,),
    "elasticsearch": (9200,),
    "redis": (6379,),
    "caddy": (8600, 8601),
    "seshat-gateway": (9001,),
}
#: The production CSP has a `webrtc` directive. Chromium does not know it and logs this console
#: error on every page. The publish check of ADR-0159 D7.4 ("zero console errors") must ignore it.
_KNOWN_BENIGN_CONSOLE_ERROR = "Unrecognized Content-Security-Policy directive 'webrtc'"
_VERSION = re.compile(r"\d+\.\d+\.\d+")


@dataclass
class RunResult:
    """The outcome of one container run."""

    exit_code: int
    stdout: str
    stderr: str
    inspect: Mapping[str, object]


@dataclass
class Session:
    """Everything that this run created, so the cleanup can remove it."""

    suffix: str = field(default_factory=lambda: secrets.token_hex(4))
    containers: list[str] = field(default_factory=list)
    volumes: list[str] = field(default_factory=list)
    networks: list[str] = field(default_factory=list)

    def name(self, kind: str) -> str:
        """Return a unique resource name for this run."""
        return f"fre1682-verify-{self.suffix}-{kind}"


def docker(
    *args: str, env: Mapping[str, str] | None = None, timeout: float = 120.0, check: bool = True
) -> subprocess.CompletedProcess[str]:
    """Run a docker command as an argument list (no shell)."""
    full_env = {**os.environ, **(env or {})}
    return subprocess.run(  # noqa: S603
        ["docker", *args],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=check,
        env=full_env,
    )


def parse_version(text: str) -> str | None:
    """Return the first ``x.y.z`` in ``text``, or ``None``."""
    found = _VERSION.search(text)
    return found.group(0) if found else None


def canary_target_specs(canary_ip: str) -> list[str]:
    """Probe target specs that point at the canary listeners, under the required labels."""
    return [f"{label}|{canary_ip}|{port}|direct|deny" for label, port in _CANARY_PORTS.items()]


def _last_json_line(text: str) -> dict[str, object]:
    for line in reversed(text.strip().splitlines()):
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict):
            return data
    return {}


def _entries(report: Mapping[str, object], key: str) -> list[Mapping[str, object]]:
    """Return the dict items of a list field of a report. Anything else gives an empty list."""
    value = report.get(key)
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def run_container(
    session: Session,
    profile: ContainerProfile,
    *,
    kind: str,
    command: Sequence[str],
    network_name: str,
    volumes: Mapping[str, str],
    launcher_env: Mapping[str, str] | None = None,
    timeout: float = 180.0,
) -> RunResult:
    """Create a container from a profile, inspect it, run it to the end, and remove it.

    The container has no ``--rm``: the inspect must see it before the run, and the removal
    happens in ``finally``, so a failed run cannot leave it behind.
    """
    name = session.name(kind)
    args = build_create_args(
        profile,
        name=name,
        network_name=network_name,
        labels={"seshat.dev_agent.verify": session.suffix},
        volumes=volumes,
    )
    env = {n: "placeholder-not-a-credential" for n in profile.env_from_launcher}
    env.update(launcher_env or {})
    session.containers.append(name)
    try:
        docker(*[*args[1:], *command], env=env)
        inspect_raw = json.loads(docker("inspect", name).stdout)
        inspect = inspect_raw[0] if inspect_raw else {}
        try:
            done = docker("start", "--attach", name, timeout=timeout, check=False)
            return RunResult(done.returncode, done.stdout, done.stderr, inspect)
        except subprocess.TimeoutExpired:
            docker("kill", name, check=False)
            return RunResult(124, "", "timed out", inspect)
    finally:
        docker("rm", "-f", name, check=False)


def _volume(session: Session, kind: str) -> str:
    name = session.name(kind)
    docker("volume", "create", name)
    session.volumes.append(name)
    return name


def _volumes_for(session: Session, profile: ContainerProfile) -> dict[str, str]:
    return {
        m.target: _volume(session, "v" + m.target.strip("/").replace("/", "-"))
        for m in profile.mounts
    }


def _network_internal(name: str) -> bool:
    data = json.loads(docker("network", "inspect", name).stdout)
    return bool(data and data[0].get("Internal") is True)


def _compose(project: str, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603
        ["docker", "compose", "-p", project, "-f", str(_COMPOSE_FILE), *args],
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, **_COMPOSE_PLACEHOLDERS},
        timeout=600,
    )


def _wait_healthy(project: str, timeout: float = 90.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        ids = _compose(project, "ps", "-q", "dev-agent-proxy").stdout.split()
        if ids:
            status = docker("inspect", "--format", "{{.State.Health.Status}}", ids[0], check=False)
            if status.stdout.strip() == "healthy":
                return True
        time.sleep(2)
    return False


def _start_canary(session: Session, profiles: ContainerProfiles) -> tuple[str, str]:
    """Start the canary listeners on their own network. Returns (network name, canary IP)."""
    network = session.name("canary-net")
    docker("network", "create", network)
    session.networks.append(network)
    name = session.name("canary")
    script = _CANARY_SCRIPT.format(ports=list(_CANARY_PORTS.values()))
    docker("run", "-d", "--name", name, "--network", network, "--entrypoint", "python3",
           profiles.check.image, "-c", script)  # fmt: skip
    session.containers.append(name)
    time.sleep(1.5)
    fmt = '{{(index .NetworkSettings.Networks "' + network + '").IPAddress}}'
    return network, docker("inspect", "--format", fmt, name).stdout.strip()


def _production_ip_specs() -> list[str]:
    """Targets for the real containers on cloud-sim. Used only with --include-production-ips."""
    specs: list[str] = []
    data = json.loads(docker("network", "inspect", "seshat_cloud-sim", check=False).stdout or "[]")
    containers = data[0].get("Containers", {}) if data else {}
    for info in containers.values():
        service = str(info.get("Name", "")).removeprefix("cloud-sim-")
        ip = str(info.get("IPv4Address", "")).split("/")[0]
        for port in _PRODUCTION_SERVICES.get(service, ()):
            specs.append(f"prod-ip:{service}:{port}|{ip}|{port}|direct|deny")
    return specs


def check_network(
    session: Session,
    profiles: ContainerProfiles,
    *,
    dev_network: str,
    canary_ip: str,
    include_production: bool,
) -> dict[str, object]:
    """The acceptance probe: an agent container on dev-agent, then its inspect."""
    registry_ip = socket.gethostbyname("registry.npmjs.org")
    specs = canary_target_specs(canary_ip)
    specs.append(f"registry-ip-via-proxy|{registry_ip}|443|proxy|deny")
    if include_production:
        specs += _production_ip_specs()
    command = [_PROBE, "network"]
    for spec in specs:
        command += ["--target", spec]
    volumes = _volumes_for(session, profiles.agent)
    result = run_container(
        session, profiles.agent, kind="agent-probe", command=["python3", *command],
        network_name=dev_network, volumes=volumes,
    )  # fmt: skip
    report = _last_json_line(result.stdout)
    violations = probe_report_violations(report, allowed_env=declared_env_names(profiles.agent))
    violations += [
        f"registry-ip-via-proxy: {t.get('result')}"
        for t in _entries(report, "targets")
        if isinstance(t, dict)
        and t.get("label") == "registry-ip-via-proxy"
        and t.get("result") != "failed"
    ]
    inspect_found = inspect_violations(
        profiles.agent, result.inspect, network_name=dev_network, volumes=volumes
    )
    return {
        "report": report,
        "probe_exit_code": result.exit_code,
        "probe_violations": violations,
        "inspect_violations": inspect_found,
        "inspect_summary": _inspect_summary(result.inspect),
        "stderr": result.stderr[-500:],
    }


def _inspect_summary(inspect: Mapping[str, object]) -> dict[str, object]:
    host = inspect.get("HostConfig")
    config = inspect.get("Config")
    mounts = inspect.get("Mounts")
    if not (isinstance(host, dict) and isinstance(config, dict) and isinstance(mounts, list)):
        return {}
    keys = (
        "CapDrop",
        "SecurityOpt",
        "Memory",
        "MemorySwap",
        "NanoCpus",
        "PidsLimit",
        "ReadonlyRootfs",
        "Tmpfs",
        "NetworkMode",
        "Init",
    )
    return {
        **{k: host.get(k) for k in keys},
        "User": config.get("User"),
        "EnvNames": sorted(str(e).split("=")[0] for e in config.get("Env", [])),
        "Mounts": [
            (m.get("Type"), m.get("Destination"), m.get("RW"))
            for m in mounts
            if isinstance(m, dict)
        ],
    }


def check_control(
    session: Session, profiles: ContainerProfiles, *, canary_net: str, canary_ip: str
) -> dict[str, object]:
    """The instrument control: the same probe on a network that has the canaries and the internet."""
    command = ["python3", _PROBE, "network"]
    for spec in canary_target_specs(canary_ip):
        command += ["--target", spec]
    result = run_container(
        session, profiles.agent, kind="control-probe", command=command,
        network_name=canary_net, volumes=_volumes_for(session, profiles.agent),
    )  # fmt: skip
    report = _last_json_line(result.stdout)
    answered = {
        str(t.get("label"))
        for t in _entries(report, "targets")
        if isinstance(t, dict) and t.get("result") == "connected"
    }
    must_answer = set(_CANARY_PORTS) | {"github.com", "ip-literal"}
    missing = sorted(must_answer - answered)
    return {
        "answered": sorted(answered),
        "violations": [
            f"control: {label} did not answer, so the probe cannot see it" for label in missing
        ],
    }


def check_opencode(
    session: Session, profiles: ContainerProfiles, *, dev_network: str
) -> dict[str, object]:
    """``opencode --version`` in the hardened agent container must print the pin."""
    result = run_container(
        session, profiles.agent, kind="opencode", command=["opencode", "--version"],
        network_name=dev_network, volumes=_volumes_for(session, profiles.agent),
    )  # fmt: skip
    printed = parse_version(result.stdout)
    pin = profiles.opencode_version
    return {
        "printed": result.stdout.strip(),
        "pin": pin,
        "violations": [] if printed == pin else [f"opencode prints {printed!r}, pin is {pin!r}"],
    }


def check_csp(session: Session, profiles: ContainerProfiles) -> dict[str, object]:
    """The CSP test server of the check image sends the expected policy for its own origin."""
    result = run_container(
        session, profiles.check, kind="csp", command=["python3", _PROBE, "csp"],
        network_name="none", volumes=_volumes_for(session, profiles.check),
    )  # fmt: skip
    report = _last_json_line(result.stdout)
    # The image holds the manifest of the placeholder origins (see the README), so compare with those.
    settings.artifacts_public_base_url = None
    settings.pwa_public_origin = "https://agent.example.com"
    found = csp_header_violations(
        str(report.get("header", "")),
        origin=str(report.get("origin", "")),
        expected=expected_csp_directives(),
        artifacts_origin="https://artifacts.example.com",
    )
    if not report.get("origin"):
        found.append("the CSP server printed no origin")
    return {"origin": report.get("origin"), "header": report.get("header"), "violations": found}


def check_browser(session: Session, profiles: ContainerProfiles) -> dict[str, object]:
    """Chromium runs under the hardened flags, and sees a CSP violation when there is one."""
    result = run_container(
        session, profiles.check, kind="browser", command=["python3", _PROBE, "browser"],
        network_name="none", volumes=_volumes_for(session, profiles.check),
    )  # fmt: skip
    report = _last_json_line(result.stdout)
    pages = {str(p.get("page")): p for p in _entries(report, "pages")}
    found: list[str] = []
    good, bad = pages.get("good.html", {}), pages.get("bad.html", {})
    raw_errors = good.get("errors")
    good_errors = [
        e
        for e in (raw_errors if isinstance(raw_errors, list) else [raw_errors])
        if e is not None and _KNOWN_BENIGN_CONSOLE_ERROR not in str(e)
    ]
    if good.get("text") != "clicked" or good_errors or not good:
        found.append(f"good page: {good or result.stderr[-300:]}")
    bad_errors = bad.get("errors")
    csp_errors = [
        e
        for e in (bad_errors if isinstance(bad_errors, list) else [])
        if "Content Security Policy" in str(e)
    ]
    if bad.get("text") != "clicked" or not csp_errors:
        found.append("seeded negative: the page that violates the CSP produced no console error")
    return {"pages": report.get("pages"), "violations": found}


_HOSTILE_SETUP = (
    "cd /workspace && git init -q -b job/test . && git config user.email a@b.c && git config user.name a "
    "&& echo hi > f && git add f && git commit -qm c "
    "&& git config core.fsmonitor 'touch /out/PWNED #'"
)


_WRAPPER_SCRIPT = _REPO / "docker" / "dev-agent" / "handoff-bundle.sh"
_WRAPPER_FLAG = re.compile(r"^\s*-c\s+(\S+?)\s*\\?$", re.MULTILINE)


def wrapper_git_flags(script_text: str) -> list[str]:
    """Return the ``-c key=value`` overrides of ``handoff-bundle.sh``, as plain ``key=value``."""
    return [m.group(1).replace("'", "") for m in _WRAPPER_FLAG.finditer(script_text)]


def _marker_listing(image: str, volume: str) -> str:
    return docker(
        "run", "--rm", "-v", f"{volume}:/out:ro", "--entrypoint", "ls", image, "/out"
    ).stdout


def check_handoff(session: Session, profiles: ContainerProfiles) -> dict[str, object]:
    """The wrapper bundles a branch of a workspace that has hostile local config.

    ``git bundle create`` does not read the index, so ``core.fsmonitor`` does not fire on it,
    with or without the wrapper. A check on the bundle command alone could not fail. So the
    check has three parts:

    * control: plain ``git status`` in the hostile repo runs the config command (the marker appears);
    * the wrapper's own ``-c`` overrides, applied to that same ``git status``, stop it;
    * the wrapper in the hardened handoff container writes a valid bundle and no marker.
    """
    image = profiles.handoff.image
    workspace = _volume(session, "handoff-ws")
    control_out = _volume(session, "handoff-out-control")
    flags_out = _volume(session, "handoff-out-flags")
    out = _volume(session, "handoff-out")
    docker("run", "--rm", "-v", f"{workspace}:/workspace", "-v", f"{control_out}:/out", "--entrypoint", "sh",
           image, "-c", _HOSTILE_SETUP)  # fmt: skip

    def git_status(out_volume: str, extra: Sequence[str]) -> None:
        docker("run", "--rm", "--network", "none", "-v", f"{workspace}:/workspace:ro", "-v", f"{out_volume}:/out",
               "--entrypoint", "git", image, "-C", "/workspace", *extra, "status", check=False)  # fmt: skip

    git_status(control_out, ["-c", "safe.directory=*"])
    flags = wrapper_git_flags(_WRAPPER_SCRIPT.read_text(encoding="utf-8"))
    flag_args = [arg for flag in flags for arg in ("-c", flag)]
    git_status(flags_out, flag_args)
    control_marker = _marker_listing(image, control_out)
    flags_marker = _marker_listing(image, flags_out)
    result = run_container(
        session, profiles.handoff, kind="handoff", command=["job/test"], network_name="none",
        volumes={"/workspace": workspace, "/out": out},
    )  # fmt: skip
    listing = _marker_listing(image, out)
    found: list[str] = []
    if "PWNED" not in control_marker:
        found.append(
            "control: plain git status did not run the hostile config, so the check proves nothing"
        )
    if "core.fsmonitor=false" not in flags:
        found.append("the wrapper no longer sets core.fsmonitor=false")
    if "PWNED" in flags_marker:
        found.append("the wrapper's -c overrides did not stop the hostile config")
    if result.exit_code != 0:
        found.append(f"handoff exited {result.exit_code}: {result.stderr[-300:]}")
    if "job.bundle" not in listing:
        found.append("no bundle was written")
    if "PWNED" in listing:
        found.append("the hostile repo config ran inside the handoff wrapper")
    return {
        "wrapper_flags": flags,
        "control_marker_seen": "PWNED" in control_marker,
        "marker_with_wrapper_flags": "PWNED" in flags_marker,
        "output_files": listing.split(),
        "violations": found,
    }


def cleanup(session: Session) -> None:
    """Remove every container, volume and network that this run created."""
    for name in session.containers:
        docker("rm", "-f", name, check=False)
    for name in session.volumes:
        docker("volume", "rm", "-f", name, check=False)
    for name in session.networks:
        docker("network", "rm", name, check=False)


def main(argv: Sequence[str] | None = None) -> int:
    """Run every check and print the JSON report. Returns 0 only if all pass."""
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--compose-project", default="seshat", help="compose project of the dev-agent network"
    )
    parser.add_argument(
        "--up", action="store_true", help="start dev-agent-proxy first (no dependencies)"
    )
    parser.add_argument("--down", action="store_true", help="stop what --up started")
    parser.add_argument("--include-production-ips", action="store_true",
                        help="also try the real containers on seshat_cloud-sim (a TCP connect each)")  # fmt: skip
    parser.add_argument("--output", type=Path, help="also write the JSON report to this file")
    args = parser.parse_args(argv)
    if (args.up or args.down) and args.compose_project == "seshat":
        # `down` would stop the production stack, and `up` is a deploy. Both belong to master.
        parser.error("--up and --down need a throwaway --compose-project, not the production one")

    profiles = load_profiles()
    dev_network = f"{args.compose_project}_dev-agent"
    session = Session()
    sections: dict[str, dict[str, object]] = {}
    try:
        if args.up:
            up = _compose(args.compose_project, "up", "-d", "--no-deps", "dev-agent-proxy")
            if up.returncode != 0 or not _wait_healthy(args.compose_project):
                print(
                    json.dumps(
                        {
                            "ok": False,
                            "error": "dev-agent-proxy did not become healthy",
                            "detail": up.stderr[-800:],
                        }
                    )
                )
                return 1
        internal = _network_internal(dev_network)
        canary_net, canary_ip = _start_canary(session, profiles)
        sections["network"] = check_network(
            session, profiles, dev_network=dev_network, canary_ip=canary_ip,
            include_production=args.include_production_ips,
        )  # fmt: skip
        sections["control"] = check_control(
            session, profiles, canary_net=canary_net, canary_ip=canary_ip
        )
        sections["opencode"] = check_opencode(session, profiles, dev_network=dev_network)
        sections["csp"] = check_csp(session, profiles)
        sections["browser"] = check_browser(session, profiles)
        sections["handoff"] = check_handoff(session, profiles)
        violations: list[str] = [] if internal else [f"network {dev_network} is not internal"]
        for name, section in sections.items():
            for key in ("violations", "probe_violations", "inspect_violations"):
                items = section.get(key)
                if isinstance(items, list):
                    violations += [f"{name}: {item}" for item in items]
        report_text = json.dumps(
            {
                "ok": not violations,
                "network_internal": internal,
                "violations": violations,
                "sections": sections,
            },
            indent=2,
        )
        if args.output:
            args.output.write_text(report_text + "\n", encoding="utf-8")
        print(report_text)
        return 0 if not violations else 1
    finally:
        cleanup(session)
        if args.down:
            _compose(args.compose_project, "down")


if __name__ == "__main__":
    sys.exit(main())
