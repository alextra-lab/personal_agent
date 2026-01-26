# ADR-0007: Unified Configuration Management

**Status:** Accepted (Updated 2025-01-01)
**Date:** 2025-12-29
**Last Updated:** 2025-01-01
**Decision Owner:** Project Owner

---

## 1. Context

The Personal Local AI Collaborator needs to manage configuration from multiple sources:

1. **Environment variables** (`.env` files) for secrets, API keys, and environment-specific settings
2. **YAML configuration files** for structured policies (governance configs, model configs)
3. **Application settings** for runtime parameters (log levels, paths, timeouts)
4. **Defaults** for safe fallback values

Currently, configuration management is fragmented:

- ADR-0005 defines governance configuration loading (YAML files only)
- ADR-0003 mentions model configuration but no unified loader
- No established pattern for environment variables
- No single entry point for accessing all configuration

This fragmentation creates several problems:

- **Inconsistent access patterns**: Different components use different methods to get config
- **No precedence rules**: Unclear whether env vars override YAML files or vice versa
- **Validation gaps**: Some configs validated (governance YAML), others not (env vars)
- **Security risks**: Secrets may be accessed inconsistently, increasing risk of exposure
- **Testing difficulties**: Hard to mock or override configuration in tests

Multiple components need configuration:

- **Telemetry**: Log directories, log levels, log formats
- **Governance**: Mode thresholds, tool permissions (already defined in ADR-0005)
- **LLM Client**: Model endpoints, API keys, timeouts, retry settings
- **Orchestrator**: Concurrency limits, task timeouts
- **Tools**: Filesystem paths, command allowlists
- **Brainstem**: Sensor polling intervals, threshold values
- **UI**: CLI output formats, approval timeouts

The system must:

- Provide a **single source of truth** for all configuration
- Support **type-safe access** with Pydantic validation
- Establish **clear precedence** (env vars → YAML → defaults)
- **Integrate seamlessly** with existing governance config loader
- **Log configuration loading** using structlog (not print statements)
- **Fail fast** if critical configuration is invalid or missing
- **Support testing** via dependency injection and overrides

---

## 2. Decision

### 2.1 Single Configuration Manager

We establish a **unified `AppConfig` class** (Pydantic-based) that:

- Loads and validates all configuration from multiple sources
- Provides a single import point: `from personal_agent.config import settings`
- Integrates with existing governance config loader (does not replace it)
- Uses Pydantic for validation and type safety

### 2.2 Configuration Sources and Precedence

Configuration is loaded in this order (later sources override earlier ones):

1. **Default values** (hardcoded in `AppConfig` model)
2. **YAML configuration files** (`config/governance/*.yaml`, `config/models.yaml`)
3. **Environment variables** (from `.env` files or system environment)
4. **Runtime overrides** (for testing only, via dependency injection)

**Precedence rule**: Environment variables > YAML files > Defaults

This allows:

- **YAML files** for structured, versioned configuration (governance policies, model configs)
- **Environment variables** for secrets and environment-specific overrides
- **Defaults** for safe fallback values that always work

### 2.3 Configuration Structure

The unified `AppConfig` class organizes configuration into logical groups:

```python
class AppConfig(BaseSettings):
    """Unified application configuration.

    Loads configuration from environment variables, YAML files, and defaults.
    Validates all values using Pydantic.
    """

    # Environment
    environment: Environment = Field(default=Environment.DEVELOPMENT)
    debug: bool = Field(default=False)

    # Application
    project_name: str = Field(default="Personal Local AI Collaborator")
    version: str = Field(default="0.1.0")

    # Telemetry
    log_dir: Path = Field(default=Path("telemetry/logs"))
    log_level: str = Field(default="INFO")
    log_format: Literal["json", "console"] = Field(default="json")

    # LLM Client
    llm_base_url: str = Field(default="http://localhost:1234/v1")
    llm_timeout_seconds: int = Field(default=120)
    llm_max_retries: int = Field(default=3)

    # Orchestrator
    orchestrator_max_concurrent_tasks: int = Field(default=5)
    orchestrator_task_timeout_seconds: int = Field(default=300)

    # Brainstem
    brainstem_sensor_poll_interval_seconds: float = Field(default=5.0)

    # Governance (delegates to governance config loader)
    governance_config_path: Path = Field(default=Path("config/governance"))

    # Model config (delegates to model config loader)
    model_config_path: Path = Field(default=Path("config/models.yaml"))
```

### 2.4 Environment Variable Loading

Environment-specific `.env` files are loaded with priority order:

1. `.env.{environment}.local` (highest priority, gitignored)
2. `.env.{environment}` (environment-specific)
3. `.env.local` (local overrides, gitignored)
4. `.env` (base configuration)

Environment is detected from `APP_ENV` environment variable:

- `production` or `prod` → `Environment.PRODUCTION`
- `staging` or `stage` → `Environment.STAGING`
- `test` → `Environment.TEST`
- Default → `Environment.DEVELOPMENT`

**Security**: Secrets must be in `.env` files (gitignored), never committed.

### 2.5 Consolidated Configuration Management

**All configuration loaders live in the `config/` module** - this is the single source of truth for configuration management.

The `config/` module manages:
- **App-level settings** (`AppConfig`): Environment variables, timeouts, paths, log settings
- **Domain configuration loaders**: Governance config, model config, and future domain configs

**Architecture Pattern**:
- `config/settings.py`: `AppConfig` for app-level settings
- `config/loader.py`: Shared YAML loading utilities (DRY principle)
- `config/governance_loader.py`: Loads and validates governance config
- `config/model_loader.py`: Loads and validates model config
- Domain Pydantic models stay in domain modules (e.g., `governance/models.py`)

**Single Import Point**:
```python
from personal_agent.config import (
    settings,                    # AppConfig singleton
    load_governance_config,      # Governance config loader
    load_model_config,           # Model config loader
)
```

**Usage Pattern**:
```python
from personal_agent.config import settings, load_governance_config

# App-level settings
log_level = settings.log_level
timeout = settings.llm_timeout_seconds

# Domain configs (paths come from settings)
governance_config = load_governance_config()  # Uses settings.governance_config_path
model_config = load_model_config()            # Uses settings.model_config_path

# Components use both
mode_manager = ModeManager(
    governance_config=governance_config,
    poll_interval=settings.brainstem_sensor_poll_interval_seconds,
)
```

**Benefits of Consolidation**:
- ✅ **Single source of truth**: All config management in one module
- ✅ **Consistency**: All loaders follow same patterns, return typed Pydantic models
- ✅ **DRY**: Shared YAML loading utilities eliminate code duplication
- ✅ **Discoverability**: Developers know exactly where to find config code
- ✅ **Maintainability**: Changes to config loading logic happen in one place
- ✅ **Type safety**: All configs validated with Pydantic models (not raw dicts)

### 2.6 Implementation Details

#### Module Structure

```
src/personal_agent/
├── config/
│   ├── __init__.py              # Exports: settings, load_governance_config, load_model_config
│   ├── settings.py              # AppConfig class, environment detection
│   ├── env_loader.py            # .env file loading logic
│   ├── validators.py            # Custom Pydantic validators
│   ├── loader.py                # Shared YAML loading utilities
│   ├── governance_loader.py     # Governance config loading + validation
│   └── model_loader.py          # Model config loading + validation
```

**Key Principle**: All configuration file loading happens in the `config/` module. Domain-specific Pydantic models (e.g., `GovernanceConfig`) remain in domain modules, but their loaders live in `config/`.

#### Pydantic BaseSettings

Use Pydantic `BaseSettings` for environment variable integration:

```python
from pydantic import BaseSettings, Field
from pydantic_settings import SettingsConfigDict

class AppConfig(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",  # Ignore extra env vars
    )

    # Configuration fields with defaults and validation
    log_level: str = Field(default="INFO", pattern="^(DEBUG|INFO|WARNING|ERROR|CRITICAL)$")
    llm_timeout_seconds: int = Field(default=120, gt=0, le=600)
```

#### Environment Variable Naming

Use `UPPER_SNAKE_CASE` with `APP_` prefix for app-level settings:

- `APP_ENV` → `environment`
- `APP_DEBUG` → `debug`
- `APP_LOG_LEVEL` → `log_level`
- `LLM_BASE_URL` → `llm_base_url`

Pydantic `BaseSettings` automatically maps `APP_LOG_LEVEL` → `app_log_level` → `log_level`.

#### Structured Logging

Use structlog (never print) for configuration loading:

```python
from personal_agent.telemetry import get_logger

log = get_logger(__name__)

def load_app_config() -> AppConfig:
    """Load and validate application configuration."""
    log.info("loading_config", environment=get_environment().value)

    try:
        config = AppConfig()
        log.info("config_loaded", environment=config.environment.value, debug=config.debug)
        return config
    except ValidationError as e:
        log.error("config_validation_failed", errors=e.errors())
        raise ConfigError("Invalid configuration") from e
```

#### Singleton Pattern

Create a module-level singleton instance:

```python
# src/personal_agent/config/__init__.py
from .settings import AppConfig, load_app_config

_settings: AppConfig | None = None

def get_settings() -> AppConfig:
    """Get the application settings singleton."""
    global _settings
    if _settings is None:
        _settings = load_app_config()
    return _settings

# Convenience export
settings = get_settings()
```

**Testing**: Allow overriding via dependency injection (pass `AppConfig` instance to components).

### 2.7 Error Handling

Configuration loading must **fail fast** on critical errors:

- **Missing required config**: Raise `ConfigError` if critical values are missing
- **Invalid values**: Pydantic validation errors are raised immediately
- **File not found**: Log warning, use defaults if non-critical; raise if critical

Non-critical configs (e.g., log format) may have safe defaults and log warnings.

---

## 3. Implementation Plan

### Phase 1: Core Configuration Manager (Week 1)

1. **Add dependencies**:
   - Add `pydantic-settings>=2.0.0` to `pyproject.toml`
   - Add `python-dotenv>=1.0.0` to `pyproject.toml`

2. **Create config module structure**:

   ```bash
   mkdir -p src/personal_agent/config
   touch src/personal_agent/config/{__init__,settings,env_loader,validators}.py
   ```

3. **Implement environment detection**:
   - `get_environment() -> Environment` enum
   - Support development, staging, production, test

4. **Implement .env file loader**:
   - `load_env_file()` function with priority order
   - Use `python-dotenv` for loading
   - Log loaded file using structlog

5. **Define AppConfig Pydantic model**:
   - Core fields: environment, debug, project_name, version
   - Telemetry fields: log_dir, log_level, log_format
   - LLM Client fields: llm_base_url, llm_timeout_seconds, llm_max_retries
   - Orchestrator fields: max_concurrent_tasks, task_timeout_seconds
   - Brainstem fields: sensor_poll_interval_seconds
   - Path fields: governance_config_path, model_config_path

6. **Implement singleton pattern**:
   - `load_app_config() -> AppConfig`
   - `get_settings() -> AppConfig` with caching
   - Export `settings` from `__init__.py`

7. **Write tests**:
   - Test environment detection
   - Test .env file loading priority
   - Test Pydantic validation
   - Test precedence (env vars override defaults)
   - Test singleton behavior

**Acceptance criteria**: Can import `settings` and access configuration values with type safety.

### Phase 2: Integration with Existing Loaders (Week 1-2)

1. **Update governance config loader**:
   - Accept `governance_config_path: Path` parameter (from `settings.governance_config_path`)
   - Use `settings` for any governance-related app settings

2. **Update model config loader** (when implemented):
   - Accept `model_config_path: Path` parameter (from `settings.model_config_path`)
   - Use `settings` for any model-related app settings

3. **Update telemetry logger**:
   - Use `settings.log_level`, `settings.log_format`, `settings.log_dir`
   - Initialize logger with config values

4. **Write integration tests**:
   - Test governance loader uses correct path from settings
   - Test logger uses correct settings

**Acceptance criteria**: Existing config loaders use `settings` for paths and related configuration.

### Phase 3: Component Integration (Week 2)

1. **Update LLM Client**:
   - Use `settings.llm_base_url`, `settings.llm_timeout_seconds`, `settings.llm_max_retries`

2. **Update Orchestrator**:
   - Use `settings.orchestrator_max_concurrent_tasks`, `settings.orchestrator_task_timeout_seconds`

3. **Update Brainstem**:
   - Use `settings.brainstem_sensor_poll_interval_seconds`

4. **Update UI**:
   - Use `settings` for CLI-related settings (if any)

5. **Write component tests**:
   - Test components use correct settings
   - Test settings can be overridden via dependency injection in tests

**Acceptance criteria**: All components use `settings` for configuration access.

### Phase 4: Consolidate Config Loaders (Week 2-3)

**Goal**: Move all configuration file loaders into `config/` module for single source of truth.

1. **Create shared YAML loader**:
   - Create `config/loader.py` with `load_yaml_file()` utility
   - Shared error handling and logging patterns
   - Used by all domain config loaders

2. **Move governance config loader**:
   - Move `governance/config_loader.py` → `config/governance_loader.py`
   - Update to use shared `load_yaml_file()` from `config/loader.py`
   - Keep `GovernanceConfig` Pydantic model in `governance/models.py`
   - Update imports: `from personal_agent.config import load_governance_config`
   - Maintain backward compatibility via re-exports in `governance/__init__.py` (deprecate)

3. **Move model config loader**:
   - Move `llm_client/config_loader.py` → `config/model_loader.py`
   - Update to use shared `load_yaml_file()` from `config/loader.py`
   - Create `ModelConfig` Pydantic model in `llm_client/models.py` (or `config/models.py`)
   - Update `load_model_config()` to return `ModelConfig` instead of `dict[str, Any]`
   - Update imports: `from personal_agent.config import load_model_config`
   - Update `LocalLLMClient` to use typed `ModelConfig`
   - Maintain backward compatibility via re-exports in `llm_client/__init__.py` (deprecate)

4. **Update `config/__init__.py`**:
   - Export all loaders: `load_governance_config`, `load_model_config`
   - Single import point: `from personal_agent.config import settings, load_governance_config, load_model_config`

5. **Update tests**:
   - Move config loader tests to `tests/test_config/`
   - Test shared YAML loader utilities
   - Test all loaders return typed Pydantic models
   - Test backward compatibility (deprecated imports still work)

6. **Update documentation**:
   - Update `config/AGENTS.md` with consolidated architecture
   - Update component AGENTS.md files with new import patterns
   - Document deprecation timeline for old import paths

**Acceptance criteria**:
- ✅ All config loaders live in `config/` module
- ✅ Shared YAML loading utilities eliminate code duplication
- ✅ All loaders return validated Pydantic models (not raw dicts)
- ✅ Single import point: `from personal_agent.config import ...`
- ✅ Backward compatibility maintained (deprecated imports work with warnings)
- ✅ All tests pass with new architecture

---

## 4. Consequences

### Positive

✅ **Single source of truth**: One import point for all configuration
✅ **Type safety**: Pydantic validation catches errors at startup
✅ **Clear precedence**: Env vars → YAML → defaults is explicit
✅ **Security**: Secrets in .env files (gitignored), never in code
✅ **Testability**: Easy to override config in tests via dependency injection
✅ **Integration**: Works alongside existing config loaders (governance, models)
✅ **Observability**: Configuration loading is logged with structlog
✅ **Fail fast**: Invalid config detected at startup, not runtime

### Negative

⚠️ **Additional dependency**: `pydantic-settings` and `python-dotenv`
⚠️ **Singleton pattern**: Can make testing harder if not designed for dependency injection
⚠️ **Complexity**: More abstraction layer (but reduces overall complexity)

### Mitigations

- **Dependency injection**: Components accept `AppConfig` parameter in constructors (default to `settings` singleton)
- **Testing**: Provide `override_settings()` context manager for tests
- **Documentation**: Clear examples in component AGENTS.md files

---

## 5. Alternatives Considered

### 5.1 Multiple Config Managers (Rejected)

**Approach**: Keep separate config managers per domain (governance, models, app settings)

**Rejected because**:

- Fragmentation leads to inconsistent patterns
- Hard to know where to look for configuration
- Precedence rules become unclear across domains

### 5.2 Scattered Config Loaders (Rejected)

**Approach**: Keep config loaders in their respective domain modules (governance/config_loader.py, llm_client/config_loader.py)

**Rejected because**:

- **Fragmentation**: Config loaders scattered across codebase, hard to find
- **Code duplication**: Similar YAML loading logic repeated in multiple places
- **Inconsistency**: Different return types (Pydantic model vs raw dict)
- **Unclear ownership**: Where does config management live?

**Decision**: Consolidate all config loaders into `config/` module while keeping domain Pydantic models in domain modules. This provides:
- Single source of truth for configuration management
- Shared utilities (DRY principle)
- Consistent patterns across all loaders
- Clear ownership: `config/` module owns all configuration file management

### 5.3 No Environment Detection (Rejected)

**Approach**: Always load `.env` file, no environment-specific files

**Rejected because**:

- Production and development need different secrets
- Environment-specific files reduce risk of using wrong config
- Standard pattern in production systems

### 5.4 Configuration as Code Only (Rejected)

**Approach**: All config in Python code, no .env files

**Rejected because**:

- Secrets should not be in code
- Environment-specific values need to change without code changes
- Less flexible for deployment scenarios

---

## 6. Related ADRs

- **ADR-0005**: Governance Configuration & Operational Modes (governance YAML loader)
- **ADR-0003**: Model Stack (model configuration)
- **ADR-0004**: Telemetry and Metrics (log configuration)

---

## 7. Open Questions

- Should we support **hot-reloading** of configuration at runtime? (Future: only for non-critical settings)
- Should we support **configuration validation** that checks cross-field dependencies? (Future: Pydantic validators)
- Should we generate **configuration documentation** from Pydantic models? (Future: use Pydantic schema export)

---

## 8. References

- [Pydantic Settings Documentation](https://docs.pydantic.dev/latest/concepts/pydantic_settings/)
- [python-dotenv Documentation](https://pypi.org/project/python-dotenv/)
- [Reference Implementation](https://github.com/FareedKhan-dev/production-grade-agentic-system/blob/master/src/config/settings.py) (adapted for this project's patterns)

---

**Decision Log**:

- 2025-12-29: Initial proposal
- 2025-01-01: **Updated to consolidate all config loaders into `config/` module** - Single source of truth for configuration management. All YAML file loaders (governance, models) now live in `config/` module, providing consistency, DRY principles, and clear ownership.
