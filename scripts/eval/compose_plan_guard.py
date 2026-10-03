"""FRE-1542 — refuse an eval compose plan that includes a production service.

`make eval-infra-up` and `make eval-infra-down` run `docker compose ... --dry-run` first and
pipe its output here. The compose plan lists one `Container <name> <action>` line per
container it will touch. Every container that `docker-compose.eval.yml` does not define is
production's, so any such line is a refusal.

Why a guard and not only `--no-deps`: on 2026-10-03 an eval bring-up recreated production's
`cloud-sim-searxng` because `depends_on` pulls dependencies into the scope of an `up`, and
FRE-1344's `required: false` did not stop it. `--no-deps` removes that path. This guard
fails before any real command runs if the path comes back by another route.
"""

from __future__ import annotations

import argparse
import re
import sys
from collections.abc import Iterable, Sequence
from pathlib import Path

import yaml

_CONTAINER_LINE = re.compile(r"^\s*Container\s+(\S+)\s+\S+\s*$")

#: Any line that names a container at all. A line that matches this but not `_CONTAINER_LINE`
#: has a shape the guard cannot read, so it must refuse rather than skip it.
_MENTIONS_CONTAINER = re.compile(r"\bContainer\b")

#: Compose names the replacement of a recreated container `<12 hex>_<name>` while it runs.
_RECREATE_PREFIX = re.compile(r"^[0-9a-f]{12}_")

_DEFAULT_EVAL_FILE = "docker-compose.eval.yml"


class PlanGuardError(Exception):
    """The compose plan is unsafe, or is not a compose plan at all."""


def repo_root() -> Path:
    """Resolve the repo root without importing `personal_agent.config`.

    Returns:
        The directory that holds `docker-compose.eval.yml`.
    """
    return Path(__file__).resolve().parent.parent.parent


def eval_container_names(eval_file: Path) -> frozenset[str]:
    """Return the container names that `eval_file` defines.

    Args:
        eval_file: Path to `docker-compose.eval.yml`.

    Returns:
        The `container_name` of every service in the file. These are the only containers an
        eval bring-up may touch.

    Raises:
        PlanGuardError: A service in the file has no `container_name`, so the guard cannot
            tell its container from production's.
    """
    document = yaml.safe_load(eval_file.read_text())
    names: set[str] = set()
    for service, definition in document["services"].items():
        name = definition.get("container_name")
        if not name:
            raise PlanGuardError(
                f"service {service!r} in {eval_file.name} has no container_name; "
                "the plan guard cannot tell it from a production container"
            )
        names.add(name)
    return frozenset(names)


def production_containers_in(plan: str, eval_containers: Iterable[str]) -> list[str]:
    """List the non-eval containers that a compose plan mentions.

    Args:
        plan: Output of `docker compose ... --dry-run`.
        eval_containers: Container names that the plan may touch.

    Returns:
        Each non-eval container name once, in order of first appearance. Any action counts,
        `Running` and `Waiting` included: with `--no-deps` no production service is in scope.
    """
    allowed = set(eval_containers)
    found: list[str] = []
    for line in plan.splitlines():
        match = _CONTAINER_LINE.match(line)
        if match is None:
            continue
        name = _RECREATE_PREFIX.sub("", match.group(1))
        if name not in allowed and name not in found:
            found.append(name)
    return found


def check_plan(plan: str, eval_containers: Iterable[str], *, allow_empty: bool = False) -> None:
    """Raise if the plan is empty or touches a production container.

    Args:
        plan: Output of `docker compose ... --dry-run`.
        eval_containers: Container names that the plan may touch.
        allow_empty: Accept a plan with no container line. A `down` of a stack that is
            already stopped has none. An `up` always has some, so leave this off for `up`.

    Raises:
        PlanGuardError: The plan has no container line, has a container line the guard cannot
            read, or names a production container.
    """
    if not allow_empty and not any(_CONTAINER_LINE.match(line) for line in plan.splitlines()):
        raise PlanGuardError(
            "the dry-run printed no container lines; it failed or its output changed, "
            "so the plan cannot be checked"
        )
    unreadable = [
        line.strip()
        for line in plan.splitlines()
        if _MENTIONS_CONTAINER.search(line) and not _CONTAINER_LINE.match(line)
    ]
    if unreadable:
        raise PlanGuardError(
            "the dry-run printed a container line in a shape the guard cannot read: "
            f"{unreadable[0]!r}. The compose output format may have changed."
        )
    production = production_containers_in(plan, eval_containers)
    if production:
        raise PlanGuardError(
            "the eval plan touches production container(s): "
            + ", ".join(production)
            + ". No command was run. Name only eval services and pass --no-deps."
        )


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point: read a dry-run plan on stdin, exit 1 if it is unsafe.

    Args:
        argv: Command-line arguments. Defaults to `sys.argv[1:]`.

    Returns:
        0 when the plan touches only eval containers, 1 otherwise.
    """
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--eval-file",
        type=Path,
        default=repo_root() / _DEFAULT_EVAL_FILE,
        help="Compose file whose services are the eval containers.",
    )
    parser.add_argument(
        "--allow-empty-plan",
        action="store_true",
        help="Accept a plan with no container line (a `down` of a stopped stack).",
    )
    args = parser.parse_args(argv)
    try:
        check_plan(
            sys.stdin.read(),
            eval_container_names(args.eval_file),
            allow_empty=args.allow_empty_plan,
        )
    except PlanGuardError as exc:
        sys.stderr.write(f"REFUSED: {exc}\n")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
