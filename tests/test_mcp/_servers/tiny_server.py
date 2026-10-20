"""A minimal real MCP server over stdio, for the FRE-1625 end-to-end client test.

Not imported by the suite: ``test_client_stdio_roundtrip`` spawns it as a subprocess.
"""

from mcp.server.mcpserver import MCPServer
from mcp.shared.exceptions import MCPError
from mcp.types import INVALID_PARAMS

server = MCPServer("tiny")


@server.tool()
def add(a: int, b: int) -> int:
    """Add two integers."""
    return a + b


@server.tool()
def reject(team: str) -> str:
    """Reject every call as an invalid argument."""
    raise MCPError(INVALID_PARAMS, f"team {team!r} does not exist")


if __name__ == "__main__":
    server.run("stdio")
