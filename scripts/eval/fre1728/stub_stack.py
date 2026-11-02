"""FRE-1728: the stub-model gateway on the eval stack. It can reach no real model and no paid vendor.

    uv run python -m scripts.eval.fre1728.stub_stack up --run-dir <run>
    uv run python -m scripts.eval.fre1728.stub_stack down

Never ``docker compose``: a compose run from a worktree recreates production's searxng. This launcher uses
``docker run`` with its own names and a label, as ``scripts/eval/fre1537/gateway.py`` does. It reuses the deployed
production image, so it builds nothing.

**What keeps a stub run from a real model:**

- ``model_roles.yaml`` is mounted with every model role bound to the local deployment, and that deployment's
  ``AGENT_SLM_BASE_URL`` names the stub.
- The embedder uses the managed path, with its endpoint at the stub.
- Each cloud key is a placeholder.
- ``--add-host`` maps ``caddy`` (the path to the owner's Mac), every production service name and every model host in
  ``config/models.yaml`` to loopback.

**What keeps it from the real-keyed eval gateways:** its own Postgres database, its own Redis index and its own
captures volume. Control and treatment hold real keys. Their idle sweep summarizes dirty sessions on real Sonnet,
so a stub session must not reach their database. It shares eval Neo4j and eval Elasticsearch, because the wipe and
the settle wait must act on those stores.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import time
from collections.abc import Mapping, Sequence
from pathlib import Path

import httpx
import yaml
from scripts.eval.fre1537.gateway import EVAL_R2_DISABLED, PRODUCTION_GUARD, env_flags

REPO_ROOT = Path(__file__).resolve().parents[3]
LABEL = "fre1728.stub"
GATEWAY_NAME = "fre1728-stub-gateway"
MODEL_NAME = "fre1728-stub-model"
CAPTURES_VOLUME = "fre1728_stub_captures"
IMAGE = "seshat-seshat-gateway"
NETWORK = "seshat_cloud-sim"
STUB_DATABASE = "fre1728_stub"
STUB_REDIS_INDEX = 5
STUB_EMBEDDING_MODEL = "stub-embedding"
GATEWAY_PORT = 9014
MODEL_PORT = 9015
STUB_URL = f"http://{MODEL_NAME}:8700"
#: The host-side URL of the stub, for MemAudit's ``OPENAI_BASE_URL``.
STUB_OPENAI_BASE_URL = f"http://127.0.0.1:{MODEL_PORT}/v1"
PLACEHOLDER = "fre1728-stub-unused"
LOCAL_DEPLOYMENT = "qwen3.8-flash-next"
#: Bindings that are not a model of their own: ``inherit`` follows the primary. The embedder and the reranker
#: stay bound, because the managed endpoint and the blocked hosts cover them.
_KEEP_DEPLOYMENTS = frozenset({"inherit", "embedding", "reranker", "reranker_fallback"})
#: Production's compose services on the shared network. None of them may answer a stub gateway.
_BLOCKED_SERVICES = (
    "caddy",
    "searxng",
    "tempo",
    "postgres",
    "neo4j",
    "elasticsearch",
    "redis",
    "seshat-gateway",
)
#: Vendors that a tool or a role can reach by a fixed name, outside ``config/models.yaml``.
_BLOCKED_VENDORS = (
    "api.openai.com",
    "api.anthropic.com",
    "api.voyageai.com",
    "api.scaleway.ai",
    "api.exa.ai",
    "api.search.brave.com",
    "api.perplexity.ai",
    "api.linear.app",
)
_URL_HOST = re.compile(r"://(?:[^/@\s]*@)?([A-Za-z0-9._-]+)")
_SECRETS = frozenset({"AGENT_DATABASE_URL", "AGENT_DATABASE_ADMIN_URL", "AGENT_NEO4J_PASSWORD"})
_NEUTRAL_OWNER_NAME = "Sam"
_EVAL_POSTGRES = "cloud-sim-postgres-eval"


def stub_role_bindings(source: str) -> str:
    """Bind every model role to the local deployment.

    Args:
        source: The text of ``config/model_roles.yaml``.

    Returns:
        The rewritten YAML.
    """
    data = yaml.safe_load(source)
    for binding in data["bindings"].values():
        if binding["deployment"] not in _KEEP_DEPLOYMENTS:
            binding["deployment"] = LOCAL_DEPLOYMENT
    return yaml.safe_dump(data, sort_keys=False)


def blocked_hosts(models_yaml: str) -> list[str]:
    """List every host that the stub gateway must not reach.

    Args:
        models_yaml: The text of ``config/models.yaml``.

    Returns:
        Sorted host names.
    """
    hosts = set(_BLOCKED_SERVICES) | set(_BLOCKED_VENDORS) | set(_URL_HOST.findall(models_yaml))
    return sorted(hosts)


def gateway_env(host_env: Mapping[str, str]) -> dict[str, str]:
    """Build the stub gateway's environment.

    Args:
        host_env: The caller's environment.

    Returns:
        The environment.

    Raises:
        SystemExit: If a required credential is missing.
    """
    missing = [k for k in ("POSTGRES_PASSWORD", "NEO4J_PASSWORD") if not host_env.get(k)]
    if missing:
        raise SystemExit(f"set {', '.join(missing)} in the environment before `up`")
    app_password = host_env.get("SESHAT_APP_EVAL_PASSWORD") or "seshat_app_dev_password"
    database = f"postgres-eval:5432/{STUB_DATABASE}"
    return {
        "AGENT_DATABASE_URL": f"postgresql+asyncpg://seshat_app:{app_password}@{database}",
        "AGENT_DATABASE_ADMIN_URL": (
            f"postgresql+asyncpg://agent:{host_env['POSTGRES_PASSWORD']}@{database}"
        ),
        "AGENT_NEO4J_URI": "bolt://neo4j-eval:7687",
        "AGENT_NEO4J_USER": "neo4j",
        "AGENT_NEO4J_PASSWORD": host_env["NEO4J_PASSWORD"],
        "AGENT_ELASTICSEARCH_URL": "http://elasticsearch-eval:9200",
        "AGENT_EVENT_BUS_ENABLED": "true",
        "AGENT_EVENT_BUS_REDIS_URL": f"redis://redis-eval:6379/{STUB_REDIS_INDEX}",
        "AGENT_OWNER_STORAGE_ALLOWLIST": '["postgres-eval","neo4j-eval","elasticsearch-eval"]',
        "AGENT_SLM_BASE_URL": STUB_URL,
        "AGENT_DEPLOYMENT_PROFILE": "eval",
        "APP_ENV": "eval",
        "API_HOST": "0.0.0.0",  # noqa: S104 - published on loopback only
        "API_PORT": "9001",
        "AGENT_LOG_LEVEL": "info",
        # A placeholder address: the wipe and the identity path do not depend on the real owner.
        "AGENT_OWNER_EMAIL": "fre1728-owner@eval.invalid",
        "AGENT_OWNER_NAME": _NEUTRAL_OWNER_NAME,
        "AGENT_ANTHROPIC_API_KEY": PLACEHOLDER,
        "AGENT_OPENAI_API_KEY": PLACEHOLDER,
        "AGENT_GATEWAY_AUTH_ENABLED": "false",
        "AGENT_MCP_GATEWAY_ENABLED": "false",
        "AGENT_PRIMITIVE_TOOLS_ENABLED": "false",
        "AGENT_DELEGATION_ENABLED": "false",
        "AGENT_EXPANSION_ENABLED": "false",
        "AGENT_ENABLE_MEMORY_GRAPH": "true",
        "AGENT_ENABLE_SECOND_BRAIN": "true",
        "AGENT_SECOND_BRAIN_RESOURCE_GATING_ENABLED": "false",
        "AGENT_PROACTIVE_MEMORY_ENABLED": "true",
        "AGENT_MULTIPATH_RECALL_ENABLED": "true",
        "AGENT_LEXICAL_ARM_ENABLED": "true",
        # Production's value. Without it a broad recall reports no relevance score, and the turn says that its
        # records could not be reached.
        "AGENT_RELEVANCE_BOUNDED_RECALL_ENABLED": "true",
        "AGENT_RECALL_SIMILARITY_FLOOR": "0.60",
        # The relevance gate trusts only the calibrated reranker. The stub's score comes from elsewhere, so an armed
        # gate answers that it cannot tell, and the turn says that its records could not be reached.
        "AGENT_BROAD_RECALL_RELEVANCE_GATE_ENABLED": "false",
        "AGENT_ENTITY_MATCH_RELEVANCE_GATE_ENABLED": "false",
        "AGENT_SUBSTRATE_PROFILE": "managed_embedder",
        "AGENT_MANAGED_EMBEDDING_ENDPOINT": f"{STUB_URL}/v1",
        "AGENT_MANAGED_EMBEDDING_MODEL": STUB_EMBEDDING_MODEL,
        "AGENT_MANAGED_EMBEDDING_TOKEN": PLACEHOLDER,
        "AGENT_CAPTAINS_LOG_REFLECTION_ENABLED": "false",
        "AGENT_CONSTRAINT_PAUSE_TIMEOUT_SECONDS": "5",
        **EVAL_R2_DISABLED,
    }


def add_host_flags(hosts: Sequence[str]) -> list[str]:
    """Map each host to loopback on both IPv4 and IPv6.

    An IPv4 entry alone leaves the IPv6 lookup to public DNS, so the real vendor address still resolves.

    Args:
        hosts: The host names.

    Returns:
        The ``docker run`` flags.
    """
    flags: list[str] = []
    for host in hosts:
        flags += ["--add-host", f"{host}:127.0.0.1", "--add-host", f"{host}:[::1]"]
    return flags


def assert_stub_env_is_eval_only(env: Mapping[str, str]) -> None:
    """Refuse an environment that names a production host.

    Args:
        env: The gateway environment.

    Raises:
        SystemExit: If a URL names a host that is not an ``-eval`` store, the stub or a ``.invalid`` host.
    """
    for key, value in env.items():
        for host in _URL_HOST.findall(value):
            if not (host.endswith("-eval") or host == MODEL_NAME or host.endswith(".invalid")):
                raise SystemExit(f"{key} names production host {host!r}")


def _docker(
    args: Sequence[str],
    *,
    env: Mapping[str, str] | None = None,
    check: bool = True,
    stdin: str | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["docker", *args],
        env={**os.environ, **(env or {})},
        check=check,
        capture_output=True,
        text=True,
        input=stdin,
    )


def _identity(name: str) -> str | None:
    result = _docker(["inspect", "--format", "{{.Id}} {{.State.StartedAt}}", name], check=False)
    return result.stdout.strip() if result.returncode == 0 else None


def _remove_own_containers() -> None:
    for name in (GATEWAY_NAME, MODEL_NAME):
        label = _docker(
            ["inspect", "--format", '{{index .Config.Labels "' + LABEL + '"}}', name], check=False
        )
        if label.returncode == 0:
            if label.stdout.strip() != "1":
                raise SystemExit(f"{name} exists without the label {LABEL}: not touching it")
            _docker(["rm", "-f", name])


def _create_database() -> None:
    exists = _docker(
        [
            "exec",
            _EVAL_POSTGRES,
            "psql",
            "-U",
            "agent",
            "-d",
            "postgres",
            "-tAc",
            f"SELECT 1 FROM pg_database WHERE datname = '{STUB_DATABASE}'",
        ]
    )
    if exists.stdout.strip():
        return
    _docker(
        ["exec", _EVAL_POSTGRES, "psql", "-U", "agent", "-d", "postgres", "-c",
         f"CREATE DATABASE {STUB_DATABASE}"]
    )  # fmt: skip
    schema = (REPO_ROOT / "docker" / "postgres" / "init.sql").read_text()
    _docker(
        [
            "exec",
            "-i",
            _EVAL_POSTGRES,
            "psql",
            "-U",
            "agent",
            "-d",
            STUB_DATABASE,
            "-q",
            "-v",
            "ON_ERROR_STOP=1",
        ],
        stdin=schema,
    )


def _wait_healthy(timeout: float = 180.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if httpx.get(f"http://127.0.0.1:{GATEWAY_PORT}/health", timeout=5.0).status_code == 200:
                return
        except httpx.HTTPError:
            pass
        time.sleep(3.0)
    logs = _docker(["logs", "--tail", "40", GATEWAY_NAME], check=False)
    raise SystemExit(
        f"the stub gateway was not healthy:\n{logs.stdout[-3000:]}{logs.stderr[-3000:]}"
    )


def up(run_dir: Path, host_env: Mapping[str, str]) -> None:
    """Start the stub model and the stub gateway.

    Args:
        run_dir: The run directory. The stub writes its calls to ``<run_dir>/stub-out``.
        host_env: The caller's environment.

    Raises:
        SystemExit: If a precondition fails, or if a production container changed.
    """
    env = gateway_env(host_env)
    assert_stub_env_is_eval_only(env)
    guard_before = _identity(PRODUCTION_GUARD)
    run_dir = run_dir.resolve()
    out = run_dir / "stub-out"
    out.mkdir(parents=True, exist_ok=True)
    roles = run_dir / "model_roles.stub.yaml"
    roles.write_text(stub_role_bindings((REPO_ROOT / "config" / "model_roles.yaml").read_text()))
    hosts = blocked_hosts((REPO_ROOT / "config" / "models.yaml").read_text())
    _remove_own_containers()
    _create_database()
    label = ["--label", f"{LABEL}=1"]
    here = Path(__file__).resolve().parent
    _docker(
        [
            "run", "-d", "--name", MODEL_NAME, *label, "--network", NETWORK,
            "-p", f"127.0.0.1:{MODEL_PORT}:8700",
            "--user", f"{os.getuid()}:{os.getgid()}", "--memory", "256m", "--no-healthcheck",
            "-v", f"{here}:/stub:ro", "-v", f"{out}:/out", "-e", "STUB_OUT=/out",
            "--entrypoint", "/app/.venv/bin/python", IMAGE, "/stub/stub_model.py",
        ]
    )  # fmt: skip
    flags, process_env = env_flags(env, _SECRETS & env.keys())
    add_hosts = add_host_flags(hosts)
    _docker(
        [
            "run", "-d", "--name", GATEWAY_NAME, *label, "--network", NETWORK,
            "-p", f"127.0.0.1:{GATEWAY_PORT}:9001", "--memory", "768m", "--cpus", "1",
            "-v", f"{CAPTURES_VOLUME}:/app/telemetry/captains_log/captures",
            "-v", f"{roles}:/app/config/model_roles.yaml:ro",
            *add_hosts, *flags, IMAGE,
        ],
        env=process_env,
    )  # fmt: skip
    _wait_healthy()
    if _identity(PRODUCTION_GUARD) != guard_before:
        raise SystemExit(f"{PRODUCTION_GUARD} changed: stop and check production")
    print(f"stub gateway on 127.0.0.1:{GATEWAY_PORT}, stub calls in {out / 'calls'}")


def down() -> None:
    """Remove the labeled containers and the stub's captures volume and database."""
    _remove_own_containers()
    _docker(["volume", "rm", "-f", CAPTURES_VOLUME], check=False)
    _docker(
        ["exec", _EVAL_POSTGRES, "psql", "-U", "agent", "-d", "postgres", "-c",
         f"DROP DATABASE IF EXISTS {STUB_DATABASE}"],
        check=False,
    )  # fmt: skip
    print("stub stack removed")


def main(argv: Sequence[str] | None = None) -> int:
    """Run ``up`` or ``down``.

    Args:
        argv: Command-line arguments.

    Returns:
        0 on success.
    """
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("command", choices=("up", "down"))
    parser.add_argument("--run-dir", type=Path)
    args = parser.parse_args(argv)
    if args.command == "up":
        if args.run_dir is None:
            parser.error("up needs --run-dir")
        up(args.run_dir, os.environ)
    else:
        down()
    return 0


if __name__ == "__main__":
    sys.exit(main())
