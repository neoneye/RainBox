"""Hello-world MCP server: small string utilities for the MCP agent.

Tools:
    reverse_string  — character-reverse the input
    base64_encode   — standard base64 of the input's UTF-8 bytes
    base64_decode   — decode standard base64 back to the UTF-8 string

Spawned by MCPAgent via mcp.json over stdio. To run by hand:
    venv/bin/python mcp_servers/hello_server.py
"""

import base64

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("hello")


@mcp.tool()
def reverse_string(s: str) -> str:
    """Return the input string with its characters in reverse order."""
    return s[::-1]


@mcp.tool()
def base64_encode(s: str) -> str:
    """Return the standard base64 encoding of the input string's UTF-8 bytes."""
    return base64.b64encode(s.encode("utf-8")).decode("ascii")


@mcp.tool()
def base64_decode(s: str) -> str:
    """Decode a standard base64 string back to its UTF-8 string form."""
    return base64.b64decode(s.encode("ascii")).decode("utf-8")


if __name__ == "__main__":
    mcp.run()
