"""Seshat API Gateway — FRE-206.

Provides versioned REST API endpoints for external clients (mobile PWA, cloud
execution agents, remote tooling) to access knowledge graph, session data, and
observation traces.

The gateway mounts under ``/api/v1`` and supports two deployment modes:

- **Local dev** (default): routes mounted on the same FastAPI app as the
  execution service (port 9000, ``settings.gateway_mount_local = True``).
- **Standalone**: ``create_gateway_app()`` produces a separate uvicorn process
  (port 9001 or behind reverse proxy) with a minimal lifespan — no LLM client,
  no orchestrator.

See: docs/plans/2026-04-14-fre-206-gateway-design.md
"""
