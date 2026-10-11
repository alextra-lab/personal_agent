"""Reduce a curl call to one request, and judge it against the ``curl_read`` table (FRE-1583).

ADR-0156 D8.2, as amended on 2026-10-10: ``curl`` runs without asking only for a read of internal
telemetry. :func:`parse_curl_request` reads curl's arguments with an allowlist. An option that it
does not list, a second URL, or a URL that it cannot reduce to one host, one method and one path
gives a :class:`CurlRefusal`, and the call asks. A :class:`CurlReadRule` from
``config/governance/permissions.yaml`` then decides whether the request is a listed read.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass

#: The methods a ``curl_read`` entry can list.
CURL_READ_METHODS = frozenset({"GET", "POST"})

#: Options that take no argument and do not change the host, the method, the path or a file.
_FLAGS: dict[str, str] = {
    "-s": "--silent",
    "-S": "--show-error",
    "-f": "--fail",
    "-i": "--include",
    "-G": "--get",
}
_LONG_FLAGS = frozenset({*_FLAGS.values(), "--fail-with-body", "--compressed"})

#: Options that take one argument. The value is checked by :func:`_check_value`.
_WITH_ARG: dict[str, str] = {
    "-m": "--max-time",
    "-X": "--request",
    "-H": "--header",
    "-d": "--data",
    "-o": "--output",
    "-w": "--write-out",
}
_LONG_WITH_ARG = frozenset(
    {
        *_WITH_ARG.values(),
        "--connect-timeout",
        "--data-raw",
        "--data-binary",
        "--data-urlencode",
    }
)
_DATA_OPTIONS = frozenset({"--data", "--data-raw", "--data-binary", "--data-urlencode"})
_ONCE_ONLY = frozenset({"--request", "--output"})

_NUMBER = re.compile(r"[0-9]+(?:\.[0-9]+)?")
_ALLOWED_HEADERS = frozenset({"content-type", "accept"})
#: ``--output`` targets: the response stays on the terminal or is dropped. No file is written.
_OUTPUT_TARGETS = frozenset({"/dev/null", "/dev/stdout"})
# Literal text, %%, %{name} and the escapes \n \r \t. Not %output{…}, %header{…} or @file.
_WRITE_OUT = re.compile(r"(?:[^%@\\]|%%|%\{[a-z_]+\}|\\[nrt])*")
_URL = re.compile(r"http://(?P<authority>[^/?]*)(?P<path>/[^?]*)?(?:\?(?P<query>.*))?", re.DOTALL)
_AUTHORITY = re.compile(r"[a-z0-9-]+(?:\.[a-z0-9-]+)*:[0-9]+")
_PATH_CHARS = re.compile(r"/[A-Za-z0-9._*,/-]*")
_URL_FORBIDDEN = frozenset("#\\{}[]@ ")


@dataclass(frozen=True)
class CurlRequest:
    """The one request that a curl call makes.

    Attributes:
        authority: ``host:port``, exactly as the URL gives it.
        method: ``GET`` or ``POST``.
        path: The URL path, without the query and without one trailing ``/``.
        body: Whether the call sends a request body.
    """

    authority: str
    method: str
    path: str
    body: bool


@dataclass(frozen=True)
class CurlRefusal:
    """Why a curl call does not reduce to one listed request.

    Attributes:
        reason: The reason, in plain text, naming the argument.
    """

    reason: str


def _check_value(option: str, value: str) -> str | None:
    """Judge the value of one option that takes an argument.

    Args:
        option: The long option name.
        value: The option's value.

    Returns:
        A reason when the value is not allowed, else ``None``.
    """
    if option in ("--max-time", "--connect-timeout"):
        return None if _NUMBER.fullmatch(value) else f"{option} takes a number"
    if option == "--request":
        return None if value in CURL_READ_METHODS else f"method {value!r}"
    if option == "--header":
        name, colon, header_value = value.partition(":")
        if colon and name.strip().lower() in _ALLOWED_HEADERS:
            if header_value.strip() == "application/json":
                return None
        return f"header {value!r}"
    if option == "--output":
        return None if value in _OUTPUT_TARGETS else f"--output to {value!r}"
    if option == "--write-out":
        return None if _WRITE_OUT.fullmatch(value) else f"--write-out {value!r}"
    if option == "--data-urlencode":
        return f"{option} reads a file" if re.match(r"^[^=@]*@", value) else None
    if option in _DATA_OPTIONS:
        return f"{option} reads a file" if value.startswith("@") else None
    return None


def _parse_url(url: str) -> tuple[str, str] | CurlRefusal:
    """Split an allowed URL into its authority and its path.

    Args:
        url: The URL word, after quote removal.

    Returns:
        ``(authority, path)``, or a refusal.
    """
    if any(c in _URL_FORBIDDEN or ord(c) < 32 or ord(c) == 127 for c in url):
        return CurlRefusal(f"URL {url!r} holds a character that is not allowed")
    match = _URL.fullmatch(url)
    if match is None:
        return CurlRefusal(f"URL {url!r} is not a plain http:// URL")
    authority = match.group("authority")
    if not _AUTHORITY.fullmatch(authority):
        return CurlRefusal(f"URL host {authority!r} is not host:port")
    path = match.group("path") or "/"
    if not _PATH_CHARS.fullmatch(path):
        return CurlRefusal(f"URL path {path!r} holds a character that is not allowed")
    if "//" in path:
        return CurlRefusal(f"URL path {path!r} has an empty segment")
    if len(path) > 1 and path.endswith("/"):
        path = path[:-1]
    segments = path.split("/")[1:]
    if path != "/" and any(s in ("", ".", "..") for s in segments):
        return CurlRefusal(f"URL path {path!r} has an empty or dot segment")
    return authority, path


def parse_curl_request(args: Sequence[str]) -> CurlRequest | CurlRefusal:
    """Reduce curl's arguments to one request.

    Args:
        args: curl's arguments after quote removal, without the command name.

    Returns:
        The request, or a refusal that names the argument it could not accept.
    """
    urls: list[str] = []
    seen: set[str] = set()
    method: str | None = None
    get = False
    data = False

    def take(option: str, value: str) -> str | None:
        nonlocal method, data
        if option in _ONCE_ONLY and option in seen:
            return f"{option} given twice"
        seen.add(option)
        reason = _check_value(option, value)
        if reason is not None:
            return reason
        if option == "--request":
            method = value
        elif option in _DATA_OPTIONS:
            data = True
        return None

    i = 0
    while i < len(args):
        word = args[i]
        if not word.startswith("-"):
            urls.append(word)
        elif word.startswith("--"):
            if word in _LONG_FLAGS:
                get = get or word == "--get"
            elif word in _LONG_WITH_ARG:
                if i + 1 >= len(args):
                    return CurlRefusal(f"{word} has no value")
                i += 1
                reason = take(word, args[i])
                if reason is not None:
                    return CurlRefusal(reason)
            else:
                return CurlRefusal(f"option {word!r} is not a listed read option")
        elif word == "-":
            return CurlRefusal("option '-' is not a listed read option")
        else:
            j = 1
            while j < len(word):
                letter = f"-{word[j]}"
                if letter in _FLAGS:
                    get = get or letter == "-G"
                    j += 1
                    continue
                if letter in _WITH_ARG:
                    value = word[j + 1 :]
                    if not value:
                        if i + 1 >= len(args):
                            return CurlRefusal(f"{letter} has no value")
                        i += 1
                        value = args[i]
                    reason = take(_WITH_ARG[letter], value)
                    if reason is not None:
                        return CurlRefusal(reason)
                    break
                return CurlRefusal(f"option {letter!r} is not a listed read option")
        i += 1

    if len(urls) != 1:
        return CurlRefusal(f"{len(urls)} URLs; exactly one is allowed")
    parsed = _parse_url(urls[0])
    if isinstance(parsed, CurlRefusal):
        return parsed
    authority, path = parsed
    if method is None:
        method = "GET" if get or not data else "POST"
    return CurlRequest(authority=authority, method=method, path=path, body=data and not get)


def path_matches(pattern: str, path: str) -> bool:
    """Report whether a URL path matches a ``curl_read`` path pattern.

    A literal segment matches itself. ``*`` matches one segment that does not start with ``_``
    (an index name or a trace id, never an API word). Every path is a fixed list of segments, so
    a route that a server adds later under a listed prefix asks.

    Args:
        pattern: The pattern, starting with ``/``.
        path: The URL path, as :func:`parse_curl_request` gives it.

    Returns:
        ``True`` on a match.
    """
    if pattern == "/" or path == "/":
        return pattern == path
    want = pattern.split("/")[1:]
    have = path.split("/")[1:]
    if len(want) != len(have):
        return False
    for w, h in zip(want, have, strict=True):
        if w == "*":
            if h.startswith("_"):
                return False
        elif w != h:
            return False
    return True


@dataclass(frozen=True)
class CurlReadRule:
    """One ``curl_read`` entry: requests that run without asking.

    Attributes:
        text: A short text of the entry, for the decision's ``rule`` field.
        hosts: The ``host:port`` authorities.
        methods: The methods.
        paths: The path patterns.
        body: Whether a request body is allowed.
        modes: The modes the entry applies in, or ``None`` for every mode.
    """

    text: str
    hosts: frozenset[str]
    methods: frozenset[str]
    paths: tuple[str, ...]
    body: bool = False
    modes: frozenset[str] | None = None

    def applies_in(self, mode: str) -> bool:
        """Report whether the entry applies in a mode.

        Args:
            mode: The current mode name.

        Returns:
            ``True`` when the entry has no mode list or lists ``mode``.
        """
        return self.modes is None or mode in self.modes

    def allows(self, request: CurlRequest) -> bool:
        """Report whether the entry lists a request.

        Args:
            request: The request from :func:`parse_curl_request`.

        Returns:
            ``True`` when the host, the method and the path match, and a body is allowed if
            the request has one.
        """
        return (
            request.authority in self.hosts
            and request.method in self.methods
            and (self.body or not request.body)
            and any(path_matches(p, request.path) for p in self.paths)
        )
