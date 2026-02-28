# Testing SLM Server Integration

## Current Status

✅ **SLM Server is running** on `localhost:8000`
- Health check: ✅ Working
- List models: ✅ Working
- Chat completions: ⚠️ Needs backend servers running

## Issue Found

The router was copying all headers (including `Content-Length`) which caused a mismatch. **Fixed** - router now filters problematic headers.

## Next Steps

### 1. Restart SLM Server Routing Service

After the router fix, restart the routing service:

```bash
cd slm_server  # or wherever you cloned it
# Stop current router (Ctrl+C if running in foreground)
# Then restart:
uv run python -m slm_server router
```

### 2. Start Backend Model Servers

The router needs backend servers running on the configured ports. Start them:

```bash
cd slm_server  # or wherever you cloned it

# Option A: Start all backends at once
uv run python -m slm_server backends

# Option B: Start individual models (in separate terminals)
uv run python -m slm_server.benchmark_models start --backend mlx --model router --port 1234
uv run python -m slm_server.benchmark_models start --backend mlx --model standard --port 8002
# ... etc
```

**Note**: Check `slm_server/config/models.yaml` to see which backend/port each model uses.

### 3. Test SLM Server

```bash
cd personal_agent  # or wherever you cloned it
./tools/test_slm_server.sh
```

### 4. Test Agent Integration

Once SLM Server is working, test the agent:

```bash
cd ~/Dev/personal_agent

# Test LLM client directly
python -c "
import asyncio
from personal_agent.llm_client import LocalLLMClient
from personal_agent.llm_client.types import ModelRole
from personal_agent.telemetry.trace import TraceContext

async def test():
    ctx = TraceContext()
    async with LocalLLMClient() as client:
        response = await client.respond(
            role=ModelRole.ROUTER,
            messages=[{'role': 'user', 'content': 'Say hello'}],
            trace_ctx=ctx
        )
        print(f'Response: {response[\"content\"]}')

asyncio.run(test())
"

# Or run existing tests
pytest tests/test_llm_client/ -v
```

## Configuration

Your `config/models.yaml` is correctly configured with:
```yaml
models:
  router:
    endpoint: "http://localhost:8000/v1"  # ✅ Points to slm_server
```

The agent will use this endpoint for all models.

## Troubleshooting

### Backend server not responding
- Check if backend is running: `lsof -i :1234`
- Check backend logs for errors
- Verify model file exists in expected location

### Router can't find model
- Check `slm_server/config/models.yaml` has the model ID
- Verify model ID in request matches config exactly

### Connection refused
- Ensure backend server is started before making requests
- Check firewall/network settings
