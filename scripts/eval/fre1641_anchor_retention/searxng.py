"""The harness SearXNG instances (ADR-0158 D5.2).

Five instances run under the compose project ``fre1641``, one per Exa highlight length. Each one
mounts a copy of the production ``settings.yml`` in which only the exa engine's
``content_max_characters`` differs. The 2,000 instance is the unchanged copy, the full form.
The production container ``cloud-sim-searxng`` is only inspected (image, ``StartedAt``), never
started, stopped or queried.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import time
from collections.abc import Mapping, Sequence
from pathlib import Path

import httpx
import yaml  # type: ignore[import-untyped]

HERE = Path(__file__).resolve().parent
COMPOSE_FILE = HERE / "docker-compose.fre1641.yml"
PROJECT = "fre1641"
PROD_CONTAINER = "cloud-sim-searxng"
PROD_SETTINGS_DIR = Path("/opt/seshat/docker/searxng")
#: The production Exa length: the full form of D5.3.
FULL_FORM_LENGTH = 2000
#: Exa length → host port. The order is the grid of D2.1, then the full form.
PORTS: Mapping[int, int] = {500: 18500, 800: 18501, 1200: 18502, 1600: 18503, 2000: 18504}
_EXA_BLOCK = re.compile(r"^  - name: exa\n(?P<body>(?:(?!  - name:).*\n)*)", re.MULTILINE)
_LENGTH_LINE = re.compile(r"^(?P<indent>\s+)content_max_characters:\s*\d+\s*$", re.MULTILINE)
_CONTAINER = re.compile(r"\bContainer\s+(\S+)")


class SearxngSetupError(Exception):
    """The instances cannot be set up safely."""


def _exa_engine(settings: Mapping[str, object]) -> dict[str, object]:
    engines = [e for e in settings.get("engines") or [] if isinstance(e, dict)]  # type: ignore[attr-defined]
    exa = [e for e in engines if e.get("name") == "exa"]
    if len(exa) != 1:
        raise SearxngSetupError(f"expected one exa engine, found {len(exa)}")
    return exa[0]


def with_exa_length(text: str, length: int) -> str:
    """Return the settings text with only the exa engine's ``content_max_characters`` changed.

    Args:
        text: The production ``settings.yml`` text.
        length: The new highlight length.

    Returns:
        The changed text.

    Raises:
        SearxngSetupError: If the exa block or its length line is not found exactly once, the
            production value is not the full form, or any other setting would change.
    """
    original = yaml.safe_load(text)
    if _exa_engine(original).get("content_max_characters") != FULL_FORM_LENGTH:
        raise SearxngSetupError("production exa content_max_characters is not the full form")
    blocks = list(_EXA_BLOCK.finditer(text))
    if len(blocks) != 1:
        raise SearxngSetupError(f"expected one exa block, found {len(blocks)}")
    block = blocks[0]
    body, n = _LENGTH_LINE.subn(
        lambda m: f"{m.group('indent')}content_max_characters: {length}", block.group("body")
    )
    if n != 1:
        raise SearxngSetupError(f"expected one content_max_characters line, found {n}")
    changed = text[: block.start("body")] + body + text[block.end("body") :]
    parsed = yaml.safe_load(changed)
    if _exa_engine(parsed).get("content_max_characters") != length:
        raise SearxngSetupError("the length did not change")
    _exa_engine(parsed)["content_max_characters"] = FULL_FORM_LENGTH
    if parsed != original:
        raise SearxngSetupError("a setting other than the exa length changed")
    return changed


def write_settings(root: Path, prod_dir: Path = PROD_SETTINGS_DIR) -> None:
    """Write one settings copy per instance under ``root`` (a 0700 directory).

    Args:
        root: The directory for the copies.
        prod_dir: The production SearXNG config directory.
    """
    text = (prod_dir / "settings.yml").read_text()
    root.mkdir(parents=True, exist_ok=True)
    root.chmod(0o700)
    for length in PORTS:
        target = root / str(length)
        target.mkdir(exist_ok=True)
        settings = target / "settings.yml"
        settings.write_text(with_exa_length(text, length))
        # The container user (uid 977) reads the mounted file; the 0700 parent keeps host users out.
        settings.chmod(0o644)
        shutil.copyfile(prod_dir / "limiter.toml", target / "limiter.toml")
        (target / "limiter.toml").chmod(0o644)


def remove_settings(root: Path) -> None:
    """Delete the settings copies, which hold vendor keys."""
    shutil.rmtree(root, ignore_errors=True)


def prod_container() -> dict[str, str]:
    """Return the production SearXNG container's image and ``StartedAt``.

    Returns:
        ``image`` and ``started_at``.
    """
    out = subprocess.run(
        ["docker", "inspect", "-f", "{{.Config.Image}}|{{.State.StartedAt}}", PROD_CONTAINER],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    image, started_at = out.split("|", 1)
    return {"image": image, "started_at": started_at}


def check_plan(plan: str) -> list[str]:
    """Refuse a compose plan that names any container outside the ``fre1641`` project.

    Args:
        plan: The ``docker compose ... --dry-run`` output.

    Returns:
        The container names in the plan.

    Raises:
        SearxngSetupError: If a container is not ``fre1641-*`` or the plan names none.
    """
    names = _CONTAINER.findall(plan)
    if not names:
        raise SearxngSetupError("the dry-run plan names no container")
    foreign = [n for n in names if not n.startswith(f"{PROJECT}-")]
    if foreign:
        raise SearxngSetupError(f"the plan touches containers outside {PROJECT}: {foreign}")
    return names


def _compose(args: Sequence[str], env: Mapping[str, str]) -> str:
    cmd = ["docker", "compose", "-p", PROJECT, "-f", str(COMPOSE_FILE), *args]
    done = subprocess.run(cmd, capture_output=True, text=True, env={**os.environ, **env})
    if done.returncode != 0:
        raise SearxngSetupError(f"{' '.join(args)} failed: {done.stderr[-2000:]}")
    return done.stdout + done.stderr


def up(root: Path, image: str, timeout_s: float = 120.0) -> None:
    """Start the five instances and wait until each answers ``/healthz``.

    Args:
        root: The settings copies' directory.
        image: The production SearXNG image.
        timeout_s: Seconds to wait for health.

    Raises:
        SearxngSetupError: On an unsafe plan or an instance that does not become healthy.
    """
    env = {"FRE1641_ROOT": str(root), "FRE1641_SEARXNG_IMAGE": image}
    check_plan(_compose(["up", "-d", "--dry-run"], env))
    _compose(["up", "-d"], env)
    deadline = time.monotonic() + timeout_s
    pending = dict(PORTS)
    while pending and time.monotonic() < deadline:
        for length, port in list(pending.items()):
            try:
                if httpx.get(f"http://127.0.0.1:{port}/healthz", timeout=3.0).status_code == 200:
                    del pending[length]
            except httpx.HTTPError:
                pass
        time.sleep(2.0)
    if pending:
        raise SearxngSetupError(f"instances not healthy: {sorted(pending)}")


def down(root: Path, image: str) -> None:
    """Stop and remove the five instances, then delete the settings copies.

    Args:
        root: The settings copies' directory.
        image: The image the instances ran.
    """
    try:
        _compose(["down"], {"FRE1641_ROOT": str(root), "FRE1641_SEARXNG_IMAGE": image})
    finally:
        remove_settings(root)
