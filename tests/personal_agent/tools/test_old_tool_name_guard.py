"""ADR-0156 A5.10 (FRE-1596 AC-5): the old tool name is gone from live code.

The one telemetry tool of FRE-1564 was split into ``search_logs`` and ``measure_model_latency``.
Its old name may remain only where it names history:

- the FRE-1517 audit, which reads old events (the tool name in a call row and the name of the
  refusal event) that Elasticsearch still holds;
- the FRE-1594 spike, which reads a stored capture of a request that held the old tool;
- documents under ``docs/architecture_decisions/``, ``docs/research/`` and
  ``docs/superpowers/plans/``.

The guard allows an exact line, not a whole file: a second use of the old name in an allowed file
fails. The scan covers the scope that the ticket names. The guard builds the old name from two
parts, so its own text does not match.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from pathlib import Path

import pytest

_OLD = "query_" + "telemetry"
_REPO = Path(__file__).resolve().parents[3]

_ROOTS = ("src", "config", "scripts", "tests", "skills", "docs")
_ROOT_FILES = (".env.example",)
_ROOT_GLOBS = ("Dockerfile*", "docker-compose*")
_HISTORY = (
    "docs/architecture_decisions/",
    "docs/research/",
    "docs/superpowers/plans/",
)
_SUFFIXES = frozenset(
    {".py", ".md", ".yaml", ".yml", ".json", ".toml", ".txt", ".sh", ".example", ".sql", ""}
)
_SKIP_PARTS = frozenset({"__pycache__", "node_modules", ".venv", ".git"})

# file → the exact stripped lines that may hold the old name. Each is history, not a live tool.
ALLOWED: Mapping[str, frozenset[str]] = {
    # The FRE-1517 audit reads old events.
    "scripts/eval/fre1517/tool_audit/buckets.py": frozenset({f'LEGACY_TOOL = "{_OLD}"'}),
    "scripts/eval/fre1517/tool_audit/calls.py": frozenset({f'LEGACY_REFUSED = "{_OLD}_refused"'}),
    # The FRE-1594 spike reads a stored capture of a request that held the old tool.
    "scripts/eval/fre1594/reveal.py": frozenset({f'REMOVED_TOOL = "{_OLD}"'}),
}
# Historical documents: they describe a run on the old tool, or hold the text of the stored
# capture that the spike transforms. The exact lines are listed, with <OLD> for the old name, so a
# new use of it in one of these files fails the guard too.
_HISTORICAL_LINES: Mapping[str, tuple[str, ...]] = {
    "scripts/eval/fre1594/README.md": (
        "| 1 | The base request: the captured primary request, without `<OLD>` (the tool and every mention of it), with the `log-search` and `model-latency` catalog lines (`scenarios.yaml`), the old skill names renamed everywhere (the `read_skill` enum and examples, the cross-references), and the prompt. The build refuses a body that still names an old skill or `<OLD>`. | 1: a `read_skill` call names the prompt's skill |",
        "capture must hold `<OLD>`, and its system prompt must hold the `query-elasticsearch` and",
    ),
    "scripts/eval/fre1594/scenarios.yaml": (
        "# `system_edits` removes the two other mentions of `<OLD>` from the captured system prompt",
        '- old: "- read_only: <OLD>, read, read_skill"',
        '- old: "Starts with the <OLD> tool for event data and uses curl for the budget snapshot and the Captain\'s Log, which the tool does not read."',
    ),
    "scripts/eval/fre1517/spot_retest_stage4.md": (
        "- FRE-1564: workers get read-only tools, `<OLD>` for `general`, and a researcher gets no",
        "- PASS: delivered, and the latency figures come from our own telemetry, through `<OLD>` by a",
    ),
}
ALLOWED = {
    **ALLOWED,
    **{
        path: frozenset(line.replace("<OLD>", _OLD) for line in lines)
        for path, lines in _HISTORICAL_LINES.items()
    },
}


def _files(repo: Path) -> Iterator[Path]:
    for root in _ROOTS:
        base = repo / root
        if base.is_dir():
            yield from (p for p in base.rglob("*") if p.is_file())
    for name in _ROOT_FILES:
        if (repo / name).is_file():
            yield repo / name
    for pattern in _ROOT_GLOBS:
        yield from (p for p in repo.glob(pattern) if p.is_file())


def offences(repo: Path, allowed: Mapping[str, frozenset[str]] = ALLOWED) -> list[str]:
    """Return ``path:line`` for each use of the old name that is not allowed.

    Args:
        repo: The repository root.
        allowed: File → exact stripped lines that may hold the old name.

    Returns:
        One ``path:line: text`` entry per offending line.
    """
    found: list[str] = []
    for path in _files(repo):
        rel = path.relative_to(repo).as_posix()
        if rel.startswith(_HISTORY):
            continue
        if _SKIP_PARTS & set(path.parts):
            continue
        if path.parent != repo and path.suffix not in _SUFFIXES:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        if _OLD not in text:
            continue
        for number, line in enumerate(text.splitlines(), start=1):
            if _OLD in line and line.strip() not in allowed.get(rel, frozenset()):
                found.append(f"{rel}:{number}: {line.strip()[:100]}")
    return found


def test_the_old_tool_name_is_only_on_the_named_history_lines() -> None:
    assert offences(_REPO) == []


def test_a_seeded_live_use_fails_the_guard(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "tool_list.py").write_text(f'TOOLS = ["{_OLD}"]\n', encoding="utf-8")
    assert offences(tmp_path) == [f'src/tool_list.py:1: TOOLS = ["{_OLD}"]']


def test_a_second_use_in_an_allowed_file_fails_the_guard(tmp_path: Path) -> None:
    path = tmp_path / "scripts" / "eval" / "fre1594"
    path.mkdir(parents=True)
    (path / "reveal.py").write_text(f'REMOVED_TOOL = "{_OLD}"\nLIVE = "{_OLD}"\n', encoding="utf-8")
    found = offences(tmp_path)
    assert found == [f'scripts/eval/fre1594/reveal.py:2: LIVE = "{_OLD}"']


def test_the_documents_that_the_ticket_exempts_are_not_scanned(tmp_path: Path) -> None:
    for folder in ("docs/architecture_decisions", "docs/research", "docs/superpowers/plans"):
        directory = tmp_path / folder
        directory.mkdir(parents=True)
        (directory / "note.md").write_text(f"The tool {_OLD} was split.\n", encoding="utf-8")
    assert offences(tmp_path) == []


def test_a_live_document_outside_the_history_folders_is_scanned(tmp_path: Path) -> None:
    guides = tmp_path / "docs" / "guides"
    guides.mkdir(parents=True)
    (guides / "TELEMETRY.md").write_text(f"Call {_OLD} once.\n", encoding="utf-8")
    assert [o.split(":")[0] for o in offences(tmp_path)] == ["docs/guides/TELEMETRY.md"]


@pytest.mark.parametrize("name", [".env.example", "docker-compose.eval.yml", "Dockerfile.gateway"])
def test_a_root_file_of_the_scope_is_scanned(tmp_path: Path, name: str) -> None:
    (tmp_path / name).write_text(f"# reads {_OLD}\n", encoding="utf-8")
    assert [o.split(":")[0] for o in offences(tmp_path)] == [name]
