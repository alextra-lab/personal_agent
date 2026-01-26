# SLM Server Integration

## Overview

The Personal Agent uses a separate **SLM Server** (Small Language Model Server) for local LLM inference. This is a multi-model routing service that runs Apple Silicon optimized models via MLX.

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                    Personal Agent Service                    │
│                     (Port 9000)                             │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐    │
│  │ Orchestrator │  │  Brainstem   │  │    Tools     │    │
│  └──────┬───────┘  └──────────────┘  └──────────────┘    │
│         │                                                   │
│         │ LLM API Calls                                    │
│         │ (OpenAI-compatible)                              │
└─────────┼──────────────────────────────────────────────────┘
          │
          ▼
┌─────────────────────────────────────────────────────────────┐
│                      SLM Server Router                       │
│                        (Port 8000)                          │
│                                                              │
│  Route requests to specialized models based on task         │
└────┬─────────┬─────────┬─────────┬──────────────────────────┘
     │         │         │         │
     ▼         ▼         ▼         ▼
┌─────────┬─────────┬─────────┬─────────┐
│ Router  │Standard │Reasoning│ Coding  │
│ LFM 1.2B│Qwen 4B  │Qwen 8B  │Devstral │
│Port 8500│Port 8501│Port 8502│Port 8503│
└─────────┴─────────┴─────────┴─────────┘
     MLX Backend (Apple Silicon optimized)
```

## SLM Server Details

### Location

The SLM Server is a separate project:

- **Repository**: [https://github.com/alextra-lab/slm_server](https://github.com/alextra-lab/slm_server)
- **Documentation**: See [SLM Server README](https://github.com/alextra-lab/slm_server#readme) for setup

### Models Configuration

**Router Model** (Port 8500)

- **Model**: liquid/lfm2.5-1.2b (LFM2.5-1.2B-Instruct-8bit)
- **Role**: Fast routing decisions
- **Purpose**: Determine which backend model to use for each request
- **Latency**: ~50ms (optimized for speed)

**Standard Model** (Port 8501)

- **Model**: qwen/qwen3-4b-2507 (Qwen3-4B-Instruct-2507-MLX-8bit)
- **Role**: General conversational tasks
- **Purpose**: Default model for most user interactions
- **Balance**: Good speed/quality tradeoff

**Reasoning Model** (Port 8502)

- **Model**: qwen/qwen3-8b (Qwen3-8B-MLX-8bit)
- **Role**: Complex reasoning and analysis
- **Purpose**: Tasks requiring deeper thinking
- **Trade-off**: Slower but more capable

**Coding Model** (Port 8503)

- **Model**: mistralai/devstral-small-2-2512 (Devstral-Small-2507-MLX-8bit)
- **Role**: Code generation and technical tasks
- **Purpose**: Specialized for programming tasks
- **Optimization**: Tuned for code understanding

## Starting SLM Server

### Prerequisites

```bash
git clone https://github.com/alextra-lab/slm_server.git
cd slm_server
uv sync --extra mlx  # Install MLX backend dependencies
```

### Start Server

```bash
cd slm_server  # or wherever you cloned it
./start.sh
```

### Expected Output

```
🚀 Starting SLM Server...
📦 Starting backend model servers...
✅ All backend servers are ready
🔄 Starting routing service...
✅ SLM Server running on http://localhost:8000
```

### Verify Running

```bash
# Check router health
curl http://localhost:8000/health

# List available models
curl http://localhost:8000/v1/models

# Test inference
curl -X POST http://localhost:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "qwen/qwen3-4b-2507",
    "messages": [{"role": "user", "content": "Hello!"}]
  }'
```

## Integration with Personal Agent

### Configuration

The Personal Agent connects to SLM Server via settings:

```python
# In src/personal_agent/config/settings.py
llm_base_url: str = Field(
    default="http://localhost:8000/v1",
    description="Base URL for LLM API (slm_server default)"
)
```

### Environment Variable

```bash
# .env
LLM_BASE_URL=http://localhost:8000/v1
```

### Usage in Code

The orchestrator uses the LLM client which connects to SLM Server:

```python
from personal_agent.llm_client import LLMClient

# Client automatically uses settings.llm_base_url
client = LLMClient()

# Make requests - router will select appropriate backend
response = await client.chat_completion(
    messages=[{"role": "user", "content": "Explain quantum computing"}]
)
```

## Port Allocation

**Why port 9000 for Personal Agent?**

Originally, the Personal Agent Service was designed to run on port 8000. However, since SLM Server (the LLM inference backend) runs on port 8000, we changed the Personal Agent Service to port 9000 to avoid conflicts.

**Port Summary:**

- **8000**: SLM Server Router (OpenAI-compatible API)
- **8500-8503**: Backend model servers (internal, called by router)
- **9000**: Personal Agent Service (FastAPI)
- **5432**: PostgreSQL
- **9200**: Elasticsearch
- **5601**: Kibana
- **7474/7687**: Neo4j

## Startup Order

For optimal operation, start services in this order:

1. **Infrastructure** (docker-compose)

   ```bash
   ./scripts/init-services.sh
   ```

2. **SLM Server** (LLM backend)

   ```bash
   cd slm_server  # or wherever you cloned it
   ./start.sh
   ```

3. **Personal Agent Service** (API)

   ```bash
   cd personal_agent  # or wherever you cloned it
   uv run uvicorn personal_agent.service.app:app --reload --port 9000
   ```

## Model Selection Strategy

The router model (LFM 1.2B) analyzes incoming requests and routes to:

- **Standard (Qwen 4B)**: Default for conversations, simple queries
- **Reasoning (Qwen 8B)**: Complex analysis, multi-step reasoning
- **Coding (Devstral)**: Code generation, debugging, technical explanations

This routing is transparent to the Personal Agent - it simply makes OpenAI-compatible API calls to port 8000.

## Performance Characteristics

**Apple Silicon Optimization:**

- All models run via MLX (optimized for M-series chips)
- 8-bit quantization for memory efficiency
- GPU acceleration for neural matrix operations
- Concurrent model serving (different models on different ports)

**Expected Latency:**

- Router decision: ~50ms
- Standard model: ~100-200ms per token
- Reasoning model: ~150-300ms per token
- Coding model: ~100-200ms per token

**Memory Usage:**

- Total: ~8-12GB RAM with all 4 models loaded
- Router: ~1.5GB
- Standard: ~3GB
- Reasoning: ~5GB
- Coding: ~3GB

## Troubleshooting

### SLM Server Not Starting

```bash
# Check if ports are already in use
lsof -i :8000
lsof -i :8500-8503

# View logs
cd slm_server  # or wherever you cloned it
tail -f logs/slm_server.log
```

### Connection Refused

```bash
# Verify SLM Server is running
curl http://localhost:8000/health

# Check Personal Agent configuration
grep llm_base_url personal_agent/.env
```

### Model Loading Errors

- **Issue**: Models not found in cache
- **Solution**: Download models via LM Studio first
- **Path**: `~/.cache/lm-studio/models/`

## Development Notes

### Testing Without SLM Server

For testing Personal Agent without LLM inference:

1. Mock the LLM client responses
2. Use a placeholder model endpoint
3. Set `LLM_BASE_URL` to a test fixture

### Using Different Models

To change models, update SLM Server configuration:

```bash
cd slm_server  # or wherever you cloned it
# Edit config/models.yaml
# Restart SLM Server
```

## References

- **SLM Server Project**: [https://github.com/alextra-lab/slm_server](https://github.com/alextra-lab/slm_server)
- **MLX Documentation**: <https://ml-explore.github.io/mlx/>
- **Model Sources**: LM Studio model directory
- **OpenAI API Compatibility**: SLM Server implements OpenAI-compatible endpoints
