"""FRE-1511 / ADR-0154 D7: the design A planner request, rendered with the production functions.

Design A is an isolated planner call: the planner system prompt, then one user message made of the
rendered conversation history, an optional memory digest, and the query. The history and the
system prompt come from the production functions in ``expansion_controller``. The decline rule is
inserted as ADR-0152 D1 and the FRE-1502 probe wrote it, because production code does not carry it yet.
The user message comes from the production ``build_planner_user_message``, so the bound and the framing
are those that ship (FRE-1541).

This file imports nothing from ``scripts``. The live step runs it inside the gateway container,
where the settings are those of the image under test:

    docker exec -i fre1537-gateway python /probe/render.py < fixtures.json

The last stdout line is ``FRE1537_PROMPTS=<json>``. The gateway package logs to stdout on import,
so the host reads that line and nothing else.
"""

from __future__ import annotations

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


def _production_system_prompt(surface: Sequence[str]) -> str:
    from personal_agent.orchestrator.expansion_controller import _build_planner_system_prompt

    return _build_planner_system_prompt(list(surface))


def render_system_prompt(surface: Sequence[str]) -> str:
    """Render the planner system prompt with the decline rule and a schema that admits ``SINGLE``.

    Args:
        surface: Sub-agent tool names currently grantable.

    Returns:
        The system prompt of design A.

    Raises:
        RuntimeError: If the production prompt no longer holds an anchor that the rule needs.
            The probe must be updated, not skipped, because a changed prompt is a changed instrument.
    """
    base = _production_system_prompt(surface)
    for anchor in (_SCHEMA_ANCHOR, _RULES_ANCHOR):
        if anchor not in base:
            raise RuntimeError(f"planner system prompt lost the anchor {anchor!r}")
    return base.replace(_SCHEMA_ANCHOR, _SCHEMA_REPLACEMENT, 1).replace(
        _RULES_ANCHOR, _RULES_ANCHOR + DECLINE_RULE, 1
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


def main() -> None:
    """Render every fixture on stdin and print the prompts file as the last stdout line."""
    from personal_agent.config import settings
    from personal_agent.orchestrator.expansion_controller import _current_sub_agent_tool_surface

    fixtures = json.load(sys.stdin)
    surface = _current_sub_agent_tool_surface("fre1537")
    system = render_system_prompt(surface)
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
        "surface": surface,
        "history_max_chars": max_chars,
        "system": system,
        "prompt_hash": prompt_hash(system),
        "fixtures": rendered,
    }
    print(PROMPTS_PREFIX + json.dumps(out))


if __name__ == "__main__":
    main()
