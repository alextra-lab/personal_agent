# FRE-1070 — the OTel Collector as trace egress, with redaction and effective-config artifacts

**Backing ADR:** ADR-0129 D5 (design intent only — ADR-0129's own 10 acceptance criteria belong to
its seam ticket, FRE-1073, per ADR-0130 D1/D2; this ticket carries only its own 5 ACs below).

**Revision note:** this plan went through one codex plan-review round. The original draft proposed
`otel/opentelemetry-collector-contrib` + the `redaction` processor (allow-list mode), justified by
a claim that core ships no attribute-redaction mechanism at all. Codex verified against the
upstream 0.158.0 manifests that this claim was **wrong**: core's `otelcol` distribution ships the
`attributes` processor, which supports declarative key-based deletion (a blocklist) — enough to
satisfy AC-1 without leaving "vanilla upstream" at all. Codex also caught that the original
`allowed_keys` draft would have silently deleted real span attributes
(`personal_agent.step.iteration`, `personal_agent.step.tool_count`, `personal_agent.tool.name`) had
it shipped. This revision replaces contrib+allow-list with **core+blocklist**, and hardens the
AC-1/AC-2/AC-3 verification per the rest of that review. See git history of this file for the
original draft and the full review transcript (ticket comment on FRE-1070 links it).

## Scope (this ticket's own acceptance criteria)

- **AC-1** — a declared redaction rule demonstrably fires on a span attribute that passed through
  the running Collector, and a non-matching attribute survives (positive control).
- **AC-2** — the running Collector container's image belongs to the upstream
  `otel/opentelemetry-collector` family (not Alloy/EDOT/Splunk/Datadog).
- **AC-3** — every enumerated in-repo span producer publishes a runtime-derived effective-config
  artifact naming the Collector's OTLP endpoint.
- **AC-4** — no Collector exporter in `config/otel/` addresses anything off-box.
- **AC-5** — the `es_logger` log path is untouched; existing ES logging tests still pass.

## Current-state findings (verified by reading the repo, not assumed)

- `otel_bootstrap.py` bootstraps a `TracerProvider` with **zero span processors** — its own
  docstring says export is deliberately deferred to this ticket. No spans currently leave the
  process at all.
- `service/app.py:668` is the **only** call site of `configure_tracing()` in `src/` — the sole
  in-repo span producer today. No background entrypoint (`brainstem/scheduler.py`,
  `scripts/monitors/*`) calls it or opens spans, so AC-3's enumeration correctly finds **one**
  producer, not several. Stated plainly in the PR/handoff, not treated as a gap to fill — wiring
  root spans into background entrypoints is ADR-0129 D3's concern, not this ticket's.
- Tempo + Grafana already exist in both compose files (FRE-1072, merged), with `4317`/`4318`
  **host-mapped** on Tempo in `docker-compose.yml` for
  `tests/integration/test_fre1072_tempo_grafana_acceptance.py`'s direct-inject fixtures. That
  suite's docstring explicitly says it does *not* wait on this ticket's Collector — no changes
  needed there, and the Collector must use *different* host ports so it doesn't collide.
- `docker-compose.yml` already carries a comment above the `tempo` block anticipating this ticket —
  reword once the Collector actually attaches.
- No app/gateway container exists in dev `docker-compose.yml` (app runs via `make dev` / `uvicorn`
  on the host) — the OTLP endpoint default must be host-reachable (`localhost:<mapped-port>`),
  matching the existing `elasticsearch_url` default pattern. `docker-compose.cloud.yml`'s
  `seshat-gateway` overrides host-default URLs with compose-internal service names
  (`AGENT_ELASTICSEARCH_URL: http://elasticsearch:9200`) — same pattern applies.
- Verified live (Docker Hub API) that both `otel/opentelemetry-collector:0.158.0` (core) and
  `otel/opentelemetry-collector-contrib:0.158.0` are real, current, multi-arch tags (pushed
  2026-08-04). **Core is used** — see revision note above.

## Design decisions (post-review)

1. **Redaction mechanism: core's `attributes` processor, blocklist mode.** Declares exactly one
   rule: delete the attribute key `fre1070.fixture.blocked` if present. This is a positive,
   narrowly-scoped rule — it never touches any real span attribute this project emits (no
   `personal_agent.*` or `gen_ai.*` key is named), so there is no risk of the allow-list collateral
   damage the original draft would have caused. It is genuinely declarative and auditable (ADR-0129
   D5's stated reason for wanting Collector-side redaction), just narrower in this first cut than a
   full PII-pattern policy — extending the blocklist to real sensitive keys is a natural follow-on,
   not required by this ticket's own AC-1 text (which only requires *a* declared rule with a
   working positive control, not a comprehensive PII policy).
2. **Image: `otel/opentelemetry-collector:0.158.0` (core, not contrib).** Matches ADR-0129 D5's
   "vanilla upstream Collector" wording without needing to lean on the ambiguous "family" reading
   at all — moot now that redaction doesn't require contrib.
3. **AC-1 read-back: `debug` exporter (verbosity `detailed`, core-shipped) → container stdout**,
   read via `docker compose logs --since <injection-time> otel-collector` (not the full unbounded
   log — avoids stale-log false positives per review). The test uses a fresh `uuid4` value for the
   surviving attribute on every run and polls (short retries) for that value to appear before
   asserting, since OTLP export/log flush is not synchronous with the HTTP response.
4. **Effective-config artifact:** `GET /telemetry/effective-config` on the gateway, returning
   `{"service_name", "otlp_endpoint", "otlp_protocol": "grpc", "insecure": true}` computed from
   `settings` at request time. No auth requirement, matching `/health`'s posture.
5. **Ports:** Collector's OTLP receiver host-mapped at `127.0.0.1:4319` (gRPC) /
   `127.0.0.1:4320` (HTTP) in dev compose — loopback-only (tightened per review), and deliberately
   not `4317`/`4318` (Tempo's, owned by FRE-1072). No host ports in `docker-compose.cloud.yml`.
6. **`configure_tracing(otlp_endpoint=...)`:** the parameter stays optional at the function level
   so existing unit tests can construct a provider with no export path, but the **only production
   call site** (`service/app.py`) always passes the real resolved `settings.otel_exporter_endpoint`
   — never `None` outside tests. Docstring states this explicitly.
7. **Provider shutdown:** `app.py`'s lifespan shutdown calls `trace.get_tracer_provider().shutdown()`
   so the `BatchSpanProcessor` flushes on graceful termination rather than relying on process exit.

## Files

1. **`config/otel/collector-config.yaml`** (new)
   ```yaml
   receivers:
     otlp:
       protocols:
         grpc:
           endpoint: 0.0.0.0:4317
         http:
           endpoint: 0.0.0.0:4318

   processors:
     batch: {}
     attributes/redaction:
       actions:
         - key: fre1070.fixture.blocked
           action: delete

   exporters:
     otlp/tempo:
       endpoint: tempo:4317
       tls:
         insecure: true
     debug:
       verbosity: detailed

   service:
     pipelines:
       traces:
         receivers: [otlp]
         processors: [attributes/redaction, batch]
         exporters: [otlp/tempo, debug]
   ```

2. **`docker-compose.yml`** — add, near the existing `tempo`/`grafana` block:
   ```yaml
   otel-collector:
     image: otel/opentelemetry-collector:0.158.0
     command: ["--config=/etc/otel-collector-config.yaml"]
     volumes:
       - ./config/otel/collector-config.yaml:/etc/otel-collector-config.yaml:ro
     ports:
       - "127.0.0.1:4319:4317"   # OTLP gRPC (Tempo keeps 4317/4318 for FRE-1072's direct-inject tests)
       - "127.0.0.1:4320:4318"   # OTLP HTTP — used only by this ticket's acceptance fixture; the
                                  # gateway itself exports over gRPC only
     depends_on:
       tempo:
         condition: service_started
   ```
   Reword the existing anticipatory comment above `tempo:`.

3. **`docker-compose.cloud.yml`** — mirror the service (mem/cpu limits matching Tempo's 512m/0.5,
   no host ports, `cloud-sim` network); add to `seshat-gateway`'s `environment:` block:
   `AGENT_OTEL_EXPORTER_ENDPOINT: otel-collector:4317`; add `otel-collector: condition:
   service_started` to its `depends_on:`.

4. **`src/personal_agent/config/settings.py`** — add near `elasticsearch_url`:
   ```python
   otel_exporter_endpoint: str = Field(
       default="localhost:4319",
       description="OTLP gRPC endpoint of the OTel Collector (ADR-0129 D5, FRE-1070)",
   )
   ```

5. **`src/personal_agent/telemetry/otel_bootstrap.py`** — extend `configure_tracing`:
   ```python
   def configure_tracing(
       service_name: str = "personal-agent", otlp_endpoint: str | None = None
   ) -> TracerProvider:
       """... otlp_endpoint: OTLP gRPC endpoint of the Collector. The production call site
       (service/app.py) always supplies this from settings; None is for tests that need a
       provider with no export path."""
       provider = TracerProvider(resource=Resource.create({"service.name": service_name}))
       trace.set_tracer_provider(provider)
       set_global_textmap(TraceContextTextMapPropagator())
       if otlp_endpoint is not None:
           provider.add_span_processor(
               BatchSpanProcessor(OTLPSpanExporter(endpoint=otlp_endpoint, insecure=True))
           )
       return provider
   ```

6. **`src/personal_agent/service/app.py`**
   - Change the existing call to
     `configure_tracing(service_name=settings.agent_id or "personal-agent", otlp_endpoint=settings.otel_exporter_endpoint)`.
   - Add:
     ```python
     @app.get("/telemetry/effective-config")
     async def telemetry_effective_config() -> dict[str, str | bool]:
         return {
             "service_name": settings.agent_id or "personal-agent",
             "otlp_endpoint": settings.otel_exporter_endpoint,
             "otlp_protocol": "grpc",
             "insecure": True,
         }
     ```
   - In the lifespan shutdown path, add `trace.get_tracer_provider().shutdown()`.

## Tests

- **`tests/personal_agent/telemetry/test_otel_bootstrap.py`** — update
  `test_configure_tracing_installs_no_span_processor` to pass `otlp_endpoint=None` explicitly. Add
  `test_configure_tracing_attaches_otlp_processor_when_endpoint_given`: call with a real endpoint
  string, assert one processor is attached and its exporter is an `OTLPSpanExporter` (construction
  doesn't connect eagerly — no live Collector needed).
- **`tests/personal_agent/service/test_telemetry_effective_config.py`** (new) — `TestClient(app)`,
  `GET /telemetry/effective-config`; parametrize over **two distinct** `otel_exporter_endpoint`
  settings values (dev-default and a cloud-style `otel-collector:4317`) via monkeypatch/settings
  override, asserting the JSON reflects whichever value settings resolved to — proves genuine
  resolution rather than a hardcoded string (addresses the review's "tautological" concern without
  needing a live cloud deployment).
- **`tests/personal_agent/test_otel_collector_config.py`** (new, no live infra needed) — load
  `config/otel/collector-config.yaml`; walk every `exporters.*.endpoint` value, parsing both bare
  `host:port` and URL forms; assert each hostname is `tempo`, `localhost`, or `127.0.0.1`, and fail
  (don't silently skip) on any endpoint shape it can't parse (AC-4).
- **`tests/integration/test_fre1070_otel_collector_acceptance.py`** (new, `pytest.mark.integration`,
  skip-if-unreachable like FRE-1072's suite — run live as part of Step 8/Verify, not relied on for
  CI proof):
  - AC-1: record `time.time()` as `t0`; POST a fixture span to `http://localhost:4320/v1/traces`
    carrying `fre1070.fixture.blocked="should-be-deleted"` and a `fre1070.fixture.survives=<uuid4>`
    control attribute; poll `docker compose logs --since <t0> otel-collector` (subprocess, cwd=repo
    root) for the uuid4 value to appear (bounded retries); once found, assert the same log output
    does not contain `"should-be-deleted"`.
  - AC-2: `docker inspect` the running `otel-collector` container (via `docker compose ps -q
    otel-collector` to resolve the container id first), parse `.Config.Image`, assert the
    repository component is **exactly** `otel/opentelemetry-collector` (not a prefix match, not
    `-contrib`, not any vendor image).

## Verify

1. `make test-file FILE=tests/personal_agent/telemetry/test_otel_bootstrap.py` → pass
2. `make test-file FILE=tests/personal_agent/service/test_telemetry_effective_config.py` → pass
3. `make test-file FILE=tests/personal_agent/test_otel_collector_config.py` → pass
4. `make test-file FILE=tests/test_telemetry/test_es_logger.py` and
   `tests/test_telemetry/test_es_logger_redaction.py` → pass unchanged (AC-5)
5. `docker compose up -d tempo otel-collector` then
   `PERSONAL_AGENT_INTEGRATION=1 pytest -m integration tests/integration/test_fre1070_otel_collector_acceptance.py -v`
   → pass (AC-1, AC-2), with actual observed output recorded in the ticket handoff comment
6. `make mypy` / `make ruff-check` / `make ruff-format` / `pre-commit run --all-files`
