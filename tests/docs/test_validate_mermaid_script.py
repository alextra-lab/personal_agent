"""ADR-0156 D5.4 (FRE-1579 AC-2): the ``mmdc`` check of the mermaid-diagrams skill is a script.

The script runs ``mmdc`` and prints ``VALID``, ``INVALID`` or ``ERROR``. These tests use a fake
``mmdc`` that repeats what the real ``@mermaid-js/mermaid-cli`` 11.17.0 did when this file was
written (FRE-1579, observed on the binary):

- a parse error: exit 1, no SVG, the error and a JavaScript stack on stderr;
- a broken ``pie`` diagram: exit 0 and an SVG that is mermaid's error diagram, marked by
  ``aria-roledescription="error"`` on the root tag;
- a diagram of more than 50,000 characters: exit 0 and an SVG of a placeholder diagram, so a
  broken diagram of that size is not read at all;
- a browser failure: exit 1, no SVG, a first line that starts ``Error: Tried to find the
  browser``, ``Error: Failed to launch the browser process`` or ``Configuration file``. It looks
  like a diagram failure and is not one. The other first lines in this file are the messages of
  puppeteer and node for the same kind of failure.

One test runs the real ``mmdc`` when it is on ``PATH``. A machine without ``mmdc``, or where its
browser cannot start, skips it.
"""

from __future__ import annotations

import importlib.util
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

from personal_agent.governance.permissions import evaluate_permission, load_permission_rules
from personal_agent.tools.primitives.bash import _DEFAULT_TIMEOUT as _BASH_DEFAULT_TIMEOUT

_SCRIPT = (
    Path(__file__).resolve().parents[2]
    / "docs"
    / "skills"
    / "mermaid-diagrams"
    / "scripts"
    / "validate_mermaid.py"
)

_PRELUDE = """#!/bin/sh
while [ $# -gt 0 ]; do
  case "$1" in -o) out="$2"; shift ;; esac
  shift
done
echo "Generating single mermaid chart"
"""

_GOOD_SVG = '<svg id="my-svg" role="graphics-document document" aria-roledescription="flowchart-v2"'
_GOOD = f"""printf '%s' '{_GOOD_SVG}><text>Syntax error in text</text></svg>' > "$out"\n"""
_NO_FILE = "exit 0\n"
_EMPTY_FILE = ': > "$out"\nexit 0\n'
_ERROR_DIAGRAM = (
    'printf \'%s\' \'<svg id="my-svg" role="graphics-document document" '
    'aria-roledescription="error"><text>Syntax error in text</text></svg>\' > "$out"\n'
)
_PARSE_ERROR = """cat >&2 <<'EOT'

Error: Parse error on line 2:
...wchart TD    A --> end
----------------------^
Expecting 'AMP', 'COLON', 'PIPE', got 'end'
Parser.parseError (https://mermaid-cli-intercept.invalid/x/chunk.mjs:1523:21)
    at #evaluate (file:///x/ExecutionContext.js:402:19)
EOT
exit 1
"""
_PARSE_ERROR_WITH_BROWSER_WORD = """cat >&2 <<'EOT'

Error: Parse error on line 1:
Browser --> end
-------------^
Expecting 'AMP', got 'end'
    at #evaluate (file:///x/ExecutionContext.js:402:19)
EOT
exit 1
"""
_NO_BROWSER = """cat >&2 <<'EOT'

Error: Tried to find the browser at the configured path (/x/chrome), but no executable was found.
    at ChromeLauncher.resolveExecutablePath (file:///x/BrowserLauncher.js:363:23)
EOT
exit 1
"""
_LAUNCH_FAILED = """cat >&2 <<'EOT'

Error: Failed to launch the browser process:  Code: null

stderr:
[1:1:FATAL:process_singleton_posix.cc:313] Socket path too long: /x/SingletonSocket

    at ChromeLauncher.launch (file:///x/ChromeLauncher.js:92:19)
EOT
exit 1
"""
_NO_CONFIG = """cat >&2 <<'EOT'

Configuration file "/x/pp.json" doesn't exist

EOT
exit 1
"""
# Output that is not UTF-8: the script must not raise.
_BINARY_OUTPUT = r"""printf '\377\376 not utf-8\n' >&2
exit 1
"""
_SLOW = "exec sleep 5\n"


def _fails_with(first_line: str) -> str:
    """Return a fake body that exits 1 with ``first_line`` and then a stack frame."""
    return f"cat >&2 <<'EOT'\n\n{first_line}\n    at fn (file:///x/y.js:1:2)\nEOT\nexit 1\n"


# Other first lines of a browser or launcher failure (puppeteer and node messages).
_OTHER_BROWSER_FAILURES = {
    "timed-out": "TimeoutError: Timed out after 30000 ms while waiting for the WebSocket URL",
    "protocol": "Error: Protocol error (Target.setDiscoverTargets): Target closed",
    "disconnected": "Navigation failed because browser has disconnected!",
    "spawn": "Error: spawn /x/chrome ENOENT",
    "expected": "Error: Could not find expected browser (chrome) locally.",
    "not-found": "Error: Browser was not found at the configured executablePath (/x)",
}

# The fakes call cat, dirname and sleep. The fake directory comes first, so it shadows a real mmdc.
_SYSTEM = sorted({Path(p).parent for c in ("cat", "dirname", "sleep") if (p := shutil.which(c))})


def _fake_mmdc(root: Path, body: str, log: Path | None = None) -> Path:
    """Write a fake ``mmdc`` into ``root/bin`` and return that directory.

    Args:
        root: The directory that holds ``bin/``.
        body: The shell text that runs after the argument parsing.
        log: A file that receives the directory of the output file, one line per run.

    Returns:
        The directory with the fake.
    """
    bin_dir = root / "bin"
    bin_dir.mkdir(exist_ok=True)
    exe = bin_dir / "mmdc"
    record = f'dirname "$out" >> "{log}"\n' if log else ""
    exe.write_text(_PRELUDE + record + body, encoding="utf-8")
    exe.chmod(0o755)
    return bin_dir


def _run(
    tmp_path: Path, args: list[str], path_dirs: list[Path]
) -> subprocess.CompletedProcess[str]:
    """Run the script with ``path_dirs`` as the whole ``PATH`` and a private scratch directory."""
    scratch = tmp_path / "scratch"
    scratch.mkdir(exist_ok=True)
    env = {"PATH": os.pathsep.join(str(p) for p in path_dirs), "TMPDIR": str(scratch)}
    return subprocess.run(
        [sys.executable, str(_SCRIPT), *args],
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
        check=False,
    )


def _diagram(tmp_path: Path, text: str = "flowchart TD\n    A --> B\n") -> Path:
    path = tmp_path / "m.mmd"
    path.write_text(text, encoding="utf-8")
    return path


def _verdict(result: subprocess.CompletedProcess[str]) -> str:
    return result.stdout.splitlines()[0] if result.stdout else ""


def _load_script() -> ModuleType:
    spec = importlib.util.spec_from_file_location("validate_mermaid", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_a_good_diagram_is_valid(tmp_path: Path) -> None:
    fake = _fake_mmdc(tmp_path, _GOOD)
    result = _run(tmp_path, [str(_diagram(tmp_path))], [fake, *_SYSTEM])
    assert (result.returncode, result.stdout.strip()) == (0, "VALID")


def test_a_parse_error_is_invalid_with_the_error_and_no_stack(tmp_path: Path) -> None:
    fake = _fake_mmdc(tmp_path, _PARSE_ERROR)
    result = _run(tmp_path, [str(_diagram(tmp_path))], [fake, *_SYSTEM])
    assert result.returncode == 1
    assert _verdict(result) == "INVALID"
    assert "Parse error on line 2" in result.stdout
    assert "got 'end'" in result.stdout
    assert "Parser.parseError" not in result.stdout
    assert "at #evaluate" not in result.stdout


@pytest.mark.parametrize("body", [_NO_FILE, _EMPTY_FILE], ids=["no-file", "empty-file"])
def test_exit_zero_without_an_svg_is_invalid(tmp_path: Path, body: str) -> None:
    """The output file is the proof: exit 0 alone never makes a diagram valid."""
    fake = _fake_mmdc(tmp_path, body)
    result = _run(tmp_path, [str(_diagram(tmp_path))], [fake, *_SYSTEM])
    assert (result.returncode, _verdict(result)) == (1, "INVALID")


def test_mermaids_error_diagram_is_invalid_though_mmdc_exits_zero(tmp_path: Path) -> None:
    """FRE-1579 F7: a broken ``pie`` diagram made the real mmdc exit 0 and write an SVG."""
    fake = _fake_mmdc(tmp_path, _ERROR_DIAGRAM)
    result = _run(tmp_path, [str(_diagram(tmp_path, 'pie\n    "a" : abc\n'))], [fake, *_SYSTEM])
    assert (result.returncode, _verdict(result)) == (1, "INVALID")
    assert "error diagram" in result.stdout


def test_a_label_that_says_syntax_error_is_still_valid(tmp_path: Path) -> None:
    """The check reads the root tag, not the label text (the ``_GOOD`` SVG holds that text)."""
    fake = _fake_mmdc(tmp_path, _GOOD)
    result = _run(tmp_path, [str(_diagram(tmp_path))], [fake, *_SYSTEM])
    assert _verdict(result) == "VALID"


def _text_of(length: int) -> str:
    """Return a diagram text of exactly ``length`` characters."""
    head = "flowchart TD\n%% "
    return head + "x" * (length - len(head))


def test_a_diagram_over_the_mermaid_text_limit_is_invalid(tmp_path: Path) -> None:
    """FRE-1579: mmdc exits 0 on a text of over 50,000 characters, and it reads none of it.

    The fake writes a good SVG, as the real mmdc did for a broken diagram of this size, so only
    the size check can make the verdict INVALID.
    """
    fake = _fake_mmdc(tmp_path, _GOOD)
    limit = _load_script().MAX_DIAGRAM_CHARS
    over = _run(tmp_path, [str(_diagram(tmp_path, _text_of(limit + 1)))], [fake, *_SYSTEM])
    assert (over.returncode, _verdict(over)) == (1, "INVALID")
    assert str(limit) in over.stdout
    at_limit = _run(tmp_path, [str(_diagram(tmp_path, _text_of(limit)))], [fake, *_SYSTEM])
    assert _verdict(at_limit) == "VALID"


_BROWSER_FAILURES = {
    "no-browser": _NO_BROWSER,
    "no-config": _NO_CONFIG,
    "launch": _LAUNCH_FAILED,
    **{name: _fails_with(line) for name, line in _OTHER_BROWSER_FAILURES.items()},
}


@pytest.mark.parametrize("body", _BROWSER_FAILURES.values(), ids=_BROWSER_FAILURES.keys())
def test_a_browser_failure_is_an_error_not_a_bad_diagram(tmp_path: Path, body: str) -> None:
    """FRE-1579 F8: the model must not repair a good diagram because the browser did not start."""
    fake = _fake_mmdc(tmp_path, body)
    result = _run(tmp_path, [str(_diagram(tmp_path))], [fake, *_SYSTEM])
    assert (result.returncode, _verdict(result)) == (2, "ERROR")
    assert "not a diagram error" in result.stdout
    if body is _LAUNCH_FAILED:
        assert "Socket path too long" in result.stdout  # the reason is on a later line


@pytest.mark.parametrize(
    "body", ["exit 127\n", "exit 126\n", "kill -9 $$\n"], ids=["not-found", "not-exec", "killed"]
)
def test_a_wrapper_failure_or_a_killed_mmdc_is_an_error(tmp_path: Path, body: str) -> None:
    fake = _fake_mmdc(tmp_path, body)
    result = _run(tmp_path, [str(_diagram(tmp_path))], [fake, *_SYSTEM])
    assert (result.returncode, _verdict(result)) == (2, "ERROR")
    assert "did not run to the end" in result.stdout


def test_the_word_browser_inside_a_parse_error_does_not_make_it_an_error(tmp_path: Path) -> None:
    fake = _fake_mmdc(tmp_path, _PARSE_ERROR_WITH_BROWSER_WORD)
    result = _run(tmp_path, [str(_diagram(tmp_path, "Browser --> end\n"))], [fake, *_SYSTEM])
    assert (result.returncode, _verdict(result)) == (1, "INVALID")


def test_output_that_is_not_utf8_gives_a_verdict_not_a_traceback(tmp_path: Path) -> None:
    fake = _fake_mmdc(tmp_path, _BINARY_OUTPUT)
    result = _run(tmp_path, [str(_diagram(tmp_path))], [fake, *_SYSTEM])
    assert (result.returncode, _verdict(result)) == (1, "INVALID")
    assert "Traceback" not in result.stderr


@pytest.mark.parametrize("body", [_PARSE_ERROR, _NO_FILE], ids=["exit-1", "exit-0-no-file"])
def test_a_stale_svg_next_to_the_input_cannot_make_a_broken_diagram_valid(
    tmp_path: Path, body: str
) -> None:
    """FRE-1579 F2: the old recipe tested a fixed ``/tmp/m.svg`` and read a stale file as VALID."""
    (tmp_path / "m.svg").write_text("<svg></svg>", encoding="utf-8")
    fake = _fake_mmdc(tmp_path, body)
    result = _run(tmp_path, [str(_diagram(tmp_path))], [fake, *_SYSTEM])
    assert (result.returncode, _verdict(result)) == (1, "INVALID")


@pytest.mark.parametrize(
    ("body", "args", "verdict"),
    [
        (_GOOD, [], "VALID"),
        (_PARSE_ERROR, [], "INVALID"),
        (_SLOW, ["--timeout", "1"], "ERROR"),
    ],
    ids=["valid", "invalid", "timed-out"],
)
def test_each_run_uses_its_own_scratch_directory_and_removes_it(
    tmp_path: Path, body: str, args: list[str], verdict: str
) -> None:
    """The fake records where mmdc was told to write. That place is new, and it is gone after."""
    log = tmp_path / "output-dirs.log"
    fake = _fake_mmdc(tmp_path, body, log=log)
    first = _run(tmp_path, [*args, str(_diagram(tmp_path))], [fake, *_SYSTEM])
    second = _run(tmp_path, [*args, str(_diagram(tmp_path))], [fake, *_SYSTEM])
    assert _verdict(first) == _verdict(second) == verdict
    dirs = log.read_text(encoding="utf-8").split()
    assert len(dirs) == 2 and dirs[0] != dirs[1]
    scratch = str(tmp_path / "scratch")
    for directory in dirs:
        assert directory.startswith(scratch), directory
        assert not Path(directory).exists(), directory


def test_a_missing_mmdc_is_an_error(tmp_path: Path) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    result = _run(tmp_path, [str(_diagram(tmp_path))], [empty])
    assert (result.returncode, _verdict(result)) == (2, "ERROR")
    assert "mmdc is not installed" in result.stdout


def test_an_mmdc_that_cannot_run_is_an_error(tmp_path: Path) -> None:
    """A file named mmdc that is not executable raises PermissionError in subprocess."""
    fake = _fake_mmdc(tmp_path, _GOOD)
    (fake / "mmdc").chmod(0o644)
    result = _run(tmp_path, [str(_diagram(tmp_path))], [fake])
    assert (result.returncode, _verdict(result)) == (2, "ERROR")
    assert "Traceback" not in result.stderr


def test_a_missing_input_file_is_an_error(tmp_path: Path) -> None:
    fake = _fake_mmdc(tmp_path, _GOOD)
    result = _run(tmp_path, [str(tmp_path / "nope.mmd")], [fake, *_SYSTEM])
    assert (result.returncode, _verdict(result)) == (2, "ERROR")
    assert "nope.mmd" in result.stdout


def test_a_check_that_runs_too_long_is_an_error(tmp_path: Path) -> None:
    fake = _fake_mmdc(tmp_path, _SLOW)
    result = _run(tmp_path, ["--timeout", "1", str(_diagram(tmp_path))], [fake, *_SYSTEM])
    assert (result.returncode, _verdict(result)) == (2, "ERROR")
    assert "timed out after 1" in result.stdout


def test_the_default_timeout_ends_before_the_bash_tool_does() -> None:
    """The model must get the script's ERROR line, not a bash time-out with no verdict."""
    assert _load_script().DEFAULT_TIMEOUT_SECONDS < _BASH_DEFAULT_TIMEOUT


def test_help_explains_the_three_answers(tmp_path: Path) -> None:
    result = _run(tmp_path, ["--help"], _SYSTEM)
    assert result.returncode == 0
    for word in ("usage", "VALID", "INVALID", "ERROR"):
        assert word in result.stdout


def test_the_command_the_script_runs_is_allowed_by_the_permission_rules() -> None:
    """ADR-0156 D8.7 for ``scripts/``: the one command the script builds comes out allow."""
    build_command = _load_script().build_command
    command = shlex.join(build_command(Path("/tmp/m.mmd"), Path("/tmp/tmpabc123/diagram.svg")))
    decision = evaluate_permission("bash", {"command": command}, "NORMAL", load_permission_rules())
    assert decision.decision == "allow", f"{command!r}: {decision.reason}"


@pytest.mark.skipif(
    shutil.which("mmdc") is None,
    reason="mmdc is not installed here; the fake-mmdc tests cover the script logic",
)
@pytest.mark.parametrize(
    ("diagram", "verdict"),
    [
        ("flowchart LR\n    A[Open PR] --> B{CI passes?}\n    B -->|Yes| C[Merge]\n", "VALID"),
        ("flowchart TD\n    A --> end\n", "INVALID"),
        ("A --> B\n", "INVALID"),
        ('pie\n    "a" : abc\n', "INVALID"),
        # A broken diagram of more than 50,000 characters: the real mmdc exits 0 on it.
        ("flowchart TD\n" + "    A --> B\n" * 5000 + "    A --> end\n", "INVALID"),
    ],
    ids=["good", "reserved-word", "no-header", "pie-error-diagram", "oversized-broken"],
)
def test_the_real_mmdc_agrees(tmp_path: Path, diagram: str, verdict: str) -> None:
    """AC-2 on a machine that has mmdc: one good diagram valid, the broken ones invalid."""
    # TMPDIR stays as it is: Chrome fails with "Socket path too long" under a long pytest path.
    result = subprocess.run(
        [sys.executable, str(_SCRIPT), str(_diagram(tmp_path, diagram))],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    if _verdict(result) == "ERROR":
        pytest.skip(f"mmdc cannot run its browser on this machine: {result.stdout.strip()}")
    assert _verdict(result) == verdict, result.stdout + result.stderr
    assert result.returncode == (0 if verdict == "VALID" else 1)
