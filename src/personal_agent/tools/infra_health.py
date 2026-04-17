"""Infrastructure health check tool (FRE / ADR-0028 Tier-1).

Probes every backend service the agent depends on using only Python stdlib
and already-imported deps — no curl/wget required.  Safe to call from inside
a Docker container where CLI tools may be absent.

Services checked:
  postgres        TCP connect via settings.database_url
  neo4j           Bolt TCP (7687) + HTTP browser (7474)
  elasticsearch   GET /_cluster/health
  redis           TCP connect via settings.event_bus_redis_url
  embeddings      GET /health (endpoint from models config)
  reranker        GET /health (endpoint from models config)
"""

from __future__ import annotations

import socket
import urllib.error
import urllib.request
from typing import Any
from urllib.parse import urlparse

import yaml

from personal_agent.config import settings
from personal_agent.telemetry import TraceContext, get_logger
from personal_agent.tools.types import ToolDefinition

log = get_logger(__name__)

_TCP_TIMEOUT = 3.0
_HTTP_TIMEOUT = 5.0


def _tcp_check(host: str, port: int) -> dict[str, Any]:
    try:
        s = socket.create_connection((host, port), timeout=_TCP_TIMEOUT)
        s.close()
        return {"reachable": True}
    except Exception as exc:
        return {"reachable": False, "error": str(exc)}


def _http_check(url: str) -> dict[str, Any]:
    try:
        with urllib.request.urlopen(url, timeout=_HTTP_TIMEOUT) as resp:
            body = resp.read(512).decode("utf-8", errors="replace")
            return {"reachable": True, "http_status": resp.status, "body": body[:300]}
    except urllib.error.HTTPError as exc:
        return {"reachable": True, "http_status": exc.code, "error": str(exc)}
    except Exception as exc:
        return {"reachable": False, "error": str(exc)}


def _parse_host_port(uri: str, default_port: int) -> tuple[str, int]:
    parsed = urlparse(uri)
    return parsed.hostname or "localhost", parsed.port or default_port


def _load_model_endpoints() -> tuple[str | None, str | None]:
    """Return (embedding_base_url, reranker_base_url) from the active models config."""
    try:
        with open(settings.model_config_path) as f:
            cfg = yaml.safe_load(f)
        models = cfg.get("models", {})
        emb = models.get("embedding", {}).get("endpoint")
        rnk = models.get("reranker", {}).get("endpoint")
        # Strip /v1 suffix so we can append /health
        if emb:
            emb = emb.rstrip("/").removesuffix("/v1")
        if rnk:
            rnk = rnk.rstrip("/").removesuffix("/v1")
        return emb, rnk
    except Exception:
        return None, None


def infra_health_executor(
    ctx: TraceContext | None = None,
) -> dict[str, Any]:
    """Check reachability and basic health of all backend infrastructure services.

    Args:
        ctx: Optional trace context for structured logging.

    Returns:
        Dict with:
        - success: bool (always True — errors are per-service)
        - all_reachable: bool (True only when every service responds)
        - services: per-service result dicts with at minimum {"reachable": bool}
    """
    trace_id = getattr(ctx, "trace_id", "unknown") if ctx else "unknown"
    services: dict[str, Any] = {}

    # PostgreSQL — TCP connect
    try:
        host, port = _parse_host_port(settings.database_url, 5432)
        services["postgres"] = {"host": host, "port": port, **_tcp_check(host, port)}
    except Exception as exc:
        services["postgres"] = {"reachable": False, "error": str(exc)}

    # Neo4j — Bolt TCP + HTTP browser port
    try:
        host, bolt_port = _parse_host_port(settings.neo4j_uri, 7687)
        services["neo4j"] = {
            "bolt": {"host": host, "port": bolt_port, **_tcp_check(host, bolt_port)},
            "http": {"url": f"http://{host}:7474", **_http_check(f"http://{host}:7474")},
        }
    except Exception as exc:
        services["neo4j"] = {"reachable": False, "error": str(exc)}

    # Elasticsearch — cluster health endpoint
    try:
        es_base = settings.elasticsearch_url.rstrip("/")
        result = _http_check(f"{es_base}/_cluster/health")
        services["elasticsearch"] = {"url": f"{es_base}/_cluster/health", **result}
    except Exception as exc:
        services["elasticsearch"] = {"reachable": False, "error": str(exc)}

    # Redis — TCP connect
    try:
        host, port = _parse_host_port(settings.event_bus_redis_url, 6379)
        services["redis"] = {"host": host, "port": port, **_tcp_check(host, port)}
    except Exception as exc:
        services["redis"] = {"reachable": False, "error": str(exc)}

    # Embeddings + Reranker — /health HTTP
    emb_url, rnk_url = _load_model_endpoints()

    if emb_url:
        result = _http_check(f"{emb_url}/health")
        services["embeddings"] = {"url": f"{emb_url}/health", **result}
    else:
        services["embeddings"] = {"reachable": None, "note": "No endpoint in models config"}

    if rnk_url:
        result = _http_check(f"{rnk_url}/health")
        services["reranker"] = {"url": f"{rnk_url}/health", **result}
    else:
        services["reranker"] = {"reachable": None, "note": "No endpoint in models config"}

    reachable_flags = [
        v.get("reachable")
        for k, v in services.items()
        if isinstance(v, dict) and "bolt" not in v  # neo4j has nested dicts
    ]
    # neo4j: check bolt sub-dict
    if isinstance(services.get("neo4j"), dict) and "bolt" in services["neo4j"]:
        reachable_flags.append(services["neo4j"]["bolt"].get("reachable"))

    all_reachable = all(f is True for f in reachable_flags if f is not None)

    def _is_reachable(k: str, v: Any) -> bool | None:
        if k == "neo4j":
            return v.get("bolt", {}).get("reachable")
        return v.get("reachable") if isinstance(v, dict) else None

    log.info(
        "infra_health_checked",
        trace_id=trace_id,
        all_reachable=all_reachable,
        reachable=[k for k, v in services.items() if _is_reachable(k, v) is True],
        unreachable=[k for k, v in services.items() if _is_reachable(k, v) is False],
    )

    return {"success": True, "all_reachable": all_reachable, "services": services}


infra_health_tool = ToolDefinition(
    name="infra_health",
    description=(
        "Check reachability and basic health of all backend infrastructure services "
        "(Postgres, Neo4j, Elasticsearch, Redis, embeddings, reranker). "
        "Uses TCP/HTTP probes via internal Docker hostnames — works from inside a container. "
        "Returns per-service status with host/port/HTTP details."
    ),
    category="read_only",
    parameters=[],
    risk_level="low",
    allowed_modes=["NORMAL", "ALERT", "DEGRADED", "LOCKDOWN", "RECOVERY"],
    requires_approval=False,
    requires_sandbox=False,
    timeout_seconds=30,
    rate_limit_per_hour=60,
)
