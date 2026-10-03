"""Approval-denial check for eval harness turns (FRE-1539).

FRE-1535 made tool approval fail closed. A harness turn on the production gateway has no PWA
WebSocket, so each approval-capable tool call is denied. The model continues without the tool, the
turn completes, and it looks valid. It measures a different behaviour. This module decides, for one
trace, whether any approval-required tool call was denied.

Evidence (the executor denies with ``ToolResult.error == "Permission denied: approval_<reason>"``):

- **The Captain's Log capture** is primary. It is one document per turn. Its ``tool_results``
  carries every dispatched call, including a denied one. A capture that was read and shows no
  denial proves the primary turn had none.
- **``agent-logs`` events** are positive evidence only. FRE-1051: that index loses events, so an
  absent event proves nothing. The events still name the sub-agent denial. The capture does not.
- **Sub-agent captures** record the name of every attempted tool in ``rounds``. They show whether a
  sub-agent tried an approval-capable tool. They do not show the outcome of the call.

Verdicts: ``invalid`` (a denial was seen), ``valid`` (the evidence proves a clean turn), or
``unverified`` (the evidence cannot prove it). A harness counts only ``valid`` turns in a rate.

Limits that remain:

- The approval-capable tool set comes from this checkout's ``config/governance/tools.yaml``, not
  from the deployed copy.
- A lost sub-agent capture is detected only when ``sub_agent_start`` events outnumber the sub-agent
  capture documents. Lost log events can hide that gap.

Run on one trace (exit 0 valid, 1 invalid, 2 unverified)::

    uv run python -m scripts.eval.approval_denial <trace_id> --es-url <elasticsearch url>
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import httpx
import yaml  # type: ignore[import-untyped]

Validity = Literal["valid", "invalid", "unverified"]
DenialSource = Literal["capture", "log"]

#: ``ToolResult.error`` of a denied call (``tools/executor.py``): the prefix, then the reason.
DENIAL_ERROR_PREFIX = "Permission denied: "
#: Every approval reason ``PermissionResult`` returns starts with this.
APPROVAL_REASON_PREFIX = "approval_"

#: Log events that record a denial. The first three come from ``tools/executor.py``, the last from
#: ``orchestrator/sub_agent.py`` (the sub-agent owner's gate, FRE-1461).
LOG_EVENT_APPROVAL_DENIED = "approval_denied"
LOG_EVENT_NO_SESSION_ID = "approval_denied_no_session_id"
LOG_EVENT_NO_TRANSPORT = "approval_denied_no_transport"
LOG_EVENT_SUB_AGENT_DENIED = "sub_agent_tool_approval_denied"
LOG_EVENT_SUB_AGENT_START = "sub_agent_start"
DENIAL_LOG_EVENTS = frozenset(
    {
        LOG_EVENT_APPROVAL_DENIED,
        LOG_EVENT_NO_SESSION_ID,
        LOG_EVENT_NO_TRANSPORT,
        LOG_EVENT_SUB_AGENT_DENIED,
    }
)

LOGS_INDEX = "agent-logs-*"
#: The sub-agent audit index is a sibling under the same wildcard. Without the exclusion a
#: sub-agent document would pass for the turn's own capture (same pattern as
#: ``captains_log.capture.read_session_captures``).
SUBAGENT_CAPTURES_INDEX = "agent-captains-captures-subagents-*"
CAPTURES_INDEX = f"agent-captains-captures-*,-{SUBAGENT_CAPTURES_INDEX}"

DEFAULT_TOOLS_YAML = Path(__file__).resolve().parents[2] / "config" / "governance" / "tools.yaml"

#: A turn no check was run on (a re-read of an older pass). It stays in the rates it was in.
UNASSESSED = "unassessed"

UNVERIFIED_CAPTURE_NOT_FOUND = "capture_not_found"
UNVERIFIED_SUB_AGENT_TOOL = "sub_agent_approval_capable_tool"
UNVERIFIED_SUB_AGENT_MISSING = "sub_agent_capture_missing"
UNVERIFIED_TOOLS_YAML = "tools_yaml_unreadable"
UNVERIFIED_ES_ERROR = "es_error"


@dataclass(frozen=True)
class ApprovalDenial:
    """One denied approval-required tool call.

    Attributes:
        tool_name: The tool whose call was denied.
        reason: The normalised reason, such as ``approval_connection_lost``.
        source: Where the denial was read, ``capture`` or ``log``.
    """

    tool_name: str
    reason: str
    source: DenialSource


@dataclass(frozen=True)
class ApprovalVerdict:
    """The validity of one turn, with the evidence for it.

    Attributes:
        validity: ``valid``, ``invalid`` or ``unverified``.
        denials: Every denial found, one entry per tool and reason.
        capture_found: Whether the turn's own capture document was read.
        unverified_reason: Why the evidence could not prove a clean turn. ``None`` otherwise.
    """

    validity: Validity
    denials: tuple[ApprovalDenial, ...] = ()
    capture_found: bool = False
    unverified_reason: str | None = None

    def as_dict(self) -> dict[str, object]:
        """Return the verdict as a JSON-safe mapping for a result row.

        Returns:
            The validity, the denials, whether the capture was read, and the unverified reason.
        """
        return {
            "validity": self.validity,
            "denials": [
                {"tool_name": d.tool_name, "reason": d.reason, "source": d.source}
                for d in self.denials
            ],
            "capture_found": self.capture_found,
            "unverified_reason": self.unverified_reason,
        }

    def summary(self) -> str:
        """Return a one-line text for a console report.

        Returns:
            ``valid``, or the validity followed by the denials or the unverified reason.
        """
        if self.denials:
            listed = ", ".join(f"{d.tool_name}({d.reason})" for d in self.denials)
            return f"{self.validity}: {listed}"
        if self.unverified_reason:
            return f"{self.validity}: {self.unverified_reason}"
        return self.validity


def in_rates(validity: str) -> bool:
    """Say whether a turn with this validity belongs in a rate.

    Args:
        validity: ``valid``, ``invalid``, ``unverified`` or ``unassessed``.

    Returns:
        ``True`` for ``valid`` and ``unassessed``. An invalid turn measured another behaviour,
        and an unverified turn could not be proven clean. Neither is in a rate.
    """
    return validity in ("valid", UNASSESSED)


def _mappings(value: object) -> list[Mapping[str, object]]:
    """Return the mapping items of a list value, or an empty list for anything else."""
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, Mapping)]


def _text(value: object) -> str:
    return value if isinstance(value, str) else ""


def denials_from_tool_results(
    tool_results: Sequence[Mapping[str, object]],
) -> tuple[ApprovalDenial, ...]:
    """Find the denied approval calls in a capture's ``tool_results``.

    Args:
        tool_results: The capture's ``tool_results`` entries.

    Returns:
        One denial per entry whose error is ``Permission denied: approval_<reason>``. An entry
        that failed for another reason, or that succeeded, is not a denial.
    """
    found: list[ApprovalDenial] = []
    for result in tool_results:
        error = _text(result.get("error"))
        reason = error.removeprefix(DENIAL_ERROR_PREFIX)
        if error.startswith(DENIAL_ERROR_PREFIX) and reason.startswith(APPROVAL_REASON_PREFIX):
            found.append(ApprovalDenial(_text(result.get("tool_name")), reason, "capture"))
    return tuple(found)


def _event_name(event: Mapping[str, object]) -> str:
    """Return the event name. ES documents key it ``event_type``, local log files ``event``."""
    return _text(event.get("event_type")) or _text(event.get("event"))


def denials_from_events(events: Sequence[Mapping[str, object]]) -> tuple[ApprovalDenial, ...]:
    """Find the denied approval calls in ``agent-logs`` events.

    Args:
        events: Log events for one trace.

    Returns:
        One denial per denial event. ``approval_ui_disabled_proceeding`` is the eval opt-out. The
        tool ran, so it is not a denial and not in the result.
    """
    found: list[ApprovalDenial] = []
    for event in events:
        name = _event_name(event)
        tool = _text(event.get("tool_name"))
        if name == LOG_EVENT_APPROVAL_DENIED:
            reason = f"{APPROVAL_REASON_PREFIX}{_text(event.get('decision')) or 'unknown'}"
        elif name == LOG_EVENT_NO_SESSION_ID:
            reason = f"{APPROVAL_REASON_PREFIX}no_session_id"
        elif name == LOG_EVENT_NO_TRANSPORT:
            reason = f"{APPROVAL_REASON_PREFIX}no_transport"
        elif name == LOG_EVENT_SUB_AGENT_DENIED:
            reason = f"sub_agent_{_text(event.get('reason')) or 'unknown'}"
        else:
            continue
        found.append(ApprovalDenial(tool, reason, "log"))
    return tuple(found)


def approval_capable_tools(tools_yaml: Path = DEFAULT_TOOLS_YAML) -> frozenset[str]:
    """Return the tools that can need approval, in any mode.

    The set is a superset on purpose: a mode change at the gateway then cannot hide a tool.

    Args:
        tools_yaml: The governance tool policy file.

    Returns:
        Every tool with ``requires_approval: true`` or a non-empty ``requires_approval_in_modes``.

    Raises:
        OSError: If the file cannot be read.
        yaml.YAMLError: If the file is not valid YAML.
        KeyError: If the file has no ``tools`` mapping.
    """
    tools = yaml.safe_load(tools_yaml.read_text())["tools"]
    return frozenset(
        name
        for name, policy in tools.items()
        if policy.get("requires_approval") or policy.get("requires_approval_in_modes")
    )


def _dedupe(denials: Sequence[ApprovalDenial]) -> tuple[ApprovalDenial, ...]:
    """Keep the first denial for each tool and reason. The capture comes first, so it wins."""
    seen: dict[tuple[str, str], ApprovalDenial] = {}
    for denial in denials:
        seen.setdefault((denial.tool_name, denial.reason), denial)
    return tuple(seen.values())


def assess(
    capture_docs: Sequence[Mapping[str, object]],
    events: Sequence[Mapping[str, object]],
    sub_agent_docs: Sequence[Mapping[str, object]] = (),
    approval_capable: frozenset[str] = frozenset(),
) -> ApprovalVerdict:
    """Decide the validity of one turn from its evidence.

    Args:
        capture_docs: The turn's Captain's Log capture documents.
        events: ``agent-logs`` events for the trace (denial events and ``sub_agent_start``).
        sub_agent_docs: The turn's sub-agent capture documents.
        approval_capable: Tools that can need approval, from :func:`approval_capable_tools`.

    Returns:
        ``invalid`` when any denial is found. ``unverified`` when no capture was read, when a
        sub-agent capture is missing, or when a sub-agent tried an approval-capable tool and no
        denial was logged. ``valid`` otherwise.
    """
    from_capture = tuple(
        denial
        for doc in capture_docs
        for denial in denials_from_tool_results(_mappings(doc.get("tool_results")))
    )
    denials = _dedupe([*from_capture, *denials_from_events(events)])
    capture_found = bool(capture_docs)
    if denials:
        return ApprovalVerdict("invalid", denials, capture_found)
    if not capture_found:
        return ApprovalVerdict("unverified", (), False, UNVERIFIED_CAPTURE_NOT_FOUND)
    starts = sum(1 for event in events if _event_name(event) == LOG_EVENT_SUB_AGENT_START)
    if starts > len(sub_agent_docs):
        return ApprovalVerdict("unverified", (), True, UNVERIFIED_SUB_AGENT_MISSING)
    tried = sorted(
        {
            _text(call.get("tool"))
            for doc in sub_agent_docs
            for call in _mappings(doc.get("rounds"))
            if _text(call.get("tool")) in approval_capable
        }
    )
    if tried:
        reason = f"{UNVERIFIED_SUB_AGENT_TOOL}:{','.join(tried)}"
        return ApprovalVerdict("unverified", (), True, reason)
    return ApprovalVerdict("valid", (), True)


def merge(verdicts: Sequence[ApprovalVerdict]) -> ApprovalVerdict:
    """Combine the verdicts of several turns into one, for a case that spans turns.

    Args:
        verdicts: One verdict per turn.

    Returns:
        ``invalid`` with all denials if any turn is invalid. Else ``unverified`` with the first
        reason if any turn is unverified. Else ``valid``.
    """
    denials = _dedupe([d for v in verdicts for d in v.denials])
    capture_found = all(v.capture_found for v in verdicts)
    if any(v.validity == "invalid" for v in verdicts):
        return ApprovalVerdict("invalid", denials, capture_found)
    unverified = [v for v in verdicts if v.validity == "unverified"]
    if unverified:
        return ApprovalVerdict("unverified", (), capture_found, unverified[0].unverified_reason)
    return ApprovalVerdict("valid", (), capture_found)


def _search(
    client: httpx.Client, es_url: str, index: str, body: Mapping[str, object]
) -> list[Mapping[str, object]]:
    """Run one search and return the ``_source`` of each hit."""
    resp = client.post(f"{es_url.rstrip('/')}/{index}/_search", json=body, timeout=30.0)
    resp.raise_for_status()
    hits = _mappings(resp.json().get("hits", {}).get("hits"))
    return [source for hit in hits if isinstance(source := hit.get("_source"), Mapping)]


def check_turn(
    trace_id: str,
    *,
    es_url: str,
    logs_index: str = LOGS_INDEX,
    captures_index: str = CAPTURES_INDEX,
    subagent_index: str = SUBAGENT_CAPTURES_INDEX,
    wait_s: float = 60.0,
    poll_s: float = 3.0,
    client: httpx.Client | None = None,
) -> ApprovalVerdict:
    """Read the evidence for one turn from Elasticsearch and decide its validity.

    The capture is written after the turn returns, so this polls for it up to ``wait_s``. The
    events and the sub-agent captures are read once, after the capture lands. Async callers run
    this in ``asyncio.to_thread``.

    Args:
        trace_id: The turn's trace id.
        es_url: Elasticsearch base URL.
        logs_index: The ``agent-logs`` index pattern.
        captures_index: The turn capture index pattern. It must exclude the sub-agent index.
        subagent_index: The sub-agent capture index pattern.
        wait_s: How long to wait for the capture document. ``0`` tries once.
        poll_s: The pause between capture polls.
        client: An open client. When ``None``, one is opened and closed here.

    Returns:
        The verdict. An Elasticsearch or policy-file failure gives ``unverified``. It never raises.
    """
    try:
        capable = approval_capable_tools()
    except (OSError, yaml.YAMLError, KeyError, AttributeError):
        return ApprovalVerdict("unverified", (), False, UNVERIFIED_TOOLS_YAML)
    http = client if client is not None else httpx.Client()
    try:
        capture_body = {
            "size": 5,
            "query": {"term": {"trace_id": trace_id}},
            "_source": [
                "trace_id",
                "tool_results.tool_name",
                "tool_results.success",
                "tool_results.error",
            ],
        }
        deadline = time.monotonic() + wait_s
        while True:
            captures = _search(http, es_url, captures_index, capture_body)
            if captures or time.monotonic() >= deadline:
                break
            time.sleep(poll_s)
        events = _search(
            http,
            es_url,
            logs_index,
            {
                "size": 500,
                "query": {
                    "bool": {
                        "filter": [
                            {"term": {"trace_id": trace_id}},
                            {
                                "terms": {
                                    "event_type": sorted(
                                        DENIAL_LOG_EVENTS | {LOG_EVENT_SUB_AGENT_START}
                                    )
                                }
                            },
                        ]
                    }
                },
            },
        )
        subs = _search(
            http,
            es_url,
            subagent_index,
            {
                "size": 100,
                "query": {"term": {"trace_id": trace_id}},
                "_source": ["task_id", "rounds.tool"],
            },
        )
    except httpx.HTTPError as exc:
        return ApprovalVerdict(
            "unverified", (), False, f"{UNVERIFIED_ES_ERROR}: {type(exc).__name__}"
        )
    finally:
        if client is None:
            http.close()
    return assess(captures, events, subs, capable)


def main(argv: Sequence[str] | None = None) -> int:
    """Check one trace and print the verdict as JSON.

    Args:
        argv: Command-line arguments. ``None`` reads ``sys.argv``.

    Returns:
        0 for ``valid``, 1 for ``invalid``, 2 for ``unverified``.
    """
    parser = argparse.ArgumentParser(description="FRE-1539: was an approval tool denied in a turn?")
    parser.add_argument("trace_id")
    parser.add_argument("--es-url", required=True, help="Elasticsearch base URL.")
    parser.add_argument("--wait-s", type=float, default=10.0, help="Wait for the capture document.")
    args = parser.parse_args(argv)
    verdict = check_turn(args.trace_id, es_url=args.es_url, wait_s=args.wait_s)
    sys.stdout.write(json.dumps({"trace_id": args.trace_id, **verdict.as_dict()}, indent=2) + "\n")
    return {"valid": 0, "invalid": 1, "unverified": 2}[verdict.validity]


if __name__ == "__main__":
    sys.exit(main())
