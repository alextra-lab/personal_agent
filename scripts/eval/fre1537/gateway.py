"""FRE-1511 / ADR-0154 D7: the capture gateway and its recording stub, as standalone containers.

Never ``docker compose``. On 2026-10-03 the eval compose files, run with ``-p seshat`` from a worktree,
recreated production's ``cloud-sim-searxng`` on the worktree's bind mount (ADR-0154 Implementation Notes).
This launcher uses ``docker run`` only, with its own container names, its own image tag and a label on
everything it creates. It removes only what carries the label.

    uv run python -m scripts.eval.fre1537.gateway up     --run-dir <run>
    uv run python -m scripts.eval.fre1537.gateway render --run-dir <run>
    uv run python -m scripts.eval.fre1537.gateway down

The eval substrates (``make eval-infra-up``) must already run. ``up`` needs ``POSTGRES_PASSWORD``,
``NEO4J_PASSWORD`` and ``AGENT_OWNER_EMAIL`` in the environment. Secrets reach the container by name, so
they never appear in a process listing.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from collections.abc import Mapping, Sequence
from pathlib import Path

import httpx
from scripts.eval.fre1537.common import RunPaths
from scripts.eval.fre1537.fixtures import REPO_ROOT, load_fixtures
from scripts.eval.fre1537.render import PROMPTS_PREFIX
from scripts.eval.gateway_freshness import compute_build_fingerprint

LABEL = "fre1537.probe"
STUB_NAME = "fre1537-stub"
GATEWAY_NAME = "fre1537-gateway"
IMAGE_REPO = "fre1537-probe-gateway"
STUB_URL = f"http://{STUB_NAME}:8700"
EVAL_SUBSTRATES = (
    "cloud-sim-postgres-eval",
    "cloud-sim-neo4j-eval",
    "cloud-sim-elasticsearch-eval",
    "cloud-sim-redis-eval",
)
# A production container that the 2026-10-03 incident recreated. `up` proves that it did not change.
PRODUCTION_GUARD = "cloud-sim-searxng"
DEFAULT_PORT = 9012
# The eval deployment profile refuses to boot without these two secrets. The model is a stub, so the
# probe passes an inert value and no cloud call can spend money.
PLACEHOLDER_KEY = "fre1537-probe-unused"
PROBE_DIR = Path(__file__).resolve().parent
CONTAINER_PYTHON = "/app/.venv/bin/python"

FMT_LABEL = '{{index .Config.Labels "' + LABEL + '"}}'
FMT_NETWORKS = "{{json .NetworkSettings.Networks}}"
FMT_STATE = "{{.State.Status}} {{if .State.Health}}{{.State.Health.Status}}{{end}}"
FMT_IDENTITY = "{{.Id}} {{.State.StartedAt}}"

# Passed by name, never by value.
SECRET_KEYS = frozenset(
    {"AGENT_DATABASE_URL", "AGENT_DATABASE_ADMIN_URL", "AGENT_NEO4J_PASSWORD", "AGENT_OWNER_EMAIL"}
)
REQUIRED_HOST_ENV = ("POSTGRES_PASSWORD", "NEO4J_PASSWORD", "AGENT_OWNER_EMAIL")
# Settings that shape the primary's prompt. They pass through from the caller's environment when set.
PASSTHROUGH_KEYS = (
    "AGENT_SKILL_ROUTING_MODE",
    "AGENT_OWNER_NAME",
    "AGENT_CONVERSATION_MAX_HISTORY_MESSAGES",
    "AGENT_LOCATION_ENABLED",
)
_URL_HOST = re.compile(r"://(?:[^/@\s]*@)?([A-Za-z0-9._-]+)")


def build_fingerprint() -> str:
    """Return the content fingerprint of the files that ``Dockerfile.gateway`` copies."""
    return compute_build_fingerprint(REPO_ROOT)


def run_docker(
    args: Sequence[str],
    env: Mapping[str, str] | None = None,
    cwd: Path | None = None,
    check: bool = True,
    stdin: str | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run one docker CLI command. The only place that starts a process.

    Args:
        args: Arguments after ``docker``.
        env: Extra environment, for secrets that ``-e NAME`` hands to the container.
        cwd: Working directory.
        check: Raise on a non-zero exit.
        stdin: Text for standard input.

    Returns:
        The completed process.

    Raises:
        subprocess.CalledProcessError: On a non-zero exit when ``check`` is true.
    """
    return subprocess.run(
        ["docker", *args],
        env={**os.environ, **(env or {})},
        cwd=cwd,
        check=check,
        capture_output=True,
        text=True,
        input=stdin,
    )


def gateway_env(host_env: Mapping[str, str]) -> tuple[dict[str, str], frozenset[str]]:
    """Build the capture gateway's environment.

    Every store is an eval store. The model endpoint is the stub. Primitive tools and prefer-primitives are
    on, as in production. The event bus is off, so a capture turn writes to no knowledge graph.

    Args:
        host_env: The caller's environment.

    Returns:
        The environment and the names of its secret entries.

    Raises:
        SystemExit: If a required credential is missing.
    """
    missing = [k for k in REQUIRED_HOST_ENV if not host_env.get(k)]
    if missing:
        raise SystemExit(f"set {', '.join(missing)} in the environment before `up`")
    app_password = host_env.get("SESHAT_APP_EVAL_PASSWORD") or "seshat_app_dev_password"
    db = "postgres-eval:5432/personal_agent"
    env = {
        "AGENT_DATABASE_URL": f"postgresql+asyncpg://seshat_app:{app_password}@{db}",
        "AGENT_DATABASE_ADMIN_URL": f"postgresql+asyncpg://agent:{host_env['POSTGRES_PASSWORD']}@{db}",
        "AGENT_NEO4J_URI": "bolt://neo4j-eval:7687",
        "AGENT_NEO4J_USER": "neo4j",
        "AGENT_NEO4J_PASSWORD": host_env["NEO4J_PASSWORD"],
        "AGENT_ELASTICSEARCH_URL": "http://elasticsearch-eval:9200",
        "AGENT_EVENT_BUS_REDIS_URL": "redis://redis-eval:6379/0",
        "AGENT_OWNER_STORAGE_ALLOWLIST": '["postgres-eval","neo4j-eval","elasticsearch-eval"]',
        "AGENT_SLM_BASE_URL": STUB_URL,
        "AGENT_DEPLOYMENT_PROFILE": "eval",
        "APP_ENV": "eval",
        "API_HOST": "0.0.0.0",  # noqa: S104 - published on loopback only
        "API_PORT": "9001",
        "AGENT_LOG_LEVEL": "info",
        "AGENT_OWNER_EMAIL": host_env["AGENT_OWNER_EMAIL"],
        "AGENT_ANTHROPIC_API_KEY": PLACEHOLDER_KEY,
        "AGENT_OPENAI_API_KEY": PLACEHOLDER_KEY,
        "AGENT_GATEWAY_AUTH_ENABLED": "false",
        "AGENT_MCP_GATEWAY_ENABLED": "false",
        "AGENT_PRIMITIVE_TOOLS_ENABLED": "true",
        "AGENT_PREFER_PRIMITIVES": "true",
        "AGENT_DELEGATION_ENABLED": "false",
        "AGENT_EXPANSION_ENABLED": "false",
    }
    env.update({k: host_env[k] for k in PASSTHROUGH_KEYS if host_env.get(k)})
    return env, SECRET_KEYS & env.keys()


def assert_eval_only(env: Mapping[str, str]) -> None:
    """Refuse an environment that names a production host.

    Args:
        env: The gateway environment.

    Raises:
        SystemExit: If any URL in it points at a host that is neither an ``-eval`` store nor the stub.
    """
    for key, value in env.items():
        for host in _URL_HOST.findall(value):
            if not (host.endswith("-eval") or host == STUB_NAME):
                raise SystemExit(f"{key} names production host {host!r}")


def env_flags(env: Mapping[str, str], secrets: frozenset[str]) -> tuple[list[str], dict[str, str]]:
    """Turn an environment into ``-e`` flags.

    Args:
        env: The gateway environment.
        secrets: Names whose values must not appear in the command line.

    Returns:
        The flags, and the environment that the docker CLI needs to resolve the secret names.
    """
    flags: list[str] = []
    process_env: dict[str, str] = {}
    for key in sorted(env):
        if key in secrets:
            flags += ["-e", key]
            process_env[key] = env[key]
        else:
            flags += ["-e", f"{key}={env[key]}"]
    return flags, process_env


def _inspect(name: str, fmt: str) -> str | None:
    result = run_docker(["inspect", "--format", fmt, name], check=False)
    return result.stdout.strip() if result.returncode == 0 else None


def _check_substrates() -> str:
    """Check the eval substrates and return the one network that they all share."""
    networks: list[set[str]] = []
    for name in EVAL_SUBSTRATES:
        state = _inspect(name, FMT_STATE)
        if state is None:
            raise SystemExit(
                f"{name} is not running: start the eval substrates first (make eval-infra-up)"
            )
        status, _, health = state.partition(" ")
        if status != "running" or health != "healthy":
            raise SystemExit(f"{name} is not running and healthy ({state!r})")
        raw = _inspect(name, FMT_NETWORKS)
        networks.append(set(json.loads(raw or "{}")))
    shared = set.intersection(*networks)
    if len(shared) != 1:
        raise SystemExit(
            f"the eval substrates must share exactly one network, found {sorted(shared) or 'none'}"
        )
    return next(iter(shared))


def _check_names_are_ours() -> None:
    for name in (STUB_NAME, GATEWAY_NAME):
        label = _inspect(name, FMT_LABEL)
        if label is not None and label != "1":
            raise SystemExit(
                f"container {name!r} exists and was not created by this probe: not touching it"
            )


def _remove_own_containers() -> None:
    for name in (GATEWAY_NAME, STUB_NAME):
        if _inspect(name, FMT_LABEL) == "1":
            run_docker(["rm", "-f", name])


def _ensure_image() -> str:
    """Build the gateway image under the probe's own tag, unless that tag exists."""
    fingerprint = build_fingerprint()
    tag = f"{IMAGE_REPO}:{fingerprint[:12]}"
    if run_docker(["image", "inspect", tag], check=False).returncode != 0:
        run_docker(
            [
                "build",
                "-f",
                "Dockerfile.gateway",
                "--build-arg",
                f"BUILD_FINGERPRINT={fingerprint}",
                "--label",
                f"{LABEL}=1",
                "-t",
                tag,
                ".",
            ],
            cwd=REPO_ROOT,
        )
    return tag


def wait_healthy(port: int, fingerprint: str, timeout: float = 180.0) -> None:
    """Wait for the gateway's ``/health`` and check that it runs the image that was just built.

    Args:
        port: The published host port.
        fingerprint: The expected build fingerprint.
        timeout: Seconds to wait.

    Raises:
        SystemExit: On timeout, or on a different build fingerprint.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            health = httpx.get(f"http://127.0.0.1:{port}/health", timeout=5.0).json()
        except (httpx.HTTPError, ValueError):
            time.sleep(3)
            continue
        if health.get("build_fingerprint") not in (None, fingerprint):
            raise SystemExit(
                f"the gateway reports build {health.get('build_fingerprint')!r}, not {fingerprint!r}"
            )
        return
    logs = run_docker(["logs", "--tail", "40", GATEWAY_NAME], check=False)
    raise SystemExit(
        f"the gateway was not healthy after {timeout:.0f} s:\n{logs.stdout}{logs.stderr}"
    )


def up(run_dir: Path, host_env: Mapping[str, str], port: int = DEFAULT_PORT) -> None:
    """Start the recording stub and the capture gateway.

    Args:
        run_dir: The run directory. The stub writes its calls to ``<run_dir>/stub-out``.
        host_env: The caller's environment.
        port: Host port for the gateway, bound to loopback.

    Raises:
        SystemExit: If a precondition fails. Nothing is started before every precondition holds.
    """
    env, secrets = gateway_env(host_env)
    assert_eval_only(env)
    network = _check_substrates()
    _check_names_are_ours()
    guard_before = _inspect(PRODUCTION_GUARD, FMT_IDENTITY)
    image = _ensure_image()
    out = RunPaths(run_dir.resolve()).stub_out
    out.mkdir(parents=True, exist_ok=True)
    _remove_own_containers()
    label = ["--label", f"{LABEL}=1"]
    probe_mount = ["-v", f"{PROBE_DIR}:/probe:ro"]
    run_docker(
        [
            "run", "-d", "--name", STUB_NAME, *label, "--network", network,
            "--user", f"{os.getuid()}:{os.getgid()}", "--memory", "256m", "--no-healthcheck",
            *probe_mount, "-v", f"{out}:/out", "-e", "STUB_OUT=/out",
            "--entrypoint", CONTAINER_PYTHON, image, "/probe/stub.py",
        ]
    )  # fmt: skip
    flags, process_env = env_flags(env, secrets)
    run_docker(
        [
            "run", "-d", "--name", GATEWAY_NAME, *label, "--network", network,
            "-p", f"127.0.0.1:{port}:9001", "--memory", "768m", "--cpus", "1",
            *probe_mount, *flags, image,
        ],
        env=process_env,
    )  # fmt: skip
    wait_healthy(port, build_fingerprint())
    if _inspect(PRODUCTION_GUARD, FMT_IDENTITY) != guard_before:
        raise SystemExit(
            f"{PRODUCTION_GUARD} changed while the probe started: stop and check production"
        )
    print(f"gateway up on http://127.0.0.1:{port}/chat, stub calls in {out / 'calls'}")


def down() -> None:
    """Remove the probe's own containers. A container without the probe label is left alone."""
    _remove_own_containers()
    print("probe containers removed")


def render(run_dir: Path) -> None:
    """Render the design A planner prompts inside the running gateway and write ``prompts.json``.

    Args:
        run_dir: The run directory.

    Raises:
        SystemExit: If the gateway printed no prompts line.
    """
    payload = json.dumps(
        [
            {"label": f.label, "message": f.message, "history_messages": f.history_messages}
            for f in load_fixtures()
        ]
    )
    result = run_docker(
        ["exec", "-i", GATEWAY_NAME, CONTAINER_PYTHON, "/probe/render.py"], stdin=payload
    )
    lines = [x for x in result.stdout.splitlines() if x.startswith(PROMPTS_PREFIX)]
    if not lines:
        raise SystemExit(f"the gateway printed no {PROMPTS_PREFIX} line:\n{result.stdout[-500:]}")
    target = RunPaths(run_dir).prompts
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(lines[-1][len(PROMPTS_PREFIX) :])
    print(f"prompts written to {target}")


def main(argv: Sequence[str] | None = None) -> int:
    """Run ``up``, ``render`` or ``down``.

    Args:
        argv: Command-line arguments. Default is ``sys.argv[1:]``.

    Returns:
        0 on success.
    """
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("command", choices=("up", "render", "down"))
    parser.add_argument("--run-dir", type=Path)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    args = parser.parse_args(argv)
    if args.command != "down" and args.run_dir is None:
        parser.error(f"{args.command} needs --run-dir")
    if args.command == "up":
        up(args.run_dir, os.environ, args.port)
    elif args.command == "render":
        render(args.run_dir)
    else:
        down()
    return 0


if __name__ == "__main__":
    sys.exit(main())
