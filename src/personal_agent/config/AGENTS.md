# Configuration Management

Unified configuration management for the Personal Agent.

**Spec**: `../../docs/architecture_decisions/ADR-0007-unified-configuration-management.md`

## Responsibilities

- Load and validate all configuration from multiple sources (env vars, YAML files, defaults)
- Provide type-safe, validated access to all configuration values
- Integrate with existing config loaders (governance, models)
- Support environment-specific configuration (development, staging, production, test)

## Structure

```
config/
├── __init__.py              # Exports: settings, load_governance_config, load_model_config
├── settings.py              # AppConfig class, environment detection
├── env_loader.py            # .env file loading logic
├── validators.py            # Custom Pydantic validators
├── loader.py                # Shared YAML loading utilities
├── governance_loader.py      # Governance config loading + validation
└── model_loader.py          # Model config loading + validation
```

**Key Principle**: All configuration file loading happens in the `config/` module. Domain-specific Pydantic models (e.g., `GovernanceConfig`, `ModelConfig`) remain in domain modules, but their loaders live in `config/`.

## Usage

### Basic Access

```python
from personal_agent.config import settings

# Access any configuration value (type-safe, validated)
log_level = settings.log_level
base_url = settings.llm_base_url
timeout = settings.llm_timeout_seconds
max_tasks = settings.orchestrator_max_concurrent_tasks
poll_interval = settings.brainstem_sensor_poll_interval_seconds
```

### Configuration Sources and Precedence

1. **Environment variables** (highest priority) - from `.env` files or system env
2. **YAML configuration files** - `config/governance/*.yaml`, `config/models.yaml`
3. **Default values** - hardcoded in `AppConfig` model

**Precedence rule**: Environment variables > YAML files > Defaults

### Environment-Specific .env Files

The system loads environment-specific `.env` files in priority order:

1. `.env.{environment}.local` (highest priority, gitignored)
2. `.env.{environment}` (environment-specific)
3. `.env.local` (local overrides, gitignored)
4. `.env` (base configuration)

Environment is detected from `APP_ENV`:
- `production` or `prod` → `Environment.PRODUCTION`
- `staging` or `stage` → `Environment.STAGING`
- `test` → `Environment.TEST`
- Default → `Environment.DEVELOPMENT`

### Configuration Loaders

**All configuration loaders live in the `config/` module** (per ADR-0007 consolidation).

```python
from personal_agent.config import settings, load_governance_config, load_model_config

# App-level settings
log_level = settings.log_level
timeout = settings.llm_timeout_seconds

# Domain configs (paths come from settings automatically)
governance_config = load_governance_config()  # Uses settings.governance_config_path
model_config = load_model_config()            # Uses settings.model_config_path

# Components use both
mode_manager = ModeManager(
    governance_config=governance_config,
    poll_interval=settings.brainstem_sensor_poll_interval_seconds,
)
```

**Note**: The old import paths (`from personal_agent.governance import load_governance_config`) still work but are deprecated and will be removed in v0.2.0.

## Configuration Fields

The `AppConfig` class provides these configuration groups:

### Environment
- `environment: Environment` - Current environment (development, staging, production, test)
- `debug: bool` - Debug mode flag

### Application
- `project_name: str` - Project name
- `version: str` - Application version

### Telemetry
- `log_dir: Path` - Log directory path
- `log_level: str` - Logging level (DEBUG, INFO, WARNING, ERROR, CRITICAL)
- `log_format: Literal["json", "console"]` - Log format

### LLM Client
- `llm_base_url: str` - Base URL for LLM API
- `llm_timeout_seconds: int` - Request timeout
- `llm_max_retries: int` - Maximum retry attempts

### Orchestrator
- `orchestrator_max_concurrent_tasks: int` - Maximum concurrent tasks
- `orchestrator_task_timeout_seconds: int` - Task timeout

### Brainstem
- `brainstem_sensor_poll_interval_seconds: float` - Sensor polling interval

### Paths (for domain config loaders)
- `governance_config_path: Path` - Path to governance config directory
- `model_config_path: Path` - Path to model config file

## Environment Variable Naming

Environment variables use `UPPER_SNAKE_CASE` with `APP_` prefix for app-level settings:

- `APP_ENV` → `environment`
- `APP_DEBUG` → `debug`
- `APP_LOG_LEVEL` → `log_level`
- `LLM_BASE_URL` → `llm_base_url`

Pydantic `BaseSettings` automatically maps environment variable names to field names.

## Testing

### Dependency Injection Pattern

Components should accept `AppConfig` as a parameter (default to singleton):

```python
class LLMClient:
    def __init__(self, config: AppConfig | None = None):
        self.config = config or settings
        self.base_url = self.config.llm_base_url
        self.timeout = self.config.llm_timeout_seconds
```

### Override for Tests

```python
from personal_agent.config import AppConfig

# Create test configuration
test_config = AppConfig(
    llm_base_url="http://test-server:1234/v1",
    llm_timeout_seconds=5,
    debug=True,
)

# Pass to component
client = LLMClient(config=test_config)
```

### Context Manager for Temporary Overrides

```python
from personal_agent.config import override_settings

with override_settings(llm_base_url="http://test-server:1234/v1"):
    # Use settings here - temporarily overridden
    client = LLMClient()
    # settings.llm_base_url is "http://test-server:1234/v1"
# Original settings restored after context
```

## Dependencies

- **Pydantic**: Configuration validation and type safety
- **pydantic-settings**: Environment variable integration
- **python-dotenv**: `.env` file loading
- **telemetry**: Structured logging for config loading

## Search

```bash
rg -n "from personal_agent.config import" src/  # Find config usage
rg -n "settings\." src/                         # Find config access
rg -n "os\.getenv\|os\.environ" src/            # Find direct env access (should be none)
```

## Critical Rules

- **Never use `os.getenv()` or `os.environ`** - always use `settings`
- **All configuration must be in `AppConfig`** - no scattered config access
- **Type-safe access only** - Pydantic validates all values at startup
- **Fail fast** - Invalid configuration prevents startup
- **Secrets in .env files only** - never in code or YAML files
- **Use dependency injection** - Accept `AppConfig` parameter for testability

## Common Patterns

### Component Initialization

```python
from personal_agent.config import settings
from personal_agent.telemetry import get_logger

class MyComponent:
    def __init__(self, config: AppConfig | None = None):
        self.config = config or settings
        self.log = get_logger(__name__)
        self.timeout = self.config.llm_timeout_seconds
```

### Accessing Configuration in Functions

```python
from personal_agent.config import settings

def do_something():
    # Use settings directly (singleton pattern)
    timeout = settings.llm_timeout_seconds
    base_url = settings.llm_base_url
    # ...
```

### Integration with Domain Configs

```python
from personal_agent.config import settings, load_governance_config, load_model_config

# Load domain configs (paths come from settings automatically)
governance_config = load_governance_config()  # Uses settings.governance_config_path
model_config = load_model_config()            # Uses settings.model_config_path

# Use both app settings and domain configs
manager = ModeManager(
    governance_config=governance_config,
    poll_interval=settings.brainstem_sensor_poll_interval_seconds,
)

# Model config is now typed (ModelConfig, not dict)
router_model = model_config.models["router"]
print(router_model.id)  # Type-safe access
```

## Never

- ❌ `os.getenv("LOG_LEVEL")` - use `settings.log_level`
- ❌ `os.environ["LOG_LEVEL"]` - use `settings.log_level`
- ❌ Hardcode configuration values - use `settings` or defaults in `AppConfig`
- ❌ Access environment variables directly - always go through `settings`
- ❌ Create separate config objects - use the unified `AppConfig` only

## Before Implementing

1. Read `../../docs/architecture_decisions/ADR-0007-unified-configuration-management.md`
2. Check if configuration field already exists in `AppConfig`
3. If adding new field, update `AppConfig` in `config/settings.py`
4. Add Pydantic validation rules if needed
5. Update tests to verify new configuration field
