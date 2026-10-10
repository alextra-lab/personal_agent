"""FRE-1574 AC-5: the eval stack still runs its calls, with no model call (ADR-0156 D8.5).

The fixture holds every tool call of the FRE-1517 stage-3 runs (s2_tool and s3_logs_learning,
Flash and Gemma), with its full arguments from ``agent-logs-*``. The audit's ``useful`` labels
were never committed, so the owner set the criterion on 2026-10-10 (FRE-1574): every call that
ran successfully must come out allow under ``permissions.yaml`` plus ``permissions.eval.yaml``,
except the classes that the ADR makes ask. Those are listed in ``_adr_ask_class`` and counted.
"""

from __future__ import annotations

import json
import posixpath
from collections import Counter
from pathlib import Path

from personal_agent.governance.bash_matcher import parse_bash
from personal_agent.governance.permissions import (
    PROGRAM_RUNNING_WORDS,
    PermissionDecision,
    evaluate_permission,
    load_permission_rules,
)

_ROOT = Path(__file__).resolve().parents[3]
_FIXTURE = _ROOT / "tests" / "fixtures" / "fre1574_stage3_calls.json"
_RULES = load_permission_rules(
    [
        _ROOT / "config" / "governance" / "permissions.yaml",
        _ROOT / "config" / "governance" / "permissions.eval.yaml",
    ]
)


def _adr_ask_class(tool: str, args: dict[str, object], decision: PermissionDecision) -> str | None:
    """Name the ADR class that makes this call ask, or ``None`` when no ADR rule does.

    The classes: an explicit ask rule (D8.6 ``run_python`` network), a bash subcommand that
    fails the D8.2 closed condition, and a bash subcommand whose first word can run another
    program (``python3``, ``awk``, ``sed``, ``env``: the owner's decision of 2026-10-10).
    """
    if decision.explicit:
        return f"explicit ask rule {decision.rule}"
    if tool != "bash":
        return None
    parsed = parse_bash(str(args.get("command", "")))
    if parsed.refusal is not None:
        return f"not a plain command ({parsed.refusal})"
    for sub in parsed.top_level:
        if sub.refusal is not None:
            return f"not a plain subcommand ({sub.refusal})"
        if sub.words and posixpath.basename(sub.words[0]) in PROGRAM_RUNNING_WORDS:
            return f"{sub.words[0]} can run another program"
    return None


def _calls() -> list[dict[str, object]]:
    return list(json.loads(_FIXTURE.read_text())["calls"])


def test_the_fixture_covers_the_four_stage3_runs() -> None:
    calls = _calls()
    assert len(calls) == 372
    assert {c["session"] for c in calls} == {"fn_s2", "fn_s3", "gm_s2", "gm_s3"}


def test_every_successful_call_is_allowed_or_an_adr_ask() -> None:
    unexplained: list[tuple[str, str]] = []
    classes: Counter[str] = Counter()
    allowed = 0
    for call in _calls():
        if not call["ran_ok"]:
            continue
        tool, args = str(call["tool"]), dict(call["args"])  # type: ignore[call-overload]
        decision = evaluate_permission(tool, args, "NORMAL", _RULES)
        if decision.decision == "allow":
            allowed += 1
            continue
        adr_class = _adr_ask_class(tool, args, decision)
        if adr_class is None or decision.decision == "deny":
            unexplained.append((tool, str(decision.refused)[:200]))
        else:
            classes[adr_class.split(" (")[0]] += 1
    assert unexplained == []
    # The counts the PR reports: 346 successful calls; 21 run_python network and 17 bash asks.
    assert allowed == 308
    assert sum(classes.values()) == 38


def test_seeded_negative_without_the_eval_file_a_call_is_unexplained() -> None:
    """The eval file is load-bearing: without it, the redis probe is neither allowed nor ADR."""
    base = load_permission_rules([_ROOT / "config" / "governance" / "permissions.yaml"])
    args = {"command": "redis-cli -h redis ping"}
    decision = evaluate_permission("bash", args, "NORMAL", base)
    assert decision.decision == "ask"
    assert _adr_ask_class("bash", args, decision) is None
