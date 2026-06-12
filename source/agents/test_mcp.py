"""Tests for the MCP agent's config loader and the hello MCP server.

The first test is a unit test over mcp_config.load_mcp_servers().
The second test (Task 3) spawns the hello server via the LlamaIndex MCP
client and asserts the reverse_string tool returns the reversed input.
No LLM is involved in any assertion path.
"""

import asyncio
import json
from pathlib import Path

from llama_index.tools.mcp import BasicMCPClient, McpToolSpec

from agents.mcp_config import load_mcp_servers


def test_load_mcp_servers_has_hello():
    servers = load_mcp_servers()
    names = [s.name for s in servers]
    assert "hello" in names, f"expected 'hello' server, got {names}"
    hello = next(s for s in servers if s.name == "hello")
    assert hello.args, "hello server should have args"
    assert hello.args[0].endswith("hello_server.py")


def test_load_mcp_servers_parses_http_entry(tmp_path: Path):
    """A url-style entry (no `command`) parses into a ServerSpec with
    url + headers set and no command/args. Used for HTTP/SSE MCP servers
    (the `planexeremote` case)."""
    cfg = tmp_path / "mcp.json"
    cfg.write_text(json.dumps({
        "servers": {
            "stdio_one": {
                "command": "python",
                "args": ["mcp_servers/hello_server.py"],
            },
            "http_one": {
                "url": "https://example.invalid/mcp",
                "headers": {"X-API-Key": "test-key"},
            },
        }
    }))
    specs = load_mcp_servers(path=cfg)
    by_name = {s.name: s for s in specs}
    assert set(by_name) == {"stdio_one", "http_one"}

    stdio = by_name["stdio_one"]
    assert stdio.command is not None and stdio.url is None
    assert stdio.args and stdio.args[0].endswith("hello_server.py")

    http = by_name["http_one"]
    assert http.url == "https://example.invalid/mcp"
    assert http.headers == {"X-API-Key": "test-key"}
    assert http.command is None


def _write_cfg(path: Path, servers: dict) -> Path:
    path.write_text(json.dumps({"servers": servers}))
    return path


def test_overlay_adds_servers(tmp_path: Path):
    base = _write_cfg(tmp_path / "mcp.json", {
        "hello": {"command": "python", "args": ["mcp_servers/hello_server.py"]},
    })
    overlay = _write_cfg(tmp_path / "overlay.json", {
        "extra": {"url": "https://example.invalid/mcp"},
    })
    names = {s.name for s in load_mcp_servers(path=base, overlay_path=overlay)}
    assert names == {"hello", "extra"}


def test_overlay_replaces_same_named_server_wholesale(tmp_path: Path):
    """An overlay entry with the same name REPLACES the base entry —
    no field bleed-through (base stdio fields must not survive)."""
    base = _write_cfg(tmp_path / "mcp.json", {
        "hello": {"command": "python", "args": ["mcp_servers/hello_server.py"]},
    })
    overlay = _write_cfg(tmp_path / "overlay.json", {
        "hello": {"url": "https://example.invalid/mcp",
                  "headers": {"X-API-Key": "k"}},
    })
    specs = load_mcp_servers(path=base, overlay_path=overlay)
    assert len(specs) == 1
    hello = specs[0]
    assert hello.url == "https://example.invalid/mcp"
    assert hello.headers == {"X-API-Key": "k"}
    assert hello.command is None and hello.args == []


def test_overlay_missing_file_is_base_only(tmp_path: Path):
    base = _write_cfg(tmp_path / "mcp.json", {
        "hello": {"command": "python", "args": ["mcp_servers/hello_server.py"]},
    })
    specs = load_mcp_servers(path=base, overlay_path=tmp_path / "nope.json")
    assert {s.name for s in specs} == {"hello"}


def test_overlay_only_when_base_missing(tmp_path: Path):
    """A deployment may define ALL servers privately: missing base +
    present overlay → overlay-only (not [])."""
    overlay = _write_cfg(tmp_path / "overlay.json", {
        "private": {"url": "https://example.invalid/mcp"},
    })
    specs = load_mcp_servers(path=tmp_path / "missing.json", overlay_path=overlay)
    assert {s.name for s in specs} == {"private"}


def test_hello_server_reverses_string():
    """Spawn the real hello MCP server over stdio via the same client
    the agent uses, call `reverse_string('hello')`, and assert it
    returns 'olleh'. No LLM involved."""
    async def run() -> object:
        hello = next(s for s in load_mcp_servers() if s.name == "hello")
        client = BasicMCPClient(hello.command, args=hello.args)
        spec = McpToolSpec(client=client)
        tools = await spec.to_tool_list_async()
        reverse = next(
            t for t in tools if "reverse_string" in t.metadata.name
        )
        return await reverse.acall(s="hello")

    result = asyncio.run(run())
    # result is a ToolOutput; extract the text from the MCP CallToolResult.
    text = result.raw_output.content[0].text
    assert text == "olleh", f"got {result!r}"


def test_hello_server_base64_encodes_string():
    """base64-encode tool over the real hello MCP server: `hello`
    should encode to `aGVsbG8=` (the standard base64 of the UTF-8
    bytes). No LLM involved."""
    async def run() -> object:
        hello = next(s for s in load_mcp_servers() if s.name == "hello")
        client = BasicMCPClient(hello.command, args=hello.args)
        spec = McpToolSpec(client=client)
        tools = await spec.to_tool_list_async()
        encode = next(
            t for t in tools if "base64_encode" in t.metadata.name
        )
        return await encode.acall(s="hello")

    result = asyncio.run(run())
    text = result.raw_output.content[0].text
    assert text == "aGVsbG8=", f"got {result!r}"


def test_hello_server_base64_decodes_string():
    """base64-decode tool over the real hello MCP server: `aGVsbG8=`
    should decode back to `hello`. No LLM involved."""
    async def run() -> object:
        hello = next(s for s in load_mcp_servers() if s.name == "hello")
        client = BasicMCPClient(hello.command, args=hello.args)
        spec = McpToolSpec(client=client)
        tools = await spec.to_tool_list_async()
        decode = next(
            t for t in tools if "base64_decode" in t.metadata.name
        )
        return await decode.acall(s="aGVsbG8=")

    result = asyncio.run(run())
    text = result.raw_output.content[0].text
    assert text == "hello", f"got {result!r}"
