"""Run the pre-registered session-digest fixture sets (ADR-0124 AC-9/10/12/13, FRE-947).

Runs the **real** producer — the same ``generate_session_digest`` the sweep calls, on
the ``session_summary`` role's live deployment — against the frozen fixture sets in
``tests/fixtures/session_digest/``, and scores each criterion against the ground truth
recorded there.

The fixture sets and their labels are fixed in ``REGISTRY.md`` and were committed
before this script was first run. Do not edit a set to improve a result: a criterion
evaluated on a post-hoc sample has not been met.

    uv run python scripts/eval/session_digest_eval.py            # every set
    uv run python scripts/eval/session_digest_eval.py --set ac9  # one set
    uv run python scripts/eval/session_digest_eval.py --dry-run  # no model calls

This writes nothing to any substrate. It reads fixtures from disk and calls the model.
"""

from __future__ import annotations

import argparse
import asyncio
import collections
import json
import pathlib
import sys
import uuid
from datetime import datetime, timezone
from typing import Any

from personal_agent.captains_log.capture import TaskCapture
from personal_agent.memory.session_digest import (
    SessionDigest,
    SessionSummaryStatus,
)
from personal_agent.second_brain.session_summary import (
    build_prompt,
    generate_session_digest,
)

_FIXTURES = pathlib.Path(__file__).parent.parent.parent / "tests" / "fixtures" / "session_digest"

_SETS = {
    "ac8": "ac8_input_completeness",
    "ac9": "ac9_tool_only_facts",
    "ac10": "ac10_basis_labelling",
    "ac12": "ac12_corrections",
    "ac13": "ac13_missing_evidence",
}


def _load(name: str) -> dict[str, Any]:
    return json.loads((_FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


def _captures(case: dict[str, Any]) -> list[TaskCapture]:
    return [TaskCapture(**c) for c in case["captures"]]


def _all_items(digest: SessionDigest) -> list[Any]:
    return [*digest.established, *digest.decisions, *digest.unresolved, *digest.corrections]


def _digest_text(digest: SessionDigest) -> str:
    return " ".join(item.text for item in _all_items(digest)).lower()


def _case_session_id(case_id: str) -> str:
    """Derive a stable UUID session id from a readable case id.

    Production session ids are UUID strings and the cost-gate reservation path
    parses them as such, so a readable fixture id ("payload_absent") fails before
    the model is ever called. Deriving one keeps the report readable while the
    producer sees exactly the shape it sees in production — uuid5, so a case's id is
    identical across runs and its spend is attributable.
    """
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"fre-947-eval/{case_id}"))


async def _run_case(case: dict[str, Any]) -> tuple[str, SessionDigest | None, str | None]:
    """Generate for one case. Returns (case_id, digest, failure_reason)."""
    captures = _captures(case)
    outcome = await generate_session_digest(
        captures,
        session_id=_case_session_id(case["case_id"]),
        ended_at=captures[-1].timestamp,
        trace_id=f"eval-{case['case_id']}",
    )
    if outcome.status is not SessionSummaryStatus.GENERATED:
        reason = outcome.failure_reason.value if outcome.failure_reason else outcome.status.value
        return case["case_id"], None, reason
    return case["case_id"], outcome.digest, None


# ==========================================================================
# AC-8 — checked offline, no model call needed
# ==========================================================================


def score_ac8(payload: dict[str, Any]) -> dict[str, Any]:
    """Assert prompt completeness directly against the capture records."""
    failures: list[str] = []
    for case in payload["cases"]:
        captures = _captures(case)
        prompt = build_prompt(captures)
        for capture in captures:
            if f"capture_id: {capture.trace_id}" not in prompt:
                failures.append(f"{case['case_id']}: turn {capture.trace_id} missing")
            if capture.user_message and capture.user_message not in prompt:
                failures.append(f"{case['case_id']}: user text truncated")
            if capture.assistant_response and capture.assistant_response not in prompt:
                failures.append(f"{case['case_id']}: assistant text truncated")
            for result in capture.tool_results:
                if result["tool_name"] not in prompt:
                    failures.append(f"{case['case_id']}: tool {result['tool_name']} missing")
                if result.get("error") and result["error"] not in prompt:
                    failures.append(f"{case['case_id']}: tool error missing")
                if "arguments" in result and result["arguments"]:
                    rendered = (
                        result["arguments"]
                        if isinstance(result["arguments"], str)
                        else json.dumps(result["arguments"])
                    )
                    # Compare parsed structure, not raw bytes (AC-8's canonical
                    # serialisation clause).
                    if not isinstance(result["arguments"], str):
                        for key in result["arguments"]:
                            if f'"{key}"' not in prompt:
                                failures.append(f"{case['case_id']}: argument {key} missing")
                    elif rendered not in prompt:
                        failures.append(f"{case['case_id']}: raw arguments missing")
    return {
        "criterion": "AC-8",
        "passed": not failures,
        "cases": len(payload["cases"]),
        "failures": failures,
    }


# ==========================================================================
# AC-9 — tool-only facts survive
# ==========================================================================


async def score_ac9(payload: dict[str, Any]) -> dict[str, Any]:
    reproduced, missing, errored = [], [], []
    for case in payload["cases"]:
        case_id, digest, failure = await _run_case(case)
        if digest is None:
            errored.append({"case_id": case_id, "reason": failure})
            continue
        text = _digest_text(digest)
        # The fact is judged reproduced when its distinguishing token survives —
        # the value that appears ONLY in tool output.
        needles = [
            t.strip(",.\"'").lower()
            for t in case["expected_fact"].split()
            if any(ch.isdigit() for ch in t) or len(t) > 6
        ]
        hit = any(n in text for n in needles)
        (reproduced if hit else missing).append(
            {"case_id": case_id, "tool": case["tool"], "expected": case["expected_fact"]}
        )
    total = len(payload["cases"])
    return {
        "criterion": "AC-9",
        "passed": len(reproduced) == total,
        "reproduced": len(reproduced),
        "total": total,
        "missing": missing,
        "errored": errored,
    }


# ==========================================================================
# AC-10 — basis tagging discriminates
# ==========================================================================


async def score_ac10(payload: dict[str, Any]) -> dict[str, Any]:
    emitted: collections.Counter[str] = collections.Counter()
    agreements, comparisons, errored = 0, 0, []
    for case in payload["cases"]:
        case_id, digest, failure = await _run_case(case)
        if digest is None:
            errored.append({"case_id": case_id, "reason": failure})
            continue
        items = _all_items(digest)
        untagged = [i for i in items if not i.basis]
        if untagged:
            return {"criterion": "AC-10", "passed": False, "reason": "untagged item emitted"}
        for item in items:
            emitted[item.basis] += 1
        # Agreement is scored by matching each emitted item to the labelled item it
        # most plausibly restates, via token overlap; unmatched emissions are not
        # counted either way, since the producer is free to choose what to include.
        for item in items:
            tokens = {t for t in item.text.lower().split() if len(t) > 4}
            best, best_overlap = None, 0
            for label in case["labelled_items"]:
                label_tokens = {t for t in label["content"].lower().split() if len(t) > 4}
                overlap = len(tokens & label_tokens)
                if overlap > best_overlap:
                    best, best_overlap = label, overlap
            if best is not None and best_overlap >= 2:
                comparisons += 1
                if item.basis == best["true_basis"]:
                    agreements += 1

    total_emitted = sum(emitted.values())
    agreement = agreements / comparisons if comparisons else 0.0
    dominant = max(emitted.values()) / total_emitted if total_emitted else 0.0
    return {
        "criterion": "AC-10",
        "passed": agreement >= 0.85 and dominant <= 0.60 and not errored,
        "agreement": round(agreement, 3),
        "matched_comparisons": comparisons,
        "dominant_tag_share": round(dominant, 3),
        "tag_distribution": dict(emitted),
        "errored": errored,
    }


# ==========================================================================
# AC-12 — corrections fire when they should, stay silent when they should not
# ==========================================================================


async def score_ac12(payload: dict[str, Any]) -> dict[str, Any]:
    false_positives, true_positives, missed, errored = [], [], [], []
    missing_evidence_span = []

    for case in payload["cases"]:
        case_id, digest, failure = await _run_case(case)
        if digest is None:
            errored.append({"case_id": case_id, "reason": failure})
            continue
        fired = list(digest.corrections)
        if case["expected"] == "no_correction":
            if fired:
                false_positives.append(
                    {
                        "case_id": case_id,
                        "tier_c_kind": case.get("tier_c_kind"),
                        "emitted": [c.text for c in fired],
                    }
                )
        else:
            if fired:
                true_positives.append({"case_id": case_id, "tier": case["tier"]})
                # AC-12 additionally requires a Tier-B correction to carry the located
                # span of its SUPPORTING EVIDENCE, not merely of the self-correction.
                if case["tier"] == "B":
                    for correction in fired:
                        if not correction.evidence_span or not correction.evidence_locator:
                            missing_evidence_span.append(case_id)
            else:
                missed.append({"case_id": case_id, "tier": case["tier"]})

    positives = [c for c in payload["cases"] if c["expected"] == "correction"]
    recall = len(true_positives) / len(positives) if positives else 0.0
    return {
        "criterion": "AC-12",
        "passed": (
            not false_positives and recall >= 0.80 and not missing_evidence_span and not errored
        ),
        "precision_note": "absolute — any false positive fails the criterion",
        "false_positives": false_positives,
        "recall": round(recall, 3),
        "true_positives": len(true_positives),
        "positives_total": len(positives),
        "missed": missed,
        "tier_b_missing_evidence_span": missing_evidence_span,
        "errored": errored,
    }


# ==========================================================================
# AC-13 — missing evidence produces silence, not invention
# ==========================================================================


async def score_ac13(payload: dict[str, Any]) -> dict[str, Any]:
    results, failures, errored = [], [], []
    for case in payload["cases"]:
        case_id, digest, failure = await _run_case(case)
        if digest is None:
            errored.append({"case_id": case_id, "reason": failure})
            continue
        fired = len(digest.corrections)
        expected_correction = case["expected"] == "correction"
        ok = (fired > 0) == expected_correction
        results.append(
            {
                "case_id": case_id,
                "expected": case["expected"],
                "corrections_emitted": fired,
                "ok": ok,
            }
        )
        if not ok:
            failures.append(case_id)
    return {
        "criterion": "AC-13",
        "passed": not failures and not errored,
        "results": results,
        "failures": failures,
        "errored": errored,
    }


# ==========================================================================


async def _open_cost_gate() -> Any:
    """Register a CostGate so the producer's paid calls can reserve and commit budget.

    A standalone script never runs the service's startup hook, so without this every
    call raises ``No CostGate registered`` and the whole arm reports ``model_error``
    — which is what the first run of this harness did. Reusing the real gate rather
    than stubbing it keeps the eval's spend on the same ledger as production's, so
    the arm is visible in the budget surface rather than invisible to it.
    """
    from personal_agent.config import settings  # noqa: PLC0415
    from personal_agent.cost_gate import (  # noqa: PLC0415
        CostGate,
        load_budget_config,
        set_default_gate,
    )

    gate = CostGate(config=load_budget_config(), db_url=settings.database_url)
    await gate.connect()
    set_default_gate(gate)
    return gate


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--set", dest="only", choices=sorted(_SETS), help="run one set")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="offline checks only (AC-8); prove the harness runs without spending",
    )
    parser.add_argument("--out", type=pathlib.Path, help="write the JSON report here")
    args = parser.parse_args()

    report: dict[str, Any] = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "synthetic_fixtures": True,
        "note": (
            "Fixture sets are pre-registered synthetic supplements, labelled as such per "
            "ADR-0124's corpus-feasibility rule: the real corpus holds zero multi-turn "
            "sessions with captures on disk. See tests/fixtures/session_digest/REGISTRY.md."
        ),
        "criteria": [],
    }

    wanted = [args.only] if args.only else sorted(_SETS)

    if "ac8" in wanted:
        report["criteria"].append(score_ac8(_load(_SETS["ac8"])))

    if not args.dry_run:
        gate = await _open_cost_gate()
        try:
            scorers = {
                "ac9": score_ac9,
                "ac10": score_ac10,
                "ac12": score_ac12,
                "ac13": score_ac13,
            }
            for key in wanted:
                if key in scorers:
                    report["criteria"].append(await scorers[key](_load(_SETS[key])))
        finally:
            await gate.disconnect()

    print(json.dumps(report, indent=2))
    if args.out:
        args.out.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")

    return 0 if all(c.get("passed") for c in report["criteria"]) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
