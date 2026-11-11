"""Native URL fetch tool — ADR-0028 Phase 3, finished for ADR-0138 D2 (FRE-1297).

A typed fetch tool taking ``url`` as its only argument: the fetched page is an
admissible citation source, the model-chosen URL argument is not (D2's independence
rule). ``bash``/curl reads the same page but is a fully-excluded arbitrary-code tool
under that rule, so it never yields a citable source — this tool is what closes that
gap. Uses ``httpx`` for the fetch and stdlib ``html.parser`` for text extraction, no
new dependency required.
"""

from __future__ import annotations

import asyncio
import codecs
import html
import ipaddress
import json
import re
import socket
from collections import OrderedDict
from dataclasses import dataclass
from datetime import UTC, datetime
from html.parser import HTMLParser
from typing import Any, Literal, cast

import httpx
import pypdfium2 as pdfium
from pydantic import JsonValue

from personal_agent.documents.pdf_utils import PDFIUM_LOCK, extract_page_text
from personal_agent.security import (
    EgressBlockedError,
    create_guarded_http_client,
    get_novelty_tracker,
)
from personal_agent.telemetry import TraceContext, get_logger
from personal_agent.tools.contract import ToolError, error_codes
from personal_agent.tools.types import ToolDefinition, ToolParameter

log = get_logger(__name__)

FetchCode = Literal[
    "missing_url",
    "invalid_url",
    "private_target",
    "http_error",
    "blocked_by_domain_guard",
    "connect_failed",
    "upstream_timeout",
    "fetch_failed",
    "no_readable_text",
    "unsupported_binary",
    "pdf_unreadable",
]


class FetchError(ToolError[FetchCode]):
    """A ``fetch_url`` failure (ADR-0156 D6.1)."""


_OTHER_SOURCE = "Use web_search to find another page with the same facts."

# FRE-1672: a URL that failed in a session is not fetched again by that session. A failure the
# error marks ``retryable`` gets one more real attempt (a timeout can be a slow moment); any
# other failure, and a second failure of any kind, is answered from the memo with no request.
_MAX_REAL_ATTEMPTS = 2
# The memo is process memory, so it is bounded: least recently used sessions leave first, and
# the oldest URLs of one session leave first.
_MEMO_MAX_SESSIONS = 256
_MEMO_MAX_URLS = 100


@dataclass(frozen=True)
class _FailedFetch:
    """The last failure of one URL in one session."""

    code: FetchCode
    message: str
    retryable: bool
    attempts: int
    failed_at: datetime


# session id -> URL -> last failure. A session id is required: without one there is no scope.
_failed_fetches: OrderedDict[str, OrderedDict[str, _FailedFetch]] = OrderedDict()


def _repeat_refusal(session_id: str | None, url: str, *, trace_id: str) -> FetchError | None:
    """Build the refusal for a URL that already failed in this session, if there is one.

    Args:
        session_id: Session of the call. ``None`` turns the memo off.
        url: The requested URL.
        trace_id: Trace id for logging.

    Returns:
        A non-retryable ``FetchError`` that carries the stored code and message, or ``None``
        when the URL has no stored failure or its failure still earns another real attempt.
    """
    if not session_id:
        return None
    urls = _failed_fetches.get(session_id)
    entry = urls.get(url) if urls is not None else None
    if entry is None or (entry.retryable and entry.attempts < _MAX_REAL_ATTEMPTS):
        return None
    # A session that keeps asking for failed URLs is in use: refresh it so it is not the one evicted.
    _failed_fetches.move_to_end(session_id)
    log.info(
        "fetch_url_repeat_refused",
        trace_id=trace_id,
        session_id=session_id,
        url=url,
        code=entry.code,
        attempts=entry.attempts,
        failed_at=entry.failed_at.isoformat(),
    )
    return FetchError(
        entry.code,
        f"This URL already failed in this session: {entry.message}",
        retryable=False,
        next_action="Do not call fetch_url for this URL again. " + _OTHER_SOURCE,
    )


def _remember_failure(session_id: str | None, url: str, exc: FetchError) -> None:
    """Store a failed fetch for the session. Does nothing when the call has no session.

    Args:
        session_id: Session of the call.
        url: The requested URL.
        exc: The failure the fetch raised.
    """
    if not session_id:
        return
    urls = _failed_fetches.setdefault(session_id, OrderedDict())
    _failed_fetches.move_to_end(session_id)
    previous = urls.pop(url, None)
    urls[url] = _FailedFetch(
        # ToolError holds the code as ``str``; FetchError only builds it from FetchCode.
        code=cast("FetchCode", exc.code),
        message=exc.message,
        retryable=exc.retryable,
        attempts=(previous.attempts if previous else 0) + 1,
        failed_at=datetime.now(UTC),
    )
    while len(urls) > _MEMO_MAX_URLS:
        urls.popitem(last=False)
    while len(_failed_fetches) > _MEMO_MAX_SESSIONS:
        _failed_fetches.popitem(last=False)


def _forget_failure(session_id: str | None, url: str) -> None:
    """Drop the stored failure of a URL that has now been fetched.

    Args:
        session_id: Session of the call.
        url: The requested URL.
    """
    if session_id and (urls := _failed_fetches.get(session_id)) is not None:
        urls.pop(url, None)


# Tags whose inner content should be skipped entirely.
#
# The second row is boilerplate rather than markup plumbing, and it is here because
# ADR-0138 D3(c) requires navigation and footer text to be **excluded from the matched
# content**: a site-wide nav listing "Mercury", "Health", "Products" would otherwise let
# a fetched page satisfy containment for a claim the article itself never makes. This is
# the only layer that still has the DOM to make that cut correctly (FRE-1282).
_SKIP_TAGS = frozenset(
    [
        "script",
        "style",
        "noscript",
        "head",
        "meta",
        "link",
        "svg",
        "iframe",
        "nav",
        "footer",
        "header",
        "aside",
    ]
)
_BLOCK_TAGS = frozenset(["br", "p", "div", "h1", "h2", "h3", "h4", "h5", "h6", "li", "tr"])

# HTML5 void elements — never have a closing tag and never contain children, so there
# is nothing to skip inside them. `html.parser` calls handle_starttag but never
# handle_endtag for a bare void tag (e.g. `<meta charset=utf-8>`), so a void tag that
# also sits in _SKIP_TAGS (meta, link today) must never increment skip_depth — a
# starttag-only increment with no matching endtag would never return to 0, blanking
# the rest of the document (FRE-1307). Checked independently of _SKIP_TAGS membership
# so adding a future void element to _SKIP_TAGS cannot reintroduce the bug.
_VOID_TAGS = frozenset(
    [
        "area",
        "base",
        "br",
        "col",
        "embed",
        "hr",
        "img",
        "input",
        "link",
        "meta",
        "param",
        "source",
        "track",
        "wbr",
    ]
)
_DEFAULT_MAX_CHARS = 10_000
_MAX_CHARS_CAP = 50_000
_TIMEOUT = 20.0

# FRE-1554: the tool fetches pages the user asked for, as `Claude-User` does, so it says
# so. No "bot": topgear.com refuses any UA that contains it. Not browser-shaped on
# purpose — the owner chose honesty over the extra sites a browser UA would reach.
_USER_AGENT = "Seshat-User/0.1 (personal research assistant; user-initiated)"

# FRE-1554: the response headers that name a site's bot protection. An allowlist, never
# the whole header set, so the log cannot carry a cookie or a token.
_PROTECTION_HEADERS = ("server", "cf-mitigated", "x-datadome", "cf-ray")


def _protection_headers(headers: httpx.Headers) -> dict[str, str]:
    """Return the bot-protection headers present on a response.

    Args:
        headers: Response headers (case-insensitive; a repeated field is comma-joined).

    Returns:
        Map of header name to value, for the ``_PROTECTION_HEADERS`` that are present.
    """
    return {name: headers[name] for name in _PROTECTION_HEADERS if name in headers}


def _header_tokens(headers: httpx.Headers, name: str) -> frozenset[str]:
    """Return the lower-case, comma-split tokens of every value of one header.

    ``httpx.Headers.get`` joins a repeated field (``"nginx, AkamaiGHost"``), so a rule
    must match a token, never the whole string and never a prefix.

    Args:
        headers: Response headers.
        name: Header name (any case).

    Returns:
        The set of tokens. Empty if the header is absent.
    """
    return frozenset(
        token.strip().lower()
        for value in headers.get_list(name)
        for token in value.split(",")
        if token.strip()
    )


def _bot_protection_cause(status: int, headers: httpx.Headers) -> str | None:
    """Name the bot protection behind a 403, if the headers identify one.

    Only a 403 is classified: Akamai and Cloudflare also put ``server`` on a temporary
    5xx, where "retrying will not help" would be false.

    Args:
        status: HTTP status of the response.
        headers: Response headers.

    Returns:
        A sentence that names the cause, or ``None`` when the status is not 403 or no
        header identifies a protection. ``cf-ray`` and ``server: cloudflare`` also sit on
        an origin 403 (for example a revoked signed link), so that case is hedged and
        does not promise that a retry fails.
    """
    if status != 403:
        return None
    server = _header_tokens(headers, "server")
    protection = None
    if "challenge" in _header_tokens(headers, "cf-mitigated"):
        protection = "Cloudflare challenge"
    elif "x-datadome" in headers:
        protection = "DataDome"
    elif "akamaighost" in server:
        protection = "Akamai"
    if protection:
        return f"blocked by the site's bot protection ({protection}). Retrying will not help."
    if "cloudflare" in server or "cf-ray" in headers:
        return "the site is behind Cloudflare and may use its bot protection."
    return None


async def _resolves_to_private_or_internal(hostname: str) -> bool:
    """Whether ``hostname`` resolves to a loopback/private/link-local/reserved address.

    ``fetch_url`` is the one tool whose target host is fully model-chosen — every other
    ``create_guarded_http_client`` consumer (web_search, context7, the SLM health probe, …)
    hits a fixed, operator-configured base URL, some of them deliberately loopback/internal
    (e.g. the local model server). ``DomainGuard``'s blocklist mode only checks hostnames
    against a malicious-domain feed, so it does not stop a model-chosen URL from reaching an
    internal service. Rather than narrow ``DomainGuard`` itself (shared by seams that
    legitimately target internal addresses), this tool rejects such targets on its own.

    Resolves via DNS rather than only testing whether the host *string* parses as an IP
    literal — this deployment's internal services (``elasticsearch``, ``postgres``,
    ``neo4j``, ``redis``, …) are plain Docker service-name hostnames on a flat network
    (FRE-362), not IP literals, and an IP-literal-only check let them through (PR #957
    bounce). An unresolvable hostname is treated as not-internal here — the connection
    attempt itself then fails with a clear "cannot connect", rather than a DNS failure
    being misclassified as a security block.

    Residual gap, stated rather than claimed closed: the resolved address is used only to
    decide reachability here, not pinned for the request itself, so a DNS answer that
    changes between this check and the actual connect (rebinding) is not closed by this
    alone.
    """
    try:
        loop = asyncio.get_running_loop()
        infos = await loop.getaddrinfo(hostname, None)
    except socket.gaierror:
        return False

    for info in infos:
        raw_addr = info[4][0]
        try:
            addr = ipaddress.ip_address(raw_addr)
        except ValueError:
            continue
        if (
            addr.is_private
            or addr.is_loopback
            or addr.is_link_local
            or addr.is_reserved
            or addr.is_unspecified
        ):
            return True
    return False


async def _reject_private_targets(request: httpx.Request) -> None:
    """Request hook: refuse a target that resolves to a private/internal address.

    Installed alongside ``create_guarded_http_client``'s own ``DomainGuard`` hook, so it
    fires on the initial request and on every redirect (same hook mechanism ADR-0132 D2
    relies on for the domain-blocklist check).
    """
    hostname = request.url.host
    if await _resolves_to_private_or_internal(hostname):
        raise FetchError(
            "private_target",
            f"URL host '{hostname}' resolves to a private or internal address and cannot "
            "be fetched.",
            retryable=False,
            next_action="Do not retry. fetch_url reads public web pages only.",
        )


@dataclass
class _TableRow:
    """Cell state of the current row of one open ``<table>`` (FRE-1764)."""

    cells: int = 0
    last_span: int = 1


# FRE-1764: a colspan beyond this adds no more empty cells, so a hostile page cannot blow
# up the extracted text.
_MAX_COLSPAN = 50
_CELL_SEPARATOR = " | "


def _colspan(attrs: list[tuple[str, str | None]]) -> int:
    """Read a cell's ``colspan``, bounded to ``1.._MAX_COLSPAN``.

    Args:
        attrs: The cell tag's attributes.

    Returns:
        The column count of the cell; 1 when the attribute is absent or not a number.
    """
    for name, value in attrs:
        if name.lower() == "colspan" and value is not None:
            try:
                return max(1, min(int(value.strip()), _MAX_COLSPAN))
            except ValueError:
                return 1
    return 1


class _TextExtractor(HTMLParser):
    """Minimal HTML to plain text extractor using stdlib ``html.parser``.

    A table comes out one row per line with ``" | "`` between cells (FRE-1764). Each open
    ``<table>`` keeps its own row state, so a nested table does not reset the outer row.
    """

    def __init__(self) -> None:
        super().__init__()
        self._parts: list[str] = []
        self._skip_depth = 0
        self._tables: list[_TableRow] = []

    def _handle_table_tag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        """Emit the line breaks and cell separators of a table start tag.

        Args:
            tag: ``table``, ``tr``, ``td`` or ``th`` (lower case).
            attrs: The tag's attributes.
        """
        if tag == "table":
            self._tables.append(_TableRow())
            self._parts.append("\n")
        elif tag == "tr":
            if self._tables:
                self._tables[-1] = _TableRow()
            self._parts.append("\n")
        elif self._tables:  # td or th inside a table
            row = self._tables[-1]
            # A spanned cell is followed by one empty cell per extra column it covers.
            self._parts.append(_CELL_SEPARATOR * row.last_span if row.cells else " ")
            row.cells += 1
            row.last_span = _colspan(attrs)
        else:  # a stray cell outside any table: a word boundary only
            self._parts.append(" ")

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if tag in _SKIP_TAGS and tag not in _VOID_TAGS:
            self._skip_depth += 1
        elif tag in ("table", "tr", "td", "th"):
            if self._skip_depth == 0:
                self._handle_table_tag(tag, attrs)
            elif tag == "table":
                # Keep the stack balanced with the end tag; a skipped table emits nothing.
                self._tables.append(_TableRow())
        elif tag in _BLOCK_TAGS:
            self._parts.append("\n")
        else:
            # Inline tags (span, a, td, …) carry no separator of their own; without
            # one, adjacent elements with no literal whitespace between them — a
            # common shape in templated markup, e.g. <td>A</td><td>B</td> — extract
            # as "AB" instead of "A B", corrupting the word boundary this tool's
            # citation-content use depends on. get_text() collapses the resulting
            # whitespace runs.
            self._parts.append(" ")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in _VOID_TAGS:
            # A void element never opened a skip region (handle_starttag guards the
            # same way), so it must never close one either. Without this guard, a
            # self-closed void tag (e.g. `<link />`) reaches here too — html.parser's
            # default handle_startendtag calls handle_starttag then handle_endtag for
            # self-closed tags — and would prematurely decrement an *enclosing* skip
            # region's depth (e.g. `<head><link/><title>SECRET</title></head>` would
            # leak "SECRET"), same shape as FRE-1307's original bug.
            return
        if tag in _SKIP_TAGS:
            self._skip_depth = max(0, self._skip_depth - 1)
        elif tag == "table":
            if self._tables:
                self._tables.pop()
            # The text after a table starts its own line, not the end of the last row.
            self._parts.append("\n")
        elif tag not in _BLOCK_TAGS:
            self._parts.append(" ")

    def handle_data(self, data: str) -> None:
        if self._skip_depth == 0:
            self._parts.append(data)

    def get_text(self) -> str:
        """Return extracted text with collapsed whitespace."""
        raw = html.unescape("".join(self._parts))
        raw = re.sub(r"[ \t]+", " ", raw)
        raw = re.sub(r"\n{3,}", "\n\n", raw)
        lines = [line.strip() for line in raw.splitlines()]
        return "\n".join(line for line in lines if line).strip()


def _extract_text(html_body: str) -> str:
    """Extract readable text from an HTML document."""
    extractor = _TextExtractor()
    extractor.feed(html_body)
    return extractor.get_text()


# FRE-1764: a key that names the position of each element in its sibling arrays, so the
# arrays can be read as rows. snake/kebab case words, then a camelCase suffix.
_INDEX_KEY_WORDS = (
    "time|times|timestamp|timestamps|date|dates|datetime|datetimes|index|indices|idx"
    "|hour|hours|day|days|epoch|period|periods|ts"
)
_INDEX_KEY = re.compile(rf"(?:^|[_-])(?:{_INDEX_KEY_WORDS})$", re.IGNORECASE)
_CAMEL_INDEX_KEY = re.compile(r"[a-z0-9](?:Time|Date|Timestamp|Datetime|Index|Hour|Day)s?$")


@dataclass(frozen=True)
class _RowGroup:
    """Sibling arrays of one JSON object, rendered one line per index."""

    label: str
    index_key: str
    lines: tuple[str, ...]


def _is_json(content_type: str) -> bool:
    """Whether a ``Content-Type`` names JSON (``application/json`` or any ``+json`` type).

    Args:
        content_type: The ``Content-Type`` header value (may be empty).

    Returns:
        ``True`` for a JSON media type.
    """
    media_type = content_type.split(";", 1)[0].strip().lower()
    return media_type.endswith(("/json", "+json"))


def _is_index_key(key: str) -> bool:
    """Whether a JSON key looks like a time or index axis.

    Args:
        key: The object key.

    Returns:
        ``True`` when the key's last word is a time or index word.
    """
    return bool(_INDEX_KEY.search(key) or _CAMEL_INDEX_KEY.search(key))


def _cell(value: JsonValue) -> str:
    """Render one array element on one line.

    Args:
        value: A scalar JSON value.

    Returns:
        A string as it is (line breaks become spaces), anything else as JSON.
    """
    if isinstance(value, str):
        return value.replace("\r\n", " ").replace("\n", " ").replace("\r", " ")
    return json.dumps(value, ensure_ascii=False)


def _row_groups(obj: dict[str, JsonValue]) -> list[tuple[str, list[str]]]:
    """Find the sibling arrays of an object that line up by index.

    Args:
        obj: One JSON object.

    Returns:
        One ``(index_key, keys)`` pair per group: two or more arrays of scalars with the
        same length (at least 2), one of them under a time or index key. ``keys`` keeps the
        object's key order with the index key first.
    """
    by_length: dict[int, list[str]] = {}
    for key, value in obj.items():
        if (
            isinstance(value, list)
            and len(value) >= 2
            and not any(isinstance(item, dict | list) for item in value)
        ):
            by_length.setdefault(len(value), []).append(key)
    groups: list[tuple[str, list[str]]] = []
    for keys in by_length.values():
        index_keys = [key for key in keys if _is_index_key(key)]
        if len(keys) >= 2 and index_keys:
            index_key = index_keys[0]
            groups.append((index_key, [index_key, *(k for k in keys if k != index_key)]))
    return groups


def _restructure(
    value: JsonValue,
    label: str,
    units: JsonValue,
    found: list[_RowGroup],
) -> JsonValue:
    """Copy a JSON value with every group of parallel arrays moved out into rows.

    Args:
        value: The JSON value to walk.
        label: Its path in the document, for the row heading.
        units: The ``<key>_units`` object next to it in its parent, or ``None``.
        found: Collects the row groups, in document order.

    Returns:
        The value with each grouped array replaced by a short pointer to its rows.
    """
    if isinstance(value, list):
        return [_restructure(item, f"{label}[{i}]", None, found) for i, item in enumerate(value)]
    if not isinstance(value, dict):
        return value
    unit_of = units if isinstance(units, dict) else {}
    out: dict[str, JsonValue] = {}
    for index_key, keys in _row_groups(value):
        columns = [cast("list[JsonValue]", value[key]) for key in keys]
        lines = []
        for i, index_value in enumerate(columns[0]):
            cells = [_cell(index_value)]
            for key, column in zip(keys[1:], columns[1:], strict=True):
                unit = unit_of.get(key)
                suffix = f" {unit}" if isinstance(unit, str) and unit else ""
                cells.append(f"{key}={_cell(column[i])}{suffix}")
            lines.append(" | ".join(cells))
        found.append(_RowGroup(label=label, index_key=index_key, lines=tuple(lines)))
        pointer = f"<{len(lines)} values: see the rows of {label} by {index_key} below>"
        out.update(dict.fromkeys(keys, pointer))
    for key, item in value.items():
        if key not in out:
            child = f"{label}.{key}" if label != "(top level)" else key
            out[key] = _restructure(item, child, value.get(f"{key}_units"), found)
    # Keep the original key order.
    return {key: out[key] for key in value}


def _render_json(body: str) -> str:
    """Render a JSON body so that parallel arrays read one line per index (FRE-1764).

    An API often returns a series as one array per measure (``time``, ``temperature_2m``,
    …), so the model must count positions to pair a value with its time. Each object that
    holds two or more equal-length arrays, one under a time or index key, has those arrays
    rendered as rows such as ``2026-11-11T15:00 | temperature_2m=12.3 °C``, with the unit
    from a sibling ``<key>_units`` object. All other content is kept in full.

    Args:
        body: The response body.

    Returns:
        The body unchanged when it is not valid JSON or holds no parallel arrays; otherwise
        the rest of the document as JSON, then one block of rows per group.
    """
    found: list[_RowGroup] = []
    try:
        document = cast("JsonValue", json.loads(body))
        rest = _restructure(document, "(top level)", None, found)
    except ValueError, RecursionError:
        # Not JSON, or nested too deep to walk: the body as it came, as before FRE-1764.
        return body
    if not found:
        return body
    blocks = [json.dumps(rest, ensure_ascii=False)]
    for group in found:
        heading = f"{group.label}: {len(group.lines)} rows by {group.index_key}"
        blocks.append("\n".join([heading, *group.lines]))
    return "\n\n".join(blocks)


# FRE-1669: a body is judged by its first bytes, not only by its header, so a binary body
# is never returned as text.
_BINARY_SIGNATURES: tuple[tuple[bytes, str], ...] = (
    (b"\x89PNG\r\n\x1a\n", "PNG image"),
    (b"\xff\xd8\xff", "JPEG image"),
    (b"GIF87a", "GIF image"),
    (b"GIF89a", "GIF image"),
    (b"II*\x00", "TIFF image"),
    (b"MM\x00*", "TIFF image"),
    (b"PK\x03\x04", "ZIP archive"),
    (b"PK\x05\x06", "ZIP archive"),
    (b"PK\x07\x08", "ZIP archive"),
    (b"\x1f\x8b", "gzip data"),
    (b"7z\xbc\xaf\x27\x1c", "7-Zip archive"),
    (b"Rar!\x1a\x07", "RAR archive"),
    (b"\xfd7zXZ\x00", "xz archive"),
    (b"\x28\xb5\x2f\xfd", "Zstandard data"),
    (b"\x7fELF", "ELF executable"),
    (b"\xfe\xed\xfa\xce", "Mach-O executable"),
    (b"\xfe\xed\xfa\xcf", "Mach-O executable"),
    (b"\xce\xfa\xed\xfe", "Mach-O executable"),
    (b"\xcf\xfa\xed\xfe", "Mach-O executable"),
)
# A NUL byte this early marks a binary format with no signature above (PE, BMP, ICO,
# WebAssembly). UTF-16 and UTF-32 text holds NUL bytes too, so a byte-order mark exempts it.
# A compressed format without a signature above can hold no NUL byte, and then it is not
# refused.
_SNIFF_BYTES = 1024
_WIDE_TEXT_BOMS = (
    codecs.BOM_UTF16_LE,
    codecs.BOM_UTF16_BE,
    codecs.BOM_UTF32_LE,
    codecs.BOM_UTF32_BE,
)


def _binary_kind(body: bytes) -> str | None:
    """Name the binary format of a response body, if it is one.

    Args:
        body: The raw response body.

    Returns:
        A short name such as ``"PNG image"``, ``"binary data"`` for an unrecognised binary
        body, or ``None`` when the body looks like text.
    """
    for magic, name in _BINARY_SIGNATURES:
        if body.startswith(magic):
            return name
    if body.startswith(b"RIFF") and body[8:12] == b"WEBP":
        return "WebP image"
    # bzip2: "BZh", a level digit, then the block magic. "BZh" alone is too short a prefix.
    if body.startswith(b"BZh") and body[4:10] == b"1AY&SY":
        return "bzip2 data"
    if b"\x00" in body[:_SNIFF_BYTES] and not body.startswith(_WIDE_TEXT_BOMS):
        return "binary data"
    return None


def _is_pdf(content_type: str, body: bytes) -> bool:
    """Whether a response is a PDF, by its header or by its first bytes.

    Args:
        content_type: The ``Content-Type`` header value (may be empty).
        body: The raw response body.

    Returns:
        ``True`` for the ``application/pdf`` media type (any case, any parameters) or a
        body that starts with ``%PDF``. A server can send a PDF under a wrong header.
    """
    media_type = content_type.split(";", 1)[0].strip().lower()
    return media_type == "application/pdf" or body.startswith(b"%PDF")


def _pdf_text(body: bytes) -> str:
    """Read the text layer of every page. Blocking: call it through ``asyncio.to_thread``.

    Args:
        body: The raw PDF bytes.

    Returns:
        The page texts, joined by blank lines.

    Raises:
        pypdfium2.PdfiumError: The bytes are not a PDF that PDFium can open, or the PDF
            needs a password.
    """
    with PDFIUM_LOCK, pdfium.PdfDocument(body) as pdf:
        return "\n\n".join(extract_page_text(page) for page in pdf)


async def _extract_pdf_text(body: bytes, url: str) -> str:
    """Extract the text of a PDF response body.

    Args:
        body: The raw PDF bytes.
        url: The fetched URL, for the error message.

    Returns:
        The text of the PDF.

    Raises:
        FetchError: ``pdf_unreadable`` if PDFium cannot open the body, or
            ``no_readable_text`` if the PDF has no text layer (a scan).
    """
    try:
        text = await asyncio.to_thread(_pdf_text, body)
    except pdfium.PdfiumError as exc:
        raise FetchError(
            "pdf_unreadable",
            f"Fetched {url} as a PDF but could not open it: {exc}",
            retryable=False,
            next_action=("The file is damaged, or it needs a password. " + _OTHER_SOURCE),
        ) from exc
    if not text.strip():
        raise FetchError(
            "no_readable_text",
            f"Fetched {url} as a PDF but it has no text layer.",
            retryable=False,
            next_action="The PDF holds scanned images, not text. " + _OTHER_SOURCE,
        )
    return text


fetch_url_tool = ToolDefinition(
    name="fetch_url",
    description=(
        "Fetch one web page by URL and return its readable text, with HTML tags, scripts, "
        "navigation and footers removed. "
        "A PDF URL returns the PDF's extracted text. Images, archives and other binary "
        "files are refused. "
        "Use it when you have a URL and need its content: the fetched page is a citable "
        "source, and a bash curl fetch of the same page is not. "
        "Use web_search instead when you have no URL. Use bash curl only for a non-GET "
        "request or one that needs custom headers, and then say in the answer that the "
        "result is not citable. A bash curl to an outside host asks the user every time; "
        "fetch_url does not. "
        "It returns at most max_chars characters (default 10,000, at most 50,000); private "
        "and internal addresses are refused, and a site's bot protection can refuse the "
        "fetch. "
        'Examples: fetch_url(url="https://docs.python.org/3/library/asyncio.html"), '
        'fetch_url(url="https://raw.githubusercontent.com/org/repo/main/README.md", '
        "max_chars=30000)."
    ),
    category="network",
    parameters=[
        ToolParameter(
            name="url",
            type="string",
            description="Full URL to fetch (must start with http:// or https://).",
            required=True,
            default=None,
            json_schema=None,
        ),
        ToolParameter(
            name="max_chars",
            type="number",
            description="Maximum characters of extracted text to return (default 10,000, max 50,000).",
            required=False,
            default=None,
            json_schema=None,
        ),
    ],
    # risk_level is "medium" (the `network` category's own default), not web_search's
    # "low" override — deliberately not inherited. web_search's targets are two fixed
    # vendor endpoints (Exa and Brave, FRE-1766); fetch_url's target host is fully
    # model-chosen and unbounded, so it does not earn the same override even with the
    # private-target guard above in place (PR #957 bounce). requires_approval stays
    # False: the concrete exploit that motivated raising this (unauthenticated internal
    # services reachable by Docker service name) is closed by the guard, not by adding
    # approval friction — gating every call behind human approval would reintroduce the
    # friction ADR-0138 built fetch_url to remove, for a residual risk (arbitrary
    # public-host content entering context) already accepted, unapproved, for web_search.
    risk_level="medium",
    allowed_modes=["NORMAL", "DEGRADED"],
    requires_approval=False,
    requires_sandbox=False,
    timeout_seconds=30,
    error_codes=error_codes(FetchError),
    max_result_tokens=18_000,
    truncation_next_action="Call fetch_url again with a lower max_chars.",
    # ADR-0098 Amendment A2: fetch_url addresses exactly one external referent.
    referent_parameter="url",
)


async def download_text(url: str, *, trace_id: str) -> tuple[str, int]:
    """Download one page and extract its readable text, with no length cut.

    The download and extraction half of ``fetch_url``. The FRE-1641 harness records its page
    fixtures through this function, so the text it scores is the text the tool extracts
    (ADR-0158 D1.8).

    Args:
        url: Target URL (http or https), already validated.
        trace_id: Trace id for logging.

    Returns:
        The whole extracted text and the HTTP status of the response.

    Raises:
        FetchError: On a private target, a blocked domain, connection failure, timeout, a
            non-2xx HTTP response, a binary body that is not a PDF (``unsupported_binary``),
            or a PDF that cannot be opened (``pdf_unreadable``) or has no text layer
            (``no_readable_text``).
    """
    try:
        async with create_guarded_http_client(
            timeout=_TIMEOUT,
            follow_redirects=True,
            headers={"User-Agent": _USER_AGENT},
            event_hooks={"request": [_reject_private_targets]},
        ) as client:
            resp = await client.get(url)
            if resp.is_error:
                msg = f"HTTP {resp.status_code} fetching {url}"
                cause = _bot_protection_cause(resp.status_code, resp.headers)
                if cause:
                    msg = f"{msg}: {cause}"
                log.error(
                    "fetch_url_http_error",
                    trace_id=trace_id,
                    status=resp.status_code,
                    url=url,
                    response_headers=_protection_headers(resp.headers),
                )
                temporary = resp.status_code == 429 or resp.status_code >= 500
                raise FetchError(
                    "http_error",
                    msg,
                    retryable=temporary,
                    next_action=(
                        "Call fetch_url once more later. " + _OTHER_SOURCE
                        if temporary
                        else _OTHER_SOURCE
                    ),
                )

            content_type = resp.headers.get("content-type", "")
            body = resp.content
            if _is_pdf(content_type, body):
                text = await _extract_pdf_text(body, url)
            elif (binary_kind := _binary_kind(body)) is not None:
                log.warning(
                    "fetch_url_binary_refused",
                    trace_id=trace_id,
                    url=url,
                    binary_kind=binary_kind,
                    content_type=content_type,
                )
                raise FetchError(
                    "unsupported_binary",
                    f"Fetched {url} (HTTP {resp.status_code}) but the body is "
                    f"{binary_kind}, not text.",
                    retryable=False,
                    next_action="Do not retry. fetch_url reads web pages and PDFs only. "
                    + _OTHER_SOURCE,
                )
            elif "html" in content_type or not content_type:
                text = _extract_text(resp.text)
            elif _is_json(content_type):
                text = _render_json(resp.text)
            else:
                text = resp.text

    except FetchError:
        raise
    except EgressBlockedError as exc:
        log.warning(
            "fetch_url_blocked",
            trace_id=trace_id,
            url=url,
            reason=exc.reason,
            matched_entry=exc.matched_entry,
        )
        detail = f" (matched: {exc.matched_entry})" if exc.matched_entry else ""
        raise FetchError(
            "blocked_by_domain_guard",
            f"URL blocked by domain guard: {url} [{exc.reason}{detail}]",
            retryable=False,
            next_action=_OTHER_SOURCE,
        ) from exc
    except httpx.ConnectError as exc:
        msg = f"Cannot connect to {url}."
        log.error("fetch_url_connect_failed", trace_id=trace_id, url=url, error=msg)
        raise FetchError(
            "connect_failed",
            msg,
            retryable=True,
            next_action="Check the URL and call fetch_url once more. " + _OTHER_SOURCE,
        ) from exc
    except httpx.TimeoutException as exc:
        msg = f"Request to {url} timed out after {_TIMEOUT}s."
        log.error("fetch_url_timeout", trace_id=trace_id, url=url)
        raise FetchError(
            "upstream_timeout",
            msg,
            retryable=True,
            next_action="Call fetch_url once more. " + _OTHER_SOURCE,
        ) from exc
    except Exception as exc:
        log.error("fetch_url_failed", trace_id=trace_id, url=url, error=str(exc), exc_info=True)
        raise FetchError(
            "fetch_failed",
            f"Fetching {url} failed: {type(exc).__name__}: {exc}",
            retryable=False,
            next_action=_OTHER_SOURCE,
        ) from exc

    return text, resp.status_code


async def fetch_url_executor(
    url: str = "",
    max_chars: int | None = None,
    *,
    ctx: TraceContext,
) -> dict[str, Any]:
    """Fetch and extract readable text from a URL.

    Args:
        url: Target URL (http or https).
        max_chars: Maximum characters to return (default 10,000, max 50,000).
        ctx: Trace context for logging.

    Returns:
        Dict with ``url``, ``text`` (extracted content), ``char_count``,
        and ``truncated`` (bool) keys.

    Raises:
        FetchError: On invalid URL, a private target, a blocked domain, connection failure,
            timeout, non-2xx HTTP response, a binary body that is not a PDF, a PDF that
            cannot be opened, a successful response that yields no readable text
            content, or a URL that already failed in this session (FRE-1672; no request is
            made).
    """
    url = (url or "").strip()
    if not url:
        raise FetchError(
            "missing_url",
            "'url' parameter is required and cannot be empty.",
            retryable=True,
            next_action="Call fetch_url again with a full http:// or https:// URL.",
        )
    if not url.startswith(("http://", "https://")):
        raise FetchError(
            "invalid_url",
            f"Invalid URL '{url}'. Must start with http:// or https://.",
            retryable=True,
            next_action="Call fetch_url again with a full http:// or https:// URL.",
        )

    cap = max(1, min(int(max_chars or _DEFAULT_MAX_CHARS), _MAX_CHARS_CAP))
    trace_id = ctx.trace_id

    log.info("fetch_url_started", trace_id=trace_id, url=url)

    # FRE-1672: refuse a repeat before any I/O. A repeat is never a novel destination, so the
    # novelty check below loses nothing by being skipped.
    session_id = ctx.session_id
    if (refusal := _repeat_refusal(session_id, url, trace_id=trace_id)) is not None:
        raise refusal

    # FRE-1330: observe-only signal on what the model *targeted* — fires regardless of
    # whether the fetch below succeeds, is blocked, or errors. Wrapped in its own
    # try/except: a tracker I/O failure must never block or fail a fetch.
    try:
        novelty = await get_novelty_tracker().check_and_record(url)
        if novelty.novel:
            log.info(
                "fetch_url_novel_destination",
                trace_id=trace_id,
                url=url,
                domain=novelty.registrable_domain,
            )
    except Exception as exc:
        log.warning("fetch_url_novelty_check_failed", trace_id=trace_id, url=url, error=str(exc))

    try:
        text, status = await download_text(url, trace_id=trace_id)

        if not text.strip():
            log.warning("fetch_url_empty_extraction", trace_id=trace_id, url=url, status=status)
            raise FetchError(
                "no_readable_text",
                f"Fetched {url} (HTTP {status}) but extracted no readable text content.",
                retryable=False,
                next_action=(
                    "The page has no readable text (it may need JavaScript). " + _OTHER_SOURCE
                ),
            )
    except FetchError as exc:
        _remember_failure(session_id, url, exc)
        raise
    _forget_failure(session_id, url)

    truncated = len(text) > cap
    output_text = text[:cap]

    log.info(
        "fetch_url_completed",
        trace_id=trace_id,
        url=url,
        char_count=len(output_text),
        truncated=truncated,
    )

    return {
        "url": url,
        "text": output_text,
        "char_count": len(output_text),
        "truncated": truncated,
    }
