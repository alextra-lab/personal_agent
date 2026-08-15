"""Seshat API Gateway — FRE-206.

Provides versioned REST API endpoints for external clients (mobile PWA, cloud
execution agents, remote tooling) to access knowledge graph, session data, and
observation traces.

The gateway mounts under ``/api/v1`` via :func:`~personal_agent.gateway.app.create_gateway_router`,
included on the main execution service's FastAPI app (port 9000,
``settings.gateway_mount_local = True``).

See: docs/plans/2026-04-14-fre-206-gateway-design.md
"""
