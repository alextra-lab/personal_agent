"""Tests for FRE-1417's zero-tool-call performance-claim detector.

AC-5 evidence (measured, not assumed): ``detect_unmeasured_claim`` was run
against 103 real production ``SubAgentCapture`` outputs (``tools_used=[]``,
``full_output`` longer than 30 chars, most recent ~300 sub-agent captures as
of 2026-09-06). It raised 5 alerts. All 5 were manually read and confirmed
true positives — the same unexecuted-algorithm-comparison fabrication family
as AC-1's incident (three of the five belong to two other occurrences of the
identical median-benchmark prompt). That is 0 false positives among the
alerts (0/5) and 0 false positives across the full reviewed corpus (0/103).
The remaining 98 non-alerted outputs span unrelated domains — cleaning
instructions, travel planning, sports analysis, home maintenance, a health
note, cost dashboards — none of which false-positived, because
``_COMPUTE_CONTEXT_RE`` requires an algorithm/complexity/hashing/sorting term
in the same sentence as the performance claim. The raw corpus is not
committed here (it is the owner's real usage and touches personal content);
``scripts/research/fre1417_unmeasured_claim_probe.py`` reproduces the alert
*counts* against live data without ever printing ``full_output``. Human
adjudication of what an alert means is not automated and is not reproduced
by the script.
"""

from __future__ import annotations

import hashlib

from personal_agent.orchestrator.unmeasured_claim import detect_unmeasured_claim

# Real captures, verbatim from production Elasticsearch (SUBAGENT_CAPTURES_INDEX_PREFIX),
# fetched read-only in the build session on 2026-09-06. Each sha256 lets a reviewer
# confirm the text below matches the live record via
# scripts/research/fre1417_unmeasured_claim_probe.py replay.

# trace_id=91f974ff50b843f84c56bf80532bbec1 task_id=657c9160-971e-4bb5-815f-918400538e29
# The 2026-09-05 incident's "compare the execution times" sub-agent: zero tool calls,
# reports which of three unexecuted median algorithms is fastest.
_AC1_FABRICATED_COMPARISON = (
    "### Comparative Analysis of Median Computation Methods\n\n"
    "**1. Sorting-Based Approach (`sorted(list)[n//2]`)**\n"
    "*   **Method:** Sorts the entire list using Timsort (O(n log n)) and selects the "
    "middle element.\n"
    "*   **Performance:** High memory overhead due to creating a sorted copy or "
    "modifying the original in-place. The time complexity is dominated by the sorting "
    "algorithm, making it inefficient for very large datasets where only the median is "
    "required.\n"
    "*   **Speed:** Slowest of the three for large lists.\n\n"
    "**2. `statistics.median()` (Python Standard Library)**\n"
    "*   **Method:** Similar to the sorting approach, it typically sorts a copy of the "
    "data to find the middle value(s).\n"
    "*   **Performance:** Includes overhead from function calls and type checking "
    "within the standard library. It is optimized for correctness and ease of use "
    "rather than raw computational speed on large arrays.\n"
    "*   **Speed:** Slower than optimized NumPy implementations; comparable or "
    "slightly slower than manual sorting due to library overhead.\n\n"
    "**3. `numpy.median()` (NumPy)**\n"
    "*   **Method:** Uses an optimized selection algorithm (based on Introselect) "
    "which is O(n) on average, or a highly optimized sort for small arrays. It "
    "operates on contiguous blocks of memory in C, avoiding Python loop overhead.\n"
    "*   **Performance:** Minimal memory overhead when using views; leverages "
    "low-level optimizations and SIMD instructions. For large lists, the linear time "
    "complexity and compiled code execution provide significant speed advantages.\n"
    "*   **Speed:** Fastest method by a wide margin for large datasets.\n\n"
    "### Conclusion\n\n"
    "**Fastest Method:** `numpy.median()`\n\n"
    "**Summary:** For large lists of numbers, `numpy.median()` is significantly "
    "faster than sorting-based approaches or the standard library `statistics."
    "median()`. This is due to its O(n) average time complexity and optimized "
    "C-level implementation, whereas the other methods rely on O(n log n) sorting "
    "algorithms with higher Python-level overhead."
)
assert (
    hashlib.sha256(_AC1_FABRICATED_COMPARISON.encode()).hexdigest()
    == "41b174eee01ac9be42e45bdd7346a361b1ebd8f18ea0c3157b83a9ae970a7095"
)

# trace_id=6f6628b3ff0cfd75b7a604add9c1ed9e task_id=512b25f4-2517-40ff-b070-3cb9b0609f60
# tools_used=["run_python"]. A genuine hashing benchmark that DID run — reports the
# same "fastest"/"slowest" shape of claim as AC-1, but backed by an actual tool call.
_AC2_DIGEST_WITH_TOOL = (
    "SHA-256: 4185ff4d76385012623d497036d9b7b94c940f2a76bf32c46e05a69ebb8fc415\n"
    "SHA-512: ee349f3211794eb6983b55242d052bcfb60c3ec8ed4ad6efdea1ff9ccb022803fe11845"
    "ccb72db88af7814458257e6cb636249828a212a838c36f04776cf695d\n"
    "BLAKE2b-256: 9396fc445604ba52d2f6d34543d174f44f29eb949c33b733c704020789c747e9\n\n"
    "Speed: SHA-512 was fastest (~14.0 µs/hash), narrowly ahead of SHA-256 (~14.6 "
    "µs/hash); BLAKE2b-256 was slowest (~16.9 µs/hash) in this Python `hashlib` "
    "benchmark. Note that for such a short input, timings are dominated by per-call "
    "overhead and may vary by hardware; on typical 64-bit CPUs with hashing "
    "extensions, BLAKE2b is often fastest at larger data sizes."
)
assert (
    hashlib.sha256(_AC2_DIGEST_WITH_TOOL.encode()).hexdigest()
    == "0439f34206c3e9ce246202c0ceb33f5d948a0480eccbb6e0ffd7361bb426c1f5"
)

# trace_id=6f6628b3ff0cfd75b7a604add9c1ed9e task_id=480563cb-8c9e-424e-b6b4-ec7ff702f59e
# tools_used=["run_python"]. Same turn's benchmark sub-agent — also ran a tool.
_AC2_BENCHMARK_WITH_TOOL = (
    "**Digests of `seshat-sub-1788634125-54f5adc59645`:**\n"
    "- SHA-256: `4185ff4d76385012623d497036d9b7b94c940f2a76bf32c46e05a69ebb8fc415`\n"
    "- SHA-512: `ee349f3211794eb6983b55242d052bcfb60c3ec8ed4ad6efdea1ff9ccb022803fe11845"
    "ccb72db88af7814458257e6cb636249828a212a838c36f04776cf695d`\n"
    "- BLAKE2b-256: `9396fc445604ba52d2f6d34543d174f44f29eb949c33b733c704020789c747e9`\n\n"
    "**Speed benchmark (100 MB input, 7 iterations, median ms/MB) — fastest to "
    "slowest:**\n"
    "1. BLAKE2b-256: 1.410 ms/MB\n"
    "2. SHA-512: 1.533 ms/MB\n"
    "3. SHA-256: 2.299 ms/MB\n\n"
    "**Fastest algorithm: BLAKE2b-256** (~1.6× faster than SHA-256 on this 64-bit "
    "platform)."
)
assert (
    hashlib.sha256(_AC2_BENCHMARK_WITH_TOOL.encode()).hexdigest()
    == "98ebde3ece05c5d56c8803dcdbf5fc3748f72253a66f3aaaafd201f37c7c787c"
)


class TestAC1RealFabricationIsFlagged:
    """AC-1: replayed from the incident's own capture, not a synthetic fixture."""

    def test_zero_tool_comparison_is_flagged(self) -> None:
        assert detect_unmeasured_claim([], _AC1_FABRICATED_COMPARISON) is True


class TestAC2ToolBackedClaimIsNotFlagged:
    """AC-2: a sub-agent that ran a tool must not be flagged, even though its
    text makes the same shape of speed claim as the fabrication.
    """

    def test_digest_sub_agent_not_flagged(self) -> None:
        assert detect_unmeasured_claim(["run_python"], _AC2_DIGEST_WITH_TOOL) is False

    def test_benchmark_sub_agent_not_flagged(self) -> None:
        assert detect_unmeasured_claim(["run_python"], _AC2_BENCHMARK_WITH_TOOL) is False

    def test_same_text_would_be_flagged_without_the_tool(self) -> None:
        """Proves the tools_used gate, not an accidental text mismatch, is what
        saves these two — the text alone is claim-shaped.
        """
        assert detect_unmeasured_claim([], _AC2_DIGEST_WITH_TOOL) is True
        assert detect_unmeasured_claim([], _AC2_BENCHMARK_WITH_TOOL) is True


class TestAC3ZeroToolsAloneIsNotSufficient:
    """AC-3: running nothing is normal for synthesis; only a claim on top of it
    is flagged.
    """

    def test_real_empty_synthesis_output_not_flagged(self) -> None:
        """trace_id=6f6628b3ff0cfd75b7a604add9c1ed9e
        task_id=79b33477-5ae8-4620-9fcc-25a41e524c33 — the same 18:51 turn's
        synthesis sub-agent returned empty output (0 tokens generated) in this
        capture. Trivial (nothing can match empty text), kept as the real
        record; the next test exercises the actual boundary.
        """
        assert detect_unmeasured_claim([], "") is False

    def test_synthesis_without_a_claim_not_flagged(self) -> None:
        """Synthetic — the real capture above was empty, so it does not exercise
        the "zero tools + non-empty output + no claim" boundary. Modeled on a
        combine/synthesis task that reports facts without a speed claim.
        """
        text = (
            "Both digests were computed successfully:\n"
            "- SHA-256: 4185ff4d76385012623d497036d9b7b94c940f2a76bf32c46e05a69ebb8fc415\n"
            "- SHA-512: ee349f3211794eb6983b55242d052bcfb60c3ec8ed4ad6efdea1ff9ccb022803fe\n\n"
            "Combining the two reports above into a single table for the user."
        )
        assert detect_unmeasured_claim([], text) is False

    def test_compute_context_without_measurement_claim_not_flagged(self) -> None:
        """Naming the algorithm/complexity is not itself a claim of measurement."""
        text = (
            "This implementation uses the sorting algorithm with O(n log n) time "
            "complexity, as requested."
        )
        assert detect_unmeasured_claim([], text) is False

    def test_measurement_language_without_compute_context_not_flagged(self) -> None:
        """A speed/duration claim outside any computational context is not this
        check's target — it needs both conditions.
        """
        text = "The connecting flight is about 45 minutes faster via the northern route."
        assert detect_unmeasured_claim([], text) is False

    def test_honest_negation_not_flagged(self) -> None:
        """An explicit "I did not measure this" statement must not be flagged —
        the invariant is about claiming a result, not naming the concepts.
        """
        text = (
            "I cannot determine which sorting algorithm is fastest without running "
            "a benchmark; that would require executing each implementation."
        )
        assert detect_unmeasured_claim([], text) is False


class TestPositiveShapesBeyondTheIncident:
    """The detector must not depend on the incident's exact wording."""

    def test_numeric_timing_claim_without_comparative_words(self) -> None:
        text = "Sorting the list took 450 ms to complete using the built-in algorithm."
        assert detect_unmeasured_claim([], text) is True

    def test_comparative_claim_without_digits(self) -> None:
        text = "Quickselect significantly outperforms the sorting-based algorithm."
        assert detect_unmeasured_claim([], text) is True


class TestAC5FalsePositiveRateOnUnrelatedDomains:
    """AC-5 regression coverage — hand-authored, non-PII examples in the same
    shape as the unrelated-domain outputs observed in the real corpus (see
    module docstring for the live-measured 0/103 figure this coverage guards).
    """

    _BENIGN_EXAMPLES = [
        "Let it soak for 10-15 minutes before wiping with a clean cloth.",
        "The villa is within 30-45 minutes of the nearest beach by car.",
        "A faster pace of play favors high-scoring offenses in this matchup.",
        "Weekly expert consensus rankings serve as a benchmark against your model.",
        "Run the cold tap for 5 minutes and check whether the smell clears.",
        "Consider offloading non-urgent jobs to cheaper, slower inference engines.",
        "The lesion is described as slower-progressing than a high-grade one.",
        "Reclaiming wasted disk space can improve I/O throughput over time.",
        "This ancient grain has a slower digestion profile than refined wheat.",
        "The itinerary favors a slower pace of sightseeing over long drives.",
        "Add an index to reduce query latency on the reporting dashboard.",
        "The report lists three action items, none of which mention timing.",
    ]

    def test_no_benign_example_is_flagged(self) -> None:
        false_positives = [
            text for text in self._BENIGN_EXAMPLES if detect_unmeasured_claim([], text)
        ]
        assert false_positives == []
