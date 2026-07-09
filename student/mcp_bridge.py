"""Generic MCP client bridge, shared by the sandbox CLI and both agents.

The subject requires that "the sandbox contains an MCP client that
connects to an external MCP server" and that "MCP tools must be
callable as Python functions from the sandbox". Concretely, that means
something has to own a live ``mcp.ClientSession`` and turn its tools
into plain, synchronous Python callables the ``Sandbox`` can inject.

MCP is an async protocol (``ClientSession.call_tool`` is a coroutine),
but ``Sandbox.execute_code``'s tool-dispatch loop is synchronous, and
callers differ (the ``sandbox`` CLI is plain sync code, while
``agent_swebench`` already runs inside ``asyncio.run``). Rather than
fight over who owns "the" event loop, ``MCPBridge`` runs its own
dedicated event loop on a background thread and exposes every tool as
a synchronous function that blocks on
``asyncio.run_coroutine_threadsafe(...).result()``. This works
identically from sync or async calling code.
"""
import asyncio
import shlex
import threading
from typing import Any, Callable, Dict, List, Optional

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

try:
    from mcp.client.streamable_http import streamablehttp_client
except ImportError:  # pragma: no cover - older mcp versions
    streamablehttp_client = None  # type: ignore[assignment]


class MCPBridge:
    """Owns one MCP ClientSession and exposes it as sync Python calls."""

    def __init__(self) -> None:
        """Start the dedicated background event loop thread."""
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self._loop.run_forever, daemon=True
        )
        self._thread.start()
        self._session: Optional[ClientSession] = None
        self._cm_stack: List[Any] = []
        self.tools: List[Dict[str, Any]] = []

    def _run(self, coro: Any) -> Any:
        """Run a coroutine on the background loop and block for its result."""
        return asyncio.run_coroutine_threadsafe(coro, self._loop).result()

    def connect_stdio(self, command: str, cwd: Optional[str] = None) -> None:
        """Spawn an MCP server as a subprocess and connect over stdio.

        Args:
            command: The full shell command line to launch the
                server, e.g. ``"python mcp_tools_mbpp.py"``.
            cwd: Optional working directory for the spawned server
                process (defaults to the current process's cwd).
        """
        parts = shlex.split(command)
        params = StdioServerParameters(
            command=parts[0], args=parts[1:], cwd=cwd
        )

        async def _connect() -> None:
            read_ctx = stdio_client(params)
            read_stream, write_stream = await read_ctx.__aenter__()
            self._cm_stack.append(read_ctx)

            session_ctx = ClientSession(read_stream, write_stream)
            session = await session_ctx.__aenter__()
            self._cm_stack.append(session_ctx)

            await session.initialize()
            self._session = session

            tools_result = await session.list_tools()
            self.tools = [t.model_dump() for t in tools_result.tools]

        self._run(_connect())

    def connect_http(self, url: str) -> None:
        """Connect to a remote MCP server over streamable HTTP.

        Args:
            url: The MCP server's base URL.
        """
        if streamablehttp_client is None:
            raise RuntimeError(
                "Streamable HTTP transport is not available in this "
                "version of the `mcp` package."
            )

        async def _connect() -> None:
            read_ctx = streamablehttp_client(url)
            read_stream, write_stream, _ = await read_ctx.__aenter__()
            self._cm_stack.append(read_ctx)

            session_ctx = ClientSession(read_stream, write_stream)
            session = await session_ctx.__aenter__()
            self._cm_stack.append(session_ctx)

            await session.initialize()
            self._session = session

            tools_result = await session.list_tools()
            self.tools = [t.model_dump() for t in tools_result.tools]

        self._run(_connect())

    def call_tool(self, name: str, arguments: Dict[str, Any]) -> str:
        """Call a connected MCP tool synchronously and return its text.

        Args:
            name: The MCP tool name.
            arguments: Keyword arguments for the tool call.

        Returns:
            str: The concatenated text content of the tool's result.

        Raises:
            RuntimeError: If no MCP session is currently connected.
        """
        if self._session is None:
            raise RuntimeError(
                "MCPBridge.call_tool: no MCP session connected. Call "
                "connect_stdio()/connect_http() first."
            )
        session = self._session

        async def _call() -> str:
            result = await session.call_tool(name, arguments)
            parts: List[str] = []
            for block in result.content:
                text = getattr(block, "text", None)
                if text is not None:
                    parts.append(text)
            if getattr(result, "isError", False) and not parts:
                parts.append(f"Error calling tool '{name}'.")
            return "\n".join(parts) if parts else ""

        return self._run(_call())

    def build_tool_proxies(self) -> Dict[str, Callable[..., Any]]:
        """Build one synchronous Python callable per tool.

        Accepts both positional and keyword arguments: positional
        args are mapped to parameter names using the tool's declared
        JSON schema property order, so existing prompts written for
        ``read_file(filepath, start_line, end_line)``-style calls
        keep working unchanged.

        Returns:
            Dict[str, Callable[..., Any]]: A mapping suitable for
            ``Sandbox.execute_code(..., injected_tools=...)``.
        """
        proxies: Dict[str, Callable[..., Any]] = {}
        for tool in self.tools:
            tool_name = tool["name"]
            schema = tool.get("inputSchema") or {}
            param_names = list((schema.get("properties") or {}).keys())

            def make_proxy(
                name: str, params: List[str]
            ) -> Callable[..., Any]:
                def proxy(*args: Any, **kwargs: Any) -> Any:
                    if len(args) > len(params):
                        raise TypeError(
                            f"{name}(...): too many positional "
                            f"arguments (expected at most "
                            f"{len(params)}: {params})."
                        )
                    merged = dict(zip(params, args))
                    merged.update(kwargs)
                    return self.call_tool(name, merged)
                return proxy

            proxies[tool_name] = make_proxy(tool_name, param_names)
        return proxies

    def build_manual(self) -> str:
        """Render a human-readable tool manual from the MCP schemas.

        Generated dynamically from whatever server is connected, so
        it automatically reflects that server's actual tools (subject
        section V.2.6) instead of a hardcoded list.

        Returns:
            str: A "tool_name(param: type, ...) -- description" block
            per available tool, ready to be embedded in a system
            prompt.
        """
        lines: List[str] = []
        for tool in self.tools:
            schema = tool.get("inputSchema") or {}
            props = schema.get("properties", {}) or {}
            required = set(schema.get("required", []) or [])
            params = ", ".join(
                f"{p}: {info.get('type', 'any')}"
                + ("" if p in required else " = ...")
                for p, info in props.items()
            )
            lines.append(f"- {tool['name']}({params})")
            description = (tool.get("description") or "").strip()
            if description:
                first_line = description.splitlines()[0].strip()
                lines.append(f"    {first_line}")
        lines.append("- final_answer(answer: str)")
        lines.append(
            "    Built-in sandbox construct (NOT an MCP tool): call "
            "it to submit your final solution and end the loop."
        )
        return "\n".join(lines)

    def close(self) -> None:
        """Tear down the MCP session/transport and stop the background loop."""

        async def _close() -> None:
            while self._cm_stack:
                ctx = self._cm_stack.pop()
                try:
                    await ctx.__aexit__(None, None, None)
                except Exception:
                    pass

        try:
            self._run(_close())
        finally:
            self._loop.call_soon_threadsafe(self._loop.stop)
            self._thread.join(timeout=2)
