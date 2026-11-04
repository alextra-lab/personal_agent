r"""FRE-1505, FRE-1732 adversarial probe: can a bash call on a deployed gateway read a credential?

Run it inside the gateway image as root, the way a deployed gateway runs. It is
self-contained (stdlib plus the installed ``personal_agent`` package), so it is piped in
rather than copied into the image.

Against a locally built image, with seeded fake credentials only::

    docker run --rm -i -e AGENT_DEPLOYMENT_PROFILE=eval \
      -e AGENT_SLM_BASE_URL=http://127.0.0.1:9/v1 \
      -e AGENT_ANTHROPIC_API_KEY=fake-anthropic-key-0123456789 \
      -e AGENT_OPENAI_API_KEY=fake-openai-key-0123456789 \
      --entrypoint /app/.venv/bin/python seshat-gateway:<tag> - \
      < scripts/eval/probe_bash_credential_isolation.py

With ``AGENT_DEPLOYMENT_PROFILE=cloud``, the probe also runs the commands that FRE-1732 names,
the two reviewed scripts that need a credential, and the workspace steps. Use fake credentials and
a tmpfs at ``/app/agent_workspace``. To test a branch, mount its ``src``, ``config``, ``skills``
and ``scripts`` over the image's. Never run this mode in a production container, because it
changes the workspace.

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

Exit 0 only when the probe ran on uvloop, the instrument finds a credential, the scan
finds none, and ``echo ok`` returns ``ok``. On ``cloud`` every extra check must also pass.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import uvloop

from personal_agent.config import settings
from personal_agent.telemetry import TraceContext
from personal_agent.tools.contract import ToolError
from personal_agent.tools.primitives import read as read_module
from personal_agent.tools.primitives._child_workspace import give_to_child, prepare_workspace
from personal_agent.tools.primitives.bash import (
    CHILD_ENV_NAMES,
    CHILD_SETPRIV_ARGV,
    bash_executor,
    child_env,
)
from personal_agent.tools.primitives.edit import edit_executor
from personal_agent.tools.primitives.read import read_executor
from personal_agent.tools.primitives.write import write_executor

SCAN_SECONDS = 25
MIN_VALUE_LENGTH = 8
RACE_ATTEMPTS = 3000

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
        if name not in CHILD_ENV_NAMES and len(value) >= MIN_VALUE_LENGTH
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


async def _item_one_commands(credentials: dict[str, str], ctx: TraceContext) -> dict[str, list[str]]:
    """Run the four commands that FRE-1732 names, and return the names that each one leaks."""
    leaks: dict[str, list[str]] = {}
    for command in (
        "cat /proc/self/environ",
        "cat /proc/1/environ",
        "ps eww -p 1",
        "jq -n env",
    ):
        result = await bash_executor(command, ctx=ctx)
        leaks[command] = _leaked_names(_call_output(result), credentials)
    return leaks


async def _script_calls(credentials: dict[str, str], ctx: TraceContext) -> dict[str, object]:
    """Check the reviewed scripts: the plain call gets its credential, and no other form does."""
    cypher = "python3 skills/graph-inspection/scripts/cypher.py 'RETURN 1 AS n'"
    probe = "python3 skills/service-health/scripts/probe_services.py postgres"
    report: dict[str, object] = {}
    for label, command in (
        ("cypher_plain", cypher),
        ("cypher_abs", cypher.replace("python3 skills", "python3 /app/skills")),
        ("cypher_pipe", f"{cypher} | cat"),
        ("cypher_list", f"true && {cypher}"),
        ("probe_plain", probe),
        ("probe_pipe", f"{probe} | cat"),
    ):
        result = await bash_executor(command, ctx=ctx)
        output = _call_output(result)
        report[label] = {
            "says_not_set": "is not set in this environment" in output,
            "first_line": (output.strip().splitlines() or [""])[0][:120],
            "import_failed": "No module named" in output,
            "leaked": _leaked_names(output, credentials),
        }
    # The scripts that need no credential run as nobody. Each must still do its work.
    diagram = "/tmp/fre1732-diagram.mmd"
    Path(diagram).write_text("flowchart LR\n  A-->B\n")
    for label, command, expected in (
        (
            "mermaid_as_nobody",
            f"python3 skills/mermaid-diagrams/scripts/validate_mermaid.py {diagram}",
            "VALID",
        ),
        ("mmdc_as_nobody", f"mmdc -i {diagram} -o /tmp/fre1732-diagram.svg", "Generating"),
        (
            "index_names_as_nobody",
            "printf 'agent-logs-2026.11.04\\n' | "
            "python3 skills/log-search/scripts/es_index_granularity.py agent-logs",
            "family: agent-logs",
        ),
        (
            "timeline_as_nobody",
            "echo no-event | python3 skills/turn-explanation/scripts/call_timeline.py trace-1",
            "no model call or tool call",
        ),
    ):
        result = await bash_executor(command, timeout_seconds=90, ctx=ctx)
        output = _call_output(result)
        report[label] = {
            "did_its_work": expected in output,
            "first_line": (output.strip().splitlines() or [""])[0][:120],
            "import_failed": "No module named" in output,
            "leaked": _leaked_names(output, credentials),
        }
    return report


async def _workspace_steps(ctx: TraceContext) -> dict[str, object]:
    """AC-6: the child creates, edits and runs files in the workspace.

    The seeded negative runs first: before ``prepare_workspace`` the same child cannot write.
    """
    root = Path("/app/agent_workspace")
    project = root / "probe-project"
    project.mkdir()
    (project / "tool.sh").write_text("echo before\n")
    (root / "sandbox").mkdir()

    async def run(command: str) -> tuple[int, str]:
        result = await bash_executor(command, ctx=ctx)
        return int(str(result.get("exit_code"))), _call_output(result)

    step = f"echo x > {project}/new.txt"
    before_code, before_text = await run(step)
    report: dict[str, object] = {
        "before_prepare_refused": before_code != 0 and "Permission denied" in before_text,
    }

    prepare_workspace()
    checks = {
        "create_in_project": f"echo x > {project}/new.txt",
        "edit_in_place": f"echo echo after > {project}/tool.sh",
        "sed_in_place": f"sed -i s/after/changed/ {project}/tool.sh",
        "run_file": f"sh {project}/tool.sh | grep -x changed",
        "mkdir_new_project": f"mkdir {root}/made-by-child && echo x > {root}/made-by-child/a",
        "create_in_sandbox": f"mkdir {root}/sandbox/t1 && echo x > {root}/sandbox/t1/a",
        "python_in_project": f"python3 -c \"print(1)\" > {project}/py.txt",
    }
    report["after"] = {name: (await run(command))[0] == 0 for name, command in checks.items()}

    # Files that root tools make stay usable by the child.
    tool_file = root / "made-by-tool" / "deep" / "f.txt"
    await write_executor(str(tool_file), "one two\n", ctx=ctx)
    report["child_edits_write_tool_file"] = (await run(f"echo x >> {tool_file}"))[0] == 0
    report["child_creates_beside_write_tool_file"] = (
        await run(f"echo x > {tool_file.parent}/g.txt")
    )[0] == 0
    await edit_executor(str(tool_file), "one", "three", ctx=ctx)
    report["child_edits_edit_tool_file"] = (await run(f"echo x >> {tool_file}"))[0] == 0

    # A child cannot use the tree to get root to change another file.
    await run(f"ln -s /etc/hostname {root}/planted")
    give_to_child([root / "planted"], trace_id=ctx.trace_id)
    report["planted_link_target_keeps_owner"] = Path("/etc/hostname").stat().st_uid == 0

    # The overflow file: a child plants the next name as a link, and then prints too much.
    victim = Path("/app/skills-probe-victim.txt")
    victim.write_text("untouched\n")
    trace_dir = f"/tmp/agent_scratch/{ctx.trace_id}"
    await run(f"mkdir -p {trace_dir} && ln -s {victim} {trace_dir}/bash_output_1.txt")
    await run("head -c 60000 /dev/zero | tr '\\0' a")
    report["overflow_does_not_follow_a_link"] = victim.read_text() == "untouched\n"

    # What a root tool makes in /tmp stays usable by the child.
    await write_executor("/tmp/fre1732-scratch/deep/f.txt", "one\n", ctx=ctx)
    report["child_edits_tmp_write_tool_file"] = (
        await run("echo x >> /tmp/fre1732-scratch/deep/f.txt")
    )[0] == 0
    report["child_creates_beside_tmp_write_tool_file"] = (
        await run("echo x > /tmp/fre1732-scratch/deep/g.txt")
    )[0] == 0
    report["tmp_keeps_its_owner"] = Path("/tmp").stat().st_uid == 0

    # fs.protected_regular: root opens a child's file in sticky /tmp with no O_CREAT.
    await write_executor("/tmp/fre1732-flat.txt", "one\n", ctx=ctx)
    await write_executor("/tmp/fre1732-flat.txt", "two\n", mode="append", ctx=ctx)
    await write_executor("/tmp/fre1732-flat.txt", "three\n", ctx=ctx)
    report["second_write_to_a_child_file_in_tmp"] = (
        Path("/tmp/fre1732-flat.txt").read_text() == "three\n"
    )
    await run("echo mine > /tmp/fre1732-made-by-child.txt")
    await write_executor("/tmp/fre1732-made-by-child.txt", "root\n", ctx=ctx)
    report["write_to_a_file_that_the_child_made_in_tmp"] = (
        Path("/tmp/fre1732-made-by-child.txt").read_text() == "root\n"
    )

    # A root tool gives away only what its own call created.
    private = Path("/tmp/fre1732-root-private")
    private.mkdir(mode=0o700)
    existing = Path("/tmp/fre1732-root-file.txt")
    existing.write_text("one two\n")
    await write_executor(str(private / "new.txt"), "x\n", ctx=ctx)
    await edit_executor(str(existing), "one", "three", ctx=ctx)
    report["existing_root_directory_stays_with_root"] = private.stat().st_uid == 0
    report["edit_keeps_a_root_file_with_root"] = existing.stat().st_uid == 0

    # A FIFO that the child plants does not block the gateway.
    await run("mkdir /tmp/fre1732-fd && mkfifo /tmp/fre1732-fd/f")
    fifo_blocked = False
    for call in (
        write_executor("/tmp/fre1732-fd/f", "x", ctx=ctx),
    ):
        try:
            await asyncio.wait_for(asyncio.shield(call), timeout=5)
        except TimeoutError:
            fifo_blocked = True
        except ToolError:
            pass
    report["a_planted_fifo_does_not_block_write"] = not fifo_blocked
    return report


async def _read_race(credentials: dict[str, str], ctx: TraceContext) -> dict[str, object]:
    """Race ``read`` against a child that swaps a file for a link to ``/proc/1/environ``.

    The child runs the swap through ``setpriv``, as a bash child does. The first pass restores
    the old open, and it must leak, or this probe proves nothing about the second pass.
    """
    race_dir = Path("/tmp/fre1732-race")
    swap = (
        f"mkdir -p {race_dir}; cd {race_dir}; "
        "while :; do "
        "ln -sf /proc/1/environ l.tmp; mv -T l.tmp target; "
        "echo plain > f.tmp; mv -T f.tmp target; "
        "done"
    )
    swapper = subprocess.Popen([*CHILD_SETPRIV_ARGV, "/bin/bash", "-c", swap], env=child_env())

    async def leaks_in(attempts: int) -> int:
        count = 0
        for _ in range(attempts):
            try:
                result = await read_executor(str(race_dir / "target"), ctx=ctx)
            except ToolError:
                continue
            count += bool(_leaked_names(json.dumps(result), credentials))
        return count

    try:
        await asyncio.sleep(0.5)
        with patch.object(read_module, "open_file_no_symlinks", lambda path, flags: os.open(path, flags)):
            old_open_leaks = await leaks_in(RACE_ATTEMPTS)
        new_open_leaks = await leaks_in(RACE_ATTEMPTS)
    finally:
        swapper.kill()
        swapper.wait()
    return {
        "attempts": RACE_ATTEMPTS,
        "old_open_leaks": old_open_leaks,
        "new_open_leaks": new_open_leaks,
    }


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
    report: dict[str, object] = {
        "event_loop": type(asyncio.get_running_loop()).__module__.split(".")[0],
        "deployment_profile": settings.deployment_profile,
        "gateway_euid": os.geteuid(),
        "credential_names_checked": sorted(credentials),
        "instrument_leaked": _leaked_names(instrument.stdout, credentials),
        "eval_scan_error": scan.get("error"),
        "eval_scan_child_uid": child_uid,
        "eval_scan_leaked": _leaked_names(scan_output, credentials),
        "echo_ok": echo.get("stdout") == "ok\n" and echo.get("exit_code") == 0,
    }
    if settings.deployment_profile == "cloud":
        report["item_one_leaks"] = await _item_one_commands(credentials, ctx)
        report["script_calls"] = await _script_calls(credentials, ctx)
        report["workspace"] = await _workspace_steps(ctx)
        report["read_race"] = await _read_race(credentials, ctx)
    return report


def _cloud_checks_pass(report: dict[str, object]) -> bool:
    """Return whether the FRE-1732 checks in the report all pass."""
    leaks = report["item_one_leaks"]
    scripts = report["script_calls"]
    workspace = report["workspace"]
    race = report["read_race"]
    assert isinstance(leaks, dict) and isinstance(scripts, dict) and isinstance(workspace, dict)
    assert isinstance(race, dict)
    after = workspace.pop("after")
    assert isinstance(after, dict)
    report["workspace_after"] = after
    return (
        all(not names for names in leaks.values())
        and all(not entry["leaked"] and not entry["import_failed"] for entry in scripts.values())
        and not scripts["cypher_plain"]["says_not_set"]
        and not scripts["cypher_abs"]["says_not_set"]
        and not scripts["probe_plain"]["says_not_set"]
        and scripts["cypher_pipe"]["says_not_set"]
        and scripts["cypher_list"]["says_not_set"]
        and scripts["probe_pipe"]["says_not_set"]
        and scripts["mermaid_as_nobody"]["did_its_work"]
        and scripts["mmdc_as_nobody"]["did_its_work"]
        and scripts["index_names_as_nobody"]["did_its_work"]
        and scripts["timeline_as_nobody"]["did_its_work"]
        and all(after.values())
        and all(workspace.values())
        and race["old_open_leaks"] > 0
        and race["new_open_leaks"] == 0
    )


def main() -> int:
    """Run the probe, print the JSON report, and return the process exit code."""
    report = uvloop.run(_probe())
    passed = (
        report["event_loop"] == "uvloop"
        and report["deployment_profile"] in ("eval", "cloud")
        and bool(report["instrument_leaked"])
        and report["eval_scan_error"] is None
        and report["eval_scan_child_uid"] == "65534"
        and report["eval_scan_leaked"] == []
        and report["echo_ok"] is True
    )
    if report["deployment_profile"] == "cloud":
        passed = _cloud_checks_pass(report) and passed
    report["passed"] = passed
    sys.stdout.write(json.dumps(report, indent=2) + "\n")
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
