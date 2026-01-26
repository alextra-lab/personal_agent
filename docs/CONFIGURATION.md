# Configuration Guide

This document explains how to configure the Personal Agent for your local environment.

## Quick Start

The agent is pre-configured with sensible defaults. To get started:

1. **Review `.env` file** - A minimal configuration file has been created with defaults
2. **Update LLM endpoint** (if needed) - Edit `LLM_BASE_URL` in `.env` if not using LM Studio on default port
3. **Run the agent** - The system will work with defaults for everything else

## Configuration Files

### Environment Variables (`.env` files)

**Location**: Project root

- **`.env`** - Your local configuration (gitignored, safe for secrets)
- **`.env.example`** - Template showing all available options (tracked in git)

**Priority order** (highest to lowest):
1. `.env.{environment}.local` (e.g., `.env.development.local`) - gitignored
2. `.env.{environment}` (e.g., `.env.production`) - can be tracked
3. `.env.local` - gitignored
4. `.env` - your main local config (gitignored)

The environment is set via `APP_ENV` environment variable (defaults to `development`).

### YAML Configuration Files

**Location**: `config/` directory

- **`config/models.yaml`** - LLM model configurations (router, planner, executor models)
- **`config/governance/`** - Governance policies (modes, safety, tools, models)
  - `modes.yaml` - Operational mode thresholds (NORMAL, ALERT, DEGRADED, etc.)
  - `safety.yaml` - Safety policies and constraints
  - `tools.yaml` - Tool permissions and allowlists
  - `models.yaml` - Model-specific governance rules

These files are tracked in git and define the agent's behavior.

## What You Should Configure

### Essential (Required)

**LLM Endpoint** - Update if not using LM Studio on default port:

```bash
# In .env file
LLM_BASE_URL=http://localhost:1234/v1
```

Common alternatives:
- **LM Studio**: `http://localhost:1234/v1` (default)
- **Ollama**: `http://localhost:11434/v1`
- **OpenAI**: `https://api.openai.com/v1` (requires API key)

### Recommended (Optional)

**Log Format** - Use console format for easier reading during development:

```bash
# In .env file
APP_LOG_FORMAT=console  # Default: json (structured)
```

**Log Level** - Increase verbosity for debugging:

```bash
# In .env file
APP_LOG_LEVEL=DEBUG  # Default: INFO
```

### Advanced (Optional)

**LLM Timeouts** - Adjust if requests are timing out:

```bash
# In .env file
LLM_TIMEOUT_SECONDS=180  # Default: 120 (2 minutes)
LLM_MAX_RETRIES=5        # Default: 3
```

**Orchestrator Limits** - Control task execution:

```bash
# In .env file
ORCHESTRATOR_MAX_CONCURRENT_TASKS=10  # Default: 5
ORCHESTRATOR_TASK_TIMEOUT_SECONDS=600 # Default: 300 (5 minutes)
ORCHESTRATOR_MAX_TOOL_ITERATIONS=5    # Default: 3 (prevents loops)
```

**MCP Gateway** - Enable Model Context Protocol integration:

```bash
# In .env file
MCP_GATEWAY_ENABLED=true              # Default: false
MCP_GATEWAY_TIMEOUT_SECONDS=60        # Default: 30
```

**Request Monitoring** - Control system metrics collection:

```bash
# In .env file
REQUEST_MONITORING_ENABLED=true       # Default: true
REQUEST_MONITORING_INCLUDE_GPU=true   # Default: true (Apple Silicon)
```

### Secrets (Never Commit)

Add API keys to `.env` file only (never commit):

```bash
# In .env file
OPENAI_API_KEY=sk-...
ANTHROPIC_API_KEY=sk-ant-...
```

## Configuration Reference

### All Available Settings

See `.env.example` for complete list with descriptions. Key categories:

1. **Environment** - `APP_ENV`, `APP_DEBUG`
2. **Application** - `PROJECT_NAME`, `VERSION`
3. **Telemetry** - `LOG_DIR`, `APP_LOG_LEVEL`, `APP_LOG_FORMAT`
4. **LLM Client** - `LLM_BASE_URL`, `LLM_TIMEOUT_SECONDS`, `LLM_MAX_RETRIES`
5. **Orchestrator** - `ORCHESTRATOR_MAX_CONCURRENT_TASKS`, `ORCHESTRATOR_TASK_TIMEOUT_SECONDS`
6. **Brainstem** - `BRAINSTEM_SENSOR_POLL_INTERVAL_SECONDS`
7. **Request Monitoring** - `REQUEST_MONITORING_ENABLED`, `REQUEST_MONITORING_INTERVAL_SECONDS`
8. **MCP Gateway** - `MCP_GATEWAY_ENABLED`, `MCP_GATEWAY_COMMAND`
9. **Paths** - `GOVERNANCE_CONFIG_PATH`, `MODEL_CONFIG_PATH`

### Validation

All configuration values are validated at startup using Pydantic:

- **Type checking** - Values must match expected types (int, bool, string, etc.)
- **Range validation** - Numeric values must be within allowed ranges
- **Enum validation** - Log levels, formats, environments must be valid options
- **Path validation** - Paths are resolved to absolute paths

**Invalid configuration will prevent startup** - this is intentional (fail-fast principle).

## Environment-Specific Configuration

For different environments (development, staging, production):

1. **Set environment**:
   ```bash
   export APP_ENV=production
   ```

2. **Create environment-specific file**:
   ```bash
   cp .env .env.production
   # Edit .env.production with production settings
   ```

3. **Run agent** - It will automatically load `.env.production` when `APP_ENV=production`

## Troubleshooting

### Configuration Not Loading

Check log output for configuration loading messages:

```json
{"event": "loading_app_config", "environment": "development"}
{"event": "env_files_loaded", "files": [".env"], "environment": "development"}
{"event": "app_config_loaded", "environment": "development", "log_level": "INFO"}
```

### Validation Errors

If you see validation errors at startup:

1. Check the error message for which field is invalid
2. Verify the value in your `.env` file
3. Consult `.env.example` for valid values and formats
4. Check ADR-0007 for detailed validation rules

### Direct Environment Variable Access

**Never use `os.getenv()` or `os.environ` in code** - always use `settings`:

```python
# ❌ Wrong
import os
log_level = os.getenv("LOG_LEVEL")

# ✅ Correct
from personal_agent.config import settings
log_level = settings.log_level
```

## Architecture

Configuration management follows **ADR-0007: Unified Configuration Management**.

Key principles:

- **Single source of truth** - All config through `personal_agent.config.settings`
- **Type safety** - Pydantic validation catches errors at startup
- **Clear precedence** - Environment variables > YAML files > Defaults
- **Security** - Secrets in `.env` files (gitignored), never in code
- **Fail fast** - Invalid config prevents startup

## References

- **ADR-0007**: `architecture_decisions/ADR-0007-unified-configuration-management.md`
- **Config Module**: `src/personal_agent/config/AGENTS.md`
- **Template**: `.env.example`
- **Code**: `src/personal_agent/config/settings.py`
