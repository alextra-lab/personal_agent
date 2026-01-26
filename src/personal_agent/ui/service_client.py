"""Thin CLI client for service mode."""

import asyncio
from typing import Optional

import httpx
import typer
from rich.console import Console
from rich.markdown import Markdown

console = Console()

# Default service URL (port 9000 to avoid conflict with SLM Server on 8000)
DEFAULT_SERVICE_URL = "http://localhost:9000"


class ServiceClient:
    """HTTP client for Personal Agent Service.

    Usage:
        client = ServiceClient()
        response = await client.chat("Hello!")
    """

    def __init__(self, base_url: str = DEFAULT_SERVICE_URL):  # noqa: D107
        """Initialize service client with base URL."""
        self.base_url = base_url
        self.session_id: Optional[str] = None

    async def health_check(self) -> dict:
        """Check service health.

        Returns:
            Health status dict

        Raises:
            httpx.ConnectError: If service is not running
        """
        async with httpx.AsyncClient() as client:
            response = await client.get(f"{self.base_url}/health")
            response.raise_for_status()
            return response.json()

    async def chat(self, message: str) -> str:
        """Send chat message and get response.

        Args:
            message: User's message

        Returns:
            Assistant's response
        """
        async with httpx.AsyncClient(timeout=120.0) as client:
            response = await client.post(
                f"{self.base_url}/chat", params={"message": message, "session_id": self.session_id}
            )
            response.raise_for_status()
            data = response.json()

            # Store session ID for continuity
            self.session_id = data.get("session_id")

            return data["response"]

    async def list_sessions(self) -> list[dict]:
        """List recent sessions."""
        async with httpx.AsyncClient() as client:
            response = await client.get(f"{self.base_url}/sessions")
            response.raise_for_status()
            return response.json()


# ============================================================================
# CLI Commands
# ============================================================================

app = typer.Typer(help="Personal Agent CLI (Service Mode)")


@app.command()
def chat(message: str):
    """Send a chat message to the agent."""
    client = ServiceClient()

    try:
        response = asyncio.run(client.chat(message))
        console.print(Markdown(response))
    except httpx.ConnectError:
        console.print("[red]Error: Service not running. Start with 'agent serve'[/red]")
        raise typer.Exit(1) from None
    except Exception as e:
        console.print(f"[red]Error: {e}[/red]")
        raise typer.Exit(1) from None


@app.command()
def health():
    """Check service health."""
    client = ServiceClient()

    try:
        status = asyncio.run(client.health_check())
        console.print(f"[green]Status: {status['status']}[/green]")
        for component, state in status.get("components", {}).items():
            color = "green" if state == "connected" else "yellow"
            console.print(f"  [{color}]{component}: {state}[/{color}]")
    except httpx.ConnectError:
        console.print("[red]Service not running[/red]")
        raise typer.Exit(1) from None


@app.command()
def sessions():
    """List recent sessions."""
    client = ServiceClient()

    try:
        sessions = asyncio.run(client.list_sessions())
        for s in sessions[:10]:
            console.print(
                f"  {s['session_id'][:8]}... - {s['channel'] or 'default'} - {s['last_active_at']}"
            )
    except httpx.ConnectError:
        console.print("[red]Service not running[/red]")
        raise typer.Exit(1) from None


if __name__ == "__main__":
    app()
