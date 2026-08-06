"""Secret redaction for telemetry records (FRE-1068).

Every document written to ``agent-logs-*`` passes through :func:`redact_mapping`
at :meth:`ElasticsearchLogger._index_agent_log`, the single write chokepoint for
that index family.

**Why this lives at the emit seam and not in the index template.** The
``free_text`` dynamic template in ``docker/elasticsearch/index-template.json``
governs *searchability*, not *storage*: Elasticsearch retains the submitted
value in ``_source`` whatever the mapping says. The FRE-1068 audit measured
``arguments.command`` returning zero from an ``exists`` query while 262
documents carried full shell command lines in ``_source`` — and 43 fields in
that state overall. Narrowing the pattern would therefore have closed
nothing. Redaction before the write is the only point that governs what is
actually stored.

**Fail-closed.** A value whose redaction raises becomes ``[REDACTED:error]``
rather than being forwarded intact. "Secrets are always redacted" and "degrade
to the original record on failure" cannot both hold; content loses.

**Precision over recall, deliberately.** Detectors are high-precision and
guarded against environment-lookup and placeholder forms, because live
telemetry contains ``password=os.environ.get(...)`` — redacting that would
destroy diagnostic value while protecting nothing. The cost of that choice is
that a novel secret shape can pass; the inventory script re-runs the same
detectors over the corpus so drift is measurable rather than assumed.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from typing import Final

#: Signature of a :func:`re.sub` replacement callback.
Replacer = Callable[[re.Match[str]], str]

__all__ = ["detect_secrets", "redact_mapping", "redact_text"]

#: Strings shorter than this cannot match any detector, so they skip the scan.
_MIN_SCAN_LENGTH: Final = 6

_MARKER: Final = "[REDACTED:{name}]"
_ERROR_MARKER: Final = "[REDACTED:error]"

#: Credential-bearing key names. Matched on the whole key, optionally with a
#: leading ``<word>_`` qualifier, so ``db_password`` matches while
#: ``api_key_name`` and ``token_count`` do not.
_CREDENTIAL_KEY: Final = re.compile(
    r"(?i)^(?:[a-z0-9]+_)*"
    r"(?:password|passwd|pwd|secret|api_?key|apikey|token|access_key|private_key|credential|credentials)$"
)

#: Value prefixes that denote an indirection or a placeholder rather than a
#: literal secret. Measured against live telemetry (FRE-1068 audit).
_SAFE_VALUE_PREFIXES: Final = (
    "os.environ",
    "os.getenv",
    "environ[",
    "getenv(",
    "process.env",
    "$",
    "{",
    "<",
    "[REDACTED:",
    "%(",
)

#: Whole values that are conventional placeholders.
_SAFE_VALUE_WORDS: Final = frozenset(
    {
        "changeme",
        "redacted",
        "none",
        "null",
        "true",
        "false",
        "password",
        "secret",
        "token",
        "your_password",
        "your-password",
    }
)

#: Value prefixes conventionally used in documentation examples.
_SAFE_VALUE_EXAMPLE_PREFIXES: Final = ("example", "your", "placeholder", "dummy", "sample", "xxx")


def _is_safe_value(value: str) -> bool:
    """Return True when a captured value is an indirection or a placeholder.

    Args:
        value: The candidate secret text captured by an assignment detector.

    Returns:
        True when the value must be left intact.
    """
    stripped = value.strip()
    if not stripped:
        return True
    lowered = stripped.lower()
    if lowered in _SAFE_VALUE_WORDS:
        return True
    if stripped.startswith(_SAFE_VALUE_PREFIXES):
        return True
    return lowered.startswith(_SAFE_VALUE_EXAMPLE_PREFIXES)


def _replace_group(name: str, group: int) -> Replacer:
    """Build a substitution callback that redacts one capture group.

    Args:
        name: Detector name, embedded in the emitted marker.
        group: Index of the capture group holding the secret.

    Returns:
        A callable suitable for :func:`re.sub`.
    """

    def _sub(match: re.Match[str]) -> str:
        secret = match.group(group)
        if _is_safe_value(secret):
            return match.group(0)
        start, end = match.span(group)
        offset = match.start()
        whole = match.group(0)
        return whole[: start - offset] + _MARKER.format(name=name) + whole[end - offset :]

    return _sub


def _replace_all(name: str) -> Replacer:
    """Build a substitution callback that redacts the entire match.

    Args:
        name: Detector name, embedded in the emitted marker.

    Returns:
        A callable suitable for :func:`re.sub`.
    """

    def _sub(_match: re.Match[str]) -> str:
        return _MARKER.format(name=name)

    return _sub


#: Ordered detectors. Specific token shapes run before the generic assignment
#: forms so the emitted marker names the most informative rule; the broad email
#: detector runs last.
_DETECTORS: Final = (
    (
        "private_key",
        re.compile(
            r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----",
        ),
        _replace_all("private_key"),
    ),
    (
        "aws_access_key",
        re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
        _replace_all("aws_access_key"),
    ),
    (
        "jwt",
        re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"),
        _replace_all("jwt"),
    ),
    (
        "github_pat",
        re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"),
        _replace_all("github_pat"),
    ),
    (
        "slack_token",
        re.compile(r"\bxox[abposr]-[A-Za-z0-9-]{10,}\b"),
        _replace_all("slack_token"),
    ),
    (
        "api_key",
        re.compile(r"\b(?:sk|pk|rk)-[A-Za-z0-9_-]{16,}\b"),
        _replace_all("api_key"),
    ),
    (
        "connection_string",
        re.compile(r"\b[a-zA-Z][a-zA-Z0-9+.-]*://[^\s:/@]+:([^\s@/]+)@"),
        _replace_group("connection_string", 1),
    ),
    (
        "credential_flag",
        re.compile(
            r"(?i)--[a-z-]*(?:password|passwd|secret|api-?key|token|access-?key)[=\s]+"
            r"(['\"]?)([^\s'\"]{4,})\1"
        ),
        _replace_group("credential_flag", 2),
    ),
    (
        "credential_assignment",
        re.compile(
            r"(?i)[a-z_]*(?:password|passwd|pwd|secret|api[_-]?key|apikey|token|access[_-]?key)"
            r"\s*[:=]\s*(['\"]?)([^\s'\",;)}\]]{4,})\1"
        ),
        _replace_group("credential_assignment", 2),
    ),
    (
        "email",
        re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"),
        _replace_all("email"),
    ),
)


def _apply_detectors(value: str) -> str:
    """Run every detector over a string, redacting what fires.

    Args:
        value: Raw string to scan.

    Returns:
        The string with each detected secret replaced by an attributable marker.
    """
    for _name, pattern, replacer in _DETECTORS:
        value = pattern.sub(replacer, value)
    return value


def detect_secrets(value: str) -> tuple[str, ...]:
    """Return the names of every detector that fires on a string.

    Reports without redacting, so an audit can measure the detector surface
    over a corpus without ever handling the matched values. Used by
    ``scripts/audit/fre1068_free_text_inventory.py``.

    Reports only detectors that would *actually redact*, not merely those whose
    pattern matches. The two differ: the assignment detectors match
    ``password=os.environ.get(...)`` but decline to redact it, so a
    match-based count would report environment lookups as secrets and inflate
    every audit figure.

    Args:
        value: Raw string to scan.

    Returns:
        Detector names that would redact, in declaration order.
    """
    if len(value) < _MIN_SCAN_LENGTH:
        return ()
    fired: list[str] = []
    for name, pattern, replacer in _DETECTORS:
        if pattern.sub(replacer, value) != value:
            fired.append(name)
    return tuple(fired)


def redact_text(value: str) -> str:
    """Redact secret-shaped substrings from a single string.

    Idempotent: redacting an already-redacted value is a no-op. Never raises —
    a detector failure yields ``[REDACTED:error]`` rather than the raw value.

    Args:
        value: Raw string, typically one telemetry field value.

    Returns:
        The redacted string.
    """
    if len(value) < _MIN_SCAN_LENGTH:
        return value
    try:
        return _apply_detectors(value)
    except Exception:
        return _ERROR_MARKER


def _redact_value(key: str | None, value: object) -> object:
    """Redact one value, recursing into nested containers.

    Args:
        key: Owning field name, or None when the value came from a list.
        value: The value to redact.

    Returns:
        The redacted value; non-string scalars are returned unchanged.
    """
    if isinstance(value, Mapping):
        return {k: _redact_value(k, v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_redact_value(None, item) for item in value]
    if not isinstance(value, str):
        return value
    if key is not None and _CREDENTIAL_KEY.match(key) and not _is_safe_value(value):
        # The field name alone establishes the value is a credential, so the
        # value's own shape does not need to match a detector.
        return _MARKER.format(name="credential_field")
    return redact_text(value)


def redact_mapping(data: Mapping[str, object]) -> dict[str, object]:
    """Redact every string value in a telemetry document, recursively.

    Field names are preserved — a field named ``password`` stays queryable, only
    its value goes. Dicts and lists are traversed at any depth; non-string
    scalars pass through with their types intact. The input is not mutated.

    Args:
        data: The telemetry document about to be indexed.

    Returns:
        A new dict with secret-shaped content replaced by attributable markers.
    """
    return {key: _redact_value(key, value) for key, value in data.items()}
