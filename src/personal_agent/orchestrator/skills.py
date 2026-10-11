"""Skill loader, catalog and validator (ADR-0156 D1, D2, D3).

Each agent skill is a folder ``docs/skills/<name>/`` in the Agent Skills format (S1,
https://agentskills.io/specification): a ``SKILL.md`` whose frontmatter holds ``name``,
``description`` and an optional ``metadata`` map, with optional ``scripts/``, ``references/``
and ``assets/`` beside it.

The model sees one catalog line per skill (name and description) in the cached system prompt
and loads a body with ``read_skill`` (D2). Nothing in the user's message selects a skill: the
keyword routing, the hint loop and the separate routing model are gone (FRE-1577).

The cache is reloaded when a ``SKILL.md`` file is added, removed or modified (mtime-based).
"""

from __future__ import annotations

import os
import re
import stat
from collections.abc import Collection
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, get_args

import structlog
import yaml  # type: ignore[import-untyped]

log = structlog.get_logger(__name__)

SKILLS_DIR = Path(__file__).resolve().parents[3] / "docs" / "skills"
SKILL_FILE = "SKILL.md"
_CHARS_PER_TOKEN = 4  # the repo's token estimate
_MAX_FILE_BYTES = 1_000_000


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SkillDoc:
    """One skill folder, parsed.

    Attributes:
        name: The skill name, equal to the folder name.
        description: What the skill does and when to use it (the catalog text).
        requires_tools: ``metadata.requires-tools``, split on commas (D4).
        body: The ``SKILL.md`` text after the frontmatter.
        folder: The skill folder.
    """

    name: str
    description: str
    requires_tools: tuple[str, ...]
    body: str
    folder: Path


@dataclass(frozen=True)
class SkillFile:
    """One file of a skill folder, as listed: its relative path and its inode."""

    path: str
    device: int
    inode: int


@dataclass
class _SkillCache:
    """Loaded skills by name, with the mtimes of their ``SKILL.md`` files."""

    docs: dict[str, SkillDoc]
    mtimes: dict[Path, float]


# ---------------------------------------------------------------------------
# Frontmatter parsing
# ---------------------------------------------------------------------------


def _split_frontmatter(content: str) -> tuple[dict[str, object], str] | None:
    """Split YAML frontmatter from a body, or return None when there is no frontmatter block."""
    if not content.startswith("---\n"):
        return None
    end = content.find("\n---\n", 4)
    if end == -1:
        return None
    loaded = yaml.safe_load(content[4:end])
    fm: dict[str, object] = loaded if isinstance(loaded, dict) else {}
    return fm, content[end + 5 :].strip()


def _parse_frontmatter(path: Path) -> tuple[dict[str, object], str]:
    """Split YAML frontmatter and body from a markdown file.

    Args:
        path: Path to the markdown file.

    Returns:
        Tuple of (frontmatter_dict, body_text). Returns ({}, full_text) when the file has no
        frontmatter block.
    """
    content = path.read_text(encoding="utf-8")
    split = _split_frontmatter(content)
    return split if split is not None else ({}, content.strip())


def _requires_tools(fm: dict[str, object]) -> tuple[str, ...]:
    metadata = fm.get("metadata")
    if not isinstance(metadata, dict):
        return ()
    raw = metadata.get("requires-tools")
    if not isinstance(raw, str):
        return ()
    return tuple(t.strip() for t in raw.split(",") if t.strip())


# ---------------------------------------------------------------------------
# Cache management
# ---------------------------------------------------------------------------


def _skill_files_on_disk(skills_dir: Path) -> list[Path]:
    """Return every ``<name>/SKILL.md``, except one whose folder or file is a symlink.

    A symlinked folder or ``SKILL.md`` would make ``read_skill`` return text from outside the
    skill folders (ADR-0156 AC-10), so it is not a skill.
    """
    found = []
    for path in sorted(skills_dir.glob(f"*/{SKILL_FILE}")):
        if path.parent.is_symlink() or path.is_symlink():
            log.warning("skill_doc_symlink_skipped", file=str(path))
            continue
        found.append(path)
    return found


def _load_all_skills(skills_dir: Path) -> _SkillCache:
    """Parse every ``<name>/SKILL.md`` under *skills_dir*.

    A file without a ``name``, with a ``name`` that is not its folder name, or without a
    ``description`` is skipped with a warning. The CI validator rejects all three.

    Args:
        skills_dir: The folder that holds one sub-folder per skill.

    Returns:
        The loaded skills with the mtimes of their files.
    """
    docs: dict[str, SkillDoc] = {}
    mtimes: dict[Path, float] = {}
    for path in _skill_files_on_disk(skills_dir):
        mtimes[path] = path.stat().st_mtime
        try:
            fm, body = _parse_frontmatter(path)
        except (OSError, UnicodeDecodeError) as exc:
            log.warning("skill_doc_unreadable", file=str(path), error=str(exc))
            continue
        except yaml.YAMLError as exc:
            log.warning("skill_doc_parse_error", file=str(path), error=str(exc))
            continue
        name = fm.get("name")
        description = fm.get("description")
        if not isinstance(name, str) or name != path.parent.name or not description:
            log.warning("skill_doc_invalid", file=str(path), name=name)
            continue
        docs[name] = SkillDoc(
            name=name,
            description=" ".join(str(description).split()),
            requires_tools=_requires_tools(fm),
            body=body,
            folder=path.parent,
        )
    return _SkillCache(docs=docs, mtimes=mtimes)


_cache: _SkillCache | None = None


def _needs_reload(skills_dir: Path) -> bool:
    """Return True when the cache is absent or any ``SKILL.md`` file has changed."""
    if _cache is None:
        return True
    if set(_skill_files_on_disk(skills_dir)) != set(_cache.mtimes):
        return True
    return any(
        not path.exists() or path.stat().st_mtime != mtime for path, mtime in _cache.mtimes.items()
    )


def _get_cache() -> _SkillCache:
    """Return the skill cache, reloading from disk when stale."""
    global _cache
    if _cache is None or _needs_reload(SKILLS_DIR):
        _cache = _load_all_skills(SKILLS_DIR)
    return _cache


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def get_all_skills() -> dict[str, SkillDoc]:
    """Return every loaded agent skill keyed by name.

    Returns:
        Mapping of skill name to SkillDoc.
    """
    return dict(_get_cache().docs)


def skill_names() -> tuple[str, ...]:
    """Return the agent skill names, sorted: the enum of ``read_skill``'s ``name`` (D2.2).

    Returns:
        The sorted skill names.
    """
    return tuple(sorted(_get_cache().docs))


_CATALOG_HEADER = (
    "## Skills\n\n"
    "Each skill below holds the instructions for one kind of task. When a task matches a "
    'skill\'s description, call `read_skill(name="<name>")` to load its instructions before '
    "you start the task, and follow them. A task that matches no description needs no skill.\n\n"
)


def skills_for_tools(held: Collection[str]) -> dict[str, SkillDoc]:
    """Return the skills whose ``metadata.requires-tools`` an agent holds (ADR-0156 D9.1).

    A skill that requires no tool is held by every agent.

    Args:
        held: The tool names the agent holds.

    Returns:
        Mapping of skill name to SkillDoc, for the visible skills only.
    """
    tools = set(held)
    return {
        name: doc for name, doc in _get_cache().docs.items() if set(doc.requires_tools) <= tools
    }


def assemble_skill_catalog(held: Collection[str] | None = None) -> str:
    """Return the skill catalog: one line per skill, name and description only (D2.1).

    The text depends only on the skill files and on ``held``, never on the request, so it stays
    in the cached prefix of the system prompt.

    Args:
        held: The tools a worker holds. Only the skills whose tools it holds are listed, and a
            filtered skill is not mentioned at all (D9.1). ``None`` lists every skill (the
            primary).

    Returns:
        The catalog block, or an empty string when no skill is listed.
    """
    docs = _get_cache().docs if held is None else skills_for_tools(held)
    if not docs:
        return ""
    lines = "".join(f"- {docs[name].name}: {docs[name].description}\n" for name in sorted(docs))
    return _CATALOG_HEADER + lines


# ---------------------------------------------------------------------------
# Files of a skill folder (D3)
# ---------------------------------------------------------------------------


class SkillFileRefused(ValueError):
    """A requested skill file is not one listed file of the skill folder (D3.2)."""


def list_skill_files(folder: Path) -> tuple[SkillFile, ...]:
    """List the regular files of a skill folder, without following symlinks.

    ``SKILL.md`` is not listed (``read_skill`` without a path returns it). A symlink, at any
    depth, is not listed and its target is not entered.

    Args:
        folder: The skill folder.

    Returns:
        The files, sorted by relative path (POSIX separators).
    """
    files: list[SkillFile] = []
    for dirpath, dirnames, filenames in os.walk(folder, followlinks=False):
        base = Path(dirpath)
        dirnames[:] = sorted(d for d in dirnames if not (base / d).is_symlink())
        for filename in filenames:
            info = os.lstat(base / filename)
            if not stat.S_ISREG(info.st_mode):
                continue
            rel = (base / filename).relative_to(folder).as_posix()
            if rel == SKILL_FILE or "__pycache__" in rel.split("/"):
                continue
            files.append(SkillFile(path=rel, device=info.st_dev, inode=info.st_ino))
    return tuple(sorted(files, key=lambda f: f.path))


def read_skill_file(folder: Path, path: str) -> str:
    """Return one listed file of a skill folder as text (D3.2).

    The path must equal a listed path exactly. The file is then opened one path part at a time
    relative to the previous directory, each with ``O_NOFOLLOW``, so no part can be a symlink,
    and the opened file must be a regular file with one link and the listed inode. A file
    swapped after the listing is refused.

    Args:
        folder: The skill folder.
        path: A relative path, as listed by :func:`list_skill_files`.

    Returns:
        The file content.

    Raises:
        SkillFileRefused: The path is not listed, the file changed, it is too large, or it is
            not UTF-8 text.
    """
    listed = {f.path: f for f in list_skill_files(folder)}
    entry = listed.get(path)
    if entry is None:
        raise SkillFileRefused(f"'{path}' is not a file of this skill.")
    parts = path.split("/")
    nofollow = os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    opened: list[int] = []
    try:
        fd = os.open(folder, nofollow | os.O_DIRECTORY)
        opened.append(fd)
        for part in parts[:-1]:
            fd = os.open(part, nofollow | os.O_DIRECTORY, dir_fd=fd)
            opened.append(fd)
        fd = os.open(parts[-1], nofollow, dir_fd=fd)
        opened.append(fd)
        info = os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_nlink != 1
            or (info.st_dev, info.st_ino) != (entry.device, entry.inode)
        ):
            raise SkillFileRefused(f"'{path}' changed after it was listed.")
        if info.st_size > _MAX_FILE_BYTES:
            raise SkillFileRefused(f"'{path}' is larger than {_MAX_FILE_BYTES:,} bytes.")
        chunks: list[bytes] = []
        while chunk := os.read(fd, 65536):
            chunks.append(chunk)
        data = b"".join(chunks)
    except OSError as exc:
        raise SkillFileRefused(f"'{path}' cannot be opened: {exc.strerror}.") from exc
    finally:
        for descriptor in reversed(opened):
            os.close(descriptor)
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SkillFileRefused(f"'{path}' is not UTF-8 text.") from exc


# ---------------------------------------------------------------------------
# The S1 validator (D1.3, D5.1, D5.2) — run by tests/docs/test_skill_validator.py
# ---------------------------------------------------------------------------

SkillKind = Literal["agent", "external"]
SkillRule = Literal[
    "frontmatter",
    "name",
    "description",
    "metadata",
    "body_lines",
    "body_tokens",
    "file_refs",
    "sections",
    "evals",
]
SKILL_RULES: tuple[SkillRule, ...] = get_args(SkillRule)


@dataclass(frozen=True)
class SkillViolation:
    """One broken rule of one skill folder."""

    rule: SkillRule
    detail: str


_ALLOWED_FIELDS = frozenset({"name", "description", "metadata"})
_NAME = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
# S3: the description is in the third person.
_PERSON = re.compile(r"\bI\b(?!/O)|\b(?i:me|my|you|your)\b")  # "I/O" is not a pronoun
_MAX_BODY_LINES = 500  # the body is under 500 lines
_MAX_BODY_TOKENS = 5000  # and under 5,000 tokens
_LINK = re.compile(r"\]\(([^)\s]+)\)")
_SKILL_PATH = re.compile(r"(?<![\w./-])(?:\./)?((?:scripts|references|assets)/[\w./-]*[\w-])")
_SUCCESS = re.compile(r"^##\s+Success\b", re.IGNORECASE | re.MULTILINE)
_RECOVERY = re.compile(r"^##\s+(?:Failure\s+)?Recovery\b", re.IGNORECASE | re.MULTILINE)
_MIN_SCENARIOS = 3


def _named_files(body: str) -> set[str]:
    """Return the relative files a body names: relative links and S1 folder paths."""
    named = {m.group(1) for m in _SKILL_PATH.finditer(body)}
    for target in _LINK.findall(body):
        target = target.split("#", 1)[0]
        if target and "://" not in target and not target.startswith(("/", "mailto:")):
            named.add(target.removeprefix("./"))
    return named


def _check_frontmatter(fm: dict[str, object], folder: Path) -> list[SkillViolation]:
    found: list[SkillViolation] = []
    extra = sorted(set(fm) - _ALLOWED_FIELDS)
    if extra:
        found.append(SkillViolation("frontmatter", f"fields not allowed: {extra}"))
    name = fm.get("name")
    if not isinstance(name, str) or not 1 <= len(name) <= 64 or not _NAME.match(name):
        found.append(SkillViolation("name", f"name {name!r} is not 1-64 of [a-z0-9-]"))
    elif name != folder.name:
        found.append(SkillViolation("name", f"name {name!r} is not the folder {folder.name!r}"))
    description = fm.get("description")
    if not isinstance(description, str) or not 1 <= len(description) <= 1024:
        size = len(description) if isinstance(description, str) else 0
        found.append(SkillViolation("description", f"description has {size} characters"))
    elif _PERSON.search(description):
        found.append(SkillViolation("description", "description is not in the third person"))
    metadata = fm.get("metadata", {})
    if not isinstance(metadata, dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in metadata.items()
    ):
        found.append(SkillViolation("metadata", "metadata is not a map of string to string"))
    return found


def _check_body(body: str, folder: Path) -> list[SkillViolation]:
    found: list[SkillViolation] = []
    lines = body.count("\n") + 1
    if lines >= _MAX_BODY_LINES:
        found.append(SkillViolation("body_lines", f"body has {lines} lines"))
    tokens = len(body) // _CHARS_PER_TOKEN
    if tokens >= _MAX_BODY_TOKENS:
        found.append(SkillViolation("body_tokens", f"body has about {tokens} tokens"))
    for named in sorted(_named_files(body)):
        parts = Path(named).parts
        target = folder / named
        if ".." in parts or len(parts) > 2 or not target.is_file() or target.is_symlink():
            found.append(
                SkillViolation(
                    "file_refs", f"'{named}' is not a file one level below {folder.name}/"
                )
            )
    return found


def _check_agent_parts(body: str, folder: Path) -> list[SkillViolation]:
    found: list[SkillViolation] = []
    missing = [
        n for n, rx in (("success", _SUCCESS), ("recovery", _RECOVERY)) if not rx.search(body)
    ]
    if missing:
        found.append(SkillViolation("sections", f"no {' or '.join(missing)} section"))
    evals = folder / "evals.yaml"
    scenarios: object = None
    if evals.is_file():
        try:
            loaded = yaml.safe_load(evals.read_text(encoding="utf-8"))
        except yaml.YAMLError:
            loaded = None
        scenarios = loaded.get("scenarios") if isinstance(loaded, dict) else None
    count = len(scenarios) if isinstance(scenarios, list) else 0
    if count < _MIN_SCENARIOS:
        found.append(SkillViolation("evals", f"evals.yaml has {count} scenarios"))
    return found


def validate_skill(folder: Path, kind: SkillKind) -> tuple[SkillViolation, ...]:
    """Check one skill folder against the S1 rules of ADR-0156 D1.3.

    An agent skill must also hold a success and a recovery section (D5.1) and an
    ``evals.yaml`` with at least 3 scenarios (D5.2).

    Args:
        folder: The skill folder.
        kind: ``"agent"`` for ``docs/skills/``, ``"external"`` for ``docs/external-skills/``.

    Returns:
        Every broken rule; empty when the skill conforms.
    """
    skill_md = folder / SKILL_FILE
    if not skill_md.is_file():
        return (SkillViolation("frontmatter", f"{folder.name}/ has no {SKILL_FILE}"),)
    split = _split_frontmatter(skill_md.read_text(encoding="utf-8"))
    if split is None:
        return (SkillViolation("frontmatter", f"{SKILL_FILE} has no frontmatter"),)
    fm, body = split
    found = _check_frontmatter(fm, folder) + _check_body(body, folder)
    if kind == "agent":
        found += _check_agent_parts(body, folder)
    return tuple(found)
