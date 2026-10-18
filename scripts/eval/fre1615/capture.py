"""FRE-1615 Phase 0: capture the primary's request body of each suite task, with no model call.

Runs each task of ``suite.yaml`` as a turn through the FRE-1537 probe gateway, whose model is the recording
stub. A follow-up task runs its scripted first turn, then its message, in one session. The primary's body of the
task's last turn goes to ``<run>/captured/<id>.json`` with the task and the Stage 4 intent of the message.

Prerequisite: ``uv run python -m scripts.eval.fre1537.gateway up --run-dir <abs run>`` (see
``scripts/eval/fre1537/README.md``). Run from the repo root.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path

import httpx
import yaml
from scripts.eval.fre1537 import capture
from scripts.eval.fre1537.common import RunPaths, utc_now

from personal_agent.request_gateway.intent import classify_intent

SUITE = Path(__file__).with_name("suite.yaml")


def load_suite(path: Path = SUITE) -> list[dict[str, str]]:
    """Return the tasks of the suite file."""
    return list(yaml.safe_load(path.read_text())["tasks"])


def main(argv: Sequence[str] | None = None) -> int:
    """Run the CLI."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--chat-url", default=capture.DEFAULT_CHAT_URL)
    parser.add_argument("--only", nargs="*", help="task ids to capture (default: all)")
    args = parser.parse_args(argv)
    paths = RunPaths(args.run_dir.resolve())
    paths.captured.mkdir(parents=True, exist_ok=True)
    with httpx.Client() as client:
        for task in load_suite():
            if args.only and task["id"] not in args.only:
                continue
            session_id = None
            if task.get("history_user"):
                session_id, _ = capture.turn(
                    client,
                    args.chat_url,
                    paths,
                    task["history_user"],
                    task["history_assistant"],
                    None,
                )
            _, files = capture.turn(client, args.chat_url, paths, task["message"], "OK", session_id)
            intent = classify_intent(task["message"])
            record = {
                "task": task,
                "captured_at": utc_now(),
                "intent": {
                    "task_type": intent.task_type.value,
                    "complexity": intent.complexity.value,
                },
                "body": capture.primary_body(files, task["message"]),
            }
            (paths.captured / f"{task['id']}.json").write_text(json.dumps(record, indent=1))
            print(task["id"], intent.task_type.value, len(record["body"]["tools"]), "tools")
    return 0


if __name__ == "__main__":
    sys.exit(main())
