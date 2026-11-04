"""FRE-1730 AC-2: read from a session transcript what the model received.

The check reads the requests that the transcript records, not the hook's own output. A hook can run and its text can
still not reach the model.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

Arm = Literal["plugin", "control"]
ARMS: tuple[Arm, Arm] = ("plugin", "control")
#: A file of each kind must reach the model: the owner's global file and the repo's file.
_REQUIRED_INSTRUCTIONS = frozenset({"User", "Project"})


@dataclass(frozen=True)
class TranscriptFacts:
    """What one transcript shows.

    Attributes:
        marker_sent: The marker is in a request to the model.
        instructions_sent: The kinds of CLAUDE.md file whose text is in a request to the model.
        request_count: The count of requests that the transcript records.
    """

    marker_sent: bool
    instructions_sent: frozenset[str]
    request_count: int


def read_transcript(path: Path, marker: str) -> TranscriptFacts:
    """Read one session transcript.

    Args:
        path: The session's ``.jsonl`` file.
        marker: The text that only the plugin adds.

    Returns:
        The facts.
    """
    sent_parts: list[str] = []
    instructions: list[dict[str, object]] = []
    request_count = 0
    for line in path.read_text().splitlines():
        entry = json.loads(line)
        kind = entry.get("type")
        if kind == "api-request-blob":
            sent_parts.append(json.dumps(entry.get("message"), ensure_ascii=False))
        elif kind == "api-request":
            request_count += 1
        elif kind == "attachment" and entry["attachment"].get("type") == "instructions":
            instructions += entry["attachment"].get("files", [])
    sent = "\n".join(sent_parts)
    delivered = {
        str(file["type"])
        for file in instructions
        if file.get("content")
        and json.dumps(str(file["content"]), ensure_ascii=False)[1:-1] in sent
    }
    return TranscriptFacts(marker in sent, frozenset(delivered), request_count)


def arm_problems(facts: TranscriptFacts, arm: Arm) -> Sequence[str]:
    """Check one session against its arm.

    Args:
        facts: What the transcript shows.
        arm: The session's arm.

    Returns:
        Each problem. An empty result means that the session is valid.
    """
    problems = []
    if facts.request_count == 0:
        problems.append("the transcript records no request to the model")
    if arm == "plugin" and not facts.marker_sent:
        problems.append("plugin arm: the marker did not reach the model")
    if arm == "control" and facts.marker_sent:
        problems.append("control arm: the marker reached the model")
    for kind in sorted(_REQUIRED_INSTRUCTIONS - facts.instructions_sent):
        problems.append(f"{arm} arm: the {kind} CLAUDE.md did not reach the model")
    return problems
