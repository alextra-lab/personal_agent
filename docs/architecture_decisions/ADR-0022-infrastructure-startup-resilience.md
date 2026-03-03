# ADR-0022: Infrastructure Startup Resilience and Developer Workflow

**Status**: Accepted
**Date**: 2026-03-03
**Deciders**: System Architect
**Related**: ADR-0016 (Service-Based Cognitive Architecture)

---

## 1. Context

### Problem

The Personal Agent service depends on three external infrastructure services — PostgreSQL, Elasticsearch, and Neo4j — all managed via Docker Compose. When any of these services is not running, the FastAPI `lifespan()` startup sequence fails with a raw low-level exception (e.g. `ConnectionRefusedError: [Errno 61] Connection refused`) buried in a 50-line SQLAlchemy/asyncpg traceback.

This provides no actionable guidance: the developer must read the full stack trace, identify the root cause, and know independently how to start the infrastructure. This problem was observed in practice when `uvicorn` was invoked without first running `docker compose up`.

### Current Startup Sequence (as of ADR-0016)

```
lifespan() start
├── init_db()              ← FATAL if PostgreSQL not running (no guard)
├── es_handler.connect()   ← non-fatal, graceful degradation
├── memory_service.connect() ← FATAL if Neo4j not running (no guard)
├── BrainstemScheduler.start()
└── MCPGatewayAdapter.initialize() ← non-fatal (explicit try/except)
```

Two distinct fatal paths exist with no actionable error messages:

1. **PostgreSQL** — `init_db()` raises `ConnectionRefusedError` via SQLAlchemy's asyncpg dialect with a 50-line traceback
2. **Neo4j** — `memory_service.connect()` has no `try/except`, so any Neo4j connection error is also fatal

### Developer Workflow Gap

`scripts/init-services.sh` exists and correctly handles Docker Compose startup and health-check polling, but:
- It is completely decoupled from the application start process
- There is no documented or enforced sequence for starting the full stack
- There is no `Makefile` providing a canonical, single-command developer workflow

---

## 2. Decision

### 2a. Fail-fast with actionable pre-flight checks

Before any service initialization, `lifespan()` performs explicit TCP-level connectivity checks for required services. On failure, it:

1. Emits a single structured `startup_preflight_failed` log event identifying the service, host, port, and a `remedy` field with the exact command to run
2. Raises a `RuntimeError` with a human-readable message — no raw asyncpg/Neo4j stacktrace reaches the developer

PostgreSQL is **required** (checked unconditionally). Elasticsearch and Neo4j are checked only when enabled in settings, and their failure produces a `WARNING`-level event rather than aborting startup (matching existing graceful-degradation behavior for optional services).

### 2b. Fix Neo4j fatal path

Wrap `memory_service.connect()` in `try/except` to match the existing Elasticsearch pattern — connection failure disables Neo4j functionality but does not crash the service.

### 2c. Makefile for canonical developer workflow

A `Makefile` at the project root codifies the authoritative start/stop sequences, removing the dependency on developer knowledge of the correct invocation order.

---

## 3. Alternatives Considered

### Alternative 1: Auto-start Docker Compose as a subprocess

The service spawns `docker compose up -d` automatically if services are unreachable.

**Rejected** — creates hidden side-effects (infrastructure mutation as a side-effect of app startup), is inappropriate in CI/CD or production environments, and masks misconfiguration that should be fixed explicitly.

### Alternative 2: Retry with exponential backoff

The service retries connectivity checks with backoff before failing, allowing containers to finish starting.

**Rejected for MVP** — correct behavior for a container-orchestrated production deploy (where readiness probes handle ordering), but inappropriate for local development where the services must already be healthy. Backoff hides the actual problem (infra not started) and extends the time-to-failure. This is the right approach for a future Kubernetes/Docker Swarm deployment and can be added as an opt-in `--wait-for-infra` flag at that time.

### Alternative 3: Status quo — surface raw SQLAlchemy/asyncpg error

No changes; let the existing error propagate.

**Rejected** — poor developer experience, no remediation path visible in the error output.

### Alternative 4: Move all infrastructure to optional (graceful degradation everywhere)

Make PostgreSQL optional like Elasticsearch, allowing the service to start without a database.

**Rejected** — PostgreSQL is the primary persistence layer for sessions. The service has no meaningful function without it. Pretending it is optional would produce confusing runtime errors on every API call.

---

## 4. Consequences

### Positive

- Developer gets a single actionable log line when infrastructure is missing, naming the failed service and the fix command
- `make dev` is the single canonical command to start the full stack in development
- `make infra-up / infra-down` decouple infrastructure lifecycle from service lifecycle for scripts and CI
- Neo4j failures are no longer silently fatal — the service degrades gracefully matching ES behavior
- The dependency contract between the service and its infrastructure is made explicit in code, not just in documentation

### Negative

- Pre-flight TCP check adds ~2s to startup time when infrastructure is down (timeout before the error is emitted). This is acceptable since it replaces an equivalent asyncpg connection timeout.
- A pre-flight check that passes does not guarantee the service will function correctly (e.g. database schema migrations, authentication failures). It only confirms TCP reachability. Full health validation remains the responsibility of the services themselves.

---

## 5. Implementation

### 5a. Pre-flight check helper (`src/personal_agent/service/app.py`)

```python
async def _preflight_check_tcp(service: str, host: str, port: int) -> None:
    """Attempt a TCP connection to verify a service is reachable.

    Args:
        service: Human-readable service name for error messages.
        host: Hostname or IP address.
        port: TCP port.

    Raises:
        RuntimeError: If the service is not reachable within 2 seconds.
    """
    try:
        _, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port), timeout=2.0
        )
        writer.close()
        await writer.wait_closed()
    except (OSError, asyncio.TimeoutError) as e:
        log.error(
            "startup_preflight_failed",
            service=service,
            host=host,
            port=port,
            remedy="Run 'make infra-up' to start required Docker services",
            error=str(e),
        )
        raise RuntimeError(
            f"{service} at {host}:{port} is unreachable. "
            f"Run 'make infra-up' to start Docker services."
        ) from e
```

Called in `lifespan()` before `init_db()`:

```python
# Pre-flight: verify PostgreSQL is reachable
await _preflight_check_tcp("PostgreSQL", pg_host, pg_port)
await init_db()
```

### 5b. Neo4j safety guard

```python
if settings.enable_memory_graph:
    try:
        memory_service = MemoryService()
        await memory_service.connect()
        log.info("memory_service_initialized")
    except Exception as e:
        log.warning(
            "memory_service_connect_failed",
            error=str(e),
            remedy="Neo4j may not be running. Run 'make infra-up'.",
        )
        memory_service = None
```

### 5c. Makefile

```makefile
.PHONY: infra-up infra-down dev stop logs

infra-up:
    docker compose up -d
    bash scripts/init-services.sh

infra-down:
    docker compose down

dev: infra-up
    uv run uvicorn personal_agent.service.app:app --reload --port 9000

stop:
    docker compose stop

logs:
    docker compose logs -f
```

---

## 6. Acceptance Criteria

- [ ] Running `uv run uvicorn personal_agent.service.app:app` with Docker services stopped produces exactly one structured log event at `ERROR` level with keys: `service`, `host`, `port`, `remedy`
- [ ] The `remedy` field contains the string `make infra-up`
- [ ] The service startup time when infra is **running** is not measurably affected (pre-flight check completes in < 50ms on localhost)
- [ ] `make infra-up && make dev` successfully starts the full stack in a single terminal command
- [ ] `make infra-down` stops all Docker services cleanly
- [ ] Neo4j connection failure does not crash the service — it logs a warning and continues with `memory_service = None`
