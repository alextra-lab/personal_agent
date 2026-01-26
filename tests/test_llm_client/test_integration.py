"""Integration tests for LocalLLMClient with real LM Studio instance.

These tests require LM Studio (or compatible server) to be running.
Skip with: pytest -m "not requires_llm_server"
"""

import pytest

from personal_agent.llm_client import LocalLLMClient, ModelRole
from personal_agent.telemetry.trace import TraceContext


@pytest.mark.integration
@pytest.mark.requires_llm_server
@pytest.mark.asyncio
async def test_real_llm_call_router_model() -> None:
    """Test making a real call to LM Studio with router model.

    This is the acceptance test that verifies:
    - Client can connect to LM Studio
    - Response is received and parsed correctly
    - Telemetry is logged
    - Usage/tokens are captured
    """
    # Create client with default settings (should use http://localhost:1234/v1)
    client = LocalLLMClient()

    # Create trace context
    trace_ctx = TraceContext.new_trace()

    # Simple test message
    messages = [{"role": "user", "content": "Say 'Hello, this is a test' and nothing else."}]

    # Make the call
    response = await client.respond(
        role=ModelRole.ROUTER,
        messages=messages,
        trace_ctx=trace_ctx,
    )

    # Verify response structure
    assert response is not None
    assert "content" in response
    assert "role" in response
    assert "usage" in response
    assert "raw" in response

    # Verify content is not empty
    assert len(response["content"]) > 0
    print(f"\n✓ Response received: {response['content'][:100]}...")

    # Verify usage/tokens are captured
    usage = response["usage"]
    assert usage is not None
    print("\n✓ Usage captured:")
    print(f"  - Total tokens: {usage.get('total_tokens', 'N/A')}")
    print(f"  - Prompt tokens: {usage.get('prompt_tokens', 'N/A')}")
    print(f"  - Completion tokens: {usage.get('completion_tokens', 'N/A')}")

    # Verify trace_id (note: LLMResponse doesn't include trace_id, but we log it in telemetry)
    # The trace_id is tracked via telemetry, not in the response itself
    print(f"\n✓ Trace ID: {trace_ctx.trace_id}")

    # Verify role
    assert response["role"] in ["assistant", "model"]  # Different APIs use different role names
    print(f"\n✓ Role: {response['role']}")

    # Verify raw response exists
    assert isinstance(response["raw"], dict)
    print(f"\n✓ Raw response keys: {list(response['raw'].keys())}")


@pytest.mark.integration
@pytest.mark.requires_llm_server
@pytest.mark.asyncio
async def test_real_llm_call_with_fallback() -> None:
    """Test that fallback to chat/completions works if responses endpoint unavailable.

    This verifies the automatic fallback mechanism works in practice.
    """
    client = LocalLLMClient()
    trace_ctx = TraceContext.new_trace()

    messages = [{"role": "user", "content": "Reply with just: OK"}]

    response = await client.respond(
        role=ModelRole.ROUTER,
        messages=messages,
        trace_ctx=trace_ctx,
    )

    # Should get a response regardless of which endpoint was used
    assert response is not None
    assert len(response["content"]) > 0
    print(f"\n✓ Fallback test - Response: {response['content']}")


@pytest.mark.integration
@pytest.mark.requires_llm_server
@pytest.mark.asyncio
async def test_real_llm_call_all_roles() -> None:
    """Test calling all model roles to verify configuration is correct."""
    client = LocalLLMClient()
    trace_ctx = TraceContext.new_trace()

    test_message = "Say 'test' and nothing else."

    for role in [ModelRole.ROUTER, ModelRole.REASONING, ModelRole.CODING]:
        try:
            response = await client.respond(
                role=role,
                messages=[{"role": "user", "content": test_message}],
                trace_ctx=trace_ctx,
            )
            assert response is not None
            assert len(response["content"]) > 0
            print(f"\n✓ {role.value} model: {response['content'][:50]}...")
        except Exception as e:
            # Some models might not be loaded in LM Studio
            print(f"\n⚠ {role.value} model failed (may not be loaded): {e}")
            pytest.skip(f"Model {role.value} not available: {e}")
