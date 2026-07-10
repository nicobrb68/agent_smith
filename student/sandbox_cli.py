"""Interactive sandbox CLI.

Usage:
    uv run sandbox                          # interactive REPL, default config
    uv run sandbox config.json              # interactive REPL, custom config
    uv run sandbox --mcp-stdio "command"    # connect MCP server via stdio
    uv run sandbox --mcp-server <URL>       # connect MCP server via HTTP
"""
import argparse
import asyncio
import json
import sys
import threading
from typing import Any, Callable, Dict, List

from student.sandbox import Sandbox
from student.sandbox_config import SandboxConfig


def _load_config(path: str | None) -> SandboxConfig:
    if path:
        try:
            with open(path, "r", encoding="utf-8") as f:
                return SandboxConfig(**json.load(f))
        except Exception as e:
            print(f"Failed to load {path}: {e}. Using defaults.")
    return SandboxConfig()


class _McpConnection:
    """Keeps an MCP session alive in a background event loop."""

    def __init__(self) -> None:
        self._loop: asyncio.AbstractEventLoop = (
            asyncio.new_event_loop()
        )
        self._thread: threading.Thread = threading.Thread(
            target=self._loop.run_forever, daemon=True
        )
        self._thread.start()
        self._session: Any = None
        self._transport_cm: Any = None
        self._session_cm: Any = None
        self.tool_list: List[Any] = []

    def connect_stdio(self, command: str) -> None:
        """Connect to an MCP server via stdio transport."""
        future = asyncio.run_coroutine_threadsafe(
            self._do_connect_stdio(command), self._loop
        )
        future.result(timeout=30)

    async def _do_connect_stdio(self, command: str) -> None:
        from mcp import StdioServerParameters, stdio_client
        from mcp.client.session import ClientSession
        import shlex

        parts = shlex.split(command)
        params = StdioServerParameters(
            command=parts[0],
            args=parts[1:],
            stderr=sys.stderr,
        )
        self._transport_cm = stdio_client(params)
        r, w = await self._transport_cm.__aenter__()
        self._session_cm = ClientSession(r, w)
        self._session = await self._session_cm.__aenter__()
        await self._session.initialize()
        result = await self._session.list_tools()
        self.tool_list = result.tools

    def connect_sse(self, url: str) -> None:
        """Connect to an MCP server via HTTP/SSE transport."""
        future = asyncio.run_coroutine_threadsafe(
            self._do_connect_sse(url), self._loop
        )
        future.result(timeout=30)

    async def _do_connect_sse(self, url: str) -> None:
        from mcp import ClientSession
        from mcp.client.sse import sse_client

        self._transport_cm = sse_client(url)
        r, w = await self._transport_cm.__aenter__()
        self._session_cm = ClientSession(r, w)
        self._session = await self._session_cm.__aenter__()
        await self._session.initialize()
        result = await self._session.list_tools()
        self.tool_list = result.tools

    def call_tool(
        self, name: str, arguments: Dict[str, Any]
    ) -> str:
        """Call an MCP tool and return its text result."""
        future = asyncio.run_coroutine_threadsafe(
            self._session.call_tool(name, arguments=arguments),
            self._loop,
        )
        result = future.result(timeout=60)
        parts: List[str] = []
        for block in result.content:
            if hasattr(block, "text"):
                parts.append(block.text)
        return "\n".join(parts) if parts else str(result)

    def close(self) -> None:
        """Gracefully close the MCP session and transport."""
        async def _cleanup() -> None:
            try:
                if self._session_cm:
                    await self._session_cm.__aexit__(
                        None, None, None
                    )
            except Exception:
                pass
            try:
                if self._transport_cm:
                    await self._transport_cm.__aexit__(
                        None, None, None
                    )
            except Exception:
                pass

        future = asyncio.run_coroutine_threadsafe(
            _cleanup(), self._loop
        )
        try:
            future.result(timeout=5)
        except Exception:
            pass
        self._loop.call_soon_threadsafe(self._loop.stop)


def _make_tool_wrapper(
    tools: Dict[str, Callable],
    name: str,
    schema: Dict[str, Any],
    conn: _McpConnection,
) -> None:
    props = schema.get("properties", {})
    param_names = list(props.keys())

    def wrapper(*args: Any, **kwargs: Any) -> str:
        call_kwargs: Dict[str, Any] = dict(
            zip(param_names, args)
        )
        call_kwargs.update(kwargs)
        return conn.call_tool(name, call_kwargs)

    wrapper.__name__ = name
    wrapper.__doc__ = (
        f"MCP tool: {name}({', '.join(param_names)})"
    )
    tools[name] = wrapper


def _repl(sandbox: Sandbox, tools: Dict[str, Callable]) -> None:
    print("Agent Smith Sandbox CLI")
    print(
        "Type Python code to execute. "
        "Empty line to submit, Ctrl+D to exit."
    )
    if tools:
        print(
            f"Available MCP tools: {', '.join(tools.keys())}"
        )
    print()

    while True:
        lines: List[str] = []
        try:
            while True:
                prompt = ">>> " if not lines else "... "
                line = input(prompt)
                if line == "" and lines:
                    break
                lines.append(line)
        except EOFError:
            print("\nBye.")
            break
        except KeyboardInterrupt:
            print("\nKeyboardInterrupt — session ended.")
            break

        code = "\n".join(lines).strip()
        if not code:
            continue

        result = sandbox.execute_code(
            code, injected_tools=tools or None
        )
        output = result.get("output", "")
        if output:
            print(output)
        if result.get("is_final"):
            print(
                f"\n[final_answer]: {result.get('solution', '')}"
            )
        if not result.get("success") and not result.get(
            "is_final"
        ):
            print("[execution failed]")
        print()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Sandbox CLI"
    )
    parser.add_argument(
        "config",
        nargs="?",
        default=None,
        help="Path to a JSON sandbox config file",
    )
    parser.add_argument(
        "--mcp-stdio",
        default=None,
        help="Command to launch an MCP server via stdio",
    )
    parser.add_argument(
        "--mcp-server",
        default=None,
        help="URL of an MCP server (HTTP/SSE)",
    )
    args = parser.parse_args()

    config = _load_config(args.config)
    sandbox = Sandbox(config)

    conn: _McpConnection | None = None
    tools: Dict[str, Callable] = {}

    if args.mcp_stdio or args.mcp_server:
        conn = _McpConnection()
        try:
            if args.mcp_stdio:
                conn.connect_stdio(args.mcp_stdio)
            else:
                conn.connect_sse(args.mcp_server)
            for t in conn.tool_list:
                _make_tool_wrapper(
                    tools, t.name, t.inputSchema, conn
                )
        except Exception as e:
            print(f"MCP connection failed: {e}")
            conn.close()
            conn = None

    try:
        _repl(sandbox, tools)
    finally:
        if conn:
            conn.close()


if __name__ == "__main__":
    main()
