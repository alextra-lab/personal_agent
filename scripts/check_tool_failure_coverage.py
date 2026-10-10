r"""Fail when a line that builds a tool failure is not executed by any test (ADR-0156 AC-1).

ADR-0156 AC-1 asks that every line building a failure in the one tool contract is executed by
a test, shown by a coverage report over those lines: one test per code is not enough when
several paths share a code. This script reads a coverage.py JSON report and lists each
failure-building line that the test run did not execute.

A failure-building line is the first line of a statement that calls:

* a :class:`~personal_agent.tools.contract.ToolError` subclass (a tool's typed failure);
* a function whose return type is a ``ToolError`` subclass or ``ToolErrorDetail`` (for example
  ``dispatch_error`` and ``query_telemetry``'s ``_refuse``);
* one of the layer's refusal builders named in :data:`EXTRA_BUILDERS`.

The scanned modules are the executor module of every tool registered with every registration
flag on, plus the fixed layer, dispatch and MCP modules in :data:`FIXED_MODULES`.

Usage:
    uv run python -m pytest -m "not integration" --cov=src/personal_agent \
        --cov-report=json:coverage-tools.json
    uv run python scripts/check_tool_failure_coverage.py coverage-tools.json

FRE-1575.
"""

from __future__ import annotations

import argparse
import ast
import importlib
import inspect
import json
import sys
import typing
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType

REPO_ROOT = Path(__file__).resolve().parents[1]

#: Modules scanned in addition to the registered tools' executor modules.
FIXED_MODULES: tuple[str, ...] = (
    "personal_agent.tools.executor",
    "personal_agent.tools.primitives._governance",
    "personal_agent.orchestrator.tool_dispatch",
    "personal_agent.orchestrator.executor",
    "personal_agent.orchestrator.sub_agent",
    "personal_agent.mcp.client",
    "personal_agent.mcp.gateway",
)

#: Refusal builders whose return type alone does not mark them as failure builders.
EXTRA_BUILDERS: frozenset[str] = frozenset(
    {"_permission_refusal", "_governance_refusal", "undispatched_failure"}
)


@dataclass(frozen=True)
class FailureLine:
    """One statement that builds a tool failure.

    Attributes:
        path: Source file.
        line: First line of the statement.
        builder: The name of the called builder.
    """

    path: Path
    line: int
    builder: str


def _is_builder(obj: object, name: str) -> bool:
    """Report whether a module-level object builds a tool failure.

    Args:
        obj: The object a call's name resolves to.
        name: The name it was called by.

    Returns:
        True for a ``ToolError`` subclass, a function returning one or ``ToolErrorDetail``,
        and the names in :data:`EXTRA_BUILDERS`.
    """
    from personal_agent.tools.contract import ToolError  # noqa: PLC0415
    from personal_agent.tools.types import ToolErrorDetail  # noqa: PLC0415

    if name in EXTRA_BUILDERS:
        return True
    if inspect.isclass(obj):
        return issubclass(obj, ToolError)
    if inspect.isfunction(obj):
        try:
            returned = typing.get_type_hints(obj).get("return")
        except Exception:  # noqa: BLE001 — an unresolvable hint is not a builder
            return False
        return inspect.isclass(returned) and issubclass(returned, (ToolError, ToolErrorDetail))
    return False


def _own_calls(statement: ast.stmt) -> Iterator[ast.Call]:
    """Yield the calls that belong to a statement, not to the statements nested in it."""
    stack: list[ast.AST] = [
        child for child in ast.iter_child_nodes(statement) if not isinstance(child, ast.stmt)
    ]
    while stack:
        node = stack.pop()
        if isinstance(node, ast.Call):
            yield node
        stack.extend(
            child for child in ast.iter_child_nodes(node) if not isinstance(child, ast.stmt)
        )


def failure_lines(module: ModuleType) -> list[FailureLine]:
    """Find every failure-building statement in a module.

    Args:
        module: An imported module.

    Returns:
        One entry per statement that calls a failure builder, in source order.
    """
    path = Path(inspect.getfile(module)).resolve()
    tree = ast.parse(path.read_text())
    namespace = vars(module)
    found: dict[int, FailureLine] = {}
    for statement in ast.walk(tree):
        if not isinstance(statement, ast.stmt):
            continue
        for call in _own_calls(statement):
            if not isinstance(call.func, ast.Name):
                continue
            name = call.func.id
            if name in namespace and _is_builder(namespace[name], name):
                found.setdefault(statement.lineno, FailureLine(path, statement.lineno, name))
    return [found[line] for line in sorted(found)]


def registered_tool_modules() -> list[ModuleType]:
    """Import the executor module of every tool registered with every flag on.

    Returns:
        The modules, without duplicates.
    """
    from personal_agent.config import settings  # noqa: PLC0415
    from personal_agent.tools import register_mvp_tools  # noqa: PLC0415
    from personal_agent.tools.registry import ToolRegistry  # noqa: PLC0415

    settings.primitive_tools_enabled = True
    settings.location_enabled = True
    settings.tool_result_compression_enabled = True
    settings.r2_endpoint_url = settings.r2_endpoint_url or "https://r2.example.invalid"
    settings.r2_access_key_id = settings.r2_access_key_id or "coverage-check"
    settings.r2_secret_access_key = settings.r2_secret_access_key or "coverage-check"
    registry = ToolRegistry()
    register_mvp_tools(registry)
    modules: dict[str, ModuleType] = {}
    for name in registry.list_tool_names():
        entry = registry.get_tool(name)
        module = inspect.getmodule(entry[1]) if entry else None
        if module is not None:
            modules[module.__name__] = module
    return list(modules.values())


def executed_lines(report: Mapping[str, object]) -> dict[Path, frozenset[int]]:
    """Read the executed lines per file from a coverage.py JSON report.

    Args:
        report: The parsed report.

    Returns:
        Executed line numbers per resolved file path.
    """
    files = report.get("files")
    if not isinstance(files, dict):
        raise ValueError("not a coverage.py JSON report: no 'files' object")
    executed: dict[Path, frozenset[int]] = {}
    for name, data in files.items():
        lines = data.get("executed_lines", []) if isinstance(data, dict) else []
        path = Path(name)
        if not path.is_absolute():
            path = REPO_ROOT / path
        executed[path.resolve()] = frozenset(int(line) for line in lines)
    return executed


def uncovered(
    lines: Iterable[FailureLine], executed: Mapping[Path, frozenset[int]]
) -> list[FailureLine]:
    """Return the failure lines that the report did not execute.

    A file absent from the report counts as not executed.

    Args:
        lines: The failure-building lines.
        executed: Executed lines per file.

    Returns:
        The lines not executed.
    """
    return [line for line in lines if line.line not in executed.get(line.path, frozenset())]


def main(argv: list[str] | None = None) -> int:
    """Run the check.

    Args:
        argv: Command-line arguments (the coverage JSON path).

    Returns:
        0 when every failure-building line ran, 1 otherwise.
    """
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("coverage_json", type=Path, help="coverage.py JSON report")
    args = parser.parse_args(argv)

    report = json.loads(args.coverage_json.read_text())
    executed = executed_lines(report)
    modules = {m.__name__: m for m in registered_tool_modules()}
    for name in FIXED_MODULES:
        modules.setdefault(name, importlib.import_module(name))

    all_lines = [line for module in modules.values() for line in failure_lines(module)]
    missing = uncovered(all_lines, executed)
    for line in missing:
        rel = line.path.relative_to(REPO_ROOT) if line.path.is_relative_to(REPO_ROOT) else line.path
        print(f"{rel}:{line.line}: failure built by {line.builder}() is not executed by a test")
    print(
        f"{len(all_lines) - len(missing)}/{len(all_lines)} failure-building lines executed "
        f"across {len(modules)} modules"
    )
    return 1 if missing else 0


if __name__ == "__main__":
    sys.exit(main())
