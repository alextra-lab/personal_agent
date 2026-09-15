"""FRE-1517 amendment A2 — classify every script line offline, before the owner approves.

For each turn of each session script this computes the route today's gateway gives it
(``classify_intent`` then ``_apply_matrix`` — the pre-ADR-0152 matrix, message-only, no LLM)
and checks three things against what the script declares:

1. ``expect_route`` equals the computed strategy, so the script cannot drift from the
   router silently.
2. Every script has at least ``MIN_HYBRID`` HYBRID turns, or its worker columns stay empty.
3. Every back-reference's ``window`` label matches its distance: ``in`` needs the referenced
   turn inside the 20-message history slice, ``memory`` needs it clearly outside.

It assumes governance permits expansion (``expansion_permitted`` and a budget above zero). A
turn the live gateway forces SINGLE for resource pressure is a runtime fact, recorded by the
session runner from ``route_traces``, not a script defect.

No gateway, model or substrate is touched. Run from the repo root:

    uv run python scripts/eval/fre1517/classify.py            # table + checks, exit 1 on a failed check
"""

from __future__ import annotations

import sys
from pathlib import Path

import yaml

from personal_agent.request_gateway.decomposition import _apply_matrix
from personal_agent.request_gateway.intent import classify_intent

HERE = Path(__file__).parent
SCRIPTS_DIR = HERE / "scripts"
MIN_HYBRID = 4

#: ``AGENT_CONVERSATION_MAX_HISTORY_MESSAGES`` read from the running gateway on 2026-09-14
#: (``printenv`` in ``cloud-sim-seshat-gateway``: 20). ``service/app.py`` slices the stored
#: messages *before* appending the current one, and a completed turn stores two messages.
HISTORY_MESSAGES = 20
TURNS_IN_WINDOW = HISTORY_MESSAGES // 2
#: A turn that stored only its user message (a failed turn) shifts the slice by one. The
#: labels keep a two-turn margin on each side of the boundary so that one or two failed
#: turns cannot flip a label: ``in`` at distance <= 8, ``memory`` at distance >= 12.
IN_MAX_DISTANCE = TURNS_IN_WINDOW - 2
MEMORY_MIN_DISTANCE = TURNS_IN_WINDOW + 2


def route(message: str) -> tuple[str, str, str, str]:
    """Compute the gateway route for one message.

    Args:
        message: The user message, verbatim.

    Returns:
        (task_type, complexity, strategy, reason) as the live stage 4 and 5 compute them.
    """
    intent = classify_intent(message)
    strategy, reason = _apply_matrix(intent.task_type, intent.complexity, False)
    return intent.task_type.value, intent.complexity.value, strategy.value.upper(), reason


def check_script(path: Path) -> tuple[list[str], list[str]]:
    """Classify one script and collect its table rows and failed checks.

    Args:
        path: A session script YAML file.

    Returns:
        (table lines, failure messages).
    """
    doc = yaml.safe_load(path.read_text())
    name = doc["script"]
    failures: list[str] = []
    lines = [
        f"\n### {name} — {doc['title']}\n",
        "| turn | task_type / complexity | route (reason) | expect | back-ref | message head |",
        "|---|---|---|---|---|---|",
    ]
    hybrid = 0
    numbers = [t["n"] for t in doc["turns"]]
    if numbers != list(range(1, len(numbers) + 1)):
        failures.append(f"{name}: turn numbers are not 1..{len(numbers)} in order")
    instruction_ids = {i["id"]: i["set_at"] for i in doc.get("instructions", [])}
    for turn in doc["turns"]:
        n = turn["n"]
        task_type, complexity, strategy, reason = route(turn["message"])
        if strategy == "HYBRID":
            hybrid += 1
        if turn.get("expect_route") != strategy:
            failures.append(
                f"{name} t{n}: expect_route {turn.get('expect_route')!r} but computed {strategy} ({reason})"
            )
        ref_cells = []
        for ref in turn.get("back_refs", []):
            distance = n - ref["turn"]
            label = ref["window"]
            ref_cells.append(f"t{ref['turn']} d={distance} {label}")
            if distance <= 0:
                failures.append(f"{name} t{n}: back_ref to t{ref['turn']} is not earlier")
            elif label == "in" and distance > IN_MAX_DISTANCE:
                failures.append(
                    f"{name} t{n}: back_ref t{ref['turn']} labelled in, distance {distance} > {IN_MAX_DISTANCE}"
                )
            elif label == "memory" and distance < MEMORY_MIN_DISTANCE:
                failures.append(
                    f"{name} t{n}: back_ref t{ref['turn']} labelled memory, distance {distance} < {MEMORY_MIN_DISTANCE}"
                )
            elif label not in ("in", "memory"):
                failures.append(f"{name} t{n}: back_ref window must be in or memory, got {label!r}")
        for check in turn.get("holds", []):
            if check not in instruction_ids:
                failures.append(f"{name} t{n}: holds unknown instruction {check}")
            elif instruction_ids[check] >= n:
                failures.append(f"{name} t{n}: holds {check} before it is set")
        head = turn["message"].replace("|", "/")[:70]
        lines.append(
            f"| {n} | {task_type} / {complexity} | {strategy} ({reason}) | {turn.get('expect_route')} "
            f"| {'; '.join(ref_cells) or '—'} | {head} |"
        )
    if hybrid < MIN_HYBRID:
        failures.append(f"{name}: {hybrid} HYBRID turns, needs at least {MIN_HYBRID}")
    lines.append(f"\nHYBRID turns: {hybrid} of {len(doc['turns'])}")
    return lines, failures


def main() -> int:
    """Classify every script and print the table.

    Returns:
        0 when every check holds, 1 otherwise.
    """
    all_failures: list[str] = []
    for path in sorted(SCRIPTS_DIR.glob("*.yaml")):
        lines, failures = check_script(path)
        sys.stdout.write("\n".join(lines) + "\n")
        all_failures.extend(failures)
    if all_failures:
        sys.stdout.write("\nFAILED CHECKS\n" + "\n".join(f"- {f}" for f in all_failures) + "\n")
        return 1
    sys.stdout.write("\nAll checks hold.\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
