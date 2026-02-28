"""E-018: Run baseline vs LangExtract treatment for entity extraction.

Usage (from repo root):
    uv run python -m experiments.langextract_evaluation.run_comparison

Prerequisites:
    - Local LLM running (same as entity_extraction config)
    - Optional: LANGEXTRACT_DATASET_PATH pointing to JSONL with user_message, assistant_response

Output:
    - Prints parse rate and latency summary for baseline (and treatment when implemented)
    - Use results to fill docs/architecture_decisions/experiments/E-018-langextract-results.md
"""

import asyncio
import json
import os
import time

# Baseline: current implementation
from personal_agent.second_brain.entity_extraction import (
    extract_entities_and_relationships,
)


def _load_dataset(path: str | None = None) -> list[tuple[str, str]]:
    """Load conversation pairs for evaluation.

    Each line: JSON with user_message, assistant_response.
    If path is None or missing, returns a minimal in-memory sample for structure testing.
    """
    if path and os.path.isfile(path):
        pairs: list[tuple[str, str]] = []
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                obj = json.loads(line)
                pairs.append(
                    (
                        obj.get("user_message", ""),
                        obj.get("assistant_response", ""),
                    )
                )
        return pairs
    # Minimal sample for CI/structure testing (no LLM required if skipped)
    return [
        (
            "What is Python?",
            "Python is a high-level programming language known for readability.",
        ),
        (
            "Explain quantum entanglement.",
            "Quantum entanglement is a phenomenon where particles remain correlated.",
        ),
    ]


async def _run_baseline(pairs: list[tuple[str, str]]) -> dict[str, object]:
    """Run current entity extraction on dataset; return metrics."""
    n = len(pairs)
    parse_ok = 0
    latencies_ms: list[float] = []

    for user_msg, assistant_msg in pairs:
        start = time.perf_counter()
        try:
            result = await extract_entities_and_relationships(user_msg, assistant_msg or "")
            elapsed_ms = (time.perf_counter() - start) * 1000
            latencies_ms.append(elapsed_ms)
            # Consider parse success if we got expected keys and entities list
            if isinstance(result.get("entities"), list):
                parse_ok += 1
        except Exception:
            latencies_ms.append(-1.0)  # failed

    latencies_ms = [x for x in latencies_ms if x >= 0]
    p50 = float("nan")
    p95 = float("nan")
    if latencies_ms:
        sorted_ms = sorted(latencies_ms)
        p50 = sorted_ms[min(len(sorted_ms) // 2, len(sorted_ms) - 1)]
        p95 = sorted_ms[min(int(len(sorted_ms) * 0.95), len(sorted_ms) - 1)]

    return {
        "n": n,
        "parse_success": parse_ok,
        "parse_rate": parse_ok / n if n else 0.0,
        "latency_p50_ms": p50,
        "latency_p95_ms": p95,
        "latency_count": len(latencies_ms),
    }


async def _run_treatment(_pairs: list[tuple[str, str]]) -> dict[str, object]:
    """Run LangExtract-based extraction (same schema). Stub until LangExtract is integrated.

    When implementing:
    1. Define schema matching current output (summary, entities, relationships).
    2. Call LangExtract extract; measure parse success and latency.
    3. Return same shape as _run_baseline() for comparison.
    """
    # Stub: return placeholder so comparison loop runs
    return {
        "n": len(_pairs),
        "parse_success": 0,
        "parse_rate": 0.0,
        "latency_p50_ms": float("nan"),
        "latency_p95_ms": float("nan"),
        "latency_count": 0,
        "note": "LangExtract treatment not yet implemented",
    }


async def main() -> None:
    dataset_path = os.environ.get("LANGEXTRACT_DATASET_PATH")
    pairs = _load_dataset(dataset_path)
    print(f"Dataset: {len(pairs)} conversation pairs")
    if not pairs:
        print("No pairs to run. Set LANGEXTRACT_DATASET_PATH or use in-memory sample.")
        return

    print("Running baseline (current entity extraction)...")
    baseline = await _run_baseline(pairs)
    print("Baseline:", json.dumps(baseline, indent=2))

    print("Running treatment (LangExtract)...")
    treatment = await _run_treatment(pairs)
    print("Treatment:", json.dumps(treatment, indent=2))

    # Summary for E-018
    print("\n--- E-018 summary ---")
    print(
        f"Parse rate: baseline={baseline['parse_rate']:.2%}, treatment={treatment['parse_rate']:.2%}"
    )
    print(
        f"P95 latency (ms): baseline={baseline['latency_p95_ms']}, treatment={treatment['latency_p95_ms']}"
    )
    print(
        "Document results in docs/architecture_decisions/experiments/E-018-langextract-results.md"
    )


if __name__ == "__main__":
    asyncio.run(main())
