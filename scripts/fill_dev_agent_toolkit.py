#!/usr/bin/env python3
"""Fill the dev-agent toolkit volume from the pinned ``/lib/`` bytes (ADR-0159 D3.3; FRE-1751).

Run it on the host that runs the dev-agent containers, after ``make dev-agent-build``::

    make dev-agent-toolkit

It reads each asset of ``config/artifact_lib_manifest.json`` from the R2 bucket (key
``lib/<path>``), checks every file against its SHA-384 pin in
``config/artifact_lib_substitution_map.json``, and only then replaces the Docker volume
``AGENT_DEV_AGENT_TOOLKIT_VOLUME``. It reads the volume back and checks it again. One bad file
refuses the whole volume: nothing is created, and an older volume stays as it was.

It prints the toolkit digest and exits 0, or prints the problem and exits 1. It exits 2 when R2 is
not configured.
"""

from __future__ import annotations

import asyncio
import sys

from personal_agent.config import settings
from personal_agent.dev_agent.container_spec import load_profiles
from personal_agent.dev_agent.launcher import run_command
from personal_agent.dev_agent.toolkit import fill_toolkit_volume, load_toolkit_assets
from personal_agent.exceptions import DevAgentToolkitError
from personal_agent.storage.artifact_store import (
    ArtifactStoreError,
    aclose_artifact_store,
    get_artifact_store,
)

#: The R2 key of an asset is this prefix and its path in the manifest.
R2_LIB_PREFIX = "lib/"


async def _run() -> int:
    store = get_artifact_store()
    if store is None:
        print("R2 is not configured: set the AGENT_R2_* variables.", file=sys.stderr)
        return 2
    volume = settings.dev_agent_toolkit_volume

    async def fetch(path: str) -> bytes:
        return await store.get(f"{R2_LIB_PREFIX}{path}")

    try:
        digest = await fill_toolkit_volume(
            volume,
            load_toolkit_assets(),
            fetch,
            image=load_profiles().agent.image,
            runner=run_command,
        )
    except (DevAgentToolkitError, ArtifactStoreError) as exc:
        print(f"toolkit volume NOT built: {exc}", file=sys.stderr)
        return 1
    finally:
        await aclose_artifact_store()
    print(f"volume: {volume}")
    print(f"digest: {digest}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(_run()))
