"""FRE-1590: compare the environment of the production gateway with each eval gateway.

Reads ``docker exec <container> printenv -0`` of the production gateway and of the two eval
gateways, and prints the key names that differ, in three groups: only in the first container,
only in the second, and value differs. It also compares control with treatment, which must
differ in nothing but ``HOSTNAME``: the two arms of FRE-1590 Part 2 differ only in the skill
catalog, which is a mount, not a variable.

It never prints or stores a value. Values are compared in memory and dropped.

Each known difference has a one-line reason in ``EXPECTED`` or ``EXPECTED_PREFIXES``. Any other
difference is unexpected and the exit code is 1. Run from the repo root:

    uv run python -m scripts.eval.fre1590.envdiff
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass

PRODUCTION = "cloud-sim-seshat-gateway"
CONTROL = "cloud-sim-seshat-gateway-control"
TREATMENT = "cloud-sim-seshat-gateway-treatment"

#: The production gateway reads all of /opt/seshat/.env (``env_file``). The eval gateways list
#: each key (docker-compose.eval.yml), so a key that only production has is a key that eval does
#: not pass. Each one here is a decision with its reason. A behaviour key that is not here fails.
EXPECTED: Mapping[str, str] = {
    # Each container
    "HOSTNAME": "Docker sets the container id.",
    "AGENT_BUILD_FINGERPRINT": "The eval image is built from the tree under test.",
    # Eval substrate (FRE-375)
    "AGENT_DATABASE_URL": "Eval uses postgres-eval as the restricted role.",
    "AGENT_DATABASE_ADMIN_URL": "Eval uses postgres-eval.",
    "AGENT_NEO4J_URI": "Eval uses neo4j-eval.",
    "AGENT_NEO4J_USER": "Eval states the default user explicitly.",
    "AGENT_NEO4J_PASSWORD": "Eval uses the neo4j-eval password.",
    "AGENT_ELASTICSEARCH_URL": "Eval uses elasticsearch-eval.",
    "AGENT_EVENT_BUS_REDIS_URL": "Eval uses redis-eval (FRE-1342).",
    "AGENT_OWNER_STORAGE_ALLOWLIST": "Eval declares its -eval stores (FRE-1344).",
    "AGENT_SYSGRAPH_DATABASE_URL": "Points at production Postgres; eval does not pass it.",
    "AGENT_DEPLOYMENT_PROFILE": "eval, not cloud (ADR-0132).",
    "APP_ENV": "eval, not production.",
    # Eval stack settings (docker-compose.eval.yml)
    "AGENT_GATEWAY_AUTH_ENABLED": "The eval stack has no auth.",
    "AGENT_MCP_GATEWAY_ENABLED": "MCP is off on eval.",
    "AGENT_MCP_GATEWAY_COMMAND": "MCP is off on eval.",
    "MCP_SECRETS_FILE": "MCP is off on eval.",
    "AGENT_APPROVAL_UI_ENABLED": "Eval has no approval UI; asks are denied (ADR-0063 Amendment B).",
    "AGENT_PRIMITIVE_TOOLS_ENABLED": "Both eval arms run primitives; the value can differ from .env.",
    "AGENT_EXTRA_PERMISSION_RULES_PATH": "Eval adds permissions.eval.yaml (ADR-0156 D8.5).",
    "AGENT_DELEGATION_ENABLED": "Eval enables delegation for CP-05 (FRE-1376).",
    "AGENT_CLOUD_WEEKLY_BUDGET_USD": "Eval has its own budget cap.",
    "AGENT_LOG_LEVEL": "Eval states its log level explicitly.",
    "AGENT_EVENT_BUS_ENABLED": "Eval enables the bus explicitly (FRE-1342).",
    # FRE-1590 override
    "AGENT_ENABLE_SECOND_BRAIN": "FRE-1590: consolidation off, no extraction cost, no graph writes.",
    "AGENT_CONSTRAINT_PAUSE_TIMEOUT_SECONDS": "FRE-1581: no UI answers a pause on eval.",
    # Not passed to eval on purpose (docker-compose.eval.yml comments)
    "AGENT_LINEAR_API_KEY": "Write-capable credential, kept from eval (security review).",
    "AGENT_PERPLEXITY_API_KEY": "Tool credential, kept from eval (security review).",
    "AGENT_VOYAGE_API_KEY": "Tool credential, kept from eval (security review).",
    "AGENT_SESHAT_CHANNEL_SECRET": "Production dispatch channel; not an eval concern.",
    "AGENT_ARTIFACTS_PUBLIC_BASE_URL": "Artifact publishing is not passed to eval.",
    "AGENT_ARTIFACT_RESOLVE_INTERNAL_TOKEN": "Artifact publishing is not passed to eval.",
    "AGENT_ARTIFACTS_EGRESS_BASE_URL": "Artifact publishing is not passed to eval.",
    "AGENT_CORS_ALLOWED_ORIGINS": "Browser origins; eval has no PWA.",
    "AGENT_ALLOWED_WS_ORIGINS": "Browser origins; eval has no PWA.",
    "AGENT_PWA_PUBLIC_ORIGIN": "Browser origins; eval has no PWA.",
    "AGENT_HOST": "Public host name for Caddy; not read by the gateway's turn path.",
    "AGENT_INSIGHTS_ENABLED": "Insights run after consolidation, which is off.",
    "AGENT_INSIGHTS_WIRING_ENABLED": "Insights run after consolidation, which is off.",
    "AGENT_FEEDBACK_POLLING_ENABLED": "Polls Linear, which eval cannot reach.",
    "AGENT_GRAPH_QUALITY_GOVERNANCE_ENABLED": "Graph quality runs on consolidation, which is off.",
    "AGENT_OTEL_EXPORTER_ENDPOINT": "Eval turns do not export spans to production Tempo.",
    # Compose interpolation and other services, not read by the gateway
    "POSTGRES_PASSWORD": "Compose interpolation only.",
    "SESHAT_APP_PASSWORD": "Compose interpolation only; eval uses its own (FRE-1344).",
    "NEO4J_PASSWORD": "Compose interpolation only.",
    "STUDY_NEO4J_PASSWORD": "Study harness only.",
    "CLOUDFLARE_TUNNEL_TOKEN": "cloudflared only.",
    "NEO4J_ADVERTISED_ADDRESS": "neo4j service only.",
    "ARTIFACTS_HOST": "Caddy only.",
    "NEXT_PUBLIC_ARTIFACTS_HOST": "PWA build only.",
    "ES_HOST": "Caddy only.",
    "GRAPH_HOST": "Caddy only.",
    "OTLP_HOST": "Caddy only.",
    "SLM_HOST": "Caddy only.",
    "SESHAT_URL": "Owner tooling only.",
}

#: Families of keys with one reason each.
EXPECTED_PREFIXES: Mapping[str, str] = {
    "AGENT_R2_": "Artifact storage is not passed to eval.",
    "AGENT_FRESHNESS_": "Freshness runs on the knowledge graph after consolidation, which is off.",
    "AGENT_SECOND_BRAIN_": "Consolidation tuning; consolidation is off (resource gating is passed).",
    "AGENT_EVENT_BUS_": "Bus consumer tuning; eval keeps the defaults.",
    "CF_ACCESS_": "Cloudflare Access belongs to Caddy, not the gateway (ADR-0132 D1).",
    "GATEWAY_TOKEN_": "Gateway auth is off on eval.",
    "GRAFANA_": "Grafana only.",
}

#: control and treatment must have the same environment except these.
EXPECTED_BETWEEN_ARMS: Mapping[str, str] = {
    "HOSTNAME": "Docker sets the container id.",
}

FetchEnv = Callable[[str], Mapping[str, str]]


class EnvReadError(RuntimeError):
    """``docker exec ... printenv`` failed for a container."""


def fetch_env(container: str) -> dict[str, str]:
    """Read the environment of a running container.

    ``printenv -0`` separates entries with NUL, so a value that holds a newline cannot be read
    as a key. The output is never printed, not even on an error.

    Args:
        container: The container name.

    Returns:
        The environment, key to value.

    Raises:
        EnvReadError: If ``docker exec`` fails.
    """
    result = subprocess.run(
        ["docker", "exec", container, "printenv", "-0"],
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        raise EnvReadError(f"docker exec {container} printenv failed (exit {result.returncode})")
    env: dict[str, str] = {}
    for entry in result.stdout.decode("utf-8", errors="replace").split("\0"):
        key, sep, value = entry.partition("=")
        if sep and key:
            env[key] = value
    return env


@dataclass(frozen=True)
class EnvDiff:
    """The key names that differ between two environments. No value is held.

    Attributes:
        left: The first container.
        right: The second container.
        only_left: Keys only the first has.
        only_right: Keys only the second has.
        differs: Keys both have, with different values.
    """

    left: str
    right: str
    only_left: tuple[str, ...]
    only_right: tuple[str, ...]
    differs: tuple[str, ...]


def compare(
    left: str, left_env: Mapping[str, str], right: str, right_env: Mapping[str, str]
) -> EnvDiff:
    """Compare two environments by key and by value equality.

    Args:
        left: The first container name.
        left_env: Its environment.
        right: The second container name.
        right_env: Its environment.

    Returns:
        The differing key names, sorted.
    """
    both = left_env.keys() & right_env.keys()
    return EnvDiff(
        left=left,
        right=right,
        only_left=tuple(sorted(left_env.keys() - right_env.keys())),
        only_right=tuple(sorted(right_env.keys() - left_env.keys())),
        differs=tuple(sorted(k for k in both if left_env[k] != right_env[k])),
    )


def reason_for(
    key: str, expected: Mapping[str, str], prefixes: Mapping[str, str] | None = None
) -> str | None:
    """Return why a key may differ, or ``None`` when the difference is unexpected.

    Args:
        key: The variable name.
        expected: Exact names and their reasons.
        prefixes: Name prefixes and their reasons.

    Returns:
        The reason, or ``None``.
    """
    if key in expected:
        return expected[key]
    for prefix, reason in (prefixes or {}).items():
        if key.startswith(prefix):
            return reason
    return None


def render(
    diff: EnvDiff, expected: Mapping[str, str], prefixes: Mapping[str, str] | None = None
) -> tuple[list[str], int]:
    """Render one comparison as lines of key names and reasons.

    Args:
        diff: The comparison.
        expected: Exact names and their reasons.
        prefixes: Name prefixes and their reasons.

    Returns:
        The lines, and the count of unexpected keys.
    """
    lines = [f"== {diff.left} vs {diff.right}"]
    unexpected = 0
    groups = (
        (f"only in {diff.left}", diff.only_left),
        (f"only in {diff.right}", diff.only_right),
        ("value differs", diff.differs),
    )
    for title, keys in groups:
        lines.append(f"  {title}: {len(keys)}")
        for key in keys:
            reason = reason_for(key, expected, prefixes)
            if reason is None:
                unexpected += 1
                lines.append(f"    UNEXPECTED  {key}")
            else:
                lines.append(f"    expected    {key}  ({reason})")
    return lines, unexpected


def run(fetch: FetchEnv = fetch_env) -> tuple[list[str], int]:
    """Compare production with each eval gateway, and control with treatment.

    Args:
        fetch: Reads one container's environment. Tests pass a fake.

    Returns:
        The report lines, and the total count of unexpected keys.
    """
    envs = {name: fetch(name) for name in (PRODUCTION, CONTROL, TREATMENT)}
    pairs = (
        (PRODUCTION, CONTROL, EXPECTED, EXPECTED_PREFIXES),
        (PRODUCTION, TREATMENT, EXPECTED, EXPECTED_PREFIXES),
        (CONTROL, TREATMENT, EXPECTED_BETWEEN_ARMS, None),
    )
    lines: list[str] = []
    total = 0
    for left, right, expected, prefixes in pairs:
        diff = compare(left, envs[left], right, envs[right])
        block, unexpected = render(diff, expected, prefixes)
        lines.extend(block)
        total += unexpected
    return lines, total


def main(argv: Sequence[str] | None = None, fetch: FetchEnv = fetch_env) -> int:
    """Print the key-name differences; exit 1 on any unexpected one.

    Args:
        argv: Command-line arguments (none are taken). Default is ``sys.argv[1:]``.
        fetch: Reads one container's environment. Tests pass a fake.

    Returns:
        0 when every difference is expected, 1 otherwise, 2 when a container cannot be read.
    """
    argparse.ArgumentParser(description=__doc__.splitlines()[0]).parse_args(argv)
    try:
        lines, unexpected = run(fetch)
    except EnvReadError as exc:
        sys.stderr.write(f"{exc}\n")
        return 2
    sys.stdout.write("\n".join(lines) + "\n")
    sys.stdout.write(f"RESULT: {'PASS' if not unexpected else f'FAIL ({unexpected} unexpected)'}\n")
    return 1 if unexpected else 0


if __name__ == "__main__":
    sys.exit(main())
