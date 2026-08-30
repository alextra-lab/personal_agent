"""Assert SearXNG loaded every engine its settings declare (FRE-1331 AC-4).

The failure this exists to catch is silent by construction. A declared engine can
fail to load for reasons that produce no error anywhere we look:

* the engine module is absent from the image — ``exaapi`` was missing from a
  four-month-stale build, so FRE-1310's config could never load (FRE-1331);
* upstream's own defaults ship the engine ``inactive: true`` and
  ``use_default_settings`` merges by name, so our block inherits it and the engine
  is never loaded at all — ``braveapi`` did exactly this, with **no** log line
  (FRE-1331);
* the module was deleted upstream between image versions — ``reddit`` (FRE-1331).

Nothing in the pre-merge path catches any of these: ``make test`` does not restart
SearXNG, CI does not, and the config guard reads files rather than a running
service. The mismatch is invisible until someone queries the right category and
notices the results are wrong — which on 2026-08-29/30 took two days.

So the check is deliberately dumb and post-deploy: read the declared engine names,
ask the running instance what it actually registered, and fail loudly on any
declared engine that is missing.

Exit codes:
    0   green   — every declared engine is registered
    2   red     — at least one declared engine is missing
    3   skipped — SearXNG unreachable (not a parity failure)
    64  usage   — bad CLI args

Usage:
    python -m scripts.monitors.searxng_engine_parity
    python -m scripts.monitors.searxng_engine_parity --base-url http://127.0.0.1:8888
    python -m scripts.monitors.searxng_engine_parity --settings docker/searxng/settings.yml
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import yaml

EXIT_OK = 0
EXIT_MISSING = 2
EXIT_UNREACHABLE = 3
EXIT_USAGE = 64

DEFAULT_BASE_URL = "http://127.0.0.1:8888"


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def settings_path(explicit: str | None) -> Path:
    """Resolve which SearXNG settings file to read.

    The real ``settings.yml`` is gitignored (FRE-1310) because it carries API keys,
    so a fresh clone and CI have only the template. Prefer the real file when it
    exists — it is what the container actually mounts — and fall back to the
    template otherwise, mirroring ``tests/test_tools/test_web_search.py``.

    Args:
        explicit: A caller-supplied path, or None to auto-resolve.

    Returns:
        The path to read declared engines from.
    """
    if explicit:
        return Path(explicit)
    searxng_dir = _repo_root() / "docker" / "searxng"
    real = searxng_dir / "settings.yml"
    return real if real.exists() else searxng_dir / "settings.yml.example"


def declared_engines(config: dict[str, Any]) -> set[str]:
    """Engine names the settings file declares, minus those it explicitly removes.

    ``use_default_settings.engines.remove`` names engines deliberately dropped
    because they cannot load in this image (missing deps, deleted upstream). Those
    are an intentional absence, not a parity failure, so they are subtracted rather
    than reported.

    Args:
        config: The parsed settings mapping.

    Returns:
        The set of engine names expected to be registered.
    """
    raw_engines = config.get("engines") or []
    if not isinstance(raw_engines, list):
        raise ValueError(
            "top-level `engines:` is not a list — this file is not a SearXNG settings file, "
            "or `use_default_settings.engines` was edited by mistake"
        )
    names = {e["name"] for e in raw_engines if isinstance(e, dict) and e.get("name")}

    # `use_default_settings.engines` is a mapping (`remove:`, `keep_only:`). REFUSE on any
    # other shape rather than defaulting the remove-list to empty. Treating an
    # unrecognised shape as "nothing removed" is fail-open: the file would be structurally
    # broken, SearXNG would not honour it, and this check would still print OK and exit 0.
    # A guard that reports green on input it does not understand is worse than no guard.
    uds = config.get("use_default_settings")
    uds_engines = uds.get("engines") if isinstance(uds, dict) else None
    if uds_engines is not None and not isinstance(uds_engines, dict):
        raise ValueError(
            "`use_default_settings.engines` is not a mapping — expected `remove:`/`keep_only:` "
            f"keys, got {type(uds_engines).__name__}. Refusing rather than assuming nothing "
            "is removed, which would report a false OK on a file SearXNG cannot honour."
        )
    removed = set((uds_engines or {}).get("remove") or [])
    return names - removed


def registered_engines(base_url: str) -> set[str]:
    """Engine names the RUNNING instance reports, via its own /config endpoint.

    Asking the service is the whole point: the file says what we intended, and only
    the running instance knows what it honoured.

    Args:
        base_url: Base URL of the SearXNG instance.

    Returns:
        The set of engine names the instance has registered.

    Raises:
        urllib.error.URLError: If the instance cannot be reached.
    """
    with urllib.request.urlopen(f"{base_url.rstrip('/')}/config", timeout=15) as resp:
        payload = json.loads(resp.read())
    return {e["name"] for e in payload.get("engines", []) if e.get("name")}


def main(argv: list[str] | None = None) -> int:
    """Compare declared engines against registered ones and report.

    Args:
        argv: Command-line arguments, or None to read from sys.argv.

    Returns:
        A process exit code (see the module docstring).
    """
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--base-url",
        default=DEFAULT_BASE_URL,
        help=f"SearXNG base URL (default {DEFAULT_BASE_URL})",
    )
    parser.add_argument(
        "--settings", default=None, help="Path to settings.yml (default: real file, else .example)"
    )
    args = parser.parse_args(argv)

    path = settings_path(args.settings)
    if not path.exists():
        print(f"usage: settings file not found: {path}", file=sys.stderr)
        return EXIT_USAGE

    try:
        declared = declared_engines(yaml.safe_load(path.read_text()))
    except (ValueError, yaml.YAMLError) as exc:
        print(f"usage: cannot read declared engines from {path}: {exc}", file=sys.stderr)
        return EXIT_USAGE

    try:
        registered = registered_engines(args.base_url)
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        # Not a parity failure — distinguish "cannot tell" from "mismatch", or the
        # check reads as green whenever the service happens to be down.
        print(
            f"skipped: SearXNG unreachable at {args.base_url} ({type(exc).__name__})",
            file=sys.stderr,
        )
        return EXIT_UNREACHABLE

    missing = sorted(declared - registered)

    print(f"settings:   {path}")
    print(f"declared:   {len(declared)} engines")
    print(f"registered: {len(registered)} engines")

    if missing:
        print(f"\nFAIL — {len(missing)} declared engine(s) NOT registered by the running instance:")
        for name in missing:
            print(f"  - {name}")
        print(
            "\nA declared engine that never registers produces no error and no log line. "
            "Check for a missing module in the image, or an upstream default shipping it "
            "`inactive: true` (which use_default_settings merges by name and which "
            "`disabled: false` does NOT override)."
        )
        return EXIT_MISSING

    print("\nOK — every declared engine is registered.")
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
