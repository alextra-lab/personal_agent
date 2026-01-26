# Development Tools Overview

This directory contains operational and development tools for the Personal Agent project.

## Model Server Management

**Note**: Model server management tools have been moved to the `slm_server` project.

For starting model servers, A/B testing, and managing backend inference servers (MLX, llama.cpp, LMStudio), see:
- **`slm_server` project**: [https://github.com/alextra-lab/slm_server](https://github.com/alextra-lab/slm_server)
- **CLI tool**: `uv run python -m slm_server.benchmark_models`
- **Documentation**: See [SLM Server README](https://github.com/alextra-lab/slm_server#readme)

The `slm_server` project provides:
- Unified LLM server with nginx reverse proxy
- Model-based routing (single endpoint, routes by model ID)
- CLI tools for starting/managing backend servers
- Full integration with `config/models.yaml`

---

## Legacy Documentation (for reference)

### `benchmark_models.py` (moved to slm_server)

**Status**: This tool has been moved to the `slm_server` project. Use `slm_server.benchmark_models` instead.

A CLI tool for A/B testing models configured in `config/models.yaml` using different backends:

- **MLX**: Apple Silicon optimized inference
- **llama.cpp**: High-performance C++ inference engine
- **LMStudio**: GUI-based model server (uses cache at `~/.cache/lm-studio/models`)

### Usage

The script can be run using `uv` (recommended) or by activating the virtual environment manually.

#### Using `uv` (Recommended)

`uv` automatically manages the virtual environment, so you don't need to activate it:

```bash
# List configured models
uv run python tools/benchmark_models.py list-models

# Check model availability for a backend
uv run python tools/benchmark_models.py check --backend mlx
uv run python tools/benchmark_models.py check --backend llamacpp
uv run python tools/benchmark_models.py check --backend lmstudio

# Start a model server
uv run python tools/benchmark_models.py start --backend mlx --model router
uv run python tools/benchmark_models.py start --backend llamacpp --model reasoning --port 8001
uv run python tools/benchmark_models.py start --backend lmstudio --model coding --port 1234

# Override model file path manually
uv run python tools/benchmark_models.py start --backend mlx --model router --model-file /path/to/model
```

#### Using Virtual Environment Directly

Alternatively, activate the virtual environment first:

```bash
# Activate virtual environment
source .venv/bin/activate

# Then run the script
python tools/benchmark_models.py list-models
python tools/benchmark_models.py check --backend mlx
python tools/benchmark_models.py start --backend mlx --model router

# Deactivate when done
deactivate
```

#### Make Script Executable (Optional)

You can also make the script executable and run it directly:

```bash
chmod +x tools/benchmark_models.py

# With uv
uv run tools/benchmark_models.py list-models

# Or with activated venv
./tools/benchmark_models.py list-models
```

### Model Discovery

The script automatically searches for models in common locations:

- **LMStudio**: `~/.cache/lm-studio/models/{org}/{model}/`
- **MLX**: `~/.mlx_models/`, `~/mlx_models/`, `/opt/mlx_models/`
- **llama.cpp**: `~/.cache/llama.cpp/`, `~/llama_models/`, `/opt/llama_models/`

If auto-discovery fails, use `--model-file` to specify the path manually.

### Backend Requirements

**Optional Dependencies**: Backend servers are available as optional extras in `pyproject.toml`:
- Install MLX: `uv sync --extra mlx`
- Install llama.cpp: `uv sync --extra llamacpp`
- Install both: `uv sync --extra mlx --extra llamacpp`

Or install manually with pip if preferred.

All backends provide **OpenAI-compatible API endpoints**:

- Base URL: `http://localhost:{port}/v1`
- Endpoints: `/v1/chat/completions`, `/v1/completions`, `/v1/embeddings`
- This ensures seamless integration with the agent's LLM client

#### MLX

- **Installation**: 
  - Via uv: `uv sync --extra mlx`
  - Or manually: `pip install mlx-openai-server`
- **Server**: `mlx-openai-server` (provides OpenAI-compatible API)
- Optimized for Apple Silicon (M-series chips)
- Model path should be a directory containing MLX model files
- **Concurrency**: Supports `--max-concurrent-requests` (set from `max_concurrency` in models.yaml)
- **Performance parameters**: Context length, concurrency

#### llama.cpp

- **Installation**:
  - Via uv: `uv sync --extra llamacpp`
  - Or manually: `pip install 'llama-cpp-python[server]'`
- **Server**: `python3 -m llama_cpp.server` (provides OpenAI-compatible API)
- Supports GGUF format models
- GPU acceleration on Apple Silicon via Metal
- **Concurrency**: Supports `--n-parallel` (set from `max_concurrency` in models.yaml)
- **Performance parameters**: Context length, GPU layers (based on quantization), concurrency, CPU threads

#### LMStudio

- Requires LM Studio application installed
- Models must be in cache: `~/.cache/lm-studio/models`
- Provides OpenAI-compatible API when server is started
- Can be started via GUI or CLI (if available)
- Ensure server exposes: `http://localhost:{port}/v1`
- **Concurrency**: Managed internally by LMStudio (not configurable via script)

### Examples

#### Benchmark router model across backends

```bash
# Terminal 1: MLX
python tools/benchmark_models.py start --backend mlx --model router --port 1234

# Terminal 2: llama.cpp
python tools/benchmark_models.py start --backend llamacpp --model router --port 8001

# Terminal 3: LMStudio (start via GUI, then test)
# Or use the script if CLI is available
python tools/benchmark_models.py start --backend lmstudio --model router --port 8002
```

#### Check all backends for model availability

```bash
for backend in mlx llamacpp lmstudio; do
    echo "=== Checking $backend ==="
    python tools/benchmark_models.py check --backend $backend
done
```

### Configuration

The script reads from `config/models.yaml` by default. Override with `--config`:

```bash
uv run python tools/benchmark_models.py start --backend mlx --model router --config config/models.medium.yaml
```

### Port Management

**Each model server requires its own port.** For A/B testing, start different backends on different ports:

```bash
# Terminal 1: MLX backend
uv run python tools/benchmark_models.py start --backend mlx --model router --port 1234

# Terminal 2: llama.cpp backend (same model, different backend)
uv run python tools/benchmark_models.py start --backend llamacpp --model router --port 8001

# Terminal 3: LMStudio backend
uv run python tools/benchmark_models.py start --backend lmstudio --model router --port 8002
```

Then configure `models.yaml` with per-model endpoints:

```yaml
models:
  router:
    id: "qwen/qwen3-1.7b"
    endpoint: "http://localhost:1234/v1"  # MLX backend
    # Or: endpoint: "http://localhost:8001/v1"  # llama.cpp backend
```

**No nginx required** - the agent's LLM client supports per-model endpoints via the `endpoint` field in `models.yaml`. Each model can point to a different port/backend.

### Notes

- The script uses the existing `load_model_config()` function from `personal_agent.config`
- Model paths are auto-discovered but can be overridden
- Server processes run in foreground (Ctrl+C to stop)
- LMStudio may require manual GUI interaction if CLI is not available
- Each server instance serves one model - use different ports for different models/backends