#!/usr/bin/env python3
"""Live check of the dev-agent broker (ADR-0159 D4.3, D4.4, AC-3; FRE-1683 AC-5).

Run it on the VPS, from the repo root, after the broker starts::

    make dev-broker-verify

What it checks, in order:

1. ``docker inspect`` of the broker: read-only root, ``cap_drop ALL``, ``no-new-privileges``, a
   non-root user, no mount (so no Docker socket), no published port, exactly two networks, and an
   environment whose variable names are the declared allowlist. It prints names, never values.
2. A full TCP port scan of the broker from a throwaway container on ``dev-agent``: only the model
   port answers. The same scan from ``cloud-sim``: only the control port answers. Each network's
   scan also covers the broker's address on the other network, which must not answer at all.
3. The model route without a job token answers 401. The control API without the secret answers
   401. With the secret, a relay to a job that is not current answers 404.
4. The broker's database role is refused on ``dev_jobs`` (once it exists), ``artifacts`` and
   ``users``.

It writes nothing to any table. It prints one JSON report and exits 1 if any check fails.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import uuid
from collections.abc import Mapping, Sequence

from personal_agent.dev_broker.config import load_broker_config
from personal_agent.dev_broker.relay import SECRET_HEADER

SERVICE = "seshat-dev-broker"
POSTGRES = "cloud-sim-postgres"

#: The variable names that the broker's environment may hold. Docker and the image add the rest
#: of :data:`_IMAGE_ENV`.
EXPECTED_ENV = frozenset(
    {
        "APP_ENV",
        "AGENT_DATABASE_URL",
        "AGENT_SCALEWAY_SECRET_KEY",
        "AGENT_DEV_BROKER_CONTROL_SECRET",
        "AGENT_URL_GUARD_MODE",
        "AGENT_URL_GUARD_ALLOWLIST",
        "AGENT_OTEL_EXPORTER_ENDPOINT",
    }
)
#: Variables that Dockerfile.gateway and its base image set (read from the image, 2026-11-07).
#: They hold no credential.
_IMAGE_ENV = frozenset(
    {
        "PATH",
        "LANG",
        "PYTHON_VERSION",
        "PYTHON_SHA256",
        "AGENT_BUILD_FINGERPRINT",
        "PUPPETEER_EXECUTABLE_PATH",
        "PUPPETEER_SKIP_CHROMIUM_DOWNLOAD",
    }
)

#: Runs in the scan container. Prints the open ports of each target as JSON.
_SCAN = r"""
import asyncio, json, sys
async def one(host, port, sem):
    async with sem:
        try:
            _, w = await asyncio.wait_for(asyncio.open_connection(host, port), 0.5)
        except Exception:
            return None
        w.close()
        return port
async def main(hosts):
    sem = asyncio.Semaphore(800)
    out = {}
    for host in hosts:
        found = await asyncio.gather(*(one(host, p, sem) for p in range(1, 65536)))
        out[host] = sorted(p for p in found if p)
    print(json.dumps(out))
asyncio.run(main(sys.argv[1:]))
"""

#: Runs in the scan container. Prints the HTTP status of each request as JSON.
_HTTP = r"""
import json, os, sys, urllib.request, urllib.error
out = []
for spec in json.loads(sys.argv[1]):
    req = urllib.request.Request(spec["url"], method=spec["method"], data=spec.get("body", "").encode() or None)
    for name, env in spec.get("headers_from_env", {}).items():
        req.add_header(name, os.environ[env])
    for name, value in spec.get("headers", {}).items():
        req.add_header(name, value)
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            out.append(r.status)
    except urllib.error.HTTPError as e:
        out.append(e.code)
    except Exception as e:
        out.append(type(e).__name__)
print(json.dumps(out))
"""


def docker(*args: str, env: Mapping[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    """Run ``docker`` with an argument list (never a shell)."""
    return subprocess.run(  # noqa: S603 — fixed executable, argument list
        ["docker", *args],  # noqa: S607
        capture_output=True,
        text=True,
        check=False,
        env=None if env is None else {**os.environ, **env},
    )


def _obj(value: object) -> Mapping[str, object]:
    """Return ``value`` as a JSON object, or an empty one. A missing shape fails the checks."""
    return value if isinstance(value, Mapping) else {}


def _items(value: object) -> list[object]:
    """Return ``value`` as a JSON array, or an empty one."""
    return list(value) if isinstance(value, list) else []


def inspect_violations(inspect: Mapping[str, object]) -> list[str]:
    """Return every way the broker's ``docker inspect`` differs from its declared boundary.

    Args:
        inspect: One element of ``docker inspect`` output.

    Returns:
        Human-readable violations; empty when the container matches.
    """
    host = _obj(inspect.get("HostConfig"))
    config = _obj(inspect.get("Config"))
    problems: list[str] = []
    if host.get("ReadonlyRootfs") is not True:
        problems.append("root filesystem is writable")
    if "ALL" not in _items(host.get("CapDrop")):
        problems.append("capabilities are not all dropped")
    if not any("no-new-privileges" in str(opt) for opt in _items(host.get("SecurityOpt"))):
        problems.append("no-new-privileges is not set")
    if host.get("Privileged"):
        problems.append("the container is privileged")
    user = str(config.get("User", ""))
    if user in ("", "0", "root") or user.startswith(("0:", "root:")):
        problems.append(f"runs as root (user {user!r})")
    mounts = _items(inspect.get("Mounts"))
    if mounts:
        problems.append(f"has mounts: {[_obj(m).get('Destination') for m in mounts]}")
    if host.get("PortBindings"):
        problems.append("publishes ports")
    networks = set(_obj(_obj(inspect.get("NetworkSettings")).get("Networks")))
    if len(networks) != 2:
        problems.append(f"is on {len(networks)} networks, not 2: {sorted(networks)}")
    names = {str(entry).split("=", 1)[0] for entry in _items(config.get("Env"))}
    extra = sorted(names - EXPECTED_ENV - _IMAGE_ENV)
    if extra:
        problems.append(f"environment holds undeclared variables: {extra}")
    missing = sorted(EXPECTED_ENV - names)
    if missing:
        problems.append(f"environment lacks: {missing}")
    return problems


def scan_violations(
    open_ports: Mapping[str, Sequence[int]], *, own: str, other: str, expected_port: int
) -> list[str]:
    """Return how a scan from one network differs from "only the listener of that network".

    Args:
        open_ports: Open ports per scanned address.
        own: The broker's address on the scanning network.
        other: The broker's address on the other network.
        expected_port: The one port that must answer on ``own``.

    Returns:
        Violations; empty when only ``expected_port`` on ``own`` answers.
    """
    problems: list[str] = []
    if list(open_ports.get(own, [])) != [expected_port]:
        problems.append(f"{own}: open {list(open_ports.get(own, []))}, expected [{expected_port}]")
    if open_ports.get(other):
        problems.append(f"{other}: reachable on {list(open_ports[other])}, expected nothing")
    return problems


def _broker_inspect() -> Mapping[str, object]:
    ids = docker(
        "ps", "-q", "--filter", f"label=com.docker.compose.service={SERVICE}"
    ).stdout.split()
    if len(ids) != 1:
        raise SystemExit(f"expected one running {SERVICE} container, found {len(ids)}")
    data = _items(json.loads(docker("inspect", ids[0]).stdout))
    if len(data) != 1:
        raise SystemExit(f"docker inspect returned {len(data)} objects")
    return _obj(data[0])


def _addresses(inspect: Mapping[str, object]) -> dict[str, str]:
    """Map 'cloud-sim' and 'dev-agent' to the real network name and the broker's address."""
    found: dict[str, str] = {}
    for name, net in _obj(_obj(inspect.get("NetworkSettings")).get("Networks")).items():
        for key in ("cloud-sim", "dev-agent"):
            if name.endswith(f"_{key}") or name == key:
                found[key] = name
                found[f"{key}-ip"] = str(_obj(net).get("IPAddress", ""))
    if set(found) != {"cloud-sim", "cloud-sim-ip", "dev-agent", "dev-agent-ip"}:
        raise SystemExit(f"the broker is not on both networks: {sorted(found)}")
    return found


def _in_scan_container(
    image: str, network: str, code: str, *args: str, env: Mapping[str, str] | None = None
) -> str:
    env_args = [arg for name in (env or {}) for arg in ("-e", name)]
    result = docker(
        "run", "--rm", "--network", network, "--user", "65534:65534", "--read-only",
        "--cap-drop", "ALL", "--security-opt", "no-new-privileges", "--memory", "256m",
        *env_args, "--entrypoint", "/app/.venv/bin/python", image, "-c", code, *args,
        env=env,
    )  # fmt: skip
    if result.returncode != 0:
        raise SystemExit(f"scan container failed on {network}: {result.stderr.strip()[-400:]}")
    return result.stdout.strip().splitlines()[-1]


def _role_refusals() -> dict[str, str]:
    out: dict[str, str] = {}
    exists = docker(
        "exec", POSTGRES, "psql", "-U", "agent", "-d", "personal_agent", "-tAc",
        "SELECT to_regclass('public.dev_jobs') IS NOT NULL",
    ).stdout.strip()  # fmt: skip
    tables = ["artifacts", "users"] + (["dev_jobs"] if exists == "t" else [])
    for table in tables:
        result = docker(
            "exec", POSTGRES, "psql", "-U", "seshat_dev_broker", "-d", "personal_agent", "-tAc",
            f"SELECT 1 FROM {table} LIMIT 1",
        )  # fmt: skip
        out[table] = "refused" if "permission denied" in result.stderr else "ALLOWED"
    if "dev_jobs" not in out:
        out["dev_jobs"] = "absent (FRE-1684); no default privileges"
    return out


def main(argv: Sequence[str]) -> int:
    """Run every check and print the report.

    Args:
        argv: Command-line arguments.

    Returns:
        0 when every check passes, else 1.
    """
    argparse.ArgumentParser(description=__doc__.splitlines()[0]).parse_args(argv)
    from personal_agent.config import settings  # noqa: PLC0415

    config = load_broker_config()
    inspect = _broker_inspect()
    image = str(_obj(inspect.get("Config")).get("Image", ""))
    nets = _addresses(inspect)
    report: dict[str, object] = {"inspect": inspect_violations(inspect)}

    dev_scan = json.loads(
        _in_scan_container(
            image, nets["dev-agent"], _SCAN, nets["dev-agent-ip"], nets["cloud-sim-ip"]
        )
    )
    cloud_scan = json.loads(
        _in_scan_container(
            image, nets["cloud-sim"], _SCAN, nets["cloud-sim-ip"], nets["dev-agent-ip"]
        )
    )
    report["scan_dev_agent"] = scan_violations(
        dev_scan,
        own=nets["dev-agent-ip"],
        other=nets["cloud-sim-ip"],
        expected_port=config.model_port,
    )
    report["scan_cloud_sim"] = scan_violations(
        cloud_scan,
        own=nets["cloud-sim-ip"],
        other=nets["dev-agent-ip"],
        expected_port=config.control_port,
    )

    model_url = f"http://{nets['dev-agent-ip']}:{config.model_port}/v1/chat/completions"
    model_status = json.loads(
        _in_scan_container(
            image, nets["dev-agent"], _HTTP,
            json.dumps([{"url": model_url, "method": "POST", "body": "{}"}]),
        )
    )  # fmt: skip
    control = f"http://{nets['cloud-sim-ip']}:{config.control_port}/relay/{uuid.uuid4()}/session"
    secret = settings.dev_broker_control_secret or ""
    control_status = json.loads(
        _in_scan_container(
            image, nets["cloud-sim"], _HTTP,
            json.dumps([
                {"url": control, "method": "GET"},
                {"url": control, "method": "GET", "headers_from_env": {SECRET_HEADER: "BROKER_SECRET"}},
            ]),
            env={"BROKER_SECRET": secret},
        )
    )  # fmt: skip
    report["http"] = [
        problem
        for problem, bad in (
            (
                f"model route without a token answered {model_status[0]}, expected 401",
                model_status != [401],
            ),
            (
                f"relay without the secret answered {control_status[0]}, expected 401",
                control_status[0] != 401,
            ),
            (
                f"relay to a job that is not current answered {control_status[1]}, expected 404",
                control_status[1] != 404,
            ),
            ("AGENT_DEV_BROKER_CONTROL_SECRET is not set on this host", not secret),
        )
        if bad
    ]
    roles = _role_refusals()
    report["role"] = [f"{t}: {v}" for t, v in roles.items() if v == "ALLOWED"]
    report["role_detail"] = roles

    failed = any(report[k] for k in ("inspect", "scan_dev_agent", "scan_cloud_sim", "http", "role"))
    report["result"] = "FAIL" if failed else "PASS"
    print(json.dumps(report, indent=2))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
