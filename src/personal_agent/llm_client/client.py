"""Local LLM Client implementation.

This module provides the LocalLLMClient class for interacting with local LLM
servers (LM Studio, Ollama, etc.) with proper error handling, retries, and telemetry.
"""

import asyncio
import time
from pathlib import Path
from typing import Any

import httpx

from personal_agent.config import settings

# Import from module directly to avoid circular import
from personal_agent.config.model_loader import ModelConfigError, load_model_config
from personal_agent.llm_client.adapters import (
    adapt_chat_completions_response,
    build_chat_completions_request,
)
from personal_agent.llm_client.models import ModelConfig, ModelDefinition
from personal_agent.llm_client.types import (
    LLMClientError,
    LLMConnectionError,
    LLMInvalidResponse,
    LLMRateLimit,
    LLMResponse,
    LLMServerError,
    LLMTimeout,
    ModelRole,
)
from personal_agent.telemetry import get_logger
from personal_agent.telemetry.events import (
    MODEL_CALL_COMPLETED,
    MODEL_CALL_ERROR,
    MODEL_CALL_STARTED,
)
from personal_agent.telemetry.trace import TraceContext

log = get_logger(__name__)


class LocalLLMClient:
    """Client for interacting with local LLM servers.

    This client provides a unified interface for calling local LLM models
    (via LM Studio, Ollama, etc.) with proper error handling, retries, and telemetry.

    Attributes:
        base_url: Base URL for the LLM API (e.g., "http://localhost:1234/v1").
        timeout_seconds: Default timeout for requests.
        max_retries: Maximum number of retry attempts.
        model_configs: Dictionary mapping model role names (str) to ModelDefinition.
    """

    def __init__(
        self,
        base_url: str | None = None,
        timeout_seconds: int | None = None,
        max_retries: int | None = None,
        model_config_path: Path | None = None,
    ) -> None:
        """Initialize the LocalLLMClient.

        Args:
            base_url: Base URL for the LLM API. If None, uses settings.llm_base_url.
            timeout_seconds: Default timeout for requests. If None, uses settings.llm_timeout_seconds.
            max_retries: Maximum number of retry attempts. If None, uses settings.llm_max_retries.
            model_config_path: Path to models.yaml file. If None, uses settings.model_config_path.
        """
        self.base_url = base_url or settings.llm_base_url
        self.timeout_seconds = timeout_seconds or settings.llm_timeout_seconds
        self.max_retries = max_retries or settings.llm_max_retries

        # Load model configurations
        try:
            config: ModelConfig = load_model_config(model_config_path)
            self.model_configs: dict[str, ModelDefinition] = config.models
        except ModelConfigError as e:
            log.warning("model_config_load_failed", error=str(e), using_defaults=True)
            self.model_configs = {}

        # Build timeout map per role from model configs
        # Use default_timeout from each model's config, fallback to hardcoded defaults
        self._role_timeouts: dict[ModelRole, int] = {}
        for role in ModelRole:
            model_def = self.model_configs.get(role.value)
            if model_def and model_def.default_timeout:
                self._role_timeouts[role] = model_def.default_timeout
            else:
                # Fallback to spec defaults if config missing
                fallback_timeouts = {
                    ModelRole.ROUTER: 30,  # Increased for thinking models
                    ModelRole.STANDARD: 45,
                    ModelRole.REASONING: 60,
                    ModelRole.CODING: 45,
                }
                self._role_timeouts[role] = fallback_timeouts.get(role, self.timeout_seconds)

    async def respond(
        self,
        role: ModelRole,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | dict[str, Any] | None = None,
        response_format: dict[str, Any] | None = None,
        system_prompt: str | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
        timeout_s: float | None = None,
        max_retries: int | None = None,
        reasoning_effort: str | None = None,
        trace_ctx: TraceContext | None = None,
        previous_response_id: str | None = None,
        # TODO: Add governance hooks (Section 7 of spec)
        # mode: Mode | None = None,  # Current operational mode for constraint enforcement
        # governance_config: GovernanceConfig | None = None,  # Mode-aware limits
    ) -> LLMResponse:
        """Make a single-turn LLM call for a given model role.

        This method handles:
        - Model configuration lookup by role
        - HTTP request with timeout and retries
        - Response normalization via adapters
        - Telemetry emission
        - Error handling and classification

        Args:
            role: Model role (router, reasoning, coding).
            messages: List of message dicts with role and content.
            tools: Optional list of tool definitions for function calling.
            tool_choice: Tool choice parameter ("auto", "none", or specific tool).
            response_format: Optional structured output constraints (OpenAI-compatible).
            system_prompt: Optional system prompt (prepended to messages).
            max_tokens: Maximum tokens to generate.
            temperature: Sampling temperature.
            timeout_s: Request timeout in seconds (overrides default for role).
            max_retries: Maximum number of retry attempts (overrides default).
            reasoning_effort: Optional reasoning effort level. LM Studio /v1/responses API
                supports "minimal", "low", "medium", "high". Warnings about model support are harmless.
            trace_ctx: Trace context for telemetry correlation.
            previous_response_id: ID from previous response (for /v1/responses API stateful conversation).

        Returns:
            LLMResponse with normalized structure.

        Raises:
            LLMTimeout: If request times out.
            LLMConnectionError: If connection fails.
            LLMServerError: If server returns an error.
            LLMInvalidResponse: If response format is invalid.
            ModelConfigError: If model configuration is missing or invalid.
        """
        # Get model configuration
        model_config: ModelDefinition | None = self.model_configs.get(role.value)
        if not model_config:
            raise ModelConfigError(f"No configuration found for role: {role.value}")

        # Get model ID
        model_id = model_config.id
        effective_temperature = temperature
        if effective_temperature is None:
            effective_temperature = model_config.temperature

        # Determine base URL for this model
        # If endpoint is specified in config, use it; otherwise use client's base_url
        # This allows different models to use different providers/ports
        model_base_url = model_config.endpoint
        if model_base_url:
            # Use model-specific endpoint (may be different provider/port)
            base = model_base_url.rstrip("/")
        else:
            # Use default base_url from settings
            base = self.base_url.rstrip("/")

        # Construct endpoint from base URL
        # Use /v1/chat/completions by default - it's the standard OpenAI API supported by all backends
        # (MLX, llama.cpp, Ollama, etc.). The /v1/responses endpoint is LM Studio-specific.
        if base.endswith("/v1"):
            current_endpoint = f"{base}/chat/completions"
        else:
            current_endpoint = f"{base}/v1/chat/completions"

        # Always use chat_completions - it's universally supported
        current_api_type = "chat_completions"
        tried_fallback = False  # No fallback needed since we're using the standard endpoint

        # Check if model supports native function calling
        # If not, filter out tools to avoid confusing the model
        supports_function_calling = model_config.supports_function_calling
        if tools and not supports_function_calling:
            log.warning(
                "tools_filtered_no_function_calling",
                model_id=model_id,
                role=role.value,
                tools_count=len(tools),
                reason="Model does not support native function calling",
            )
            tools = None
            tool_choice = None

        # Determine timeout (role default or override)
        if timeout_s is None:
            timeout_s = float(self._role_timeouts.get(role, self.timeout_seconds))

        # Determine effective retry count (override or default)
        effective_max_retries = self.max_retries if max_retries is None else max_retries

        # TODO: Governance hooks (Section 7 of LOCAL_LLM_CLIENT_SPEC_v0.1.md)
        # When Brainstem/ModeManager is integrated, enforce:
        # - Mode-aware limits: check allowed_roles, cap max_tokens/temperature per mode
        # - Budget-aware limits: track cumulative usage, hard-stop on limits
        # - Tool filtering: disallow certain tools in Conservative/LOCKDOWN modes
        # Example:
        # if mode and governance_config:
        #     constraints = governance_config.mode_constraints.get(mode.value, {})
        #     if role.value not in constraints.get("allowed_roles", []):
        #         raise LLMClientError(f"Role {role.value} not allowed in mode {mode.value}")
        #     max_tokens = min(max_tokens or float('inf'), constraints.get("max_tokens", {}).get(role.value, max_tokens))
        #     temperature = min(temperature or 1.0, constraints.get("temperature", {}).get(role.value, temperature))

        # Prepare messages (add system prompt if provided)
        request_messages = messages.copy()
        if system_prompt:
            request_messages.insert(0, {"role": "system", "content": system_prompt})

        # Note: reasoning_effort is ignored for /v1/chat/completions (it's LM Studio /v1/responses specific)
        # We keep the parameter for API compatibility but don't use it

        # Create trace context if not provided
        if trace_ctx is None:
            trace_ctx = TraceContext.new_trace()

        # Emit telemetry: call started
        start_time = time.time()
        span_ctx, span_id = trace_ctx.new_span()
        log.info(
            MODEL_CALL_STARTED,
            role=role.value,
            model_id=model_id,
            endpoint=current_endpoint,
            trace_id=trace_ctx.trace_id,
            span_id=span_id,
        )

        last_error: Exception | None = None
        attempt = 0
        while attempt <= effective_max_retries:
            try:
                # Build payload using standard OpenAI chat/completions format
                payload = build_chat_completions_request(
                    messages=request_messages,
                    model=model_id,
                    tools=tools,
                    tool_choice=tool_choice,
                    max_tokens=max_tokens,
                    temperature=effective_temperature,
                    response_format=response_format,
                    previous_response_id=previous_response_id,
                    reasoning_effort=reasoning_effort,
                )

                # Debug: Log payload structure
                payload_messages = payload.get("messages", [])
                assistant_with_tool_calls = [
                    i
                    for i, msg in enumerate(payload_messages)
                    if msg.get("role") == "assistant" and msg.get("tool_calls")
                ]
                log.debug(
                    "chat_completions_payload",
                    endpoint=current_endpoint,
                    message_count=len(payload_messages),
                    assistant_with_tool_calls_indices=assistant_with_tool_calls,
                    has_tools="tools" in payload,
                    tools_count=len(payload.get("tools", [])),
                    trace_id=trace_ctx.trace_id,
                )

                # Configure httpx timeout - use longer timeout for read (model generation)
                timeout_config = httpx.Timeout(
                    connect=10.0,  # Connection timeout
                    read=timeout_s,  # Read timeout (model generation)
                    write=10.0,  # Write timeout
                    pool=10.0,  # Pool timeout
                )
                # Disable SSL verification for localhost (local LLM servers don't need it)
                # This also avoids macOS sandbox issues with certificate loading
                verify_ssl = not (
                    current_endpoint.startswith("http://localhost")
                    or current_endpoint.startswith("http://127.0.0.1")
                )
                async with httpx.AsyncClient(timeout=timeout_config, verify=verify_ssl) as client:
                    response = await client.post(current_endpoint, json=payload)
                    response.raise_for_status()
                    response_data = response.json()

                    # Log raw response for debugging
                    output_items = response_data.get("output", [])
                    output_types = (
                        [item.get("type") for item in output_items if isinstance(item, dict)]
                        if isinstance(output_items, list)
                        else []
                    )
                    log.debug(
                        "raw_llm_response",
                        api_type=current_api_type,
                        status_code=response.status_code,
                        has_output=("output" in response_data),
                        has_error=("error" in response_data),
                        output_items_count=len(output_items)
                        if isinstance(output_items, list)
                        else 0,
                        output_types=output_types,
                        response_preview=str(response_data)[:200],
                        trace_id=trace_ctx.trace_id,
                    )

                    # Check for error in response body (LM Studio returns 200 with error object)
                    if "error" in response_data and response_data["error"] is not None:
                        error_obj = response_data["error"]
                        error_msg = (
                            error_obj.get("message", str(error_obj))
                            if isinstance(error_obj, dict)
                            else str(error_obj)
                        )
                        error_param = (
                            error_obj.get("param", "") if isinstance(error_obj, dict) else ""
                        )
                        error_type = (
                            error_obj.get("type", "") if isinstance(error_obj, dict) else ""
                        )
                        log.error(
                            "llm_api_error",
                            api_type=current_api_type,
                            endpoint=current_endpoint,
                            error_message=error_msg,
                            error_param=error_param,
                            error_type=error_type,
                            trace_id=trace_ctx.trace_id,
                        )
                        raise LLMClientError(f"API returned error: {error_msg}")

                    # Adapt response to LLMResponse format (always chat/completions format)
                    llm_response = adapt_chat_completions_response(response_data)

                    # Emit telemetry: call completed
                    duration_ms = int((time.time() - start_time) * 1000)
                    log.info(
                        MODEL_CALL_COMPLETED,
                        role=role.value,
                        model_id=model_id,
                        endpoint=current_endpoint,
                        api_type=current_api_type,
                        fallback_used=tried_fallback,
                        latency_ms=duration_ms,
                        prompt_tokens=llm_response["usage"].get("prompt_tokens", 0),
                        completion_tokens=llm_response["usage"].get("completion_tokens", 0),
                        trace_id=trace_ctx.trace_id,
                        span_id=span_id,
                    )

                    return llm_response

            except httpx.TimeoutException:
                last_error = LLMTimeout(
                    f"Request to {current_endpoint} timed out after {timeout_s}s"
                )
                # Retry if we haven't exhausted retries
                if attempt < effective_max_retries:
                    wait_time = 2**attempt
                    log.warning(
                        "model_call_retry",
                        attempt=attempt + 1,
                        wait_time=wait_time,
                        trace_id=trace_ctx.trace_id,
                    )
                    await asyncio.sleep(wait_time)
                    attempt += 1
                    continue
                break

            except httpx.ConnectError as e:
                last_error = LLMConnectionError(f"Failed to connect to {current_endpoint}: {e}")
                # Don't retry connection errors (server is likely down)
                break

            except httpx.HTTPStatusError as e:
                if e.response.status_code == 429:
                    last_error = LLMRateLimit(f"Rate limit exceeded: {e}")
                    if attempt < effective_max_retries:
                        wait_time = 2**attempt
                        await asyncio.sleep(wait_time)
                        attempt += 1
                        continue
                elif e.response.status_code >= 500:
                    last_error = LLMServerError(f"Server error {e.response.status_code}: {e}")
                    if attempt < effective_max_retries:
                        wait_time = 2**attempt
                        await asyncio.sleep(wait_time)
                        attempt += 1
                        continue
                else:
                    last_error = LLMClientError(f"HTTP error {e.response.status_code}: {e}")
                break

            except httpx.RequestError as e:
                last_error = LLMConnectionError(f"Request error: {e}")
                break

            except LLMInvalidResponse as e:
                # Re-raise LLMInvalidResponse from adapter
                last_error = e
                break

            except (ValueError, KeyError, TypeError) as e:
                last_error = LLMInvalidResponse(f"Invalid response format: {e}")
                break

            except Exception as e:
                # Log the actual exception type and traceback for debugging
                import traceback as tb

                log.error(
                    "unexpected_exception_in_respond",
                    exception_type=type(e).__name__,
                    exception_message=str(e),
                    traceback=tb.format_exc(),
                    trace_id=trace_ctx.trace_id,
                )
                last_error = LLMClientError(f"Unexpected error: {e}")
                break

        # Emit telemetry: call error
        duration_ms = int((time.time() - start_time) * 1000)
        error_type = type(last_error).__name__ if last_error else "UnknownError"
        log.error(
            MODEL_CALL_ERROR,
            role=role.value,
            model_id=model_id,
            endpoint=current_endpoint,
            error_type=error_type,
            error=str(last_error) if last_error else "Unknown error",
            latency_ms=duration_ms,
            trace_id=trace_ctx.trace_id,
            span_id=span_id,
        )

        if last_error:
            raise last_error
        raise LLMClientError("Request failed with unknown error")

    def get_dspy_lm(self, role: ModelRole) -> Any:
        """Get a configured DSPy LM instance for the specified role.

        This method provides integration between LocalLLMClient and DSPy for
        structured outputs. It creates a DSPy language model adapter configured
        with the same base_url, timeout, and role-based model selection as the
        LocalLLMClient.

        Args:
            role: Model role (ROUTER, REASONING, CODING, STANDARD) for model selection.

        Returns:
            Configured dspy.LM instance ready to use with dspy.configure().

        Raises:
            ImportError: If dspy package is not installed.
            ModelConfigError: If model configuration is missing for role.

        Example:
            >>> import dspy
            >>> from personal_agent.llm_client import LocalLLMClient, ModelRole
            >>>
            >>> client = LocalLLMClient()
            >>> lm = client.get_dspy_lm(role=ModelRole.REASONING)
            >>> dspy.configure(lm=lm)
            >>>
            >>> class MySignature(dspy.Signature):
            ...     question: str = dspy.InputField()
            ...     answer: str = dspy.OutputField()
            >>>
            >>> predictor = dspy.ChainOfThought(MySignature)
            >>> result = predictor(question="What is Python?")

        Notes:
            - This is the recommended way to use DSPy with LocalLLMClient
            - Ensures DSPy uses the same configuration as regular LLM calls
            - For detailed DSPy integration, see: dspy_adapter.py
            - Based on E-008 prototype evaluation and ADR-0010
        """
        from personal_agent.llm_client.dspy_adapter import configure_dspy_lm

        model_def = self.model_configs.get(role.value)
        effective_base_url = (
            model_def.endpoint if model_def and model_def.endpoint else self.base_url
        )

        return configure_dspy_lm(
            role=role,
            base_url=effective_base_url,
            timeout_s=self.timeout_seconds,
        )
