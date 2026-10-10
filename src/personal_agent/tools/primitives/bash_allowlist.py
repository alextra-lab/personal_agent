"""Whether a bash command may skip the approval prompt (FRE-283, FRE-1572).

``auto_approve_prefixes`` in ``config/governance/tools.yaml`` lists the commands that run
without the owner's approval. A command qualifies only when every command bash would run
starts with an allowlisted prefix, and no allowlisted binary is asked, through its own
options, to run another command or write a file outside scratch space.

FRE-1572 closed the gaps. The old splitter split only on ``;``, ``|``, ``||`` and ``&&``, so a
newline, a single ``&``, ``$(…)`` and backticks hid a second command behind an allowlisted
first word. Allowlisted binaries could also run a command or write a file through their own
options (``find -exec``, ``env CMD``, ``awk 'system(…)'``, ``curl -d @file``).

The design refuses rather than parses. A segment holding a construct the checker does not
look into is not auto-approvable: it goes to the normal approval flow (a card in the PWA, a
denial in a CLI turn), and it is not blocked. A recursive bash parser would be a second,
partial bash, and each gap in it would be a bypass. A refusal costs one card.

Owner decisions of 2026-10-10 (FRE-1572): ``python3`` stays auto-approved, recorded as
accepted; the option rules below catch the run-a-command and write-a-file options; ``curl``'s
file-read, upload and write options need approval, and ``psql -c`` and ``redis-cli`` leave the
NORMAL allowlist.
"""

from __future__ import annotations

import posixpath
import re
import shlex
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace

#: Write targets that stay auto-approvable. ``/tmp/**`` matches the ``write`` tool's
#: ``unattended_paths``: scratch space that needs no approval.
_SCRATCH_DEVICES = frozenset({"/dev/null", "/dev/stdout", "/dev/stderr", "-"})

# Characters that end an unquoted shell word.
_WORD_END = frozenset(" \t\n;&|<>()")


@dataclass(frozen=True)
class _Segment:
    """One command of a command line, and why it cannot be auto-approved, if it cannot.

    Attributes:
        text: The segment's text, without comments and here-doc bodies.
        unsafe: Why the segment needs approval whatever its first word, or ``None``.
    """

    text: str
    unsafe: str | None = None


def is_scratch_path(path: str) -> bool:
    """Report whether a write target is scratch space that needs no approval.

    Args:
        path: The write target, with shell quotes already removed.

    Returns:
        ``True`` for ``/dev/null``, ``/dev/stdout``, ``/dev/stderr``, ``-``, and a path that
        stays under ``/tmp/`` after normalization and holds no expansion.
    """
    if path in _SCRATCH_DEVICES:
        return True
    if any(c in path for c in "$`~*?[{"):
        return False
    return path.startswith("/tmp/") and posixpath.normpath(path).startswith("/tmp/")


def _read_word(command: str, start: int) -> tuple[str, int]:
    """Read one shell word, quotes included, from ``start``.

    Args:
        command: The full command line.
        start: Index of the word's first character.

    Returns:
        The raw word and the index just after it.
    """
    i, n = start, len(command)
    quote: str | None = None
    while i < n:
        c = command[i]
        if quote is not None:
            if c == "\\" and quote == '"' and i + 1 < n:
                i += 2
                continue
            if c == quote:
                quote = None
            i += 1
        elif c == "\\" and i + 1 < n:
            i += 2
        elif c in "'\"":
            quote = c
            i += 1
        elif c in _WORD_END:
            break
        else:
            i += 1
    return command[start:i], i


def _unquote(word: str) -> str | None:
    """Remove shell quoting from one word.

    Args:
        word: A raw shell word.

    Returns:
        The word as bash would see it, or ``None`` when it does not parse.
    """
    try:
        parts = shlex.split(word)
    except ValueError:
        return None
    return parts[0] if len(parts) == 1 else None


def scan_segments(command: str) -> list[_Segment]:
    """Split a command line into the commands bash would run, and mark the unsafe ones.

    Outside quotes, a segment ends at ``;``, ``|``, ``||``, ``|&``, ``&&``, a single ``&`` and a
    newline. An ``&`` inside a redirection (``2>&1``, ``&>``) does not end one. A comment is
    dropped. A here-doc body is read as data, up to its delimiter line.

    A segment is unsafe when it holds a command substitution (``$(``, ``$((``, a backtick,
    also inside double quotes, where bash expands them), a process substitution, ANSI-C
    quoting (``$'…'``, which the quote tracking cannot follow), a ``${…@…}`` transformation,
    a read-write redirection, an output redirection to a non-scratch file, an unquoted
    here-doc whose body holds a substitution, or an unterminated quote or here-doc.

    Args:
        command: The raw command line.

    Returns:
        The segments in order. Empty segments are dropped.
    """
    segments: list[_Segment] = []
    current: list[str] = []
    unsafe: str | None = None
    heredocs: list[tuple[str, bool, bool]] = []  # (delimiter, quoted, strip_tabs)
    in_single = False
    in_double = False
    i, n = 0, len(command)

    def flag(reason: str) -> None:
        nonlocal unsafe
        if unsafe is None:
            unsafe = reason

    def close() -> None:
        nonlocal current, unsafe
        text = "".join(current).strip()
        if text:
            segments.append(_Segment(text, unsafe))
        elif unsafe is not None:
            if segments:
                last = segments[-1]
                segments[-1] = replace(last, unsafe=last.unsafe or unsafe)
            else:
                segments.append(_Segment(command.strip(), unsafe))
        current = []
        unsafe = None

    def check_parameter_expansion(at: int) -> None:
        # `at` indexes the `$` of `${`. A transformation (`${x@P}` and friends) can expand
        # a prompt string, and prompt expansion can run a command substitution.
        end = command.find("}", at + 2)
        body = command[at + 2 : end if end != -1 else n]
        if "@" in body:
            flag("parameter_transformation")

    def read_heredoc_bodies(at: int) -> int:
        # `at` is the index just after a newline. Returns the index after the last body.
        pos = at
        for delimiter, quoted, strip_tabs in heredocs:
            found = False
            while pos <= n:
                end = command.find("\n", pos)
                line = command[pos : end if end != -1 else n]
                pos = (end + 1) if end != -1 else n + 1
                if (line.lstrip("\t") if strip_tabs else line) == delimiter:
                    found = True
                    break
                if not quoted and ("$(" in line or "`" in line):
                    flag("heredoc_substitution")
                if end == -1:
                    break
            if not found:
                flag("unterminated_heredoc")
        heredocs.clear()
        return min(pos, n)

    while i < n:
        c = command[i]
        nxt = command[i + 1] if i + 1 < n else ""

        if in_single:
            current.append(c)
            if c == "'":
                in_single = False
            i += 1
            continue

        if in_double:
            if c == "\\" and nxt:
                current.append(c + nxt)
                i += 2
                continue
            if c == "`" or (c == "$" and nxt == "("):
                flag("command_substitution")
            elif c == "$" and nxt == "{":
                check_parameter_expansion(i)
            elif c == '"':
                in_double = False
            current.append(c)
            i += 1
            continue

        if c == "\\" and nxt:
            if nxt != "\n":  # a line continuation joins the two lines
                current.append(c + nxt)
            i += 2
        elif c == "'":
            if current and current[-1] == "$":
                flag("ansi_c_quoting")
            in_single = True
            current.append(c)
            i += 1
        elif c == '"':
            in_double = True
            current.append(c)
            i += 1
        elif c == "`":
            flag("command_substitution")
            current.append(c)
            i += 1
        elif c == "$":
            if nxt == "(":
                flag("command_substitution")
            elif nxt == "{":
                check_parameter_expansion(i)
            current.append(c)
            i += 1
        elif c not in "\t\n" and (ord(c) < 32 or ord(c) == 127):
            # shlex reads a control character as a separator; bash does not.
            flag("control_character")
            current.append(c)
            i += 1
        elif c == "#" and (not current or current[-1] in " \t"):
            end = command.find("\n", i)
            i = end if end != -1 else n
        elif c == "\n":
            i = read_heredoc_bodies(i + 1) if heredocs else i + 1
            close()
        elif c == ";":
            close()
            i += 1
        elif c == "|":
            close()
            i += 2 if nxt in ("|", "&") else 1
        elif c == "&":
            if nxt == "&":
                close()
                i += 2
            elif nxt == ">":
                op_len = 3 if command.startswith("&>>", i) else 2
                i = _consume_output_redirection(command, i, op_len, current, flag)
            else:
                close()
                i += 1
        elif c == "<":
            if nxt == "(":
                flag("process_substitution")
                current.append(c)
                i += 1
            elif command.startswith("<<<", i):
                current.append("<<<")
                i += 3
            elif nxt == "<":
                strip_tabs = command.startswith("<<-", i)
                j = i + (3 if strip_tabs else 2)
                while j < n and command[j] in " \t":
                    j += 1
                raw, end = _read_word(command, j)
                delimiter = _unquote(raw)
                if not raw or delimiter is None:
                    flag("unparseable_heredoc")
                    delimiter = raw
                quoted = any(q in raw for q in "'\"\\")
                heredocs.append((delimiter, quoted, strip_tabs))
                current.append(command[i:end])
                i = end
            elif nxt == ">":
                flag("read_write_redirection")
                current.append("<>")
                i += 2
            else:
                current.append(c)
                i += 1
        elif c == ">":
            if nxt == "(":
                flag("process_substitution")
                current.append(c)
                i += 1
            elif nxt == "&" and i + 2 < n and (command[i + 2].isdigit() or command[i + 2] == "-"):
                current.append(command[i : i + 3])  # fd duplication: 2>&1, >&2, >&-
                i += 3
            else:
                op_len = 2 if nxt in (">", "|", "&") else 1
                i = _consume_output_redirection(command, i, op_len, current, flag)
        else:
            current.append(c)
            i += 1

    if in_single or in_double:
        flag("unterminated_quote")
    if heredocs:
        flag("unterminated_heredoc")
    close()
    return segments


def _consume_output_redirection(
    command: str,
    at: int,
    op_len: int,
    current: list[str],
    flag: Callable[[str], None],
) -> int:
    """Read an output redirection and its target, and flag a non-scratch target.

    Args:
        command: The full command line.
        at: Index of the operator.
        op_len: Length of the operator (``>``, ``>>``, ``>|``, ``>&``, ``&>``, ``&>>``).
        current: The current segment's characters, extended in place.
        flag: Records why the segment is unsafe.

    Returns:
        The index just after the target.
    """
    j = at + op_len
    n = len(command)
    while j < n and command[j] in " \t":
        j += 1
    raw, end = _read_word(command, j)
    target = _unquote(raw) if raw else None
    if target is None or not is_scratch_path(target):
        flag("output_redirection")
    current.append(command[at:end])
    return end


# --------------------------------------------------------------------------------------
# Option rules for allowlisted binaries
# --------------------------------------------------------------------------------------

_REDIRECTION_TOKEN = re.compile(r"^\d*(?:&>>|&>|>>|>\||>&|>|<<<|<<-|<<|<>|<&|<)")


def _command_words(words: Sequence[str]) -> list[str]:
    """Drop redirection tokens, and their detached targets, from a segment's words.

    Args:
        words: The segment's shell words.

    Returns:
        The command name and its arguments only.
    """
    out: list[str] = []
    skip_next = False
    for word in words:
        if skip_next:
            skip_next = False
            continue
        match = _REDIRECTION_TOKEN.match(word)
        if match:
            skip_next = match.end() == len(word)
            continue
        out.append(word)
    return out


_ENV_SAFE_OPTIONS = frozenset({"-i", "-0", "-", "--null", "--ignore-environment"})
_ASSIGNMENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=.*", re.DOTALL)
_FIND_ACTIONS = frozenset(
    {"-exec", "-execdir", "-ok", "-okdir", "-delete", "-fprint", "-fprint0", "-fprintf", "-fls"}
)
_AWK_LOADING_OPTIONS = frozenset(
    {"-f", "--file", "-i", "--include", "-l", "--load", "-E", "--exec"}
)
_AWK_PROGRAM_HAZARDS = ("system", "getline", "|", "@load", "@include")
_AWK_PRINT_REDIRECT = re.compile(r"\bprintf?\b[^;}\n]*>")
_SED_S_FLAGS = re.compile(
    r"s(?P<d>[^\\\n])(?:(?!(?P=d))[^\\]|\\.)*(?P=d)(?:(?!(?P=d))[^\\]|\\.)*(?P=d)(?P<flags>[A-Za-z0-9]*)"
)
_SED_ADDRESS = r"(?:[0-9]+|\$|/(?:[^/\\]|\\.)*/[IM]*)"
_SED_EXEC_COMMAND = re.compile(
    rf"(?:^|[;{{}}\n])\s*(?:{_SED_ADDRESS}(?:\s*,\s*(?:{_SED_ADDRESS}|[~+][0-9]+))?)?\s*!?\s*[ewW](?:\s|$|;)"
)
_CURL_SHORT_WITH_ARG = frozenset("AbcCdDeEFHKmoPQrtTuUwxXyYz")
_CURL_LONG_WITH_ARG = frozenset(
    {
        "--output",
        "--dump-header",
        "--cookie-jar",
        "--trace",
        "--trace-ascii",
        "--libcurl",
        "--stderr",
        "--upload-file",
        "--config",
        "--output-dir",
        "--data",
        "--data-ascii",
        "--data-binary",
        "--data-urlencode",
        "--json",
        "--form",
        "--header",
        "--proxy-header",
        "--variable",
        "--url",
        "--cookie",
    }
)
_CURL_ALWAYS_HAZARD = frozenset(
    {"--upload-file", "--config", "--output-dir", "--remote-name", "--remote-name-all"}
)
_CURL_WRITE_TARGET = frozenset(
    {
        "--output",
        "--dump-header",
        "--cookie-jar",
        "--trace",
        "--trace-ascii",
        "--libcurl",
        "--stderr",
    }
)
_CURL_AT_FILE = frozenset(
    {"--data", "--data-ascii", "--data-binary", "--json", "--header", "--proxy-header"}
)
_CURL_SHORT_LONG = {
    "o": "--output",
    "D": "--dump-header",
    "c": "--cookie-jar",
    "T": "--upload-file",
    "K": "--config",
    "d": "--data",
    "F": "--form",
    "H": "--header",
    "b": "--cookie",
}
_FILE_URL = re.compile(r"(?i)^\s*file:")


def _curl_value_hazard(option: str, value: str) -> str | None:
    """Judge one curl option and its value.

    Args:
        option: The long option name (a short option is mapped to it first).
        value: The option's value.

    Returns:
        A reason when the option reads a local file into the request, uploads, or writes a
        non-scratch file, else ``None``.
    """
    if option in _CURL_ALWAYS_HAZARD:
        return f"curl {option}"
    if option in _CURL_WRITE_TARGET and not is_scratch_path(value):
        return f"curl {option} to a file"
    if option in _CURL_AT_FILE and value.startswith("@"):
        return f"curl {option} reads a file"
    if option == "--form" and ("=@" in value or "=<" in value):
        return "curl --form reads a file"
    if option in ("--data-urlencode", "--variable") and re.match(r"^[^=@]*@", value):
        return f"curl {option} reads a file"
    if option == "--cookie" and "=" not in value:
        return "curl --cookie reads a file"
    if option == "--url" and _FILE_URL.match(value):
        return "curl reads a file: URL"
    return None


def _curl_hazard(args: Sequence[str]) -> str | None:
    """Return why a curl call needs approval, or ``None``.

    Args:
        args: curl's arguments, without the command name.

    Returns:
        A reason, or ``None`` for a plain request.
    """
    i = 0
    while i < len(args):
        word = args[i]
        if _FILE_URL.match(word):
            return "curl reads a file: URL"
        if word.startswith("--"):
            name, eq, value = word.partition("=")
            if name in ("--remote-name", "--remote-name-all", "--remote-header-name"):
                return f"curl {name}"
            if name in _CURL_LONG_WITH_ARG:
                if not eq:
                    i += 1
                    value = args[i] if i < len(args) else ""
                reason = _curl_value_hazard(name, value)
                if reason:
                    return reason
        elif word.startswith("-") and len(word) > 1:
            for j, letter in enumerate(word[1:], start=1):
                if letter in ("O", "J"):
                    return f"curl -{letter}"
                if letter in _CURL_SHORT_WITH_ARG:
                    value = word[j + 1 :]
                    if not value:
                        i += 1
                        value = args[i] if i < len(args) else ""
                    option = _CURL_SHORT_LONG.get(letter)
                    if option is not None:
                        reason = _curl_value_hazard(option, value)
                        if reason:
                            return reason
                    break
        i += 1
    return None


def _sed_hazard(args: Sequence[str]) -> str | None:
    """Return why a sed call needs approval, or ``None``.

    Args:
        args: sed's arguments, without the command name.

    Returns:
        A reason, or ``None`` for a script that only prints or substitutes.
    """
    scripts: list[str] = []
    positional: list[str] = []
    i = 0
    while i < len(args):
        word = args[i]
        if word in ("-e", "--expression"):
            i += 1
            if i < len(args):
                scripts.append(args[i])
        elif word.startswith("--expression="):
            scripts.append(word.partition("=")[2])
        elif word.startswith("--in-place") or word.startswith("--file"):
            return f"sed {word.partition('=')[0]}"
        elif word.startswith("-") and not word.startswith("--") and len(word) > 1:
            letters = word[1:]
            if "e" in letters:
                head, _, tail = letters.partition("e")
                if "i" in head or "f" in head:
                    return f"sed -{letters}"
                if tail:
                    scripts.append(tail)
                else:
                    i += 1
                    if i < len(args):
                        scripts.append(args[i])
            elif "i" in letters or "f" in letters:
                return f"sed -{letters}"
        elif not word.startswith("-"):
            positional.append(word)
        i += 1
    if not scripts and positional:
        scripts.append(positional[0])
    for script in scripts:
        for match in _SED_S_FLAGS.finditer(script):
            flags = match.group("flags")
            if "e" in flags or "w" in flags:
                return "sed s command with an e or w flag"
        if _SED_EXEC_COMMAND.search(script):
            return "sed e or w command"
    return None


def _awk_hazard(args: Sequence[str]) -> str | None:
    """Return why an awk call needs approval, or ``None``.

    Args:
        args: awk's arguments, without the command name.

    Returns:
        A reason, or ``None`` for a program that only reads and prints.
    """
    i = 0
    texts: list[str] = []
    while i < len(args):
        word = args[i]
        name = word.partition("=")[0]
        if word in _AWK_LOADING_OPTIONS or name in _AWK_LOADING_OPTIONS:
            return f"awk {name}"
        if word in ("-F", "-v"):
            i += 2  # the field separator and an assignment cannot run anything
            continue
        if word.startswith("-F") or word.startswith("-v"):
            i += 1
            continue
        texts.append(word)
        i += 1
    for text in texts:
        if any(h in text for h in _AWK_PROGRAM_HAZARDS):
            return "awk program runs a command or reads a pipe"
        if _AWK_PRINT_REDIRECT.search(text):
            return "awk program writes a file"
    return None


def _long_option_abbreviates(word: str, full: str) -> bool:
    """Report whether ``word`` names ``full``, possibly abbreviated (getopt_long rules).

    Args:
        word: A ``--`` option word, with or without ``=value``.
        full: The full long option name.

    Returns:
        ``True`` when the name part is at least three characters and a prefix of ``full``.
    """
    name = word.partition("=")[0]
    return len(name) >= 3 and full.startswith(name)


def option_hazard(words: Sequence[str]) -> str | None:
    """Return why an allowlisted command needs approval because of its options, or ``None``.

    Args:
        words: The segment's shell words, command name first.

    Returns:
        A reason when the command runs another command or writes a non-scratch file.
    """
    command_words = _command_words(words)
    if not command_words:
        return None
    name, args = command_words[0], command_words[1:]
    match name:
        case "env":
            for word in args:
                if word not in _ENV_SAFE_OPTIONS and not _ASSIGNMENT.fullmatch(word):
                    return "env runs a command or takes an unread option"
        case "find":
            for word in args:
                if word in _FIND_ACTIONS:
                    return f"find {word}"
        case "awk":
            return _awk_hazard(args)
        case "sed":
            return _sed_hazard(args)
        case "sort":
            for word in args:
                if word.startswith("--"):
                    if _long_option_abbreviates(word, "--compress-program") or (
                        _long_option_abbreviates(word, "--output")
                    ):
                        return f"sort {word.partition('=')[0]}"
                elif word.startswith("-") and "o" in word[1:]:
                    return "sort -o"
        case "uniq":
            positional: list[str] = []
            skip = False
            for word in args:
                if skip:
                    skip = False
                elif word in ("-f", "-s", "-w"):
                    skip = True
                elif not word.startswith("-") or word == "-":
                    positional.append(word)
            if len(positional) >= 2:
                return "uniq writes an output file"
        case "rg":
            for word in args:
                if word == "--pre" or word.startswith("--pre="):
                    return "rg --pre"
        case "git":
            for word in args:
                if word.startswith("--output") or word == "--ext-diff":
                    return f"git {word.partition('=')[0]}"
        case "mmdc":
            i = 0
            while i < len(args):
                word = args[i]
                if word in ("-p", "--puppeteerConfigFile") or word.startswith(
                    "--puppeteerConfigFile="
                ):
                    return "mmdc puppeteer config"
                if word in ("-o", "--output"):
                    i += 1
                    if i >= len(args) or not is_scratch_path(args[i]):
                        return "mmdc writes a non-scratch file"
                elif word.startswith("--output=") or (word.startswith("-o") and len(word) > 2):
                    value = word.partition("=")[2] if word.startswith("--") else word[2:]
                    if not is_scratch_path(value):
                        return "mmdc writes a non-scratch file"
                i += 1
        case "curl":
            return _curl_hazard(args)
        case _:
            return None
    return None


def check_segment_allowlist(command: str, allowlist: Sequence[str]) -> str | None:
    """Return the first segment that needs approval, or ``None`` to auto-approve.

    Multi-word allowlist entries (e.g. ``"docker ps"``) match when the segment begins with
    those exact words in order.

    Args:
        command: Raw shell command string.
        allowlist: Allowed prefix strings for the current mode.

    Returns:
        The first segment that is unsafe, unparseable, not allowlisted, or whose options run a
        command or write a non-scratch file. ``None`` when every segment passes, so the command
        may be auto-approved.
    """
    for segment in scan_segments(command):
        if segment.unsafe is not None:
            return segment.text
        try:
            words = shlex.split(segment.text)
        except ValueError:
            return segment.text
        if not words:
            continue
        matched = any(
            words[: len(prefix_words)] == prefix_words
            for entry in allowlist
            if (prefix_words := entry.split())
        )
        if not matched or option_hazard(words) is not None:
            return segment.text
    return None


def split_command_segments(command: str) -> list[str]:
    """Split a command line into the commands bash would run.

    Args:
        command: Raw shell command string.

    Returns:
        Non-empty, stripped segment strings, without comments and here-doc bodies.
    """
    return [segment.text for segment in scan_segments(command)]
