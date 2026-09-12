"""ADR-0150 D1's full-fill probe: ``worker_report_v1`` at every declared ceiling.

The schema's ``maxLength``/``maxItems`` values bound characters, not tokens —
JSON escaping doubles a quote, and a non-ASCII character can cost several
tokens — so "worst case" is defined by this fixture rather than a formula.
Every array is filled to ``maxItems`` and every string to ``maxLength``, built
from one fixed English paragraph (repeated and cut to length) with the
sequence ``\\"`` substituted at every 100th character, so the escaping the
schema's worst case actually produces is exercised, not just the raw
character count.

T3's per-deployment token counts (this ticket's close comment) are taken
against the serialized JSON committed beside this module
(``worker_report_full_fill.json``), not a fresh call to
:func:`build_full_fill_report`, so the fixture and the counts stay pinned
together. Regenerate the committed JSON with::

    uv run python -m tests.personal_agent.orchestrator.fixtures.worker_report_full_fill

**Live measurement not run in this session.** The token counts per deployment
(``qwen3.8-flash-next`` via llama-server ``/tokenize``, the OVH deployment via
``usage.prompt_tokens`` on a probe call) and the landing-ceiling reading
require the local SLM tunnel / OVH credentials, neither reachable from this
build session. See the ticket's handoff comment.
"""

from __future__ import annotations

import json
from pathlib import Path

from personal_agent.orchestrator.worker_types import Finding, Gap, WorkerReport

_PARAGRAPH = (
    "The committee convened at the appointed hour to review the proposal "
    "submitted by the working group, weighing its costs against the benefits "
    "described in the accompanying report and the testimony offered by each "
    "of the invited witnesses who had travelled from neighbouring districts. "
)

#: Substitute a literal double quote at every 100th character (1-indexed), so
#: the fixture exercises JSON's own escaping (`"` -> `\"`) at its worst case.
_QUOTE_EVERY = 100


def _filled(length: int) -> str:
    """Repeat ``_PARAGRAPH`` to exactly ``length`` characters, quoting every Nth.

    Args:
        length: Target character count.

    Returns:
        A string of exactly ``length`` characters.
    """
    if length <= 0:
        return ""
    repeats = length // len(_PARAGRAPH) + 1
    text = list((_PARAGRAPH * repeats)[:length])
    for i in range(_QUOTE_EVERY - 1, length, _QUOTE_EVERY):
        text[i] = '"'
    return "".join(text)


def build_full_fill_report() -> WorkerReport:
    """Build a ``worker_report_v1`` report at every declared ceiling (ADR-0150 D1).

    Returns:
        A validated report with 20 findings, 10 gaps, and every string at its
        schema's ``maxLength``.
    """
    finding = Finding(
        claim=_filled(400),
        source_url="https://" + _filled(200 - len("https://")),
        date_or_period=_filled(60),
        why_it_matters=_filled(150),
    )
    gap = Gap(looked_for=_filled(150), where=_filled(200))
    return WorkerReport(
        working_notes=_filled(1000),
        findings=[finding] * 20,
        gaps=[gap] * 10,
        tool_gap=_filled(60),
    )


def _committed_json_path() -> Path:
    return Path(__file__).with_name("worker_report_full_fill.json")


if __name__ == "__main__":
    report = build_full_fill_report()
    _committed_json_path().write_text(
        json.dumps(report.model_dump(mode="json"), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
