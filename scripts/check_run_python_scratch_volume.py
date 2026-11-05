r"""FRE-1736 check: a ``run_python`` script writes to /sandbox, and ``scratch_files`` returns the file.

The check uses real Docker. It makes no model call. It starts no compose service, and it reads and
writes no production volume. Run it on a host that has the gateway image and the sandbox image::

    uv run python scripts/check_run_python_scratch_volume.py

What it does:

1. It creates a throwaway volume and a throwaway container from the gateway image. The container
   runs as root with the Docker socket, as the deployed gateway does, and it mounts the volume at
   ``/app/agent_workspace``. The container takes the code from this checkout (``src`` is mounted
   over the image's).
2. In the container it sets up the state of a deployed gateway before the fix: the ``sandbox``
   directory and one trace directory belong to ``nobody``. Then it runs ``prepare_workspace`` as
   startup does.
3. It calls ``run_python_executor`` for three traces. Each script writes a file to /sandbox.
4. It checks the exit code, ``scratch_files``, the content and owner of each file from the gateway
   side, and that a trace sees no file of another trace.
5. It removes the container, then the volume, on every exit path.

The container gets fake credentials, because the ``cloud`` profile needs them to start. They are
never used.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import uuid
from pathlib import Path

GATEWAY_IMAGE = "seshat-seshat-gateway:latest"


def inner() -> int:
    """Run inside the throwaway gateway container, as root. Return the exit code."""
    import asyncio
    import os

    from personal_agent.telemetry import TraceContext
    from personal_agent.tools.primitives._child_workspace import (
        CHILD_GID,
        CHILD_UID,
        prepare_workspace,
    )
    from personal_agent.tools.primitives.run_python import run_python_executor

    workspace = Path("/app/agent_workspace")
    sandbox = workspace / "sandbox"
    failures: list[str] = []

    def check(condition: bool, what: str) -> None:
        print(("PASS " if condition else "FAIL ") + what)
        if not condition:
            failures.append(what)

    trace_a, trace_b, trace_old = (TraceContext.new_trace().trace_id for _ in range(3))
    # The state of a deployed gateway before the fix.
    sandbox.mkdir(exist_ok=True)
    os.chown(sandbox, CHILD_UID, CHILD_GID)
    (sandbox / trace_old).mkdir()
    os.chown(sandbox / trace_old, CHILD_UID, CHILD_GID)
    prepare_workspace()  # as at gateway startup

    async def run(trace_id: str, name: str, text: str) -> dict[str, object]:
        script = (
            f"import os\nopen('/sandbox/{name}', 'w').write({text!r})\n"
            "print(sorted(os.listdir('/sandbox')))"
        )
        return await run_python_executor(script, ctx=TraceContext(trace_id=trace_id))

    async def main() -> None:
        results = {
            "a": await run(trace_a, "a.txt", "from A"),
            "b": await run(trace_b, "b.txt", "from B"),
            "old": await run(trace_old, "old.txt", "from old"),
        }
        print("RESULTS " + json.dumps(results))
        for key, trace_id, name, text in (
            ("a", trace_a, "a.txt", "from A"),
            ("b", trace_b, "b.txt", "from B"),
            ("old", trace_old, "old.txt", "from old"),
        ):
            path = sandbox / trace_id / name
            result = results[key]
            check(result["exit_code"] == 0, f"{key}: the call succeeds")
            check(result["scratch_files"] == [str(path)], f"{key}: scratch_files lists the file")
            check(
                path.is_file() and path.read_text() == text, f"{key}: the gateway reads the content"
            )
            check(
                path.is_file() and path.stat().st_uid == 1000,
                f"{key}: the sandbox user owns the file",
            )
            check(f"['{name}']" in str(result["stdout"]), f"{key}: the sandbox sees only its file")
        check(
            (sandbox.stat().st_uid, sandbox.stat().st_gid) == (0, 0),
            "the sandbox parent directory belongs to root",
        )

    asyncio.run(main())
    return 1 if failures else 0


def driver(sandbox_image: str, gateway_image: str, src: Path) -> int:
    """Run this file inside a throwaway container, and remove everything. Return the exit code."""
    token = uuid.uuid4().hex[:12]
    volume = f"fre1736-check-{token}"
    container = f"fre1736-check-{token}"
    docker = ["docker"]
    created_volume = False
    try:
        subprocess.run(
            [*docker, "image", "inspect", sandbox_image], check=True, capture_output=True
        )
        subprocess.run([*docker, "volume", "create", volume], check=True, capture_output=True)
        created_volume = True
        completed = subprocess.run(
            [
                *docker,
                "run",
                "--name",
                container,
                "-i",
                "--network",
                "none",
                "--user",
                "0",
                "-v",
                f"{volume}:/app/agent_workspace",
                "-v",
                "/var/run/docker.sock:/var/run/docker.sock",
                "-v",
                f"{src.resolve()}:/app/src:ro",
                "-e",
                "PYTHONPATH=/app/src",
                "-e",
                "AGENT_DEPLOYMENT_PROFILE=cloud",
                "-e",
                f"AGENT_SANDBOX_SCRATCH_VOLUME={volume}",
                "-e",
                f"AGENT_SANDBOX_IMAGE={sandbox_image}",
                "-e",
                "AGENT_SLM_BASE_URL=http://127.0.0.1:9",
                "-e",
                "AGENT_ANTHROPIC_API_KEY=fake-anthropic-key-0123456789",
                "-e",
                "AGENT_OPENAI_API_KEY=fake-openai-key-0123456789",
                "-e",
                "LITELLM_LOCAL_MODEL_COST_MAP=True",
                "--entrypoint",
                "/app/.venv/bin/python",
                gateway_image,
                "-",
                "--inner",
            ],
            input=Path(__file__).read_text(),
            text=True,
            capture_output=True,
            check=False,
        )
        for line in completed.stdout.splitlines():
            if line.startswith(("PASS ", "FAIL ", "RESULTS ")):
                print(line)
        if completed.returncode != 0:
            print(f"container exit code {completed.returncode}")
            print(completed.stderr[-2000:])
        return completed.returncode
    finally:
        # The container holds the Docker socket and the volume: remove it first, always.
        subprocess.run([*docker, "rm", "-f", container], capture_output=True, check=False)
        if created_volume:
            subprocess.run(
                [*docker, "volume", "rm", "-f", volume], capture_output=True, check=False
            )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--inner", action="store_true", help="run inside the container")
    parser.add_argument("--sandbox-image", default="seshat-sandbox-python:0.1")
    parser.add_argument("--gateway-image", default=GATEWAY_IMAGE)
    parser.add_argument(
        "--src",
        type=Path,
        default=Path(__file__).resolve().parent.parent / "src" if "__file__" in globals() else None,
        help="the source tree to check (default: this checkout)",
    )
    arguments = parser.parse_args()
    sys.exit(
        inner()
        if arguments.inner
        else driver(arguments.sandbox_image, arguments.gateway_image, arguments.src)
    )
