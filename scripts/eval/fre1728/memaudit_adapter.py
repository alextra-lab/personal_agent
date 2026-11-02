"""FRE-1728 AC-1: MemAudit's ``MemoryAgent``, backed by Seshat. It runs in MemAudit's environment.

This file imports no project module. It reaches Seshat only through the loopback bridge, over HTTP.
"""

from __future__ import annotations

import json
import os
from typing import Dict, List, Optional

import httpx
from base_agent import MemoryAgent

_TIMEOUT_S = 3800.0


class SpendCapReached(Exception):
    """The bridge refused work at the spend cap."""


class HarnessFailure(Exception):
    """A failure of the harness, which must stop the run and never score as a Seshat defect."""


class BridgeClient:
    """The driver's only way to reach Seshat."""

    def __init__(self, base_url: str, transport: Optional[httpx.BaseTransport] = None) -> None:
        """Connect to the bridge.

        Args:
            base_url: The bridge's loopback URL.
            transport: A test transport.
        """
        self._client = httpx.Client(base_url=base_url, timeout=_TIMEOUT_S, transport=transport)

    def _json(self, response: httpx.Response) -> object:
        if response.status_code == 402:
            raise SpendCapReached(response.text)
        if response.status_code != 200:
            raise HarnessFailure(f"bridge answered {response.status_code}: {response.text[:300]}")
        return response.json()

    def turn(self, message: str, session_id: Optional[str]) -> Dict[str, str]:
        """Send one user message.

        Args:
            message: The message.
            session_id: The session to continue, or None for a new session.

        Returns:
            ``session_id``, ``trace_id`` and ``response``.
        """
        return self._json(  # type: ignore[return-value]
            self._client.post("/turn", json={"message": message, "session_id": session_id})
        )

    def wipe(self) -> Dict[str, object]:
        """Clear the eval graph and captures for the next user or run.

        Returns:
            The wait, and any facts that remain.
        """
        return self._json(self._client.post("/wipe"))  # type: ignore[return-value]

    def settle(self, trace_ids: List[str]) -> Dict[str, object]:
        """Wait until the gateway is quiet and check that the turns reached the graph.

        Args:
            trace_ids: The turns that must be in the graph.

        Returns:
            The wait, the missing turns and the count of budget denials.
        """
        return self._json(  # type: ignore[return-value]
            self._client.post("/settle", json={"trace_ids": trace_ids})
        )

    def memories(self) -> List[Dict[str, str]]:
        """Read the structured facts of the eval graph.

        Returns:
            One record per fact.
        """
        return self._json(self._client.get("/memories"))  # type: ignore[return-value]

    def spend(self) -> Dict[str, object]:
        """Read the spend.

        Returns:
            ``spent_usd``, ``cap_usd`` and ``stop``.
        """
        return self._json(self._client.get("/spend"))  # type: ignore[return-value]

    def check_spend(self) -> None:
        """Refuse at the cap.

        Raises:
            SpendCapReached: At or above the cap.
        """
        spend = self.spend()
        if spend["stop"]:
            raise SpendCapReached(f"spent {spend['spent_usd']} USD, cap {spend['cap_usd']} USD")

    def report_usage(self, prompt_tokens: int, completion_tokens: int) -> None:
        """Report one MemAudit call. This never raises.

        ``llm_client.get_completion`` calls its usage hook inside a broad ``except`` and then retries, so an
        exception here pays for the call again.

        Args:
            prompt_tokens: Input tokens.
            completion_tokens: Output tokens.
        """
        try:
            self._client.post(
                "/usage",
                json={"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens},
            )
        except httpx.HTTPError as exc:
            print(f"[fre1728] usage report failed, spend undercounts: {exc}")


class SeshatAgent(MemoryAgent):
    """Seshat on the eval gateway, as a MemAudit agent.

    A task is a new session. A later turn sends only the user's latest message, because the gateway holds the
    session already, while ``Episode`` replays the task and the dialogue in ``task``.
    """

    def __init__(self, bridge: BridgeClient) -> None:
        """Bind to the bridge.

        Args:
            bridge: The bridge client.
        """
        self._bridge = bridge
        self._session_id: Optional[str] = None
        self.trace_ids: List[str] = []

    def __call__(self, task: str, history: List[Dict]) -> str:
        """Drive one turn.

        Args:
            task: The task on the first turn. Later, the task plus the replay, which is ignored.
            history: The prior turns of this task.

        Returns:
            The agent's reply.

        Raises:
            HarnessFailure: If the simulated user's message is empty.
        """
        if not history:
            self._session_id = None
            message = task
        else:
            message = history[-1]["user"]
        if not message or not message.strip():
            raise HarnessFailure("the simulated user's message is empty")
        reply = self._bridge.turn(message, self._session_id)
        self._session_id = reply["session_id"]
        self.trace_ids.append(reply["trace_id"])
        return reply["response"]

    def ask(self, question: str) -> str:
        """Ask one question in a fresh session.

        Args:
            question: The question.

        Returns:
            The agent's reply.
        """
        reply = self._bridge.turn(question, None)
        return reply["response"]

    def get_all_memories(self) -> List[Dict]:
        """Read the eval graph.

        Returns:
            One record per fact, each with a ``content`` field.
        """
        return list(self._bridge.memories())

    def save_memories(self, path: str) -> None:
        """Write the eval graph's facts to a JSON file.

        Args:
            path: The file.
        """
        memories = self.get_all_memories()
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            json.dump({"num_memories": len(memories), "memories": memories}, handle, indent=2)

    def reset(self) -> None:
        """Clear the eval graph and captures."""
        self._bridge.wipe()
        self._session_id = None
        self.trace_ids = []
