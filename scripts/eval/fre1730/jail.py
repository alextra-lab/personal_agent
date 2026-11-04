"""FRE-1730: the jail for every process that a replay starts.

A session runs a plugin under test, with Bash on and with no one watching. So the jail, not a rule in a prompt, keeps
the session from the master tree, the seats' worktrees, production substrate on loopback and the shared ``~/.claude``.
The session's ``~`` is a new directory for each run. So each file that a plugin writes there can be attributed to it.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

ROOT_IN_JAIL = "/tmp/work"
REPO_IN_JAIL = f"{ROOT_IN_JAIL}/repo"
HOME_IN_JAIL = f"{ROOT_IN_JAIL}/home"
PLUGIN_IN_JAIL = f"{ROOT_IN_JAIL}/plugin"
EGRESS_SOCKET_NAME = "egress.sock"
EGRESS_PORT = 8787
#: ``/etc/resolv.conf`` can link here, and the jail hides ``/run``.
_RESOLVER_DIR = Path("/run/systemd/resolve")
_CPU_QUOTA = "200%"


def main_repo_root() -> Path:
    """Find the root of the main checkout. Every worktree of the repo is under it or next to it.

    Returns:
        The root of the main checkout.
    """
    common = subprocess.run(
        ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
        cwd=Path(__file__).parent,
        capture_output=True,
        text=True,
        check=True,
    )
    return Path(common.stdout.strip()).parent


def _tool_paths() -> list[Path]:
    """List what must stay readable under a hidden home: the tools and the Python that uv manages.

    Returns:
        The paths that exist.
    """
    home = Path.home()
    paths = [home / ".local" / "bin" / "uv", Path(uv_python_dir())]
    claude = shutil.which("claude")
    if claude:
        paths.append(Path(claude).resolve())
    return [path for path in paths if path.exists()]


def uv_python_dir() -> str:
    """Name the directory of uv's Python. A jailed ``HOME`` hides the default.

    Returns:
        The path.
    """
    return str(Path.home() / ".local" / "share" / "uv" / "python")


def scope_env() -> dict[str, str]:
    """Build what ``systemd-run --user`` needs. The jail unsets it before the command starts.

    Returns:
        The variables.
    """
    return {"XDG_RUNTIME_DIR": os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")}


def session_path() -> str:
    """Build ``PATH`` for a jailed process.

    Returns:
        The value.
    """
    return f"{Path.home() / '.local' / 'bin'}:/usr/bin:/bin"


@dataclass(frozen=True)
class Jail:
    """One jail.

    Attributes:
        run_root: The host directory that the process sees at ``ROOT_IN_JAIL``.
        venv: A virtual environment, bound read-only at ``.venv`` of the repo.
        plugin: A plugin directory, bound read-only at ``PLUGIN_IN_JAIL``. The other arm's jail does not hold it.
        network: Keep the host network. Only the environment build needs it.
        writable_venv: Bind ``venv`` read-write. Only the environment build needs it.
        memory_max: The memory limit of the process and its children. The VPS runs production.
    """

    run_root: Path
    venv: Path | None = None
    plugin: Path | None = None
    network: bool = False
    writable_venv: bool = False
    memory_max: str = "4G"

    def argv(self, command: Sequence[str]) -> list[str]:
        """Build the ``bwrap`` command.

        Args:
            command: The command to run inside.

        Returns:
            The whole command.
        """
        argv = [
            "systemd-run",
            "--user",
            "--scope",
            "--quiet",
            "-p",
            f"MemoryMax={self.memory_max}",
            "-p",
            "MemorySwapMax=0",
            "-p",
            f"CPUQuota={_CPU_QUOTA}",
            "nice",
            "-n",
            "10",
            "bwrap",
            "--unsetenv",
            "XDG_RUNTIME_DIR",
            "--ro-bind",
            "/",
            "/",
        ]
        for hidden in (main_repo_root(), Path.home()):
            argv += ["--tmpfs", str(hidden)]
        argv += ["--tmpfs", "/tmp", "--tmpfs", "/run", "--dev", "/dev", "--proc", "/proc"]
        for path in _tool_paths():
            argv += ["--ro-bind", str(path), str(path)]
        argv += ["--bind", str(self.run_root), ROOT_IN_JAIL]
        if self.venv is not None:
            bind = "--bind" if self.writable_venv else "--ro-bind"
            argv += [bind, str(self.venv), f"{REPO_IN_JAIL}/.venv"]
        if self.plugin is not None:
            argv += ["--ro-bind", str(self.plugin), PLUGIN_IN_JAIL]
        if self.network:
            if _RESOLVER_DIR.exists():
                argv += ["--ro-bind", str(_RESOLVER_DIR), str(_RESOLVER_DIR)]
        else:
            argv.append("--unshare-net")
        argv += [
            "--unshare-pid",
            "--new-session",
            "--die-with-parent",
            "--chdir",
            REPO_IN_JAIL,
            "--",
        ]
        return argv + list(command)


def with_egress_relay(command: Sequence[str]) -> list[str]:
    """Wrap a command so that loopback ``EGRESS_PORT`` inside the jail reaches the host's egress gate.

    Args:
        command: The command.

    Returns:
        The wrapped command.
    """
    relay = (
        f"socat TCP-LISTEN:{EGRESS_PORT},bind=127.0.0.1,fork,reuseaddr "
        f"UNIX-CONNECT:{ROOT_IN_JAIL}/{EGRESS_SOCKET_NAME} & "
        'exec "$@"'
    )
    return ["sh", "-c", relay, "sh", *command]


def run_in_jail(
    jail: Jail,
    command: Sequence[str],
    *,
    env: Mapping[str, str] | None = None,
    timeout_s: float = 600,
    stdin_text: str | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run one command in a jail, with a minimal environment.

    Args:
        jail: The jail.
        command: The command.
        env: More environment variables.
        timeout_s: The time limit.
        stdin_text: The text for the command's standard input.

    Returns:
        The finished process.
    """
    base = {
        "PATH": session_path(),
        "HOME": "/tmp",
        "LANG": os.environ.get("LANG", "C.UTF-8"),
        "UV_PYTHON_INSTALL_DIR": uv_python_dir(),
        **scope_env(),
    }
    return subprocess.run(
        jail.argv(command),
        env={**base, **(env or {})},
        capture_output=True,
        text=True,
        errors="replace",
        timeout=timeout_s,
        check=False,
        input=stdin_text,
    )
