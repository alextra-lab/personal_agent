"""Service-backed conversational CLI for Personal Agent."""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

import httpx
import typer
from rich.console import Console
from rich.markdown import Markdown

from personal_agent.config import settings

console = Console()
app = typer.Typer(help="Personal Agent service client")
session_app = typer.Typer(help="Manage conversational session state")
app.add_typer(session_app, name="session")


def _session_file_path() -> Path:
    """Resolve session file path for local project or XDG use.

    Returns:
        Path to the active session file.
    """
    project_config_dir = Path.cwd() / "config"
    if project_config_dir.exists() and project_config_dir.is_dir():
        return project_config_dir / "current_session"

    xdg_config_dir = Path.home() / ".config" / "personal_agent"
    if xdg_config_dir.exists() and xdg_config_dir.is_dir():
        return xdg_config_dir / "current_session"

    return project_config_dir / "current_session"


def _read_current_session(path: Path) -> str | None:
    """Read and validate current session identifier from disk."""
    if not path.exists():
        return None

    raw = path.read_text(encoding="utf-8").strip()
    if not raw:
        return None

    try:
        uuid.UUID(raw)
        return raw
    except ValueError:
        return None


def _write_current_session(path: Path, session_id: str) -> None:
    """Persist current session identifier to disk."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"{session_id}\n", encoding="utf-8")


def _request_error_message(error: Exception) -> str:
    """Convert client errors to user-friendly CLI output."""
    if isinstance(error, httpx.HTTPStatusError):
        status = error.response.status_code
        details = error.response.text[:300]
        return f"Service request failed ({status}): {details}"
    if isinstance(error, httpx.RequestError):
        return (
            f"Cannot reach Personal Agent service at {settings.service_url}. "
            "Set AGENT_SERVICE_URL and ensure the service is running."
        )
    return str(error)


def _create_session(client: httpx.Client) -> str:
    """Create a new server-side session and return its id."""
    response = client.post(
        f"{settings.service_url}/sessions", json={"channel": "CLI", "mode": "NORMAL"}
    )
    response.raise_for_status()
    data: dict[str, Any] = response.json()
    session_id = str(data["session_id"])
    return session_id


def _ensure_session_id(client: httpx.Client, *, force_new: bool = False) -> str:
    """Get reusable session id from file or create a new one."""
    session_file = _session_file_path()
    if not force_new:
        existing = _read_current_session(session_file)
        if existing:
            return existing

    created = _create_session(client)
    _write_current_session(session_file, created)
    return created


def _send_chat(message: str, force_new: bool) -> int:
    """Send one chat request and print the service response."""
    try:
        with httpx.Client(timeout=120.0) as client:
            session_id = _ensure_session_id(client, force_new=force_new)
            response = client.post(
                f"{settings.service_url}/chat",
                params={"message": message, "session_id": session_id},
            )
            response.raise_for_status()
            data: dict[str, Any] = response.json()
            resolved_session_id = str(data.get("session_id", session_id))
            _write_current_session(_session_file_path(), resolved_session_id)
            console.print(Markdown(str(data.get("response", ""))))
            console.print(f"[dim]session: {resolved_session_id}[/dim]")
            return 0
    except Exception as error:  # noqa: BLE001
        console.print(f"[red]{_request_error_message(error)}[/red]")
        return 1


@app.callback(invoke_without_command=True)
def main(
    ctx: typer.Context,
    message: str | None = typer.Argument(None, help="Message to send to the agent"),
    new: bool = typer.Option(
        False,
        "--new",
        help="Start a new conversation session before sending the message.",
    ),
) -> None:
    """Default entrypoint: `agent \"message\"` behaves like chat."""
    if ctx.invoked_subcommand is not None:
        return

    if not message:
        console.print("[yellow]No message provided. Use `agent --help` for usage.[/yellow]")
        raise typer.Exit(1)

    raise typer.Exit(_send_chat(message, force_new=new))


@app.command()
def chat(
    message: str = typer.Argument(..., help="Message to send to the agent"),
    new: bool = typer.Option(
        False,
        "--new",
        help="Start a new conversation session before sending the message.",
    ),
) -> None:
    """Send a chat message to the service."""
    raise typer.Exit(_send_chat(message, force_new=new))


@session_app.callback(invoke_without_command=True)
def session_show() -> None:
    """Print the current active session id."""
    session_id = _read_current_session(_session_file_path())
    if session_id:
        console.print(session_id)
    else:
        console.print("No session")


@session_app.command("new")
def session_new() -> None:
    """Create a new server session and set it as current."""
    try:
        with httpx.Client(timeout=30.0) as client:
            session_id = _create_session(client)
            _write_current_session(_session_file_path(), session_id)
            console.print(session_id)
    except Exception as error:  # noqa: BLE001
        console.print(f"[red]{_request_error_message(error)}[/red]")
        raise typer.Exit(1) from error


if __name__ == "__main__":
    app()
