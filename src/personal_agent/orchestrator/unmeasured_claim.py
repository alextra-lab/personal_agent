"""FRE-1417 — flag a zero-tool-call sub-agent that makes a performance claim.

A component that executed no tools cannot have measured anything (the
2026-09-05 median-benchmark incident: four sub-agents made zero tool calls,
and one of them reported which of three unexecuted algorithms was fastest).
This is deliberately the *structural* half only: it checks the exact
``SubAgentCapture``/``SubAgentResult`` ground truth (``tools_used``), and a
fixed, non-semantic keyword/regex pattern on the output text — never a
wall-clock magnitude comparison, which needs prose understanding and is out
of scope by design (see the ticket).
"""

from __future__ import annotations

import re
from collections.abc import Sequence

#: A number with a duration unit, a comparative/superlative speed word, a
#: multiplier ("2x faster"), a latency/throughput mention, or a rate/percentage
#: performance claim. Matched per-sentence, never across the whole output —
#: two unrelated sentences must not combine into a false claim.
_MEASUREMENT_CLAIM_RE = re.compile(
    r"\b\d[\d,]*(?:\.\d+)?\s*(?:ms|milliseconds?|µs|us|microseconds?|ns|nanoseconds?|"
    r"secs?|seconds?|mins?|minutes?)\b"
    r"|\b(fastest|slowest|faster|slower|quicker|outperform(?:s|ed|ing)?|speedup)\b"
    r"|\b\d+(?:\.\d+)?\s*[x×]\s*(?:faster|slower|as fast)\b"
    r"|\b(?:latency|throughput)\b"
    r"|\b\d+(?:\.\d+)?\s*(?:requests?|ops|operations)\s*/\s*(?:sec|second)\b"
    r"|\b\d+(?:\.\d+)?\s*%\s*(?:faster|slower|longer|quicker)\b",
    re.IGNORECASE,
)

#: Restricts the claim to a computational/algorithmic context, so an
#: unrelated domain (travel time, cleaning instructions, sports pace) using
#: the same comparative words never matches — measured empirically at 0/103
#: false positives against real production sub-agent output (see
#: tests/personal_agent/orchestrator/test_unmeasured_claim.py and
#: scripts/research/fre1417_unmeasured_claim_probe.py).
_COMPUTE_CONTEXT_RE = re.compile(
    r"\b(algorithm(?:ic)?s?|complexity|big[- ]?o|O\(\s*[nN]|runtime|execution time|"
    r"sort(?:ing)?|hash(?:ing)?|numpy|quickselect|median|cache|constant factors?|"
    r"time complexity)\b",
    re.IGNORECASE,
)

#: An honest "I did not measure this" statement must not be flagged — the
#: invariant is about claiming a result without execution, not about naming
#: the concepts involved.
_HEDGE_RE = re.compile(
    r"\b(cannot determine|can't determine|without (?:running|benchmarking|measuring|executing)|"
    r"would need to (?:run|benchmark|measure)|not (?:sure|certain) which|"
    r"difficult to (?:say|determine|know)|hard to (?:say|determine|know)|"
    r"can't say|cannot say|unable to (?:determine|say|measure))\b",
    re.IGNORECASE,
)

_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+|\n+")


def detect_unmeasured_claim(tools_used: Sequence[str], full_output: str) -> bool:
    """Flag a sub-agent that reports a performance result but ran no tool.

    The precondition (``tools_used`` empty) is the structural fact from
    ``SubAgentCapture``/``SubAgentResult`` — exact, no inference. The text
    check is a fixed heuristic, not prose understanding: a computational
    performance claim (a duration, a comparative/superlative speed word, a
    rate) must appear in the same sentence as a computational-context word,
    and the sentence must not also hedge ("cannot determine ... without
    running it").

    Args:
        tools_used: Tool names the component actually invoked, per its own
            capture record.
        full_output: The component's complete response text.

    Returns:
        True if the component made a computational performance claim while
        its own record shows zero tool executions.
    """
    if tools_used:
        return False
    for sentence in _SENTENCE_SPLIT_RE.split(full_output):
        if not sentence.strip():
            continue
        if _HEDGE_RE.search(sentence):
            continue
        if _MEASUREMENT_CLAIM_RE.search(sentence) and _COMPUTE_CONTEXT_RE.search(sentence):
            return True
    return False
