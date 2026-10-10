"""Parse a bash command into the subcommands that the permission rules judge (ADR-0156 D8.2).

The command is parsed with ``tree-sitter-bash``, not split on text. Each subcommand that bash
would run is extracted with its words after quote removal. A subcommand can match an allow rule
only under D8.2's closed condition:

- the parser classifies it as a simple command;
- every word is a literal after quote removal: no parameter, arithmetic or command expansion, no
  glob, no brace or tilde expansion;
- no variable assignment stands in front of it;
- no argument is ``-exec``, ``-execdir``, ``-ok`` or ``-okdir``;
- its only redirections send output or errors to ``/dev/null``, or are exactly ``2>&1``;
- FRE-1572's option rules find no run-a-command, read-a-file or write-a-file option.

A subcommand whose first word is an indirect-execution word can match only an exact allow rule.

FRE-1572's scanner runs as a second, independent check. If it marks a segment unsafe, or counts a
different number of top-level commands, the whole command asks. Two parsers must agree before
anything runs without the user, so a gap in one parser alone does not allow a command.

Deny and ask rules also apply inside subshells, substitutions and control-flow bodies (S6), so
every command node in the tree is returned, nested ones included.
"""

from __future__ import annotations

import posixpath
import shlex
from dataclasses import dataclass
from functools import lru_cache

import tree_sitter_bash
from tree_sitter import Language, Node, Parser

from personal_agent.tools.primitives.bash_allowlist import (
    _command_words,
    option_hazard,
    scan_segments,
)

#: First words that run another program given as their arguments (D8.2). Only an exact allow
#: rule that names the full inner command can allow one. ``coproc`` and ``trap`` are not in
#: D8.2's list: tree-sitter-bash parses ``coproc X`` as a plain command while bash runs ``X``,
#: and ``trap`` runs its first argument as a command later.
INDIRECT_EXECUTION_WORDS: frozenset[str] = frozenset(
    {
        "coproc",
        "trap",
        "eval",
        "source",
        ".",
        "exec",
        "command",
        "builtin",
        "env",
        "nohup",
        "timeout",
        "nice",
        "stdbuf",
        "time",
        "sudo",
        "xargs",
        "bash",
        "sh",
        "zsh",
        "dash",
    }
)

#: Arguments that make ``find`` (and similar) run another program (D8.2).
_EXEC_ARGUMENTS = frozenset({"-exec", "-execdir", "-ok", "-okdir"})

#: Output redirection operators that may point at ``/dev/null``.
_NULL_REDIRECT_OPERATORS = frozenset({">", ">>", "&>", "&>>"})

#: Node types that are an expansion or a substitution: never a literal word.
_EXPANSION_NODES = frozenset(
    {
        "simple_expansion",
        "expansion",
        "command_substitution",
        "process_substitution",
        "arithmetic_expansion",
        "ansi_c_string",
        "translated_string",
        "brace_expression",
    }
)

#: Characters that bash treats specially in an unquoted word.
_UNQUOTED_SPECIAL = {
    "*": "glob",
    "?": "glob",
    "[": "glob",
    "{": "brace_expansion",
    "}": "brace_expansion",
    "~": "tilde_expansion",
    "$": "expansion",
    "`": "expansion",
    "'": "quote",
    '"': "quote",
}


@dataclass(frozen=True)
class Subcommand:
    """One command that bash would run, as the permission rules see it.

    Attributes:
        text: The command's source text.
        words: The words after quote removal, or ``None`` when any word is not a literal.
        literal_prefix: The leading words that are literals. Deny and ask rules match on these
            when ``words`` is ``None``.
        refusal: Why the command fails the closed condition, or ``None``. A command with a
            refusal never matches an allow rule.
        indirect: Whether the first word is an indirect-execution word.
    """

    text: str
    words: tuple[str, ...] | None
    literal_prefix: tuple[str, ...]
    refusal: str | None
    indirect: bool


@dataclass(frozen=True)
class ParsedBash:
    """A parsed bash command.

    Attributes:
        top_level: The commands joined by ``&&``, ``||``, ``;``, ``|``, ``|&``, ``&`` and newlines.
            Allow rules judge these.
        all_commands: Every command node, nested ones included. Deny and ask rules judge these.
        refusal: Why the whole command asks whatever the rules say, or ``None``.
        refused_text: The part of the command that ``refusal`` names.
    """

    top_level: tuple[Subcommand, ...]
    all_commands: tuple[Subcommand, ...]
    refusal: str | None
    refused_text: str | None


@lru_cache(maxsize=1)
def _parser() -> Parser:
    """Return the shared tree-sitter parser for bash.

    Returns:
        A parser for the bash grammar.
    """
    return Parser(Language(tree_sitter_bash.language()))


def _text(node: Node) -> str:
    """Return a node's source text.

    Args:
        node: A tree-sitter node.

    Returns:
        The node's text, decoded as UTF-8.
    """
    return (node.text or b"").decode("utf-8", errors="replace")


def _has_control_character(text: str) -> bool:
    """Report whether text holds a control character other than tab and newline.

    A newline inside quotes is literal data (a multi-line ``jq`` filter or JSON body). In an
    unquoted word it cannot occur: it ends the command.

    Args:
        text: The text to check.

    Returns:
        ``True`` when a character below 32 (tab and newline excepted) or DEL is present.
    """
    return any((ord(c) < 32 and c not in "\t\n") or ord(c) == 127 for c in text)


def _unquoted_word(text: str) -> tuple[str | None, str | None]:
    """Remove the escapes from an unquoted word.

    Args:
        text: The word's source text.

    Returns:
        The literal value and ``None``, or ``None`` and the reason it is not a literal.
    """
    out: list[str] = []
    i = 0
    while i < len(text):
        c = text[i]
        if c == "\\":
            if i + 1 >= len(text):
                return None, "trailing_backslash"
            nxt = text[i + 1]
            if nxt != "\n":  # a line continuation is removed
                out.append(nxt)
            i += 2
            continue
        reason = _UNQUOTED_SPECIAL.get(c)
        if reason is not None:
            return None, reason
        out.append(c)
        i += 1
    value = "".join(out)
    if _has_control_character(value):
        return None, "control_character"
    return value, None


def _double_quoted(inner: str) -> tuple[str | None, str | None]:
    """Remove the escapes from the inside of a double-quoted string.

    Args:
        inner: The text between the double quotes.

    Returns:
        The literal value and ``None``, or ``None`` and the reason it is not a literal.
    """
    if "$" in inner or "`" in inner:
        return None, "expansion"
    out: list[str] = []
    i = 0
    while i < len(inner):
        c = inner[i]
        if c == "\\" and i + 1 < len(inner) and inner[i + 1] in '"\\\n':
            if inner[i + 1] != "\n":
                out.append(inner[i + 1])
            i += 2
            continue
        out.append(c)
        i += 1
    value = "".join(out)
    if _has_control_character(value):
        return None, "control_character"
    return value, None


def _literal(node: Node) -> tuple[str | None, str | None]:
    """Return the literal value of one word node.

    Args:
        node: A word-like tree-sitter node.

    Returns:
        The value and ``None``, or ``None`` and the reason the node is not a literal.
    """
    kind = node.type
    text = _text(node)
    if kind == "word":
        return _unquoted_word(text)
    if kind == "raw_string":
        if len(text) < 2 or not (text.startswith("'") and text.endswith("'")):
            return None, "quote"
        value = text[1:-1]
        return (None, "control_character") if _has_control_character(value) else (value, None)
    if kind == "string":
        if any(child.type != "string_content" for child in node.named_children):
            return None, "expansion"
        if len(text) < 2 or not (text.startswith('"') and text.endswith('"')):
            return None, "quote"
        return _double_quoted(text[1:-1])
    if kind == "number":
        # tree-sitter reads `-3` and `5` as numbers; any other number form is refused.
        digits = text[1:] if text[:1] in "+-" else text
        return (text, None) if digits.isdigit() else (None, "not_a_literal:number")
    if kind == "concatenation":
        parts: list[str] = []
        for child in node.children:
            part, reason = _literal(child)
            if part is None:
                return None, reason
            parts.append(part)
        return "".join(parts), None
    if kind in _EXPANSION_NODES:
        return None, "expansion"
    return None, f"not_a_literal:{kind}"


def _redirect_refusal(node: Node) -> str | None:
    """Return why a redirection fails the closed condition, or ``None``.

    Only output or errors to ``/dev/null`` and exactly ``2>&1`` pass. A redirection node that
    holds more than its target is refused: tree-sitter puts the words that follow the target
    (``ls >/dev/null -la``) inside the redirection, while bash passes them to the command.

    Args:
        node: A redirection node.

    Returns:
        A reason, or ``None``.
    """
    if node.type != "file_redirect":
        return node.type  # heredoc_redirect, herestring_redirect
    fd: str | None = None
    operator: str | None = None
    targets: list[Node] = []
    for child in node.children:
        if child.type == "file_descriptor":
            fd = _text(child)
        elif not child.is_named:
            operator = _text(child)
        else:
            targets.append(child)
    if len(targets) != 1:
        return "redirect_with_extra_words"
    target, _ = _literal(targets[0])
    if operator == ">&" and fd == "2" and target == "1":
        return None
    if operator in _NULL_REDIRECT_OPERATORS and fd in (None, "1", "2") and target == "/dev/null":
        return None
    if operator in ("<", "<&", "<>"):
        return "input_redirection"
    return "output_redirection"


def _subcommand(node: Node, redirects: list[Node]) -> Subcommand:
    """Build the subcommand of one ``command`` node.

    Args:
        node: A ``command`` node.
        redirects: Redirections that tree-sitter placed beside the command, in a
            ``redirected_statement``.

    Returns:
        The subcommand.
    """
    words: list[str] = []
    prefix: list[str] = []
    literal = True
    refusal: str | None = None

    def refuse(reason: str) -> None:
        nonlocal refusal
        if refusal is None:
            refusal = reason

    for child in [*node.children, *redirects]:
        kind = child.type
        if kind == "variable_assignment":
            refuse("variable_assignment")
            continue
        if kind in ("file_redirect", "heredoc_redirect", "herestring_redirect"):
            reason = _redirect_refusal(child)
            if reason is not None:
                refuse(reason)
            continue
        target = child
        if kind == "command_name":
            named = child.named_children
            if len(named) != 1:
                refuse("command_name")
                literal = False
                continue
            target = named[0]
        elif not child.is_named:
            refuse(f"token:{_text(child)}")
            literal = False
            continue
        value, reason = _literal(target)
        if value is None:
            refuse(reason or "not_a_literal")
            literal = False
            continue
        words.append(value)
        if literal:
            prefix.append(value)

    indirect = bool(words) and posixpath.basename(words[0]) in INDIRECT_EXECUTION_WORDS
    if literal:
        if not words:
            refuse("empty_command")
        elif any(word in _EXEC_ARGUMENTS for word in words):
            refuse("exec_argument")
        else:
            hazard = option_hazard(words)
            if hazard is not None:
                refuse(hazard)
    return Subcommand(
        text=_text(node) if not redirects else " ".join([_text(node), *map(_text, redirects)]),
        words=tuple(words) if literal else None,
        literal_prefix=tuple(prefix),
        refusal=refusal,
        indirect=indirect,
    )


def _all_commands(root: Node) -> list[Subcommand]:
    """Return every ``command`` node of the tree as a subcommand.

    Args:
        root: The tree's root node.

    Returns:
        The subcommands in source order, nested ones included.
    """
    found: list[Subcommand] = []
    stack = [root]
    while stack:
        node = stack.pop()
        if node.type == "command":
            found.append(_subcommand(node, []))
        stack.extend(reversed(node.children))
    return found


def _top_level(node: Node, out: list[tuple[Node, list[Node]]]) -> tuple[str | None, str | None]:
    """Collect the top-level simple commands of a list of statements.

    Args:
        node: A ``program``, ``list`` or ``pipeline`` node.
        out: Receives each command node with its separate redirections.

    Returns:
        A refusal and the text it names, or ``(None, None)``.
    """
    for child in node.children:
        kind = child.type
        if not child.is_named or kind == "comment":
            continue  # ; & && || | |& and newlines join commands
        if kind == "command":
            out.append((child, []))
        elif kind == "redirected_statement":
            body = child.child_by_field_name("body")
            if body is None or body.type != "command":
                return f"redirect_on_{body.type if body else 'nothing'}", _text(child)
            out.append((body, [c for c in child.named_children if c.id != body.id]))
        elif kind in ("list", "pipeline"):
            refusal = _top_level(child, out)
            if refusal[0] is not None:
                return refusal
        else:
            return kind, _text(child)
    return None, None


def parse_bash(command: str) -> ParsedBash:
    """Parse a bash command into the subcommands that the permission rules judge.

    Args:
        command: The raw command string, as the ``bash`` tool receives it.

    Returns:
        The parsed command. ``refusal`` is set when the command cannot be parsed fully, holds a
        construct that is not a simple command, or the two parsers disagree.
    """
    tree = _parser().parse(command.encode("utf-8"))
    root = tree.root_node
    all_commands = tuple(_all_commands(root))
    if root.has_error:
        return ParsedBash((), all_commands, "unparseable", command)
    nodes: list[tuple[Node, list[Node]]] = []
    refusal, refused_text = _top_level(root, nodes)
    top_level = tuple(_subcommand(node, redirects) for node, redirects in nodes)
    if refusal is not None:
        return ParsedBash(top_level, all_commands, refusal, refused_text)
    if not top_level:
        return ParsedBash((), all_commands, "empty_command", command)
    segments = scan_segments(command)
    unsafe = next((s for s in segments if s.unsafe is not None), None)
    if unsafe is not None:
        return ParsedBash(top_level, all_commands, f"scanner:{unsafe.unsafe}", unsafe.text)
    if len(segments) != len(top_level):
        return ParsedBash(top_level, all_commands, "parser_disagreement", command)
    for segment, sub in zip(segments, top_level, strict=True):
        if sub.words is None:
            continue  # not literal: it asks already
        # The scanner joins line continuations as bash does, and shlex removes quotes as bash
        # does. Different words mean the parsers read the command differently, for example
        # `find . -dele\<newline>te`, which bash runs as `-delete` and tree-sitter reads as two
        # words. A quoted word that looks like a redirection also differs here, and asks.
        try:
            scanner_words = tuple(_command_words(shlex.split(segment.text)))
        except ValueError:
            return ParsedBash(top_level, all_commands, "parser_disagreement", segment.text)
        if scanner_words != sub.words:
            return ParsedBash(top_level, all_commands, "parser_disagreement", segment.text)
    return ParsedBash(top_level, all_commands, None, None)
