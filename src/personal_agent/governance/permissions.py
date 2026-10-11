"""One permission model for every tool call: deny, ask and allow rules (ADR-0156 D8).

Rules live in ``config/governance/permissions.yaml``. A second file, named by
``settings.extra_permission_rules_path``, adds its lists (the eval stack's rules, D8.5). The
order is S6's: "deny, then ask, then allow. The first match in that order determines the
outcome." A call that matches no rule asks. Nothing runs by default.

Rule grammar (D8.1):

- ``tool`` matches every call of ``tool``.
- ``bash(<words>)`` matches a subcommand whose argument list equals ``<words>``, and
  ``bash(<words> *)`` one whose argument list starts with ``<words>``. The words are split
  like a shell line (``shlex``).
- ``tool(k=v, …)`` matches a call where every named parameter matches. ``v`` is ``true``,
  ``false``, ``null`` or a number (typed equality), a bare string or a ``"JSON string"``
  (exact), or either string form followed by ``*`` (prefix). Unnamed parameters are free.
- ``tool(=<canonical JSON>)`` is a session rule (D8.3): the full argument set is equal. Only
  :mod:`personal_agent.governance.session_rules` makes these. The file may not hold one.

The rules fail closed: :func:`decide_permission` turns a load error or an evaluation error into a
denial.
"""

from __future__ import annotations

import json
import re
import shlex
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import yaml  # type: ignore[import-untyped]

from personal_agent.config import settings
from personal_agent.governance.bash_matcher import (
    INDIRECT_EXECUTION_WORDS,
    Subcommand,
    parse_bash,
)
from personal_agent.governance.curl_request import (
    CURL_READ_METHODS,
    CurlReadRule,
    CurlRefusal,
    parse_curl_request,
)
from personal_agent.governance.models import Mode

#: JSON scalar values a parameter pattern can hold.
JsonScalar = str | int | float | bool | None

#: Words that can run another program from their arguments. An allow rule ``bash(<word> *)`` for
#: one of these is rejected when the rules load (D8.2). An exact rule may name the full command.
#: The interpreters run program text. The builtins evaluate an array subscript in an argument,
#: and a subscript can hold a command substitution (``printf -v 'a[$(rm x)]' y``).
PROGRAM_RUNNING_WORDS: frozenset[str] = INDIRECT_EXECUTION_WORDS | frozenset(
    {
        "awk",
        "gawk",
        "mawk",
        "nawk",
        "sed",
        "python",
        "python3",
        "perl",
        "ruby",
        "node",
        "let",
        "declare",
        "typeset",
        "local",
        "export",
        "readonly",
        "printf",
        "read",
        "test",
        "[",
        "alias",
        "fc",
        "mapfile",
        "readarray",
    }
)

RULES_FILE_NAME = "permissions.yaml"

_RULE = re.compile(r"(?P<tool>[A-Za-z0-9_.\-]+)(?:\((?P<body>.*)\))?", re.DOTALL)
_PARAM_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_NUMBER = re.compile(r"-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?")
_LIST_KEYS = ("deny", "ask", "allow")
_ITEM_KEYS = frozenset({"rule", "modes", "reason"})
_CURL_READ_KEY = "curl_read"
_CURL_READ_KEYS = frozenset({"hosts", "methods", "paths", "body", "modes", "reason"})
_CURL_HOST = re.compile(r"[a-z0-9-]+(?:\.[a-z0-9-]+)*:[0-9]+")
_CURL_PATH = re.compile(r"/|(?:/(?:[A-Za-z0-9._,-]+|\*))+")
_VALID_MODES = frozenset(mode.value for mode in Mode)
#: The only path form that a ``bash(python3 <path> *)`` allow rule may name (FRE-1585): relative,
#: one skill, its ``scripts/`` directory, one ``.py`` file. No dot segment, no glob, no prefix.
_SKILL_SCRIPT = re.compile(r"docs/skills/[a-z0-9][a-z0-9-]*/scripts/[A-Za-z0-9_][A-Za-z0-9_-]*\.py")
#: The directory where the bash tool starts in the gateway image: ``WORKDIR /app`` in
#: ``Dockerfile.gateway``, and ``bash.py`` passes no ``cwd=``. There, ``/app/<path>`` names the
#: file that ``<path>`` names (FRE-1589).
_IMAGE_WORKDIR = "/app"


def _bash_workdir() -> Path:
    """Return the directory where the bash tool starts a command.

    Returns:
        This process's working directory, which ``bash.py`` passes on to every command.
    """
    return Path.cwd()


class PermissionRuleError(ValueError):
    """Raised when a permission rule or a rules file is not valid."""


def canonical_arguments(arguments: Mapping[str, object]) -> str:
    """Return the canonical JSON of a call's arguments, as a session rule stores it.

    Args:
        arguments: The call's arguments.

    Returns:
        JSON with sorted keys and no spaces.

    Raises:
        TypeError: When an argument is not JSON-serialisable.
    """
    return json.dumps(dict(arguments), sort_keys=True, separators=(",", ":"), ensure_ascii=False)


@dataclass(frozen=True)
class ParamPattern:
    """One ``name=value`` part of a ``tool(…)`` rule.

    Attributes:
        name: The parameter name.
        value: The value for an exact match (typed for JSON scalars).
        prefix: The string prefix for a ``*`` match, or ``None`` for an exact match.
    """

    name: str
    value: JsonScalar = None
    prefix: str | None = None

    def matches(self, arguments: Mapping[str, object]) -> bool:
        """Report whether the call's value for this parameter matches.

        Args:
            arguments: The call's arguments.

        Returns:
            ``True`` when the parameter is present and matches. An absent parameter never
            matches.
        """
        if self.name not in arguments:
            return False
        actual = arguments[self.name]
        if self.prefix is not None:
            return isinstance(actual, str) and actual.startswith(self.prefix)
        if isinstance(self.value, str):
            return isinstance(actual, str) and actual == self.value
        # Typed equality: True must not equal 1, so compare JSON forms.
        try:
            return json.dumps(actual) == json.dumps(self.value)
        except (TypeError, ValueError):
            return False


@dataclass(frozen=True)
class PermissionRule:
    """One parsed rule.

    Attributes:
        text: The rule as written.
        tool: The tool name the rule is for.
        bash_words: For a ``bash(…)`` rule, the words. ``None`` for any other form.
        bash_prefix: Whether a ``bash(…)`` rule ends in ``*``.
        params: For a ``tool(k=v, …)`` rule, the parameter patterns. ``None`` for any other form.
        session_arguments: For a session rule, the canonical JSON of the arguments.
        modes: The modes the rule applies in, or ``None`` for every mode.
        reason: Why the rule exists. Required for a deny rule.
    """

    text: str
    tool: str
    bash_words: tuple[str, ...] | None = None
    bash_prefix: bool = False
    params: tuple[ParamPattern, ...] | None = None
    session_arguments: str | None = None
    modes: frozenset[str] | None = None
    reason: str | None = None

    @property
    def matches_every_call(self) -> bool:
        """Whether the rule is the bare ``tool`` form."""
        return self.bash_words is None and self.params is None and self.session_arguments is None

    def applies_in(self, mode: str) -> bool:
        """Report whether the rule applies in a mode.

        Args:
            mode: The current mode name.

        Returns:
            ``True`` when the rule has no mode list or lists ``mode``.
        """
        return self.modes is None or mode in self.modes

    def matches_call(self, arguments: Mapping[str, object]) -> bool:
        """Report whether a non-bash rule matches a call.

        Args:
            arguments: The call's arguments.

        Returns:
            ``True`` on a match.
        """
        if self.session_arguments is not None:
            try:
                return canonical_arguments(arguments) == self.session_arguments
            except (TypeError, ValueError):
                return False
        if self.params is None:
            return self.bash_words is None
        return all(pattern.matches(arguments) for pattern in self.params)

    def names_subcommand(self, sub: Subcommand) -> bool:
        """Report whether a deny or ask ``bash`` rule names a subcommand.

        A subcommand with a non-literal word is matched on its leading literal words, and only
        by a prefix rule: an exact rule cannot know what the expansion holds.

        Args:
            sub: A subcommand from :func:`parse_bash`.

        Returns:
            ``True`` on a match.
        """
        if self.bash_words is None:
            return self.matches_every_call
        if sub.words is not None:
            return _words_match(sub.words, self.bash_words, self.bash_prefix)
        return self.bash_prefix and _words_match(sub.literal_prefix, self.bash_words, True)

    def allows_subcommand(self, sub: Subcommand) -> bool:
        """Report whether an allow ``bash`` rule allows a subcommand (D8.2).

        Args:
            sub: A subcommand from :func:`parse_bash`.

        Returns:
            ``True`` only when the subcommand meets the closed condition and the words match. An
            indirect-execution word matches only an exact rule. A skill-script rule also matches
            its script named as ``/app/<path>`` when bash starts in ``/app`` (FRE-1589).
        """
        if sub.refusal is not None or sub.words is None or self.bash_words is None:
            return False
        if sub.indirect and self.bash_prefix:
            return False
        if _words_match(sub.words, self.bash_words, self.bash_prefix):
            return True
        return (
            self._is_skill_script_rule
            and _bash_workdir() == Path(_IMAGE_WORKDIR)
            and _words_match(
                sub.words, ("python3", f"{_IMAGE_WORKDIR}/{self.bash_words[1]}"), prefix=True
            )
        )

    @property
    def _is_skill_script_rule(self) -> bool:
        """Whether the rule is ``bash(python3 docs/skills/<skill>/scripts/<file>.py *)``."""
        return (
            self.bash_prefix
            and self.bash_words is not None
            and len(self.bash_words) == 2
            and self.bash_words[0] == "python3"
            and _SKILL_SCRIPT.fullmatch(self.bash_words[1]) is not None
        )


def _words_match(words: Sequence[str], rule_words: Sequence[str], prefix: bool) -> bool:
    """Compare a subcommand's words with a rule's words.

    Args:
        words: The subcommand's words.
        rule_words: The rule's words.
        prefix: Whether the rule is a prefix rule.

    Returns:
        ``True`` when equal, or for a prefix rule when ``words`` starts with ``rule_words``.
    """
    if prefix:
        return tuple(words[: len(rule_words)]) == tuple(rule_words)
    return tuple(words) == tuple(rule_words)


def _split_params(body: str) -> list[str]:
    """Split a ``tool(…)`` body on the commas outside double quotes.

    Args:
        body: The text between the parentheses.

    Returns:
        The parts, stripped.

    Raises:
        PermissionRuleError: On an unterminated quote.
    """
    parts: list[str] = []
    current: list[str] = []
    in_quote = False
    i = 0
    while i < len(body):
        c = body[i]
        if in_quote:
            current.append(c)
            if c == "\\" and i + 1 < len(body):
                current.append(body[i + 1])
                i += 2
                continue
            if c == '"':
                in_quote = False
        elif c == '"':
            in_quote = True
            current.append(c)
        elif c == ",":
            parts.append("".join(current).strip())
            current = []
        else:
            current.append(c)
        i += 1
    if in_quote:
        raise PermissionRuleError(f"unterminated quote in {body!r}")
    parts.append("".join(current).strip())
    return parts


def _parse_value(raw: str, rule: str) -> ParamPattern:
    """Parse the value part of one ``name=value`` pattern, with an empty name.

    Args:
        raw: The value text.
        rule: The whole rule, for error messages.

    Returns:
        A pattern with ``name`` empty, to be filled in by the caller.

    Raises:
        PermissionRuleError: When the value is not valid.
    """
    if raw.startswith('"'):
        try:
            value, end = json.JSONDecoder().raw_decode(raw)
        except json.JSONDecodeError as exc:
            raise PermissionRuleError(f"{rule}: bad quoted value {raw!r}") from exc
        rest = raw[end:]
        if rest == "*":
            return ParamPattern(name="", prefix=str(value))
        if rest:
            raise PermissionRuleError(f"{rule}: text after a quoted value: {rest!r}")
        return ParamPattern(name="", value=str(value))
    if not raw or '"' in raw or "(" in raw or ")" in raw:
        raise PermissionRuleError(f"{rule}: bad value {raw!r}")
    if raw.endswith("*"):
        return ParamPattern(name="", prefix=raw[:-1])
    if raw in ("true", "false", "null") or _NUMBER.fullmatch(raw):
        return ParamPattern(name="", value=json.loads(raw))
    return ParamPattern(name="", value=raw)


def parse_rule(
    text: str,
    *,
    modes: Collection[str] | None = None,
    reason: str | None = None,
    allow_session_form: bool = False,
) -> PermissionRule:
    """Parse one rule.

    Args:
        text: The rule, for example ``bash(git status *)`` or ``run_python(network=true)``.
        modes: The modes the rule applies in, or ``None`` for every mode.
        reason: Why the rule exists.
        allow_session_form: Whether the ``tool(=<JSON>)`` form is accepted. Only session rules
            use it.

    Returns:
        The parsed rule.

    Raises:
        PermissionRuleError: When the rule does not follow the grammar.
    """
    match = _RULE.fullmatch(text.strip())
    if match is None:
        raise PermissionRuleError(f"not a rule: {text!r}")
    tool = match.group("tool")
    body = match.group("body")
    frozen_modes = frozenset(modes) if modes is not None else None
    base = PermissionRule(text=text.strip(), tool=tool, modes=frozen_modes, reason=reason)
    if body is None:
        return base
    if not body.strip():
        raise PermissionRuleError(f"{text!r}: empty parentheses; write {tool!r} for every call")
    if body.startswith("="):
        if not allow_session_form:
            raise PermissionRuleError(f"{text!r}: the session form is not allowed in a rules file")
        try:
            canonical = canonical_arguments(json.loads(body[1:]))
        except (ValueError, TypeError, AttributeError) as exc:
            raise PermissionRuleError(f"{text!r}: bad session arguments") from exc
        return PermissionRule(
            text=base.text, tool=tool, session_arguments=canonical, modes=frozen_modes
        )
    if tool == "bash":
        try:
            words = shlex.split(body)
        except ValueError as exc:
            raise PermissionRuleError(f"{text!r}: {exc}") from exc
        prefix = bool(words) and words[-1] == "*"
        if prefix:
            words = words[:-1]
        if not words:
            raise PermissionRuleError(f"{text!r}: no words; write 'bash' for every call")
        if "*" in words:
            raise PermissionRuleError(f"{text!r}: '*' is allowed only as the last word")
        return PermissionRule(
            text=base.text,
            tool=tool,
            bash_words=tuple(words),
            bash_prefix=prefix,
            modes=frozen_modes,
            reason=reason,
        )
    patterns: list[ParamPattern] = []
    seen: set[str] = set()
    for part in _split_params(body):
        name, eq, raw = part.partition("=")
        name = name.strip()
        if not eq or not _PARAM_NAME.fullmatch(name):
            raise PermissionRuleError(f"{text!r}: expected name=value, got {part!r}")
        if name in seen:
            raise PermissionRuleError(f"{text!r}: parameter {name!r} named twice")
        seen.add(name)
        value = _parse_value(raw.strip(), text)
        patterns.append(ParamPattern(name=name, value=value.value, prefix=value.prefix))
    return PermissionRule(
        text=base.text, tool=tool, params=tuple(patterns), modes=frozen_modes, reason=reason
    )


def _skill_script_refusal(path: str) -> str | None:
    """Return why ``path`` is not one reviewed skill script, or ``None`` (FRE-1585).

    The bash tool starts every command in this process's directory (``/app`` in the image), so
    the path is checked there, as bash will open it. The file must be a regular file, and no
    part of the path may be a symbolic link, so the rule names the file that the image holds.

    Args:
        path: The script path of a ``bash(python3 <path> *)`` rule.

    Returns:
        A reason, or ``None`` when the path names one file under ``docs/skills/*/scripts/``.
    """
    if not _SKILL_SCRIPT.fullmatch(path):
        return f"{path!r} is not docs/skills/<skill>/scripts/<file>.py"
    base = _bash_workdir()
    current = base
    for part in Path(path).parts:
        current = current / part
        if current.is_symlink():
            return f"{path!r} passes through a symbolic link"
    if not current.is_file():
        return f"{path!r} is not a file in {base}"
    return None


def _check_allow_rule(rule: PermissionRule) -> None:
    """Reject an allow rule that would let a program run another program (D8.2).

    One prefix form of a program-running word loads: ``bash(python3 <path> *)``, when
    ``<path>`` names one reviewed script under ``docs/skills/*/scripts/`` (FRE-1585). The words
    after the script are the script's own arguments.

    Args:
        rule: A parsed allow rule.

    Raises:
        PermissionRuleError: For bare ``bash``, for ``bash(<word> *)`` where ``<word>`` runs
            another program (except the skill-script form above), and for a ``bash(curl … *)``
            prefix rule (FRE-1583).
    """
    if rule.tool != "bash":
        return
    if rule.bash_words is None:
        raise PermissionRuleError(
            f"{rule.text!r}: an allow rule for every bash command is not allowed"
        )
    first = rule.bash_words[0].rsplit("/", 1)[-1]
    if rule.bash_prefix and rule.bash_words[0] == "python3" and len(rule.bash_words) == 2:
        refusal = _skill_script_refusal(rule.bash_words[1])
        if refusal is None:
            return
        raise PermissionRuleError(
            f"{rule.text!r}: {refusal}. A python3 prefix rule must be "
            "'bash(python3 docs/skills/<skill>/scripts/<file>.py *)' for one reviewed script "
            "(FRE-1585)"
        )
    if rule.bash_prefix and first == "curl":
        raise PermissionRuleError(
            f"{rule.text!r}: a curl prefix rule would pass curl calls that curl_read does not "
            "list (FRE-1583). List the request under curl_read, or name the full command"
        )
    if rule.bash_prefix and first in PROGRAM_RUNNING_WORDS:
        raise PermissionRuleError(
            f"{rule.text!r}: {first!r} can run another program, so an allow rule must name "
            f"the full command, for example 'bash({first} …)' without '*'"
        )


@dataclass(frozen=True)
class PermissionRules:
    """The loaded rule lists.

    Attributes:
        deny: Deny rules, in file order.
        ask: Ask rules, in file order.
        allow: Allow rules, in file order.
        curl_read: The curl requests that run without asking (FRE-1583). A ``curl`` subcommand
            that no allow rule matches is allowed only when one of these lists its request.
    """

    deny: tuple[PermissionRule, ...] = ()
    ask: tuple[PermissionRule, ...] = ()
    allow: tuple[PermissionRule, ...] = ()
    curl_read: tuple[CurlReadRule, ...] = ()

    def merged(self, other: PermissionRules) -> PermissionRules:
        """Return these rules with another set's rules added after them.

        Args:
            other: The rules to add.

        Returns:
            The combined rules.
        """
        return PermissionRules(
            deny=self.deny + other.deny,
            ask=self.ask + other.ask,
            allow=self.allow + other.allow,
            curl_read=self.curl_read + other.curl_read,
        )


def _string_list(value: object) -> list[str] | None:
    """Return a non-empty list of strings, or ``None`` when the value is not one.

    Args:
        value: A YAML value.

    Returns:
        The list, or ``None``.
    """
    if not isinstance(value, list) or not value or not all(isinstance(v, str) for v in value):
        return None
    return list(value)


def _parse_curl_read(item: object, *, source: str) -> CurlReadRule:
    """Validate and parse one ``curl_read`` entry.

    Args:
        item: The YAML entry.
        source: Where it came from, for error messages.

    Returns:
        The parsed entry.

    Raises:
        PermissionRuleError: When the entry is not valid.
    """
    if not isinstance(item, dict):
        raise PermissionRuleError(f"{source}: each curl_read item must be a mapping")
    extra = set(item) - _CURL_READ_KEYS
    if extra:
        raise PermissionRuleError(f"{source}: curl_read item has unknown keys {sorted(extra)}")
    hosts = _string_list(item.get("hosts"))
    if hosts is None or not all(_CURL_HOST.fullmatch(h) for h in hosts):
        raise PermissionRuleError(f"{source}: curl_read hosts must be lower-case host:port")
    methods = _string_list(item.get("methods"))
    if methods is None or set(methods) - CURL_READ_METHODS:
        raise PermissionRuleError(
            f"{source}: curl_read methods must be a list of {sorted(CURL_READ_METHODS)}"
        )
    paths = _string_list(item.get("paths"))
    if paths is None or not all(_CURL_PATH.fullmatch(p) for p in paths):
        raise PermissionRuleError(
            f"{source}: curl_read paths must start with '/' and hold literal or '*' segments"
        )
    body = item.get("body", False)
    if not isinstance(body, bool):
        raise PermissionRuleError(f"{source}: curl_read body must be true or false")
    modes = item.get("modes")
    if modes is not None and (_string_list(modes) is None or set(modes) - _VALID_MODES):
        raise PermissionRuleError(
            f"{source}: curl_read modes must be a list of {sorted(_VALID_MODES)}"
        )
    reason = item.get("reason")
    if reason is not None and (not isinstance(reason, str) or not reason.strip()):
        raise PermissionRuleError(f"{source}: curl_read reason must be text")
    text = f"curl_read({','.join(hosts)} {','.join(methods)}{' +body' if body else ''})"
    return CurlReadRule(
        text=text,
        hosts=frozenset(hosts),
        methods=frozenset(methods),
        paths=tuple(paths),
        body=body,
        modes=frozenset(modes) if modes is not None else None,
    )


def parse_rules_document(document: object, *, source: str) -> PermissionRules:
    """Validate and parse one rules document.

    Args:
        document: The YAML document, already loaded.
        source: Where it came from, for error messages.

    Returns:
        The parsed rules.

    Raises:
        PermissionRuleError: When the document is not valid.
    """
    if not isinstance(document, dict):
        raise PermissionRuleError(f"{source}: expected a mapping with deny, ask and allow")
    unknown = set(document) - {*_LIST_KEYS, _CURL_READ_KEY}
    if unknown:
        raise PermissionRuleError(f"{source}: unknown keys {sorted(unknown)}")
    lists: dict[str, list[PermissionRule]] = {key: [] for key in _LIST_KEYS}
    for key in _LIST_KEYS:
        items = document.get(key) or []
        if not isinstance(items, list):
            raise PermissionRuleError(f"{source}: {key} must be a list")
        for item in items:
            if not isinstance(item, dict) or not isinstance(item.get("rule"), str):
                raise PermissionRuleError(f"{source}: each {key} item needs a 'rule' string")
            extra = set(item) - _ITEM_KEYS
            if extra:
                raise PermissionRuleError(f"{source}: {item['rule']!r} has unknown keys {extra}")
            modes = item.get("modes")
            if modes is not None:
                if not isinstance(modes, list) or not modes or set(modes) - _VALID_MODES:
                    raise PermissionRuleError(
                        f"{source}: {item['rule']!r} modes must be a list of {sorted(_VALID_MODES)}"
                    )
            reason = item.get("reason")
            if reason is not None and (not isinstance(reason, str) or not reason.strip()):
                raise PermissionRuleError(f"{source}: {item['rule']!r} reason must be text")
            if key == "deny" and reason is None:
                raise PermissionRuleError(f"{source}: deny rule {item['rule']!r} needs a reason")
            rule = parse_rule(item["rule"], modes=modes, reason=reason)
            if key == "allow":
                _check_allow_rule(rule)
            lists[key].append(rule)
    curl_items = document.get(_CURL_READ_KEY) or []
    if not isinstance(curl_items, list):
        raise PermissionRuleError(f"{source}: {_CURL_READ_KEY} must be a list")
    return PermissionRules(
        deny=tuple(lists["deny"]),
        ask=tuple(lists["ask"]),
        allow=tuple(lists["allow"]),
        curl_read=tuple(_parse_curl_read(item, source=source) for item in curl_items),
    )


def rules_file_paths() -> list[Path]:
    """Return the rules files that this deployment loads, in order.

    Returns:
        ``permissions.yaml`` in the governance directory, then the extra file when set.
    """
    paths = [Path(settings.governance_config_path) / RULES_FILE_NAME]
    if settings.extra_permission_rules_path is not None:
        paths.append(Path(settings.extra_permission_rules_path))
    return paths


def load_permission_rules(paths: Sequence[Path] | None = None) -> PermissionRules:
    """Load and validate the rules files.

    Read on every call, without a cache, so a broken file denies the next call instead of a
    stale copy allowing it.

    Args:
        paths: The files to load, in order. Defaults to :func:`rules_file_paths`.

    Returns:
        The merged rules.

    Raises:
        PermissionRuleError: When a file is missing or not valid.
    """
    rules = PermissionRules()
    for path in paths if paths is not None else rules_file_paths():
        try:
            document = yaml.safe_load(path.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError) as exc:
            raise PermissionRuleError(f"{path}: {exc}") from exc
        rules = rules.merged(parse_rules_document(document, source=str(path)))
    return rules


@dataclass(frozen=True)
class PermissionDecision:
    """The outcome of evaluating one call.

    Attributes:
        decision: ``allow`` runs the call, ``ask`` needs the user's answer, ``deny`` refuses it.
        reason: Why, in plain text.
        rule: The rule that decided it, or ``None`` when no rule matched. For an allowed bash
            command, every allow rule that matched a subcommand, comma-separated.
        refused: For ``ask`` and ``deny``, the subcommand or argument that decided it.
        explicit: For ``ask``, whether an explicit ask rule matched. Only an unmatched call
            (``explicit`` false) offers "allow for this session" (D8.3).
    """

    decision: Literal["allow", "ask", "deny"]
    reason: str
    rule: str | None = None
    refused: str | None = None
    explicit: bool = False


def _call_summary(tool: str, arguments: Mapping[str, object]) -> str:
    """Return a short text of a call for a refusal message.

    Args:
        tool: The tool name.
        arguments: The call's arguments.

    Returns:
        The bash command, or the arguments as JSON.
    """
    if tool == "bash" and isinstance(arguments.get("command"), str):
        return str(arguments["command"])
    try:
        return canonical_arguments(arguments)
    except (TypeError, ValueError):
        return repr(dict(arguments))


def evaluate_permission(
    tool: str,
    arguments: Mapping[str, object],
    mode: str,
    rules: PermissionRules,
    session_rules: Collection[PermissionRule] = (),
) -> PermissionDecision:
    """Evaluate one call against the rules: deny, then ask, then allow; no match asks.

    Args:
        tool: The tool name.
        arguments: The call's arguments.
        mode: The current mode name.
        rules: The loaded rules.
        session_rules: This session's "allow for this session" rules.

    Returns:
        The decision.
    """
    deny = [r for r in rules.deny if r.tool == tool and r.applies_in(mode)]
    ask = [r for r in rules.ask if r.tool == tool and r.applies_in(mode)]
    allow = [r for r in rules.allow if r.tool == tool and r.applies_in(mode)]
    session = [r for r in session_rules if r.tool == tool]

    if tool == "bash":
        curl_read = [r for r in rules.curl_read if r.applies_in(mode)]
        return _evaluate_bash(arguments, deny, ask, allow, session, curl_read)

    for rule in deny:
        if rule.matches_call(arguments):
            return PermissionDecision(
                "deny", rule.reason or "denied by rule", rule.text, _call_summary(tool, arguments)
            )
    for rule in ask:
        if rule.matches_call(arguments):
            return PermissionDecision(
                "ask",
                rule.reason or "an ask rule matches",
                rule.text,
                _call_summary(tool, arguments),
                explicit=True,
            )
    for rule in [*session, *allow]:
        if rule.matches_call(arguments):
            return PermissionDecision("allow", "an allow rule matches", rule.text)
    return PermissionDecision(
        "ask", "no rule allows this call", None, _call_summary(tool, arguments)
    )


def _evaluate_bash(
    arguments: Mapping[str, object],
    deny: Sequence[PermissionRule],
    ask: Sequence[PermissionRule],
    allow: Sequence[PermissionRule],
    session: Sequence[PermissionRule],
    curl_read: Sequence[CurlReadRule] = (),
) -> PermissionDecision:
    """Evaluate one bash call, subcommand by subcommand (D8.2).

    Args:
        arguments: The call's arguments.
        deny: Deny rules for bash in this mode.
        ask: Ask rules for bash in this mode.
        allow: Allow rules for bash in this mode.
        session: This session's bash session rules.
        curl_read: The ``curl_read`` entries for this mode (FRE-1583).

    Returns:
        The decision.
    """
    command = arguments.get("command")
    if not isinstance(command, str):
        return PermissionDecision("ask", "no command text to evaluate", None, repr(command))
    parsed = parse_bash(command)

    for rule in deny:
        if rule.matches_every_call:
            return PermissionDecision("deny", rule.reason or "denied by rule", rule.text, command)
        for sub in parsed.all_commands:
            if rule.names_subcommand(sub):
                return PermissionDecision(
                    "deny", rule.reason or "denied by rule", rule.text, sub.text
                )
    for rule in ask:
        if rule.matches_every_call:
            return PermissionDecision(
                "ask", rule.reason or "an ask rule matches", rule.text, command, explicit=True
            )
        for sub in parsed.all_commands:
            if rule.names_subcommand(sub):
                return PermissionDecision(
                    "ask", rule.reason or "an ask rule matches", rule.text, sub.text, explicit=True
                )
    for rule in session:
        if rule.matches_call(arguments):
            return PermissionDecision("allow", "allowed for this session", rule.text)

    if parsed.refusal is not None:
        return PermissionDecision(
            "ask",
            f"the command is not a plain command ({parsed.refusal})",
            None,
            parsed.refused_text or command,
        )
    matched: list[str] = []
    for sub in parsed.top_level:
        if sub.refusal is not None:
            return PermissionDecision(
                "ask", f"the subcommand is not plain ({sub.refusal})", None, sub.text
            )
        allowing = next((r for r in allow if r.allows_subcommand(sub)), None)
        if allowing is None and sub.words and sub.words[0] == "curl":
            request = parse_curl_request(sub.words[1:])
            if isinstance(request, CurlRefusal):
                why = request.reason
            else:
                entry = next((r for r in curl_read if r.allows(request)), None)
                if entry is not None:
                    matched.append(entry.text)
                    continue
                why = f"{request.method} {request.authority}{request.path} is not listed"
            return PermissionDecision(
                "ask", f"curl is not a listed telemetry read ({why})", None, sub.text
            )
        if allowing is None:
            why = (
                "an indirect-execution word needs an exact allow rule"
                if sub.indirect
                else "no allow rule matches this subcommand"
            )
            return PermissionDecision("ask", why, None, sub.text)
        matched.append(allowing.text)
    return PermissionDecision(
        "allow", "every subcommand matches an allow rule", ", ".join(dict.fromkeys(matched))
    )


def decide_permission(
    tool: str,
    arguments: Mapping[str, object],
    mode: str,
    *,
    rules: PermissionRules | None = None,
    session_rules: Collection[PermissionRule] = (),
) -> PermissionDecision:
    """Load the rules and evaluate one call, failing closed.

    Args:
        tool: The tool name.
        arguments: The call's arguments.
        mode: The current mode name.
        rules: Preloaded rules. ``None`` loads the deployment's files.
        session_rules: This session's "allow for this session" rules.

    Returns:
        The decision. A load error or an evaluation error is a ``deny`` (D8.1).
    """
    try:
        loaded = rules if rules is not None else load_permission_rules()
        return evaluate_permission(tool, arguments, mode, loaded, session_rules)
    except Exception as exc:  # noqa: BLE001 — every failure must deny, never allow (D8.1)
        return PermissionDecision(
            "deny",
            f"the permission rules could not be evaluated: {exc}",
            None,
            _call_summary(tool, arguments),
        )
