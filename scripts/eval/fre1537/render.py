"""FRE-1511 / ADR-0154 D7: the design A planner request, rendered with the production functions.

Design A is an isolated planner call: the planner system prompt, then one user message made of the
rendered conversation history, an optional memory digest, and the query. The history and the
system prompt come from the production functions in ``expansion_controller``. The probe keeps its own
copy of the D2 additions: the decline rule as ADR-0152 D1 and the FRE-1502 probe wrote it, and, for a
level whose ``planner_policy`` is ``p2``, the ADR-0157 D6 block placed as FRE-1615's P2.patch placed it.
A test asserts that production's D2 prompt is byte-identical to this rendering for every level
(``tests/personal_agent/orchestrator/test_fre1515_decline_prompt.py``).
The user message comes from the production ``build_planner_user_message``, so the bound and the framing
are those that ship (FRE-1541).

ADR-0157 D7: the prompt that D7 qualifies is a Seshat effort level's D2 prompt. ``--level`` names the
level, and the default is the committed ``default_level``.

This file imports nothing from ``scripts``. The live step runs it inside the gateway container,
where the settings are those of the image under test:

    docker exec -i fre1537-gateway python /probe/render.py [--level standard] < fixtures.json

The last stdout line is ``FRE1537_PROMPTS=<json>``. The gateway package logs to stdout on import,
so the host reads that line and nothing else.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections.abc import Mapping, Sequence

PROMPTS_PREFIX = "FRE1537_PROMPTS="
# The trace id of the probe's harness exchanges. Production derives the call id from the turn's
# trace id; the probe uses one fixed id, so every request of a run is byte-identical across draws.
PROBE_TRACE_ID = "fre1537probe"

_SCHEMA_ANCHOR = '"strategy": "HYBRID|DECOMPOSE"'
_SCHEMA_REPLACEMENT = '"strategy": "SINGLE|HYBRID|DECOMPOSE"'
_RULES_ANCHOR = "Rules:\n"
DECLINE_RULE = (
    "- First decide whether this query needs independent sub-tasks at all. If one assistant working "
    "alone, with the same tools, would answer it well in one pass — a greeting, a short factual "
    "question, a single lookup, a follow-up — output "
    '{"strategy": "SINGLE", "tasks": []} and nothing else. Choose HYBRID or DECOMPOSE only when '
    "splitting the work into independent sub-tasks would produce a better answer than one pass\n"
)
# ADR-0157 D6: the owner's P2 text, the probe's own copy of FRE-1615's P2.patch constant.
P2_BLOCK = (
    "DECOMPOSITION POLICY\n"
    "Prefer SINGLE.\n"
    "Delegate only when independent execution is likely\n"
    "to materially improve evidence quality, coverage,\n"
    "or total completion time.\n"
    "Account for:\n"
    "- Additional model calls and context overhead\n"
    "- Shared inference-server contention\n"
    "- Information lost through worker summaries\n"
    "- Work the primary agent can perform directly\n"
    "Do not create a worker solely to summarize,\n"
    "compare, or reformat information another worker\n"
    "is already gathering, unless independent analysis\n"
    "has a specific benefit.\n"
    "HYBRID delegates bounded specialized work while\n"
    "the primary handles remaining reasoning or synthesis.\n"
    "DECOMPOSE requires at least two genuinely independent\n"
    "subtasks whose results can be merged without depending\n"
    "on one another.\n"
    "Prefer fewer, more complete tasks when they share\n"
    "the same tools, sources, and information.\n"
    "Every task needs a specific deliverable and a\n"
    "measurable completion condition.\n"
)


def _level_policy(level: str | None) -> tuple[str, str | None]:
    """Return the resolved level name and its ``planner_policy``."""
    from personal_agent.config.seshat_effort import effort_spec

    spec = effort_spec(level)  # type: ignore[arg-type]
    return spec.level, spec.planner_policy


def _production_system_prompt(surface: Sequence[str], level: str | None = None) -> str:
    """Return production's prompt without the D2 additions, at the level's round budget."""
    from personal_agent.config.seshat_effort import effort_spec
    from personal_agent.orchestrator.expansion_controller import _build_planner_system_prompt

    return _build_planner_system_prompt(list(surface), effort=effort_spec(level))  # type: ignore[arg-type]


def render_system_prompt(surface: Sequence[str], level: str | None = None) -> str:
    """Render a level's D2 planner system prompt (ADR-0154 D7, ADR-0157 D7 rule 1).

    The decline rule, a schema that admits ``SINGLE`` and, on a ``p2`` level, the D6 block.

    Args:
        surface: Sub-agent tool names currently grantable.
        level: The Seshat effort level. ``None`` is the committed ``default_level``.

    Returns:
        The system prompt of design A.

    Raises:
        RuntimeError: If the production prompt no longer holds an anchor that the rule needs.
            The probe must be updated, not skipped, because a changed prompt is a changed instrument.
    """
    base = _production_system_prompt(surface, level)
    for anchor in (_SCHEMA_ANCHOR, _RULES_ANCHOR):
        if anchor not in base:
            raise RuntimeError(f"planner system prompt lost the anchor {anchor!r}")
    _, policy = _level_policy(level)
    block = P2_BLOCK + "\n" if policy == "p2" else ""
    return base.replace(_SCHEMA_ANCHOR, _SCHEMA_REPLACEMENT, 1).replace(
        _RULES_ANCHOR, block + _RULES_ANCHOR + DECLINE_RULE, 1
    )


def prompt_hash(system_prompt: str) -> str:
    """Return the SHA-256 hex digest of the rendered planner system prompt."""
    return hashlib.sha256(system_prompt.encode()).hexdigest()


def build_user_message(history: str, digest: str | None, query: str) -> str:
    """Build the planner user message: history, then query.

    A thin wrapper over the production framing in ``expansion_controller``, for a caller that
    holds a rendered history and no messages. The stable parts come first, so a change in the
    query never breaks the cached history before it (ADR-0154 D1). Since FRE-1360 the digest
    is not in this message: it rides a tool result (:func:`digest_exchange`). The ``digest``
    argument stays so existing callers keep their signature, and must be ``None``.

    Args:
        history: Rendered conversation history. Empty for a first turn.
        digest: Must be ``None``. Pass a digest to :func:`digest_exchange` instead.
        query: The current message.

    Returns:
        The user message text.

    Raises:
        ValueError: If a digest is given — production no longer puts it in the user text.
    """
    if digest:
        raise ValueError(
            "FRE-1360: the planner digest rides a memory_recall tool result; "
            "send it with digest_exchange(), not in the user message"
        )
    from personal_agent.orchestrator.expansion_controller import (
        _frame_planner_query,
        _join_planner_blocks,
    )

    return _join_planner_blocks(history, "", _frame_planner_query(query, "HYBRID"))


def digest_exchange(digest: str | None) -> list[dict[str, object]]:
    """Return the production ``memory_recall`` exchange for a digest, after the user message.

    FRE-1360: production carries the planner's memory digest as a harness tool result, never
    as user text. This calls the production builder, so a digest run qualifies that request.

    Args:
        digest: The digest lines, untitled, or ``None``.

    Returns:
        The assistant tool call and the tool result, or an empty list for no digest.
    """
    from personal_agent.orchestrator.expansion_controller import planner_digest_exchange

    return planner_digest_exchange(digest or "", trace_id=PROBE_TRACE_ID)


def build_planner_request(
    system_prompt: str,
    history_messages: Sequence[Mapping[str, str]],
    query: str,
    digest: str | None,
    history_max_chars: int,
) -> dict[str, object]:
    """Build the design A planner messages for one query.

    Args:
        system_prompt: The output of :func:`render_system_prompt`. It never depends on the digest.
        history_messages: Conversation messages before the query, oldest first.
        query: The current message.
        digest: Optional memory digest text.
        history_max_chars: History budget. The production function trims whole messages from the
            oldest end.

    Returns:
        ``{"messages": [system, user, (assistant, tool when a digest is given)],
        "history": str, "history_chars": int}``.
    """
    from personal_agent.config import settings
    from personal_agent.orchestrator.expansion_controller import (
        build_planner_user_message,
        planner_request_messages,
    )

    # The production builder, so the probe qualifies the message that ships: the same bound,
    # the same fill order, the same framing (FRE-1541). It drops the trailing query message.
    built = build_planner_user_message(
        query,
        "HYBRID",
        [*(dict(m) for m in history_messages), {"role": "user", "content": query}],
        digest_text=digest or "",
        history_max_chars=history_max_chars,
        input_max_chars=settings.planner_input_max_chars,
    )
    return {
        # FRE-1360: the production request — a digest rides a tool result after the user
        # message, never the user text.
        "messages": planner_request_messages(system_prompt, built, trace_id=PROBE_TRACE_ID),
        "history": built.history_text,
        "history_chars": built.history_chars,
    }


def main(argv: Sequence[str] | None = None) -> None:
    """Render every fixture on stdin and print the prompts file as the last stdout line.

    Args:
        argv: Command-line arguments. Default is ``sys.argv[1:]``.
    """
    from personal_agent.config import settings
    from personal_agent.orchestrator.expansion_controller import _current_sub_agent_tool_surface

    parser = argparse.ArgumentParser(description="Render a level's D2 planner prompts.")
    parser.add_argument("--level", choices=("fast", "standard", "deep"), default=None)
    args = parser.parse_args(argv)
    level, policy = _level_policy(args.level)
    fixtures = json.load(sys.stdin)
    surface = _current_sub_agent_tool_surface("fre1537")
    system = render_system_prompt(surface, level)
    max_chars = settings.planner_history_max_chars
    rendered: dict[str, dict[str, object]] = {}
    for fx in fixtures:
        req = build_planner_request(system, fx["history_messages"], fx["message"], None, max_chars)
        user = req["messages"][1]["content"]  # type: ignore[index]
        rendered[fx["label"]] = {
            "user": user,
            "history": req["history"],
            "query": fx["message"],
            "history_chars": req["history_chars"],
        }
    out = {
        "level": level,
        "planner_policy": policy,
        "surface": surface,
        "history_max_chars": max_chars,
        "system": system,
        "prompt_hash": prompt_hash(system),
        "fixtures": rendered,
    }
    print(PROMPTS_PREFIX + json.dumps(out))


if __name__ == "__main__":
    main()
