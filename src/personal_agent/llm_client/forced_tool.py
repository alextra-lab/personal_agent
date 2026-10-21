"""Whether a client's deployment can force a tool (FRE-1647).

Three producers force their single tool so the model cannot answer in prose:
``span_extraction``, ``entailment`` and ``session_summary``. ``claude-sonnet-5-5``
returns 400 on a forced ``tool_choice``, and litellm 1.106.0 refuses it client-side
(``supports_forced_tool_use: false`` in its map). On such a deployment the producers
send ``"auto"`` with the same single tool. Their prompts already say to answer only
through the tool, and a reply with no tool call goes through each producer's existing
fail-closed path.

Strict tool use is not an option here: litellm 1.106.0 drops ``function.strict`` on the
Anthropic chat path, so the request would not carry it.
"""

from __future__ import annotations


def can_force_tool(client: object) -> bool:
    """Return whether the client's deployment accepts a forced ``tool_choice``.

    The client is asked, not the role binding, because an eval harness overrides the
    deployment through the client it builds (``get_llm_client_for_key(--model-key)``).

    Args:
        client: The client the producer dispatches through.

    Returns:
        ``False`` only when the client states ``supports_forced_tool_use`` is ``False``.
        A client that does not state it (a test stub, a mock) keeps the forced request,
        which is the request every deployment sent before FRE-1647.
    """
    return getattr(client, "supports_forced_tool_use", True) is not False
