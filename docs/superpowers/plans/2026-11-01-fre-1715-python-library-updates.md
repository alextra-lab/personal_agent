# FRE-1715 — Python library updates on 3.12

Step 2 of 3 (security fixes, library updates, Python 3.14). The ticket is the specification.

## Measured facts

- `uv lock --upgrade` changes 125 packages. Ten are major: attrs, multidict, mypy, pywin32, redis, rich,
  rpds-py, structlog, websockets, zipp. All upper bounds in `pyproject.toml` stay. Exact pins hold back
  `litellm` (1.106.0, current 1.106.6) and the OpenTelemetry family (1.45.1, current 1.45.2).
- Baseline on `main` at c99494b1: 1 failed, 15029 passed. The failure is
  `test_type_scoped_recall_keeps_unenforced_entities`.
- Servers: Redis 7.4.8, Neo4j 5.26, Postgres 17, Elasticsearch 8.19. redis-py 8.1 lists Redis 7.4 as supported.
  The Elasticsearch client stays on 8.x.

## Changes that the upgrade forces

| Cause | Effect | Change |
|---|---|---|
| SQLAlchemy 2.1 types a class-level `Column()` as `Never` | 28 mypy errors, and mypy treats later code as unreachable | Convert the 118 columns in `service/models.py` to `Mapped[...] = mapped_column(...)`. Same arguments. DDL snapshot must not change |
| redis-py 8 sets `socket_timeout=5` by default | `XREADGROUP` with `block>=5000` raises `TimeoutError`. The setting allows 10000 | `RedisStreamBus.connect` sets `socket_timeout` to the poll interval plus 5 s |
| redis-py 8 return types | mypy errors in `events/consumer.py`, two unused ignores | `cast` the read result, delete the ignores |
| DSPy 3.4 `engine="auto"` | `dspy.LM` calls the provider natively, not through LiteLLM | Pass `engine="litellm"` in `configure_dspy_lm`, raise the floor to `dspy>=3.4.0` |
| DSPy 3.4 no longer installs numpy | Three eval test modules fail at collection | Add numpy to the `dev` dependency group, which CI installs and the gateway image does not |
| FastAPI 0.142 native OpenTelemetry | With a configured global provider, every request and WebSocket gets new spans, metrics and logs, and an inbound `traceparent` becomes the parent | `FastAPI(telemetry={"tracing": False, "metrics": False, "logs": False})`, raise the floor to `fastapi>=0.142.0` |
| FastAPI 0.137 route tree | `app.routes` no longer lists included routes, three doc-path tests fail | The test reads `router.routes` |
| pytest 9.2 rejects unregistered markers | `test_dspy_adapter.py` fails at collection | Register `slow` |
| ruff 0.17 formats Markdown code blocks | `make ruff-format` rewrites 10 `AGENTS.md` files that CI's ruff 0.15.15 accepts | Exclude `*.md` from ruff |

## Not changed

- `ci.yml` pins ruff 0.15.15. The ticket forbids a CI change, so the pin stays. FRE-1716 edits CI and can move it.
- The `[tool.pydantic-mypy]` section swallows mypy options by TOML nesting. This predates the ticket.

## Steps

1. Lock upgrade, marker, numpy. Verify: suite collects.
2. Failing tests for the DSPy route, the telemetry default and the Redis timeout. Verify: red.
3. Code changes above. Verify: `make test`, `uv run mypy src/`, `uv run ruff check src/`, `uv run pip-audit`.
4. Probes outside the suite, because the suite mocks these seams: real `redis:7-alpine` for stream shapes and
   timeouts, real uvicorn with websockets 17, real repositories on the test Postgres, and an import of
   the app from a `--no-dev` environment.
