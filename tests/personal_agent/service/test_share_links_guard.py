"""The agent has no path to a share link (ADR-0153 Amendment 1 L10, V-L10 parts 2 and 3 / FRE-1782).

* Part 2: the registered tool catalog, with every optional tool switched on, holds no tool whose
  name or executor creates, lists or revokes a link. No skill names the share-link endpoints.
* Part 3: only the share-links router module writes ``artifact_share_links``, and no module under
  ``tools/``, ``orchestrator/``, ``mcp/`` or the repo's ``skills/`` imports it. A seeded violation
  (a module under ``tools/`` that inserts into the table) makes the scanner fail.

Part 1 (a real ``bash`` call through ``ToolExecutionLayer`` gets 401) is in
``test_share_links_agent_path.py``, because it needs the test Postgres.
"""

from __future__ import annotations

import ast
import inspect
import re
from pathlib import Path

import pytest

from personal_agent.config import settings
from personal_agent.tools import register_mvp_tools
from personal_agent.tools.registry import ToolRegistry

_REPO = Path(__file__).resolve().parents[3]
_SRC = _REPO / "src" / "personal_agent"
_ROUTER_MODULE = "personal_agent.service.share_links_router"
_ROUTER_FILE = Path("service") / "share_links_router.py"
_AGENT_PACKAGES = ("tools", "orchestrator", "mcp")
_WRITE_RE = re.compile(
    r"\b(?:INSERT\s+INTO|UPDATE|DELETE\s+FROM|TRUNCATE(?:\s+TABLE)?)\s+artifact_share_links\b",
    re.IGNORECASE,
)
_LINK_MARKERS = ("share-links", "share_links", "share_link", "artifact_share_links")


def table_writers(src_root: Path) -> list[Path]:
    """Return every Python file under ``src_root`` that writes ``artifact_share_links``.

    Args:
        src_root: The package root to scan.

    Returns:
        The writing files, relative to ``src_root``, sorted.
    """
    return sorted(
        path.relative_to(src_root)
        for path in src_root.rglob("*.py")
        if _WRITE_RE.search(path.read_text(encoding="utf-8"))
    )


def router_importers(roots: list[Path]) -> list[Path]:
    """Return every Python file under ``roots`` that imports the share-links router module.

    Args:
        roots: Directories to scan.

    Returns:
        The importing files, sorted.
    """
    found: list[Path] = []
    for root in roots:
        for path in root.rglob("*.py") if root.exists() else []:
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                names: list[str] = []
                if isinstance(node, ast.Import):
                    names = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom) and node.module:
                    names = [node.module] + [f"{node.module}.{a.name}" for a in node.names]
                if any(name.startswith(_ROUTER_MODULE) for name in names):
                    found.append(path)
                    break
    return sorted(found)


def _agent_roots(src_root: Path, repo_root: Path) -> list[Path]:
    return [src_root / package for package in _AGENT_PACKAGES] + [repo_root / "skills"]


# --- Part 3: one writer, no agent importer --------------------------------------------------


def test_only_the_share_links_router_writes_the_table() -> None:
    assert table_writers(_SRC) == [_ROUTER_FILE]


def test_no_agent_module_imports_the_share_links_router() -> None:
    assert router_importers(_agent_roots(_SRC, _REPO)) == []


def test_seeded_a_tools_module_that_inserts_fails_the_writer_scan(tmp_path: Path) -> None:
    (tmp_path / "service").mkdir()
    (tmp_path / "service" / "share_links_router.py").write_text(
        'SQL = "INSERT INTO artifact_share_links (id) VALUES (1)"\n'
    )
    (tmp_path / "tools").mkdir()
    (tmp_path / "tools" / "leak.py").write_text(
        'SQL = """\n    insert into   artifact_share_links (id)\n    values (1)\n"""\n'
    )
    assert table_writers(tmp_path) == [_ROUTER_FILE, Path("tools") / "leak.py"]


@pytest.mark.parametrize(
    "line",
    [
        "from personal_agent.service.share_links_router import create_share_link",
        "import personal_agent.service.share_links_router",
        "from personal_agent.service import share_links_router",
    ],
)
def test_seeded_an_import_from_a_tools_module_fails_the_import_scan(
    tmp_path: Path, line: str
) -> None:
    (tmp_path / "tools").mkdir()
    (tmp_path / "tools" / "leak.py").write_text(f"{line}\n")
    assert router_importers(_agent_roots(tmp_path, tmp_path)) == [tmp_path / "tools" / "leak.py"]


# --- Part 2: the catalog ---------------------------------------------------------------------


@pytest.fixture
def every_optional_tool(monkeypatch: pytest.MonkeyPatch) -> None:
    """Switch on every flag that gates a native tool, so the catalog is the largest one."""
    for name, value in {
        "r2_endpoint_url": "https://r2.example.invalid",
        "r2_access_key_id": "fre1782-id",
        "r2_secret_access_key": "fre1782-secret",
        "tool_result_compression_enabled": True,
        "dev_agent_supervisor_enabled": True,
        "location_enabled": True,
        "primitive_tools_enabled": True,
    }.items():
        monkeypatch.setattr(settings, name, value)


@pytest.mark.usefixtures("every_optional_tool")
def test_no_registered_tool_creates_lists_or_revokes_a_link() -> None:
    registry = ToolRegistry()
    register_mvp_tools(registry)
    names = registry.list_tool_names()
    assert "bash" in names and "artifact_write" in names, "the flags did not take effect"

    for name in names:
        assert "share" not in name.lower(), name
        entry = registry.get_tool(name)
        assert entry is not None
        _definition, executor = entry
        module = inspect.getmodule(executor)
        assert module is not None and module.__name__ != _ROUTER_MODULE
        source = inspect.getsource(module)
        assert not any(marker in source for marker in _LINK_MARKERS), (name, module.__name__)


def test_no_skill_names_the_share_link_endpoints_or_table() -> None:
    hits = [
        path
        for path in (_REPO / "skills").rglob("*")
        if path.is_file()
        and path.suffix in {".md", ".py", ".sh", ".yaml", ".yml", ".json"}
        and any(marker in path.read_text(encoding="utf-8") for marker in _LINK_MARKERS)
    ]
    assert hits == []


def test_the_default_mcp_server_list_adds_no_server() -> None:
    """The MCP gateway exposes external servers. The default adds none, and none holds a JWT."""
    field = type(settings).model_fields["mcp_gateway_enabled_servers"]
    assert field.get_default(call_default_factory=True) == []
