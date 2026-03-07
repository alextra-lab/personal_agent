"""FRE-103: Local vs Cloud inference concurrency experiment.

Validates the hypothesis that HTTP 400 and timeout errors are caused by local
SLM inference server contention (LM Studio single-GPU limitation), and would
NOT occur with cloud Foundation Model providers.

Three tests:
  1. Local contention — 3 concurrent requests to LM Studio
  2. Cloud comparison — 3 concurrent requests to Anthropic Claude
  3. Mixed mode — local router + cloud reasoning concurrently

Usage:
    # Run all tests (requires LM Studio running + AGENT_ANTHROPIC_API_KEY set)
    python -m experiments.concurrency_research.run_experiment

    # Run only local test (no API key needed)
    python -m experiments.concurrency_research.run_experiment --test local

    # Run only cloud test
    python -m experiments.concurrency_research.run_experiment --test cloud

    # Run only mixed-mode test
    python -m experiments.concurrency_research.run_experiment --test mixed

    # Dry run (print config, don't make requests)
    python -m experiments.concurrency_research.run_experiment --dry-run

See: ADR-0029, config/models.yaml
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

EXPERIMENT_DIR = Path(__file__).parent
RESULTS_DIR = EXPERIMENT_DIR / "results"

SIMPLE_PROMPT = "What is 2 + 2? Answer in one sentence."
REASONING_PROMPT = (
    "Explain step by step why the sky appears blue during the day. "
    "Keep your answer under 100 words."
)


@dataclass
class RequestResult:
    """Result of a single inference request."""

    role: str
    endpoint: str
    model_id: str
    provider_type: str
    status: str  # "success", "error_400", "error_timeout", "error_other"
    http_status: int | None = None
    latency_ms: int = 0
    error_message: str | None = None
    response_preview: str | None = None
    tokens_prompt: int = 0
    tokens_completion: int = 0


@dataclass
class TestResult:
    """Aggregate result of a concurrency test."""

    test_name: str
    description: str
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    concurrent_requests: int = 0
    total_duration_ms: int = 0
    success_count: int = 0
    failure_count: int = 0
    results: list[RequestResult] = field(default_factory=list)
    hypothesis_confirmed: bool | None = None
    notes: str = ""


def _load_models_yaml() -> dict[str, Any]:
    """Load models.yaml without importing the full config stack."""
    import yaml

    config_path = Path(__file__).parents[2] / "config" / "models.yaml"
    with open(config_path) as f:
        return yaml.safe_load(f)


def _get_anthropic_key() -> str | None:
    """Get Anthropic API key from environment or project settings.

    Tries (in order): AGENT_ANTHROPIC_API_KEY, ANTHROPIC_API_KEY env vars,
    then falls back to the project's settings loader which reads .env files.
    """
    import os

    key = os.environ.get("AGENT_ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_API_KEY")
    if key:
        return key

    try:
        from personal_agent.config.settings import get_settings

        s = get_settings()
        if s.anthropic_api_key:
            return s.anthropic_api_key
    except Exception:
        pass

    return None


async def _request_local(
    endpoint: str,
    model_id: str,
    role: str,
    prompt: str,
    timeout_s: float = 60.0,
) -> RequestResult:
    """Send a single chat completion request to a local OpenAI-compatible server."""
    url = endpoint.rstrip("/")
    if url.endswith("/v1"):
        url = f"{url}/chat/completions"
    elif not url.endswith("/chat/completions"):
        url = f"{url}/v1/chat/completions"

    payload = {
        "model": model_id,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": 150,
        "temperature": 0.1,
    }

    start = time.monotonic()
    try:
        timeout_config = httpx.Timeout(connect=10.0, read=timeout_s, write=10.0, pool=10.0)
        async with httpx.AsyncClient(timeout=timeout_config, verify=False) as client:
            resp = await client.post(url, json=payload)
            latency_ms = int((time.monotonic() - start) * 1000)

            if resp.status_code == 400:
                return RequestResult(
                    role=role,
                    endpoint=endpoint,
                    model_id=model_id,
                    provider_type="local",
                    status="error_400",
                    http_status=400,
                    latency_ms=latency_ms,
                    error_message=resp.text[:500],
                )

            resp.raise_for_status()
            data = resp.json()

            content = ""
            choices = data.get("choices", [])
            if choices:
                content = choices[0].get("message", {}).get("content", "")

            usage = data.get("usage", {})
            return RequestResult(
                role=role,
                endpoint=endpoint,
                model_id=model_id,
                provider_type="local",
                status="success",
                http_status=resp.status_code,
                latency_ms=latency_ms,
                response_preview=content[:200] if content else None,
                tokens_prompt=usage.get("prompt_tokens", 0),
                tokens_completion=usage.get("completion_tokens", 0),
            )

    except httpx.TimeoutException:
        latency_ms = int((time.monotonic() - start) * 1000)
        return RequestResult(
            role=role,
            endpoint=endpoint,
            model_id=model_id,
            provider_type="local",
            status="error_timeout",
            latency_ms=latency_ms,
            error_message=f"Timed out after {timeout_s}s",
        )
    except httpx.HTTPStatusError as e:
        latency_ms = int((time.monotonic() - start) * 1000)
        return RequestResult(
            role=role,
            endpoint=endpoint,
            model_id=model_id,
            provider_type="local",
            status="error_other",
            http_status=e.response.status_code,
            latency_ms=latency_ms,
            error_message=str(e)[:500],
        )
    except Exception as e:
        latency_ms = int((time.monotonic() - start) * 1000)
        return RequestResult(
            role=role,
            endpoint=endpoint,
            model_id=model_id,
            provider_type="local",
            status="error_other",
            latency_ms=latency_ms,
            error_message=f"{type(e).__name__}: {e}",
        )


async def _request_cloud_anthropic(
    api_key: str,
    model: str,
    role: str,
    prompt: str,
    timeout_s: float = 60.0,
) -> RequestResult:
    """Send a single request to Anthropic's Messages API."""
    url = "https://api.anthropic.com/v1/messages"
    headers = {
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    payload = {
        "model": model,
        "max_tokens": 150,
        "messages": [{"role": "user", "content": prompt}],
    }

    start = time.monotonic()
    try:
        timeout_config = httpx.Timeout(connect=10.0, read=timeout_s, write=10.0, pool=10.0)
        async with httpx.AsyncClient(timeout=timeout_config) as client:
            resp = await client.post(url, json=payload, headers=headers)
            latency_ms = int((time.monotonic() - start) * 1000)

            if resp.status_code == 400:
                return RequestResult(
                    role=role,
                    endpoint="https://api.anthropic.com/v1",
                    model_id=model,
                    provider_type="cloud",
                    status="error_400",
                    http_status=400,
                    latency_ms=latency_ms,
                    error_message=resp.text[:500],
                )

            resp.raise_for_status()
            data = resp.json()

            content = ""
            for block in data.get("content", []):
                if block.get("type") == "text":
                    content += block.get("text", "")

            usage = data.get("usage", {})
            return RequestResult(
                role=role,
                endpoint="https://api.anthropic.com/v1",
                model_id=model,
                provider_type="cloud",
                status="success",
                http_status=resp.status_code,
                latency_ms=latency_ms,
                response_preview=content[:200] if content else None,
                tokens_prompt=usage.get("input_tokens", 0),
                tokens_completion=usage.get("output_tokens", 0),
            )

    except httpx.TimeoutException:
        latency_ms = int((time.monotonic() - start) * 1000)
        return RequestResult(
            role=role,
            endpoint="https://api.anthropic.com/v1",
            model_id=model,
            provider_type="cloud",
            status="error_timeout",
            latency_ms=latency_ms,
            error_message=f"Timed out after {timeout_s}s",
        )
    except httpx.HTTPStatusError as e:
        latency_ms = int((time.monotonic() - start) * 1000)
        return RequestResult(
            role=role,
            endpoint="https://api.anthropic.com/v1",
            model_id=model,
            provider_type="cloud",
            status="error_other",
            http_status=e.response.status_code,
            latency_ms=latency_ms,
            error_message=str(e)[:500],
        )
    except Exception as e:
        latency_ms = int((time.monotonic() - start) * 1000)
        return RequestResult(
            role=role,
            endpoint="https://api.anthropic.com/v1",
            model_id=model,
            provider_type="cloud",
            status="error_other",
            latency_ms=latency_ms,
            error_message=f"{type(e).__name__}: {e}",
        )


# ---------------------------------------------------------------------------
# Test 1: Local contention
# ---------------------------------------------------------------------------


async def test_local_contention(config: dict[str, Any]) -> TestResult:
    """Fire 3 concurrent requests at the local LM Studio endpoint.

    Simulates: router classification + response generation + background reflection
    all hitting the same single-GPU server simultaneously.
    """
    models = config.get("models", {})
    router_cfg = models.get("router", {})
    standard_cfg = models.get("standard", {})
    reasoning_cfg = models.get("reasoning", {})

    router_endpoint = router_cfg.get("endpoint", "http://127.0.0.1:1234/v1")
    standard_endpoint = standard_cfg.get("endpoint", "http://127.0.0.1:1234/v1")
    reasoning_endpoint = reasoning_cfg.get("endpoint", "http://127.0.0.1:1234/v1")

    print("\n" + "=" * 70)
    print("TEST 1: Local Inference Contention")
    print("=" * 70)
    print(f"  Router:    {router_cfg.get('id', '?')} @ {router_endpoint}")
    print(f"  Standard:  {standard_cfg.get('id', '?')} @ {standard_endpoint}")
    print(f"  Reasoning: {reasoning_cfg.get('id', '?')} @ {reasoning_endpoint}")
    print("  Firing 3 concurrent requests...")

    start = time.monotonic()
    results = await asyncio.gather(
        _request_local(
            router_endpoint, router_cfg.get("id", ""), "router", SIMPLE_PROMPT, timeout_s=30.0
        ),
        _request_local(
            standard_endpoint, standard_cfg.get("id", ""), "standard", SIMPLE_PROMPT, timeout_s=60.0
        ),
        _request_local(
            reasoning_endpoint,
            reasoning_cfg.get("id", ""),
            "reasoning",
            REASONING_PROMPT,
            timeout_s=90.0,
        ),
    )
    total_ms = int((time.monotonic() - start) * 1000)

    successes = sum(1 for r in results if r.status == "success")
    failures = len(results) - successes

    test_result = TestResult(
        test_name="local_contention",
        description="3 concurrent requests to local LM Studio (router + standard + reasoning)",
        concurrent_requests=3,
        total_duration_ms=total_ms,
        success_count=successes,
        failure_count=failures,
        results=list(results),
        hypothesis_confirmed=failures > 0,
        notes=(
            "CONFIRMED: Local contention causes failures"
            if failures > 0
            else "UNEXPECTED: All requests succeeded — server may support queuing"
        ),
    )

    _print_test_summary(test_result)
    return test_result


# ---------------------------------------------------------------------------
# Test 1b: Local contention under heavier load
# ---------------------------------------------------------------------------


async def test_local_contention_heavy(config: dict[str, Any]) -> TestResult:
    """Fire 5 concurrent requests at the local LM Studio endpoint.

    Heavier variant of Test 1: simulates router + response + reflection +
    entity extraction + a second user request arriving simultaneously.
    Uses longer prompts to increase GPU time per request.
    """
    models = config.get("models", {})
    router_cfg = models.get("router", {})
    standard_cfg = models.get("standard", {})
    reasoning_cfg = models.get("reasoning", {})

    router_endpoint = router_cfg.get("endpoint", "http://127.0.0.1:1234/v1")
    standard_endpoint = standard_cfg.get("endpoint", "http://127.0.0.1:1234/v1")
    reasoning_endpoint = reasoning_cfg.get("endpoint", "http://127.0.0.1:1234/v1")

    long_prompt = (
        "Write a detailed comparison of Python and Rust for systems programming. "
        "Cover memory safety, performance, ecosystem, learning curve, and concurrency models. "
        "Provide specific examples for each point."
    )

    print("\n" + "=" * 70)
    print("TEST 1b: Local Inference Contention (Heavy — 5 concurrent)")
    print("=" * 70)
    print(f"  Router:    {router_cfg.get('id', '?')} @ {router_endpoint}")
    print(f"  Standard:  {standard_cfg.get('id', '?')} @ {standard_endpoint}")
    print(f"  Reasoning: {reasoning_cfg.get('id', '?')} @ {reasoning_endpoint}")
    print("  Firing 5 concurrent requests (longer prompts)...")

    start = time.monotonic()
    results = await asyncio.gather(
        _request_local(
            router_endpoint, router_cfg.get("id", ""), "router", SIMPLE_PROMPT, timeout_s=30.0
        ),
        _request_local(
            standard_endpoint, standard_cfg.get("id", ""), "standard_1", long_prompt, timeout_s=60.0
        ),
        _request_local(
            reasoning_endpoint,
            reasoning_cfg.get("id", ""),
            "reasoning_1",
            long_prompt,
            timeout_s=90.0,
        ),
        _request_local(
            standard_endpoint,
            standard_cfg.get("id", ""),
            "standard_2",
            REASONING_PROMPT,
            timeout_s=60.0,
        ),
        _request_local(
            reasoning_endpoint,
            reasoning_cfg.get("id", ""),
            "reasoning_2",
            REASONING_PROMPT,
            timeout_s=90.0,
        ),
    )
    total_ms = int((time.monotonic() - start) * 1000)

    successes = sum(1 for r in results if r.status == "success")
    failures = len(results) - successes

    # Check for queuing behavior: if all succeed but latencies show staircase pattern
    max_latency = max(r.latency_ms for r in results)
    min_latency = min(r.latency_ms for r in results)
    queuing_detected = (max_latency > 3 * min_latency) and successes == len(results)

    notes_parts = []
    if failures > 0:
        notes_parts.append(f"CONFIRMED: {failures} failures under heavy load")
    elif queuing_detected:
        notes_parts.append(
            f"QUEUING DETECTED: All succeeded but latency spread "
            f"{min_latency}ms-{max_latency}ms ({max_latency / max(min_latency, 1):.1f}x) "
            f"indicates serial processing"
        )
    else:
        notes_parts.append("All requests succeeded with similar latencies")

    test_result = TestResult(
        test_name="local_contention_heavy",
        description="5 concurrent requests to local LM Studio (heavy load, longer prompts)",
        concurrent_requests=5,
        total_duration_ms=total_ms,
        success_count=successes,
        failure_count=failures,
        results=list(results),
        hypothesis_confirmed=failures > 0 or queuing_detected,
        notes=" | ".join(notes_parts),
    )

    _print_test_summary(test_result)
    return test_result


# ---------------------------------------------------------------------------
# Test 2: Cloud provider comparison
# ---------------------------------------------------------------------------


async def test_cloud_comparison(
    api_key: str, cloud_model: str = "claude-haiku-4-5"
) -> TestResult:
    """Fire 3 concurrent requests at Anthropic's Claude API.

    Same workload pattern as Test 1, but targeting a cloud provider that
    handles concurrency server-side.
    """
    print("\n" + "=" * 70)
    print("TEST 2: Cloud Provider (Anthropic Claude) Concurrency")
    print("=" * 70)
    print(f"  Model: {cloud_model}")
    print("  Firing 3 concurrent requests...")

    start = time.monotonic()
    results = await asyncio.gather(
        _request_cloud_anthropic(
            api_key, cloud_model, "router_equivalent", SIMPLE_PROMPT, timeout_s=30.0
        ),
        _request_cloud_anthropic(
            api_key, cloud_model, "standard_equivalent", SIMPLE_PROMPT, timeout_s=60.0
        ),
        _request_cloud_anthropic(
            api_key, cloud_model, "reasoning_equivalent", REASONING_PROMPT, timeout_s=90.0
        ),
    )
    total_ms = int((time.monotonic() - start) * 1000)

    successes = sum(1 for r in results if r.status == "success")
    failures = len(results) - successes

    test_result = TestResult(
        test_name="cloud_comparison",
        description=f"3 concurrent requests to Anthropic Claude ({cloud_model})",
        concurrent_requests=3,
        total_duration_ms=total_ms,
        success_count=successes,
        failure_count=failures,
        results=list(results),
        hypothesis_confirmed=successes == 3,
        notes=(
            "CONFIRMED: Cloud provider handles all concurrent requests"
            if successes == 3
            else f"UNEXPECTED: {failures} failures on cloud provider"
        ),
    )

    _print_test_summary(test_result)
    return test_result


# ---------------------------------------------------------------------------
# Test 3: Mixed mode
# ---------------------------------------------------------------------------


async def test_mixed_mode(
    config: dict[str, Any],
    api_key: str,
    cloud_model: str = "claude-haiku-4-5",
) -> TestResult:
    """Test mixed local/cloud: router on local, reasoning on cloud, standard on local.

    Validates that the ADR-0029 concurrency controller's provider_type-aware
    behavior works correctly in a mixed deployment.
    """
    models = config.get("models", {})
    router_cfg = models.get("router", {})
    standard_cfg = models.get("standard", {})

    router_endpoint = router_cfg.get("endpoint", "http://127.0.0.1:1234/v1")
    standard_endpoint = standard_cfg.get("endpoint", "http://127.0.0.1:1234/v1")

    print("\n" + "=" * 70)
    print("TEST 3: Mixed Mode (Local Router + Cloud Reasoning + Local Standard)")
    print("=" * 70)
    print(f"  Router (local):    {router_cfg.get('id', '?')} @ {router_endpoint}")
    print(f"  Reasoning (cloud): {cloud_model} @ api.anthropic.com")
    print(f"  Standard (local):  {standard_cfg.get('id', '?')} @ {standard_endpoint}")
    print("  Firing 3 concurrent requests...")

    start = time.monotonic()
    results = await asyncio.gather(
        _request_local(
            router_endpoint, router_cfg.get("id", ""), "router", SIMPLE_PROMPT, timeout_s=30.0
        ),
        _request_cloud_anthropic(
            api_key, cloud_model, "reasoning", REASONING_PROMPT, timeout_s=60.0
        ),
        _request_local(
            standard_endpoint, standard_cfg.get("id", ""), "standard", SIMPLE_PROMPT, timeout_s=60.0
        ),
    )
    total_ms = int((time.monotonic() - start) * 1000)

    successes = sum(1 for r in results if r.status == "success")
    failures = len(results) - successes

    # In mixed mode, the cloud request should always succeed.
    # Local requests may contend with each other but there are only 2 of them
    # (router is a small model, standard is medium) so contention is lower.
    cloud_result = results[1]
    local_results = [results[0], results[2]]
    local_successes = sum(1 for r in local_results if r.status == "success")

    test_result = TestResult(
        test_name="mixed_mode",
        description="Mixed: local router + cloud reasoning + local standard (concurrent)",
        concurrent_requests=3,
        total_duration_ms=total_ms,
        success_count=successes,
        failure_count=failures,
        results=list(results),
        hypothesis_confirmed=(cloud_result.status == "success"),
        notes=(
            f"Cloud reasoning: {cloud_result.status} ({cloud_result.latency_ms}ms). "
            f"Local: {local_successes}/2 succeeded. "
            + (
                "Mixed mode viable — cloud offloads heavy reasoning."
                if cloud_result.status == "success" and local_successes >= 1
                else "Issues detected in mixed mode."
            )
        ),
    )

    _print_test_summary(test_result)
    return test_result


# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------


def _print_test_summary(result: TestResult) -> None:
    """Print a human-readable summary of a test result."""
    print(f"\n  Results ({result.total_duration_ms}ms total):")
    for r in result.results:
        status_icon = "\u2705" if r.status == "success" else "\u274c"
        print(
            f"    {status_icon} {r.role:20s} | {r.status:15s} | "
            f"{r.latency_ms:6d}ms | HTTP {r.http_status or '---'}"
        )
        if r.error_message:
            print(f"       Error: {r.error_message[:120]}")
        if r.response_preview:
            print(f"       Response: {r.response_preview[:100]}...")

    print(f"\n  Success: {result.success_count}/{result.concurrent_requests}")
    print(f"  Hypothesis confirmed: {result.hypothesis_confirmed}")
    print(f"  Notes: {result.notes}")


def _save_results(all_results: list[TestResult]) -> Path:
    """Save results as JSON to the results directory."""
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    output_path = RESULTS_DIR / f"experiment-{timestamp}.json"

    serializable = []
    for tr in all_results:
        d = asdict(tr)
        serializable.append(d)

    with open(output_path, "w") as f:
        json.dump(
            {
                "experiment": "FRE-103: Local vs Cloud Inference Concurrency",
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "tests": serializable,
            },
            f,
            indent=2,
        )

    print(f"\nResults saved to: {output_path}")
    return output_path


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    """Run the FRE-103 concurrency experiment from the command line."""
    parser = argparse.ArgumentParser(
        description="FRE-103: Local vs Cloud inference concurrency experiment"
    )
    parser.add_argument(
        "--test",
        choices=["local", "local-heavy", "cloud", "mixed", "all"],
        default="all",
        help="Which test to run (default: all)",
    )
    parser.add_argument(
        "--cloud-model",
        default="claude-haiku-4-5",
        help="Anthropic model for cloud tests (default: claude-haiku-4-5)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print configuration and exit without making requests",
    )
    args = parser.parse_args()

    config = _load_models_yaml()
    api_key = _get_anthropic_key()

    print("FRE-103: Local vs Cloud Inference Concurrency Experiment")
    print("=" * 70)
    print(f"Timestamp: {datetime.now(timezone.utc).isoformat()}")
    print(f"Test:      {args.test}")
    print(f"Cloud model: {args.cloud_model}")
    print(f"API key:   {'configured' if api_key else 'NOT SET'}")

    if args.dry_run:
        print("\n[DRY RUN] Configuration loaded, models.yaml parsed. Exiting.")
        models = config.get("models", {})
        for role, cfg in models.items():
            print(
                f"  {role}: {cfg.get('id')} @ {cfg.get('endpoint', 'default')} "
                f"(provider_type={cfg.get('provider_type', 'auto')})"
            )
        return

    needs_cloud = args.test in ("cloud", "mixed", "all")
    if needs_cloud and not api_key:
        print(
            "\nERROR: Cloud tests require AGENT_ANTHROPIC_API_KEY or ANTHROPIC_API_KEY "
            "environment variable (or set in .env). Use --test local to skip cloud tests."
        )
        sys.exit(1)

    all_results: list[TestResult] = []

    if args.test in ("local", "all"):
        result = asyncio.run(test_local_contention(config))
        all_results.append(result)

    if args.test in ("local-heavy", "all"):
        result = asyncio.run(test_local_contention_heavy(config))
        all_results.append(result)

    if args.test in ("cloud", "all"):
        assert api_key is not None
        result = asyncio.run(test_cloud_comparison(api_key, args.cloud_model))
        all_results.append(result)

    if args.test in ("mixed", "all"):
        assert api_key is not None
        result = asyncio.run(test_mixed_mode(config, api_key, args.cloud_model))
        all_results.append(result)

    # Save results
    if all_results:
        output_path = _save_results(all_results)

        # Print overall summary
        print("\n" + "=" * 70)
        print("EXPERIMENT SUMMARY")
        print("=" * 70)
        for tr in all_results:
            icon = "\u2705" if tr.hypothesis_confirmed else "\u274c"
            print(
                f"  {icon} {tr.test_name:20s} | "
                f"{tr.success_count}/{tr.concurrent_requests} succeeded | "
                f"{tr.total_duration_ms}ms | hypothesis={'confirmed' if tr.hypothesis_confirmed else 'not confirmed'}"
            )

        print(f"\nFull results: {output_path}")


if __name__ == "__main__":
    main()
