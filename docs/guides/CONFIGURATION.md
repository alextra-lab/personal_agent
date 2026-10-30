# Configuration Guide

This document explains how to configure the Personal Agent. Each fact names the file that states it. The settings class is `AppConfig` in `src/personal_agent/config/settings.py`. In this guide, "settings.py" means that file.

## Quick Start

1. Copy `.env.example` to `.env`. Source: `.env.example`, `src/personal_agent/config/env_loader.py`.
2. Set `AGENT_SLM_BASE_URL` to the origin of your llama.cpp server. Use an origin with no `/v1` suffix. Source: `settings.py`, field `slm_base_url`.
3. Add the API keys for the cloud models that you use. See "Secrets".
4. Start the service. Source: `Makefile`, target `dev`.

The field has no default. If the deployment profile is `cloud` or `eval` and `AGENT_SLM_BASE_URL` is not set, the service does not start. The error message names the setting. In the `local` profile, the start succeeds, and the first call that needs the local model server raises an error. Source: `settings.py`, field `slm_base_url`, function `enforce_slm_endpoint_declared` and property `resolved_slm_base_url`.

## Setting names

Every setting is read from an environment variable with the prefix `AGENT_`. The name is the field name in upper case. For example, the field `llm_timeout_seconds` is read from `AGENT_LLM_TIMEOUT_SECONDS`. Source: `settings.py`, `SettingsConfigDict(env_prefix="AGENT_")`.

A name without the prefix is ignored. The exceptions are the fields that declare an alias. Three examples:

| Variable | Field | Default | Source |
|---|---|---|---|
| `APP_DEBUG` | `debug` | `false` | `settings.py`, field `debug` |
| `APP_LOG_LEVEL` | `log_level` | `INFO` | `settings.py`, field `log_level` |
| `APP_LOG_FORMAT` | `log_format` | `json` | `settings.py`, field `log_format` |

Other fields that declare an alias are listed in `settings.py`. Search for `alias=`.

The variable `APP_ENV` selects the environment. It is the only variable that the environment loader reads directly. Source: `src/personal_agent/config/env_loader.py`, function `get_environment`.

| `APP_ENV` value | Environment |
|---|---|
| `production` or `prod` | production |
| `staging` or `stage` | staging |
| `test` | test |
| any other value, or not set | development |

## Configuration Files

### Environment files (`.env`)

**Location**: the project root.

- **`.env`** - Your local configuration. Git ignores it. It can hold secrets.
- **`.env.example`** - The template. It lists the settings with comments. Git tracks it.

**Priority order** (highest first). Source: `env_loader.py`, function `load_env_files`.
1. `.env.{environment}.local`
2. `.env.{environment}`
3. `.env.local`
4. `.env`

A variable that is already set in the process environment wins over every file. Source: `env_loader.py` (the loader calls `load_dotenv` with `override=False`).

### YAML files

**Location**: the `config/` directory. Git tracks these files.

| File | Content | Source |
|---|---|---|
| `config/models.yaml` | The model catalog. Each key is a deployment. | `src/personal_agent/config/model_loader.py` |
| `config/model_roles.yaml` | The role bindings. Each role names one deployment. | `config/model_roles.yaml` |
| `config/governance/modes.yaml` | The operating modes and the transition rules. | `src/personal_agent/config/governance_loader.py` |
| `config/governance/tools.yaml` | The tool policies. | `src/personal_agent/config/governance_loader.py` |
| `config/governance/permissions.yaml` | The permission rules. | `settings.py`, field `extra_permission_rules_path` |
| `config/governance/budget.yaml.example` | The template for the budget file. | `config/governance/` |

The path to the governance directory is the field `governance_config_path`. The default is `config/governance`. Source: `settings.py`, field `governance_config_path`.

A role is a job, such as `primary` or `entity_extraction`. To change the model for a role, edit the binding in `config/model_roles.yaml`. Do not add a `*_role` key to `models.yaml`.

## What You Must Configure

### Local model server

```bash
# In .env
AGENT_SLM_BASE_URL=<origin of the llama.cpp server>
```

- The local model runs on llama.cpp. Source: `config/models.yaml`, provider `slm_local`.
- The value is an origin: scheme, host and optional port. Do not add `/v1`. Callers add their own path. Source: `settings.py`, field `slm_base_url`.
- The cloud compose file uses the internal Caddy egress block. Source: `.env.example`, the comment above `AGENT_SLM_BASE_URL`.

### Secrets

Add API keys to `.env` only. Never commit them.

```bash
# In .env
AGENT_OPENAI_API_KEY=...
AGENT_ANTHROPIC_API_KEY=...
AGENT_VOYAGE_API_KEY=...
```

Source: `settings.py`, fields `openai_api_key`, `anthropic_api_key` and `voyage_api_key`. A key that a deployment needs is listed under `deployment_profile` in `config/model_roles.yaml`.

Some secrets are not read by `AppConfig`. The compose files and `config/gateway_access.yaml` read them without the `AGENT_` prefix. Examples are `CLOUDFLARE_TUNNEL_TOKEN` and `GATEWAY_TOKEN_PWA`. Source: `docker-compose.cloud.yml`, `config/gateway_access.yaml`, `.env.example`.

## Common Settings

### Logging

```bash
# In .env
APP_LOG_FORMAT=console  # Default: json
APP_LOG_LEVEL=DEBUG     # Default: INFO
```

The log directory is `AGENT_LOG_DIR`. The default is `telemetry/logs`. Source: `settings.py`, fields `log_dir`, `log_level` and `log_format`.

### Model calls

```bash
# In .env
AGENT_LLM_TIMEOUT_SECONDS=180  # Default: 120
AGENT_LLM_MAX_RETRIES=5        # Default: 3
```

Source: `settings.py`, fields `llm_timeout_seconds` and `llm_max_retries`.

### Orchestrator limits

```bash
# In .env
AGENT_ORCHESTRATOR_TASK_TIMEOUT_SECONDS=600  # Default: 900
AGENT_ORCHESTRATOR_MAX_TOOL_ITERATIONS=10    # Default: 25
```

The first value is the wall-clock limit for one turn. The second value is the maximum number of tool iterations for one request. Source: `settings.py`, fields `orchestrator_task_timeout_seconds` and `orchestrator_max_tool_iterations`.

### MCP gateway

```bash
# In .env
AGENT_MCP_GATEWAY_ENABLED=true        # Default: false
AGENT_MCP_GATEWAY_TIMEOUT_SECONDS=90  # Default: 60, range 1 to 300
```

The command that starts the gateway is `AGENT_MCP_GATEWAY_COMMAND`. The default is `docker mcp gateway run`. Source: `settings.py`, fields `mcp_gateway_enabled`, `mcp_gateway_command` and `mcp_gateway_timeout_seconds`. Tier 3 (MCP) tools need an ADR. See `docs/reference/TOOL_INTEGRATION_GUIDE.md`.

### Request monitoring

```bash
# In .env
AGENT_REQUEST_MONITORING_ENABLED=true  # Default: true
```

Source: `settings.py`, field `request_monitoring_enabled`. ADR-0012 describes the feature.

### Service port

The field `service_port` has the default `9000`. This is the port of `make dev` on a local machine. The cloud gateway listens on `9001`. Source: `settings.py`, field `service_port`, `Dockerfile.gateway`, `docker-compose.cloud.yml`.

## Configuration Reference

### All available settings

`.env.example` lists the settings with comments. `settings.py` is the source of truth for the names, types and defaults. The reference table of every field is in `docs/reference/CONFIG_INVENTORY.md`.

### Validation

Pydantic validates all values when the service starts. Source: `settings.py`.

- **Type checking** - A value must match the field type.
- **Range checking** - A numeric field can have limits, such as `ge=1`.
- **Enum checking** - The log level, the log format and the environment must be valid options.

A configuration that is not valid stops the start. This is intended (fail-fast).

## Environment-Specific Configuration

1. Set the environment:
   ```bash
   export APP_ENV=production
   ```
2. Create the environment file:
   ```bash
   cp .env .env.production
   ```
3. Edit `.env.production`. The loader reads it when `APP_ENV=production`. Source: `env_loader.py`.

## Troubleshooting

### The configuration does not load

Read the log output. These events show the loading steps:

```json
{"event": "loading_app_config", "environment": "development"}
{"event": "env_files_loaded", "files": [".env"], "environment": "development"}
{"event": "app_config_loaded", "environment": "development", "log_level": "INFO"}
```

### Validation errors

If the service reports a validation error at start:

1. Read the error message. It names the invalid field.
2. Check the value in your `.env` file.
3. Check `.env.example` for the valid values.
4. Read ADR-0007 for the validation rules.

If the message says that `AGENT_SLM_BASE_URL` is not set, set it. See "Local model server". Source: `settings.py`, function `enforce_slm_endpoint_declared`.

### Direct environment variable access

Do not use `os.getenv()` or `os.environ` in code. Use `settings`.

```python
# Wrong
import os
log_level = os.getenv("APP_LOG_LEVEL")

# Correct
from personal_agent.config import settings
log_level = settings.log_level
```

`env_loader.py` is the exception. It reads `APP_ENV` before the settings exist.

## Error Pattern Monitoring (ADR-0056)

`AGENT_FAILURE_PATH_REFLECTION_ENABLED` controls Phase 2 of ADR-0056, which extends Captain's Log reflection with GEPA-inspired failure-path analysis. It is on by default (`true`, FRE-1608). With the flag off, every reflection reads `had_errors=False`, even for a turn that holds `tool_call_failed`, `model_call_error` or `planner_failed` events. Set the flag to `false` in `.env` only as a kill switch.

## Context Quality Stream (ADR-0059)

`AGENT_CONTEXT_QUALITY_STREAM_ENABLED` (default `true`) enables Stream 7 — per-incident detection when Stage 7 compaction drops an entity that the user then references in the same session. Each incident dual-writes a `CQ-*.jsonl` line and publishes a `CompactionQualityIncidentEvent` → Captain's Log entry (`category=KNOWLEDGE_QUALITY, scope=ORCHESTRATOR`).

`AGENT_CONTEXT_QUALITY_GOVERNANCE_ENABLED` (default `false`) enables Phase 2 budget tightening. Flip after 14 days of Phase 1 telemetry validates signal quality.

## Knowledge Graph Quality Stream (ADR-0060)

Four flags ship with Phase 1 (all enabled by default except governance):

| Flag | Default | Description |
|------|---------|-------------|
| `AGENT_GRAPH_QUALITY_STREAM_ENABLED` | `true` | Enable daily anomaly events (Stream 8) and weekly freshness review events (Stream 6). Each anomaly dual-writes `telemetry/graph_quality/GQ-*.jsonl` + publishes `GraphQualityAnomalyEvent` → `cg:graph-monitor` → Captain's Log. |
| `AGENT_FRESHNESS_TIER_RERANKING_ENABLED` | `true` | Apply `StalenessTier` multiplier in recall reranking (`_calculate_relevance_scores`). DORMANT entities contribute 30 % of their freshness weight; WARM unchanged. |
| `AGENT_FRESHNESS_TIER_FACTORS` | `{"warm":1.0,"cooling":0.85,"cold":0.60,"dormant":0.30}` | JSON dict of per-tier multipliers. Adjust `dormant` upward if DORMANT entities are over-suppressed after 30 days of data. |
| `AGENT_GRAPH_QUALITY_GOVERNANCE_ENABLED` | `false` | **Phase 2 flip gate.** Enable only after 14 days of Phase 1 data confirms false-positive rate < 20 % in Linear "Knowledge Graph Quality" project. When enabled, high-severity anomalies publish `ModeAdvisoryEvent(DEGRADED, "consolidation")` to the mode controller. |

**Flip procedure for governance:** verify `XLEN stream:graph.quality_anomaly ≥ 1` in Redis, confirm `telemetry/graph_quality/GQ-*.jsonl` files are growing daily, and check the Linear project false-positive rate before setting `AGENT_GRAPH_QUALITY_GOVERNANCE_ENABLED=true` and restarting `seshat-gateway`.

Source for the three sections above: `settings.py`, fields `context_quality_stream_enabled`, `context_quality_governance_enabled`, `graph_quality_stream_enabled`, `freshness_tier_reranking_enabled`, `freshness_tier_factors` and `graph_quality_governance_enabled`.

## Architecture

ADR-0007 defines configuration management.

- **Single source of truth** - All configuration goes through `personal_agent.config.settings`.
- **Type safety** - Pydantic validation finds errors at start.
- **Clear precedence** - Environment variables > YAML files > defaults.
- **Security** - Secrets stay in `.env` files that Git ignores. Secrets never enter code.
- **Fail fast** - A configuration that is not valid stops the start.

## References

- **ADR-0007**: `docs/architecture_decisions/ADR-0007-unified-configuration-management.md`
- **Config module**: `src/personal_agent/config/AGENTS.md`
- **Template**: `.env.example`
- **Code**: `src/personal_agent/config/settings.py`
- **Field reference**: `docs/reference/CONFIG_INVENTORY.md`
