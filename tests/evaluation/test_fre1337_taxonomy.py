"""AC-2 structural proof: the probe prompt is verbatim, pure, and uncontaminated.

FRE-1337 constraint 2 ("give the model the taxonomy verbatim and nothing else... do not
show it the deterministic answer") is enforced here rather than trusted: every
``TaskType`` member must appear, the prompt must be a pure function of the message alone,
and it must never contain any token that could leak a deterministic classification result
(confidence, signal names, or the words "deterministic"/"gateway"/"Stage 4").
"""

from __future__ import annotations

from scripts.eval.fre1337_intent_probe.taxonomy import (
    PROBE_SYSTEM_PROMPT,
    TASK_TYPE_DEFINITIONS,
    build_probe_prompt,
)

from personal_agent.request_gateway.types import TaskType


def test_all_task_type_members_are_defined() -> None:
    assert set(TASK_TYPE_DEFINITIONS) == set(TaskType)


def test_all_task_type_values_appear_in_system_prompt() -> None:
    for member in TaskType:
        assert member.value in PROBE_SYSTEM_PROMPT


def test_build_probe_prompt_is_pure() -> None:
    message = "Which tinned tuna should I buy in France"
    assert build_probe_prompt(message) == build_probe_prompt(message)


def test_build_probe_prompt_contains_only_the_message() -> None:
    message = "UNIQUE_SENTINEL_MESSAGE_TEXT"
    prompt = build_probe_prompt(message)
    assert message in prompt


_LEAKAGE_TOKENS = (
    "deterministic",
    "gateway",
    "stage 4",
    "stage4",
    "confidence",
    "no_special_patterns",
    "signals",
)


def test_no_deterministic_answer_leaked_into_prompts() -> None:
    combined = (PROBE_SYSTEM_PROMPT + build_probe_prompt("anything")).lower()
    for token in _LEAKAGE_TOKENS:
        assert token not in combined, f"leaked token: {token!r}"


def test_system_prompt_requests_json_only() -> None:
    assert "JSON only" in PROBE_SYSTEM_PROMPT
    assert "task_type" in PROBE_SYSTEM_PROMPT
    assert "reason" in PROBE_SYSTEM_PROMPT
