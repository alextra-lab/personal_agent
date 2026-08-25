r"""Inter-labeller agreement for the corpus (FRE-1281, ADR-0138 AC-7).

The ticket requires "recorded inter-labeller agreement". Recording a number is not the
point — a corpus whose labellers disagree cannot measure the distinction it claims to
measure, whatever the extractor then scores against it. So κ carries a preregistered bar
(``bars.INTER_LABELLER_KAPPA``) like everything else, and below it the remedy is to revise
``ADJUDICATION.md``, not to proceed.

**Labeller A** is the session that wrote ``corpus.yaml``, by hand, against
``ADJUDICATION.md``. **Labeller B** is an independent model pass driven by *the
adjudication guidelines*, deliberately **not** by the extractor's own system prompt — if B
read the extractor's prompt, agreement would measure the extractor rather than the
guidelines, and the corpus would appear to validate whatever the extractor happens to do.

B is given each gold span's exact text and its document, and asked only the exempt /
non-exempt / not-a-claim question. Segmentation is not re-derived: boundary agreement is a
different measurement and one this ticket takes from the extractor's own boundary F1.

Run::

    uv run python -m scripts.eval.fre1281_span_extraction.iaa --write
"""

from __future__ import annotations

import os

# FRE-375: test substrate before any personal_agent import (see harness.py).
_TEST_SUBSTRATE_ENV = {
    "APP_ENV": "test",
    "AGENT_NEO4J_URI": "bolt://localhost:7688",
    "AGENT_ELASTICSEARCH_URL": "http://localhost:9201",
    "AGENT_DATABASE_URL": (
        "postgresql+asyncpg://agent:agent_dev_password@localhost:5433/personal_agent"
    ),
    "AGENT_DATABASE_ADMIN_URL": (
        "postgresql+asyncpg://agent:agent_dev_password@localhost:5433/personal_agent"
    ),
    "AGENT_SYSGRAPH_DATABASE_URL": (
        "postgresql+asyncpg://agent:agent_dev_password@localhost:5433/personal_agent"
    ),
    "AGENT_ELASTICSEARCH_INDEX_PREFIX": "agent-logs-test",
    "AGENT_CAPTAINS_LOG_INDEX_PREFIX": "agent-captains-test",
}
for _key, _value in _TEST_SUBSTRATE_ENV.items():
    os.environ.setdefault(_key, _value)

import argparse  # noqa: E402
import asyncio  # noqa: E402
import json  # noqa: E402
from collections import Counter  # noqa: E402
from collections.abc import Sequence  # noqa: E402
from pathlib import Path  # noqa: E402

from scripts.eval.fre1281_span_extraction.bars import INTER_LABELLER_KAPPA  # noqa: E402
from scripts.eval.fre1281_span_extraction.corpus import (  # noqa: E402
    GoldDocument,
    SpanLabel,
    load_corpus,
)

AGREEMENT_PATH = Path("scripts/eval/fre1281_span_extraction/corpus_agreement.json")

LABELLER_B_TOOL = "label_spans"

#: Labeller B's brief. Derived from ADJUDICATION.md, NOT from the extractor prompt — see
#: the module docstring for why that separation is the whole point of this measurement.
LABELLER_B_PROMPT = """\
You are labelling spans of an assistant's output for a grounding study. For each numbered \
span, decide ONE of:

- "claim_non_exempt" — it asserts something about the world, and none of the exempt \
regions below covers it.
- "claim_exempt" — it asserts something, but an exempt region covers it.
- "not_a_claim" — it asserts nothing about the world.

THE DEFAULT IS DENY. Outside the exempt regions, any span making a claim about the world \
is "claim_non_exempt". Do not ask whether a claim is well known; ask only whether an \
exempt region covers it.

THE EXEMPT REGIONS, AND THERE ARE NO OTHERS:
- Code the user is being offered to run. But imports, package manifest entries and \
install commands are NOT exempt — they are verified against a registry.
- Arithmetic whose every input is itself cited. Only the derived result is exempt, never \
the cited inputs themselves.
- The user's own words repeated WITH attribution ("you mentioned X"). The same content \
offered as your own recommendation ("I'd recommend X") is NOT exempt.
- Judgement over cited material that introduces no externally checkable predicate of its \
own — comparatives and orderings over cited attributes. "Well regarded", "safe", \
"popular", "recommended", "reliable" are NOT exempt: each is externally checkable, \
however evaluative it sounds.
- Claims about THIS turn's own execution — what was searched, what was retrieved, that \
nothing was found.

NOT EXEMPT, though it may look it: prose placed inside a code fence is prose. A \
world-fact claim inside a string literal or comment is still a claim. Prose asserting how \
an API behaves needs a documentation source, even though using that API in code does not.

A span that already carries a citation marker is still "claim_non_exempt" — having a \
source does not make a claim exempt.

If you cannot decide between exempt evaluation and a checkable claim, answer \
"claim_non_exempt". Answer only through the tool, one label per span id.\
"""


def _tool(count: int) -> dict[str, object]:
    """Forced-tool schema for one document's spans."""
    return {
        "type": "function",
        "function": {
            "name": LABELLER_B_TOOL,
            "description": f"Label all {count} spans.",
            "parameters": {
                "type": "object",
                "properties": {
                    "labels": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "id": {"type": "integer"},
                                "label": {
                                    "type": "string",
                                    "enum": [label.value for label in SpanLabel],
                                },
                            },
                            "required": ["id", "label"],
                        },
                    }
                },
                "required": ["labels"],
            },
        },
    }


def cohens_kappa(a: Sequence[str], b: Sequence[str]) -> float:
    """Cohen's κ for two labellers over the same items.

    Args:
        a: Labeller A's labels.
        b: Labeller B's labels, index-aligned with ``a``.

    Returns:
        κ in ``[-1, 1]``. Returns 1.0 when both labellers used a single identical
        category — perfect agreement over a degenerate distribution, where the chance
        correction is undefined rather than zero.

    Raises:
        ValueError: If the sequences differ in length or are empty.
    """
    if len(a) != len(b):
        raise ValueError(f"labeller sequences differ in length: {len(a)} vs {len(b)}")
    if not a:
        raise ValueError("no items to compare")

    n = len(a)
    observed = sum(1 for x, y in zip(a, b, strict=True) if x == y) / n
    counts_a = Counter(a)
    counts_b = Counter(b)
    expected = sum(
        (counts_a[label] / n) * (counts_b[label] / n) for label in set(counts_a) | set(counts_b)
    )
    if expected == 1.0:
        return 1.0
    return (observed - expected) / (1 - expected)


async def _label_document(client, document: GoldDocument) -> list[str]:
    """Ask labeller B for its verdict on one document's gold spans."""
    from personal_agent.llm_client.types import ModelRole
    from personal_agent.telemetry.trace import SystemTraceContext

    spans = list(document.spans)
    listing = "\n".join(f"[{i}] {span.text!r}" for i, span in enumerate(spans))
    body = (
        f"Assistant output:\n<<<OUTPUT>>>\n{document.text}\n<<<END OUTPUT>>>\n\n"
        + (
            f"The user's own words this turn:\n<<<USER>>>\n{document.user_message}\n"
            f"<<<END USER>>>\n\n"
            if document.user_message
            else ""
        )
        + f"Spans to label:\n{listing}\n"
    )
    response = await client.respond(
        role=ModelRole.SPAN_EXTRACTION,
        messages=[{"role": "user", "content": body}],
        system_prompt=LABELLER_B_PROMPT,
        tools=[_tool(len(spans))],
        tool_choice={"type": "function", "function": {"name": LABELLER_B_TOOL}},
        max_tokens=2048,
        trace_ctx=SystemTraceContext.new("span_extraction_iaa"),
    )
    payload = ""
    for call in response.get("tool_calls") or []:
        if call.get("name") == LABELLER_B_TOOL and call.get("arguments"):
            payload = str(call["arguments"])
            break

    # An unlabelled span counts as a disagreement rather than being dropped: silently
    # excluding the hard ones would inflate agreement exactly where it matters.
    verdicts = ["<unlabelled>"] * len(spans)
    try:
        parsed = json.loads(payload)
    except json.JSONDecodeError:
        return verdicts
    for entry in parsed.get("labels") or []:
        index = entry.get("id")
        label = entry.get("label")
        if isinstance(index, int) and 0 <= index < len(spans) and isinstance(label, str):
            verdicts[index] = label
    return verdicts


async def run(*, write: bool) -> int:
    """Measure agreement and, optionally, record it.

    Args:
        write: Whether to write ``corpus_agreement.json``.

    Returns:
        Exit code — nonzero if κ is below its preregistered bar, because a corpus that
        fails admissibility must not be used to score anything.
    """
    from personal_agent.config import resolve_role_model_key
    from personal_agent.config.settings import get_settings
    from personal_agent.cost_gate import CostGate, load_budget_config, set_default_gate
    from personal_agent.llm_client.factory import get_llm_client_for_key

    gate = CostGate(config=load_budget_config(), db_url=get_settings().database_url)
    await gate.connect()
    set_default_gate(gate)

    model_key = resolve_role_model_key("span_extraction")
    client = get_llm_client_for_key(model_key, budget_role="entity_extraction")

    documents = load_corpus()
    semaphore = asyncio.Semaphore(4)

    async def one(document: GoldDocument) -> tuple[GoldDocument, list[str]]:
        async with semaphore:
            return document, await _label_document(client, document)

    labels_a: list[str] = []
    labels_b: list[str] = []
    for document, verdicts in await asyncio.gather(*(one(d) for d in documents)):
        for span, verdict in zip(document.spans, verdicts, strict=True):
            labels_a.append(span.label.value)
            labels_b.append(verdict)

    kappa = cohens_kappa(labels_a, labels_b)
    agreement = sum(1 for x, y in zip(labels_a, labels_b, strict=True) if x == y) / len(labels_a)
    met = INTER_LABELLER_KAPPA.holds(kappa)

    record = {
        "corpus_version": "2026-08-24.1",
        "spans_compared": len(labels_a),
        "raw_agreement": round(agreement, 4),
        "cohens_kappa": round(kappa, 4),
        "bar": INTER_LABELLER_KAPPA.value,
        "bar_met": met,
        "labeller_a": "FRE-1281 build session, by hand, against ADJUDICATION.md v1",
        "labeller_b": f"independent model pass ({model_key}) driven by ADJUDICATION.md, "
        "not by the extractor's own prompt",
        "note": (
            "Agreement is on the exempt / non-exempt / not-a-claim decision over the gold "
            "spans. Segmentation agreement is a separate measurement, reported as the "
            "extractor's decomposition boundary F1."
        ),
    }
    print(json.dumps(record, indent=2))  # noqa: T201 — CLI output
    if write:
        AGREEMENT_PATH.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    return 0 if met else 1


def main() -> int:
    """Parse arguments and run."""
    parser = argparse.ArgumentParser(description="Inter-labeller agreement (FRE-1281).")
    parser.add_argument("--write", action="store_true")
    args = parser.parse_args()
    return asyncio.run(run(write=args.write))


if __name__ == "__main__":
    raise SystemExit(main())
