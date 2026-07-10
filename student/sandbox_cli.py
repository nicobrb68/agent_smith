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


def _discover_mcp_tools_sync(
    command: str | None, url: str | None
) -> Dict[str, Callable]:
    if not command and not url:
        return {}
    return asyncio.run(_discover_mcp_tools_async(command, url))


async def _discover_mcp_tools_async(
    command: str | None, url: str | None
) -> Dict[str, Callable]:
    tools: Dict[str, Callable] = {}

    if command:
        from mcp import stdio_client, StdioServerParameters
        from mcp.client.session import ClientSession
        import shlex

        parts = shlex.split(command)
        params = StdioServerParameters(
            command=parts[0], args=parts[1:], stderr=sys.stderr
        )
        async with stdio_client(params) as (r, w):
            async with ClientSession(r, w) as session:
                await session.initialize()
                result = await session.list_tools()
                for t in result.tools:
                    _make_tool_wrapper(tools, t.name, t.inputSchema)

    elif url:
        from mcp import ClientSession
        from mcp.client.sse import sse_client

        async with sse_client(url) as (r, w):
            async with ClientSession(r, w) as session:
                await session.initialize()
                result = await session.list_tools()
                for t in result.tools:
                    _make_tool_wrapper(tools, t.name, t.inputSchema)

    return tools


def _make_tool_wrapper(
    tools: Dict[str, Callable],
    name: str,
    schema: Dict[str, Any],
) -> None:
    props = schema.get("properties", {})
    param_names = list(props.keys())

    def wrapper(*args: Any, **kwargs: Any) -> str:
        call_kwargs = dict(zip(param_names, args))
        call_kwargs.update(kwargs)
        return json.dumps(call_kwargs)

    wrapper.__name__ = name
    wrapper.__doc__ = f"MCP tool: {name}({', '.join(param_names)})"
    tools[name] = wrapper


def _repl(sandbox: Sandbox, tools: Dict[str, Callable]) -> None:
    print("Agent Smith Sandbox CLI")
    print("Type Python code to execute. Empty line to submit, Ctrl+D to exit.")
    if tools:
        print(f"Available MCP tools: {', '.join(tools.keys())}")
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

        code = "\n".join(lines).strip()
        if not code:
            continue

        result = sandbox.execute_code(code, injected_tools=tools or None)
        output = result.get("output", "")
        if output:
            print(output)
        if result.get("is_final"):
            print(f"\n[final_answer]: {result.get('solution', '')}")
        if not result.get("success") and not result.get("is_final"):
            print("[execution failed]")
        print()


def main() -> None:
    parser = argparse.ArgumentParser(description="Sandbox CLI")
    parser.add_argument(
        "config", nargs="?", default=None,
        help="Path to a JSON sandbox config file"
    )
    parser.add_argument(
        "--mcp-stdio", default=None,
        help="Command to launch an MCP server via stdio"
    )
    parser.add_argument(
        "--mcp-server", default=None,
        help="URL of an MCP server (HTTP/SSE)"
    )
    args = parser.parse_args()

    config = _load_config(args.config)
    sandbox = Sandbox(config)

    tools = _discover_mcp_tools_sync(args.mcp_stdio, args.mcp_server)
    _repl(sandbox, tools)


if __name__ == "__main__":
    main()
