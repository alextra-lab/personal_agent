"""Check one Mermaid diagram with mmdc and print VALID, INVALID or ERROR.

The check exists because the exit code of mmdc is not enough (FRE-1579, mmdc 11.17.0):

- A broken ``pie`` diagram exits 0 and writes an SVG. The SVG is mermaid's error diagram.
- A diagram of more than 50,000 characters exits 0 and writes an SVG of a placeholder. Mermaid
  does not read the diagram at all.
- A browser failure exits 1 and looks like a diagram failure.
- A fixed output path keeps an old SVG, and a file test then calls a broken diagram valid.

This script writes the SVG into a new directory on every run, and it removes the directory
afterwards. It uses the standard library only.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
import tempfile
from collections.abc import Sequence
from pathlib import Path
from typing import NoReturn

# The bash tool stops a command after 30 seconds by default. The script must give its own
# answer first, or the model sees a bash time-out and no verdict.
DEFAULT_TIMEOUT_SECONDS = 25.0
EXIT_VALID = 0
EXIT_INVALID = 1
EXIT_ERROR = 2
# Mermaid replaces a longer text with a placeholder diagram (its maxTextSize default).
MAX_DIAGRAM_CHARS = 50_000

_MAX_ERROR_CHARS = 1200
_ROOT_TAG_CHARS = 4096
# The shell uses 126 (not executable) and 127 (not found) when the wrapper of mmdc fails.
_SHELL_FAILURE_CODES = (126, 127)
# A line of a JavaScript stack: "    at fn (file:1:2)", or "fn (https://x/y.mjs:1523:21)".
_STACK_FRAME = re.compile(r"^\s+at \S|^\S+ \(\S+:\d+:\d+\)$")
# The first line of a browser, launcher or configuration failure. The match starts at the line
# start, so diagram text that mmdc echoes later in a message cannot trigger it.
_BROWSER_FAILURE = re.compile(
    r"^(?:Error: )?(?:"
    r"Tried to find the browser|Could not find (?:Chrome|Chromium|browser|expected browser)"
    r"|Browser was not found|Failed to launch|Configuration file|Protocol error"
    r"|Navigation failed because browser|spawn "
    r")|^TimeoutError: Timed out after"
)
_ROOT_TAG = re.compile(r"<svg\b[^>]*>")
_ERROR_MARK = 'aria-roledescription="error"'
_NOT_RUN = "Do not repair the diagram. Show the source and say that it was not validated."


def build_command(source: Path, output: Path) -> list[str]:
    """Build the mmdc command.

    Args:
        source: The ``.mmd`` file with the diagram, without a code fence.
        output: The SVG file that mmdc writes.

    Returns:
        The argument list. It holds no option that the permission rules refuse.
    """
    return ["mmdc", "-i", str(source), "-o", str(output)]


def _error_text(raw: str) -> str:
    """Return the message of an mmdc failure, cut before the first stack frame."""
    kept: list[str] = []
    for line in raw.splitlines():
        if _STACK_FRAME.match(line):
            break
        kept.append(line.rstrip())
    return "\n".join(kept).strip()[:_MAX_ERROR_CHARS]


def _is_error_diagram(svg: Path) -> bool:
    """Report whether the SVG is mermaid's error diagram (the root tag says so)."""
    head = svg.read_text(encoding="utf-8", errors="replace")[:_ROOT_TAG_CHARS]
    root = _ROOT_TAG.search(head)
    return root is not None and _ERROR_MARK in root.group(0)


def _run_mmdc(source: Path, scratch: str, timeout: float) -> tuple[int, str]:
    """Run mmdc into ``scratch`` and judge the result.

    Args:
        source: The ``.mmd`` file.
        scratch: A new, empty directory for the SVG.
        timeout: Seconds that mmdc may run.

    Returns:
        The exit code and the report text.
    """
    output = Path(scratch) / "diagram.svg"
    try:
        done = subprocess.run(
            build_command(source, output),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError:
        return EXIT_ERROR, (
            "ERROR\nmmdc is not installed or is not on PATH, so the check could not run. "
            + _NOT_RUN
        )
    except subprocess.TimeoutExpired:
        return EXIT_ERROR, (
            f"ERROR\nmmdc timed out after {timeout:g} seconds, so the check could not run. "
            + _NOT_RUN
        )
    except OSError as exc:
        return EXIT_ERROR, (
            f"ERROR\nmmdc could not be started ({exc}), so the check could not run. " + _NOT_RUN
        )
    detail = _error_text(done.stderr or done.stdout)
    first = next((line for line in detail.splitlines() if line.strip()), "")
    if done.returncode < 0 or done.returncode in _SHELL_FAILURE_CODES:
        return EXIT_ERROR, (
            f"ERROR\nmmdc did not run to the end (exit status {done.returncode}), "
            f"so the check could not run. {_NOT_RUN}\n{detail}".rstrip()
        )
    if done.returncode != 0 and _BROWSER_FAILURE.match(first):
        return EXIT_ERROR, (
            "ERROR\nmmdc could not start or use its browser. This is not a diagram error, "
            f"so the check could not run. {_NOT_RUN}\n{detail}"
        )
    if done.returncode != 0:
        return EXIT_INVALID, (
            f"INVALID\n{detail or 'mmdc failed and gave no message.'}\n"
            "Fix the diagram, write it again, and run the check again. If the message is about "
            "the browser or the system and not about the diagram, the check could not run. "
            "Then do not repair the diagram."
        )
    if not output.is_file() or output.stat().st_size == 0:
        return EXIT_INVALID, (
            "INVALID\nmmdc exited 0 but wrote no SVG, so nothing proves the diagram parses.\n"
            "Fix the diagram, write it again, and run the check again."
        )
    if _is_error_diagram(output):
        return EXIT_INVALID, (
            "INVALID\nmmdc drew mermaid's error diagram ('Syntax error in text'). "
            "It exits 0 for some broken diagrams. The diagram has a syntax error.\n"
            "Compare it with the syntax rules for its type, write it again, "
            "and run the check again."
        )
    return EXIT_VALID, "VALID"


def check(source: Path, timeout: float) -> tuple[int, str]:
    """Check one diagram file with mmdc.

    Args:
        source: The ``.mmd`` file with the diagram, without a code fence.
        timeout: Seconds that mmdc may run.

    Returns:
        The exit code (0 valid, 1 invalid, 2 the check could not run) and the report text.
    """
    # An allow rule runs this script without a card (FRE-1585), and mmdc echoes the text of a
    # parse error. So the script reads only a diagram file: a .mmd file that is not a link. mmdc
    # gets the absolute path, which cannot start with '-' and be read as an option.
    if source.suffix != ".mmd" or source.is_symlink():
        return EXIT_ERROR, (
            f"ERROR\nThe file {source} is not a .mmd file, or it is a link. "
            "Write the diagram to /tmp/m.mmd with the write tool, then run the check again."
        )
    if not source.is_file():
        return EXIT_ERROR, (
            f"ERROR\nThe file {source} was not found. "
            "Write the diagram to this path, then run the check again."
        )
    source = source.absolute()
    try:
        size = len(source.read_text(encoding="utf-8", errors="replace"))
    except OSError as exc:
        return EXIT_ERROR, f"ERROR\nThe file {source} could not be read ({exc}). " + _NOT_RUN
    if size > MAX_DIAGRAM_CHARS:
        return EXIT_INVALID, (
            f"INVALID\nThe diagram has {size} characters. Mermaid replaces a text of more than "
            f"{MAX_DIAGRAM_CHARS} characters with a placeholder, so nothing proves that it "
            "parses.\nSplit it into smaller diagrams and check each one."
        )
    try:
        scratch = tempfile.TemporaryDirectory(prefix="mermaid-check-", ignore_cleanup_errors=True)
    except OSError as exc:
        return EXIT_ERROR, (
            f"ERROR\nNo scratch directory could be made ({exc}), so the check could not run. "
            + _NOT_RUN
        )
    with scratch as path:
        return _run_mmdc(source, path, timeout)


class _Parser(argparse.ArgumentParser):
    """An argument parser whose usage error is one ``error:`` line on stderr, exit 2.

    This is the contract of the skill scripts (tests/docs/test_skill_scripts.py). A verdict
    of the check itself goes to stdout, so a usage error and a verdict cannot be confused.
    """

    def error(self, message: str) -> NoReturn:
        """Print the usage error as one line and exit 2."""
        sys.stderr.write(f"error: {message}\n")
        raise SystemExit(EXIT_ERROR)


def main(argv: Sequence[str] | None = None) -> int:
    """Run the check from the command line.

    Args:
        argv: The arguments. ``None`` reads ``sys.argv``.

    Returns:
        The exit code: 0 valid, 1 invalid, 2 the check could not run.
    """
    parser = _Parser(
        prog="validate_mermaid.py",
        description=(
            "Check one Mermaid diagram with mmdc. The first line of the output is VALID "
            "(exit 0), INVALID (exit 1: fix the diagram) or ERROR (exit 2: the check could not "
            "run, so do not repair the diagram)."
        ),
    )
    parser.add_argument("diagram", type=Path, help="the .mmd file; the diagram without a fence")
    parser.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_TIMEOUT_SECONDS,
        help=f"seconds that mmdc may run (default {DEFAULT_TIMEOUT_SECONDS:g})",
    )
    args = parser.parse_args(argv)
    code, report = check(args.diagram, args.timeout)
    sys.stdout.write(report + "\n")
    return code


if __name__ == "__main__":
    sys.exit(main())
