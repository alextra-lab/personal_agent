"""The gateway prepares the child workspace at startup (FRE-1732, FRE-1734).

``prepare_workspace`` gives the ``bash`` child its workspace. If the call leaves ``lifespan``, the
service starts and the child cannot write. A unit test of the function cannot see that, so this
test reads the source. The live check is in the ticket's handoff.
"""

from __future__ import annotations

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
_APP = REPO_ROOT / "src" / "personal_agent" / "service" / "app.py"


def _lifespan_prepares_the_workspace(source: str) -> bool:
    """Return whether ``lifespan`` passes ``prepare_workspace`` to ``asyncio.to_thread``."""
    lifespan = next(
        node
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "lifespan"
    )
    return any(
        isinstance(call.func, ast.Attribute)
        and call.func.attr == "to_thread"
        and any(isinstance(arg, ast.Name) and arg.id == "prepare_workspace" for arg in call.args)
        for call in ast.walk(lifespan)
        if isinstance(call, ast.Call)
    )


def test_lifespan_prepares_the_child_workspace() -> None:
    """The service source holds the call."""
    assert _lifespan_prepares_the_workspace(_APP.read_text())


def test_the_check_fails_on_a_copy_without_the_call() -> None:
    """Seeded negative: the check fails when the call leaves the source."""
    source = _APP.read_text()
    assert "await asyncio.to_thread(prepare_workspace)" in source

    without_call = source.replace("await asyncio.to_thread(prepare_workspace)", "pass")

    assert not _lifespan_prepares_the_workspace(without_call)
