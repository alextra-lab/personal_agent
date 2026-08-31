"""FRE-1341 — assert a running eval gateway reflects the code you're about to test.

`docker-compose.eval.yml`'s gateway services reuse `seshat-gateway:latest` across `up -d`
calls — an existing tagged image is never rebuilt just because `src/` changed. That let a
cached image serve months-stale code with no error: `/health` reported `"status": "healthy"`
throughout. Bit twice: once serving a catalog four months out of date, once masking a config
guard (ADR-0112's storage allowlist) that predated the cached build entirely.

The fix here is a content fingerprint, not a git commit SHA. AC-1's seeded-negative sequence
is "build, then change something in `src/`, then run — it refuses". A `src/` edit does not
have to be committed to make a running image stale relative to what's on disk, and
`git rev-parse HEAD` is blind to exactly that case. `compute_build_fingerprint` hashes the
actual on-disk content of every path `Dockerfile.gateway` COPYs — computed once at build time
(baked into the image via a Docker ARG/ENV) and again at check time — so an uncommitted or
untracked change is caught just as reliably as a committed one.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import sys
from dataclasses import dataclass
from pathlib import Path

import httpx
import structlog

log = structlog.get_logger(__name__)

#: The exact paths Dockerfile.gateway COPYs into the image, plus the Dockerfile itself —
#: editing a COPY/RUN instruction changes what the image contains just as much as editing
#: src/. Keep this in sync with Dockerfile.gateway's COPY list.
BUILD_INPUT_PATHS: tuple[str, ...] = (
    "src",
    "config",
    "docs/skills",
    "docker/mcp",
    "pyproject.toml",
    "uv.lock",
    "Dockerfile.gateway",
)

_UNKNOWN_MARKERS = (None, "", "unknown")

_HEALTH_TIMEOUT_S = 10.0


def repo_root() -> Path:
    """Resolve the repo root without importing `personal_agent.config`.

    That package's `__init__` eagerly loads the full `AppConfig` singleton (env files,
    substrate probing) on import, which logs to stdout and would corrupt the CLI's
    `--print-fingerprint` value contract (the Makefile captures it via `$(...)`).
    """
    return Path(__file__).resolve().parent.parent.parent


def compute_build_fingerprint(repo_root: Path) -> str:
    """sha256 over the on-disk content of every Docker build input, path + bytes.

    Reads the working tree directly, never the git index, so an uncommitted or untracked
    edit under any of ``BUILD_INPUT_PATHS`` changes the result.

    Args:
        repo_root: Repository root to resolve ``BUILD_INPUT_PATHS`` against.

    Returns:
        Hex-encoded sha256 digest.
    """
    files: list[Path] = []
    for rel in BUILD_INPUT_PATHS:
        target = repo_root / rel
        if target.is_file():
            files.append(target)
        elif target.is_dir():
            files.extend(f for f in target.rglob("*") if f.is_file())

    hasher = hashlib.sha256()
    for f in sorted(files, key=lambda p: p.relative_to(repo_root).as_posix()):
        hasher.update(f.relative_to(repo_root).as_posix().encode())
        hasher.update(f.read_bytes())
    return hasher.hexdigest()


class GatewayStaleError(RuntimeError):
    """Raised when a running gateway's baked fingerprint doesn't match the working tree."""


@dataclass(frozen=True)
class FreshnessResult:
    """The outcome of comparing a running gateway's build fingerprint to the local tree.

    Attributes:
        fresh: True if the gateway's baked fingerprint matches the local tree's.
        running_fingerprint: What the gateway reported (None if missing/unreachable).
        expected_fingerprint: What the local working tree hashes to right now.
    """

    fresh: bool
    running_fingerprint: str | None
    expected_fingerprint: str


def _rebuild_command() -> str:
    return (
        "docker compose -p seshat -f docker-compose.cloud.yml -f docker-compose.eval.yml "
        "build seshat-gateway-control seshat-gateway-treatment"
    )


async def check_gateway_freshness(
    client: httpx.AsyncClient, base_url: str, repo_root: Path
) -> FreshnessResult:
    """Compare a running gateway's baked fingerprint against the current working tree.

    Args:
        client: An open httpx.AsyncClient.
        base_url: The gateway's base URL (e.g. ``http://localhost:9002``).
        repo_root: Repository root to fingerprint.

    Returns:
        The comparison outcome. Never raises on a stale/unreachable gateway — see
        :func:`assert_gateway_fresh` for the refusing variant.
    """
    expected = await asyncio.to_thread(compute_build_fingerprint, repo_root)

    running: str | None = None
    try:
        resp = await client.get(f"{base_url}/health", timeout=_HEALTH_TIMEOUT_S)
        resp.raise_for_status()
        running = resp.json().get("build_fingerprint")
    except httpx.HTTPError as exc:
        log.warning("gateway_freshness_unreachable", base_url=base_url, error=str(exc))

    if running in _UNKNOWN_MARKERS:
        running = None

    fresh = running is not None and running == expected
    return FreshnessResult(fresh=fresh, running_fingerprint=running, expected_fingerprint=expected)


async def assert_gateway_fresh(
    client: httpx.AsyncClient, base_url: str, repo_root: Path
) -> FreshnessResult:
    """Same as :func:`check_gateway_freshness`, but refuses loudly if not fresh.

    Args:
        client: An open httpx.AsyncClient.
        base_url: The gateway's base URL (e.g. ``http://localhost:9002``).
        repo_root: Repository root to fingerprint.

    Returns:
        The (fresh) comparison outcome.

    Raises:
        GatewayStaleError: The gateway is unreachable, reports no/unknown build info, or its
            baked fingerprint doesn't match the current working tree.
    """
    result = await check_gateway_freshness(client, base_url, repo_root)
    if not result.fresh:
        raise GatewayStaleError(
            f"STALE EVAL GATEWAY at {base_url}: running build_fingerprint="
            f"{result.running_fingerprint!r}, current working tree="
            f"{result.expected_fingerprint!r}. Rebuild it — {_rebuild_command()}"
        )
    return result


def main(argv: list[str] | None = None) -> int:
    """CLI entry point.

    Two modes: print the current fingerprint (for the Makefile's build-arg capture), or
    assert a running gateway is fresh (for ad hoc human/harness use).
    """
    parser = argparse.ArgumentParser(
        description="Assert an eval gateway's baked build reflects the working tree (FRE-1341)"
    )
    parser.add_argument("url", nargs="?", default="http://localhost:9002")
    parser.add_argument(
        "--print-fingerprint",
        action="store_true",
        help="Print the current working-tree build fingerprint and exit (no gateway call).",
    )
    args = parser.parse_args(argv)

    if args.print_fingerprint:
        print(compute_build_fingerprint(repo_root()))
        return 0

    async def _run() -> int:
        async with httpx.AsyncClient() as client:
            try:
                result = await assert_gateway_fresh(client, args.url, repo_root())
            except GatewayStaleError as exc:
                log.error("gateway_freshness_stale", url=args.url, error=str(exc))
                return 1
            log.info(
                "gateway_freshness_fresh",
                url=args.url,
                build_fingerprint=result.running_fingerprint,
            )
            return 0

    return asyncio.run(_run())


if __name__ == "__main__":
    sys.exit(main())
