"""Layer 1 — where, if anywhere, exemption can be proved (ADR-0138 D1, FRE-1281).

This module is deliberately not a classifier. It answers one syntactic question — *is
this provably code, and where inside it can prose hide?* — and hands everything else to
the model pass. D1 requires span classification to be "a named component, not a regex",
and layer 1 respects that by never deciding whether text is a claim.

**Exemption is granted only where it can be proved.** A plan review broke the first draft
here. It treated ``js``, ``sql`` and ``bash`` as code because they *are* code languages,
without any parser to check that a given block actually was code — so arbitrary prose in a
``js`` fence became exempt, which is a complete bypass of the contract and precisely what
ADR-0138 AC-5 fails an implementation for ("fails if fencing or mere parseability buys
exemption"). The rule is now: a parser must exist **and** the content must parse.
Otherwise the region goes to the classifier, which is the component designated to judge
it. This does not block generation — the model pass can still call a ``bash`` block code —
it only stops a table lookup from making that call.

**Prose hides in three places inside real code**, and all three are found without judging
their content: string literals, comments, and docstrings. ``print("Paris has 9 million
residents")`` parses cleanly as Python; without extraction, a parse check alone would
exempt it and a string literal becomes a delivery channel for an uncited assertion.

For Python this uses :mod:`tokenize`, which is exact. For languages with no parser the
scanner tracks quote state, so ``#`` inside a string is not a comment and ``//`` inside a
URL is not one either — a scanner that got this wrong would carve code lines in half and
hand the halves to the classifier as prose.

**Dependency declarations are the hole in the code exemption**, by design: imports,
manifest entries and install commands are verified against the registry or documentation
rather than exempted. That is the anti-squatting property that motivated covering coding
turns at all.
"""

from __future__ import annotations

import ast
import io
import json
import re
import tokenize
from collections.abc import Iterator, Sequence
from enum import StrEnum

import yaml  # type: ignore[import-untyped]
from pydantic import BaseModel, ConfigDict

try:  # pragma: no cover - stdlib on 3.11+, guarded for older interpreters
    import tomllib
except ModuleNotFoundError:  # pragma: no cover
    tomllib = None  # type: ignore[assignment]


class RegionKind(StrEnum):
    """What layer 1 was able to establish about a stretch of output.

    Only :attr:`PROVEN_CODE` is an exemption. :attr:`DEPENDENCY` is a categorical
    citation obligation, and :attr:`CLASSIFY` is an explicit refusal to decide — the
    region is passed to the model pass rather than guessed at.

    :attr:`STRUCTURAL` covers markup that is not content at all — the fence delimiters
    themselves. It exists because folding them into :attr:`PROVEN_CODE` made every
    fence, including a ``text`` one full of prose, report as containing proven code.
    """

    PROVEN_CODE = "proven_code"
    DEPENDENCY = "dependency"
    CLASSIFY = "classify"
    STRUCTURAL = "structural"


class Region(BaseModel):
    """One contiguous stretch of the output, with layer 1's verdict.

    Attributes:
        kind: What was established.
        text: The region's text.
        start: Offset into the whole output.
        end: End offset, exclusive.
        language: Declared fence language, where there was one.
    """

    model_config = ConfigDict(frozen=True)

    kind: RegionKind
    text: str
    start: int
    end: int
    language: str | None = None


#: Fence languages treated as prose outright — D1 names these explicitly.
PROSE_LANGUAGES: frozenset[str] = frozenset(
    {"text", "txt", "plaintext", "plain", "markdown", "md", "prose", "quote"}
)

#: Comment markers per language family, used only where no parser is available. Finding
#: *where* a comment is, is syntax; deciding what it says is not this module's job.
_LINE_COMMENT_MARKERS: dict[str, tuple[str, ...]] = {
    "bash": ("#",),
    "sh": ("#",),
    "shell": ("#",),
    "zsh": ("#",),
    "console": ("#",),
    "ruby": ("#",),
    "perl": ("#",),
    "r": ("#",),
    "js": ("//",),
    "javascript": ("//",),
    "ts": ("//",),
    "typescript": ("//",),
    "jsx": ("//",),
    "tsx": ("//",),
    "go": ("//",),
    "rust": ("//",),
    "java": ("//",),
    "c": ("//",),
    "cpp": ("//",),
    "csharp": ("//",),
    "swift": ("//",),
    "kotlin": ("//",),
    "scala": ("//",),
    "php": ("//", "#"),
    "sql": ("--",),
    "lua": ("--",),
    "haskell": ("--",),
    "yaml": ("#",),
    "yml": ("#",),
    "toml": ("#",),
    "ini": ("#", ";"),
    "dockerfile": ("#",),
    "makefile": ("#",),
}

_FENCE_OPEN = re.compile(r"^[ \t]*```([^\n`]*)\n", re.MULTILINE)

#: Install-command heads. Deliberately a small closed list of package managers rather
#: than a general shell parser — a missed manager is a recall miss the corpus measures,
#: whereas a general parser would be a second classifier hiding in layer 1.
_INSTALL_COMMAND = re.compile(
    r"^\s*(?:uv\s+(?:add|pip\s+install)|pip3?\s+install|poetry\s+add|pipx\s+install|"
    r"npm\s+(?:i|install)|yarn\s+add|pnpm\s+add|cargo\s+add|go\s+get|gem\s+install|"
    r"apt(?:-get)?\s+install|brew\s+install)\b.*$",
    re.MULTILINE,
)

#: Manifest dependency entries — a quoted requirement inside a dependencies block.
_MANIFEST_DEPENDENCY = re.compile(
    r"\"[A-Za-z0-9][A-Za-z0-9._-]*(?:\[[^\]]*\])?\s*(?:[<>=!~^]=?|@)[^\"]*\"|"
    r"'[A-Za-z0-9][A-Za-z0-9._-]*(?:\[[^\]]*\])?\s*(?:[<>=!~^]=?|@)[^']*'"
)


def _parses(language: str, body: str) -> bool:
    """Whether ``body`` parses as ``language`` with a parser available here.

    Args:
        language: Normalised fence language.
        body: The fenced content.

    Returns:
        ``True`` only when a real parser accepted it. An absent parser is ``False`` —
        no proof, no exemption.
    """
    if not body.strip():
        return False
    try:
        if language == "python":
            ast.parse(body)
            return True
        if language == "json":
            json.loads(body)
            return True
        if language == "toml":
            if tomllib is None:  # pragma: no cover
                return False
            tomllib.loads(body)
            return True
        if language in {"yaml", "yml"}:
            loaded = yaml.safe_load(body)
            # A bare paragraph is valid YAML — it loads as a string. Accepting that
            # would let any prose claim a `yaml` fence and be exempted, which is the
            # same bypass in a different costume.
            return not isinstance(loaded, str)
    except (SyntaxError, ValueError, yaml.YAMLError):
        return False
    return False


def _python_prose_and_dependencies(
    body: str,
) -> tuple[list[tuple[int, int]], list[tuple[int, int]]]:
    """Locate prose channels and dependency declarations inside valid Python.

    Args:
        body: Python source that has already been shown to parse.

    Returns:
        ``(prose_ranges, dependency_ranges)`` as offsets into ``body``.
    """
    lines = body.splitlines(keepends=True)
    line_starts = [0]
    for line in lines[:-1]:
        line_starts.append(line_starts[-1] + len(line))

    def offset(row: int, col: int) -> int:
        return line_starts[row - 1] + col if 0 < row <= len(line_starts) else 0

    prose: list[tuple[int, int]] = []
    dependencies: list[tuple[int, int]] = []

    try:
        tokens = list(tokenize.generate_tokens(io.StringIO(body).readline))
    except (tokenize.TokenError, IndentationError, SyntaxError):  # pragma: no cover
        return prose, dependencies

    for token in tokens:
        if token.type in {tokenize.COMMENT, tokenize.STRING}:
            start = offset(token.start[0], token.start[1])
            end = offset(token.end[0], token.end[1])
            if end > start:
                prose.append((start, end))

    try:
        tree = ast.parse(body)
    except SyntaxError:  # pragma: no cover - caller proved it parses
        return prose, dependencies

    for node in ast.walk(tree):
        if isinstance(node, ast.Import | ast.ImportFrom):
            start = offset(node.lineno, node.col_offset)
            end_lineno = node.end_lineno or node.lineno
            end_col = node.end_col_offset or 0
            dependencies.append((start, offset(end_lineno, end_col)))

    return prose, dependencies


def _scan_comments(body: str, markers: Sequence[str]) -> list[tuple[int, int]]:
    """Find line comments while tracking quote state.

    Args:
        body: The fenced content.
        markers: Line-comment markers for the declared language.

    Returns:
        Comment ranges as offsets into ``body``, marker included.
    """
    found: list[tuple[int, int]] = []
    quote: str | None = None
    index = 0
    length = len(body)

    while index < length:
        char = body[index]
        if char == "\n":
            quote = None
            index += 1
            continue
        if quote is not None:
            if char == "\\":
                index += 2
                continue
            if char == quote:
                quote = None
            index += 1
            continue
        if char in "\"'`":
            quote = char
            index += 1
            continue
        matched = next((m for m in markers if body.startswith(m, index)), None)
        if matched is not None:
            end = body.find("\n", index)
            end = length if end == -1 else end
            found.append((index, end))
            index = end
            continue
        index += 1
    return found


def _subtract(
    body: str, base_start: int, carved: Sequence[tuple[int, int, RegionKind]], language: str | None
) -> Iterator[Region]:
    """Emit regions for a code body with carved-out ranges reclassified.

    Args:
        body: The code body.
        base_start: Offset of ``body`` in the whole output.
        carved: ``(start, end, kind)`` ranges within ``body``.
        language: Declared language, for the emitted regions.

    Yields:
        Regions covering ``body`` exactly, in order.
    """
    ordered = sorted(carved, key=lambda c: c[0])
    cursor = 0
    for start, end, kind in ordered:
        if start < cursor:  # overlapping carve-outs; keep the first
            continue
        if start > cursor:
            yield Region(
                kind=RegionKind.PROVEN_CODE,
                text=body[cursor:start],
                start=base_start + cursor,
                end=base_start + start,
                language=language,
            )
        yield Region(
            kind=kind,
            text=body[start:end],
            start=base_start + start,
            end=base_start + end,
            language=language,
        )
        cursor = end
    if cursor < len(body):
        yield Region(
            kind=RegionKind.PROVEN_CODE,
            text=body[cursor:],
            start=base_start + cursor,
            end=base_start + len(body),
            language=language,
        )


def _dependency_ranges_by_scan(body: str) -> list[tuple[int, int]]:
    """Find install commands and manifest requirement entries textually."""
    ranges = [(m.start(), m.end()) for m in _INSTALL_COMMAND.finditer(body)]
    ranges += [(m.start(), m.end()) for m in _MANIFEST_DEPENDENCY.finditer(body)]
    return ranges


def _fenced_regions(body: str, base_start: int, language: str | None) -> list[Region]:
    """Classify one fenced block's body.

    Args:
        body: Content between the fence markers.
        base_start: Offset of ``body`` in the whole output.
        language: Declared language, lower-cased, or ``None``.

    Returns:
        Regions tiling ``body``.
    """
    normalised = (language or "").strip().lower()
    if normalised in PROSE_LANGUAGES or not normalised:
        return [
            Region(
                kind=RegionKind.CLASSIFY,
                text=body,
                start=base_start,
                end=base_start + len(body),
                language=language,
            )
        ]

    if _parses(normalised, body):
        carved: list[tuple[int, int, RegionKind]] = []
        if normalised == "python":
            prose, dependencies = _python_prose_and_dependencies(body)
            carved += [(s, e, RegionKind.DEPENDENCY) for s, e in dependencies]
            dependency_spans = {(s, e) for s, e in dependencies}
            carved += [
                (s, e, RegionKind.CLASSIFY)
                for s, e in prose
                if not any(ds <= s and e <= de for ds, de in dependency_spans)
            ]
        else:
            markers = _LINE_COMMENT_MARKERS.get(normalised, ())
            carved += [(s, e, RegionKind.CLASSIFY) for s, e in _scan_comments(body, markers)]
        carved += [(s, e, RegionKind.DEPENDENCY) for s, e in _dependency_ranges_by_scan(body)]
        return list(_subtract(body, base_start, carved, language))

    # No proof of code-ness, so the whole block goes to the classifier. There is
    # deliberately no comment scanning on this path: when the entire block is already
    # being classified, carving comments out of it would add nothing and would only
    # invite a scanner mistake to split a line in half.
    #
    # Dependency declarations remain categorical wherever they appear, including here —
    # that is D1's hole in the code exemption, and it does not close just because the
    # surrounding block could not be proved to be code.
    regions: list[Region] = []
    cursor = 0
    for start, end in sorted(_dependency_ranges_by_scan(body)):
        if start < cursor:
            continue
        if start > cursor:
            regions.append(
                Region(
                    kind=RegionKind.CLASSIFY,
                    text=body[cursor:start],
                    start=base_start + cursor,
                    end=base_start + start,
                    language=language,
                )
            )
        regions.append(
            Region(
                kind=RegionKind.DEPENDENCY,
                text=body[start:end],
                start=base_start + start,
                end=base_start + end,
                language=language,
            )
        )
        cursor = end
    if cursor < len(body):
        regions.append(
            Region(
                kind=RegionKind.CLASSIFY,
                text=body[cursor:],
                start=base_start + cursor,
                end=base_start + len(body),
                language=language,
            )
        )
    return regions


def partition_output(output: str) -> tuple[Region, ...]:
    """Partition model output into regions layer 1 can and cannot vouch for.

    The returned regions **tile** ``output`` exactly: concatenating their texts
    reproduces the input. A gap would be text nothing downstream ever examines, which is
    the silent seam the coverage contract exists to close.

    Args:
        output: The model's response text.

    Returns:
        Regions in document order.
    """
    regions: list[Region] = []
    cursor = 0

    for match in _FENCE_OPEN.finditer(output):
        if match.start() < cursor:
            continue
        body_start = match.end()
        closing = re.compile(r"^[ \t]*```[ \t]*$", re.MULTILINE).search(output, body_start)
        if closing is None:
            # An unterminated fence proves nothing. Everything from here on goes to the
            # classifier rather than being swallowed as exempt code.
            break
        if match.start() > cursor:
            regions.append(
                Region(
                    kind=RegionKind.CLASSIFY,
                    text=output[cursor : match.start()],
                    start=cursor,
                    end=match.start(),
                )
            )
        # The fence markers themselves are never claims; they ride with the opening
        # region so the tiling stays exact.
        regions.append(
            Region(
                kind=RegionKind.STRUCTURAL,
                text=output[match.start() : body_start],
                start=match.start(),
                end=body_start,
                language=match.group(1).strip() or None,
            )
        )
        regions.extend(
            _fenced_regions(
                output[body_start : closing.start()],
                body_start,
                match.group(1).strip() or None,
            )
        )
        regions.append(
            Region(
                kind=RegionKind.STRUCTURAL,
                text=output[closing.start() : closing.end()],
                start=closing.start(),
                end=closing.end(),
            )
        )
        cursor = closing.end()

    if cursor < len(output):
        regions.append(
            Region(
                kind=RegionKind.CLASSIFY,
                text=output[cursor:],
                start=cursor,
                end=len(output),
            )
        )
    return tuple(r for r in regions if r.end > r.start)


__all__ = ["PROSE_LANGUAGES", "Region", "RegionKind", "partition_output"]
