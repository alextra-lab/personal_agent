r"""FRE-1505 adversarial probe: can a bash tool call on an eval gateway read a credential?

Run it inside the gateway image as root, the way the eval-treatment gateway runs. It is
self-contained (stdlib plus the installed ``personal_agent`` package), so it is piped in
rather than copied into the image.

Against a locally built image, with seeded fake credentials only::

    docker run --rm -i -e AGENT_DEPLOYMENT_PROFILE=eval \
      -e AGENT_SLM_BASE_URL=http://127.0.0.1:9/v1 \
      -e AGENT_ANTHROPIC_API_KEY=fake-anthropic-key-0123456789 \
      -e AGENT_OPENAI_API_KEY=fake-openai-key-0123456789 \
      --entrypoint /app/.venv/bin/python seshat-gateway:<tag> - \
      < scripts/eval/probe_bash_credential_isolation.py

Against the running eval-treatment container (real credentials; the probe prints variable
names only, never values)::

    docker exec -i cloud-sim-seshat-gateway-treatment /app/.venv/bin/python - \
      < scripts/eval/probe_bash_credential_isolation.py

Steps:

1. Start a long-lived sibling process that inherits this process's full environment. It has
   the shape of a Docker health check or a gateway-spawned child.
2. Instrument check: run the scan as root, directly. It must find a credential value, or
   the probe proves nothing.
3. Run the same scan through ``bash_executor``, repeated for ``SCAN_SECONDS`` so that it
   crosses Docker health-check ticks.
4. Run ``echo ok`` through ``bash_executor``.

A credential value is any value of this process's environment that is not on the child
allowlist and is at least ``MIN_VALUE_LENGTH`` characters long. This includes URLs and
DSNs, not only ``*_KEY`` names.

The steps run under ``uvloop``, the event loop uvicorn serves the gateway on (FRE-1518).
An earlier version used ``asyncio.run``. It passed while every served eval bash call
failed, because uvloop rejects spawn kwargs that the stdlib loop accepts.

Exit 0 only when the probe ran on uvloop, the instrument finds a credential, the eval
scan finds none, and ``echo ok`` returns ``ok``.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

import uvloop

from personal_agent.config import settings
from personal_agent.telemetry import TraceContext
from personal_agent.tools.primitives.bash import EVAL_CHILD_ENV_NAMES, bash_executor

SCAN_SECONDS = 25
MIN_VALUE_LENGTH = 8

_SCAN_ONCE = (
    "for f in /proc/[0-9]*/environ /proc/[0-9]*/task/*/environ; do "
    "{ tr '\\0' '\\n' < \"$f\"; } 2>/dev/null; done"
)
_SCAN_REPEATED = (
    f"end=$((SECONDS+{SCAN_SECONDS})); "
    f'while [ "$SECONDS" -lt "$end" ]; do {_SCAN_ONCE}; done | sort -u; '
    'printenv; echo "child_uid=$(id -u)"'
)


def _credential_values() -> dict[str, str]:
    """Return this process's environment values that a bash child must not see."""
    return {
        name: value
        for name, value in os.environ.items()
        if name not in EVAL_CHILD_ENV_NAMES and len(value) >= MIN_VALUE_LENGTH
    }


def _leaked_names(output: str, credentials: dict[str, str]) -> list[str]:
    """Return the names of credentials whose values appear in ``output``."""
    return sorted(name for name, value in credentials.items() if value in output)


def _call_output(result: dict[str, object]) -> str:
    """Return a bash_executor result's full output, including any overflow file."""
    truncated_path = result.get("truncated_path")
    if isinstance(truncated_path, str) and Path(truncated_path).is_file():
        return Path(truncated_path).read_text(encoding="utf-8", errors="replace")
    return f"{result.get('stdout', '')}{result.get('stderr', '')}"


async def _probe() -> dict[str, object]:
    """Run the probe steps and return a report with names only."""
    credentials = _credential_values()
    sibling = subprocess.Popen(["sleep", str(SCAN_SECONDS + 60)])
    try:
        instrument = subprocess.run(
            ["/bin/bash", "-c", _SCAN_ONCE],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        ctx = TraceContext.new_trace()
        scan = await bash_executor(_SCAN_REPEATED, timeout_seconds=SCAN_SECONDS + 30, ctx=ctx)
        echo = await bash_executor("echo ok", ctx=ctx)
    finally:
        sibling.kill()
        sibling.wait()

    scan_output = _call_output(scan)
    child_uid = next(
        (ln.split("=", 1)[1] for ln in scan_output.splitlines() if ln.startswith("child_uid=")),
        None,
    )
    return {
        "event_loop": type(asyncio.get_running_loop()).__module__.split(".")[0],
        "deployment_profile": settings.deployment_profile,
        "gateway_euid": os.geteuid(),
        "credential_names_checked": sorted(credentials),
        "instrument_leaked": _leaked_names(instrument.stdout, credentials),
        "eval_scan_error": scan.get("error"),
        "eval_scan_child_uid": child_uid,
        "eval_scan_leaked": _leaked_names(scan_output, credentials),
        "echo_ok": echo.get("stdout") == "ok\n" and echo.get("success") is True,
    }


def main() -> int:
    """Run the probe, print the JSON report, and return the process exit code."""
    report = uvloop.run(_probe())
    passed = (
        report["event_loop"] == "uvloop"
        and report["deployment_profile"] == "eval"
        and bool(report["instrument_leaked"])
        and report["eval_scan_error"] is None
        and report["eval_scan_child_uid"] == "65534"
        and report["eval_scan_leaked"] == []
        and report["echo_ok"] is True
    )
    report["passed"] = passed
    sys.stdout.write(json.dumps(report, indent=2) + "\n")
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
